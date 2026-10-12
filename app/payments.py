"""Pagamentos, afetações, créditos, anulações e saldos por fração (fase 3).

Regras aplicadas nesta fase:
- Um pagamento é dinheiro que entrou: data, fração, valor, meio e referência opcional.
  O valor é estritamente positivo e o registo não se apaga; corrige-se por correção
  justificada, com histórico.
- A distribuição por cobranças é feita em afetações separadas, confirmadas pelo
  administrador. Um pagamento pode liquidar várias cobranças e uma cobrança pode
  receber vários pagamentos parciais.
- Quota ordinária e reforço do fundo não se misturam: cada afetação aponta para a
  cobrança de uma rubrica, e os totais são sempre apresentados separados.
- O montante recebido e não afetado é o crédito da fração. É calculado
  (recebidos - afetados), nunca guardado: afetar um crédito acrescenta afetações a
  um pagamento que já existe e por isso não cria uma segunda receita.
- A base trava o que a aplicação também valida: afetação negativa, afetação a outra
  fração, afetação a mais do que o pagamento, afetação a mais do que a cobrança deve
  e afetação de um pagamento anulado.
- Pagamento e afetações são gravados numa única transação, com BEGIN IMMEDIATE:
  uma falha a meio não deixa metade do registo.
- Um duplo clique reenvia o mesmo formulário: a revisão de concorrência recusa a
  segunda submissão e a marca do formulário (`form_token`, com índice único) impede
  que o mesmo pagamento entre duas vezes.
- Anular exige motivo, conserva o pagamento e as suas afetações, e todas as somas
  passam a excluir o pagamento anulado: as cobranças que ele liquidou voltam a
  estar em dívida e o crédito remanescente deixa de contar. Uma anulação não se
  repete. Uma devolução bancária não é uma anulação: seria um movimento novo.
- Dívida vencida só existe quando a cobrança tem data de vencimento.
- Sem saldo inicial confirmado, mostra-se o que se sabe e diz-se que o saldo total
  está por confirmar. As despesas pagas entram na conta no painel do administrador.
- Um pagamento corrige-se em `/payments/<id>/edit`: fração, valor, data, meio,
  referência, nota e distribuição. Os identificadores internos e o rasto de auditoria
  não são campos editáveis. A correção exige motivo, é mostrada antes de ser aplicada
  e fica guardada em `payment_revisions` com os valores anteriores e novos e a data.
  O pagamento continua a ser uma linha só, por isso corrigir não duplica receita.
  Alterar o valor recalcula o crédito; baixar o valor abaixo do distribuído exige
  ajustar a distribuição no mesmo formulário; mudar de fração retira as afetações
  antigas e não transfere nenhuma. Um pagamento anulado não é editado nem reativado.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from .auth import current_user
from .concurrency import (
    FORM_DO_PEDIDO,
    MENSAGEM_DESATUALIZADO,
    desatualizado,
    revisao_pagamentos,
    transacao_de_escrita,
)
from .db import get_all_settings, get_db, query_db, utc_now_iso
from .quotas import ROTULOS_RUBRICA
from .units import (
    format_date,
    format_money,
    format_money_input,
    format_month,
    parse_date,
    parse_int_id,
    parse_money,
    parse_stored_cents,
    today_iso,
)

bp = Blueprint("payments", __name__)


@bp.before_request
def _apenas_administrador():
    """Registar, corrigir e anular pagamentos é administração. O condómino
    consulta as suas frações no portal; a proteção é no servidor."""
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()

MEIOS = ("transferencia", "dinheiro", "cheque", "debito_direto", "outro")
ROTULOS_MEIO = {
    "transferencia": "Transferência bancária",
    "dinheiro": "Dinheiro",
    "cheque": "Cheque",
    "debito_direto": "Débito direto",
    "outro": "Outro",
}

# Um movimento acima disto é quase de certeza uma gralha de escrita.
VALOR_MAXIMO_CENTIMOS = 1_000_000  # 10.000 EUR
LIMITE_REFERENCIA = 120
LIMITE_NOTA = 120
LIMITE_MOTIVO = 200
CAMPO_AFETAO = "afecao_"


@bp.app_context_processor
def _contexto_pagamentos():
    return {
        "MEIOS": MEIOS,
        "ROTULOS_MEIO": ROTULOS_MEIO,
        "format_money": format_money,
        "format_month": format_month,
        "revisao_pagamentos": revisao_pagamentos,
    }


def _recusar_se_desatualizado() -> bool:
    if not desatualizado(request.form, "pagamentos"):
        return False
    flash(MENSAGEM_DESATUALIZADO, "erro")
    return True


def _novo_token_de_formulario() -> str:
    """Marca única deste formulário, gravada no pagamento que ele originar."""
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# Cálculos: cobranças por liquidar, crédito e dívida
# ---------------------------------------------------------------------------


_ALOCADO_VALIDO = """
    COALESCE((
        SELECT SUM(a.amount_cents)
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        WHERE a.charge_id = c.id AND p.cancelled_at IS NULL
    ), 0)
