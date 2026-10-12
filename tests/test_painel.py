"""Painel por perfil, tema persistente e gráfico só com dados reais (entrega 1)."""

from __future__ import annotations

import pytest

from conftest import (
    afetar_pagamento,
    criar_condomino,
    entrar_condomino,
    id_da_cobranca,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje, monkeypatch):
    """O painel calcula o ano padrão e os vencimentos com o mesmo dia fixo."""
    monkeypatch.setattr("app.dashboard.today_iso", lambda: hoje)


# ---------------------------------------------------------------- tema ----


def test_base_aplica_o_tema_guardado_antes_da_folha_de_estilos(client):
    html = client.get("/").get_data(as_text=True)
    script = html.index('localStorage.getItem("condominio-tema")')
    folha = html.index('rel="stylesheet"')
    assert script < folha, "o tema tem de ser aplicado antes do CSS: sem clarão ao carregar"
    assert 'data-tema="sistema"' in html
    for opcao in ("Claro", "Escuro", "Sistema"):
        assert opcao in html


def test_preferencia_do_tema_fica_guardada_na_pagina(client):
    html = client.get("/").get_data(as_text=True)
    assert 'localStorage.setItem("condominio-tema"' in html


# ------------------------------------------------- painel do administrador ----


def test_painel_vazio_nao_mostra_numeros_como_se_fossem_saldos(client):
    html = client.get("/").get_data(as_text=True)
    assert "0,00 EUR" not in html
    assert "saldo inicial por confirmar" in html
    assert "Sem recebimentos registados" in html
    # A casca da interface usa ícones SVG; o que não pode existir sem dados é o gráfico.
    assert 'class="barra"' not in html, "sem dados reais não pode haver gráfico"
    assert "Recebimentos por mês em" not in html, "sem dados reais não pode haver gráfico"


def test_painel_mostra_recebimentos_divida_credito_e_fundo_com_dados_reais(app, client, db):
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

    html = client.get("/").get_data(as_text=True)
    assert "43,00 EUR" in html  # recebimentos
    assert "321,00 EUR" in html  # dívida do prédio: 364,00 emitidos, 43,00 liquidados
    assert "3,00 EUR" in html  # crédito por afetar
    assert "Fundo de reserva" in html
    assert "não é somado outra vez à liquidez" in html
    assert "saldo inicial por confirmar" in html


def test_grafico_so_aparece_com_recebimentos_e_mostra_o_ano_escolhido(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")

    html = client.get("/").get_data(as_text=True)
    assert "<svg" in html
    assert "Recebimentos por mês em 2027" in html
    assert 'class="barra"' in html

    # Outro ano sem dados não entra na seleção: só há anos com recebimentos.
    html_2028 = client.get("/?ano=2028").get_data(as_text=True)
    assert "Recebimentos por mês em 2027" in html_2028  # mantém o ano que tem dados


# ---------------------------------------------------- painel do condómino ----


def test_painel_do_condomino_mostra_apenas_as_suas_fracoes(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    html = condomino.get("/").get_data(as_text=True)
    assert "Por pagar" in html
    assert "43,00 EUR" in html  # ainda nada distribuído: a cobrança de A está inteira
    assert "Próximos vencimentos" in html
    assert "Painel do administrador" not in html
    # Nada de outras frações: B não aparece em lado nenhum.
    assert "Fração B" not in html and ">B<" not in html


def test_condomino_sem_associacao_ve_estado_vazio_sem_inventar_nada(app, client, db):
    criar_condomino(app, db, "condomino.solto")
    condomino = entrar_condomino(app, "condomino.solto")

    html = condomino.get("/").get_data(as_text=True)
    assert "Ainda não estás associado a nenhuma fração" in html
    assert "0,00 EUR" not in html
