"""Meses de cobrança: parsing, limites e fronteiras de mês (fase 2)."""

from __future__ import annotations

import pytest

from app.units import (
    current_month,
    format_month,
    month_end,
    month_start,
    next_month,
    parse_month,
    previous_month,
)


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("2027-01", "2027-01"),
        ("01/2027", "2027-01"),
        ("1/2027", "2027-01"),
        ("12/2027", "2027-12"),
        ("  01/2027  ", "2027-01"),
        ("2027-12", "2027-12"),
    ],
)
def test_parse_month_aceita_os_formatos_de_mes(texto, esperado):
    assert parse_month(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "",
        "   ",
        "2027-13",
        "13/2027",
        "0/2027",
        "2027-00",
        "15/01/2027",  # data completa: um campo de mês não é campo de dia
        "2027-01-15",
        "janeiro",
        "2027/01",
        "20-27",
        "abc",
    ],
)
def test_parse_month_recusa_texto_que_nao_e_um_mes(texto):
    assert parse_month(texto) is None


def test_parse_month_limita_o_ano():
    assert parse_month("01/1899") is None
    assert parse_month("01/1900") == "1900-01"
    assert parse_month("12/9999") == "9999-12"
    assert parse_month("12/10000") is None


@pytest.mark.parametrize(
    "period, esperado",
    [
        ("2027-01", "2027-01-31"),
        ("2027-02", "2027-02-28"),
        ("2028-02", "2028-02-29"),  # bissexto
        ("2100-02", "2100-02-28"),  # século não bissexto
        ("2000-02", "2000-02-29"),  # século bissexto
        ("2027-04", "2027-04-30"),
        ("2027-12", "2027-12-31"),
    ],
)
def test_month_end_e_sempre_o_ultimo_dia_do_mes(period, esperado):
    assert month_end(period) == esperado


@pytest.mark.parametrize("period", ["2027-01", "2027-02", "2027-12"])
def test_month_start_e_o_primeiro_dia(period):
    assert month_start(period) == f"{period}-01"


def test_meses_seguintes_e_anteriores_passam_a_fronteira_do_ano():
    assert next_month("2027-12") == "2028-01"
    assert next_month("2027-01") == "2027-02"
    assert previous_month("2028-01") == "2027-12"
    assert previous_month("2027-01") == "2026-12"


def test_meses_fora_do_intervalo_aceite_devolvem_texto_vazio():
    """Não há mês anterior a 01/1900 nem seguinte a 12/9999: as ligações desaparecem."""
    assert previous_month("1900-01") == ""
    assert previous_month("1900-02") == "1900-01"
    assert next_month("9999-12") == ""
    assert next_month("9999-11") == "9999-12"


@pytest.mark.parametrize(
    "period, esperado",
    [("2027-01", "01/2027"), ("2027-12", "12/2027")],
)
def test_format_month_apresenta_mes_ano(period, esperado):
    assert format_month(period) == esperado


def test_current_month_tem_a_forma_de_um_mes():
    atual = current_month()
    assert parse_month(atual) == atual
