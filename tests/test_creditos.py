"""Crédito de uma fração: dinheiro recebido e não afetado, afetação posterior e concorrência."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    ADMIN_TESTE,
    afetar_pagamento,
    entrar,
    id_da_cobranca,
    liquidado_da_cobranca,
    protecao_de,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Estes testes correm como se hoje fosse 01/02/2027."""


def _credito_da_fracao(db, code: str) -> int:
    return int(
        db.scalar(
            """
            SELECT COALESCE((SELECT SUM(amount_cents) FROM payments
                             WHERE fraction_id = ? AND cancelled_at IS NULL), 0)
                 - COALESCE((SELECT SUM(a.amount_cents) FROM payment_allocations a
                             JOIN payments p ON p.id = a.payment_id
                             WHERE p.fraction_id = ? AND p.cancelled_at IS NULL), 0)
            """,
            (db.fraction(code)["id"], db.fraction(code)["id"]),
        )
    )


def test_pagamento_nao_afetado_fica_como_credito_da_fracao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    assert _credito_da_fracao(db, "A") == 4300
    assert db.allocations() == []


def test_credito_nao_liquida_automaticamente_uma_cobranca(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    charge_id = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    assert liquidado_da_cobranca(db, charge_id) == 0

    html = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "40,00 EUR" in html  # a dívida continua inteira
    assert "43,00 EUR" in html  # e o crédito está registado


def test_afectar_credito_mais_tarde_nao_cria_uma_segunda_receita(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    antes = db.payments()
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    depois = db.payments()
    assert len(depois) == len(antes) == 1, "afetar um crédito criou outro pagamento"
    assert depois[0]["amount_cents"] == antes[0]["amount_cents"] == 4300
    assert _credito_da_fracao(db, "A") == 0

    recebido_global = db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NULL"
    )
    assert recebido_global == 4300, "o mesmo dinheiro passou a contar duas vezes"


def test_credito_parc_de_continua_disponivel_depois_de_afetar_uma_parte(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)

    afetar_pagamento(
        client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"}
    )
    assert _credito_da_fracao(db, "A") == 6000

    afetar_pagamento(
        client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00"}
    )
    assert _credito_da_fracao(db, "A") == 5700
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1


def test_duas_janelas_abertas_nao_gastam_o_mesmo_credito(app, client, db):
    """A segunda submissão usa uma revisão envelhecida e é recusada sem gravar nada."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    # Janela A abre o formulário e não o envia ainda.
    protecao_a = protecao_de(client, f"/payments/{pagamento['id']}")

    # Janela B distribui o pagamento todo.
    outra = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    registar_pagamento(outra, db, "A", "1,00")  # garante sessão própria da janela B
    afetar_pagamento(
        outra,
        pagamento["id"],
        {quota: "40,00", reforco: "3,00"},
        protecao=protecao_de(outra, f"/payments/{pagamento['id']}"),
    )

    # Janela A envia agora a sua distribuição, com a revisão que tinha.
    resposta = afetar_pagamento(client, pagamento["id"], {quota: "40,00"}, protecao=protecao_a)

    assert resposta.status_code == 302
    total_afetado = db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    )
    assert total_afetado == 4300, "o mesmo crédito foi gasto duas vezes"
    assert liquidado_da_cobranca(db, quota) == 4000


def test_gastar_o_mesmo_credito_duas_vezes_e_travado_na_base(app, client, db):
    """A soma das afetações de um pagamento não pode exceder o dinheiro que entrou."""
    regras_e_mes(client, db)
    from conftest import gerar_mes

    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    db.execute(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
        "VALUES (?, ?, 4000, '2027-01-15T00:00:00+00:00')",
        (pagamento["id"], id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")),
    )
    db.execute(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
        "VALUES (?, ?, 300, '2027-01-15T00:00:00+00:00')",
        (pagamento["id"], id_da_cobranca(db, "A", "2027-01", "reforco_fundo")),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 100, '2027-01-15T00:00:01+00:00')",
            (pagamento["id"], id_da_cobranca(db, "A", "2027-02", "reforco_fundo")),
        )
    assert db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    ) == 4300


def test_credito_por_afetar_nao_e_contado_como_receita_do_fundo(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    html = client.get("/payments").get_data(as_text=True)
    assert "43,00 EUR" in html  # crédito da fração
    assert "0,00 EUR" in html  # nada foi ainda afetado ao fundo

    fundo = db.scalar(
        """
        SELECT COALESCE(SUM(a.amount_cents), 0)
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        JOIN charges AS c ON c.id = a.charge_id
        WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo'
        """
    )
    assert fundo == 0


def test_credito_aparece_na_consulta_de_pagamentos(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    afetar_pagamento(
        client,
        ultimo_pagamento(db)["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
    )

    html = client.get("/payments").get_data(as_text=True)
    assert "Crédito por afetar" in html
    assert "3,00 EUR" in html
