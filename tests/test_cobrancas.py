"""Consulta das cobranças, dia de vencimento e classificação de vencidas."""

from __future__ import annotations

import pytest

from conftest import alterar_config, configurar_regras_de_teste, gerar_mes, registar_regra


def _texto(client, caminho: str) -> str:
    return client.get(caminho).get_data(as_text=True)


def test_lista_de_cobrancas_separa_quota_ordinaria_e_reforco(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    html = _texto(client, "/charges")
    assert "01/2027" in html
    assert "340,00 EUR" in html
    assert "24,00 EUR" in html
    assert "364,00 EUR" in html
    assert "16" in html


def test_lista_de_cobrancas_sem_nada_gerado_indica_o_proximo_passo(app, client):
    html = _texto(client, "/charges")
    assert "Ainda não há cobranças geradas" in html


def test_consulta_de_um_mes_mostra_cada_fracao_e_cada_rubrica(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    html = _texto(client, "/charges/2027-01")
    assert html.count("<th scope=\"row\">") >= 16
    assert html.count("Quota ordinária") >= 8
    assert html.count("Reforço do fundo de reserva") >= 8
    assert "40,00 EUR" in html
    assert "45,00 EUR" in html
    assert "3,00 EUR" in html


def test_consulta_de_mes_sem_cobrancas_nao_falha(app, client, db):
    configurar_regras_de_teste(client, db)
    resposta = client.get("/charges/2099-01")

    assert resposta.status_code == 200
    assert "Não há cobranças geradas para 01/2099" in resposta.get_data(as_text=True)


@pytest.mark.parametrize("period", ["lixo", "2027-13", "15/01/2027", ""])
def test_consulta_com_periodo_invalido_devolve_404(app, client, period):
    assert client.get(f"/charges/{period}").status_code == 404


def test_dia_de_vencimento_configurado_fica_na_cobranca(app, client, db):
    alterar_config(client, db, quota_dia_vencimento="15")
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    datas = {c["due_date"] for c in db.charges("2027-01")}
    assert datas == {"2027-01-15"}
    assert "15/01/2027" in _texto(client, "/charges/2027-01")


def test_sem_dia_de_vencimento_a_cobranca_nao_tem_data_e_nao_e_vencida(app, client, db):
    """O dia de vencimento não é inventado: sem ele, não há vencidas."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    assert {c["due_date"] for c in db.charges("2027-01")} == {None}
    html = _texto(client, "/charges/2027-01")
    assert "por confirmar" in html
    assert ">vencida<" not in html, "sem vencimento confirmado nada pode aparecer como vencido"
    assert "nenhuma cobrança é apresentada como vencida" in html  # a nota explica a regra


def test_uma_cobranca_so_e_vencida_depois_da_data_de_vencimento_confirmada(
    app, client, db, monkeypatch
):
    monkeypatch.setattr("app.quotas.today_iso", lambda: "2027-02-01")
    alterar_config(client, db, quota_dia_vencimento="15")
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    gerar_mes(client, "2027-02")

    janeiro = _texto(client, "/charges/2027-01")
    fevereiro = _texto(client, "/charges/2027-02")

    assert janeiro.count(">vencida<") == 16, "01/2027 venceu em 15/01/2027"
    assert fevereiro.count(">vencida<") == 0, "02/2027 ainda não venceu"


def test_pagina_de_regras_mostra_quantas_cobrancas_cada_regra_originou(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    html = _texto(client, "/quotas")
    assert "Cobranças emitidas" in html
    assert html.count(">1<") >= 16


def test_resumo_apresenta_os_totais_emitidos_sem_os_confundir_com_receita(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    html = _texto(client, "/")
    assert "16 cobrança(s) emitida(s)" in html
    assert '<strong class="num">340,00 EUR</strong>' in html
    assert '<strong class="num">24,00 EUR</strong>' in html
    assert "Emitido não é recebido" in html


def test_resumo_sem_cobrancas_nao_mostra_totais_como_se_fossem_saldos(app, client):
    html = _texto(client, "/")
    assert "Nenhuma cobrança gerada ainda" in html
    assert "0,00 EUR" not in html


def test_o_vencimento_apresentado_e_o_que_esta_gravado_nas_cobrancas(app, client, db):
    """Definir o dia depois de gerar não reescreve o que já foi emitido."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    alterar_config(client, db, quota_dia_vencimento="15")
    html = _texto(client, "/charges/2027-01")

    assert "Vencimento deste mês: <strong>por confirmar</strong>" in html
    assert "15/01/2027" not in html
    assert {c["due_date"] for c in db.charges("2027-01")} == {None}


def test_gerar_dois_meses_seguidos_soma_os_dois_sem_duplicar(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    gerar_mes(client, "2027-02")

    html = _texto(client, "/charges")
    assert "01/2027" in html and "02/2027" in html
    total = int(db.scalar("SELECT COALESCE(SUM(amount_cents), 0) FROM charges"))
    assert total == 72800  # 364 EUR x 2 meses
    assert int(db.scalar("SELECT COUNT(*) FROM charges")) == 32


def test_regras_registadas_depois_de_gerar_nao_alteram_o_mes_ja_gerado(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    antes = db.charges("2027-01")

    # Um reforço decidido mais tarde, para outro período, não reescreve 01/2027.
    registar_regra(client, db, ["A"], "reforco_fundo", "8,00", "01/2028", "12/2028")

    assert db.charges("2027-01") == antes
