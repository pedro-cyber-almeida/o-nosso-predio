"""Correção de pagamentos: todos os campos de negócio, com histórico e totais recalculados.

O que estes testes fixam:
- o pagamento continua a ser um registo só; corrigir não cria uma segunda receita;
- a correção exige motivo e confirmação explícita, e é pré-visualizável sem gravar;
- baixar o valor obriga a ajustar a distribuição no mesmo formulário;
- mudar de fração retira as afetações e reabre as cobranças antigas, nunca transfere;
- o que estava antes fica guardado, com data e motivo, e é apresentado;
- um pagamento anulado não é editado nem reativado;
- reenvio e formulário envelhecido não duplicam nem sobrepõem efeitos.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.payments import _totais
from conftest import (
    afetar_pagamento,
    anular_pagamento,
    editar_pagamento,
    id_da_cobranca,
    liquidado_da_cobranca,
    protecao_de_edicao,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Estes testes correm como se hoje fosse 01/02/2027."""


def pagamento_guardado(db, payment_id: int) -> dict:
    linhas = db.rows("SELECT * FROM payments WHERE id = ?", (payment_id,))
    assert linhas, f"não há pagamento {payment_id}"
    return linhas[0]


def correcoes_guardadas(db, payment_id: int) -> list[dict]:
    return db.rows(
        "SELECT * FROM payment_revisions WHERE payment_id = ? ORDER BY id", (payment_id,)
    )