"""


def _estado_das_cobrancas(fraction_id: int) -> list[dict]:
    """Cobranças de uma fração com o que já está liquidado por pagamentos válidos."""
    hoje = today_iso()
    linhas: list[dict] = []
    for row in query_db(
        f"""
        SELECT c.id, c.period, c.rubric, c.amount_cents, c.due_date, c.descricao,
               {_ALOCADO_VALIDO} AS pago
        FROM charges AS c
        WHERE c.fraction_id = ?
        ORDER BY c.period, c.rubric, c.id
        """,
        (fraction_id,),
    ):
        valor = int(row["amount_cents"])
        pago = int(row["pago"])
        em_divida = valor - pago
        linhas.append(
            {
                "id": row["id"],
                "period": row["period"],
                "period_texto": format_month(row["period"]),
                "rubric": row["rubric"],
                "rubrica_texto": ROTULOS_RUBRICA[row["rubric"]],
                "descricao": row["descricao"] or "",
                "amount_cents": valor,
                "pago_cents": pago,
                "em_divida_cents": em_divida,
                "liquidada": em_divida == 0,
                "due_date": row["due_date"],
                "due_texto": format_date(row["due_date"]) if row["due_date"] else "por confirmar",
                # Sem vencimento definido não há dívida vencida, por muito antiga que seja.
                "vencida": bool(row["due_date"]) and row["due_date"] < hoje and em_divida > 0,
            }
        )
    return linhas


def _divida_vencida(fraction_id: int | None) -> int:
    """Montante por liquidar das cobranças com data de vencimento já passada."""
    linha = query_db(
        f"""
        SELECT COALESCE(SUM(c.amount_cents - COALESCE(p.pago, 0)), 0) AS vencida
        FROM charges AS c
        LEFT JOIN (
            SELECT a.charge_id AS charge_id, SUM(a.amount_cents) AS pago
            FROM payment_allocations AS a
            JOIN payments AS p ON p.id = a.payment_id
            WHERE p.cancelled_at IS NULL
            GROUP BY a.charge_id
        ) AS p ON p.charge_id = c.id
        WHERE c.due_date IS NOT NULL AND c.due_date < ?
          AND (? = 0 OR c.fraction_id = ?)
        """,
        (today_iso(), 0 if fraction_id is None else 1, fraction_id or 0),
        one=True,
    )
    return int(linha["vencida"])


def _totais(fraction_id: int | None) -> dict:
    """Totais de uma fração, ou do prédio todo quando a fração não é indicada.

    Só contam os pagamentos válidos (`cancelled_at IS NULL`): um pagamento anulado
    deixou de ser receita e deixou de liquidar seja o que for.
    """
    # Filtros escritos à mão, um por tabela: montar um e "adaptá-lo" com `replace`
    # produziria SQL errado sem qualquer erro se alguma consulta ganhasse um JOIN.
    filtro = "AND fraction_id = ?" if fraction_id is not None else ""
    filtro_pagamento = "AND p.fraction_id = ?" if fraction_id is not None else ""
    params: tuple = (fraction_id,) if fraction_id is not None else ()

    recebido = query_db(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS v FROM payments WHERE cancelled_at IS NULL {filtro}",
        params,
        one=True,
    )
    anulado = query_db(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS v FROM payments WHERE cancelled_at IS NOT NULL {filtro}",
        params,
        one=True,
    )
    cobrado = query_db(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS v FROM charges WHERE 1 = 1 {filtro}",
        params,
        one=True,
    )
    afetado = query_db(
        f"""
        SELECT COALESCE(SUM(a.amount_cents), 0) AS v
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        WHERE p.cancelled_at IS NULL {filtro_pagamento}
        """,
        params,
        one=True,
    )
    ao_fundo = query_db(
        f"""
        SELECT COALESCE(SUM(a.amount_cents), 0) AS v
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        JOIN charges AS c ON c.id = a.charge_id
        WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo'
          {filtro_pagamento}
        """,
        params,
        one=True,
    )

    recebido_cents = int(recebido["v"])
    afetado_cents = int(afetado["v"])
    cobrado_cents = int(cobrado["v"])
    return {
        "cobrado_cents": cobrado_cents,
        "recebido_cents": recebido_cents,
        "afetado_cents": afetado_cents,
        # O crédito é o que entrou e ainda não foi afetado a nenhuma cobrança.
        "credito_cents": recebido_cents - afetado_cents,
        "divida_cents": cobrado_cents - afetado_cents,
        "vencida_cents": _divida_vencida(fraction_id),
        "ao_fundo_cents": int(ao_fundo["v"]),
        "anulado_cents": int(anulado["v"]),
    }


def _totais_texto(totais: dict) -> dict:
    """Versão apresentada de cada total, para os não terem de formatar nada no template."""
    return {
        f"{chave.removesuffix('_cents')}_texto": format_money(valor)
        for chave, valor in totais.items()
        if chave.endswith("_cents")
    }


def resumo_financeiro() -> dict:
    """Números financeiros para o resumo.

    Não é uma tesouraria: sem despesas registadas não há saldo de caixa definitivo,
    e o saldo inicial continua por confirmar.
    """
    totais = _totais(None)
    pagamentos = query_db(
        "SELECT COUNT(*) AS n FROM payments WHERE cancelled_at IS NULL", one=True
    )
    anulados = query_db(
        "SELECT COUNT(*) AS n FROM payments WHERE cancelled_at IS NOT NULL", one=True
    )
    return {
        "pagamentos_registados": int(pagamentos["n"]),
        "pagamentos_anulados": int(anulados["n"]),
        # Um registo anulado continua a ser um registo: mostra-se, não se esconde.
        "tem_movimentos": (
            totais["recebido_cents"] > 0
            or totais["cobrado_cents"] > 0
            or totais["anulado_cents"] > 0
        ),
        "recebido_texto": format_money(totais["recebido_cents"]),
        "afetado_texto": format_money(totais["afetado_cents"]),
        "credito_texto": format_money(totais["credito_cents"]),
        "divida_texto": format_money(totais["divida_cents"]),
        "vencida_texto": format_money(totais["vencida_cents"]),
        "tem_divida_vencida": totais["vencida_cents"] > 0,
        "ao_fundo_texto": format_money(totais["ao_fundo_cents"]),
        "anulado_texto": format_money(totais["anulado_cents"]),
    }


# ---------------------------------------------------------------------------
# Registar pagamentos
# ---------------------------------------------------------------------------


def _ler_pagamento_do_form(form) -> tuple[dict | None, list[str]]:
    erros: list[str] = []

    fraction_id = parse_int_id(form.get("fracao", ""))
    fracao = (
        query_db("SELECT id, code, active FROM fractions WHERE id = ?", (fraction_id,), one=True)
        if fraction_id is not None
        else None
    )
    if fracao is None:
        erros.append("Fração: escolhe uma fração registada.")

    valor = parse_money(form.get("valor", ""))
    if valor is None or valor <= 0:
        erros.append("Valor recebido: usa um valor positivo em euros, por exemplo 52,00.")
    elif valor > VALOR_MAXIMO_CENTIMOS:
        erros.append(
            "Valor recebido: acima de 10.000,00 EUR parece um erro de escrita. "
            "Confirma o valor e tenta de novo."
        )

    data = parse_date(form.get("data", ""))
    if data is None:
        erros.append("Data do pagamento: usa DD/MM/YYYY.")
    elif data > today_iso():
        erros.append(
            "Data do pagamento: não pode ser futura. Regista o dinheiro no dia em que "
            "efetivamente entrou."
        )

    meio = form.get("meio", "").strip()
    if meio not in MEIOS:
        erros.append("Meio de pagamento: escolhe uma das opções.")

    referencia = form.get("referencia", "").strip()
    if len(referencia) > LIMITE_REFERENCIA:
        erros.append(f"Referência: no máximo {LIMITE_REFERENCIA} caracteres.")

    nota = form.get("source", "").strip()
    if len(nota) > LIMITE_NOTA:
        erros.append(f"Nota: no máximo {LIMITE_NOTA} caracteres.")

    if erros:
        return None, erros

    return {
        "fraction_id": fracao["id"],
        "code": fracao["code"],
        "amount_cents": valor,
        "paid_on": data,
        "method": meio,
        "reference": referencia,
        "source": nota,
    }, erros


def registar_pagamento(pagamento: dict, token: str, doc_id: int | None,
                       nota_origem: str = "registado a partir do documento",
                       form=FORM_DO_PEDIDO) -> tuple[int | None, str]:
    """Inserir um pagamento na transação de escrita, com idempotência por marca.

    É o mesmo caminho do formulário manual: transação de escrita, travão de
    reenvio pela marca única e rasto no documento. Devolve
    (payment_id, '') no sucesso, (None, 'repetido') se a marca já existia e
    (None, erro) em falha. Usado pelo formulário e pela automatização bancária
    (que passa `form=None`: escrita de sistema, sem revisão de formulário).
    """
    if not transacao_de_escrita("pagamentos", form=form):
        return None, "desatualizado"
    connection = get_db()
    try:
        # Dentro da transação de escrita: nenhum outro pedido pode inserir a
        # mesma marca entretanto, por isto é seguro verificar antes de inserir.
        repetido = query_db("SELECT id FROM payments WHERE form_token = ?", (token,), one=True)
        if repetido is not None:
            connection.rollback()
            return int(repetido["id"]), "repetido"

        now = utc_now_iso()
        cursor = connection.execute(
            """
            INSERT INTO payments
                (fraction_id, amount_cents, paid_on, method, reference, source,
                 created_at, updated_at, form_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pagamento["fraction_id"],
                pagamento["amount_cents"],
                pagamento["paid_on"],
                pagamento["method"],
                pagamento["reference"],
                pagamento["source"],
                now,
                now,
                token,
            ),
        )
        payment_id = int(cursor.lastrowid)
        # Registo feito a partir de um documento: a ligação é um rasto,
        # gravado na mesma transação. Não cria nem altera movimentos.
        if doc_id is not None:
            from .documentos import ligar_documento

            ligar_documento(doc_id, "pagamento", payment_id, nota_origem)
        connection.commit()
        return payment_id, ""
    except sqlite3.Error as erro:
        connection.rollback()
        return None, str(getattr(erro, "sqlite_error", erro))


