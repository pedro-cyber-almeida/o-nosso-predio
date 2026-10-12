"""Afetações: distribuir um pagamento por cobranças, parciais, limites e transação."""

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


def test_afetacao_total_liquida_a_cobranca_e_nao_deixa_credito(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    assert resposta.status_code == 302
    assert liquidado_da_cobranca(db, id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")) == 4000
    assert liquidado_da_cobranca(db, id_da_cobranca(db, "A", "2027-01", "reforco_fundo")) == 300
    assert db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    ) == 4300


def test_um_pagamento_liquida_cobrancas_de_varios_meses(app, client, db):
    regras_e_mes(client, db)
    from conftest import gerar_mes

    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "B", "100,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "B", "2027-01", "quota_ordinaria"): "45,00",
            id_da_cobranca(db, "B", "2027-02", "quota_ordinaria"): "45,00",
        },
    )

    assert resposta.status_code == 302
    assert len(db.allocations()) == 2
    for period in ("2027-01", "2027-02"):
        assert liquidado_da_cobranca(db, id_da_cobranca(db, "B", period, "quota_ordinaria")) == 4500


def test_varios_pagamentos_parciais_liquidam_a_mesma_cobranca(app, client, db):
    regras_e_mes(client, db)
    charge_id = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "20,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {charge_id: "20,00"})

    registar_pagamento(client, db, "A", "20,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {charge_id: "20,00"})

    assert liquidado_da_cobranca(db, charge_id) == 4000
    assert len(db.allocations()) == 2


def test_afetacao_que_excede_o_pagamento_e_recusada_sem_gravar_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "60,00"},
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacao_que_excede_o_que_falta_pagar_e_recusada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "60,00"},
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacao_negativa_e_recusada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "-10,00"},
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacao_a_cobranca_de_outra_fracao_e_recusada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)
    charge_de_b = id_da_cobranca(db, "B", "2027-01", "quota_ordinaria")

    resposta = afetar_pagamento(client, pagamento["id"], {charge_de_b: "40,00"})

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacao_de_pagamento_a_outra_fracao_e_travada_na_base(app, db):
    """Mesmo escrita à mão, a base não permite liquidar a cobrança de outra fração."""
    id_a = db.fraction("A")["id"]
    id_b = db.fraction("B")["id"]
    charge_de_b = id_da_cobranca_na_base(db, "B", "2027-01")

    db.execute(
        """
        INSERT INTO payments (fraction_id, amount_cents, paid_on, method, created_at, updated_at)
        VALUES (?, 4000, '2027-01-15', 'transferencia', '2027-01-15T00:00:00+00:00',
                '2027-01-15T00:00:00+00:00')
        """,
        (id_a,),
    )
    payment_id = db.payments()[0]["id"]

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 4000, '2027-01-15T00:00:00+00:00')",
            (payment_id, charge_de_b),
        )
    assert db.allocations() == []
    assert id_b != id_a


def id_da_cobranca_na_base(db, code: str, period: str) -> int:
    """Cobrança criada diretamente na base, para provar os travões do esquema."""
    id_fracao = db.fraction(code)["id"]
    db.execute(
        """
        INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, created_at, updated_at)
        VALUES (?, 'quota_ordinaria', 4000, '2027-01-01', '2026-10-01T00:00:00+00:00',
                '2026-10-01T00:00:00+00:00')
        """,
        (id_fracao,),
    )
    rule_id = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
    db.execute(
        """
        INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at)
        VALUES (?, ?, 'quota_ordinaria', ?, 4000, '2027-01-01T00:00:00+00:00')
        """,
        (id_fracao, rule_id, period),
    )
    return int(db.scalar("SELECT id FROM charges ORDER BY id DESC LIMIT 1"))


def test_distribuicao_sem_confirmacao_nao_e_guardada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
        confirmar=False,
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_distribuicao_com_todos_os_campos_vazios_nao_grava_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "0,00"}
    )

    assert resposta.status_code == 302
    assert db.allocations() == []


def test_afetacoes_mudam_mas_nunca_quebram_as_regras_financeiras(app, client, db):
    """Redistribuir passou a ser possível; os travões financeiros continuam todos.

    A imutabilidade absoluta das afetações deu lugar à correção justificada do
    pagamento (fase 3.5). O que a base continua a não deixar fazer é qualquer escrita
    que deixe o dinheiro a não bater: afetação acima do pagamento, acima do que a
    cobrança deve, mudada de cobrança, ou de um pagamento anulado.
    """
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    charge_id = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {charge_id: "40,00"})
    afetao = db.allocations()[0]

    # Acima do pagamento que a originou.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payment_allocations SET amount_cents = 4300 WHERE id = ?", (afetao["id"],))
    # Acima do que a cobrança deve.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payment_allocations SET amount_cents = 4800 WHERE id = ?", (afetao["id"],))
    # Mudar de cobrança ou de pagamento: retirar e acrescentar outra, não mover esta.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payment_allocations SET charge_id = 999 WHERE id = ?", (afetao["id"],))

    assert db.allocations()[0]["amount_cents"] == 4000
    assert liquidado_da_cobranca(db, charge_id) == 4000


