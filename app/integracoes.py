"""Definições → Integrações (entrega 4), exclusivas do administrador.

- O administrador configura aqui as credenciais OAuth do Gmail e a API de IA
  (endereço base compatível com OpenAI, chave opcional, modelo e tempo limite).
- Nada de credenciais, endereços ou modelos fixados no código: tudo vem daqui.
- Os valores guardados em `integracoes` podem ser segredos: nunca são devolvidos
  ao navegador depois de guardados (as páginas mostram só o estado — "guardado",
  "ligado a …" — e a data da última atualização), nunca vão para a consola nem
  para o repositório. Um campo de segredo em branco significa "manter o que está";
  a caixa "apagar" remove o valor guardado.
- O Gmail é só de leitura (âmbito `gmail.readonly`) e exclusivo do condomínio:
  a conta é ligada por OAuth neste navegador, com teste e estado visíveis.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .concurrency import (
    MENSAGEM_DESATUALIZADO,
    desatualizado,
    revisao_integracoes,
    transacao_de_escrita,
)
from .db import execute_db, get_db, query_db, utc_now_iso

bp = Blueprint("integracoes", __name__)


@bp.before_request
def _apenas_administrador():
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()


# Chaves conhecidas. Segredos: gmail_client_secret, gmail_refresh_token,
# gmail_access_token, ia_chave_api. Nunca são apresentados no navegador.
CHAVES_GMAIL = (
    "gmail_client_id",
    "gmail_client_secret",
    "gmail_refresh_token",
    "gmail_access_token",
    "gmail_access_token_expira",
    "gmail_conta",
    "gmail_ligado_em",
    "gmail_ultimo_teste",
)
CHAVES_IA = (
    "ia_ativa",
    "ia_base_url",
    "ia_chave_api",
    "ia_modelo",
    "ia_timeout_segundos",
)
# Automatização bancária (entrega 7): não são segredos, são regras do administrador.
CHAVES_AUTOMACAO = (
    "auto_ativa",
    "auto_remetentes",
    "auto_desde",
    "auto_modo",
    "auto_ultima_consulta",
)
CHAVES_CONHECIDAS = CHAVES_GMAIL + CHAVES_IA + CHAVES_AUTOMACAO
SEGREDOS = {"gmail_client_secret", "gmail_refresh_token", "gmail_access_token", "ia_chave_api"}

LIMITE_MODELO = 100
TIMEOUT_MIN = 1
TIMEOUT_MAX = 120
TIMEOUT_POR_OMISSAO = 30

ESCOPO_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"


# ---------------------------------------------------------------------------
# Leitura/escrita da configuração de integrações
# ---------------------------------------------------------------------------


def obter_integracao(chave: str, padrao: str = "") -> str:
    linha = query_db("SELECT valor FROM integracoes WHERE chave = ?", (chave,), one=True)
    return str(linha["valor"]) if linha is not None else padrao


def todas_integracoes() -> dict[str, str]:
    return {
        str(linha["chave"]): str(linha["valor"])
        for linha in query_db("SELECT chave, valor FROM integracoes")
    }


def guardar_integracao(chave: str, valor: str) -> None:
    assert chave in CHAVES_CONHECIDAS, f"chave de integração desconhecida: {chave}"
    execute_db(
        "INSERT INTO integracoes (chave, valor, atualizado_em) VALUES (?, ?, ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor, atualizado_em = excluded.atualizado_em",
        (chave, valor, utc_now_iso()),
    )


def apagar_integracao(chave: str) -> None:
    execute_db("DELETE FROM integracoes WHERE chave = ?", (chave,))


def gmail_configurado() -> bool:
    return bool(obter_integracao("gmail_client_id") and obter_integracao("gmail_client_secret"))


def gmail_ligado() -> bool:
    return bool(obter_integracao("gmail_refresh_token"))


def ia_configurada() -> bool:
    return bool(obter_integracao("ia_base_url") and obter_integracao("ia_modelo"))


def ia_ativa() -> bool:
    return obter_integracao("ia_ativa") == "1" and ia_configurada()


def ia_timeout() -> int:
    try:
        return max(TIMEOUT_MIN, min(TIMEOUT_MAX, int(obter_integracao("ia_timeout_segundos", str(TIMEOUT_POR_OMISSAO)))))
    except ValueError:
        return TIMEOUT_POR_OMISSAO


def url_base_atual() -> str:
    """Endereço público desta instância, para o utilizador registar no Google.

    Não é fixado no código: vem do pedido, por isso acompanha a porta usada.
    """
    return f"{request.scheme}://{request.host}"


def uri_redirecionamento() -> str:
    return f"{url_base_atual()}/email/oauth/callback"


# ---------------------------------------------------------------------------
# Página Definições → Integrações
# ---------------------------------------------------------------------------


@bp.route("/definicoes/integracoes", methods=["GET", "POST"])
def integracoes():
    if request.method == "POST":
        if desatualizado(request.form, "integracoes"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("integracoes.integracoes"))

        erros: list[str] = []
        alteracoes: dict[str, str] = {}
        apagar: list[str] = []

        # Gmail: credenciais OAuth criadas pelo utilizador na consola do Google.
        cliente_id = request.form.get("gmail_client_id", "").strip()
        cliente_secreto = request.form.get("gmail_client_secret", "").strip()
        if cliente_id:
            alteracoes["gmail_client_id"] = cliente_id
        if cliente_secreto:
            alteracoes["gmail_client_secret"] = cliente_secreto
        if request.form.get("apagar_gmail_credenciais") == "on":
            apagar += ["gmail_client_id", "gmail_client_secret"]

        # IA: endereço base compatível com OpenAI; chave opcional (servidor local).
        base_url = request.form.get("ia_base_url", "").strip()
        if base_url:
            if not base_url.startswith(("http://", "https://")):
                erros.append("Endereço base da IA: usa um endereço http:// ou https://.")
            elif len(base_url) > 200:
                erros.append("Endereço base da IA: demasiado longo.")
            else:
                alteracoes["ia_base_url"] = base_url.rstrip("/")
        chave_api = request.form.get("ia_chave_api", "").strip()
        if chave_api:
            alteracoes["ia_chave_api"] = chave_api
        modelo = request.form.get("ia_modelo", "").strip()
        if modelo:
            if len(modelo) > LIMITE_MODELO:
                erros.append(f"Modelo da IA: no máximo {LIMITE_MODELO} caracteres.")
            else:
                alteracoes["ia_modelo"] = modelo
        timeout_bruto = request.form.get("ia_timeout_segundos", "").strip()
        if timeout_bruto:
            try:
                timeout = int(timeout_bruto)
            except ValueError:
                timeout = 0
            if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
                erros.append(f"Tempo limite da IA: entre {TIMEOUT_MIN} e {TIMEOUT_MAX} segundos.")
            else:
                alteracoes["ia_timeout_segundos"] = str(timeout)
        alteracoes["ia_ativa"] = "1" if request.form.get("ia_ativa") == "on" else "0"
        if request.form.get("apagar_ia_chave") == "on":
            apagar.append("ia_chave_api")

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
            return redirect(url_for("integracoes.integracoes"))

        if not transacao_de_escrita("integracoes"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("integracoes.integracoes"))
        connection = get_db()
        try:
            for chave in apagar:
                connection.execute("DELETE FROM integracoes WHERE chave = ?", (chave,))
            for chave, valor in alteracoes.items():
                connection.execute(
                    "INSERT INTO integracoes (chave, valor, atualizado_em) VALUES (?, ?, ?) "
                    "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor, "
                    "atualizado_em = excluded.atualizado_em",
                    (chave, valor, utc_now_iso()),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            flash("Não foi possível guardar as integrações. Nada foi alterado.", "erro")
            return redirect(url_for("integracoes.integracoes"))

        flash("Integrações guardadas. Os segredos ficam neste servidor e não são "
              "mostrados outra vez.", "sucesso")
        return redirect(url_for("integracoes.integracoes"))

    guardado = todas_integracoes()
    estado_gmail = {
        "credenciais": "guardadas" if gmail_configurado() else "por configurar",
        "ligado": gmail_ligado(),
        "conta": obter_integracao("gmail_conta"),
        "ligado_em": obter_integracao("gmail_ligado_em"),
        "ultimo_teste": obter_integracao("gmail_ultimo_teste"),
        "redirecionamento": uri_redirecionamento(),
    }
    estado_ia = {
        "ativa": ia_ativa(),
        "configurada": ia_configurada(),
        "base_url": obter_integracao("ia_base_url"),
        "chave": "guardada" if obter_integracao("ia_chave_api") else "não configurada",
        "modelo": obter_integracao("ia_modelo"),
        "timeout": ia_timeout(),
    }
    return render_template(
        "integracoes.html",
        guardado=guardado,
        estado_gmail=estado_gmail,
        estado_ia=estado_ia,
        revisao_integracoes=revisao_integracoes,
    )
