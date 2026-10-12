"""Email do condomínio via Gmail (entrega 4), só de leitura e sob pedido.

- Ligação OAuth feita pelo administrador: o Google envia de volta um código que é
  trocado por tokens guardados no servidor. O âmbito é `gmail.readonly`: a
  aplicação nunca envia, apaga ou altera email.
- Nada é sincronizado automaticamente: as listas e os corpos são pedidos ao Gmail
  quando a página é aberta; o botão "Atualizar" repete esse pedido.
- O corpo HTML é apresentado sanitizado: sem scripts, sem atributos, sem
  carregamento automático de conteúdo remoto.
- Anexos são descarregados um a um, com limite de tamanho; PDF abre no navegador.
- "Guardar como documento" cria uma cópia local (ver `documentos.py`) com a
  referência à origem; consultar email não cria movimentos financeiros.

Os pedidos HTTP passam todos por `pedido_json`/`pedido_binario`, para que os
testes os simulem sem tocar em serviços reais.
"""

from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import bleach
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from markupsafe import Markup

from .auth import recusar_se_nao_for_administrador
from .concurrency import revisao_integracoes
from .db import utc_now_iso
from .integracoes import (
    ESCOPO_GMAIL,
    gmail_configurado,
    gmail_ligado,
    obter_integracao,
    uri_redirecionamento,
    guardar_integracao,
    apagar_integracao,
)
from .units import parse_date

bp = Blueprint("email", __name__)


@bp.before_request
def _apenas_administrador():
    return recusar_se_nao_for_administrador()


AUTORIDADE_GOOGLE = "https://accounts.google.com"
TOKEN_GOOGLE = "https://oauth2.googleapis.com"
API_GMAIL = "https://gmail.googleapis.com/gmail/v1/users"

MAX_ANEXO_BYTES = 10 * 1024 * 1024  # 10 MB por anexo
MENSAGENS_POR_PAGINA = 20
LIMITE_CORPO_BYTES = 300_000

# Tempo limite por tipo de pedido (s): um Gmail lento não prende a página.
TIMEOUT_TOKEN = 10
TIMEOUT_LISTA = 12
TIMEOUT_METADATA = 8
TIMEOUT_MENSAGEM = 15
TIMEOUT_ANEXO = 30
# Só 4 pedidos em simultâneo: educado com a API e suficiente para uma página.
PEDIDOS_EM_SIMULTANEO = 4

# Cache curta, só em memória e por conta: repete o pedido ao Gmail dentro da
# janela; limpa ao desligar, ao trocar de conta ou com 「Atualizar」.
CACHE_LISTA_SEGUNDOS = 60
CACHE_ANEXOS_SEGUNDOS = 600
_cache_lock = threading.Lock()
_cache_lista: dict[tuple, tuple[float, dict]] = {}
_cache_anexos: dict[tuple[str, str], tuple[float, list[dict]]] = {}

# HTML apresentado: formatação básica, sem atributos nem conteúdo remoto.
TAGS_PERMITIDAS = {
    "p", "br", "b", "strong", "i", "em", "u", "s",
    "ul", "ol", "li", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
    "div", "span", "h1", "h2", "h3", "h4",
}


# ---------------------------------------------------------------------------
# Transporte HTTP (os testes substituem estas funções)
# ---------------------------------------------------------------------------


_transport_local = threading.local()


def _sessao_http():
    """Sessão requests por thread: reaproveita ligações TLS (keep-alive)."""
    sessao = getattr(_transport_local, "sessao", None)
    if sessao is None:
        import requests

        sessao = requests.Session()
        adaptador = requests.adapters.HTTPAdapter(
            pool_connections=PEDIDOS_EM_SIMULTANEO, pool_maxsize=PEDIDOS_EM_SIMULTANEO * 2
        )
        sessao.mount("https://", adaptador)
        _transport_local.sessao = sessao
    return sessao


