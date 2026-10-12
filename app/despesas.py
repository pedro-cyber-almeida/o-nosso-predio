"""Despesas, categorias e pagamentos de despesas (entrega 3).

Regras aplicadas nesta entrega:
- Uma despesa é uma obrigação registada, não dinheiro que saiu: registar uma
  despesa não altera a caixa. O que sai são os pagamentos de despesa, registados
  um por movimento, com data, valor, meio e referência opcional.
- O valor em falta é calculado (valor - pagamentos válidos), nunca guardado.
  Pagar acima do valor em falta é recusado pela aplicação e travado na base.
- Pagamentos parciais: uma despesa pode receber vários pagamentos até ficar paga.
- Vencida só é uma despesa com vencimento passado e montante ainda por pagar.
  Sem vencimento não há classificação de vencida.
- Editar uma despesa ou um pagamento de despesa exige motivo, é pré-visualizado
  antes de gravar e fica guardado no rasto de correções, com os valores anteriores
  e novos e a identidade de quem corrigiu. O registo continua um só: corrigir não
  duplica movimentos.
- Anular um pagamento exige motivo, conserva o registo e reabre o valor em falta.
  Uma anulação não é uma devolução bancária: isso seria um movimento novo.
- Uma despesa com pagamentos válidos não é anulada: primeiro anulam-se os
  pagamentos. Reduzir o valor de uma despesa abaixo do já pago é recusado até a
  incompatibilidade estar resolvida.
- As categorias são definidas pelo administrador; não há lista rígida no código.
  Uma categoria em uso não é renomeada nem apagada: as despesas conservam o
  rótulo com que foram registadas.
- Registar, corrigir e anular são administração. O portal do condómino não mostra
  despesas: a proteção é no servidor.
- A referência documental (nº de fatura, recibo) é texto livre: fica preparada
  para a ligação a documentos das entregas seguintes.
"""

from __future__ import annotations

import secrets
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from .auth import current_user
from .concurrency import (
    FORM_DO_PEDIDO,
    MENSAGEM_DESATUALIZADO,
    desatualizado,
    revisao_despesas,
    transacao_de_escrita,
)
from .db import get_db, query_db, utc_now_iso
from .payments import MEIOS, ROTULOS_MEIO
from .units import (
    format_date,
    format_money,
    format_money_input,
    parse_date,
    parse_int_id,
    parse_money,
    today_iso,
)

bp = Blueprint("despesas", __name__)


@bp.before_request
def _apenas_administrador():
    """Despesas detalhadas e a sua gestão são reservadas ao administrador."""
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()


CLASSIFICACOES = ("ordinario", "fundo_reserva")
ROTULOS_CLASSIFICACAO = {
    "ordinario": "Ordinário",
    "fundo_reserva": "Fundo de reserva",
}

# Um valor acima disto é quase de certeza uma gralha de escrita.
VALOR_MAXIMO_CENTIMOS = 10_000_000  # 100.000 EUR
LIMITE_DESCRICAO = 120
LIMITE_FORNECEDOR = 120
LIMITE_REFERENCIA = 120
LIMITE_NOTAS = 400
LIMITE_CATEGORIA = 60
LIMITE_MOTIVO = 200


@bp.app_context_processor
def _contexto_despesas():
    return {
        "CLASSIFICACOES": CLASSIFICACOES,
        "ROTULOS_CLASSIFICACAO": ROTULOS_CLASSIFICACAO,
        "LIMITE_CATEGORIA": LIMITE_CATEGORIA,
        "revisao_despesas": revisao_despesas,
    }


def _recusar_se_desatualizado() -> bool:
    if not desatualizado(request.form, "despesas"):
        return False
    flash(MENSAGEM_DESATUALIZADO, "erro")
    return True


def _novo_token_de_formulario() -> str:
    """Marca única deste formulário, gravada no registo que ele originar."""
    return secrets.token_urlsafe(16)


def _nome_de_quem_regista() -> str:
    """Nome de quem fez o registo. Não é inventado: vem da sessão, se houver."""
    utilizador = current_user()
    return str(utilizador["username"]) if utilizador else ""


# ---------------------------------------------------------------------------
# Cálculos: o que está pago e o que falta
# ---------------------------------------------------------------------------

_PAGO_VALIDO = """
    COALESCE((
        SELECT SUM(p.amount_cents)
        FROM expense_payments AS p
        WHERE p.expense_id = e.id AND p.cancelled_at IS NULL
    ), 0)
"""


def _despesa_estado(expense_id: int) -> dict | None:
    """Estado calculado de uma despesa: valor, pago, em falta e classificação."""
    linha = query_db(
        f"""
        SELECT e.id, e.descricao, e.fornecedor, e.category_id, e.referencia, e.data,
               e.due_date, e.amount_cents, e.classificacao, e.notas, e.cancelled_at,
               e.cancellation_reason, e.created_at, e.updated_at,
               c.nome AS categoria_nome,
               {_PAGO_VALIDO} AS pago
        FROM expenses AS e
        JOIN expense_categories AS c ON c.id = e.category_id
        WHERE e.id = ?
        """,
        (expense_id,),
        one=True,
    )
    if linha is None:
        return None
    valor = int(linha["amount_cents"])
    pago = int(linha["pago"])
    em_falta = valor - pago
    hoje = today_iso()
    anulada = linha["cancelled_at"] is not None
    # Vencida: vencimento passado e montante ainda por pagar. Sem vencimento,
    # não há classificação de vencida, por muito antiga que a despesa seja.
    vencida = (
        not anulada
        and linha["due_date"] is not None
        and linha["due_date"] < hoje
        and em_falta > 0
    )
    if anulada:
        estado = "anulada"
    elif em_falta == 0:
        estado = "paga"
    elif vencida:
        estado = "vencida"
    elif pago > 0:
        estado = "parcial"
    else:
        estado = "por_pagar"
    return {
        "id": int(linha["id"]),
        "descricao": linha["descricao"],
        "fornecedor": linha["fornecedor"] or "",
        "category_id": int(linha["category_id"]),
        "categoria_nome": linha["categoria_nome"],
        "referencia": linha["referencia"] or "",
        "data": linha["data"],
        "data_texto": format_date(linha["data"]),
        "due_date": linha["due_date"],
        "due_texto": format_date(linha["due_date"]) if linha["due_date"] else "por confirmar",
        "amount_cents": valor,
        "amount_texto": format_money(valor),
        "classificacao": linha["classificacao"],
        "classificacao_texto": ROTULOS_CLASSIFICACAO[linha["classificacao"]],
        "notas": linha["notas"] or "",
        "pago_cents": pago,
        "pago_texto": format_money(pago),
        "em_falta_cents": em_falta,
        "em_falta_texto": format_money(em_falta),
        "vencida": vencida,
        "anulada": anulada,
        "cancelled_at": format_date(linha["cancelled_at"]) if linha["cancelled_at"] else "",
        "cancellation_reason": linha["cancellation_reason"] or "",
        "estado": estado,
    }