def test_afetacao_de_pagamento_anulado_nao_pode_ser_alterada_na_base(app, client, db):
    """Um pagamento anulado deixou de liquidar: as afetações dele também congelam."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"}
    )
    anular_pagamento(client, pagamento["id"], motivo="Movimento que não existia")
    afetao = db.allocations()[0]

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payment_allocations SET amount_cents = 1 WHERE id = ?", (afetao["id"],))

    assert db.allocations()[0]["amount_cents"] == 4000


def test_um_pagamento_nao_afeta_duas_vezes_a_mesma_cobranca(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)
    charge_id = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    afetar_pagamento(client, pagamento["id"], {charge_id: "20,00"})

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 2700, '2027-01-16T00:00:00+00:00')",
            (pagamento["id"], charge_id),
        )
    assert liquidado_da_cobranca(db, charge_id) == 2000


def test_afetacao_que_soma_mais_do_que_o_pagamento_e_travada_na_base(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 5300, '2027-01-15T00:00:00+00:00')",
            (pagamento["id"], id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")),
        )
    assert db.allocations() == []


def test_afetacao_que_soma_mais_do_que_a_cobranca_deve_e_travada_na_base(app, client, db):
    regras_e_mes(client, db)
    charge_id = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "30,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {charge_id: "30,00"})

    registar_pagamento(client, db, "A", "30,00")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 3000, '2027-01-16T00:00:00+00:00')",
            (ultimo_pagamento(db)["id"], charge_id),
        )
    assert liquidado_da_cobranca(db, charge_id) == 3000


def test_distribuicao_invalida_nao_deixa_metade_das_afetoes_gravada(app, client, db, db_path):
    """Duas afetações na mesma transação: se uma falhar, nenhuma fica gravada."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 4000, '2027-01-15T00:00:00+00:00')",
            (pagamento["id"], quota),
        )
        # 4000 + 1000 excede o pagamento de 4300: o travão aborta a transação.
        connection.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 1000, '2027-01-15T00:00:00+00:00')",
            (pagamento["id"], reforco),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
    finally:
        connection.close()

    assert db.allocations() == []
    assert liquidado_da_cobranca(db, quota) == 0


def test_dois_campos_para_a_mesma_cobranca_no_mesmo_envio_sao_recusados(app, client, db):
    """`afecao_5` e `afecao_05` são a mesma cobrança: a validação recusa, não a base."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    resposta = client.post(
        f"/payments/{pagamento['id']}/allocate",
        data={
            **protecao_de(client, f"/payments/{pagamento['id']}"),
            f"afecao_{quota}": "30,00",
            f"afecao_0{quota}": "17,00",
            "confirmar_afetacao": "on",
        },
        follow_redirects=True,
    )

    corpo = resposta.get_data(as_text=True)
    assert "aparece mais do que uma vez" in corpo
    assert db.allocations() == []
    assert liquidado_da_cobranca(db, quota) == 0


def test_campos_de_afetacao_com_texto_estranho_nao_provocam_erro_interno(app, client, db):
    """`afecao_²` não é uma cobrança: é ignorado e a recusa chega com mensagem."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)

    resposta = client.post(
        f"/payments/{pagamento['id']}/allocate",
        data={
            **protecao_de(client, f"/payments/{pagamento['id']}"),
            "afecao_²": "40,00",
            "confirmar_afetacao": "on",
        },
        follow_redirects=True,
    )

    assert resposta.status_code == 200
    assert "pelo menos um valor" in resposta.get_data(as_text=True)
    assert db.allocations() == []


def test_afetacoes_separam_quota_ordinaria_e_reforco(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    afetar_pagamento(
        client,
        ultimo_pagamento(db)["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    rubricas = {a["rubric"]: a["amount_cents"] for a in db.allocations()}
    assert rubricas == {"quota_ordinaria": 4000, "reforco_fundo": 300}

    resposta = client.get("/payments")
    html = resposta.get_data(as_text=True)
    # Na lista, o que foi afetado ao fundo aparece separado do resto do recebido.
    assert "3,00 EUR" in html

    detalhe = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "40,00 EUR" in detalhe
    assert "3,00 EUR" in detalhe