def test_editar_atualiza_os_campos_de_negocio_sem_criar_segundo_pagamento(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00", referencia="REF-1", nota="nota antiga")
    pagamento = ultimo_pagamento(db)

    resposta = editar_pagamento(
        client,
        db,
        pagamento["id"],
        valor="57,00",
        data="20/01/2027",
        meio="dinheiro",
        referencia="REF-2",
        nota="nota corrigida",
        motivo="Valor e meio registados errados",
    )
    assert resposta.status_code == 302

    assert len(db.payments()) == 1, "corrigir criou um segundo pagamento"
    atual = pagamento_guardado(db, pagamento["id"])
    assert atual["amount_cents"] == 5700
    assert atual["paid_on"] == "2027-01-20"
    assert atual["method"] == "dinheiro"
    assert atual["reference"] == "REF-2"
    assert atual["source"] == "nota corrigida"
    assert atual["edited_at"], "a correção não ficou marcada no pagamento"
    assert atual["edit_reason"] == "Valor e meio registados errados"

    # Identificadores e rasto de auditoria não são campos de negócio.
    assert atual["id"] == pagamento["id"]
    assert atual["created_at"] == pagamento["created_at"]
    assert atual["form_token"] == pagamento["form_token"]

    correcoes = correcoes_guardadas(db, pagamento["id"])
    assert len(correcoes) == 1
    correcao = correcoes[0]
    assert correcao["old_amount_cents"] == 4300
    assert correcao["new_amount_cents"] == 5700
    assert correcao["old_paid_on"] == "2027-01-15"
    assert correcao["new_paid_on"] == "2027-01-20"
    assert correcao["old_method"] == "transferencia"
    assert correcao["new_method"] == "dinheiro"
    assert correcao["old_reference"] == "REF-1"
    assert correcao["new_reference"] == "REF-2"
    assert correcao["old_source"] == "nota antiga"
    assert correcao["new_source"] == "nota corrigida"
    assert correcao["reason"] == "Valor e meio registados errados"
    assert correcao["changed_at"]


def test_previsualizar_mostra_o_efeito_sem_gravar_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    cobranca = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    resposta = editar_pagamento(
        client,
        db,
        pagamento["id"],
        valor="60,00",
        afetoes={cobranca: "40,00"},
        motivo="Ver o efeito antes de confirmar",
        previsualizar=True,
        confirmar=False,
    )
    html = resposta.get_data(as_text=True)
    assert "Efeito desta correção" in html
    assert "43,00 EUR" in html and "60,00 EUR" in html

    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300
    assert db.allocations() == []
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0


def test_sem_motivo_nenhum_pagamento_e_corrigido(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = editar_pagamento(client, db, pagamento["id"], valor="60,00", motivo=None)
    assert resposta.status_code == 200
    assert "Motivo da correção" in resposta.get_data(as_text=True)
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0


def test_sem_confirmacao_explícita_nenhum_pagamento_e_corrigido(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = editar_pagamento(
        client, db, pagamento["id"], valor="60,00", motivo="Valor errado", confirmar=False
    )
    assert resposta.status_code == 200
    assert "confirmação explícita" in resposta.get_data(as_text=True)
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0


def test_descer_o_valor_abaixo_do_distribuido_exige_ajustar_a_distribuicao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    cobranca = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    afetar_pagamento(client, pagamento["id"], {cobranca: "40,00"})

    resposta = editar_pagamento(
        client, db, pagamento["id"], valor="30,00", motivo="Valor errado"
    )
    assert resposta.status_code == 200
    assert "excede o novo valor" in resposta.get_data(as_text=True)
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300
    assert liquidado_da_cobranca(db, cobranca) == 4000
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0

    editar_pagamento(
        client,
        db,
        pagamento["id"],
        valor="30,00",
        afetoes={cobranca: "30,00"},
        motivo="Valor errado e distribuição ajustada",
    )
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 3000
    assert liquidado_da_cobranca(db, cobranca) == 3000

    # As duas distribuições ficam guardadas na correção.
    revision = db.scalar("SELECT id FROM payment_revisions ORDER BY id DESC LIMIT 1")
    assert db.scalar(
        "SELECT amount_cents FROM payment_allocation_revisions "
        "WHERE revision_id = ? AND lado = 'antes'",
        (revision,),
    ) == 4000
    assert db.scalar(
        "SELECT amount_cents FROM payment_allocation_revisions "
        "WHERE revision_id = ? AND lado = 'depois'",
        (revision,),
    ) == 3000


def test_mudar_de_fracao_retira_as_afetacoes_e_reabre_as_cobrancas_antigas(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    cobranca_a = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    afetar_pagamento(client, pagamento["id"], {cobranca_a: "40,00"})

    resposta = editar_pagamento(
        client,
        db,
        pagamento["id"],
        code="B",
        afetoes={},
        motivo="Movimento pertencia à fração B",
        confirmar_fracao=False,
    )
    assert resposta.status_code == 200
    assert "retira as" in resposta.get_data(as_text=True)
    assert pagamento_guardado(db, pagamento["id"])["fraction_id"] == db.fraction("A")["id"]
    assert liquidado_da_cobranca(db, cobranca_a) == 4000

    editar_pagamento(
        client,
        db,
        pagamento["id"],
        code="B",
        afetoes={},
        motivo="Movimento pertencia à fração B",
    )
    assert pagamento_guardado(db, pagamento["id"])["fraction_id"] == db.fraction("B")["id"]
    assert db.allocations() == [], "a afetação antiga ficou presa a um pagamento de outra fração"
    assert liquidado_da_cobranca(db, cobranca_a) == 0, "a cobrança de A não voltou a estar em dívida"

    cobranca_b = id_da_cobranca(db, "B", "2027-01", "quota_ordinaria")
    assert liquidado_da_cobranca(db, cobranca_b) == 0, "nenhuma afetação foi transferida"

    correcao = correcoes_guardadas(db, pagamento["id"])[-1]
    assert correcao["old_fraction_id"] == db.fraction("A")["id"]
    assert correcao["new_fraction_id"] == db.fraction("B")["id"]
    assert db.rows(
        "SELECT charge_id FROM payment_allocation_revisions WHERE revision_id = ? AND lado = 'antes'",
        (correcao["id"],),
    ) == [{"charge_id": cobranca_a}]
    assert db.rows(
        "SELECT charge_id FROM payment_allocation_revisions WHERE revision_id = ? AND lado = 'depois'",
        (correcao["id"],),
    ) == []


def test_afetacao_a_cobranca_de_outra_fracao_e_recusada_na_edicao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "60,00")
    pagamento = ultimo_pagamento(db)
    cobranca_b = id_da_cobranca(db, "B", "2027-01", "quota_ordinaria")

    resposta = editar_pagamento(
        client,
        db,
        pagamento["id"],
        afetoes={cobranca_b: "45,00"},
        motivo="Tentativa de liquidar outra fração",
    )
    assert resposta.status_code == 200
    assert "não pertence à fração A" in resposta.get_data(as_text=True)
    assert db.allocations() == []
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0


def test_afetacao_na_edicao_nao_pode_exceder_o_que_esta_em_divida(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "60,00")
    pagamento = ultimo_pagamento(db)
    cobranca = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    resposta = editar_pagamento(
        client, db, pagamento["id"], afetoes={cobranca: "45,00"}, motivo="Distribuição errada"
    )
    assert resposta.status_code == 200
    assert "faltam 40,00 EUR" in resposta.get_data(as_text=True)
    assert liquidado_da_cobranca(db, cobranca) == 0

    editar_pagamento(
        client, db, pagamento["id"], afetoes={cobranca: "40,00"}, motivo="Distribuição corrigida"
    )
    assert liquidado_da_cobranca(db, cobranca) == 4000
    with app.app_context():
        totais = _totais(db.fraction("A")["id"])
    assert totais["afetado_cents"] == 4000
    assert totais["credito_cents"] == 2000


def test_corrigir_nao_duplica_receita_e_os_totais_passam_a_refletir_a_correcao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "45,00")
    pagamento = ultimo_pagamento(db)
    cobranca_a = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    afetar_pagamento(client, pagamento["id"], {cobranca_a: "40,00"})
    cobranca_b = id_da_cobranca(db, "B", "2027-01", "quota_ordinaria")

    editar_pagamento(
        client,
        db,
        pagamento["id"],
        code="B",
        afetoes={cobranca_b: "45,00"},
        motivo="Movimento e distribuição pertenciam à fração B",
    )

    assert len(db.payments()) == 1
    assert db.scalar("SELECT SUM(amount_cents) FROM payments") == 4500
    assert db.scalar("SELECT SUM(amount_cents) FROM payment_allocations") == 4500

    with app.app_context():
        fra_a = _totais(db.fraction("A")["id"])
        fra_b = _totais(db.fraction("B")["id"])
        predio = _totais(None)

    assert fra_a["recebido_cents"] == 0
    assert fra_a["afetado_cents"] == 0
    assert fra_a["divida_cents"] == 4300
    assert fra_b["recebido_cents"] == 4500
    assert fra_b["afetado_cents"] == 4500
    assert fra_b["credito_cents"] == 0
    assert predio["recebido_cents"] == 4500, "a receita conta mais do que uma vez"

    html = client.get("/payments").get_data(as_text=True)
    assert "corrigido 1 vez" in html
    assert "Editar" in html


def test_historico_mostra_os_valores_anteriores_e_os_novos_com_o_motivo(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    editar_pagamento(client, db, pagamento["id"], valor="57,00", motivo="Primeira correção")
    editar_pagamento(client, db, pagamento["id"], valor="60,00", motivo="Segunda correção")

    assert len(db.payments()) == 1
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 6000
    correcoes = correcoes_guardadas(db, pagamento["id"])
    assert [c["reason"] for c in correcoes] == ["Primeira correção", "Segunda correção"]
    assert [c["old_amount_cents"] for c in correcoes] == [4300, 5700]

    detalhe = client.get(f"/payments/{pagamento['id']}").get_data(as_text=True)
    historico = client.get(f"/payments/{pagamento['id']}/history").get_data(as_text=True)
    assert "Primeira correção" in historico
    assert "Segunda correção" in historico
    assert "43,00 EUR" in historico and "57,00 EUR" in historico and "60,00 EUR" in historico
    assert "Este pagamento foi corrigido em" in detalhe
    assert "Segunda correção" in detalhe
    assert "Ver as correções registadas" in detalhe


def test_pagamento_anulado_nao_e_editado_nem_reativado(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    anular_pagamento(client, pagamento["id"], motivo="Movimento que não existia")

    assert client.get(f"/payments/{pagamento['id']}/edit").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payments SET cancelled_at = NULL WHERE id = ?", (pagamento["id"],))
    assert pagamento_guardado(db, pagamento["id"])["cancelled_at"] is not None


def test_alteracao_silenciosa_do_pagamento_e_travada_na_base(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    travadas = (
        "UPDATE payments SET amount_cents = 9900 WHERE id = ?",
        "UPDATE payments SET paid_on = '2027-06-01' WHERE id = ?",
        "UPDATE payments SET fraction_id = ? WHERE id = ?",
        "UPDATE payments SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        "UPDATE payments SET form_token = 'forjado' WHERE id = ?",
        "DELETE FROM payments WHERE id = ?",
    )
    for sql in travadas:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, (db.fraction("B")["id"], pagamento["id"]) if "fraction_id" in sql else (pagamento["id"],))

    atual = pagamento_guardado(db, pagamento["id"])
    assert atual["amount_cents"] == 4300
    assert atual["fraction_id"] == db.fraction("A")["id"]
    assert atual["form_token"] == pagamento["form_token"]


def test_reenvio_do_mesmo_formulario_nao_aplica_a_correcao_duas_vezes(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    protecao = protecao_de_edicao(client, pagamento["id"])

    editar_pagamento(client, db, pagamento["id"], valor="60,00", protecao=protecao)
    editar_pagamento(client, db, pagamento["id"], valor="60,00", protecao=protecao)

    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 6000
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 1


def test_formulario_envelhecido_nao_sobrepoe_outra_alteracao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    protecao = protecao_de_edicao(client, pagamento["id"])

    # Entrentanto entra outro pagamento: o estado que este formulário representa mudou.
    registar_pagamento(client, db, "C", "30,00")

    resposta = editar_pagamento(client, db, pagamento["id"], valor="60,00", protecao=protecao)
    assert resposta.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300


# --- Campos de distribuição ausentes nunca limpam afetações -------------------


def _pagamento_distribuido(client, db):
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
    return pagamento


def test_pedido_sem_os_campos_de_afetacao_nao_retira_distribuicao_nem_grava_nada(
    app, client, db
):
    """Um POST manipulado que omite os campos de afetação é recusado.

    O navegador envia sempre os campos (vazios ou preenchidos). A ausência não é
    "utilizador quis retirar": é formulário partido ou pedido forjado, e nunca
    limpa afetações em silêncio.
    """
    pagamento = _pagamento_distribuido(client, db)
    distribuicao_antes = db.rows(
        "SELECT charge_id, amount_cents FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    )

    form = {
        "fracao": db.fraction("A")["id"],
        "valor": "43,00",
        "data": "15/01/2027",
        "meio": "transferencia",
        "referencia": "",
        "source": "",
        "motivo": "tentativa sem distribuição",
        "confirmar_edicao": "on",
        **protecao_de_edicao(client, pagamento["id"]),
    }
    # Nenhum campo `afecao_*` no pedido.
    resposta = client.post(f"/payments/{pagamento['id']}/edit", data=form)

    html = resposta.get_data(as_text=True)
    assert "faltam 2 campo(s) de afetação" in html
    assert "nada foi gravado" in html.lower()
    assert db.rows(
        "SELECT charge_id, amount_cents FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    ) == distribuicao_antes
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0
    assert pagamento_guardado(db, pagamento["id"])["amount_cents"] == 4300


def test_retirar_toda_a_distribuicao_exige_confirmacao_explicita(app, client, db):
    pagamento = _pagamento_distribuido(client, db)
    distribuicao_antes = db.rows(
        "SELECT charge_id, amount_cents FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    )

    resposta = editar_pagamento(
        client,
        db,
        pagamento["id"],
        afetoes={},
        confirmar_retirada=False,
        motivo="tentativa de retirada sem confirmação",
    )
    html = resposta.get_data(as_text=True)
    assert "Retirar toda a distribuição é uma ação explícita" in html
    assert db.rows(
        "SELECT charge_id, amount_cents FROM payment_allocations WHERE payment_id = ?",
        (pagamento["id"],),
    ) == distribuicao_antes
    assert db.scalar("SELECT COUNT(*) FROM payment_revisions") == 0


def test_retirar_toda_a_distribuicao_com_confirmacao_passa_a_credito_integral(app, client, db):
    pagamento = _pagamento_distribuido(client, db)

    editar_pagamento(
        client,
        db,
        pagamento["id"],
        afetoes={},
        motivo="Adiantamento: guardar como crédito até nova regra",
    )

    assert db.scalar(
        "SELECT COUNT(*) FROM payment_allocations WHERE payment_id = ?", (pagamento["id"],)
    ) == 0
    with app.app_context():
        totais = _totais(db.fraction("A")["id"])
    assert totais["credito_cents"] == 4300
    assert totais["divida_cents"] == 4300  # 40,00 + 3,00 emitidos em 01/2027, nada liquidado

    # O que estava guardado fica na correção: lado "antes" com as duas afetações.
    revision_id = db.scalar("SELECT id FROM payment_revisions ORDER BY id DESC LIMIT 1")
    antes = db.rows(
        "SELECT charge_id, amount_cents FROM payment_allocation_revisions "
        "WHERE revision_id = ? AND lado = 'antes' ORDER BY charge_id",
        (revision_id,),
    )
    depois = db.rows(
        "SELECT charge_id FROM payment_allocation_revisions WHERE revision_id = ? AND lado = 'depois'",
        (revision_id,),
    )
    assert len(antes) == 2
    assert depois == []
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1
