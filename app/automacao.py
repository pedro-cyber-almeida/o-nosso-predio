"""Automatização bancária: consultar emails do banco, guardar documentos,
extrair movimentos e registar os casos reconhecidos (entrega 7).

Regras fixas deste módulo:
- Começa desativada; só corre quando o administrador ativa e/ou «Executar agora».
- Um ciclo de cada vez (trava na tabela `processamentos`); execuções antigas
  presas são marcadas como interrompidas e o retoma é seguro porque cada
  extração e cada movimento têm marca única.
- Não executa um ciclo por cada pedido web: só por gatilho explícito
  (botão, comando agendado ou CLI).
- Autenticidade: o endereço visível do remetente não chega; é preciso que o
  Gmail reporte SPF e DKIM aprovados. Sem isso o email vai para revisão.
- Registos passam SEMPRE pelas mesmas funções e transações dos formulários
  manuais (`registar_pagamento`, `registar_despesa`, `pagar_despesa`).
- O mesmo movimento num comprovativo e num extrato conta uma única vez
  (hash do movimento, UNIQUE). Movimentos legítimos exatamente iguais têm
  ocorrências diferentes e contam cada uma.
- Operações agendadas/pendentes/recusadas/anuladas não são registadas.
- Comissões bancárias só criam despesa mediante regra explícita.
- O registo do dinheiro recebido é separado da sua afetação às cobranças:
  a automatização nunca afeta pagamentos a cobranças.
- O modo «propor» (pré-visualização) guarda, extrai e propõe mas não regista.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .db import execute_db, query_db, utc_now_iso
from .extratores import extrair_documento, hash_movimento
from .gmail import (
    API_GMAIL,
    MAX_ANEXO_BYTES,
    TIMEOUT_ANEXO,
    TIMEOUT_LISTA,
    TIMEOUT_MENSAGEM,
    _anexos_da_mensagem,
    _bytes_de_parte_inline,
    _descodificar,
    _motivo_erro_gmail,
    pedido_json,
)
from .integracoes import gmail_ligado, guardar_integracao, obter_integracao

bp = Blueprint("automacao", __name__)

EXECUCAO_PRSA_MINUTOS = 30  # execução 'a_decorrer' mais antiga que isto está presa

CHAVES_CONFIG = {
    "auto_ativa": "0",
    "auto_remetentes": "",
    "auto_desde": "",
    "auto_modo": "propor",  # 'propor' (pré-visualização) | 'registar'
}


@bp.before_request
def _apenas_administrador():
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


def config_auto() -> dict:
    desde = obter_integracao("auto_desde", "")
    return {
        "ativa": obter_integracao("auto_ativa", "0") == "1",
        "remetentes": [
            d.strip().lower()
            for d in obter_integracao("auto_remetentes", "").split(",")
            if d.strip()
        ],
        "desde": desde if _data_valida(desde) else "",
        "modo": "registar" if obter_integracao("auto_modo", "propor") == "registar" else "propor",
        "ultima_consulta": obter_integracao("auto_ultima_consulta", ""),
    }


def _data_valida(texto: str) -> bool:
    try:
        date.fromisoformat(texto)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Trava de execução única e retoma
# ---------------------------------------------------------------------------


def execucao_em_curso() -> dict | None:
    """Devolve o ciclo em curso, marcando como interrompidos os que ficaram presos."""
    agora = datetime.now(timezone.utc)
    for linha in query_db("SELECT * FROM processamentos WHERE estado = 'a_decorrer'"):
        try:
            iniciado = datetime.fromisoformat(str(linha["iniciado_em"]))
        except ValueError:
            iniciado = agora
        if iniciado.tzinfo is None:
            iniciado = iniciado.replace(tzinfo=timezone.utc)
        if agora - iniciado > timedelta(minutes=EXECUCAO_PRSA_MINUTOS):
            execute_db(
                "UPDATE processamentos SET estado = 'interrompido', terminado_em = ?, "
                "erro = ? WHERE id = ?",
                (utc_now_iso(), "execução interrompida (o processo terminou sem concluir)",
                 linha["id"]),
            )
        else:
            return dict(linha)
    return None


def executar_agora(app, gatilho: str = "manual") -> tuple[int | None, str]:
    """Iniciar um ciclo em segundo plano. Devolve (id, '') ou (None, motivo)."""
    if execucao_em_curso() is not None:
        return None, "Já existe uma execução em curso; espera que termine."
    if not gmail_ligado():
        return None, "O Gmail não está ligado; a automatização não pode consultar emails."
    processamento_id = execute_db(
        "INSERT INTO processamentos (iniciado_em, estado, gatilho) VALUES (?, 'a_decorrer', ?)",
        (utc_now_iso(), gatilho),
    )
    threading.Thread(
        target=_correr_ciclo, args=(app, processamento_id), daemon=True
    ).start()
    return processamento_id, ""


def correr_ciclo_sync(app, gatilho: str = "agendado") -> dict:
    """Correr um ciclo no processo atual (usado pelo comando de linha)."""
    with app.app_context():
        presa = execucao_em_curso()
        if presa is not None:
            return {"ignorado": f"execução {presa['id']} já em curso"}
        processamento_id = execute_db(
            "INSERT INTO processamentos (iniciado_em, estado, gatilho) VALUES (?, 'a_decorrer', ?)",
            (utc_now_iso(), gatilho),
        )
        _correr_ciclo(app, processamento_id)
        linha = query_db("SELECT * FROM processamentos WHERE id = ?",
                         (processamento_id,), one=True)
        return json.loads(str(linha["resumo_json"])) if linha else {}


def _correr_ciclo(app, processamento_id: int) -> None:
    with app.app_context():
        try:
            gatilho = query_db("SELECT gatilho FROM processamentos WHERE id = ?",
                               (processamento_id,), one=True)["gatilho"]
            resumo = _ciclo_interno(processamento_id, gatilho)
            execute_db(
                "UPDATE processamentos SET estado = 'concluido', terminado_em = ?, "
                "resumo_json = ? WHERE id = ?",
                (utc_now_iso(), json.dumps(resumo, ensure_ascii=False), processamento_id),
            )
        except Exception as exc:  # o ciclo nunca rebenta um processo web
            execute_db(
                "UPDATE processamentos SET estado = 'erro', terminado_em = ?, erro = ? "
                "WHERE id = ?",
                (utc_now_iso(), f"falha técnica ({type(exc).__name__})", processamento_id),
            )


# ---------------------------------------------------------------------------
# O ciclo: consultar → guardar → extrair → reconhecer/registar
# ---------------------------------------------------------------------------


def _ciclo_interno(processamento_id: int, gatilho: str = "manual") -> dict:
    from . import gmail as gmail_mod

    resumo = {
        "mensagens": 0, "documentos": 0, "duplicados": 0, "extracoes": 0,
        "movimentos": 0, "registados": 0, "revisao": 0, "autenticacao_recusada": 0,
    }
    config = config_auto()
    if gatilho == "agendado" and not config["ativa"]:
        # O agendamento só corre quando a automatização está ativa; «Executar
        # agora» é gatilho manual e serve também como pré-visualização pontual.
        resumo["nota"] = "automatização desativada"
        return resumo
    if not config["remetentes"]:
        resumo["nota"] = "sem remetentes definidos"
        return resumo

    token = gmail_mod._cabecalho_autorizacao()
    if token is None:
        resumo["nota"] = "token Gmail não obtido"
        return resumo
    cabecalho = {"Authorization": f"Bearer {token}"}

    desde = config["desde"]
    if not desde:
        ultima = config["ultima_consulta"]
        try:
            desde = datetime.fromisoformat(ultima).date().isoformat() if ultima else date.today().isoformat()
        except ValueError:
            desde = date.today().isoformat()

    for dominio in config["remetentes"]:
        query = f"from:{dominio} after:{desde.replace('-', '/')}"
        estado, corpo = pedido_json(
            f"{API_GMAIL}/me/messages?{urlencode({'maxResults': 25, 'q': query, 'fields': 'messages(id)'})}",
            headers=cabecalho, timeout=TIMEOUT_LISTA,
        )
        if estado != 200:
            resumo["nota"] = _motivo_erro_gmail(corpo)[:160]
            continue
        for item in corpo.get("messages", []):
            _processar_mensagem(str(item.get("id", "")), cabecalho, config,
                                processamento_id, resumo)

    guardar_integracao("auto_ultima_consulta", utc_now_iso())
    return resumo


def _processar_mensagem(mensagem_id: str, cabecalho: dict, config: dict,
                        processamento_id: int, resumo: dict) -> None:
    from .documentos import EXTENSOES, _documento_repetido, guardar_documento

    if query_db("SELECT mensagem_id FROM auto_mensagens WHERE mensagem_id = ?",
                (mensagem_id,), one=True) is not None:
        return
    resumo["mensagens"] += 1

    estado, msg = pedido_json(
        f"{API_GMAIL}/me/messages/{mensagem_id}?format=full",
        headers=cabecalho, timeout=TIMEOUT_MENSAGEM,
    )
    if estado != 200:
        _registar_mensagem(mensagem_id, "erro", _motivo_erro_gmail(msg)[:160])
        return

    cabecalhos = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                  for h in (msg.get("payload") or {}).get("headers", [])}
    if not _autenticacao_aprovada(cabecalhos.get("authentication-results", "")):
        # O endereço visível do remetente não é prova: sem SPF+DKIM aprovados
        # o email vai para revisão e não gera registos.
        _registar_mensagem(mensagem_id, "autenticacao_recusada",
                           "SPF/DKIM não aprovados pelo Gmail")
        resumo["autenticacao_recusada"] += 1
        resumo["revisao"] += 1
        return

    anexos = _anexos_da_mensagem(msg)
    if not anexos:
        _registar_mensagem(mensagem_id, "sem_anexos", "")
        return

    guardados = 0
    for anexo in anexos:
        if guardados >= 10:
            break
        if anexo["tamanho"] > MAX_ANEXO_BYTES or anexo["tamanho"] <= 0:
            continue
        if anexo["attachment_id"].startswith("parte:"):
            dados = _bytes_de_parte_inline(msg, anexo["attachment_id"][len("parte:"):])
        else:
            estado_a, corpo_a = pedido_json(
                f"{API_GMAIL}/me/messages/{mensagem_id}/attachments/{anexo['attachment_id']}",
                headers=cabecalho, timeout=TIMEOUT_ANEXO,
            )
            dados = _descodificar(str(corpo_a["data"])) if estado_a == 200 and "data" in corpo_a else None
        if dados is None or not dados:
            continue
        mime = anexo["mime"]
        if mime not in EXTENSOES and dados[:5] == b"%PDF-":
            mime = "application/pdf"  # anexos do banco chegam como octet-stream mas são PDF
        if mime not in EXTENSOES:
            continue
        sha256 = hashlib.sha256(dados).hexdigest()
        if _documento_repetido(sha256) is not None:
            resumo["duplicados"] += 1
            continue
        documento_id = guardar_documento(
            origem="anexo",
            dados=dados,
            nome_original=anexo["nome"],
            mime=mime,
            resumo="Anexo automático de email bancário",
            gmail_mensagem_id=mensagem_id,
            gmail_anexo_id=anexo["attachment_id"],
            categoria="Extrato",
        )
        guardados += 1
        resumo["documentos"] += 1

        _extrair_e_reconhecer(documento_id, dados, mime, mensagem_id,
                              processamento_id, config, resumo)

    _registar_mensagem(mensagem_id, "processada", "")


def _registar_mensagem(mensagem_id: str, estado: str, nota: str) -> None:
    execute_db(
        "INSERT INTO auto_mensagens (mensagem_id, processado_em, estado, nota) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (mensagem_id) DO NOTHING",
        (mensagem_id, utc_now_iso(), estado, nota),
    )


def _autenticacao_aprovada(resultados: str) -> bool:
    """SPF e DKIM aprovados nos resultados de autenticação reportados pelo Gmail."""
    baixo = resultados.lower()
    return "spf=pass" in baixo and "dkim=pass" in baixo


# ---------------------------------------------------------------------------
# Extração e reconhecimento de movimentos
# ---------------------------------------------------------------------------


def _extrair_e_reconhecer(documento_id: int, dados: bytes, mime: str, mensagem_id: str,
                          processamento_id: int, config: dict, resumo: dict) -> None:
    import tempfile
    from pathlib import Path

    # O extrator lê o ficheiro: guardamos uma cópia temporária fora da app.
    extensao = ".pdf" if mime == "application/pdf" else ".bin"
    temporario = Path(tempfile.gettempdir()) / "opencode" / f"extrator_{documento_id}{extensao}"
    temporario.parent.mkdir(parents=True, exist_ok=True)
    temporario.write_bytes(dados)
    try:
        resultado = extrair_documento(temporario, mime)
    finally:
        temporario.unlink(missing_ok=True)

    estado_extração = {
        "ok": "reconhecido", "sem_movimentos": "sem_movimentos",
        "revisao": "revisao", "falha": "falha",
    }[resultado["estado"]]
    execute_db(
        "INSERT INTO extracoes (documento_id, processamento_id, extrator, versao_extrator, "
        "campos_json, origem_json, estado, nota, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (documento_id) DO NOTHING",
        (
            documento_id, processamento_id, resultado["extrator"], resultado["versao"],
            json.dumps(resultado["movimentos"], ensure_ascii=False)[:20000],
            json.dumps([m.get("origem") for m in resultado["movimentos"]], ensure_ascii=False)[:8000],
            estado_extração, resultado["nota"][:300], utc_now_iso(),
        ),
    )
    linha_extraida = query_db("SELECT id FROM extracoes WHERE documento_id = ?",
                              (documento_id,), one=True)
    if linha_extraida is None:
        return
    extracao_id = int(linha_extraida["id"])
    resumo["extracoes"] += 1

    movimentos = resultado["movimentos"]
    if not movimentos:
        resumo["revisao"] += 1
        return
    for ocorrencia, movimento in enumerate(movimentos, start=1):
        _processar_movimento(movimento, ocorrencia, extracao_id, documento_id,
                             resultado, config, resumo)


def _processar_movimento(movimento: dict, ocorrencia: int, extracao_id: int,
                         documento_id: int, resultado: dict, config: dict,
                         resumo: dict) -> None:
    resumo["movimentos"] += 1
    marca = hash_movimento(resultado["extrator"], movimento, ocorrencia)
    if query_db("SELECT id FROM auto_movimentos WHERE hash = ?", (marca,), one=True) is not None:
        resumo["duplicados"] += 1
        return

    estado = "revisao"
    nota = ""
    payment_id = None
    expense_payment_id = None
    regra = f"{resultado['extrator']} v{resultado['versao']}"

    incompleto = (not movimento.get("data") or movimento.get("valor_cents") is None
                  or not movimento.get("sentido"))
    if incompleto:
        nota = "campos incompletos ou ambíguos — nunca se adivinha valores"
    elif not movimento.get("executado_flag"):
        nota = "operação não executada (agendada, pendente, recusada ou anulada)"
    elif config["modo"] == "propor":
        estado = "proposta"
    else:
        estado, nota, payment_id, expense_payment_id = _registar_movimento(
            movimento, documento_id, marca, regra)

    if estado in ("revisao",):
        resumo["revisao"] += 1
    elif estado == "registado":
        resumo["registados"] += 1

    execute_db(
        "INSERT INTO auto_movimentos (extracao_id, hash, sentido, estado, payment_id, "
        "expense_payment_id, regra, nota, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (hash) DO NOTHING",
        (extracao_id, marca, movimento.get("sentido") or "recebido", estado,
         payment_id, expense_payment_id, regra, nota, utc_now_iso()),
    )


def _registar_movimento(movimento: dict, documento_id: int, marca: str,
                        regra: str) -> tuple[str, str, int | None, int | None]:
    """Registar via as funções dos formulários manuais. Ambíguo → revisão."""
    token = f"auto:{marca}"
    data = movimento["data"]
    valor = int(movimento["valor_cents"])
    referencia = str(movimento.get("referencia") or "")[:120]
    origem = f"automático: {regra}"

    if movimento["sentido"] == "recebido":
        fracao = _corresponder_fracao(movimento)
        if fracao is None:
            return "revisao", "sem correspondência única de fração (IBAN/ordenante/referência)", None, None
        payment_id, falha = registar_pagamento_manual(
            fracao, valor, data, referencia, token, documento_id, origem)
        if falha == "repetido":
            return "registado", "pagamento já registado (mesma marca)", payment_id, None
        if falha:
            return "revisao", f"falha ao registar pagamento ({falha})", None, None
        return "registado", "", payment_id, None

    if movimento["sentido"] == "enviado":
        despesa = _corresponder_despesa(movimento)
        if despesa is None:
            return "revisao", "associação ambígua à despesa correspondente", None, None
        ok, falha, _ = pagar_despesa_manual(int(despesa["id"]), valor, data, referencia, token)
        if falha == "repetido":
            return "registado", "pagamento já registado (mesma marca)", None, None
        if falha:
            return "revisao", f"falha ao pagar despesa ({falha})", None, None
        expense_payment_id = int(query_db(
            "SELECT id FROM expense_payments WHERE form_token = ?", (token,), one=True)["id"])
        return "registado", "", None, expense_payment_id

    # comissão: só mediante regra explícita
    correspondencia = _corresponder_comissao(movimento)
    if correspondencia is None:
        return "revisao", "comissão sem regra explícita que autorize criar despesa", None, None
    category_id = _categoria_bancaria()
    if category_id is None:
        return "revisao", "sem categoria de despesa para comissões bancárias", None, None
    despesa = {
        "descricao": "Comissão bancária",
        "fornecedor": correspondencia["alvo_texto"] or "Banco",
        "category_id": category_id,
        "referencia": referencia,
        "data": data,
        "due_date": None,
        "amount_cents": valor,
        "classificacao": "ordinario",
        "notas": f"criada automaticamente pela automatização ({regra})",
    }
    expense_id, falha = registar_despesa_manual(despesa, f"{token}:d", documento_id, origem)
    if falha and falha != "repetido":
        return "revisao", f"falha ao criar despesa de comissão ({falha})", None, None
    ok, falha_pag, _ = pagar_despesa_manual(expense_id, valor, data, referencia, f"{token}:p")
    if falha_pag and falha_pag != "repetido":
        return "revisao", f"despesa criada mas o pagamento falhou ({falha_pag})", None, None
    expense_payment_id = int(query_db(
        "SELECT id FROM expense_payments WHERE form_token = ?", (f"{token}:p",), one=True)["id"])
    return "registado", "", None, expense_payment_id


# ---------------------------------------------------------------------------
# Correspondências (regras do administrador)
# ---------------------------------------------------------------------------


def _correspondencias_ativas(tipo_alvo: str) -> list[dict]:
    return [dict(l) for l in query_db(
        "SELECT * FROM auto_correspondencias WHERE ativo = 1 AND alvo_tipo = ?", (tipo_alvo,))]


def _padrao_bate(padrao: str, texto: str) -> bool:
    p = _normalizar_texto(padrao)
    t = _normalizar_texto(texto)
    return bool(p) and p in t


def _normalizar_texto(texto: str) -> str:
    import unicodedata

    texto = unicodedata.normalize("NFD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


def _corresponder_fracao(movimento: dict) -> dict | None:
    alvo_texto = f"{movimento.get('contraparte', '')} {movimento.get('descricao', '')}"
    # Escolha explícita do administrador na página de revisão.
    m = re.search(r"__alvo_fracao:(\d+)", alvo_texto)
    if m:
        return query_db("SELECT * FROM fractions WHERE id = ?", (int(m.group(1)),), one=True)
    fracoes: set[int] = set()
    for regra in _correspondencias_ativas("fracao"):
        alvo = movimento.get("referencia") if regra["tipo"] == "referencia" else alvo_texto
        if _padrao_bate(regra["padrao"], str(alvo or "")):
            fracoes.add(int(regra["alvo_id"]))
    if len(fracoes) != 1:
        return None  # zero ou ambíguo → revisão
    return query_db("SELECT * FROM fractions WHERE id = ?", (next(iter(fracoes)),), one=True)


def _corresponder_despesa(movimento: dict) -> dict | None:
    alvo_texto = f"{movimento.get('contraparte', '')} {movimento.get('descricao', '')}"
    # Escolha explícita do administrador na página de revisão.
    m = re.search(r"__alvo_despesa:(\d+)", alvo_texto)
    if m:
        return query_db(
            "SELECT * FROM expenses WHERE id = ? AND cancelled_at IS NULL",
            (int(m.group(1)),), one=True)
    fornecedores: set[str] = set()
    for regra in _correspondencias_ativas("fornecedor"):
        alvo = movimento.get("referencia") if regra["tipo"] == "referencia" else alvo_texto
        if _padrao_bate(regra["padrao"], str(alvo or "")):
            fornecedores.add(_normalizar_texto(regra["alvo_texto"]))
    if not fornecedores:
        return None
    candidatos: list[dict] = []
    for despesa in query_db(
        "SELECT * FROM expenses WHERE cancelled_at IS NULL AND amount_cents > 0"):
        if _normalizar_texto(despesa["fornecedor"]) not in fornecedores:
            continue
        pago = int(query_db(
            "SELECT COALESCE(SUM(amount_cents), 0) AS v FROM expense_payments "
            "WHERE expense_id = ? AND cancelled_at IS NULL", (despesa["id"],), one=True)["v"])
        if despesa["amount_cents"] - pago < int(movimento["valor_cents"]):
            continue
        if (movimento.get("referencia") and despesa["referencia"]
                and _padrao_bate(despesa["referencia"], movimento["referencia"])):
            candidatos.append(dict(despesa))
            continue
        try:
            distancia = abs((date.fromisoformat(despesa["data"])
                             - date.fromisoformat(movimento["data"])).days)
        except ValueError:
            continue
        if distancia <= 15:
            candidatos.append(dict(despesa))
    if len(candidatos) != 1:
        return None  # ambíguo → revisão
    return candidatos[0]


def _corresponder_comissao(movimento: dict) -> dict | None:
    alvo_texto = f"{movimento.get('contraparte', '')} {movimento.get('descricao', '')}"
    bate = [r for r in query_db(
        "SELECT * FROM auto_correspondencias WHERE ativo = 1 AND comissao = 1")
        if _padrao_bate(r["padrao"], alvo_texto)
        or _padrao_bate(r["padrao"], str(movimento.get("referencia") or ""))]
    return bate[0] if len(bate) == 1 else None


def _categoria_bancaria() -> int | None:
    linha = query_db(
        "SELECT id FROM expense_categories ORDER BY id LIMIT 1", (), one=True)
    for candidata in query_db("SELECT id, nome FROM expense_categories"):
        nome = _normalizar_texto(candidata["nome"])
        if "banc" in nome or "comiss" in nome:
            return int(candidata["id"])
    return int(linha["id"]) if linha is not None else None


# ---------------------------------------------------------------------------
# Ponte para as funções financeiras existentes (mesmos travões dos formulários)
# ---------------------------------------------------------------------------


def registar_pagamento_manual(fracao: dict, valor: int, data: str, referencia: str,
                              token: str, documento_id: int, origem: str) -> tuple[int | None, str]:
    from .payments import registar_pagamento

    pagamento = {
        "fraction_id": int(fracao["id"]),
        "amount_cents": valor,
        "paid_on": data,
        "method": "transferencia",
        "reference": referencia,
        "source": origem,
    }
    return registar_pagamento(pagamento, token, documento_id, origem, form=None)


def registar_despesa_manual(despesa: dict, token: str, documento_id: int,
                            origem: str) -> tuple[int | None, str]:
    from .despesas import registar_despesa

    return registar_despesa(despesa, token, documento_id, origem, form=None)


def pagar_despesa_manual(despesa_id: int, valor: int, data: str, referencia: str,
                         token: str) -> tuple[bool, str, dict]:
    from .despesas import pagar_despesa

    return pagar_despesa(int(despesa_id), valor, data, "transferencia", referencia, token,
                         form=None)


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------


@bp.route("/automacao")
def pagina():
    config = config_auto()
    presa = execucao_em_curso()
    ultima = query_db("SELECT * FROM processamentos ORDER BY id DESC LIMIT 1", (), one=True)
    historico = [dict(l) for l in query_db(
        "SELECT id, iniciado_em, terminado_em, estado, gatilho, resumo_json, erro "
        "FROM processamentos ORDER BY id DESC LIMIT 5")]
    correspondencias = [dict(l) for l in query_db(
        "SELECT c.*, f.code AS fracao_code FROM auto_correspondencias c "
        "LEFT JOIN fractions f ON f.id = c.alvo_id ORDER BY c.id")]
    fracoes = [dict(l) for l in query_db("SELECT id, code FROM fractions ORDER BY code")]
    pendentes = query_db(
        "SELECT COUNT(*) AS n FROM auto_movimentos WHERE estado IN ('proposta', 'revisao')",
        (), one=True)["n"]
    extracoes_pendentes = query_db(
        "SELECT COUNT(*) AS n FROM extracoes WHERE estado IN ('revisao', 'falha')",
        (), one=True)["n"]
    return render_template(
        "automacao.html", config=config, em_curso=presa, ultima=ultima,
        historico=historico, correspondencias=correspondencias, fracoes=fracoes,
        pendentes=pendentes, extracoes_pendentes=extracoes_pendentes,
    )


@bp.route("/automacao/config", methods=["POST"])
def guardar_config():
    ativa = "1" if request.form.get("auto_ativa") == "on" else "0"
    remetentes = request.form.get("auto_remetentes", "").strip()
    desde = request.form.get("auto_desde", "").strip()
    modo = "registar" if request.form.get("auto_modo") == "registar" else "propor"

    erros: list[str] = []
    if desde and not _data_valida(desde):
        erros.append("Data a partir da qual processar: usa o formato AAAA-MM-DD.")
    dominios = [d.strip().lower() for d in remetentes.split(",") if d.strip()]
    for dominio in dominios:
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", dominio):
            erros.append(f"Remetente inválido: {dominio} (usa domínios, ex.: millenniumbcp.pt).")
    if ativa == "1" and not dominios:
        erros.append("Para ativar a automatização define pelo menos um remetente esperado.")
    if ativa == "1" and modo == "registar" and not query_db(
            "SELECT id FROM auto_correspondencias WHERE ativo = 1", (), one=True):
        erros.append(
            "O modo 'registar automaticamente' exige pelo menos uma correspondência "
            "(IBAN/ordenante/referência → fração ou fornecedor); sem ela nada é registado "
            "e tudo vai para revisão.")
    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("automacao.pagina"))

    guardar_integracao("auto_ativa", ativa)
    guardar_integracao("auto_remetentes", ",".join(dominios))
    guardar_integracao("auto_desde", desde)
    guardar_integracao("auto_modo", modo)
    flash(
        "Configuração de automatização guardada. "
        + ("A automatização está ativa." if ativa == "1" else "A automatização está desativada."),
        "sucesso",
    )
    return redirect(url_for("automacao.pagina"))


@bp.route("/automacao/correspondencias", methods=["POST"])
def adicionar_correspondencia():
    tipo = request.form.get("tipo", "")
    padrao = request.form.get("padrao", "").strip()
    alvo_tipo = request.form.get("alvo_tipo", "")
    alvo_id = request.form.get("alvo_id", "").strip()
    alvo_texto = request.form.get("alvo_texto", "").strip()
    comissao = 1 if request.form.get("comissao") == "on" else 0

    if tipo not in ("iban", "ordenante", "referencia"):
        flash("Tipo de correspondência inválido.", "erro")
        return redirect(url_for("automacao.pagina"))
    if not padrao or len(padrao) > 200:
        flash("Padrão da correspondência: escreve o IBAN, nome ou referência (máx. 200 caracteres).", "erro")
        return redirect(url_for("automacao.pagina"))
    if alvo_tipo == "fracao":
        fracao = query_db("SELECT id FROM fractions WHERE id = ?", (alvo_id,), one=True)
        if fracao is None:
            flash("Fração inválida.", "erro")
            return redirect(url_for("automacao.pagina"))
        alvo_id_final: int | None = int(fracao["id"])
        alvo_texto_final = ""
    elif alvo_tipo == "fornecedor":
        if not alvo_texto or len(alvo_texto) > 120:
            flash("Fornecedor: escreve o nome como aparece nas despesas (máx. 120 caracteres).", "erro")
            return redirect(url_for("automacao.pagina"))
        alvo_id_final = None
        alvo_texto_final = alvo_texto
    else:
        flash("Alvo da correspondência inválido.", "erro")
        return redirect(url_for("automacao.pagina"))

    execute_db(
        "INSERT INTO auto_correspondencias (tipo, padrao, alvo_tipo, alvo_id, alvo_texto, "
        "comissao, ativo, criado_em) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
        "ON CONFLICT (tipo, padrao) DO UPDATE SET alvo_tipo = excluded.alvo_tipo, "
        "alvo_id = excluded.alvo_id, alvo_texto = excluded.alvo_texto, "
        "comissao = excluded.comissao, ativo = 1",
        (tipo, padrao, alvo_tipo, alvo_id_final, alvo_texto_final, comissao, utc_now_iso()),
    )
    flash("Correspondência guardada.", "sucesso")
    return redirect(url_for("automacao.pagina"))


@bp.route("/automacao/correspondencias/<int:correspondencia_id>/alternar", methods=["POST"])
def alternar_correspondencia(correspondencia_id: int):
    linha = query_db("SELECT id, ativo FROM auto_correspondencias WHERE id = ?",
                     (correspondencia_id,), one=True)
    if linha is None:
        flash("Correspondência não encontrada.", "erro")
        return redirect(url_for("automacao.pagina"))
    execute_db("UPDATE auto_correspondencias SET ativo = ? WHERE id = ?",
               (0 if linha["ativo"] else 1, correspondencia_id))
    flash("Correspondência atualizada.", "sucesso")
    return redirect(url_for("automacao.pagina"))


@bp.route("/automacao/executar", methods=["POST"])
def executar():
    from flask import current_app

    processamento_id, motivo = executar_agora(current_app._get_current_object(), "manual")
    if motivo:
        flash(motivo, "erro")
    else:
        flash("Execução iniciada em segundo plano; atualiza a página para ver o estado.", "sucesso")
    return redirect(url_for("automacao.pagina"))


@bp.route("/automacao/revisao")
def revisao():
    movimentos = [dict(l) for l in query_db(
        "SELECT m.*, e.documento_id, e.extrator, e.versao_extrator, e.campos_json, e.nota AS nota_extracao "
        "FROM auto_movimentos m JOIN extracoes e ON e.id = m.extracao_id "
        "WHERE m.estado IN ('proposta', 'revisao') ORDER BY m.id DESC LIMIT 100")]
    for movimento in movimentos:
        campos = _campo_do_movimento(movimento)
        movimento["campos"] = campos
        valor = campos.get("valor_cents")
        movimento["valor_texto"] = (
            f"{valor // 100},{valor % 100:02d}" if isinstance(valor, int) else ""
        )
        movimento["documento"] = query_db(
            "SELECT id, nome_original, mime FROM documentos WHERE id = ?",
            (movimento["documento_id"],), one=True)
    extracoes_sem_movimento = [dict(l) for l in query_db(
        "SELECT e.*, d.nome_original FROM extracoes e JOIN documentos d ON d.id = e.documento_id "
        "WHERE e.estado IN ('revisao', 'falha') ORDER BY e.id DESC LIMIT 50")]
    recusadas = [dict(l) for l in query_db(
        "SELECT * FROM auto_mensagens WHERE estado = 'autenticacao_recusada' "
        "ORDER BY processado_em DESC LIMIT 20")]
    fracoes = [dict(l) for l in query_db("SELECT id, code FROM fractions ORDER BY code")]
    despesas_pendentes = [dict(l) for l in query_db(
        "SELECT e.id, e.descricao, e.fornecedor, e.amount_cents, "
        "e.amount_cents - COALESCE((SELECT SUM(p.amount_cents) FROM expense_payments p "
        "  WHERE p.expense_id = e.id AND p.cancelled_at IS NULL), 0) AS falta "
        "FROM expenses e WHERE e.cancelled_at IS NULL AND e.amount_cents > COALESCE("
        "(SELECT SUM(p.amount_cents) FROM expense_payments p WHERE p.expense_id = e.id "
        "AND p.cancelled_at IS NULL), 0) ORDER BY e.id DESC LIMIT 100")]
    return render_template("automacao_revisao.html", movimentos=movimentos,
                           extracoes=extracoes_sem_movimento, recusadas=recusadas,
                           fracoes=fracoes, despesas_pendentes=despesas_pendentes)


def _campo_do_movimento(movimento: dict) -> dict:
    extracao = query_db("SELECT campos_json FROM extracoes WHERE id = ?",
                        (movimento["extracao_id"],), one=True)
    try:
        campos = json.loads(str(extracao["campos_json"])) if extracao else []
    except (json.JSONDecodeError, TypeError):
        campos = []
    # O movimento corresponde ao índice da ocorrência na extração.
    indice = sum(1 for _ in query_db(
        "SELECT id FROM auto_movimentos WHERE extracao_id = ? AND id <= ?",
        (movimento["extracao_id"], movimento["id"]))) - 1
    return campos[indice] if 0 <= indice < len(campos) else {}


@bp.route("/automacao/revisao/<int:movimento_id>/confirmar", methods=["POST"])
def confirmar_movimento(movimento_id: int):
    movimento = query_db("SELECT * FROM auto_movimentos WHERE id = ?",
                         (movimento_id,), one=True)
    if movimento is None or movimento["estado"] not in ("proposta", "revisao"):
        flash("Movimento não encontrada ou já tratada.", "erro")
        return redirect(url_for("automacao.revisao"))
    campos = _campo_do_movimento(movimento)
    data = request.form.get("data", "").strip()
    valor_texto = request.form.get("valor", "").strip()
    from .extratores import parse_data, parse_valor

    data_iso = parse_data(data)
    valor = parse_valor(valor_texto)
    if valor is None and valor_texto.isdigit():
        valor = int(valor_texto) * 100  # '123' sem casas decimais
    if not data_iso or valor is None or valor <= 0:
        flash("Confirmação precisa de data válida (dd/mm/aaaa) e valor (ex.: 123,45).", "erro")
        return redirect(url_for("automacao.revisao"))

    sentido = request.form.get("sentido", movimento["sentido"])
    movimento_actualizado = {**campos, "data": data_iso, "valor_cents": valor,
                             "sentido": sentido}
    # O administrador pode escolher diretamente a fração ou a despesa na revisão.
    fracao_id = request.form.get("fracao_id", "").strip()
    despesa_id = request.form.get("despesa_id", "").strip()
    contraparte = str(movimento_actualizado.get("contraparte") or "")
    if fracao_id.isdigit():
        contraparte += f" __alvo_fracao:{fracao_id}"
    if despesa_id.isdigit():
        contraparte += f" __alvo_despesa:{despesa_id}"
    movimento_actualizado["contraparte"] = contraparte
    estado, nota, payment_id, expense_payment_id = _registar_movimento(
        movimento_actualizado,
        int(query_db("SELECT documento_id FROM extracoes WHERE id = ?",
                     (movimento["extracao_id"],), one=True)["documento_id"]),
        movimento["hash"], f"confirmado manualmente ({movimento['regra']})")
    if estado != "registado":
        flash(f"Não foi possível registar: {nota}", "erro")
        return redirect(url_for("automacao.revisao"))
    execute_db(
        "UPDATE auto_movimentos SET estado = 'registado', payment_id = ?, "
        "expense_payment_id = ?, nota = ? WHERE id = ?",
        (payment_id, expense_payment_id, "confirmado pelo administrador na revisão",
         movimento_id),
    )
    flash("Movimento registado. A afetação às cobranças continua a ser feita à mão.", "sucesso")
    return redirect(url_for("automacao.revisao"))


@bp.route("/automacao/revisao/<int:movimento_id>/ignorar", methods=["POST"])
def ignorar_movimento(movimento_id: int):
    movimento = query_db("SELECT * FROM auto_movimentos WHERE id = ?",
                         (movimento_id,), one=True)
    if movimento is None or movimento["estado"] not in ("proposta", "revisao"):
        flash("Movimento não encontrada ou já tratada.", "erro")
        return redirect(url_for("automacao.revisao"))
    execute_db("UPDATE auto_movimentos SET estado = 'ignorado', nota = ? WHERE id = ?",
               ("ignorada pelo administrador na revisão", movimento_id))
    flash("Movimento ignorada; nada foi registado.", "sucesso")
    return redirect(url_for("automacao.revisao"))
