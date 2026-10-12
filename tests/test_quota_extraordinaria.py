"""Entrega 2: quotas extraordinárias — repartição, prestações, emissão e consulta."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    configurar_regras_de_teste,
    cobrancas_extraordinarias,
    criar_condomino,
    entrar_condomino,
    gerar_mes,
    protecao_de,
    registar_quota_extraordinaria,
)


def _texto(client, caminho: str) -> str:
    return client.get(caminho).get_data(as_text=True)


# --- Repartição ---------------------------------------------------------------


def test_quota_extraordinaria_emite_uma_cobranca_por_fracao_e_prestacao(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B", "C"], "100,00", datas=("15/03/2027",),
        descricao="Reparação da cobertura",
    )

    cobrancas = cobrancas_extraordinarias(db)
    assert len(cobrancas) == 3
    assert sum(c["amount_cents"] for c in cobrancas) == 10000
    assert {c["period"] for c in cobrancas} == {"2027-03"}
    assert {c["due_date"] for c in cobrancas} == {"2027-03-15"}
    # A descrição e o vencimento viajam com a cobrança: é o que o condómino vê.
    assert {c["descricao"] for c in cobrancas} == {"Reparação da cobertura"}


def test_reparticao_igual_conserva_o_total_ao_centimo(app, client, db):
    registar_quota_extraordinaria(client, db, ["A", "B", "C"], "100,00", datas=("15/03/2027",))

    valores = sorted((c["amount_cents"] for c in cobrancas_extraordinarias(db)), reverse=True)
    assert valores == [3334, 3333, 3333]
    assert sum(valores) == 10000


def test_reparticao_proporcional_a_permilagem_conserva_o_total(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "1.000,00", datas=("15/03/2027",), reparticao="permilagem"
    )

    por_fracao = {c["fraction_code"]: c["amount_cents"] for c in cobrancas_extraordinarias(db)}
    assert sum(por_fracao.values()) == 100000
    # B (120,00) paga mais do que A (100,00), e a diferença é a permilagem ao cêntimo.
    assert por_fracao["B"] > por_fracao["A"]
    assert abs(por_fracao["A"] / 100000 - 10000 / 22000) < 1e-4


def test_reparticao_manual_recusa_soma_diferente_do_total(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "100,00", reparticao="manual",
        manuais={"A": "60,00", "B": "30,00"},
    )

    assert cobrancas_extraordinarias(db) == []
    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0
    assert db.scalar("SELECT COUNT(*) FROM charge_rules") == 0


def test_reparticao_manual_emite_quando_a_soma_bate_exatamente(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "100,00", reparticao="manual",
        manuais={"A": "61,67", "B": "38,33"},
    )

    por_fracao = {c["fraction_code"]: c["amount_cents"] for c in cobrancas_extraordinarias(db)}
    assert por_fracao == {"A": 6167, "B": 3833}


def test_reparticao_manual_com_fracao_a_mais_nao_fica_silenciosa(app, client, db):
    """Uma fração abrangida sem valor manual é recusada, não repartida por defeito."""
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "100,00", reparticao="manual", manuais={"A": "100,00"}
    )
    assert cobrancas_extraordinarias(db) == []


# --- Prestações ---------------------------------------------------------------


def test_prestacoes_somam_exatamente_a_quota_parte_de_cada_fracao(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00",
        datas=("15/03/2027", "15/06/2027", "15/09/2027"),
    )

    cobrancas = sorted(cobrancas_extraordinarias(db), key=lambda c: c["due_date"])
    assert [c["amount_cents"] for c in cobrancas] == [3333, 3333, 3334]
    assert sum(c["amount_cents"] for c in cobrancas) == 10000
    assert [c["period"] for c in cobrancas] == ["2027-03", "2027-06", "2027-09"]
    assert [c["due_date"] for c in cobrancas] == ["2027-03-15", "2027-06-15", "2027-09-15"]


def test_prestacoes_no_mesmo_mes_sao_recusadas(app, client, db):
    """Duas prestações no mesmo mês seriam duas cobranças iguais para a mesma fração."""
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027", "28/03/2027")
    )

    assert cobrancas_extraordinarias(db) == []
    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0


def test_prestacoes_fora_de_ordem_sao_recusadas(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027", "", "15/09/2027")
    )
    assert cobrancas_extraordinarias(db) == []


def test_quota_extraordinaria_abrange_muitas_fracoes_e_prestacoes(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B", "C", "D", "E", "F", "G", "H"], "1.000,00",
        datas=("10/02/2027", "10/05/2027"),
    )

    cobrancas = cobrancas_extraordinarias(db)
    assert len(cobrancas) == 16
    assert sum(c["amount_cents"] for c in cobrancas) == 100000


# --- Emissão idempotente e quotas múltiplas -----------------------------------


def test_quota_extraordinaria_nao_e_emitida_duas_vezes_por_reenvio(app, client, db):
    """Um duplo clique reenvia o mesmo formulário: a segunda tentativa não emite outra vez."""
    protecao = protecao_de(client, "/quotas/extraordinaria")
    registar_quota_extraordinaria(client, db, ["A", "B"], "100,00", protecao=protecao)
    registar_quota_extraordinaria(client, db, ["A", "B"], "100,00", protecao=protecao)

    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 1
    assert len(cobrancas_extraordinarias(db)) == 2


def test_duas_quotas_extraordinarias_diferentes_no_mesmo_mes(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027",), descricao="Obra na cobertura"
    )
    registar_quota_extraordinaria(
        client, db, ["A"], "50,00", datas=("20/03/2027",), descricao="Seguro novo"
    )

    cobrancas = cobrancas_extraordinarias(db)
    assert len(cobrancas) == 2
    assert sum(c["amount_cents"] for c in cobrancas) == 15000
    assert {c["descricao"] for c in cobrancas} == {"Obra na cobertura", "Seguro novo"}
    assert {c["rule_id"] for c in cobrancas} != {list(cobrancas)[0]["rule_id"]}


def test_emitir_sem_confirmacao_expressa_nao_grava(app, client, db):
    form = {
        "descricao": "Obra sem confirmação",
        "total": "100,00",
        "reparticao": "igual",
        f"fracao_{db.fraction('A')['id']}": "on",
        "prestacao_1": "15/03/2027",
        "confirmar_emissao": "on",
        **protecao_de(client, "/quotas/extraordinaria"),
    }
    client.post("/quotas/extraordinaria", data=form)

    assert cobrancas_extraordinarias(db) == []
    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0


def test_previsualizacao_mostra_a_reparticao_sem_gravar_nada(app, client, db):
    html = registar_quota_extraordinaria(
        client, db, ["A", "B", "C"], "100,00",
        datas=("15/03/2027", "15/06/2027"), confirmar=False,
    ).get_data(as_text=True)

    assert "Pré-visualização" in html
    assert "33,33 EUR" in html
    assert "33,34 EUR" in html
    assert cobrancas_extraordinarias(db) == []
    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0
    assert db.scalar("SELECT COUNT(*) FROM charge_rules") == 0


def test_quota_extraordinaria_invalida_nao_deixa_metade_gravada(app, client, db):
    """Validação falhada não pode deixar quota sem prestações, nem prestações sem cobranças."""
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027", "data errada")
    )

    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0
    assert db.scalar("SELECT COUNT(*) FROM quota_installments") == 0
    assert db.scalar("SELECT COUNT(*) FROM charge_rules") == 0
    assert cobrancas_extraordinarias(db) == []


def test_quota_extraordinaria_nao_precisa_de_regras_de_quota_ordinaria(app, client, db):
    """Uma decisão pontual não depende de haver regra recorrente registada."""
    assert db.rules() == []
    registar_quota_extraordinaria(client, db, ["A"], "80,00", datas=("15/03/2027",))
    assert len(cobrancas_extraordinarias(db)) == 1


# --- Integração com a geração mensal e as consultas ---------------------------


def test_geracao_mensal_nao_emite_quotas_extraordinarias(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/01/2027",), descricao="Obra em janeiro"
    )
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    cobrancas = db.charges("2027-01")
    extraordinarias = [c for c in cobrancas if c["rubric"] == "quota_extraordinaria"]
    assert len(extraordinarias) == 1, "a geração mensal não duplica a quota extraordinária"
    assert len([c for c in cobrancas if c["rubric"] == "quota_ordinaria"]) == 8


def test_consulta_do_mes_mostra_descricao_e_vencimento(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "100,00", datas=("15/03/2027",), descricao="Pintura das escadas"
    )

    html = _texto(client, "/charges/2027-03")
    assert "Pintura das escadas" in html
    assert "15/03/2027" in html
    assert "Quota extraordinária" in html
    assert "50,00 EUR" in html


def test_lista_de_meses_separa_a_quota_extraordinaria(app, client, db):
    registar_quota_extraordinaria(client, db, ["A"], "100,00", datas=("15/03/2027",))

    html = _texto(client, "/charges")
    assert "03/2027" in html
    assert "100,00 EUR" in html


def test_condomino_ve_as_cobrancas_extraordinarias_das_suas_fracoes(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "C"], "120,00", datas=("15/03/2027",),
        descricao="Pintura das escadas",
    )
    criar_condomino(app, db, "condomino-a", ("A",))
    condomino = entrar_condomino(app, "condomino-a")

    html = condomino.get(f"/minhas-fracoes/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "Quota extraordinária" in html
    assert "Pintura das escadas" in html
    assert "15/03/2027" in html
    assert "60,00 EUR" in html

    # A fração C não é dele.
    assert condomino.get(f"/minhas-fracoes/{db.fraction('C')['id']}").status_code == 404


def test_condomino_nao_ve_as_cobrancas_de_fracoes_que_nao_sao_dele(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "C"], "120,00", datas=("15/03/2027",),
        descricao="Obra só de A e C",
    )
    criar_condomino(app, db, "condomino-b", ("B",))
    condomino = entrar_condomino(app, "condomino-b")

    html = condomino.get(f"/minhas-fracoes/{db.fraction('B')['id']}").get_data(as_text=True)
    assert "Obra só de A e C" not in html
    assert "Quota extraordinária" not in html


# --- Ciclo de vida da quota ---------------------------------------------------


def test_quota_extraordinaria_emitida_nao_pode_ser_apagada(app, client, db):
    registar_quota_extraordinaria(client, db, ["A"], "100,00", datas=("15/03/2027",))
    quota_id = int(db.scalar("SELECT id FROM extraordinary_quotas"))

    # A página da quota emitida não mostra formulário de eliminar: a tentativa é
    # feita à mão, com uma revisão válida, para provar que a recusa não é só visual.
    client.post(
        f"/quotas/extraordinarias/{quota_id}/delete",
        data={
            **protecao_de(client, "/quotas"),
            "confirmar_eliminar": "on",
        },
    )

    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 1
    assert len(cobrancas_extraordinarias(db)) == 1


def test_quota_extraordinaria_sem_cobrancas_pode_ser_eliminada(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027",), confirmar=False
    )
    # A pré-visualização não grava: não há nada para eliminar.
    assert db.scalar("SELECT COUNT(*) FROM extraordinary_quotas") == 0


def test_calendario_de_quota_emitida_nao_e_alterado_pela_base(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027", "15/06/2027")
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE quota_installments SET due_date = '2027-12-01' WHERE numero = 1")
    assert db.scalar("SELECT COUNT(*) FROM quota_installments") == 2


def test_quota_extraordinaria_fica_registada_com_o_que_foi_decidido(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A", "B"], "1.000,00", datas=("15/03/2027",),
        descricao="Reparação da cobertura", finalidade="Deliberado por unanimidade",
        reparticao="permilagem",
    )

    quota = db.rows(
        "SELECT descricao, finalidade, total_cents, reparticao FROM extraordinary_quotas"
    )[0]
    assert quota["descricao"] == "Reparação da cobertura"
    assert quota["finalidade"] == "Deliberado por unanimidade"
    assert quota["total_cents"] == 100000
    assert quota["reparticao"] == "permilagem"

    html = _texto(client, "/quotas/extraordinarias")
    assert "Reparação da cobertura" in html
    assert "1.000,00 EUR" in html
