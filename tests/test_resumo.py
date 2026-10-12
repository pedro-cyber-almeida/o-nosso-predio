"""Página de resumo: totais e valores por confirmar."""

from __future__ import annotations


def test_resumo_mostra_as_oito_fracoes_e_a_permilagem_total_correta(app, client):
    resposta = client.get("/")

    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert "8 frações registadas" in html
    assert "Permilagem total: <strong>1000,00</strong>" in html
    assert "Soma completa: 1000,00" in html


def test_resumo_apresenta_por_confirmar_enquanto_nada_foi_confirmado(app, client):
    html = client.get("/").get_data(as_text=True)

    # Os dados de configuração por confirmar aparecem reunidos num único aviso,
    # como o que falta, e nunca como valores do prédio.
    for item in (
        "saldo inicial de caixa",
        "saldo inicial do fundo",
        "dia de vencimento das quotas",
        "data de início da gestão",
    ):
        assert item in html, f"falta {item} no aviso de configuração em falta"
    assert "Confirmar em Configuração" in html
    assert "por confirmar" in html
    assert "0,00 EUR" not in html


def test_resumo_avisa_quando_a_soma_das_permilagens_deixa_de_ser_100000(
    app, client, db
):
    db.execute("UPDATE fractions SET permilagem = 9000 WHERE code = 'A'")

    html = client.get("/").get_data(as_text=True)

    assert "990,00" in html
    assert "Soma incompleta: falta 10,00" in html