"""Entrega 2: corrigir cobranças emitidas e editar regras só para o futuro."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    afetar_pagamento,
    alterar_config,
    configurar_regras_de_teste,
    corrigir_cobranca,
    gerar_mes,
    id_da_regra_da_fracao,
    mudar_valor_de_regra,
    protecao_de,
    registar_pagamento,
    registar_quota_extraordinaria,
    ultimo_pagamento,
)


def _texto(client, caminho: str) -> str:
    return client.get(caminho).get_data(as_text=True)


def _cobranca_de(db, code: str, rubrica: str = "quota_ordinaria", period: str = "2027-01") -> dict:
    linhas = [
        c
        for c in db.charges(period)
        if c["fraction_code"] == code and c["rubric"] == rubrica
    ]
    assert len(linhas) == 1, linhas
    return linhas[0]


def _pagamento_afetado(client, db, code: str, charge_id: int, valor: str = "40,00") -> dict:
    """Registar um pagamento e afetá-lo a uma cobrança: é o que uma correção não pode tocar."""
    registar_pagamento(client, db, code, valor, "12/01/2027")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(client, pagamento["id"], {charge_id: valor})
    return pagamento


# --- Corrigir uma cobrança ----------------------------------------------------


def test_corrigir_cobranca_exige_motivo(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")

    corrigir_cobranca(client, db, cobranca["id"], valor="45,00", motivo=None)
    assert db.charges()[0]["amount_cents"] == 4000

    corrigir_cobranca(client, db, cobranca["id"], valor="45,00", motivo="   ")
    assert db.charges()[0]["amount_cents"] == 4000


def test_corrigir_cobranca_exige_confirmacao_dos_efeitos(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")

    form = {
        "valor": "45,00",
        "vencimento": "10/01/2027",
        "descricao": "",
        "motivo": "A ata fixou 45,00 EUR.",
        "confirmar_correcao": "on",
        **protecao_de(client, f"/charges/{cobranca['id']}/corrigir"),
    }
    client.post(f"/charges/{cobranca['id']}/corrigir", data=form)

    assert db.charges()[0]["amount_cents"] == 4000
    assert db.scalar("SELECT COUNT(*) FROM charge_revisions") == 0


def test_previsualizacao_da_correcao_mostra_os_efeitos_sem_gravar(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")

    html = corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00", confirmar=False
    ).get_data(as_text=True)

    assert "Efeitos da correção" in html
    assert "40,00 EUR" in html
    assert "45,00 EUR" in html
    assert db.charges()[0]["amount_cents"] == 4000
    assert db.scalar("SELECT COUNT(*) FROM charge_revisions") == 0


def test_correcao_fica_registada_com_antes_depois_e_motivo(app, client, db, hoje):
    configurar_regras_de_teste(client, db)
    alterar_config(client, db, quota_dia_vencimento="10")
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")

    corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00", vencimento="15/01/2027",
        motivo="A ata n.º 23 fixou 45,00 EUR.",
    )

    revisoes = db.rows(
        "SELECT motivo, amount_before, amount_after, due_before, due_after "
        "FROM charge_revisions ORDER BY id"
    )
    assert len(revisoes) == 1
    assert revisoes[0]["amount_before"] == 4000
    assert revisoes[0]["amount_after"] == 4500
    assert revisoes[0]["due_before"] == "2027-01-10"
    assert revisoes[0]["due_after"] == "2027-01-15"
    assert revisoes[0]["motivo"] == "A ata n.º 23 fixou 45,00 EUR."

    depois = _cobranca_de(db, "A")
    assert depois["amount_cents"] == 4500
    assert depois["due_date"] == "2027-01-15"

    html = _texto(client, f"/charges/{cobranca['id']}/corrigir")
    assert "A ata n.º 23 fixou 45,00 EUR." in html
    assert "40,00 EUR" in html and "45,00 EUR" in html


def test_correcao_nao_apaga_o_historico_nem_anula_pagamentos(app, client, db, hoje):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")
    _pagamento_afetado(client, db, "A", cobranca["id"])

    pagamentos_antes = db.payments()
    # Só as colunas da própria afetação: `allocations()` junta o valor atual da
    # cobrança, e esse é justamente o que a correção muda.
    afectoes_antes = db.rows(
        "SELECT id, payment_id, charge_id, amount_cents, created_at "
        "FROM payment_allocations ORDER BY id"
    )

    corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00", motivo="Valor corrigido em assembleia."
    )

    assert db.payments() == pagamentos_antes, "os pagamentos não são tocados"
    assert db.rows(
        "SELECT id, payment_id, charge_id, amount_cents, created_at "
        "FROM payment_allocations ORDER BY id"
    ) == afectoes_antes, "as ligações aos pagamentos conservam-se"
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1
    assert db.rows("SELECT cancelled_at FROM payments")[0]["cancelled_at"] is None

    # A dívida passa a ser a diferença, não um valor inventado.
    html = _texto(client, f"/payments/fraction/{db.fraction('A')['id']}")
    assert "3,00 EUR" in html


def test_correcao_abaixo_do_ja_pago_e_recusada_e_explica_o_que_ajustar(app, client, db, hoje):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")
    _pagamento_afetado(client, db, "A", cobranca["id"])

    html = corrigir_cobranca(
        client, db, cobranca["id"], valor="35,00",
        motivo="Engano no valor registado.",
    ).get_data(as_text=True)

    assert "40,00 EUR" in html
    assert "Ajusta primeiro a distribuição dos pagamentos" in html
    assert _cobranca_de(db, "A")["amount_cents"] == 4000
    assert db.scalar("SELECT COUNT(*) FROM charge_revisions") == 0


def test_correcao_igual_ao_pago_ja_afetado_e_aceite(app, client, db, hoje):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")
    _pagamento_afetado(client, db, "A", cobranca["id"])

    corrigir_cobranca(
        client, db, cobranca["id"], valor="40,00", vencimento="20/01/2027",
        motivo="O vencimento estava errado.",
    )
    assert _cobranca_de(db, "A")["due_date"] == "2027-01-20"


def test_correcao_nao_muda_a_origem_da_cobranca(app, client, db):
    """Fração, rubrica, mês e regra de origem não se alteram: isso seria outra cobrança."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")
    antes = dict(cobranca)

    corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00",
        vencimento="10/02/2027", motivo="Tentar mudar o mês.",
    )

    depois = _cobranca_de(db, "A")
    assert depois["amount_cents"] == 4000, "a correção incompatível não foi aplicada"
    for campo in ("fraction_id", "rubric", "period", "rule_id", "generated_at"):
        assert depois[campo] == antes[campo]

    # Escrita direta à base, contornando a aplicação: a base também recusa.
    db.execute(
        "INSERT INTO charge_revisions (charge_id, changed_at, motivo, edit_token, "
        "amount_before, amount_after) VALUES (?, '2027-02-01T10:00:00+00:00', "
        "'tentar mudar de mês', 'marca-direta', 4000, 4000)",
        (cobranca["id"],),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE charges SET period = '2027-02', edit_token = 'marca-direta' WHERE id = ?",
            (cobranca["id"],),
        )
    assert _cobranca_de(db, "A")["period"] == "2027-01"


