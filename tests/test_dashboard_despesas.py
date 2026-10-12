"""Entrega 3: dashboard financeiro do administrador — período, saldos e gráficos."""

from __future__ import annotations

import pytest

from conftest import (
    alterar_config,
    afetar_pagamento,
    anular_pagamento,
    categoria_de_trabalho,
    gerar_mes,
    id_da_cobranca,
    pagar_despesa,
    registar_despesa,
    registar_pagamento,
    regras_e_mes,
    ultima_despesa,
    ultimo_pagamento,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """O painel calcula tudo como se hoje fosse 01/02/2027."""


def test_dashboard_separa_fluxo_do_periodo_de_saldos_acumulados(app, client, db):
    """Um pagamento de 2026 não entra nos recebimentos do ano de 2027."""
    registar_pagamento(client, db, "A", "30,00", data="10/11/2026")
    registar_pagamento(client, db, "A", "43,00", data="15/01/2027")

    html = client.get("/?ano=2027").get_data(as_text=True)
    assert "Recebimentos em 2027" in html
    assert "43,00 EUR" in html  # fluxo do período
    assert "73,00 EUR" in html  # acumulado até 01/02/2027

    html_2026 = client.get("/?ano=2026").get_data(as_text=True)
    assert "Recebimentos em 2026" in html_2026
    assert "30,00 EUR" in html_2026  # fluxo do período
    assert "73,00 EUR" not in html_2026  # à data limite de 2026 o de 2027 ainda não entrou


def test_despesas_registadas_nao_aparecem_como_pagas(app, client, db):
    registar_despesa(client, db, valor="120,00", data="15/01/2027")

    html = client.get("/").get_data(as_text=True)
    assert "Despesas pagas em 2027" in html
    assert "Sem pagamentos de despesas registados" in html
    assert "Despesas por pagar até 01/02/2027" in html
    assert "120,00 EUR" in html


def test_despesas_por_pagar_sao_o_registado_menos_o_pago_ate_a_data(app, client, db):
    registar_despesa(client, db, valor="120,00", data="15/01/2027")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "45,00")

    html = client.get("/").get_data(as_text=True)
    assert "75,00 EUR" in html  # em falta à data limite
    assert "45,00 EUR" in html  # pago no período


def test_liquidez_desconta_somente_pagamentos_de_despesas(app, client, db):
    alterar_config(client, db, saldo_inicial_caixa_centimos="1.000,00")
    registar_pagamento(client, db, "A", "43,00", data="15/01/2027")
    registar_despesa(client, db, valor="120,00", data="10/01/2027")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "80,00")

    html = client.get("/").get_data(as_text=True)
    # 1.000,00 + 43,00 recebidos - 80,00 pagos em despesas.
    assert "963,00 EUR" in html
    assert "Não é saldo bancário reconciliado" in html


def test_sem_movimentos_nao_ha_variacao_conhecida_mas_continua_por_confirmar(app, client, db):
    registar_despesa(client, db, valor="120,00")

    html = client.get("/").get_data(as_text=True)
    assert "saldo inicial por confirmar" in html
    # A despesa registada não é saída: não existe variação conhecida só por registar despesa.
    assert "Movimentos registados" not in html


def test_fundo_de_reserva_decompoe_se_e_nao_e_somado_outra_vez_a_liquidez(app, client, db):
    """Reforços e despesas pagas pelo fundo aparecem à parte, dentro da caixa."""
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
    registar_despesa(
        client, db, descricao="Seguro do prédio", valor="30,00",
        classificacao="fundo_reserva",
    )
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "30,00")

    html = client.get("/").get_data(as_text=True)
    assert "Fundo de reserva até 01/02/2027" in html
    assert "3,00 EUR" in html  # reforços
    assert "30,00 EUR" in html  # despesas pagas pelo fundo
    assert "não é somado outra vez à liquidez" in html
    # Liquidez: 43,00 recebidos - 30,00 pagos = 13,00 (o fundo não muda a soma).
    assert "13,00 EUR" in html


def test_grafico_mostra_recebimentos_e_pagamentos_de_despesas_por_mes(app, client, db):
    registar_pagamento(client, db, "A", "43,00", data="15/01/2027")
    registar_despesa(client, db, valor="120,00", data="10/01/2027")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "80,00", data="20/01/2027")

    html = client.get("/").get_data(as_text=True)
    assert "Recebimentos e pagamentos de despesas por mês em 2027" in html
    assert 'class="barra"' in html
    assert 'class="barra-pagamento"' in html
    assert "Pagamentos de despesas" in html  # legenda
    # Fevereiro continua sem movimentos: barra vazia, não valor inventado.
    assert 'class="barra-vazia"' in html


def test_despesas_pagas_por_categoria_com_a_base_identificada(app, client, db):
    limpeza = categoria_de_trabalho(client, db, "Limpeza")
    jardim = categoria_de_trabalho(client, db, "Jardim")
    registar_despesa(client, db, descricao="Hall", valor="45,00", categoria=limpeza)
    registar_despesa(client, db, descricao="Árvores", valor="30,00", categoria=jardim)
    for despesa in db.expenses():
        pagar_despesa(client, db, despesa["id"], "10,00")

    html = client.get("/").get_data(as_text=True)
    assert "Despesas pagas por categoria em 2027" in html
    assert "Limpeza" in html and "Jardim" in html
    assert "Base: pagamentos válidos de despesas com data em 2027" in html


def test_evolucao_da_liquidez_so_com_saldo_inicial_e_data_de_gestao_confirmados(app, client, db):
    registar_pagamento(client, db, "A", "43,00", data="15/01/2027")
    html = client.get("/").get_data(as_text=True)
    assert "Evolução da liquidez registada" not in html

    alterar_config(
        client, db,
        saldo_inicial_caixa_centimos="1.000,00",
        gestao_data_inicio="01/10/2026",
    )
    html = client.get("/").get_data(as_text=True)
    assert "Evolução da liquidez registada" in html
    assert "não um saldo bancário reconciliado" in html
    assert "1.043,00 EUR" in html  # janeiro fecha com o pagamento de 43,00


def test_anos_selecionaveis_incluem_anos_so_com_pagamentos_de_despesas(app, client, db):
    registar_despesa(client, db, valor="120,00", data="10/03/2026")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "40,00", data="20/03/2026")

    html = client.get("/").get_data(as_text=True)
    assert "<option value=\"2026\"" in html  # 2026 tem pagamentos de despesa válidos
    assert "Total de 2026" in html


def test_dashboard_nao_confunde_despesa_registada_com_saida(app, client, db):
    """Registar despesa não altera a liquidez; pagar altera."""
    alterar_config(client, db, saldo_inicial_caixa_centimos="300,00")
    registar_despesa(client, db, valor="120,00")
    html = client.get("/").get_data(as_text=True)
    assert "300,00 EUR" in html  # liquidez inalterada por registar

    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "120,00")
    html = client.get("/").get_data(as_text=True)
    assert "180,00 EUR" in html  # a saída só aconteceu com o pagamento


def test_pagamentos_de_despesa_anulados_deixam_de_contar_no_painel(app, client, db):
    from conftest import anular_pagamento_de_despesa

    alterar_config(client, db, saldo_inicial_caixa_centimos="300,00")
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "120,00")
    from conftest import ultimo_pagamento_de_despesa

    anular_pagamento_de_despesa(client, ultimo_pagamento_de_despesa(db)["id"], motivo="Engano")

    html = client.get("/").get_data(as_text=True)
    assert "300,00 EUR" in html  # anulado: a saída deixou de contar
    assert "120,00 EUR" in html  # mas o em falta da despesa voltou
