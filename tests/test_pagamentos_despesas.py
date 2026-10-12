"""Entrega 3: pagamentos de despesas — parciais, em falta, correções e anulações."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    anular_pagamento_de_despesa,
    categoria_de_trabalho,
    editar_pagamento_de_despesa,
    pagar_despesa,
    protecao_de,
    protecao_de_edicao_em,
    registar_despesa,
    ultima_despesa,
    ultimo_pagamento_de_despesa,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Os pagamentos correm como se hoje fosse 01/02/2027."""


def _em_falta(db, expense_id: int) -> int:
    return int(
        db.scalar(
            """
            SELECT e.amount_cents - COALESCE((
                SELECT SUM(p.amount_cents) FROM expense_payments p
                WHERE p.expense_id = e.id AND p.cancelled_at IS NULL
            ), 0)
            FROM expenses e WHERE e.id = ?
            """,
            (expense_id,),
        )
    )


# ------------------------------------------------------- pagamentos parciais ----


def test_pagamento_parcial_deixa_valor_em_falta_calculado(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00", data="20/01/2027")

    assert _em_falta(db, despesa["id"]) == 7500
    pagamento = ultimo_pagamento_de_despesa(db)
    assert pagamento["amount_cents"] == 4500
    assert pagamento["paid_on"] == "2027-01-20"
    assert pagamento["method"] == "transferencia"


def test_pagamentos_sucessivos_ate_ficar_paga(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagar_despesa(client, db, despesa["id"], "75,00")

    assert _em_falta(db, despesa["id"]) == 0
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 2


def test_pagamento_acima_do_em_falta_e_recusado(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")

    pagar_despesa(client, db, despesa["id"], "80,00")  # faltam só 75,00
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 1
    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "não pode exceder o valor em falta" in html


def test_pagamento_sem_confirmacao_nao_e_guardado(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00", confirmar=False)
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 0


def test_pagamento_com_data_futura_e_recusado(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00", data="15/02/2027")
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 0


def test_duplo_envio_do_mesmo_pagamento_nao_cria_dois(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    protecao = protecao_de(client, f"/despesas/{despesa['id']}")
    pagar_despesa(client, db, despesa["id"], "45,00", protecao=protecao)
    pagar_despesa(client, db, despesa["id"], "45,00", protecao=protecao)
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 1
    assert _em_falta(db, despesa["id"]) == 7500


def test_pagamento_de_despesa_nao_pode_ser_apagado_da_base(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM expense_payments WHERE id = ?", (pagamento["id"],))


def test_despesa_anulada_nao_recebe_pagamento(app, client, db):
    from conftest import anular_despesa

    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    anular_despesa(client, despesa["id"], motivo="Engano")

    pagar_despesa(
        client, db, despesa["id"], "45,00",
        protecao=protecao_de(client, "/despesas/categorias"),
    )
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 0


# ---------------------------------------------------------- concorrência ----


def test_pagamento_com_estado_desatualizado_nao_passa_por_cima_do_outro(app, client, db):
    """Duas janelas abertas sobre a mesma despesa: a segunda não pode pagar a mais."""
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)

    # Página aberta antes do primeiro pagamento: revisão e em falta antigos.
    protecao_velha = protecao_de(client, f"/despesas/{despesa['id']}")
    pagar_despesa(client, db, despesa["id"], "70,00")

    # A segunda janela tenta pagar 70,00 com a página velha: faltam só 50,00.
    pagar_despesa(client, db, despesa["id"], "70,00", protecao=protecao_velha)
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 1
    assert _em_falta(db, despesa["id"]) == 5000


# ------------------------------------------------------- correção de pagamento ----


def test_editar_pagamento_com_motivo_guarda_revisao_e_recalcula_o_em_falta(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)

    editar_pagamento_de_despesa(
        client, db, pagamento["id"], valor="60,00", motivo="O comprovativo era 60,00"
    )
    assert _em_falta(db, despesa["id"]) == 6000
    revisoes = db.rows(
        "SELECT * FROM expense_payment_revisions WHERE payment_id = ?", (pagamento["id"],)
    )
    assert len(revisoes) == 1
    assert revisoes[0]["amount_before"] == 4500
    assert revisoes[0]["amount_after"] == 6000
    assert revisoes[0]["motivo"] == "O comprovativo era 60,00"

    # O pagamento continua a ser uma linha só: corrigir não duplica a saída.
    assert db.scalar("SELECT COUNT(*) FROM expense_payments") == 1


def test_edicao_de_pagamento_previsualiza_sem_gravar(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)

    editar_pagamento_de_despesa(
        client, db, pagamento["id"], valor="90,00", previsualizar=True, confirmar=False
    )
    assert _em_falta(db, despesa["id"]) == 7500


def test_edicao_de_pagamento_nao_pode_ultrapassar_o_limite_da_despesa(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    outro = pagar_despesa(client, db, despesa["id"], "40,00")
    pagamento = ultimo_pagamento_de_despesa(db)

    # Os outros pagamentos já cobrem 45,00: este não pode passar de 75,00.
    editar_pagamento_de_despesa(client, db, pagamento["id"], valor="90,00")
    assert _em_falta(db, despesa["id"]) == 3500, "a correção recusada não alterou nada"
    html = client.get(f"/despesas/pagamentos/{pagamento['id']}/editar").get_data(as_text=True)
    assert "35,00 EUR" in html


def test_reenvio_do_formulario_de_correcao_nao_duplica_o_efeito(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)

    protecao = protecao_de_edicao_em(client, f"/despesas/pagamentos/{pagamento['id']}/editar")
    editar_pagamento_de_despesa(client, db, pagamento["id"], valor="60,00", protecao=protecao)
    editar_pagamento_de_despesa(client, db, pagamento["id"], valor="60,00", protecao=protecao)
    assert db.scalar("SELECT COUNT(*) FROM expense_payment_revisions") == 1
    assert _em_falta(db, despesa["id"]) == 6000


# ------------------------------------------------------------- anulação ----


def test_anular_pagamento_reabre_o_em_falta_e_nao_e_devolucao_bancaria(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)

    anular_pagamento_de_despesa(client, pagamento["id"], motivo="O pagamento nunca saiu")
    assert _em_falta(db, despesa["id"]) == 12000

    guardado = next(p for p in db.expense_payments() if p["id"] == pagamento["id"])
    assert guardado["cancelled_at"] == "2027-02-01"
    assert guardado["cancellation_reason"] == "O pagamento nunca saiu"

    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "O pagamento nunca saiu" in html
    assert "devolução bancária seria um movimento novo" in html


def test_pagamento_anulado_nao_pode_ser_editado_nem_anulado_de_novo(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)
    anular_pagamento_de_despesa(client, pagamento["id"], motivo="Engano")

    resposta = editar_pagamento_de_despesa(
        client, db, pagamento["id"], valor="70,00",
        protecao=protecao_de(client, "/despesas/categorias"),
    )
    assert resposta.status_code == 302  # a edição de uma anulação é recusada
    assert db.scalar("SELECT amount_cents FROM expense_payments WHERE id = ?", (pagamento["id"],)) == 4500

    anular_pagamento_de_despesa(
        client, pagamento["id"], motivo="Outro motivo",
        protecao=protecao_de(client, "/despesas/categorias"),
    )
    assert db.scalar(
        "SELECT cancellation_reason FROM expense_payments WHERE id = ?", (pagamento["id"],)
    ) == "Engano"


def test_pagamentos_anulados_deixam_de_contar_mas_continuem_visiveis(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)
    anular_pagamento_de_despesa(client, pagamento["id"], motivo="Engano")

    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "45,00 EUR" in html  # o registo anulado mostra-se, não se esconde
    assert "120,00 EUR" in html  # o em falta voltou ao valor inteiro


def test_historico_de_um_pagamento_mostra_as_correcoes(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")
    pagamento = ultimo_pagamento_de_despesa(db)
    editar_pagamento_de_despesa(client, db, pagamento["id"], valor="60,00", motivo="Comprovativo")

    html = client.get(f"/despesas/pagamentos/{pagamento['id']}/historico").get_data(as_text=True)
    assert "45,00 EUR" in html and "60,00 EUR" in html
    assert "Comprovativo" in html
