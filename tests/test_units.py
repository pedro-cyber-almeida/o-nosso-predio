"""Conversões de dinheiro (cêntimos), permilagem (centésimos) e datas."""

from __future__ import annotations

import pytest

from app.units import (
    format_date,
    format_money,
    format_money_input,
    format_permilagem,
    parse_date,
    parse_money,
    parse_permilagem,
    parse_stored_cents,
)

CASOS_MONEY_VALIDOS = [
    ("47", 4700),
    ("47,00", 4700),
    ("47.00", 4700),
    ("1.234,56", 123456),
    ("1 234,56", 123456),
    ("\u00a01 234,56\u00a0", 123456),
    ("0", 0),
    ("0,05", 5),
    ("1234", 123400),
]

CASOS_MONEY_INVALIDOS = ["", "   ", "abc", "47,000", "12,345", "1,2,3", "1.2.3", ",56"]

CASOS_PERMILAGEM_VALIDOS = [
    ("100,00", 10000),
    ("100.00", 10000),
    ("108", 10800),
    ("1000,00", 100000),
    ("0,01", 1),
]

CASOS_PERMILAGEM_INVALIDOS = ["", "abc", "-1", "-100,00", "100,009", "1,2,3"]


@pytest.mark.parametrize("texto, esperado", CASOS_MONEY_VALIDOS)
def test_parse_money_aceita_os_formatos_portugueses(texto, esperado):
    valor = parse_money(texto)
    assert valor == esperado
    assert isinstance(valor, int), "o dinheiro tem de ser cêntimos inteiros"


@pytest.mark.parametrize("texto", CASOS_MONEY_INVALIDOS)
def test_parse_money_rejeita_texto_invalido(texto):
    assert parse_money(texto) is None


@pytest.mark.parametrize("texto, esperado", CASOS_PERMILAGEM_VALIDOS)
def test_parse_permilagem_aceita_virgula_e_ponto_decimais(texto, esperado):
    valor = parse_permilagem(texto)
    assert valor == esperado
    assert isinstance(valor, int), "a permilagem tem de ser centésimos inteiros"


@pytest.mark.parametrize("texto", CASOS_PERMILAGEM_INVALIDOS)
def test_parse_permilagem_rejeita_negativos_e_casas_a_mais(texto):
    assert parse_permilagem(texto) is None


@pytest.mark.parametrize(
    "centimos, esperado",
    [
        (4700, "47,00 EUR"),
        (5, "0,05 EUR"),
        (0, "0,00 EUR"),
        (123456, "1.234,56 EUR"),
        (100000000, "1.000.000,00 EUR"),
        (-4700, "-47,00 EUR"),
        (None, "por confirmar"),
    ],
)
def test_format_money_apresenta_centimos_com_separador_de_milhares(centimos, esperado):
    assert format_money(centimos) == esperado


@pytest.mark.parametrize(
    "valor, esperado",
    [
        (10000, "100,00"),
        (100000, "1000,00"),
        (1, "0,01"),
        (0, "0,00"),
        (None, "por confirmar"),
    ],
)
def test_format_permilagem_devolve_duas_casas_decimais(valor, esperado):
    assert format_permilagem(valor) == esperado


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("01/01/2027", "2027-01-01"),
        ("2027-01-01", "2027-01-01"),
        ("9/3/2027", "2027-03-09"),
        ("31/12/2027", "2027-12-31"),
    ],
)
def test_parse_date_aceita_ddmmaaa_e_iso_e_devolve_iso(texto, esperado):
    assert parse_date(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["", "   ", "abc", "31/02/2027", "00/00/0000", "2027-13-01", "01-01-2027", "2027/01/01"],
)
def test_parse_date_rejeita_datas_inexistentes_ou_fora_do_formato(texto):
    assert parse_date(texto) is None


@pytest.mark.parametrize(
    "iso, esperado",
    [("2027-01-01", "01/01/2027"), ("2026-10-09", "09/10/2026"), ("", "")],
)
def test_format_date_apresenta_dia_mes_ano(iso, esperado):
    assert format_date(iso) == esperado


def test_dinheiro_faz_ronda_completa_texto_centimos_texto():
    centimos = parse_money("1.234,56")
    apresentado = format_money(centimos)
    assert apresentado == "1.234,56 EUR"
    assert parse_money(apresentado.replace(" EUR", "")) == centimos


def test_permilagem_faz_ronda_completa_texto_centimos_texto():
    valor = parse_permilagem("100,00")
    assert format_permilagem(valor) == "100,00"
    assert parse_permilagem(format_permilagem(valor)) == valor


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("1.234", 123400),      # ponto separa milhares: 1234 euros
        ("12.345", 1234500),    # 12 345 euros
        ("-1.234", -123400),    # negativos seguem a mesma regra
    ],
)
def test_ponto_com_tres_casas_e_separador_de_milhares(texto, esperado):
    """Regra documentada: em português o ponto separa milhares, a vírgula separa cêntimos."""
    assert parse_money(texto) == esperado


@pytest.mark.parametrize("texto", ["12,345", "47,000", "100,009"])
def test_tres_casas_decimais_com_virgula_sao_rejeitadas(texto):
    assert parse_money(texto) is None


@pytest.mark.parametrize(
    "guardado, esperado",
    [
        ("123456", 123456),
        ("0", 0),
        ("", None),
        (None, None),
        ("por confirmar", None),
        ("1234,56", None),
        ("-100", None),
        ("  700  ", 700),
    ],
)
def test_parse_stored_cents_rejeita_valores_corrompidos_sem_rebentar(guardado, esperado):
    assert parse_stored_cents(guardado) == esperado


def test_format_money_input_devolve_euros_sem_moeda_para_reenvio():
    assert format_money_input(123456) == "1.234,56"
    assert format_money_input(4700) == "47,00"
    assert format_money_input(None) == ""
    # o texto devolvido volta a ser lido como euros, não como cêntimos
    assert parse_money(format_money_input(123456)) == 123456


def test_zero_nao_e_confundido_com_campo_vazio():
    """`0` é um valor; só texto vazio ou None significa "por confirmar"."""
    assert parse_money(0) == 0
    assert parse_money("0") == 0
    assert parse_permilagem(0) == 0
    assert parse_money(None) is None
    assert parse_money("") is None
