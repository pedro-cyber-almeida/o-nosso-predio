"""Portal do condómino (entrega 1): consulta das próprias frações.

- O condómino só vê as frações a que está associado; as outras devolvem 404,
  mesmo que alguém escreva o endereço à mão — a proteção é no servidor.
- Tudo aqui é leitura: cobranças, pagamentos, crédito, dívida e vencimentos.
  Registar, corrigir e anular pagamentos pertence à administração.
- Não há links para Gmail, credenciais, extratos ou outras frações.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template

from .auth import current_user, fracao_do_utilizador, fracoes_do_utilizador
from .db import query_db
from .payments import (
    ROTULOS_MEIO,
    _estado_das_cobrancas,
    _totais,
    _totais_texto,
)
from .units import format_date, format_money, format_permilagem

bp = Blueprint("portal", __name__)


def _utilizador_ou_404():
    utilizador = current_user()
    if utilizador is None:
        abort(404)
    return utilizador


@bp.route("/minhas-fracoes")
def minhas_fracoes():
    utilizador = _utilizador_ou_404()
    fracoes = fracoes_do_utilizador(int(utilizador["id"]))
    linhas = [
        {
            "id": fracao["id"],
            "code": fracao["code"],
            "totais": _totais_texto(_totais(int(fracao["id"]))),
        }
        for fracao in fracoes
    ]
    return render_template("portal_fracoes.html", fracoes=linhas)


@bp.route("/minhas-fracoes/<int:fraction_id>")
def minha_fracao(fraction_id: int):
    utilizador = _utilizador_ou_404()
    if not fracao_do_utilizador(int(utilizador["id"]), fraction_id):
        # Não é "sem permissão" nem "existe": a existência de outras frações
        # também não é revelada.
        abort(404)

    fracao = query_db("SELECT * FROM fractions WHERE id = ?", (fraction_id,), one=True)
    responsaveis = query_db(
        """
        SELECT name, role, valid_from, valid_to FROM owners
        WHERE fraction_id = ? AND valid_to IS NULL
        ORDER BY id
        """,
        (fraction_id,),
    )

    cobrancas = _estado_das_cobrancas(fraction_id)

    pagamentos = []
    afetado_por_pagamento = {
        int(linha["payment_id"]): int(linha["total"])
        for linha in query_db(
            """
            SELECT a.payment_id, SUM(a.amount_cents) AS total
            FROM payment_allocations AS a
            JOIN payments AS p ON p.id = a.payment_id
            WHERE p.fraction_id = ? AND p.cancelled_at IS NULL
            GROUP BY a.payment_id
            """,
            (fraction_id,),
        )
    }
    for linha in query_db(
        "SELECT * FROM payments WHERE fraction_id = ? ORDER BY paid_on DESC, id DESC",
        (fraction_id,),
    ):
        valor = int(linha["amount_cents"])
        afetado = afetado_por_pagamento.get(int(linha["id"]), 0)
        pagamentos.append(
            {
                "id": linha["id"],
                "valor_texto": format_money(valor),
                "data_texto": format_date(linha["paid_on"]),
                "meio_texto": ROTULOS_MEIO[linha["method"]],
                "referencia": linha["reference"] or "",
                "afetado_texto": format_money(afetado),
                "crediticio_texto": format_money(valor - afetado),
                "anulado": linha["cancelled_at"] is not None,
                "motivo_anulacao": linha["cancellation_reason"] or "",
            }
        )

    return render_template(
        "portal_fracao.html",
        fracao={
            "id": fracao["id"],
            "code": fracao["code"],
            "permilagem_texto": format_permilagem(int(fracao["permilagem"])),
            "active": bool(fracao["active"]),
        },
        responsaveis=responsaveis,
        totais=_totais_texto(_totais(fraction_id)),
        cobrancas=cobrancas,
        pagamentos=pagamentos,
    )