def _despesa_or_404(expense_id: int) -> dict:
    estado = _despesa_estado(expense_id)
    if estado is None:
        abort(404)
    return estado


def _pagamentos_da_despesa(expense_id: int) -> list[dict]:
    pagamentos = []
    for row in query_db(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM expense_payment_revisions r WHERE r.payment_id = p.id) AS correcoes
        FROM expense_payments p
        WHERE p.expense_id = ?
        ORDER BY p.paid_on, p.id
        """,
        (expense_id,),
    ):
        pagamentos.append(
            {
                "id": int(row["id"]),
                "amount_cents": int(row["amount_cents"]),
                "amount_texto": format_money(int(row["amount_cents"])),
                "paid_on_texto": format_date(row["paid_on"]),
                "method_texto": ROTULOS_MEIO[row["method"]],
                "reference": row["reference"] or "",
                "anulado": row["cancelled_at"] is not None,
                "cancelled_at": format_date(row["cancelled_at"]) if row["cancelled_at"] else "",
                "cancellation_reason": row["cancellation_reason"] or "",
                "correcoes": int(row["correcoes"]),
            }
        )
    return pagamentos


def _correcoes_da_despesa(expense_id: int) -> list[dict]:
    linhas = []
    for row in query_db(
        """
        SELECT r.*, cb.nome AS categoria_antes, ca.nome AS categoria_depois
        FROM expense_revisions AS r
        JOIN expense_categories AS cb ON cb.id = r.category_before
        JOIN expense_categories AS ca ON ca.id = r.category_after
        WHERE r.expense_id = ?
        ORDER BY r.id
        """,
        (expense_id,),
    ):
        linhas.append(
            {
                "momento_texto": _momento_texto(row["changed_at"]),
                "motivo": row["motivo"],
                "por": row["changed_by"] or "escrita direta",
                "valor_antes_texto": format_money(int(row["amount_before"])),
                "valor_depois_texto": format_money(int(row["amount_after"])),
                "descricao_antes": row["descricao_before"],
                "descricao_depois": row["descricao_after"],
                "categoria_antes": row["categoria_antes"],
                "categoria_depois": row["categoria_depois"],
                "vencimento_antes": format_date(row["due_before"]) if row["due_before"] else "por confirmar",
                "vencimento_depois": format_date(row["due_after"]) if row["due_after"] else "por confirmar",
                "classificacao_antes": ROTULOS_CLASSIFICACAO[row["classificacao_before"]],
                "classificacao_depois": ROTULOS_CLASSIFICACAO[row["classificacao_after"]],
            }
        )
    return linhas


def _correcoes_do_pagamento(payment_id: int) -> list[dict]:
    linhas = []
    for row in query_db(
        "SELECT * FROM expense_payment_revisions WHERE payment_id = ? ORDER BY id",
        (payment_id,),
    ):
        linhas.append(
            {
                "momento_texto": _momento_texto(row["changed_at"]),
                "motivo": row["motivo"],
                "por": row["changed_by"] or "escrita direta",
                "valor_antes_texto": format_money(int(row["amount_before"])),
                "valor_depois_texto": format_money(int(row["amount_after"])),
                "data_antes": format_date(row["paid_on_before"]),
                "data_depois": format_date(row["paid_on_after"]),
                "meio_antes": ROTULOS_MEIO[row["method_before"]],
                "meio_depois": ROTULOS_MEIO[row["method_after"]],
                "referencia_antes": row["reference_before"] or "",
                "referencia_depois": row["reference_after"] or "",
            }
        )
    return linhas


def _momento_texto(iso: object) -> str:
    """Momento guardado, apresentado em DD/MM/YYYY HH:MM:SS."""
    texto = str(iso or "").strip()
    if not texto:
        return ""
    data, separador, hora = texto.partition("T")
    if not separador:
        return format_date(data)
    return f"{format_date(data)} às {hora[:8]}"


def _categorias() -> list:
    return query_db("SELECT id, nome FROM expense_categories ORDER BY nome")


# ---------------------------------------------------------------------------
# Categorias (definidas pelo administrador, sem lista rígida)
# ---------------------------------------------------------------------------


@bp.route("/despesas/categorias", methods=["GET", "POST"])
def categories():
    erros: list[str] = []
    if request.method == "POST":
        if _recusar_se_desatualizado():
            return redirect(url_for("despesas.categories"))

        acao = request.form.get("acao", "").strip()
        categoria_id = parse_int_id(request.form.get("categoria_id", ""))
        nome = request.form.get("nome", "").strip()

        # Nomes existentes, sem contar a própria categoria: eliminar e renomear
        # enviam o nome atual da categoria no formulário, e isso não é duplicado.
        nomes_existentes = {
            row["nome"].lower()
            for row in _categorias()
            if categoria_id is None or int(row["id"]) != categoria_id
        }
        if not nome:
            erros.append("Nome da categoria: escreve um nome, por exemplo «Limpeza».")
        elif len(nome) > LIMITE_CATEGORIA:
            erros.append(f"Nome da categoria: no máximo {LIMITE_CATEGORIA} caracteres.")
        elif acao != "eliminar" and nome.lower() in nomes_existentes:
            erros.append(f"Já existe uma categoria «{nome}»: usa a existente ou escreve outro nome.")

        categoria = (
            query_db("SELECT id, nome FROM expense_categories WHERE id = ?", (categoria_id,), one=True)
            if categoria_id is not None
            else None
        )
        if acao in ("renomear", "eliminar") and categoria is None:
            erros.append("Categoria: escolhe uma categoria registada.")

        if not erros:
            if not transacao_de_escrita("despesas"):
                flash(MENSAGEM_DESATUALIZADO, "erro")
                return redirect(url_for("despesas.categories"))
            connection = get_db()
            try:
                now = utc_now_iso()
                if acao == "renomear":
                    cursor = connection.execute(
                        "UPDATE expense_categories SET nome = ?, updated_at = ? WHERE id = ?",
                        (nome, now, categoria["id"]),
                    )
                    mensagem = f"Categoria «{categoria['nome']}» renomeada para «{nome}»."
                elif acao == "eliminar":
                    connection.execute("DELETE FROM expense_categories WHERE id = ?", (categoria["id"],))
                    mensagem = f"Categoria «{categoria['nome']}» eliminada: nenhuma despesa a usava."
                else:
                    connection.execute(
                        "INSERT INTO expense_categories (nome, created_at, updated_at) VALUES (?, ?, ?)",
                        (nome, now, now),
                    )
                    mensagem = f"Categoria «{nome}» criada. As categorias são definidas por ti, não pelo código."
                connection.commit()
            except sqlite3.Error as erro:
                connection.rollback()
                if "em uso" in getattr(erro, "sqlite_error", str(erro)):
                    flash(
                        "Esta categoria é usada por despesas registadas e não é renomeada nem apagada: "
                        "as despesas conservam o rótulo com que foram registadas. Cria a categoria certa "
                        "e reafeta cada despesa na edição justificada dela. Nada foi alterado.",
                        "erro",
                    )
                else:
                    flash(
                        f"Não foi possível alterar a categoria ({getattr(erro, 'sqlite_error', erro)}). "
                        "Nada foi gravado.",
                        "erro",
                    )
                return redirect(url_for("despesas.categories"))
            flash(mensagem, "sucesso")
            return redirect(url_for("despesas.categories"))

        for mensagem in erros:
            flash(mensagem, "erro")

    categorias = []
    for row in _categorias():
        uso = query_db(
            "SELECT COUNT(*) AS n FROM expenses WHERE category_id = ?", (row["id"],), one=True
        )
        categorias.append({"id": int(row["id"]), "nome": row["nome"], "uso": int(uso["n"])})
    return render_template("expense_categories.html", categorias=categorias)


# ---------------------------------------------------------------------------
# Registar uma despesa
# ---------------------------------------------------------------------------


def _ler_despesa_do_form(form) -> tuple[dict | None, list[str]]:
    erros: list[str] = []

    descricao = form.get("descricao", "").strip()
    if not descricao:
        erros.append("Descrição: escreve o que esta despesa é, por exemplo «Limpeza do hall».")
    elif len(descricao) > LIMITE_DESCRICAO:
        erros.append(f"Descrição: no máximo {LIMITE_DESCRICAO} caracteres.")

    fornecedor = form.get("fornecedor", "").strip()
    if len(fornecedor) > LIMITE_FORNECEDOR:
        erros.append(f"Fornecedor: no máximo {LIMITE_FORNECEDOR} caracteres.")

    categoria_id = parse_int_id(form.get("categoria", ""))
    categoria = (
        query_db("SELECT id, nome FROM expense_categories WHERE id = ?", (categoria_id,), one=True)
        if categoria_id is not None
        else None
    )
    if categoria is None:
        erros.append(
            "Categoria: escolhe uma categoria registada. Se falta uma, cria-a em Despesas → Categorias."
        )

    referencia = form.get("referencia", "").strip()
    if len(referencia) > LIMITE_REFERENCIA:
        erros.append(f"Referência documental: no máximo {LIMITE_REFERENCIA} caracteres.")

    data = parse_date(form.get("data", ""))
    if data is None:
        erros.append("Data da despesa: usa DD/MM/YYYY.")

    vencimento: str | None = None
    bruto_vencimento = form.get("vencimento", "").strip()
    if bruto_vencimento:
        vencimento = parse_date(bruto_vencimento)
        if vencimento is None:
            erros.append("Vencimento: escreve a data como DD/MM/YYYY ou deixa vazio.")

    valor = parse_money(form.get("valor", ""))
    if valor is None or valor <= 0:
        erros.append("Valor: usa um valor positivo em euros, por exemplo 120,00.")
    elif valor > VALOR_MAXIMO_CENTIMOS:
        erros.append(
            "Valor: acima de 100.000,00 EUR parece um erro de escrita. Confirma o valor e tenta de novo."
        )

    classificacao = form.get("classificacao", "").strip()
    if classificacao not in CLASSIFICACOES:
        erros.append("Classificação: escolhe ordinário ou fundo de reserva.")

    notas = form.get("notas", "").strip()
    if len(notas) > LIMITE_NOTAS:
        erros.append(f"Notas: no máximo {LIMITE_NOTAS} caracteres.")

    if erros:
        return None, erros

    return {
        "descricao": descricao,
        "fornecedor": fornecedor,
        "category_id": int(categoria["id"]),
        "referencia": referencia,
        "data": data,
        "due_date": vencimento,
        "amount_cents": valor,
        "classificacao": classificacao,
        "notas": notas,
    }, erros


def registar_despesa(despesa: dict, token: str, doc_id: int | None,
                     nota_origem: str = "registada a partir do documento",
                     form=FORM_DO_PEDIDO) -> tuple[int | None, str]:
    """Inserir uma despesa na transação de escrita, com idempotência por marca.

    É o mesmo caminho do formulário manual. Devolve (expense_id, '') no
    sucesso, (expense_id, 'repetido') se a marca já existia e (None, erro)
    em falha. Usado pelo formulário e pela automatização bancária
    (que passa `form=None`: escrita de sistema, sem revisão de formulário).
    """
    if not transacao_de_escrita("despesas", form=form):
        return None, "desatualizado"
    connection = get_db()
    try:
        # Dentro da transação de escrita: o reenvio do mesmo formulário traz a
        # mesma marca e não cria uma segunda despesa igual.
        repetido = query_db("SELECT id FROM expenses WHERE form_token = ?", (token,), one=True)
        if repetido is not None:
            connection.rollback()
            return int(repetido["id"]), "repetido"

        now = utc_now_iso()
        cursor = connection.execute(
            """
            INSERT INTO expenses
                (descricao, fornecedor, category_id, referencia, data, due_date,
                 amount_cents, classificacao, notas, created_at, updated_at, form_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                despesa["descricao"],
                despesa["fornecedor"],
                despesa["category_id"],
                despesa["referencia"],
                despesa["data"],
                despesa["due_date"],
                despesa["amount_cents"],
                despesa["classificacao"],
                despesa["notas"],
                now,
                now,
                token,
            ),
        )
        expense_id = int(cursor.lastrowid)
        # Registo feito a partir de um documento: a ligação é um rasto,
        # gravado na mesma transação. Não cria nem altera movimentos.
        if doc_id is not None:
            from .documentos import ligar_documento

            ligar_documento(doc_id, "despesa", expense_id, nota_origem)
        connection.commit()
        return expense_id, ""
    except sqlite3.Error as erro:
        connection.rollback()
        return None, str(getattr(erro, "sqlite_error", erro))