def test_cobranca_emitida_nunca_e_apagada(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM charges WHERE id = ?", (db.charges()[0]["id"],))


def test_reenvio_da_mesma_correcao_nao_cria_segundo_registo(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    cobranca = _cobranca_de(db, "A")

    protecao = protecao_de(client, f"/charges/{cobranca['id']}/corrigir")
    corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00", motivo="Corrigido uma vez.",
        protecao=protecao,
    )
    # O mesmo corpo reenviado: a revisão do formulário já não corresponde ao estado
    # guardado, e a segunda tentativa não cria um segundo registo.
    corrigir_cobranca(
        client, db, cobranca["id"], valor="45,00", motivo="Corrigido uma vez.",
        protecao=protecao,
    )

    assert db.scalar("SELECT COUNT(*) FROM charge_revisions") == 1
    assert _cobranca_de(db, "A")["amount_cents"] == 4500


def test_correcao_de_cobranca_extraordinaria_preserva_as_outras_prestacoes(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027", "15/06/2027", "15/09/2027"),
        descricao="Reparação da cobertura",
    )
    cobrancas = sorted(
        (c for c in db.charges() if c["rubric"] == "quota_extraordinaria"),
        key=lambda c: c["due_date"],
    )
    assert [c["amount_cents"] for c in cobrancas] == [3333, 3333, 3334]

    corrigir_cobranca(
        client, db, cobrancas[0]["id"], valor="34,00",
        motivo="A primeira prestação ficou mais cara.",
    )

    depois = sorted(
        (c for c in db.charges() if c["rubric"] == "quota_extraordinaria"),
        key=lambda c: c["due_date"],
    )
    assert depois[0]["amount_cents"] == 3400
    assert [c["amount_cents"] for c in depois[1:]] == [3333, 3334], (
        "as outras prestações não são tocadas"
    )
    assert depois[0]["descricao"] == "Reparação da cobertura"


# --- Editar regras: só emissões futuras ---------------------------------------


def test_mudar_valor_de_regra_exige_motivo_e_confirmacao(app, client, db):
    configurar_regras_de_teste(client, db)
    regra = id_da_regra_da_fracao(db, "A")

    client.post(
        f"/quotas/{regra}/valor",
        data={**protecao_de(client, "/quotas"), "valor": "45,00", "confirmar_valor": "on"},
    )
    assert db.scalar("SELECT amount_cents FROM charge_rules WHERE id = ?", (regra,)) == 4000

    client.post(
        f"/quotas/{regra}/valor",
        data={
            **protecao_de(client, "/quotas"),
            "valor": "45,00",
            "motivo": "Deliberado na ata n.º 23.",
        },
    )
    assert db.scalar("SELECT amount_cents FROM charge_rules WHERE id = ?", (regra,)) == 4000


def test_mudar_valor_de_regra_afeta_so_as_emissoes_futuras(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    regra = id_da_regra_da_fracao(db, "A")
    janeiro = _cobranca_de(db, "A", period="2027-01")

    mudar_valor_de_regra(client, db, regra, "45,00", motivo="Deliberado na ata n.º 23.")
    gerar_mes(client, "2027-02")

    assert _cobranca_de(db, "A", period="2027-01")["amount_cents"] == 4000, (
        "a cobrança de janeiro não foi reescrita"
    )
    assert _cobranca_de(db, "A", period="2027-01")["id"] == janeiro["id"]
    assert _cobranca_de(db, "A", period="2027-02")["amount_cents"] == 4500

    revisoes = db.rows(
        "SELECT motivo, amount_before, amount_after FROM charge_rule_revisions ORDER BY id"
    )
    assert len(revisoes) == 1
    assert revisoes[0]["amount_before"] == 4000
    assert revisoes[0]["amount_after"] == 4500
    assert revisoes[0]["motivo"] == "Deliberado na ata n.º 23."


def test_encerrar_regra_nao_reescreve_cobrancas_emitidas(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    regra = id_da_regra_da_fracao(db, "A")

    client.post(
        f"/quotas/{regra}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "01/2027"},
    )

    assert _cobranca_de(db, "A", period="2027-01")["amount_cents"] == 4000
    assert db.scalar("SELECT COUNT(*) FROM charges") == 16


def test_regra_extraordinaria_nao_se_edita_por_si_sozinha(app, client, db):
    registar_quota_extraordinaria(
        client, db, ["A"], "100,00", datas=("15/03/2027",), descricao="Obra"
    )
    regra = int(db.scalar("SELECT id FROM charge_rules WHERE rubric = 'quota_extraordinaria'"))

    mudar_valor_de_regra(client, db, regra, "80,00", motivo="tentar")

    assert db.scalar("SELECT amount_cents FROM charge_rules WHERE id = ?", (regra,)) == 10000
    assert db.scalar("SELECT COUNT(*) FROM charge_rule_revisions") == 0


def test_escrita_direta_muda_valor_de_regra_sem_motivo_e_recusada(app, client, db):
    configurar_regras_de_teste(client, db)
    regra = id_da_regra_da_fracao(db, "A")

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE charge_rules SET amount_cents = 4500 WHERE id = ?", (regra,))
    assert db.scalar("SELECT amount_cents FROM charge_rules WHERE id = ?", (regra,)) == 4000

    # Encerrar a vigência continua a ser uma alteração normal, sem revisão de valor.
    db.execute(
        "UPDATE charge_rules SET valid_to = '2027-06-30' WHERE id = ?", (regra,)
    )
    assert db.scalar("SELECT valid_to FROM charge_rules WHERE id = ?", (regra,)) == "2027-06-30"
