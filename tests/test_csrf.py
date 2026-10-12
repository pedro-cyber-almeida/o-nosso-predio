"""CSRF e revisão de concorrência: POST sem proteção válida é recusado antes de chegar à vista."""

from __future__ import annotations

import re

import pytest

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')

ENDPOINTS_POST = [
    "/config",
    "/fractions/new",
    "/fractions/permilagens",
    "/fractions/1/edit",
    "/fractions/1/delete",
    "/fractions/1/owners",
    "/fractions/1/owners/1/close",
]


def _token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "sem token CSRF na página"
    return match.group(1)


def _revisao(html: str, campo: str) -> str:
    match = re.search(rf'name="{campo}" value="([^"]*)"', html)
    assert match, f"sem {campo} na página"
    return match.group(1)


@pytest.mark.parametrize("caminho", ENDPOINTS_POST)
def test_post_sem_token_csrf_devolve_400(client, caminho):
    resposta = client.post(caminho, data={"code": "Z", "name": "Qualquer Nome"})
    assert resposta.status_code == 400


@pytest.mark.parametrize("caminho", ENDPOINTS_POST)
def test_post_com_token_errado_devolve_400(client, csrf, caminho):
    token_valido = csrf("/config")
    resposta = client.post(caminho, data={"code": "Z", "csrf_token": token_valido + "x"})
    assert resposta.status_code == 400


def test_post_sem_token_nao_altera_a_base(client, db):
    antes = db.permilagens()
    client.post(
        "/fractions/permilagens",
        data={f"permilagem_{fracao['id']}": "1,00" for fracao in db.fractions()},
    )
    assert db.permilagens() == antes


def test_post_com_token_extraido_do_get_correspondente_e_aceite(client, db, csrf):
    html = client.get("/config").get_data(as_text=True)
    resposta = client.post(
        "/config",
        data={
            "condominio_nome": "O Nosso Prédio",
            "condominio_morada": "",
            "dividas_abertura_notas": "",
            "gestao_data_inicio": "",
            "quota_dia_vencimento": "10",
            "saldo_inicial_caixa_centimos": "",
            "saldo_inicial_fundo_centimos": "",
            "csrf_token": csrf("/config"),
            "revisao_configuracao": _revisao(html, "revisao_configuracao"),
        },
    )

    assert resposta.status_code == 302
    assert db.settings()["quota_dia_vencimento"] == "10"


def test_get_nao_exige_token(client):
    for caminho in ("/fractions", "/fractions/new", "/fractions/permilagens", "/config"):
        assert client.get(caminho).status_code == 200


def test_paginas_incluem_token_csrf_nos_formularios(client):
    for caminho in ("/fractions/new", "/fractions/permilagens", "/config", "/fractions/1"):
        html = client.get(caminho).get_data(as_text=True)
        assert 'name="csrf_token" value="' in html


def test_paginas_incluem_revisao_de_concorrencia_nos_formularios(client):
    for caminho in ("/fractions/new", "/fractions/permilagens", "/config", "/fractions/1"):
        html = client.get(caminho).get_data(as_text=True)
        assert 'name="revisao_' in html, f"{caminho} sem revisão de concorrência"


def test_post_sem_revisao_e_recusado_e_nada_e_guardado(client, db):
    """A proteção de concorrência não é opcional: sem revisão nada é gravado."""
    antes = db.settings()
    token = _token(client.get("/config").get_data(as_text=True))

    resposta = client.post(
        "/config",
        data={
            "condominio_nome": "O Nosso Prédio",
            "condominio_morada": "Rua Sem Revisão",
            "dividas_abertura_notas": "",
            "gestao_data_inicio": "",
            "quota_dia_vencimento": "10",
            "saldo_inicial_caixa_centimos": "",
            "saldo_inicial_fundo_centimos": "",
            "csrf_token": token,
        },
    )

    assert resposta.status_code == 302
    assert db.settings() == antes
    assert "desatualizada" in client.get("/config").get_data(as_text=True)
