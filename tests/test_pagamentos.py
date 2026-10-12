"""Registo de pagamentos: campos, validação, imutabilidade e duplo envio."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import protecao_de, registar_pagamento, regras_e_mes, ultimo_pagamento


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Estes testes correm como se hoje fosse 01/02/2027: as datas não dependem do dia real."""


def test_registar_pagamento_guarda_data_fracao_valor_meio_e_referencia(app, client, db):
    resposta = registar_pagamento(
        client, db, "A", "43,00", data="15/01/2027", meio="transferencia",
        referencia="TRF-2027-014", nota="extrato bancário",
    )

    assert resposta.status_code == 302
    pagamentos = db.payments()
    assert len(pagamentos) == 1
    pagamento = pagamentos[0]
    assert pagamento["fraction_code"] == "A"
    assert pagamento["amount_cents"] == 4300
    assert pagamento["paid_on"] == "2027-01-15"
    assert pagamento["method"] == "transferencia"
    assert pagamento["reference"] == "TRF-2027-014"
    assert pagamento["source"] == "extrato bancário"
    assert pagamento["cancelled_at"] is None
    assert pagamento["created_at"]


def test_registar_pagamento_aceita_referencia_opcional_e_meios_validos(app, client, db):
    for meio in ("transferencia", "dinheiro", "cheque", "debito_direto", "outro"):
        registar_pagamento(client, db, "B", "40,00", meio=meio, referencia="")

    pagamentos = db.payments()
    assert len(pagamentos) == 5
    assert {p["method"] for p in pagamentos} == {
        "transferencia", "dinheiro", "cheque", "debito_direto", "outro",
    }
    assert all(p["reference"] == "" for p in pagamentos)


def test_pagamento_sem_confirmacao_nao_e_guardado(app, client, db):
    resposta = registar_pagamento(client, db, "A", "43,00", confirmar=False)

    assert resposta.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0


def test_pagamento_com_valor_nao_positivo_ou_invalido_e_recusado(app, client, db):
    for valor in ("0,00", "-43,00", "", "abc", "43,001"):
        registar_pagamento(client, db, "A", valor)

    assert db.payments() == []


def test_pagamento_acima_do_limite_de_verosimilhanca_e_recusado(app, client, db):
    resposta = registar_pagamento(client, db, "A", "10.000,01")

    assert resposta.status_code == 302
    assert db.payments() == []


def test_pagamento_com_data_futura_ou_invalida_e_recusado(app, client, db):
    for data in ("31/12/2099", "32/01/2027", "15/13/2027", "2027-13-01", ""):
        registar_pagamento(client, db, "A", "40,00", data=data)

    assert db.payments() == []


def test_pagamento_de_fracao_inexistente_e_recusado(app, client, db):
    protecao = protecao_de(client, "/payments/add")
    resposta = client.post(
        "/payments/add",
        data={**protecao, "confirmar_pagamento": "on", "fracao": "9999",
              "valor": "40,00", "data": "15/01/2027", "meio": "transferencia"},
    )

    assert resposta.status_code == 302
    assert db.payments() == []


def test_pagamento_com_meio_fora_das_opcoes_e_recusado(app, client, db):
    protecao = protecao_de(client, "/payments/add")
    resposta = client.post(
        "/payments/add",
        data={**protecao, "confirmar_pagamento": "on", "fracao": db.fraction("A")["id"],
              "valor": "40,00", "data": "15/01/2027", "meio": "bitcoin"},
    )

    assert resposta.status_code == 302
    assert db.payments() == []


def test_pagamento_com_referencia_demasiado_longa_e_recusado(app, client, db):
    resposta = registar_pagamento(client, db, "A", "40,00", referencia="x" * 121)

    assert resposta.status_code == 302
    assert db.payments() == []


def test_pagamento_sem_marca_de_formulario_e_recusado(app, client, db):
    protecao = protecao_de(client, "/payments/add")
    protecao.pop("form_token")
    resposta = client.post(
        "/payments/add",
        data={**protecao, "confirmar_pagamento": "on", "fracao": db.fraction("A")["id"],
              "valor": "40,00", "data": "15/01/2027", "meio": "transferencia"},
    )

    assert resposta.status_code == 302
    assert db.payments() == []


