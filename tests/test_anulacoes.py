"""Anulações: motivo obrigatório, reversão dos efeitos, histórico conservado, sem repetição."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    afetar_pagamento,
    anular_pagamento,
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


def test_anular_sem_motivo_nao_altera_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = anular_pagamento(client, pagamento["id"], motivo="")

    assert resposta.status_code == 302
    guardado = db.payments()[0]
    assert guardado["cancelled_at"] is None
    assert guardado["cancellation_reason"] is None


def test_anular_sem_confirmacao_explcita_nao_altera_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = anular_pagamento(client, pagamento["id"], confirmar=False)

    assert resposta.status_code == 302
    assert db.payments()[0]["cancelled_at"] is None


def test_anular_reverte_as_afetoes_e_reabre_as_cobrancas(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    afetar_pagamento(client, pagamento["id"], {quota: "40,00", reforco: "3,00"})
    assert liquidado_da_cobranca(db, quota) == 4000

    resposta = anular_pagamento(client, pagamento["id"], motivo="Valor registado a mais")

    assert resposta.status_code == 302
    assert liquidado_da_cobranca(db, quota) == 0, "a cobrança não voltou a estar em dívida"
    assert liquidado_da_cobranca(db, reforco) == 0

    recebido = db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NULL"
    )
    assert recebido == 0, "um pagamento anulado continuou a contar como receita"


def test_anular_nao_deixa_credito_remanescente(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"})

    anular_pagamento(client, pagamento["id"], motivo="Movimento que não existia")

    html = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "anulado" in html
    # O crédito de 53,00 EUR desapareceu: o dinheiro saiu do registo.
    assert "53,00 EUR" not in html
    # A dívida da fração voltou toda: 40,00 + 3,00.
    assert "43,00 EUR" in html


def test_anulacao_conserva_o_registo_original_com_data_e_motivo(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00", data="15/01/2027", referencia="TRF-1")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
    )
    afetoes_antes = db.allocations()

    anular_pagamento(client, pagamento["id"], motivo="O condómino pagou duas vezes")

    guardado = db.payments()[0]
    assert guardado["amount_cents"] == 4300
    assert guardado["paid_on"] == "2027-01-15"
    assert guardado["reference"] == "TRF-1"
    assert guardado["created_at"] == pagamento["created_at"]
    assert guardado["cancelled_at"] == "2027-02-01"
    assert guardado["cancellation_reason"] == "O condómino pagou duas vezes"

    # As afetações conservam-se, exatamente como estavam.
    assert db.allocations() == afetoes_antes

    html = client.get("/payments").get_data(as_text=True)
    assert "O condómino pagou duas vezes" in html


def test_anular_nao_apaga_nem_reduz_o_numero_de_registos(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
    )

    anular_pagamento(client, pagamento["id"])

    assert db.scalar("SELECT COUNT(*) FROM payments") == 1
    assert db.scalar("SELECT COUNT(*) FROM payment_allocations") == 1
    assert db.scalar("SELECT COUNT(*) FROM charges") == 16


def test_dupla_anulacao_e_recusada_e_o_historico_nao_e_alterado(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    primeira = anular_pagamento(client, pagamento["id"], motivo="Registado por engano")
    guardado = db.payments()[0]

    # A página do pagamento anulado já não oferece formulário: a segunda tentativa
    # usa a proteção de outra página, e mesmo assim a anulação é recusada.
    segunda = anular_pagamento(
        client,
        pagamento["id"],
        motivo="Outro motivo",
        protecao=protecao_de(client, "/payments/add"),
    )

    assert primeira.status_code == 302
    assert segunda.status_code == 302
    depois = db.payments()[0]
    assert depois["cancellation_reason"] == "Registado por engano", "a anulação foi sobrescrita"
    assert depois["cancelled_at"] == guardado["cancelled_at"]


def test_pagamento_anulado_nao_e_alterado_por_escrita_direta(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    anular_pagamento(client, pagamento["id"], motivo="Registado por engano")

    for sql, params in (
        ("UPDATE payments SET amount_cents = 1 WHERE id = ?", (pagamento["id"],)),
        ("UPDATE payments SET cancelled_at = '2020-01-01' WHERE id = ?", (pagamento["id"],)),
        (
            "UPDATE payments SET cancellation_reason = 'outro' WHERE id = ?",
            (pagamento["id"],),
        ),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, params)

    assert db.payments()[0]["cancellation_reason"] == "Registado por engano"


def test_pagamento_anulado_nao_volta_a_liquidar_cobrancas(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    anular_pagamento(client, pagamento["id"], motivo="Registado por engano")

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
        protecao=protecao_de(client, "/payments/add"),
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacao_de_pagamento_anulado_e_travada_na_base(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    anular_pagamento(client, pagamento["id"], motivo="Registado por engano")

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 4000, '2027-01-16T00:00:00+00:00')",
            (pagamento["id"], id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")),
        )
    assert db.allocations() == []


def test_anular_um_pagamento_parcial_devolve_so_a_parte_que_liquidou(app, client, db):
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "20,00")
    primeiro = ultimo_pagamento(db)
    afetar_pagamento(client, primeiro["id"], {quota: "20,00"})

    registar_pagamento(client, db, "A", "20,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {quota: "20,00"})
    assert liquidado_da_cobranca(db, quota) == 4000

    anular_pagamento(client, primeiro["id"], motivo="Os 20 EUR eram de outra fração")

    assert liquidado_da_cobranca(db, quota) == 2000


def test_anular_nao_altera_as_cobrancas_emitidas(app, client, db):
    regras_e_mes(client, db)
    antes = db.charges("2027-01")

    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
    )
    anular_pagamento(client, pagamento["id"], motivo="Engano")

    assert db.charges("2027-01") == antes, "a anulação alterou cobranças emitidas"


def test_pagamento_anulado_mostra_as_afetoes_como_sem_efeito_na_lista(app, client, db):
    """As afetações de um pagamento anulado conservam-se, mas não liquidam nada."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    anular_pagamento(client, pagamento["id"], motivo="Engano ao registar")

    html = client.get("/payments").get_data(as_text=True)
    assert "sem efeito" in html
    assert len(db.allocations()) == 2


def test_historico_da_fracao_marca_afetoes_anuladas_como_sem_efeito(app, client, db):
    """O histórico mostra as afetações conservadas, mas diz que já não liquidam nada."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    anular_pagamento(client, pagamento["id"], motivo="Engano ao registar")

    html = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "sem efeito: o pagamento está anulado" in html
    # A dívida da fração voltou toda: 40,00 + 3,00.
    assert "43,00 EUR" in html


def test_anulacao_exige_motivo_curto_e_fica_visivel_na_consulta(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    longa = anular_pagamento(client, pagamento["id"], motivo="x" * 201)
    assert longa.status_code == 302
    assert db.payments()[0]["cancelled_at"] is None

    curta = anular_pagamento(client, pagamento["id"], motivo="Duplicado")
    assert curta.status_code == 302

    detalhe = client.get(f"/payments/{pagamento['id']}").get_data(as_text=True)
    assert "anulado" in detalhe
    assert "Duplicado" in detalhe