def pagar_despesa(expense_id: int, valor: int, data: str, meio: str, referencia: str,
                  token: str, form=FORM_DO_PEDIDO) -> tuple[bool, str, dict]:
    """Registar a saída de dinheiro de uma despesa, na transação de escrita.

    O valor em falta é relido dentro da transação: outro pedido não pode ter
    pago entretanto sem que esta escrita seja recusada. Devolve
    (ok, falha, contexto) — contexto traz `agora_pago`/`agora_valor` quando
    a falha é 'excedido'. Usado pelo formulário e pela automatização bancária
    (que passa `form=None`: escrita de sistema, sem revisão de formulário).
    """
    if not transacao_de_escrita("despesas", form=form):
        return False, "desatualizado", {}
    connection = get_db()
    try:
        repetido = query_db(
            "SELECT id FROM expense_payments WHERE form_token = ?", (token,), one=True
        )
        if repetido is not None:
            connection.rollback()
            return False, "repetido", {}

        agora_pago = int(
            query_db(
                "SELECT COALESCE(SUM(amount_cents), 0) AS v FROM expense_payments "
                "WHERE expense_id = ? AND cancelled_at IS NULL",
                (expense_id,),
                one=True,
            )["v"]
        )
        agora_valor = int(
            query_db("SELECT amount_cents FROM expenses WHERE id = ?", (expense_id,), one=True)["amount_cents"]
        )
        if valor > agora_valor - agora_pago:
            connection.rollback()
            return False, "excedido", {"agora_pago": agora_pago, "agora_valor": agora_valor}

        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO expense_payments
                (expense_id, amount_cents, paid_on, method, reference,
                 created_at, updated_at, form_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (expense_id, valor, data, meio, referencia, now, now, token),
        )
        connection.commit()
        return True, "", {}
    except sqlite3.Error as erro:
        connection.rollback()
        return False, str(getattr(erro, "sqlite_error", erro)), {}