@bp.route("/payments/add", methods=["GET", "POST"])
def add_payment():
    """Registar dinheiro recebido. A distribuição por cobranças é confirmada a seguir."""
    if request.method == "POST":
        if _recusar_se_desatualizado():
            return redirect(url_for("payments.add_payment"))

        pagamento, erros = _ler_pagamento_do_form(request.form)
        if request.form.get("confirmar_pagamento") != "on":
            erros.append(
                "Registar um pagamento exige confirmar que este dinheiro entrou e que "
                "o valor e a data correspondem a um comprovativo. Nenhum pagamento foi "
                "guardado."
            )
        token = request.form.get("form_token", "").strip()
        if not token:
            erros.append(
                "Este formulário não tem a marca de envio: reabre a página de pagamentos "
                "e tenta de novo. Nenhum pagamento foi guardado."
            )

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
            return redirect(url_for("payments.add_payment"))

        doc_id = parse_int_id(request.form.get("doc", ""))
        payment_id, falha = registar_pagamento(pagamento, token, doc_id)
        if falha == "desatualizado":
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("payments.add_payment"))
        if falha == "repetido":
            flash(
                "Este formulário já tinha sido enviado: o pagamento já está registado "
                "e não foi duplicado. Se for preciso corrigir um valor, edita o "
                "pagamento já registado.",
                "erro",
            )
            return redirect(url_for("payments.payment_detail", payment_id=payment_id))
        if falha:
            flash(
                f"Não foi possível registar o pagamento ({falha}). "
                "Nenhum pagamento foi guardado.",
                "erro",
            )
            return redirect(url_for("payments.add_payment"))

        flash(
            f"Pagamento registado: {format_money(pagamento['amount_cents'])} da fração "
            f"{pagamento['code']}, em {format_date(pagamento['paid_on'])}. Ainda não "
            "distribuído: confirma em baixo quais cobranças este dinheiro liquidou.",
            "sucesso",
        )
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    session["payment_form_token"] = _novo_token_de_formulario()
    from .documentos import documento_para_formulario

    return render_template(
        "payment_add.html",
        fracoes=query_db("SELECT id, code, active FROM fractions ORDER BY code"),
        data=today_iso(),
        form_token=session["payment_form_token"],
        documento=documento_para_formulario(request.args.get("doc", "")),
    )


# ---------------------------------------------------------------------------
# Detalhe de um pagamento, afetações e anulação
# ---------------------------------------------------------------------------


def _pagamento_or_404(payment_id: int):
    row = query_db(
        """
        SELECT p.*, f.code, f.active AS fraction_active
        FROM payments p
        JOIN fractions f ON f.id = p.fraction_id
        WHERE p.id = ?
        """,
        (payment_id,),
        one=True,
    )
    if row is None:
        abort(404)
    return row


def _afetacoes_de_pagamento(payment_id: int) -> list[dict]:
    return [
        {
            "id": row["id"],
            "charge_id": row["charge_id"],
            "period_texto": format_month(row["period"]),
            "rubrica_texto": ROTULOS_RUBRICA[row["rubric"]],
            "amount_cents": int(row["amount_cents"]),
            "charge_amount_texto": format_money(int(row["charge_amount_cents"])),
            "created_at": row["created_at"],
        }
        for row in query_db(
            """
            SELECT a.id, a.charge_id, a.amount_cents, a.created_at,
                   c.period, c.rubric, c.amount_cents AS charge_amount_cents
            FROM payment_allocations a
            JOIN charges c ON c.id = a.charge_id
            WHERE a.payment_id = ?
            ORDER BY c.period, c.rubric
            """,
            (payment_id,),
        )
    ]


@bp.route("/payments/<int:payment_id>")
def payment_detail(payment_id: int):
    pagamento = _pagamento_or_404(payment_id)
    afetacoes = _afetacoes_de_pagamento(payment_id)
    afetado = sum(a["amount_cents"] for a in afetacoes)
    credito = int(pagamento["amount_cents"]) - afetado

    return render_template(
        "payment_detail.html",
        pagamento={
            "id": pagamento["id"],
            "code": pagamento["code"],
            "fraction_id": pagamento["fraction_id"],
            "amount_cents": int(pagamento["amount_cents"]),
            "paid_on": format_date(pagamento["paid_on"]),
            "method_texto": ROTULOS_MEIO[pagamento["method"]],
            "reference": pagamento["reference"],
            "source": pagamento["source"],
            "cancelled_at": format_date(pagamento["cancelled_at"]) if pagamento["cancelled_at"] else "",
            "cancellation_reason": pagamento["cancellation_reason"] or "",
            "anulado": pagamento["cancelled_at"] is not None,
            "edited_at_texto": _momento_texto(pagamento["edited_at"]),
            "edit_reason": pagamento["edit_reason"] or "",
        },
        afetacoes=afetacoes,
        afetado_cents=afetado,
        credito_cents=credito,
        cobrancas=_estado_das_cobrancas(pagamento["fraction_id"]),
        correcoes=_correcoes_de_pagamento(payment_id),
    )


