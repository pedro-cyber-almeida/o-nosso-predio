"""Saldos e consulta: dívida, crédito, vencimento, fundo separado e saldo por confirmar."""

from __future__ import annotations

from conftest import (
    alterar_config,
    afetar_pagamento,
    anular_pagamento,
    id_da_cobranca,
    protecao_de,
    registar_pagamento,
    registar_regra,
    regras_e_mes,
    ultimo_pagamento,
)
from conftest import gerar_mes

import pytest


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Estes testes correm como se hoje fosse 01/02/2027."""


TODAS = list("ABCDEFGH")


def test_pagina_de_pagamentos_mostra_as_oito_fracoes_e_o_total_do_predio(app, client, db):
    regras_e_mes(client, db)
    html = client.get("/payments").get_data(as_text=True)

    assert "Saldos por fração" in html
    for code in TODAS:
        assert f">{code}<" in html or f"{code} (" in html
    assert "Prédio" in html
    assert "364,00 EUR" in html  # emitido em 01/2027


def test_divida_e_o_emitido_menos_o_liquidado_por_pagamentos_validos(app, client, db):
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

    html = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "43,00 EUR" in html  # emitido
    assert "liquidada" in html  # as duas cobranças da fração

    detalhe_b = client.get(f"/payments/fraction/{db.fraction('B')['id']}").get_data(as_text=True)
    assert "48,00 EUR" in detalhe_b  # 50 + 5 por liquidar


def test_pagamento_registado_sem_vencimento_nao_cria_divida_vencida(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "10,00")

    html = client.get("/payments").get_data(as_text=True)
    assert "por confirmar" in html
    assert "(vencida)" not in html

    detalhe = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "(vencida)" not in detalhe


def test_sem_vencimento_confirmado_nada_e_apresentado_como_vencido(app, client, db):
    """Mesmas cobranças antigas, sem dia de vencimento: nenhuma está vencida."""
    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "34,00", "09/2026", "12/2026")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "36,00", "09/2026", "12/2026")
    gerar_mes(client, "2026-09")

    lista = client.get("/payments").get_data(as_text=True)
    assert "(vencida)" not in lista
    assert "por confirmar" in lista

    detalhe = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "(vencida)" not in detalhe


def test_divida_vencida_aparece_quando_o_vencimento_confirmado_ja_passou(app, client, db):
    """As mesmas cobranças antigas, agora com dia de vencimento confirmado."""
    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "34,00", "09/2026", "12/2026")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "36,00", "09/2026", "12/2026")
    alterar_config(client, db, quota_dia_vencimento="10")
    gerar_mes(client, "2026-09")

    lista = client.get("/payments").get_data(as_text=True)
    # 4 x 34 + 4 x 36 = 280,00, tudo por liquidar e com vencimento em 10/09/2026.
    assert "280,00 EUR" in lista

    detalhe = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "(vencida)" in detalhe
    assert "34,00 EUR" in detalhe


def test_divida_vencida_sao_somente_as_cobrancas_com_vencimento_passado(
    app, client, db, monkeypatch
):
    """Uma cobrança de 2027 não está vencida em 15/12/2026; a de 2026 está."""
    monkeypatch.setattr("app.payments.today_iso", lambda: "2026-12-15")

    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "30,00", "09/2026", "12/2026")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "32,00", "09/2026", "12/2026")
    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "40,00", "01/2027")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "45,00", "01/2027")
    alterar_config(client, db, quota_dia_vencimento="10")
    gerar_mes(client, "2026-09")
    gerar_mes(client, "2027-01")

    lista = client.get("/payments").get_data(as_text=True)
    # Emitido: 248,00 de 2026 + 340,00 de 2027. Vencida: só os 248,00 de 2026.
    assert "588,00 EUR" in lista
    assert "248,00 EUR" in lista

    detalhe = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "(vencida)" in detalhe


def test_historico_da_fracao_separa_cobrancas_pagamentos_afetoes_divida_e_credito(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "100,00")
    afetar_pagamento(
        client,
        ultimo_pagamento(db)["id"],
        {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"},
    )

    html = client.get(f"/payments/fraction/{db.fraction('A')['id']}").get_data(as_text=True)
    for seccao in (
        "Cobranças e o que está liquidado",
        "Pagamentos desta fração",
        "Crédito por afetar",
        "Dívida",
    ):
        assert seccao in html
    assert "60,00 EUR" in html  # crédito por afetar
    assert "3,00 EUR" in html  # reforço ainda por liquidar


def test_recebimentos_sao_contabilizados_uma_unica_vez(app, client, db):
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

    soma_por_fracao = db.scalar(
        """
        SELECT COALESCE(SUM(amount_cents), 0) FROM payments
        WHERE cancelled_at IS NULL AND fraction_id IN (SELECT id FROM fractions)
        """
    )
    total_geral = db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NULL"
    )
    assert soma_por_fracao == total_geral == 4300

    afetado = db.scalar(
        """
        SELECT COALESCE(SUM(a.amount_cents), 0) FROM payment_allocations a
        JOIN payments p ON p.id = a.payment_id WHERE p.cancelled_at IS NULL
        """
    )
    assert afetado == 4300, "distribuir não pode criar receita nova"


def test_recebimentos_afetos_ao_fundo_sao_mostrados_separadamente_sem_somar_duas_vezes(
    app, client, db
):
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

    html = client.get("/payments").get_data(as_text=True)
    assert "Afeto ao fundo" in html
    assert "o fundo é uma afetação do dinheiro, não uma conta à parte" in html

    recebido = db.scalar(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NULL"
    )
    ao_fundo = db.scalar(
        """
        SELECT COALESCE(SUM(a.amount_cents), 0) FROM payment_allocations a
        JOIN payments p ON p.id = a.payment_id
        JOIN charges c ON c.id = a.charge_id
        WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo'
        """
    )
    assert ao_fundo == 300
    assert ao_fundo < recebido, "o fundo não pode valer mais do que o dinheiro recebido"


def test_credito_por_afetar_nao_e_atribuido_automaticamente_ao_fundo(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    ao_fundo = db.scalar(
        """
        SELECT COALESCE(SUM(a.amount_cents), 0) FROM payment_allocations a
        JOIN payments p ON p.id = a.payment_id
        JOIN charges c ON c.id = a.charge_id
        WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo'
        """
    )
    assert ao_fundo == 0

    html = client.get("/payments").get_data(as_text=True)
    assert "Crédito por afetar não é automaticamente do fundo." in html


def test_sem_saldo_inicial_confirmado_a_pagina_diz_que_o_saldo_esta_por_confirmar(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    html = client.get("/payments").get_data(as_text=True)
    assert "Saldo inicial por confirmar" in html
    assert "Não há um saldo total definitivo" in html
    assert "despesas" in html.lower()


def test_com_saldo_inicial_confirmado_a_conta_ja_inclui_as_despesas(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    alterar_config(client, db, saldo_inicial_caixa_centimos="1.300,00")

    html = client.get("/payments").get_data(as_text=True)
    assert "1.300,00 EUR" in html
    # A fase 4 já não está em falta: as despesas pagas entram na conta no painel.
    assert "pertence à fase 4" not in html
    assert "despesas pagas" in html
    # Não é apresentada uma tesouraria reconciliada: continua a faltar a conta bancária.
    assert "Tesouraria" not in html


def test_resumo_mostra_os_movimentos_financieros_sem_inventar_saldo(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    html = client.get("/").get_data(as_text=True)
    assert "Recebimentos" in html
    assert "43,00 EUR" in html
    # O saldo inicial não confirmado é mostrado como dado em falta, nunca como valor.
    assert "saldo inicial de caixa" in html
    assert "Confirmar em Configuração" in html
    assert '<p class="montante sem-valor">—</p>' in html


def test_consulta_de_um_mes_deixa_de_marcar_vencida_quando_um_pagamento_a_liquidou(
    app, client, db
):
    """A mesma consulta não pode dizer "vencida" numa cobrança já liquidada."""
    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "34,00", "09/2026", "12/2026")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "36,00", "09/2026", "12/2026")
    alterar_config(client, db, quota_dia_vencimento="10")
    gerar_mes(client, "2026-09")

    antes = client.get("/charges/2026-09").get_data(as_text=True)
    assert "vencida" in antes

    registar_pagamento(client, db, "A", "34,00", data="15/01/2027")
    afetar_pagamento(
        client,
        ultimo_pagamento(db)["id"],
        {id_da_cobranca(db, "A", "2026-09", "quota_ordinaria"): "34,00"},
    )

    depois = client.get("/charges/2026-09").get_data(as_text=True)
    assert "liquidada" in depois
    assert "0,00 EUR" in depois  # a cobrança de A já não tem montante em dívida
    # As outras frações continuam por liquidar e vencidas.
    assert "vencida" in depois


def test_resumo_mostra_pagamentos_anulados_em_vez_de_esconder_o_cartao(app, client, db):
    """Um registo anulado continua a ser um registo: mostra-se, não se apaga."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    anular_pagamento(client, ultimo_pagamento(db)["id"], motivo="Movimento inexistente")

    html = client.get("/").get_data(as_text=True)
    assert "Recebimentos" in html
    assert "1 anulado(s)" in html
    assert "0,00 EUR" in html  # recebido efetivo zero, mostrado em vez de esconder o cartão


def test_despesas_ja_fazem_parte_da_fase_atual_mas_so_para_o_administrador(app, client, db):
    regras_e_mes(client, db)
    html = client.get("/payments").get_data(as_text=True)

    # A página de pagamentos aponta para as despesas, que já existem nesta entrega.
    assert 'href="/despesas' in html
    assert "despesas" in html.lower()
    assert "Tesouraria" not in html


def test_fracao_sem_movimentos_nao_mostra_um_saldo_inventado(app, client, db):
    regras_e_mes(client, db)
    html = client.get(f"/payments/fraction/{db.fraction('H')['id']}").get_data(as_text=True)

    assert "Saldo inicial por confirmar" in html
    assert "movimentos conhecidos desta fração" in html