@bp.route("/despesas/nova", methods=["GET", "POST"])
def new_expense():
    if request.method == "POST":
        if _recusar_se_desatualizado():
            return redirect(url_for("despesas.new_expense"))

        despesa, erros = _ler_despesa_do_form(request.form)
        if request.form.get("confirmar_despesa") != "on":
            erros.append(
                "Registar uma despesa exige confirmar que o valor, a data e a categoria "
                "correspondem a uma despesa real do prédio. Nenhuma despesa foi guardada."
            )
        token = request.form.get("form_token", "").strip()
        if not token:
            erros.append(
                "Este formulário não tem a marca de envio: reabre a página de despesas e "
                "tenta de novo. Nenhuma despesa foi guardada."
            )

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
            return redirect(url_for("despesas.new_expense"))

        doc_id = parse_int_id(request.form.get("doc", ""))
        expense_id, falha = registar_despesa(despesa, token, doc_id)
        if falha == "desatualizado":
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("despesas.new_expense"))
        if falha == "repetido":
            flash(
                "Este formulário já tinha sido enviado: a despesa já está registada "
                "e não foi duplicada. Se for preciso corrigir um valor, edita a despesa.",
                "erro",
            )
            return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
        if falha:
            flash(
                f"Não foi possível registar a despesa ({falha}). Nenhuma despesa foi guardada.",
                "erro",
            )
            return redirect(url_for("despesas.new_expense"))

        flash(
            f"Despesa registada: {despesa['descricao']} — {format_money(despesa['amount_cents'])}. "
            "Registar uma despesa não é saída de dinheiro: a caixa só muda quando "
            "registares o pagamento.",
            "sucesso",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    session["expense_form_token"] = _novo_token_de_formulario()
    from .documentos import documento_para_formulario

    return render_template(
        "expense_new.html",
        categorias=_categorias(),
        data=today_iso(),
        form_token=session["expense_form_token"],
        documento=documento_para_formulario(request.args.get("doc", "")),
    )


# ---------------------------------------------------------------------------
# Lista e detalhe
# ---------------------------------------------------------------------------


@bp.route("/despesas")
def expenses_list():
    linhas = []
    for row in query_db(
        f"""
        SELECT e.id FROM expenses AS e ORDER BY e.data DESC, e.id DESC
        """
    ):
        estado = _despesa_estado(int(row["id"]))
        if estado is not None:
            linhas.append(estado)

    ativos = [linha for linha in linhas if not linha["anulada"]]
    totais = {
        "registadas": sum(l["amount_cents"] for l in ativos),
        "pagas": sum(l["pago_cents"] for l in ativos),
        "em_falta": sum(l["em_falta_cents"] for l in ativos),
        "vencidas": sum(l["em_falta_cents"] for l in ativos if l["vencida"]),
    }
    return render_template(
        "expense_list.html",
        despesas=linhas,
        totais=totais,
        total_textos={
            chave: format_money(valor) for chave, valor in totais.items()
        },
        tem_despesas=bool(linhas),
        tem_categorias=bool(_categorias()),
    )


@bp.route("/despesas/<int:expense_id>")
def expense_detail(expense_id: int):
    despesa = _despesa_or_404(expense_id)
    return render_template(
        "expense_detail.html",
        despesa=despesa,
        pagamentos=_pagamentos_da_despesa(expense_id),
        correcoes=_correcoes_da_despesa(expense_id),
        form_token=_novo_token_de_formulario(),
        hoje_texto=format_date(today_iso()),
    )


# ---------------------------------------------------------------------------
# Pagar uma despesa (parcial ou total)
# ---------------------------------------------------------------------------


@bp.route("/despesas/<int:expense_id>/pagar", methods=["POST"])
def pay_expense(expense_id: int):
    despesa = _despesa_or_404(expense_id)
    if despesa["anulada"]:
        flash(
            "Esta despesa está anulada e não recebe pagamentos. Se a despesa é real, "
            "registra-a outra vez.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    if _recusar_se_desatualizado():
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    erros: list[str] = []
    valor = parse_money(request.form.get("valor", ""))
    if valor is None or valor <= 0:
        erros.append("Valor pago: usa um valor positivo em euros, por exemplo 60,00.")
    elif valor > VALOR_MAXIMO_CENTIMOS:
        erros.append("Valor pago: acima de 100.000,00 EUR parece um erro de escrita.")
    elif valor > despesa["em_falta_cents"]:
        erros.append(
            f"Valor pago: faltam {despesa['em_falta_texto']} e o valor proposto é "
            f"{format_money(valor)}. Um pagamento não pode exceder o valor em falta. "
            "Regista um pagamento parcial ou corrige primeiro a despesa."
        )

    data = parse_date(request.form.get("data", ""))
    if data is None:
        erros.append("Data do pagamento: usa DD/MM/YYYY.")
    elif data > today_iso():
        erros.append(
            "Data do pagamento: não pode ser futura. Regista o dinheiro no dia em que "
            "efetivamente saiu."
        )

    meio = request.form.get("meio", "").strip()
    if meio not in MEIOS:
        erros.append("Meio de pagamento: escolhe uma das opções.")

    referencia = request.form.get("referencia", "").strip()
    if len(referencia) > LIMITE_REFERENCIA:
        erros.append(f"Referência: no máximo {LIMITE_REFERENCIA} caracteres.")

    if request.form.get("confirmar_pagamento") != "on":
        erros.append(
            "Registar um pagamento de despesa exige confirmar que este dinheiro efetivamente "
            "saiu e que o valor e a data correspondem a um comprovativo. Nenhum pagamento foi "
            "guardado."
        )
    token = request.form.get("form_token", "").strip()
    if not token:
        erros.append(
            "Este formulário não tem a marca de envio: reabre a despesa e tenta de novo. "
            "Nenhum pagamento foi guardado."
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    ok, falha, contexto = pagar_despesa(expense_id, valor, data, meio, referencia, token)
    if falha == "desatualizado":
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
    if falha == "repetido":
        flash(
            "Este formulário já tinha sido enviado: o pagamento já está registado e "
            "não foi duplicado.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
    if falha == "excedido":
        flash(
            f"Entretanto esta despesa já tem {format_money(contexto['agora_pago'])} pagos: faltam "
            f"{format_money(contexto['agora_valor'] - contexto['agora_pago'])} e o pagamento proposto é "
            f"{format_money(valor)}. Nada foi gravado.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
    if falha:
        flash(
            f"Não foi possível registar o pagamento ({falha}). Nenhum pagamento foi guardado.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    estado = _despesa_estado(expense_id)
    flash(
        f"Pagamento registado: {format_money(valor)} de {despesa['descricao']}, em "
        f"{format_date(data)}. Esta é a saída de dinheiro: falta {estado['em_falta_texto']} "
        f"({estado['estado']}).",
        "sucesso",
    )
    return redirect(url_for("despesas.expense_detail", expense_id=expense_id))


# ---------------------------------------------------------------------------
# Editar uma despesa: correção justificada, com pré-visualização dos efeitos
# ---------------------------------------------------------------------------


def _ler_edicao_despesa_do_form(despesa: dict, form) -> tuple[dict | None, str, str, list[str]]:
    valores, erros = _ler_despesa_do_form(form)

    motivo = form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Motivo da correção: escreve porque esta despesa está errada. O motivo fica "
            "guardado com os valores anteriores e nenhuma despesa é corrigida sem ele."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo da correção: no máximo {LIMITE_MOTIVO} caracteres.")

    token = form.get("edit_token", "").strip()
    if not token:
        erros.append(
            "Este formulário não tem a marca de envio: reabre a edição da despesa e tenta "
            "de novo. Nenhuma alteração foi feita."
        )

    if valores is not None and valores["amount_cents"] < despesa["pago_cents"]:
        erros.append(
            f"Já foram pagos {despesa['pago_texto']} a esta despesa. Baixá-la para "
            f"{format_money(valores['amount_cents']) if valores['amount_cents'] > 0 else 'esse valor'} "
            "deixá-la-ia abaixo do que está pago. Resolve primeiro a incompatibilidade: "
            "anula os pagamentos a mais, com motivo, e volta a corrigir a despesa."
        )

    return valores, motivo, token, erros


def _efeitos_da_edicao_despesa(despesa: dict, valores: dict) -> dict:
    """O que esta correção muda na despesa, antes de ela ser gravada."""
    pago = despesa["pago_cents"]
    falta_antes = despesa["em_falta_cents"]
    falta_depois = valores["amount_cents"] - pago
    hoje = today_iso()
    vencida_depois = (
        valores["due_date"] is not None
        and valores["due_date"] < hoje
        and falta_depois > 0
    )
    return {
        "valor_antes_texto": despesa["amount_texto"],
        "valor_depois_texto": format_money(valores["amount_cents"]),
        "pago_texto": despesa["pago_texto"],
        "falta_antes_texto": despesa["em_falta_texto"],
        "falta_depois_texto": format_money(falta_depois),
        "liquidada_depois": falta_depois == 0,
        "vencida_depois": vencida_depois,
        "mudou_valor": valores["amount_cents"] != despesa["amount_cents"],
        "vencimento_depois_texto": (
            format_date(valores["due_date"]) if valores["due_date"] else "por confirmar"
        ),
        "categoria_depois_texto": query_db(
            "SELECT nome FROM expense_categories WHERE id = ?", (valores["category_id"],), one=True
        )["nome"],
    }


@bp.route("/despesas/<int:expense_id>/editar", methods=["GET", "POST"])
def edit_expense(expense_id: int):
    despesa = _despesa_or_404(expense_id)
    if despesa["anulada"]:
        flash(
            "Esta despesa está anulada e não é editada: uma anulação não se repete. "
            "Se a despesa é real, registra-a outra vez.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    def pagina(valores: dict, efeitos: dict | None):
        return render_template(
            "expense_edit.html",
            despesa=despesa,
            valores=valores,
            efeitos=efeitos,
            categorias=_categorias(),
            edit_token=_novo_token_de_formulario(),
        )

    if request.method == "GET":
        return pagina(
            {
                "descricao": despesa["descricao"],
                "fornecedor": despesa["fornecedor"],
                "categoria": despesa["category_id"],
                "referencia": despesa["referencia"],
                "data": despesa["data_texto"],
                "vencimento": format_date(despesa["due_date"]) if despesa["due_date"] else "",
                "valor": format_money_input(despesa["amount_cents"]),
                "classificacao": despesa["classificacao"],
                "notas": despesa["notas"],
                "motivo": "",
            },
            None,
        )

    if _recusar_se_desatualizado():
        return redirect(url_for("despesas.edit_expense", expense_id=expense_id))

    valores, motivo, token, erros = _ler_edicao_despesa_do_form(despesa, request.form)
    efeitos = _efeitos_da_edicao_despesa(despesa, valores) if valores is not None else None

    previsualizar = request.form.get("previsualizar") == "on"
    confirmar = request.form.get("confirmar_correcao") == "on"
    if not previsualizar and not confirmar:
        erros.append(
            "Pré-visualiza os efeitos da correção antes de a gravar. Nada foi alterado."
        )
    if confirmar and request.form.get("confirmar_efectos") != "on":
        erros.append(
            "Gravar a correção exige confirmar os efeitos mostrados na pré-visualização. "
            "Nada foi alterado."
        )
    if valores is not None and not erros and _mesmos_valores(despesa, valores):
        erros.append("Não há nada para corrigir: os campos são os mesmos que estão guardados.")

    def reapresentar():
        return pagina(
            {
                "descricao": request.form.get("descricao", "").strip(),
                "fornecedor": request.form.get("fornecedor", "").strip(),
                "categoria": request.form.get("categoria", "").strip(),
                "referencia": request.form.get("referencia", "").strip(),
                "data": request.form.get("data", "").strip(),
                "vencimento": request.form.get("vencimento", "").strip(),
                "valor": request.form.get("valor", "").strip(),
                "classificacao": request.form.get("classificacao", "").strip(),
                "notas": request.form.get("notas", "").strip(),
                "motivo": request.form.get("motivo", "").strip(),
            },
            efeitos,
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return reapresentar()

    if previsualizar:
        return reapresentar()

    if not transacao_de_escrita("despesas"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("despesas.edit_expense", expense_id=expense_id))

    connection = get_db()
    try:
        repetida = query_db(
            "SELECT id FROM expense_revisions WHERE edit_token = ?", (token,), one=True
        )
        if repetida is not None:
            connection.rollback()
            flash(
                "Esta correção já tinha sido aplicada: o mesmo formulário foi enviado duas "
                "vezes e o efeito não foi duplicado.",
                "erro",
            )
            return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO expense_revisions
                (expense_id, changed_at, motivo, changed_by, edit_token,
                 descricao_before, descricao_after, fornecedor_before, fornecedor_after,
                 category_before, category_after, referencia_before, referencia_after,
                 data_before, data_after, due_before, due_after,
                 amount_before, amount_after, classificacao_before, classificacao_after,
                 notas_before, notas_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expense_id,
                now,
                motivo,
                _nome_de_quem_regista(),
                token,
                despesa["descricao"],
                valores["descricao"],
                despesa["fornecedor"],
                valores["fornecedor"],
                despesa["category_id"],
                valores["category_id"],
                despesa["referencia"],
                valores["referencia"],
                despesa["data"],
                valores["data"],
                despesa["due_date"],
                valores["due_date"],
                despesa["amount_cents"],
                valores["amount_cents"],
                despesa["classificacao"],
                valores["classificacao"],
                despesa["notas"],
                valores["notas"],
            ),
        )
        cursor = connection.execute(
            """
            UPDATE expenses
            SET descricao = ?, fornecedor = ?, category_id = ?, referencia = ?,
                data = ?, due_date = ?, amount_cents = ?, classificacao = ?, notas = ?,
                edit_token = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (
                valores["descricao"],
                valores["fornecedor"],
                valores["category_id"],
                valores["referencia"],
                valores["data"],
                valores["due_date"],
                valores["amount_cents"],
                valores["classificacao"],
                valores["notas"],
                token,
                now,
                expense_id,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash("Esta despesa estava anulada e não foi corrigida. Nada foi gravado.", "erro")
            return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível corrigir a despesa "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("despesas.edit_expense", expense_id=expense_id))

    flash(
        f"Despesa corrigida: {despesa['amount_texto']} passou a "
        f"{format_money(valores['amount_cents'])}, com motivo registado. O valor em falta "
        f"passou de {despesa['em_falta_texto']} para {format_money(valores['amount_cents'] - despesa['pago_cents'])}. "
        "Os pagamentos conservam-se e o rasto anterior ficou no histórico.",
        "sucesso",
    )
    return redirect(url_for("despesas.expense_detail", expense_id=expense_id))


def _mesmos_valores(despesa: dict, valores: dict) -> bool:
    return (
        valores["descricao"] == despesa["descricao"]
        and valores["fornecedor"] == despesa["fornecedor"]
        and valores["category_id"] == despesa["category_id"]
        and valores["referencia"] == despesa["referencia"]
        and valores["data"] == despesa["data"]
        and valores["due_date"] == despesa["due_date"]
        and valores["amount_cents"] == despesa["amount_cents"]
        and valores["classificacao"] == despesa["classificacao"]
        and valores["notas"] == despesa["notas"]
    )


# ---------------------------------------------------------------------------
# Anular uma despesa
# ---------------------------------------------------------------------------


@bp.route("/despesas/<int:expense_id>/anular", methods=["POST"])
def cancel_expense(expense_id: int):
    despesa = _despesa_or_404(expense_id)

    if _recusar_se_desatualizado():
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    erros: list[str] = []
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Anular uma despesa exige um motivo escrito, que fica guardado com o registo. "
            "Nenhuma anulação foi feita."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo: no máximo {LIMITE_MOTIVO} caracteres.")
    if request.form.get("confirmar_anular") != "on":
        erros.append("Anular exige confirmação explícita. Nenhuma anulação foi feita.")
    if despesa["anulada"]:
        erros.append(
            "Esta despesa já foi anulada em "
            f"{despesa['cancelled_at']} ({despesa['cancellation_reason']}). "
            "Uma anulação não se repete, e o histórico não é alterado."
        )
    elif despesa["pago_cents"] > 0:
        erros.append(
            f"Esta despesa tem {despesa['pago_texto']} pagos. Uma despesa com pagamentos "
            "válidos não é anulada: anula primeiro cada pagamento, com motivo, e depois "
            "anula a despesa. Nenhuma anulação foi feita."
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    if not transacao_de_escrita("despesas"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    connection = get_db()
    try:
        cursor = connection.execute(
            """
            UPDATE expenses
            SET cancelled_at = ?, cancellation_reason = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (today_iso(), motivo, utc_now_iso(), expense_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash("Esta despesa já estava anulada. Nenhuma alteração foi feita.", "erro")
            return redirect(url_for("despesas.expense_detail", expense_id=expense_id))
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível anular a despesa "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=expense_id))

    flash(
        f"Despesa «{despesa['descricao']}» anulada. O registo conserva-se com a data e o "
        "motivo, e deixa de contar nas somas. Anular não apaga histórico.",
        "sucesso",
    )
    return redirect(url_for("despesas.expense_detail", expense_id=expense_id))


# ---------------------------------------------------------------------------
# Editar e anular um pagamento de despesa
# ---------------------------------------------------------------------------


def _pagamento_despesa_or_404(payment_id: int) -> dict:
    row = query_db(
        """
        SELECT p.*, e.descricao AS expense_descricao, e.amount_cents AS expense_amount_cents
        FROM expense_payments p
        JOIN expenses e ON e.id = p.expense_id
        WHERE p.id = ?
        """,
        (payment_id,),
        one=True,
    )
    if row is None:
        abort(404)
    return {
        "id": int(row["id"]),
        "expense_id": int(row["expense_id"]),
        "expense_descricao": row["expense_descricao"],
        "expense_amount_cents": int(row["expense_amount_cents"]),
        "amount_cents": int(row["amount_cents"]),
        "paid_on": row["paid_on"],
        "method": row["method"],
        "reference": row["reference"] or "",
        "anulado": row["cancelled_at"] is not None,
        "cancelled_at": format_date(row["cancelled_at"]) if row["cancelled_at"] else "",
        "cancellation_reason": row["cancellation_reason"] or "",
    }


@bp.route("/despesas/pagamentos/<int:payment_id>/editar", methods=["GET", "POST"])
def edit_expense_payment(payment_id: int):
    pagamento = _pagamento_despesa_or_404(payment_id)
    despesa = _despesa_or_404(pagamento["expense_id"])
    if pagamento["anulado"]:
        flash(
            "Este pagamento está anulado e não é editado: uma anulação não se repete. "
            "Se o dinheiro saiu mesmo, regista-o outra vez como movimento novo.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

    def pagina(valores: dict, efeitos: dict | None):
        return render_template(
            "expense_payment_edit.html",
            pagamento=pagamento,
            despesa=despesa,
            valores=valores,
            efeitos=efeitos,
            edit_token=_novo_token_de_formulario(),
        )

    if request.method == "GET":
        return pagina(
            {
                "valor": format_money_input(pagamento["amount_cents"]),
                "data": format_date(pagamento["paid_on"]),
                "meio": pagamento["method"],
                "referencia": pagamento["reference"],
                "motivo": "",
            },
            None,
        )

    if _recusar_se_desatualizado():
        return redirect(url_for("despesas.edit_expense_payment", payment_id=payment_id))

    erros: list[str] = []
    valor = parse_money(request.form.get("valor", ""))
    if valor is None or valor <= 0:
        erros.append("Valor pago: usa um valor positivo em euros, por exemplo 60,00.")
    elif valor > VALOR_MAXIMO_CENTIMOS:
        erros.append("Valor pago: acima de 100.000,00 EUR parece um erro de escrita.")

    data = parse_date(request.form.get("data", ""))
    if data is None:
        erros.append("Data do pagamento: usa DD/MM/YYYY.")
    elif data > today_iso():
        erros.append("Data do pagamento: não pode ser futura.")

    meio = request.form.get("meio", "").strip()
    if meio not in MEIOS:
        erros.append("Meio de pagamento: escolhe uma das opções.")

    referencia = request.form.get("referencia", "").strip()
    if len(referencia) > LIMITE_REFERENCIA:
        erros.append(f"Referência: no máximo {LIMITE_REFERENCIA} caracteres.")

    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Motivo da correção: escreve porque este pagamento está errado. O motivo fica "
            "guardado com os valores anteriores."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo da correção: no máximo {LIMITE_MOTIVO} caracteres.")

    token = request.form.get("edit_token", "").strip()
    if not token:
        erros.append(
            "Este formulário não tem a marca de envio: reabre a edição do pagamento e tenta "
            "de novo. Nenhuma alteração foi feita."
        )

    # O limite é o valor em falta, descontando este pagamento: a despesa não pode
    # ficar com pagamentos válidos acima do seu valor.
    outros_pagos = despesa["pago_cents"] - pagamento["amount_cents"]
    limite = despesa["amount_cents"] - outros_pagos
    if valor is not None and valor > limite:
        erros.append(
            f"Valor pago: a despesa é {despesa['amount_texto']} e os outros pagamentos já "
            f"cobrem {format_money(outros_pagos)}: este pagamento não pode passar de "
            f"{format_money(limite)}. Se a despesa estava errada, corrige primeiro a despesa."
        )

    previsualizar = request.form.get("previsualizar") == "on"
    confirmar = request.form.get("confirmar_correcao") == "on"
    if not previsualizar and not confirmar:
        erros.append("Pré-visualiza os efeitos da correção antes de a gravar. Nada foi alterado.")
    if confirmar and request.form.get("confirmar_efectos") != "on":
        erros.append(
            "Gravar a correção exige confirmar os efeitos mostrados na pré-visualização. "
            "Nada foi alterado."
        )

    efeitos = None
    if valor is not None:
        falta_antes = despesa["em_falta_cents"]
        falta_depois = despesa["amount_cents"] - (outros_pagos + valor)
        efeitos = {
            "valor_antes_texto": format_money(pagamento["amount_cents"]),
            "valor_depois_texto": format_money(valor),
            "pago_antes_texto": despesa["pago_texto"],
            "pago_depois_texto": format_money(outros_pagos + valor),
            "falta_antes_texto": despesa["em_falta_texto"],
            "falta_depois_texto": format_money(falta_depois),
            "liquidada_depois": falta_depois == 0,
            "mudou_valor": valor != pagamento["amount_cents"],
        }
        if not erros and efeitos["mudou_valor"] is False and data == pagamento["paid_on"] and meio == pagamento["method"] and referencia == pagamento["reference"]:
            erros.append("Não há nada para corrigir: os campos são os mesmos que estão guardados.")

    def reapresentar():
        return pagina(
            {
                "valor": request.form.get("valor", "").strip(),
                "data": request.form.get("data", "").strip(),
                "meio": request.form.get("meio", "").strip(),
                "referencia": request.form.get("referencia", "").strip(),
                "motivo": request.form.get("motivo", "").strip(),
            },
            efeitos,
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return reapresentar()

    if previsualizar:
        return reapresentar()

    if not transacao_de_escrita("despesas"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("despesas.edit_expense_payment", payment_id=payment_id))

    connection = get_db()
    try:
        repetida = query_db(
            "SELECT id FROM expense_payment_revisions WHERE edit_token = ?", (token,), one=True
        )
        if repetida is not None:
            connection.rollback()
            flash(
                "Esta correção já tinha sido aplicada: o mesmo formulário foi enviado duas "
                "vezes e o efeito não foi duplicado.",
                "erro",
            )
            return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO expense_payment_revisions
                (payment_id, changed_at, motivo, changed_by, edit_token,
                 amount_before, amount_after, paid_on_before, paid_on_after,
                 method_before, method_after, reference_before, reference_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                now,
                motivo,
                _nome_de_quem_regista(),
                token,
                pagamento["amount_cents"],
                valor,
                pagamento["paid_on"],
                data,
                pagamento["method"],
                meio,
                pagamento["reference"],
                referencia,
            ),
        )
        cursor = connection.execute(
            """
            UPDATE expense_payments
            SET amount_cents = ?, paid_on = ?, method = ?, reference = ?,
                edit_token = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (valor, data, meio, referencia, token, now, payment_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash("Este pagamento estava anulado e não foi corrigido. Nada foi gravado.", "erro")
            return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível corrigir o pagamento "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("despesas.edit_expense_payment", payment_id=payment_id))

    flash(
        f"Pagamento corrigido: {format_money(pagamento['amount_cents'])} passou a "
        f"{format_money(valor)}, com motivo registado. O valor em falta da despesa foi "
        "recalculado e o registo anterior ficou no histórico. O movimento conta uma única vez.",
        "sucesso",
    )
    return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))


@bp.route("/despesas/pagamentos/<int:payment_id>/anular", methods=["POST"])
def cancel_expense_payment(payment_id: int):
    pagamento = _pagamento_despesa_or_404(payment_id)
    despesa = _despesa_or_404(pagamento["expense_id"])

    if _recusar_se_desatualizado():
        return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

    erros: list[str] = []
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Anular um pagamento exige um motivo escrito, que fica guardado com o registo. "
            "Nenhuma anulação foi feita."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo: no máximo {LIMITE_MOTIVO} caracteres.")
    if request.form.get("confirmar_anular") != "on":
        erros.append("Anular exige confirmação explícita. Nenhuma anulação foi feita.")
    if pagamento["anulado"]:
        erros.append(
            "Este pagamento já foi anulado em "
            f"{pagamento['cancelled_at']} ({pagamento['cancellation_reason']}). "
            "Uma anulação não se repete, e o histórico não é alterado."
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

    if not transacao_de_escrita("despesas"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

    connection = get_db()
    try:
        cursor = connection.execute(
            """
            UPDATE expense_payments
            SET cancelled_at = ?, cancellation_reason = ?, updated_at = ?
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (today_iso(), motivo, utc_now_iso(), payment_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            flash("Este pagamento já estava anulado. Nenhuma alteração foi feita.", "erro")
            return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível anular o pagamento "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))

    estado = _despesa_estado(despesa["id"])
    flash(
        f"Pagamento de {format_money(pagamento['amount_cents'])} anulado. O registo conserva-se "
        f"com o motivo, e o valor em falta da despesa reabriu: falta {estado['em_falta_texto']}. "
        "Uma devolução bancária seria um movimento novo, não uma anulação.",
        "sucesso",
    )
    return redirect(url_for("despesas.expense_detail", expense_id=despesa["id"]))


@bp.route("/despesas/pagamentos/<int:payment_id>/historico")
def expense_payment_history(payment_id: int):
    pagamento = _pagamento_despesa_or_404(payment_id)
    return render_template(
        "expense_payment_history.html",
        pagamento=pagamento,
        correcoes=_correcoes_do_pagamento(payment_id),
    )


# ---------------------------------------------------------------------------
# Totais para o painel do administrador
# ---------------------------------------------------------------------------


def resumo_despesas() -> dict:
    """Números de despesa para o painel: registadas não são saídas; pagos são.

    O fundo de reserva é uma classificação de despesa: as despesas pagas com essa
    classificação já contam como saída na tesouraria, e o seu subtotal é mostrado
    à parte, sem ser somado outra vez à liquidez.
    """
    linha = query_db(
        """
        SELECT
            COALESCE(SUM(CASE WHEN cancelled_at IS NULL THEN amount_cents END), 0) AS registadas,
            COUNT(CASE WHEN cancelled_at IS NULL THEN 1 END) AS registadas_n
        FROM expenses
        """,
        one=True,
    )
    pagos = query_db(
        """
        SELECT COALESCE(SUM(p.amount_cents), 0) AS total, COUNT(*) AS n
        FROM expense_payments AS p
        JOIN expenses AS e ON e.id = p.expense_id
        WHERE p.cancelled_at IS NULL AND e.cancelled_at IS NULL
        """,
        one=True,
    )
    return {
        "despesas_registadas_texto": format_money(int(linha["registadas"])),
        "despesas_registadas_n": int(linha["registadas_n"]),
        "despesas_pagas_texto": format_money(int(pagos["total"])),
        "despesas_pagas_n": int(pagos["n"]),
        "despesas_em_falta_texto": format_money(int(linha["registadas"]) - int(pagos["total"])),
    }