def _ler_afetacoes_do_form(form, payment_id: int) -> tuple[dict[int, int], list[str]]:
    """Ler e validar as afetações propostas. Devolve {charge_id: cêntimos}, erros.

    Um formulário manipulado pode enviar a mesma cobrança duas vezes (`afecao_5` e
    `afecao_05` são ambos a cobrança 5). Agrupar por cobrança fecha essa ambiguidade
    na validação, em vez de depender do `UNIQUE (payment_id, charge_id)` da base.
    """
    erros: list[str] = []
    propostas: dict[int, int] = {}
    for campo, texto in sorted(form.items()):
        if not campo.startswith(CAMPO_AFETAO):
            continue
        charge_id = parse_int_id(campo.removeprefix(CAMPO_AFETAO))
        if charge_id is None:
            continue
        texto = (texto or "").strip()
        if not texto:
            continue
        valor = parse_money(texto)
        if valor is None:
            erros.append(
                f"Afetação à cobrança {charge_id}: usa um valor em euros, por exemplo 47,00, "
                "ou deixa o campo vazio."
            )
            continue
        if valor < 0:
            erros.append(
                f"Afetação à cobrança {charge_id}: um valor negativo não liquida nada. "
                "Para retirar uma afetação, anula o pagamento."
            )
            continue
        if valor == 0:
            continue
        if charge_id in propostas:
            erros.append(
                f"A cobrança {charge_id} aparece mais do que uma vez neste formulário. "
                "Escreve um único valor por cobrança."
            )
            continue
        propostas[charge_id] = valor

    if not propostas and not erros:
        return {}, ["Distribuição: escreve pelo menos um valor, ou deixa o pagamento como crédito."]
    return propostas, erros


