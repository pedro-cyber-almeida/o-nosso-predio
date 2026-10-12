"""Regras de quotas: valores, vigência por meses, conflitos e proteção na base."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import configurar_regras_de_teste, protecao_de, registar_regra


def _texto(client, caminho="/quotas") -> str:
    return client.get(caminho).get_data(as_text=True)


def test_regra_valida_fica_em_centimos_e_com_vigencia_de_meses_inteiros(app, client, db):
    resposta = registar_regra(
        client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", "12/2027"
    )

    assert resposta.status_code == 302
    regras = db.rules()
    assert len(regras) == 1
    regra = regras[0]
    assert regra["fraction_code"] == "A"
    assert regra["rubric"] == "quota_ordinaria"
    assert regra["amount_cents"] == 4000
    assert regra["valid_from"] == "2027-01-01"
    assert regra["valid_to"] == "2027-12-31"


def test_regra_para_varias_fracoes_cria_uma_regra_por_fracao(app, client, db):
    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "40,00", "01/2027")

    regras = db.rules()
    assert {r["fraction_code"] for r in regras} == {"A", "C", "E", "G"}
    assert all(r["amount_cents"] == 4000 for r in regras)
    assert all(r["valid_from"] == "2027-01-01" and r["valid_to"] is None for r in regras)


@pytest.mark.parametrize("valor", ["0", "0,00", "-40,00", "-1", "abc", "", "10.000,01"])
def test_valor_invalido_ou_exagerado_e_rejeitado_sem_guardar_regra(app, client, db, valor):
    registar_regra(client, db, ["A"], "quota_ordinaria", valor, "01/2027")
    assert db.rules() == []


@pytest.mark.parametrize("inicio", ["", "15/01/2027", "2027-01-15", "13/2027", "abc"])
def test_inicio_de_vigencia_invalido_e_rejeitado(app, client, db, inicio):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", inicio)
    assert db.rules() == []


def test_fim_anterior_ao_inicio_e_rejeitado(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", "12/2026")
    assert db.rules() == []


def test_regra_sem_confirmacao_explícita_nao_e_guardada(app, client, db):
    registar_regra(
        client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", confirmar=False
    )
    assert db.rules() == []
    assert "exige confirmar" in _texto(client)


def test_sobreposicao_de_regras_da_mesma_fracao_e_rubrica_e_recusada(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    resposta = registar_regra(client, db, ["A"], "quota_ordinaria", "45,00", "06/2027")

    regras = db.rules()
    assert len(regras) == 1, "a segunda regra não pode ter entrado"
    assert regras[0]["amount_cents"] == 4000
    assert "Já existe uma regra" in _texto(client)
    assert resposta.status_code == 302


def test_meses_encostados_nao_sao_sobreposicao(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", "06/2027")
    registar_regra(client, db, ["A"], "quota_ordinaria", "45,00", "07/2027", "12/2027")

    regras = sorted(db.rules(), key=lambda r: r["valid_from"])
    assert len(regras) == 2
    assert regras[0]["valid_to"] == "2027-06-30"
    assert regras[1]["valid_from"] == "2027-07-01"


def test_rubricas_diferentes_na_mesma_fracao_coexistem(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    registar_regra(client, db, ["A"], "reforco_fundo", "3,00", "01/2027", "12/2027")

    regras = db.rules()
    assert len(regras) == 2
    assert {r["rubric"] for r in regras} == {"quota_ordinaria", "reforco_fundo"}


def test_a_base_recusa_sobreposicao_escrita_diretamente(app, db):
    """A travão da base vale mesmo quando alguém escreve fora da aplicação."""
    id_a = db.fraction("A")["id"]
    db.execute(
        "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, valid_to, "
        "source, created_at, updated_at) VALUES (?, 'quota_ordinaria', 4000, '2027-01-01', "
        "'2027-12-31', '', '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')",
        (id_a,),
    )

    with pytest.raises(sqlite3.IntegrityError) as erro:
        db.execute(
            "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, "
            "valid_to, source, created_at, updated_at) VALUES (?, 'quota_ordinaria', 4500, "
            "'2027-06-01', NULL, '', '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')",
            (id_a,),
        )
    assert "sobreposta" in str(erro.value)


def test_a_base_recusa_vigencia_que_nao_cobre_meses_inteiros(app, db):
    id_a = db.fraction("A")["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, "
            "source, created_at, updated_at) VALUES (?, 'quota_ordinaria', 4000, "
            "'2027-01-15', '', '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')",
            (id_a,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, "
            "valid_to, source, created_at, updated_at) VALUES (?, 'quota_ordinaria', 4000, "
            "'2027-01-01', '2027-01-15', '', '2026-10-09T00:00:00+00:00', "
            "'2026-10-09T00:00:00+00:00')",
            (id_a,),
        )
    assert db.rules() == []


def test_configurar_regras_de_teste_registra_as_oito_fracoes_e_as_duas_rubricas(app, client, db):
    configurar_regras_de_teste(client, db)

    regras = db.rules()
    assert len(regras) == 16, "8 quotas ordinárias + 8 reforços"

    ordinarias = {r["fraction_code"]: r["amount_cents"] for r in regras if r["rubric"] == "quota_ordinaria"}
    assert ordinarias == {"A": 4000, "C": 4000, "E": 4000, "G": 4000,
                          "B": 4500, "D": 4500, "F": 4500, "H": 4500}
    assert all(r["valid_from"] == "2027-01-01" and r["valid_to"] is None
               for r in regras if r["rubric"] == "quota_ordinaria")

    reforgos = [r for r in regras if r["rubric"] == "reforco_fundo"]
    assert len(reforgos) == 8
    assert all(r["amount_cents"] == 300 for r in reforgos)
    assert all(r["valid_from"] == "2027-01-01" and r["valid_to"] == "2027-12-31"
               for r in reforgos)


def test_registar_a_mesma_regra_duas_vezes_e_recusado_sem_duplicar(app, client, db):
    configurar_regras_de_teste(client, db)
    antes = db.rules()

    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "40,00", "01/2027")

    assert db.rules() == antes
    assert "Já existe uma regra" in _texto(client)


def test_regra_sem_confirmacao_de_vigencia_nao_e_guardada(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", confirmar=False)
    assert db.rules() == []


def test_encerrar_a_vigencia_de_uma_regra(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    regra = db.rules()[0]

    client.post(
        f"/quotas/{regra['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "06/2027"},
    )

    atualizada = db.rules()[0]
    assert atualizada["valid_to"] == "2027-06-30"
    assert atualizada["amount_cents"] == 4000


def test_encerrar_com_data_invalida_nao_altera_a_regra(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027", "12/2027")
    regra = db.rules()[0]

    client.post(
        f"/quotas/{regra['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "15/06/2027"},
    )
    assert db.rules()[0]["valid_to"] == "2027-12-31"

    client.post(
        f"/quotas/{regra['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "12/2026"},
    )
    assert db.rules()[0]["valid_to"] == "2027-12-31"


def test_eliminar_regra_sem_cobrancas_emitidas(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    regra = db.rules()[0]

    client.post(
        f"/quotas/{regra['id']}/delete",
        data={**protecao_de(client, "/quotas"), "confirmar_eliminar": "on"},
    )
    assert db.rules() == []


def test_eliminar_regra_exige_confirmacao(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    regra = db.rules()[0]

    client.post(
        f"/quotas/{regra['id']}/delete",
        data={**protecao_de(client, "/quotas")},
    )
    assert len(db.rules()) == 1


def test_ids_de_fracao_inventados_sao_recusados_sem_pagina_de_erro(app, client, db):
    resposta = client.post(
        "/quotas/add",
        data={
            **protecao_de(client, "/quotas"),
            "fracao_9999": "on",
            "rubrica": "quota_ordinaria",
            "valor": "40,00",
            "inicio": "01/2027",
            "confirmar_vigencia": "on",
        },
    )

    assert resposta.status_code == 302
    assert db.rules() == []
    assert "não corresponde a frações registadas" in _texto(client)


def test_texto_de_um_campo_com_caracteres_a_mais_e_recusado_em_vez_de_cortado(app, client, db):
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027",
                    fonte="Ata " + "x" * 200)

    assert db.rules() == []
    assert "no máximo 120 caracteres" in _texto(client)


def test_formulario_de_regras_desatualizado_nao_grava(app, client, db):
    protecao = protecao_de(client, "/quotas")

    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")

    registar_regra(client, db, ["B"], "quota_ordinaria", "45,00", "01/2027", protecao=protecao)

    codigos = {r["fraction_code"] for r in db.rules()}
    assert codigos == {"A"}, "a submissão desatualizada não pode ter registado a regra de B"
    assert "desatualizada" in _texto(client)


def test_regra_de_fracao_desativada_continua_registada_mas_nao_e_apagada(app, client, db):
    """Desativar uma fração não apaga a regra nem o que ela originou."""
    registar_regra(client, db, ["A"], "quota_ordinaria", "40,00", "01/2027")
    id_a = db.fraction("A")["id"]

    client.post(
        f"/fractions/{id_a}/edit",
        data={
            **protecao_de(client, f"/fractions/{id_a}"),
            "code": "A",
            "active": "",
        },
    )

    assert db.fraction("A")["active"] == 0
    assert len(db.rules()) == 1


def test_id_de_fracao_com_texto_estranho_e_recusado_sem_erro_interno(app, client, db):
    """`fracao_²` passa em `str.isdigit()` mas não é um id: tem de ser recusa, não 300."""
    protecao = protecao_de(client, "/quotas")
    resposta = client.post(
        "/quotas/add",
        data={**protecao, "confirmar_vigencia": "on", "rubrica": "quota_ordinaria",
              "valor": "40,00", "inicio": "01/2027", "fim": "", "fracao_²": "on"},
        follow_redirects=True,
    )

    assert resposta.status_code == 200
    assert "escolhe pelo menos uma fração" in resposta.get_data(as_text=True)
    assert db.rules() == []