def test_duplo_envio_do_mesmo_formulario_regista_um_unico_pagamento(app, client, db):
    """Um duplo clique reenvia o mesmo formulário: a marca do formulário trava a cópia.

    O segundo pedido leva uma revisão atualizada, para que a recusa se deva à marca
    repetida e não apenas à concorrência otimista.
    """
    protecao = protecao_de(client, "/payments/add")
    form = {
        **protecao,
        "confirmar_pagamento": "on",
        "fracao": db.fraction("A")["id"],
        "valor": "43,00",
        "data": "15/01/2027",
        "meio": "transferencia",
        "referencia": "TRF-1",
    }

    primeira = client.post("/payments/add", data=form)
    assert primeira.status_code == 302

    # O segundo envio tem a revisão de agora: só a marca do formulário o distingue.
    form["revisao_pagamentos"] = protecao_de(client, "/payments/add")["revisao_pagamentos"]
    client.post("/payments/add", data=form)

    pagamentos = db.payments()
    assert len(pagamentos) == 1, "o duplo envio criou uma segunda receita"
    assert pagamentos[0]["reference"] == "TRF-1"


def test_pagamento_nao_e_alterado_nem_apagado_na_base(app, client, db):
    registar_pagamento(client, db, "A", "43,00")
    id_pagamento = ultimo_pagamento(db)["id"]

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE payments SET amount_cents = 1 WHERE id = ?", (id_pagamento,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payments SET paid_on = '2020-01-01' WHERE id = ?", (id_pagamento,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payments SET fraction_id = ? WHERE id = ?", (
            db.fraction("B")["id"], id_pagamento,
        ))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM payments WHERE id = ?", (id_pagamento,))

    assert db.payments()[0]["amount_cents"] == 4300


def test_pagamento_com_valor_nao_positivo_nao_entra_por_escrita_direta(app, db):
    id_a = db.fraction("A")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO payments (fraction_id, amount_cents, paid_on, method, created_at, updated_at)
            VALUES (?, 0, '2027-01-15', 'transferencia', '2027-01-15T00:00:00+00:00',
                    '2027-01-15T00:00:00+00:00')
            """,
            (id_a,),
        )
    assert db.payments() == []


def test_pagamento_anulado_exige_motivo_guardado(app, db):
    id_a = db.fraction("A")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO payments
                (fraction_id, amount_cents, paid_on, method, created_at, updated_at,
                 cancelled_at, cancellation_reason)
            VALUES (?, 4000, '2027-01-15', 'transferencia', '2027-01-15T00:00:00+00:00',
                    '2027-01-15T00:00:00+00:00', '2027-01-20', '')
            """,
            (id_a,),
        )
    assert db.payments() == []


def test_fracao_com_pagamento_nao_pode_ser_eliminada(app, client, db):
    """Um pagamento é movimento financeiro: a fração que o recebeu fica protegida."""
    registar_pagamento(client, db, "A", "40,00")
    id_a = db.fraction("A")["id"]

    with app.app_context():
        from app.db import fraction_references, has_financial_history

        assert "payments" in fraction_references(id_a)
        assert has_financial_history(id_a) is True


def test_texto_estranho_no_id_da_fracao_e_recusado_sem_erro_interno(app, client, db):
    """`²` passa em `str.isdigit()` mas `int()` recusa: tem de ser recusa, não 300."""
    protecao = protecao_de(client, "/payments/add")
    resposta = client.post(
        "/payments/add",
        data={**protecao, "confirmar_pagamento": "on", "fracao": "²",
              "valor": "40,00", "data": "15/01/2027", "meio": "transferencia"},
        follow_redirects=True,
    )

    assert resposta.status_code == 200
    assert "escolhe uma fração registada" in resposta.get_data(as_text=True)
    assert db.payments() == []


def test_pagamento_de_fracao_desativada_e_registado(app, client, db):
    """Dinheiro que entrou é um facto: desativar a fração não apaga o movimento."""
    id_h = db.fraction("H")["id"]
    client.post(
        f"/fractions/{id_h}/edit",
        data={**protecao_de(client, f"/fractions/{id_h}"), "code": "H", "active": ""},
    )

    resposta = registar_pagamento(client, db, "H", "40,00")
    assert resposta.status_code == 302
    assert db.payments()[0]["fraction_code"] == "H"


def test_pagamentos_nao_aparecem_sem_registo_e_as_cobrancas_continam_emitidas(app, client, db):
    regras_e_mes(client, db)

    assert db.payments() == []
    assert len(db.charges("2027-01")) == 16