def pedido_json(url: str, *, metodo: str = "GET", headers: dict | None = None,
                payload: dict | None = None, timeout: int = 20) -> tuple[int, dict]:
    """Pedido JSON; devolve (estado, corpo como dict; vazio se não for JSON)."""
    import requests

    try:
        resposta = _sessao_http().request(
            metodo, url, headers=headers or {}, json=payload, timeout=timeout
        )
    except requests.RequestException:
        return 0, {}
    try:
        corpo = resposta.json()
    except ValueError:
        corpo = {}
    return resposta.status_code, corpo


def pedido_binario(url: str, *, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    import requests

    try:
        resposta = _sessao_http().get(url, headers=headers or {}, timeout=timeout)
    except requests.RequestException:
        return 0, b""
    return resposta.status_code, resposta.content


# ---------------------------------------------------------------------------
# Cache curta por conta (só em memória; nunca vai ao disco)
# ---------------------------------------------------------------------------


def _limpar_caches() -> None:
    with _cache_lock:
        _cache_lista.clear()
        _cache_anexos.clear()


def _cache_lista_obter(chave: tuple) -> dict | None:
    with _cache_lock:
        guardado = _cache_lista.get(chave)
    if guardado is None:
        return None
    instante, dados = guardado
    if time.monotonic() - instante > CACHE_LISTA_SEGUNDOS:
        return None
    return dados


def _cache_lista_guardar(chave: tuple, dados: dict) -> None:
    with _cache_lock:
        _cache_lista[chave] = (time.monotonic(), dados)


def _cache_anexos_obter(conta: str, mensagem_id: str) -> list[dict] | None:
    with _cache_lock:
        guardado = _cache_anexos.get((conta, mensagem_id))
    if guardado is None:
        return None
    instante, anexos = guardado
    if time.monotonic() - instante > CACHE_ANEXOS_SEGUNDOS:
        return None
    return anexos


def _cache_anexos_guardar(conta: str, mensagem_id: str, anexos: list[dict]) -> None:
    with _cache_lock:
        _cache_anexos[(conta, mensagem_id)] = (time.monotonic(), anexos)


# ---------------------------------------------------------------------------
# Tokens e cabeçalhos
# ---------------------------------------------------------------------------


def _cabecalho_autorizacao() -> str | None:
    """Access token válido, renovando-o com o refresh token quando expirou."""
    token = obter_integracao("gmail_access_token")
    expira = obter_integracao("gmail_access_token_expira", "0")
    try:
        expira_em = int(expira)
    except ValueError:
        expira_em = 0
    if token and expira_em > _agora() + 30:
        return token

    refresh = obter_integracao("gmail_refresh_token")
    if not refresh:
        return None
    estado, corpo = pedido_json(
        f"{TOKEN_GOOGLE}/token",
        metodo="POST",
        payload={
            "client_id": obter_integracao("gmail_client_id"),
            "client_secret": obter_integracao("gmail_client_secret"),
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT_TOKEN,
    )
    if estado != 200 or "access_token" not in corpo:
        return None
    guardar_integracao("gmail_access_token", str(corpo["access_token"]))
    guardar_integracao("gmail_access_token_expira", str(_agora() + int(corpo.get("expires_in", 3600))))
    return str(corpo["access_token"])


def _agora() -> int:
    import time

    return int(time.time())


def _recusar_se_nao_estiver_ligado() -> bool:
    if not gmail_ligado():
        flash("O Gmail ainda não está ligado: liga a conta em Definições → Integrações.", "erro")
        return True
    return False


# ---------------------------------------------------------------------------
# Ligar, testar e desligar a conta
# ---------------------------------------------------------------------------


@bp.route("/email/oauth/ligar", methods=["POST"])
def oauth_ligar():
    if not gmail_configurado():
        flash("Configura primeiro as credenciais OAuth do Gmail em Definições → Integrações.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    estado_csrf = secrets.token_urlsafe(16)
    session["gmail_oauth_state"] = estado_csrf
    parametros = urlencode({
        "response_type": "code",
        "client_id": obter_integracao("gmail_client_id"),
        "redirect_uri": uri_redirecionamento(),
        "scope": ESCOPO_GMAIL,
        "access_type": "offline",
        "prompt": "consent",
        "state": estado_csrf,
    })
    return redirect(f"{AUTORIDADE_GOOGLE}/o/oauth2/v2/auth?{parametros}")


@bp.route("/email/oauth/callback")
def oauth_callback():
    esperado = session.pop("gmail_oauth_state", "")
    estado_recebido = request.args.get("state", "")
    erro = request.args.get("error", "")
    if erro:
        flash(f"O Google recusou a ligação: {erro}. Nada foi alterado.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    if not esperado or esperado != estado_recebido:
        flash("Este regresso do Google não corresponde a uma ligação iniciada por ti. Nada foi feito.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    codigo = request.args.get("code", "")
    if not codigo:
        flash("O Google não devolveu o código de autorização. Tenta ligar outra vez.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    estado, corpo = pedido_json(
        f"{TOKEN_GOOGLE}/token",
        metodo="POST",
        payload={
            "code": codigo,
            "client_id": obter_integracao("gmail_client_id"),
            "client_secret": obter_integracao("gmail_client_secret"),
            "redirect_uri": uri_redirecionamento(),
            "grant_type": "authorization_code",
        },
    )
    if estado != 200 or "access_token" not in corpo:
        flash("Não foi possível trocar o código por tokens. Verifica as credenciais e tenta de novo.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    guardar_integracao("gmail_access_token", str(corpo["access_token"]))
    guardar_integracao("gmail_access_token_expira", str(_agora() + int(corpo.get("expires_in", 3600))))
    if corpo.get("refresh_token"):
        # O Google só devolve refresh token no primeiro consentimento; sem ele,
        # o já guardado continua a servir.
        guardar_integracao("gmail_refresh_token", str(corpo["refresh_token"]))
    guardar_integracao("gmail_ligado_em", utc_now_iso())
    _limpar_caches()  # conta nova (ou re-ligação): nada de resultados antigos
    flash("Conta ligada. Usa 「Testar ligação」 para confirmar a conta e o acesso de leitura.", "sucesso")
    return redirect(url_for("integracoes.integracoes"))


@bp.route("/email/testar", methods=["POST"])
def testar_ligacao():
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada: o acesso deixou de ser válido. Volta a ligar a conta.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    estado, corpo = pedido_json(f"{API_GMAIL}/me/profile", headers={"Authorization": f"Bearer {cabecalho}"})
    if estado != 200:
        flash(f"O teste falhou (estado {estado or 'sem resposta'}). O Gmail não confirmou o acesso.", "erro")
        return redirect(url_for("integracoes.integracoes"))
    conta = str(corpo.get("emailAddress") or corpo.get("email") or "")
    guardar_integracao("gmail_conta", conta)
    guardar_integracao("gmail_ultimo_teste", utc_now_iso())
    flash(f"Ligação confirmada: {conta or 'conta Gmail'} (só de leitura).", "sucesso")
    return redirect(url_for("integracoes.integracoes"))


@bp.route("/email/desligar", methods=["POST"])
def desligar():
    token = obter_integracao("gmail_access_token")
    if token:
        # Melhor esforço: revogar no Google. A desligação local acontece sempre.
        pedido_json(f"{TOKEN_GOOGLE}/revoke?{urlencode({'token': token})}", metodo="POST", timeout=10)
    for chave in ("gmail_access_token", "gmail_access_token_expira", "gmail_refresh_token",
                  "gmail_conta", "gmail_ligado_em", "gmail_ultimo_teste"):
        apagar_integracao(chave)
    _limpar_caches()  # ao desligar, nenhum excerto fica em memória
    flash("Conta desligada deste servidor. As credenciais OAuth conservam-se para voltar a ligar.", "sucesso")
    return redirect(url_for("integracoes.integracoes"))


# ---------------------------------------------------------------------------
# Lista de emails
# ---------------------------------------------------------------------------


def _filtros_query(args) -> str:
    partes: list[str] = []
    texto = (args.get("q") or "").strip()
    if texto:
        partes.append(texto)
    de = (args.get("de") or "").strip()
    ate = (args.get("ate") or "").strip()
    from .units import parse_date

    if de:
        data = parse_date(de)
        if data:
            partes.append(f"after:{data.replace('-', '/')}")
    if ate:
        data = parse_date(ate)
        if data:
            partes.append(f"before:{data.replace('-', '/')}")
    return " ".join(partes)


def _linha_da_mensagem(item: dict, cabecalho: str) -> dict | None:
    """Remetente, assunto e data de uma mensagem — só os cabeçalhos pedidos.

    `messages.get` com `format=metadata` e `metadataHeaders` (parâmetros
    documentados): o corpo e os anexos nunca passam por aqui.
    """
    estado_msg, msg = pedido_json(
        f"{API_GMAIL}/me/messages/{item['id']}?format=metadata"
        "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
        headers={"Authorization": f"Bearer {cabecalho}"},
        timeout=TIMEOUT_METADATA,
    )
    if estado_msg != 200:
        return None
    cabecalhos = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                  for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": str(msg.get("id", item["id"])),
        "remetente": cabecalhos.get("from", ""),
        "assunto": cabecalhos.get("subject", "(sem assunto)"),
        "data_texto": cabecalhos.get("date", ""),
        "anexo": bool(cabecalhos.get("x-attachments") or cabecalhos.get("attachments")),
    }


def _motivo_erro_gmail(corpo: dict) -> str:
    """Motivo estruturado de uma recusa, a partir do corpo `error` da resposta.

    A mensagem de erro do Google descreve o pedido (ex.: seleção de campos
    inválida); nunca contém tokens nem conteúdo de email. É limitada por
    segurança e registada sem URL nem cabeçalhos.
    """
    erro = corpo.get("error") if isinstance(corpo, dict) else None
    if not isinstance(erro, dict):
        return ""
    partes: list[str] = []
    status = str(erro.get("status") or "").strip()
    if status:
        partes.append(status)
    mensagem = " ".join(str(erro.get("message") or "").split())
    if mensagem:
        partes.append(mensagem[:200])
    return " — ".join(partes)


def _resultados_email(cabecalho: str, query: str, parametros: dict,
                      chave_cache: tuple, usar_cache: bool):
    """Gerador: a estrutura da página já foi enviada; aqui vêm os resultados.

    Cache curta por conta → repetições não repetem pedidos ao Gmail. A listagem
    só pede `id + threadId` (o contrato documentado de `users.messages.list`)
    e os cabeçalhos de cada mensagem em concorrência limitada. O estado de
    carregamento termina tanto no sucesso como no erro.
    """
    def _bloco(erro: str, dados: dict) -> Markup:
        # O partial já escapou os dados externos; Markup aqui marca apenas o
        # HTML gerado pelos nossos templates como confiável — sem |safe no
        # conteúdo. (No Jinja 3.1, render devolve str simples.)
        return Markup(render_template(
            "email_list_resultados.html",
            erro=erro,
            consulta=query,
            mensagens=dados["linhas"],
            pagina_seguinte=dados["pagina_seguinte"],
            total_estimado=dados["total_estimado"],
            falhas=dados.get("falhas", 0),
        ))

    dados = _cache_lista_obter(chave_cache) if usar_cache else None
    if dados is None:
        yield Markup('<p id="a-carregar" class="nota a-carregar" role="status">'
                     'A consultar o Gmail…</p>')
        parametros = dict(parametros)
        # Contrato documentado da listagem: só id + threadId por mensagem.
        # Pedir `snippet` ou `prevPageToken` nos `fields` (que a resposta não
        # contém) é o que leva o Gmail a recusar o pedido com 400.
        parametros["fields"] = "messages(id,threadId),nextPageToken,resultSizeEstimate"
        try:
            estado, corpo = pedido_json(
                f"{API_GMAIL}/me/messages?{urlencode(parametros)}",
                headers={"Authorization": f"Bearer {cabecalho}"},
                timeout=TIMEOUT_LISTA,
            )
        except Exception:
            current_app.logger.exception("gmail: o pedido da lista falhou a meio do fluxo")
            yield _bloco("O Gmail não respondeu: o pedido falhou. Tenta 「Atualizar」.",
                         {"linhas": [], "pagina_seguinte": "", "total_estimado": None, "falhas": 0})
            return
        if estado != 200:
            motivo = _motivo_erro_gmail(corpo)
            current_app.logger.warning("gmail: lista recusada (estado %s): %s",
                                       estado, motivo or "sem motivo estruturado na resposta")
            yield _bloco(
                f"O Gmail recusou o pedido (estado {estado or 'sem resposta'}). "
                + (f"{motivo} " if motivo else "")
                + "A lista não foi carregada. Tenta 「Atualizar」.",
                {"linhas": [], "pagina_seguinte": "", "total_estimado": None, "falhas": 0},
            )
            return
        itens = list(corpo.get("messages", []))
        linhas: list[dict | None] = []
        try:
            if itens:
                with ThreadPoolExecutor(max_workers=PEDIDOS_EM_SIMULTANEO) as executor:
                    linhas = list(
                        executor.map(lambda item: _linha_da_mensagem(item, cabecalho), itens)
                    )
        except Exception:
            current_app.logger.exception("gmail: os detalhes das mensagens falharam a meio do fluxo")
            yield _bloco("O Gmail não respondeu: os detalhes das mensagens falharam. "
                         "Tenta 「Atualizar」.",
                         {"linhas": [], "pagina_seguinte": "", "total_estimado": None, "falhas": 0})
            return
        dados = {
            "linhas": [linha for linha in linhas if linha is not None],
            "falhas": sum(1 for linha in linhas if linha is None),
            "pagina_seguinte": str(corpo.get("nextPageToken") or ""),
            "total_estimado": corpo.get("resultSizeEstimate"),
        }
        _cache_lista_guardar(chave_cache, dados)
    yield _bloco("", dados)


@bp.route("/email")
def email_lista():
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada: o acesso deixou de ser válido. Volta a ligar a conta.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    query = _filtros_query(request.args)
    parametros = {"maxResults": MENSAGENS_POR_PAGINA}
    if query:
        parametros["q"] = query
    pagina = (request.args.get("pagina") or "").strip()
    if pagina:
        parametros["pageToken"] = pagina
    usar_cache = request.args.get("atualizar") != "1"
    chave_cache = (obter_integracao("gmail_conta"), tuple(sorted(parametros.items())))

    # A estrutura da página (título, filtros, navegação) é enviada antes dos
    # resultados: numa rede lenta o utilizador vê a página e o aviso de
    # carregamento em vez de um ecrã parado.
    from .auth import current_user
    from .security import csrf_token

    template = current_app.jinja_env.get_template("email_list.html")
    fluxo = template.stream(
        {
            "consulta": query,
            "resultados": _resultados_email(cabecalho, query, parametros, chave_cache, usar_cache),
            "revisao_integracoes": revisao_integracoes,
            "csrf_token": csrf_token,
            "utilizador_atual": current_user(),
        }
    )
    return Response(stream_with_context(fluxo), mimetype="text/html")


# ---------------------------------------------------------------------------
# Mensagem: corpo sanitizado, conversa e anexos
# ---------------------------------------------------------------------------


def _descodificar(dados: str) -> bytes:
    return base64.urlsafe_b64decode((dados or "") + "==")


def _corpo_da_mensagem(msg: dict) -> tuple[str, str]:
    """(texto, tipo) do corpo: texto simples sempre; HTML só sanitizado."""
    payload = msg.get("payload", {})

    def procurar(partes, tipo_procurado: str) -> str | None:
        for parte in partes or []:
            mime = str(parte.get("mimeType", ""))
            dados = (parte.get("body") or {}).get("data")
            if dados and mime == tipo_procurado:
                return _descodificar(dados).decode("utf-8", errors="replace")
            filho = procurar(parte.get("parts"), tipo_procurado)
            if filho:
                return filho
        return None

    texto = procurar(payload.get("parts"), "text/plain")
    if texto:
        return texto, "text/plain"
    html = procurar(payload.get("parts"), "text/html")
    if html:
        # Sem atributos permitidos: scripts, estilos e imagens remotas desaparecem;
        # nada é carregado automaticamente a partir do conteúdo do email.
        # script/style e o seu conteúdo desaparecem antes do bleach: nem texto de
        # scripts sobra apresentado.
        html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
        limpo = bleach.clean(
            html,
            tags=sorted(TAGS_PERMITIDAS),
            attributes=lambda tag, attr, value: False,  # zero atributos: sem links, estilos nem eventos
            strip=True,  # tags não permitidas desaparecem; não ficam como texto escapado
            strip_comments=True,
        )
        return limpo, "text/html-sanitizado"
    return "", ""


@bp.route("/email/<mensagem_id>")
def email_mensagem(mensagem_id: str):
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    estado, msg = pedido_json(
        f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
        headers={"Authorization": f"Bearer {cabecalho}"},
        timeout=TIMEOUT_MENSAGEM,
    )
    if estado != 200:
        flash(f"O Gmail não devolveu esta mensagem (estado {estado or 'sem resposta'}).", "erro")
        return redirect(url_for("email.email_lista"))

    cabecalhos = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                  for h in msg.get("payload", {}).get("headers", [])}
    corpo, tipo_corpo = _corpo_da_mensagem(msg)
    # Uma resposta sem `payload` é uma falha de leitura, não "sem anexos".
    estrutura_ok = isinstance(msg.get("payload"), dict)
    erro_estrutura = "" if estrutura_ok else (
        "A resposta do Gmail não inclui a estrutura esperada desta mensagem; "
        "os anexos não puderam ser verificados."
    )
    anexos = _anexos_da_mensagem(msg) if estrutura_ok else []
    # Os nomes/tipos dos anexos ficam em cache curto: abrir um anexo deixa de
    # exigir nova descarga da mensagem inteira.
    if estrutura_ok:
        _cache_anexos_guardar(obter_integracao("gmail_conta"), mensagem_id, anexos)

    conversa: list[dict] = []
    erro_conversa = ""
    thread_id = str(msg.get("threadId", ""))
    if thread_id and thread_id != mensagem_id:
        estado_t, thread = pedido_json(
            f"{API_GMAIL}/me/threads/{thread_id}?format=full",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_MENSAGEM,
        )
        if estado_t == 200:
            for item in thread.get("messages", []):
                cab = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                       for h in item.get("payload", {}).get("headers", [])}
                conversa.append({
                    "id": str(item.get("id", "")),
                    "remetente": cab.get("from", ""),
                    "assunto": cab.get("subject", ""),
                    "data_texto": cab.get("date", ""),
                    "atual": item.get("id") == mensagem_id,
                    # O Gmail agrupa mensagens: o anexo pode estar noutra
                    # mensagem da conversa. Cada mensagem mostra os seus.
                    "anexos": len(_anexos_da_mensagem(item)),
                })
        else:
            erro_conversa = (
                f"A conversa não pôde ser lida (estado {estado_t or 'sem resposta'}); "
                "pode haver mais mensagens nesta conversa."
            )

    return render_template(
        "email_message.html",
        mensagem_id=mensagem_id,
        thread_id=thread_id,
        assunto=cabecalhos.get("subject", "(sem assunto)"),
        remetente=cabecalhos.get("from", ""),
        destinatarios=cabecalhos.get("to", ""),
        data_texto=cabecalhos.get("date", ""),
        corpo=corpo[:LIMITE_CORPO_BYTES],
        tipo_corpo=tipo_corpo,
        corpo_truncado=len(corpo) > LIMITE_CORPO_BYTES,
        anexos=anexos,
        erro_estrutura=erro_estrutura,
        erro_conversa=erro_conversa,
        conversa=conversa,
        revisao_integracoes=revisao_integracoes,
    )


def _anexos_da_mensagem(msg: dict) -> list[dict]:
    """Anexos de uma mensagem, percorrendo a estrutura MIME recursivamente.

    Estrutura documentada de `format=full`: cada parte tem `filename` e
    `body.attachmentId`/`body.size`; partes pequenas de texto trazem
    `body.data`. Não existe objeto `body.attachment` — procurar apenas esse
    era o motivo de os anexos reais nunca aparecerem. São também tratados:
    anexo na raiz do `payload` (mensagem de uma só parte) e partes inline
    com bytes diretamente em `body.data` (sem `attachmentId`), identificadas
    por `parte:<caminho>` e servidas a partir do próprio payload.
    """
    anexos: list[dict] = []

    def tratar(parte: dict, caminho: str):
        corpo = parte.get("body") or {}
        mime = str(parte.get("mimeType", ""))
        nome = str(parte.get("filename", "") or "")
        attachment_id = str(corpo.get("attachmentId", "") or "")
        tamanho = int(corpo.get("size", 0) or 0)
        if attachment_id:
            anexos.append({
                "attachment_id": attachment_id,
                "nome": nome or "(sem nome)",
                "mime": mime,
                "tamanho": tamanho,
            })
        elif corpo.get("data") and nome and mime not in ("text/plain", "text/html"):
            anexos.append({
                "attachment_id": f"parte:{caminho}",
                "nome": nome,
                "mime": mime,
                "tamanho": tamanho,
            })
        for i, filho in enumerate(parte.get("parts") or []):
            tratar(filho, f"{caminho}.{i}" if caminho else str(i))

    payload = msg.get("payload") or {}
    tratar({"mimeType": payload.get("mimeType", ""),
            "filename": payload.get("filename", ""),
            "body": payload.get("body") or {},
            "parts": payload.get("parts")}, "")
    return anexos


def _bytes_de_parte_inline(msg: dict, caminho: str) -> bytes | None:
    """Bytes de uma parte inline pelo caminho na estrutura MIME (ex.: '1.0')."""
    alvo = msg.get("payload") or {}
    if caminho:
        for indice in caminho.split("."):
            try:
                alvo = (alvo.get("parts") or [])[int(indice)]
            except (ValueError, IndexError, TypeError, AttributeError):
                return None
    dados = (alvo.get("body") or {}).get("data")
    return _descodificar(str(dados)) if dados else None


def _metadado_anexo(cabecalho: str, mensagem_id: str, anexo_id: str) -> dict | None:
    """Nome e tipo de um anexo: primeiro a cache curta; só depois a mensagem completa."""
    conta = obter_integracao("gmail_conta")
    anexos = _cache_anexos_obter(conta, mensagem_id)
    if anexos is None:
        _, msg = pedido_json(
            f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_MENSAGEM,
        )
        anexos = _anexos_da_mensagem(msg)
        _cache_anexos_guardar(conta, mensagem_id, anexos)
    return next((a for a in anexos if a["attachment_id"] == anexo_id), None)


@bp.route("/email/<mensagem_id>/anexo/<anexo_id>")
def email_anexo(mensagem_id: str, anexo_id: str):
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    if anexo_id.startswith("parte:"):
        # Conteúdo inline: os bytes vêm do payload da mensagem, não do
        # endpoint de anexos (que só serve attachmentId).
        estado, msg = pedido_json(
            f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_MENSAGEM,
        )
        if estado != 200:
            abort(404, description="anexo não disponível")
        anexos = _anexos_da_mensagem(msg)
        _cache_anexos_guardar(obter_integracao("gmail_conta"), mensagem_id, anexos)
        anexo = next((a for a in anexos if a["attachment_id"] == anexo_id), None)
        dados = _bytes_de_parte_inline(msg, anexo_id[len("parte:"):]) if anexo else None
    else:
        estado, corpo = pedido_json(
            f"{API_GMAIL}/me/messages/{mensagem_id}/attachments/{anexo_id}",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_ANEXO,
        )
        if estado != 200 or "data" not in corpo:
            abort(404, description="anexo não disponível")
        dados = _descodificar(str(corpo.get("data", "")))
        anexo = _metadado_anexo(cabecalho, mensagem_id, anexo_id)

    if dados is None:
        abort(404, description="anexo não disponível")
    if len(dados) > MAX_ANEXO_BYTES:
        abort(413, description="anexo demasiado grande")

    # Nome e tipo vêm do metadado da mensagem (cache curto); PDF abre no
    # navegador, o resto descarrega.
    anexo = _metadado_anexo(cabecalho, mensagem_id, anexo_id)
    nome = anexo["nome"] if anexo else "anexo"
    mime = anexo["mime"] if anexo else ""
    inline = mime in ("application/pdf", "image/png", "image/jpeg", "text/plain")
    disposicao = "inline" if inline else "attachment"
    safe_nome = nome.replace('"', "").replace("\n", "") or "anexo"
    return Response(dados, mimetype=mime or "application/octet-stream",
                    headers={"Content-Disposition": f'{disposicao}; filename="{safe_nome}"'})


# ---------------------------------------------------------------------------
# Guardar email/anexo como documento local
# ---------------------------------------------------------------------------


@bp.route("/email/<mensagem_id>/guardar", methods=["POST"])
def email_guardar(mensagem_id: str):
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    estado, msg = pedido_json(
        f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
        headers={"Authorization": f"Bearer {cabecalho}"},
        timeout=TIMEOUT_MENSAGEM,
    )
    if estado != 200:
        flash("O Gmail não devolveu esta mensagem; nada foi guardado.", "erro")
        return redirect(url_for("email.email_lista"))

    cabecalhos = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                  for h in msg.get("payload", {}).get("headers", [])}
    corpo, tipo_corpo = _corpo_da_mensagem(msg)
    assunto = cabecalhos.get("subject", "(sem assunto)")
    texto = (
        f"Assunto: {assunto}\nDe: {cabecalhos.get('from', '')}\n"
        f"Para: {cabecalhos.get('to', '')}\nData: {cabecalhos.get('date', '')}\n"
        f"---\n\n{corpo}"
    )
    from .documentos import guardar_documento

    documento_id = guardar_documento(
        origem="email",
        dados=texto.encode("utf-8"),
        nome_original=f"{assunto[:80]}.txt",
        mime="text/plain",
        gmail_mensagem_id=mensagem_id,
        resumo=f"Email: {assunto}",
    )
    flash("Email guardado como documento local, com a referência à mensagem de origem.", "sucesso")
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/email/<mensagem_id>/anexo/<anexo_id>/guardar", methods=["POST"])
def email_anexo_guardar(mensagem_id: str, anexo_id: str):
    if _recusar_se_nao_estiver_ligado():
        return redirect(url_for("integracoes.integracoes"))
    cabecalho = _cabecalho_autorizacao()
    if cabecalho is None:
        flash("A ligação precisa de ser renovada.", "erro")
        return redirect(url_for("integracoes.integracoes"))

    if anexo_id.startswith("parte:"):
        # Conteúdo inline: os bytes vêm do payload da mensagem.
        estado, msg = pedido_json(
            f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_MENSAGEM,
        )
        anexos = _anexos_da_mensagem(msg) if estado == 200 else []
        if estado == 200:
            _cache_anexos_guardar(obter_integracao("gmail_conta"), mensagem_id, anexos)
        anexo = next((a for a in anexos if a["attachment_id"] == anexo_id), None)
        dados = _bytes_de_parte_inline(msg, anexo_id[len("parte:"):]) if anexo else None
        if dados is None:
            flash("O anexo não está disponível; nada foi guardado.", "erro")
            return redirect(url_for("email.email_mensagem", mensagem_id=mensagem_id))
    else:
        estado, corpo = pedido_json(
            f"{API_GMAIL}/me/messages/{mensagem_id}/attachments/{anexo_id}",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=TIMEOUT_ANEXO,
        )
        if estado != 200 or "data" not in corpo:
            flash("O anexo não está disponível; nada foi guardado.", "erro")
            return redirect(url_for("email.email_mensagem", mensagem_id=mensagem_id))
        dados = _descodificar(str(corpo.get("data", "")))
        anexo = _metadado_anexo(cabecalho, mensagem_id, anexo_id)

    if len(dados) > MAX_ANEXO_BYTES:
        flash("Anexo demasiado grande para guardar.", "erro")
        return redirect(url_for("email.email_mensagem", mensagem_id=mensagem_id))

    from .documentos import guardar_documento

    documento_id = guardar_documento(
        origem="anexo",
        dados=dados,
        nome_original=anexo["nome"] if anexo else "anexo",
        mime=anexo["mime"] if anexo else "",
        gmail_mensagem_id=mensagem_id,
        gmail_anexo_id=anexo_id,
        resumo=f"Anexo de email: {anexo['nome'] if anexo else 'anexo'}",
    )
    flash("Anexo guardado como documento local, com a referência à origem.", "sucesso")
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))