@bp.route("/payments/<int:payment_id>/allocate", methods=["POST"])
def allocate_payment(payment_id: int):
    """Distribuir o dinheiro de um pagamento por cobranças concretas da mesma fração."""
    pagamento = _pagamento_or_404(payment_id)
    if pagamento["cancelled_at"] is not None:
        flash(
            "Este pagamento está anulado e não pode voltar a liquidar cobranças. "
            "Regista o dinheiro outra vez, se for esse o caso.",
            "erro",
        )
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    if _recusar_se_desatualizado():
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    propostas, erros = _ler_afetacoes_do_form(request.form, payment_id)
    if request.form.get("confirmar_afetacao") != "on":
        erros.append(
            "Distribuir um pagamento exige confirmação explícita de que estas afetações "
            "correspondem ao que o administrador confirmou. Nenhuma afetação foi guardada."
        )

    if not erros:
        afetado = int(
            query_db(
                "SELECT COALESCE(SUM(amount_cents), 0) AS v FROM payment_allocations WHERE payment_id = ?",
                (payment_id,),
                one=True,
            )["v"]
        )
        credito = int(pagamento["amount_cents"]) - afetado
        estado = {linha["id"]: linha for linha in _estado_das_cobrancas(pagamento["fraction_id"])}

        for charge_id, valor in propostas.items():
            cobranca = estado.get(charge_id)
            if cobranca is None:
                erros.append(
                    f"A cobrança {charge_id} não pertence à fração {pagamento['code']}. "
                    "Um pagamento só liquidou cobranças da sua fração."
                )
                continue
            if cobranca["em_divida_cents"] < valor:
                erros.append(
                    f"{cobranca['rubrica_texto']} de {cobranca['period_texto']}: faltam "
                    f"{format_money(cobranca['em_divida_cents'])} e a afetação proposta é "
                    f"{format_money(valor)}. Uma afetação não pode exceder o que está em dívida."
                )

        soma = sum(propostas.values())
        if soma > credito:
            erros.append(
                f"A soma das afetações propostas ({format_money(soma)}) excede o dinheiro "
                f"deste pagamento ainda por afetar ({format_money(credito)}). "
                "Reduz os valores ou regista outro pagamento."
            )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    if not transacao_de_escrita("pagamentos"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    connection = get_db()
    try:
        now = utc_now_iso()
        for charge_id, valor in propostas.items():
            connection.execute(
                """
                INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (payment_id, charge_id, valor, now),
            )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível guardar a distribuição "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma afetação foi gravada.",
            "erro",
        )
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    flash(
        f"Distribuição confirmada: {format_money(sum(propostas.values()))} afetados "
        f"a {len(propostas)} cobrança(s) da fração {pagamento['code']}. O resto continua "
        "crédito desta fração e pode ser afetado mais tarde.",
        "sucesso",
    )
    return redirect(url_for("payments.payment_detail", payment_id=payment_id))


@bp.route("/payments/<int:payment_id>/cancel", methods=["POST"])
def cancel_payment(payment_id: int):
    """Anular um pagamento: o registo fica, mas deixa de contar em todas as somas."""
    pagamento = _pagamento_or_404(payment_id)

    if _recusar_se_desatualizado():
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    motivo = request.form.get("motivo", "").strip()
    erros: list[str] = []
    if not motivo:
        erros.append(
            "Anular um pagamento exige um motivo escrito, que fica guardado com o registo. "
            "Nenhuma anulação foi feita."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo: no máximo {LIMITE_MOTIVO} caracteres.")
    if request.form.get("confirmar_anular") != "on":
        erros.append(
            "Anular exige confirmação explícita. Nenhuma anulação foi feita."
        )

    if pagamento["cancelled_at"] is not None:
        erros.append(
            "Este pagamento já foi anulado em "
            f"{format_date(pagamento['cancelled_at'])} ({pagamento['cancellation_reason']}). "
            "Uma anulação não se repete, e o histórico não é alterado."
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    if not transacao_de_escrita("pagamentos"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    connection = get_db()
    try:
        cursor = connection.execute(
            """
            UPDATE payments
            SET cancelled_at = ?, cancellation_reason = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (today_iso(), motivo, utc_now_iso(), payment_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash(
                "Este pagamento já estava anulado. Nenhuma alteração foi feita.",
                "erro",
            )
            return redirect(url_for("payments.payment_detail", payment_id=payment_id))
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível anular o pagamento "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    afetacoes = _afetacoes_de_pagamento(payment_id)
    flash(
        f"Pagamento de {format_money(int(pagamento['amount_cents']))} da fração "
        f"{pagamento['code']} anulado. O registo e as {len(afetacoes)} afetações "
        "conservam-se, mas deixaram de contar: as cobranças que este pagamento liquidou "
        "voltaram a estar em dívida e o crédito remanescente deixou de contar. "
        "Uma devolução bancária seria um movimento novo, não uma anulação.",
        "sucesso",
    )
    return redirect(url_for("payments.payment_detail", payment_id=payment_id))


# ---------------------------------------------------------------------------
# Editar um pagamento: correção justificada, com rasto do que mudou
# ---------------------------------------------------------------------------
#
# O pagamento continua a ser um registo só: a linha de `payments` é corrigida e
# todas as somas da aplicação lêem essa linha, por isso corrigir não duplica
# receita. O que estava antes fica guardado em `payment_revisions`, com os valores
# anteriores e novos, a data e o motivo. `changed_by` guarda o nome de utilizador
# de quem corrigiu; continua vazio quando a escrita não vem de uma sessão.
#
# O formulário mostra primeiro o efeito da correção nas cobranças, no crédito e na
# dívida; só grava depois de confirmado, e fá-lo numa única transação.

CAMPO_CONFIRMAR_EDICAO = "confirmar_edicao"
CAMPO_CONFIRMAR_FRACAO = "confirmar_mudanca_de_fracao"
CAMPO_CONFIRMAR_RETIRADA = "confirmar_retirada_distribuicao"


def _momento_texto(iso: object) -> str:
    """Momento guardado, apresentado em DD/MM/YYYY HH:MM:SS."""
    texto = str(iso or "").strip()
    if not texto:
        return ""
    data, separador, hora = texto.partition("T")
    if not separador:
        return format_date(data)
    return f"{format_date(data)} às {hora[:8]}"


def _afetacoes_da_correcao(revision_id: int, lado: str) -> list[dict]:
    """Distribuição de uma correção: 'antes' ou 'depois'."""
    return [
        {
            "charge_id": row["charge_id"],
            "period_texto": format_month(row["period"]),
            "rubrica_texto": ROTULOS_RUBRICA[row["rubric"]],
            "amount_cents": int(row["amount_cents"]),
        }
        for row in query_db(
            """
            SELECT r.charge_id, r.amount_cents, c.period, c.rubric
            FROM payment_allocation_revisions AS r
            JOIN charges AS c ON c.id = r.charge_id
            WHERE r.revision_id = ? AND r.lado = ?
            ORDER BY c.period, c.rubric
            """,
            (revision_id, lado),
        )
    ]


def _lado_da_correcao(row, prefixo: str) -> dict:
    return {
        "code": row[f"{prefixo}_fraction_code"],
        "amount_cents": int(row[f"{prefixo}_amount_cents"]),
        "paid_on_texto": format_date(row[f"{prefixo}_paid_on"]),
        "method_texto": ROTULOS_MEIO[row[f"{prefixo}_method"]],
        "reference": row[f"{prefixo}_reference"] or "",
        "source": row[f"{prefixo}_source"] or "",
    }


def _correcoes_de_pagamento(payment_id: int) -> list[dict]:
    """Correções de um pagamento, da mais antiga para a mais recente."""
    linhas = []
    for row in query_db(
        """
        SELECT r.*, fo.code AS old_fraction_code, fn.code AS new_fraction_code
        FROM payment_revisions AS r
        JOIN fractions AS fo ON fo.id = r.old_fraction_id
        JOIN fractions AS fn ON fn.id = r.new_fraction_id
        WHERE r.payment_id = ?
        ORDER BY r.id
        """,
        (payment_id,),
    ):
        linhas.append(
            {
                "id": row["id"],
                "changed_at_texto": _momento_texto(row["changed_at"]),
                "reason": row["reason"],
                "changed_by": row["changed_by"],
                "old": _lado_da_correcao(row, "old"),
                "new": _lado_da_correcao(row, "new"),
                "afetacoes_antes": _afetacoes_da_correcao(row["id"], "antes"),
                "afetacoes_depois": _afetacoes_da_correcao(row["id"], "depois"),
            }
        )
    for linha in linhas:
        # O crédito de cada versão é o que dessa distribuição sobrou por afetar.
        for lado, chave in (("antes", "old"), ("depois", "new")):
            distribuido = sum(a["amount_cents"] for a in linha[f"afetacoes_{lado}"])
            linha[f"distribuido_{lado}_cents"] = distribuido
            linha[f"credito_{lado}_cents"] = linha[chave]["amount_cents"] - distribuido
    return linhas


def _afetacoes_atuais(payment_id: int) -> dict[int, int]:
    """Distribuição atual de um pagamento: {charge_id: cêntimos}."""
    return {int(row["charge_id"]): int(row["amount_cents"]) for row in _afetacoes_de_pagamento(payment_id)}


def _estado_das_cobrancas_por_id(charge_ids: list[int]) -> dict[int, dict]:
    """Estado de cobranças concretas, possam elas pertencer a frações diferentes."""
    if not charge_ids:
        return {}
    marcas = ", ".join("?" for _ in charge_ids)
    estado = {}
    for row in query_db(
        f"""
        SELECT c.id, c.period, c.rubric, c.amount_cents, c.due_date, f.code,
               {_ALOCADO_VALIDO} AS pago
        FROM charges AS c
        JOIN fractions AS f ON f.id = c.fraction_id
        WHERE c.id IN ({marcas})
        ORDER BY c.period, c.rubric
        """,
        tuple(charge_ids),
    ):
        valor = int(row["amount_cents"])
        pago = int(row["pago"])
        estado[int(row["id"])] = {
            "id": int(row["id"]),
            "code": row["code"],
            "period_texto": format_month(row["period"]),
            "rubrica_texto": ROTULOS_RUBRICA[row["rubric"]],
            "amount_cents": valor,
            "pago_cents": pago,
            "em_divida_cents": valor - pago,
        }
    return estado


def _efeito_da_edicao(pagamento, antes: dict[int, int], depois: dict[int, int], valores: dict) -> dict:
    """O que esta correção muda em cada cobrança, no crédito e na dívida.

    Nada é gravado: é o cálculo do que aconteceria, para ser mostrado antes de
    confirmar. A dívida de cada fração é recalculada a partir dos totais atuais,
    retirando o que este pagamento liquidava e acrescentando o que passaria a liquidar.
    """
    valor_antes = int(pagamento["amount_cents"])
    fracao_antes = int(pagamento["fraction_id"])
    valor_depois = int(valores["amount_cents"])
    fracao_depois = int(valores["fraction_id"])
    soma_antes = sum(antes.values())
    soma_depois = sum(depois.values())

    estado = _estado_das_cobrancas_por_id(sorted(set(antes) | set(depois)))
    cobrancas: list[dict] = []
    for charge_id, cobranca in estado.items():
        pago_antes = cobranca["pago_cents"]
        pago_depois = pago_antes - antes.get(charge_id, 0) + depois.get(charge_id, 0)
        divida_antes = cobranca["amount_cents"] - pago_antes
        divida_depois = cobranca["amount_cents"] - pago_depois
        cobrancas.append(
            {
                "id": charge_id,
                "code": cobranca["code"],
                "period_texto": cobranca["period_texto"],
                "rubrica_texto": cobranca["rubrica_texto"],
                "amount_texto": format_money(cobranca["amount_cents"]),
                "divida_antes_cents": divida_antes,
                "divida_antes_texto": format_money(divida_antes),
                "divida_depois_texto": format_money(divida_depois),
                "liquidada_depois": divida_depois == 0,
                "mudou": divida_antes != divida_depois,
            }
        )
    cobrancas.sort(key=lambda linha: (linha["period_texto"], linha["rubrica_texto"], linha["code"]))

    codigos = {int(row["id"]): row["code"] for row in query_db("SELECT id, code FROM fractions")}
    fracoes: list[dict] = []
    for fraction_id in dict.fromkeys((fracao_antes, fracao_depois)):
        totais = _totais(fraction_id)
        recebido = totais["recebido_cents"]
        afetado = totais["afetado_cents"]
        if fraction_id == fracao_antes:
            recebido -= valor_antes
            afetado -= soma_antes
        if fraction_id == fracao_depois:
            recebido += valor_depois
            afetado += soma_depois
        fracoes.append(
            {
                "id": fraction_id,
                "code": codigos.get(fraction_id, str(fraction_id)),
                "recebido_antes_texto": format_money(totais["recebido_cents"]),
                "recebido_depois_texto": format_money(recebido),
                "credito_antes_texto": format_money(totais["credito_cents"]),
                "credito_depois_texto": format_money(recebido - afetado),
                "divida_antes_texto": format_money(totais["divida_cents"]),
                "divida_depois_texto": format_money(totais["cobrado_cents"] - afetado),
            }
        )

    return {
        "cobrancas": cobrancas,
        "fracoes": fracoes,
        "afetado_antes_texto": format_money(soma_antes),
        "afetado_depois_texto": format_money(soma_depois),
        "credito_pagamento_antes_texto": format_money(valor_antes - soma_antes),
        "credito_pagamento_depois_texto": format_money(valor_depois - soma_depois),
        "mudou_valor": valor_antes != valor_depois,
        "mudou_fracao": fracao_antes != fracao_depois,
    }


def _ler_edicao_do_form(form, exigir_confirmacao: bool) -> tuple[dict | None, str, str, list[str]]:
    """Ler o pagamento proposto, o motivo e a marca do formulário. Devolve erros."""
    valores, erros = _ler_pagamento_do_form(form)

    motivo = form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Motivo da correção: escreve porque este pagamento está errado. O motivo fica "
            "guardado com os valores anteriores e nenhum pagamento é corrigido sem ele."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo da correção: no máximo {LIMITE_MOTIVO} caracteres.")

    token = form.get("edit_token", "").strip()
    if not token:
        erros.append(
            "Este formulário não tem a marca de envio: reabre a edição do pagamento e "
            "tenta de novo. Nenhuma alteração foi feita."
        )

    if exigir_confirmacao and form.get(CAMPO_CONFIRMAR_EDICAO) != "on":
        erros.append(
            "Corrigir um pagamento exige confirmação explícita, depois de ver o efeito "
            "nas cobranças, no crédito e na dívida. Nenhuma alteração foi guardada."
        )

    return valores, motivo, token, erros


def _ler_afetacoes_da_edicao(
    form,
    fraction_id: int,
    code: str,
    novo_valor: int,
    antes: dict[int, int],
    mesma_fracao: bool,
    confirmar_retirada: bool,
) -> tuple[dict[int, int], list[str]]:
    """Ler e validar a nova distribuição. Um campo vazio retira a afetação.

    Campos de afetação ausentes num pedido não retiram distribuição nenhuma:
    o navegador envia sempre os campos (vazios ou preenchidos), por isso a
    ausência é manipulação ou formulário partido, e o pedido é recusado.
    Retirar toda a distribuição é ação explícita: exige a confirmação própria.
    """
    erros: list[str] = []
    propostas: dict[int, int] = {}
    estado = {linha["id"]: linha for linha in _estado_das_cobrancas(fraction_id)}

    for campo, texto in sorted(form.items()):
        if not campo.startswith(CAMPO_AFETAO):
            continue
        charge_id = parse_int_id(campo.removeprefix(CAMPO_AFETAO))
        if charge_id is None:
            continue
        texto = (texto or "").strip()
        if not texto:
            continue  # vazio: esta afetação é retirada
        valor = parse_money(texto)
        if valor is None:
            erros.append(
                f"Afetação à cobrança {charge_id}: usa um valor em euros, por exemplo 47,00, "
                "ou deixa o campo vazio para retirar essa afetação."
            )
            continue
        if valor < 0:
            erros.append(
                f"Afetação à cobrança {charge_id}: um valor negativo não liquida nada. "
                "Para retirar uma afetação, deixa o campo vazio."
            )
            continue
        if valor == 0:
            continue
        if charge_id not in estado:
            erros.append(
                f"Afetação à cobrança {charge_id}: essa cobrança não existe ou não pertence "
                f"à fração {code}. Ao mudar de fração, a distribuição é outra: as afetações "
                "antigas são retiradas e não são transferidas."
            )
            continue
        if charge_id in propostas:
            erros.append(
                f"A cobrança {charge_id} aparece mais do que uma vez neste formulário. "
                "Escreve um único valor por cobrança."
            )
            continue
        propostas[charge_id] = valor

    for charge_id, valor in propostas.items():
        cobranca = estado[charge_id]
        # O limite é o que falta pagar, descontando a afetação que este pagamento já tem.
        outro_pago = cobranca["pago_cents"] - antes.get(charge_id, 0)
        limite = cobranca["amount_cents"] - outro_pago
        if valor > limite:
            erros.append(
                f"{cobranca['rubrica_texto']} de {cobranca['period_texto']}: faltam "
                f"{format_money(limite)} e a afetação proposta é {format_money(valor)}. "
                "Uma afetação não pode exceder o que está em dívida."
            )

    soma = sum(propostas.values())
    if soma > novo_valor:
        erros.append(
            f"A soma das afetações propostas ({format_money(soma)}) excede o novo valor "
            f"deste pagamento ({format_money(novo_valor)}). Se o valor desceu, ajusta a "
            "distribuição neste mesmo formulário."
        )

    if mesma_fracao and antes:
        ausentes = sorted(cid for cid in antes if f"{CAMPO_AFETAO}{cid}" not in form)
        if ausentes:
            erros.append(
                "A distribuição atual chegou incompleta a este pedido: faltam "
                f"{len(ausentes)} campo(s) de afetação. Um pedido sem estes campos "
                "não retira distribuição nenhuma e nada foi gravado. Reabre a "
                "edição do pagamento e tenta de novo."
            )
        elif not propostas and not confirmar_retirada:
            erros.append(
                "Retirar toda a distribuição é uma ação explícita: este pagamento "
                f"liquidava {format_money(sum(antes.values()))} e passaria a crédito "
                "integral da fração, com as cobranças de volta a estar em dívida. "
                "Marca a confirmação de retirada ou escreve os valores da nova "
                "distribuição. Nada foi gravado."
            )

    return propostas, erros


def _render_edicao(pagamento, antes: dict[int, int], valores, propostas: dict[int, int], efeito):
    """Mostrar o formulário com os valores atuais (ou propostos) e o efeito da correção."""
    fraction_id = int(valores["fraction_id"]) if valores else int(pagamento["fraction_id"])
    cobrancas = []
    for linha in _estado_das_cobrancas(fraction_id):
        cents = propostas.get(linha["id"]) if propostas else antes.get(linha["id"])
        linha["proposta_texto"] = format_money_input(cents) if cents else ""
        cobrancas.append(linha)

    return render_template(
        "payment_edit.html",
        pagamento={
            "id": pagamento["id"],
            "code": pagamento["code"],
            "amount_texto": format_money(int(pagamento["amount_cents"])),
            "paid_on": format_date(pagamento["paid_on"]),
            "method_texto": ROTULOS_MEIO[pagamento["method"]],
            "edited_at_texto": _momento_texto(pagamento["edited_at"]),
            "edit_reason": pagamento["edit_reason"] or "",
        },
        valores={
            "fracao": fraction_id,
            "valor": format_money_input(
                int(valores["amount_cents"]) if valores else int(pagamento["amount_cents"])
            ),
            "data": format_date(valores["paid_on"] if valores else pagamento["paid_on"]),
            "meio": valores["method"] if valores else pagamento["method"],
            "referencia": (valores["reference"] if valores else pagamento["reference"]) or "",
            "source": (valores["source"] if valores else pagamento["source"]) or "",
            "motivo": request.form.get("motivo", "") if request.method == "POST" else "",
        },
        fracoes=query_db("SELECT id, code, active FROM fractions ORDER BY code"),
        cobrancas=cobrancas,
        afetacoes_atuais=antes,
        efeito=efeito,
        edit_token=_novo_token_de_formulario(),
    )


@bp.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
def edit_payment(payment_id: int):
    """Corrigir um pagamento: todos os campos de negócio, com motivo e histórico."""
    pagamento = _pagamento_or_404(payment_id)
    if pagamento["cancelled_at"] is not None:
        flash(
            "Este pagamento está anulado e não é editado: anular não se repete e um "
            "pagamento anulado não volta a contar nos totais. Se o dinheiro entrou "
            "mesmo, regista-o outra vez como movimento novo.",
            "erro",
        )
        return redirect(url_for("payments.payment_detail", payment_id=payment_id))

    antes = _afetacoes_atuais(payment_id)

    if request.method == "GET":
        return _render_edicao(pagamento, antes, None, {}, None)

    if _recusar_se_desatualizado():
        return redirect(url_for("payments.edit_payment", payment_id=payment_id))

    previsualizar = request.form.get("previsualizar") == "on"
    valores, motivo, token, erros = _ler_edicao_do_form(request.form, exigir_confirmacao=not previsualizar)

    propostas: dict[int, int] = {}
    if valores is not None:
        propostas, erros_afetacoes = _ler_afetacoes_da_edicao(
            request.form,
            valores["fraction_id"],
            valores["code"],
            valores["amount_cents"],
            antes,
            mesma_fracao=valores["fraction_id"] == pagamento["fraction_id"],
            confirmar_retirada=request.form.get(CAMPO_CONFIRMAR_RETIRADA) == "on",
        )
        erros += erros_afetacoes
        if (
            valores["fraction_id"] != pagamento["fraction_id"]
            and antes
            and request.form.get(CAMPO_CONFIRMAR_FRACAO) != "on"
        ):
            erros.append(
                f"Mudar a fração retira as {len(antes)} afetações atuais "
                f"({format_money(sum(antes.values()))}) e as cobranças dessa fração voltam "
                "a estar em dívida. As afetações não são transferidas: confirma "
                "expressamente essa retirada e distribui o dinheiro na nova fração."
            )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return _render_edicao(pagamento, antes, valores, propostas, None)

    efeito = _efeito_da_edicao(pagamento, antes, propostas, valores)
    if previsualizar:
        return _render_edicao(pagamento, antes, valores, propostas, efeito)

    if not transacao_de_escrita("pagamentos"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("payments.edit_payment", payment_id=payment_id))

    connection = get_db()
    try:
        # Dentro da transação de escrita: o mesmo formulário reenviado tem a mesma
        # marca, e a segunda cópia é recusada em vez de aplicar a correção duas vezes.
        repetida = query_db(
            "SELECT id FROM payment_revisions WHERE edit_token = ?", (token,), one=True
        )
        if repetida is not None:
            connection.rollback()
            flash(
                "Esta correção já tinha sido aplicada: o mesmo formulário foi enviado "
                "duas vezes e o efeito não foi duplicado.",
                "erro",
            )
            return redirect(url_for("payments.payment_detail", payment_id=payment_id))

        agora = utc_now_iso()
        # A identidade de quem corrigiu vem da sessão; não é inventada.
        quem = current_user()
        alterado_por = quem["username"] if quem is not None else ""
        cursor = connection.execute(
            """
            INSERT INTO payment_revisions
                (payment_id, changed_at, reason, changed_by, edit_token,
                 old_fraction_id, old_amount_cents, old_paid_on, old_method,
                 old_reference, old_source,
                 new_fraction_id, new_amount_cents, new_paid_on, new_method,
                 new_reference, new_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                agora,
                motivo,
                alterado_por,
                token,
                pagamento["fraction_id"],
                pagamento["amount_cents"],
                pagamento["paid_on"],
                pagamento["method"],
                pagamento["reference"] or "",
                pagamento["source"] or "",
                valores["fraction_id"],
                valores["amount_cents"],
                valores["paid_on"],
                valores["method"],
                valores["reference"],
                valores["source"],
            ),
        )
        revision_id = int(cursor.lastrowid)
        for lado, conjunto in (("antes", antes), ("depois", propostas)):
            for charge_id, valor in conjunto.items():
                connection.execute(
                    """
                    INSERT INTO payment_allocation_revisions (revision_id, lado, charge_id, amount_cents)
                    VALUES (?, ?, ?, ?)
                    """,
                    (revision_id, lado, charge_id, valor),
                )

        # Retirar e repor a distribuição: o rasto do que estava fica na correção.
        connection.execute("DELETE FROM payment_allocations WHERE payment_id = ?", (payment_id,))
        cursor = connection.execute(
            """
            UPDATE payments
            SET fraction_id = ?, amount_cents = ?, paid_on = ?, method = ?,
                reference = ?, source = ?, edited_at = ?, edit_reason = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (
                valores["fraction_id"],
                valores["amount_cents"],
                valores["paid_on"],
                valores["method"],
                valores["reference"],
                valores["source"],
                agora,
                motivo,
                agora,
                payment_id,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash(
                "Este pagamento estava anulado e não foi corrigido. Nenhuma alteração "
                "foi gravada.",
                "erro",
            )
            return redirect(url_for("payments.payment_detail", payment_id=payment_id))

        for charge_id, valor in propostas.items():
            connection.execute(
                """
                INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (payment_id, charge_id, valor, agora),
            )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível corrigir o pagamento "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi gravada.",
            "erro",
        )
        return redirect(url_for("payments.edit_payment", payment_id=payment_id))

    flash(
        f"Pagamento corrigido: {format_money(int(pagamento['amount_cents']))} da fração "
        f"{pagamento['code']} passou a {format_money(valores['amount_cents'])} da fração "
        f"{valores['code']}, em {format_date(valores['paid_on'])}. O registo anterior "
        f"conserva-se em Correções, com o motivo «{motivo}». Não foi criado um segundo "
        "pagamento: a receita conta uma única vez.",
        "sucesso",
    )
    return redirect(url_for("payments.payment_detail", payment_id=payment_id))


@bp.route("/payments/<int:payment_id>/history")
def payment_history(payment_id: int):
    """Correções de um pagamento: os valores anteriores, os novos, a data e o motivo."""
    pagamento = _pagamento_or_404(payment_id)
    return render_template(
        "payment_history.html",
        pagamento={
            "id": pagamento["id"],
            "code": pagamento["code"],
            "amount_texto": format_money(int(pagamento["amount_cents"])),
            "paid_on": format_date(pagamento["paid_on"]),
            "method_texto": ROTULOS_MEIO[pagamento["method"]],
            "reference": pagamento["reference"] or "",
            "source": pagamento["source"] or "",
            "edited_at_texto": _momento_texto(pagamento["edited_at"]),
            "edit_reason": pagamento["edit_reason"] or "",
            "anulado": pagamento["cancelled_at"] is not None,
        },
        correcoes=_correcoes_de_pagamento(payment_id),
    )


# ---------------------------------------------------------------------------
# Consulta: pagamentos e saldos
# ---------------------------------------------------------------------------


@bp.route("/payments")
def payments_list():
    """Pagamentos registados e saldos por fração."""
    pagamentos = []
    for row in query_db(
        """
        SELECT p.*, f.code,
               COALESCE((SELECT SUM(amount_cents) FROM payment_allocations a
                         WHERE a.payment_id = p.id), 0) AS afetado,
               (SELECT COUNT(*) FROM payment_revisions r WHERE r.payment_id = p.id) AS correcoes
        FROM payments p
        JOIN fractions f ON f.id = p.fraction_id
        ORDER BY p.paid_on DESC, p.id DESC
        """
    ):
        pagamentos.append(
            {
                "id": row["id"],
                "code": row["code"],
                "paid_on": format_date(row["paid_on"]),
                "amount_cents": int(row["amount_cents"]),
                "method_texto": ROTULOS_MEIO[row["method"]],
                "reference": row["reference"],
                "afetado_cents": int(row["afetado"]),
                "credito_cents": int(row["amount_cents"]) - int(row["afetado"]),
                "anulado": row["cancelled_at"] is not None,
                "cancelled_at": format_date(row["cancelled_at"]) if row["cancelled_at"] else "",
                "cancellation_reason": row["cancellation_reason"] or "",
                "correcoes": int(row["correcoes"]),
                "edited_at_texto": _momento_texto(row["edited_at"]),
            }
        )

    saldos = []
    for fracao in query_db("SELECT id, code, active FROM fractions ORDER BY code"):
        totais = _totais(fracao["id"])
        saldos.append(
            {
                "id": fracao["id"],
                "code": fracao["code"],
                "active": bool(fracao["active"]),
                **totais,
                **_totais_texto(totais),
            }
        )

    globais = _totais(None)
    valores = get_all_settings()
    return render_template(
        "payments_list.html",
        pagamentos=pagamentos,
        saldos=saldos,
        globais={**globais, **_totais_texto(globais)},
        saldo_inicial_confirmado=bool(valores["saldo_inicial_caixa_centimos"].strip()),
        saldo_caixa_texto=format_money(parse_stored_cents(valores["saldo_inicial_caixa_centimos"])),
        quota_dia_vencimento=valores["quota_dia_vencimento"] or "por confirmar",
    )


@bp.route("/payments/fraction/<int:fraction_id>")
def fraction_balance(fraction_id: int):
    """Histórico financeiro de uma fração: cobranças, pagamentos, afetações, dívida e crédito."""
    fracao = query_db("SELECT * FROM fractions WHERE id = ?", (fraction_id,), one=True)
    if fracao is None:
        abort(404)

    pagamentos = []
    for row in query_db(
        """
        SELECT p.*,
               COALESCE((SELECT SUM(amount_cents) FROM payment_allocations a
                         WHERE a.payment_id = p.id), 0) AS afetado,
               (SELECT COUNT(*) FROM payment_revisions r WHERE r.payment_id = p.id) AS correcoes
        FROM payments p
        WHERE p.fraction_id = ?
        ORDER BY p.paid_on, p.id
        """,
        (fraction_id,),
    ):
        pagamentos.append(
            {
                "id": row["id"],
                "paid_on": format_date(row["paid_on"]),
                "amount_cents": int(row["amount_cents"]),
                "method_texto": ROTULOS_MEIO[row["method"]],
                "reference": row["reference"],
                "afetado_cents": int(row["afetado"]),
                "credito_cents": int(row["amount_cents"]) - int(row["afetado"]),
                "anulado": row["cancelled_at"] is not None,
                "cancelled_at": format_date(row["cancelled_at"]) if row["cancelled_at"] else "",
                "cancellation_reason": row["cancellation_reason"] or "",
                "correcoes": int(row["correcoes"]),
                "edited_at_texto": _momento_texto(row["edited_at"]),
                "afetacoes": _afetacoes_de_pagamento(row["id"]),
            }
        )

    totais = _totais(fraction_id)
    valores = get_all_settings()
    return render_template(
        "fraction_balance.html",
        fracao={
            "id": fracao["id"],
            "code": fracao["code"],
            "active": bool(fracao["active"]),
        },
        cobrancas=_estado_das_cobrancas(fraction_id),
        pagamentos=pagamentos,
        totais={**totais, **_totais_texto(totais)},
        saldo_inicial_confirmado=bool(valores["saldo_inicial_caixa_centimos"].strip()),
        saldo_caixa_texto=format_money(parse_stored_cents(valores["saldo_inicial_caixa_centimos"])),
    )
