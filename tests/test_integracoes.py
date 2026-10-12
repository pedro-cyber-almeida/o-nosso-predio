"""Entrega 4: Definições → Integrações — segredos protegidos, nada fixado no código."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import criar_condomino, entrar_condomino, protecao_de


# ---------------------------------------------------------------- permissões --


def test_pagina_de_integracoes_e_exclusiva_do_administrador(app, client, db):
    criar_condomino(app, db, "condomino.leitor", ("A",))
    condomino = entrar_condomino(app, "condomino.leitor")

    for caminho in ("/definicoes/integracoes", "/email", "/documentos"):
        resposta = condomino.get(caminho)
        assert resposta.status_code == 302, caminho
        assert resposta.headers["Location"].startswith("/")

    resposta = cliente_anonimo_get(app, "/definicoes/integracoes")
    assert resposta.status_code == 302


def cliente_anonimo_get(app, caminho):
    with app.test_client() as anonimo:
        return anonimo.get(caminho)


# ---------------------------------------------------------------- segredos ----


def _guardar(client, **campos):
    return client.post(
        "/definicoes/integracoes",
        data={**protecao_de(client, "/definicoes/integracoes"), **campos},
    )


def test_segredos_guardados_nao_sao_devolvidos_ao_navegador(app, client, db):
    _guardar(
        client,
        gmail_client_id="cliente-123.apps.googleusercontent.com",
        gmail_client_secret="segredo-super-secreto",
        ia_base_url="http://127.0.0.1:11435/v1",
        ia_chave_api="chave-secreta-da-ia",
        ia_modelo="modelo-local",
        ia_timeout_segundos="45",
    )

    html = client.get("/definicoes/integracoes").get_data(as_text=True)
    # Os valores nunca voltam: nem em campos, nem em texto.
    assert "segredo-super-secreto" not in html
    assert "chave-secreta-da-ia" not in html
    assert "guardado" in html  # mostra-se o estado, não o valor
    assert "http://127.0.0.1:11435/v1" in html  # o endereço base não é segredo


def test_campo_de_segredo_em_branco_mantem_o_que_esta_guardado(app, client, db):
    from app.integracoes import obter_integracao

    _guardar(client, gmail_client_secret="segredo-1", ia_modelo="modelo-a")
    _guardar(client, ia_modelo="modelo-b")  # segredo em branco: manter

    with app.app_context():
        assert obter_integracao("gmail_client_secret") == "segredo-1"
        assert obter_integracao("ia_modelo") == "modelo-b"


def test_caixa_apagar_remove_o_segredo(app, client, db):
    from app.integracoes import obter_integracao

    _guardar(client, ia_chave_api="chave-para-apagar")
    _guardar(client, apagar_ia_chave="on")

    with app.app_context():
        assert obter_integracao("ia_chave_api") == ""


def test_ia_comeca_desativada_e_endereco_invalido_e_recusado(app, client, db):
    from app.integracoes import ia_ativa, obter_integracao

    with app.app_context():
        assert not ia_ativa()

    _guardar(client, ia_base_url="ftp://nao-presta", ia_modelo="m", ia_ativa="on")
    with app.app_context():
        assert obter_integracao("ia_base_url") == ""
        assert not ia_ativa()

    _guardar(client, ia_base_url="http://127.0.0.1:11435/v1", ia_modelo="m", ia_ativa="on")
    with app.app_context():
        assert ia_ativa()


def test_tempo_limite_fora_do_intervalo_e_recusado(app, client, db):
    from app.integracoes import obter_integracao

    _guardar(client, ia_timeout_segundos="999")
    with app.app_context():
        assert obter_integracao("ia_timeout_segundos") == ""

    _guardar(client, ia_timeout_segundos="45")
    with app.app_context():
        assert obter_integracao("ia_timeout_segundos") == "45"


def test_uri_de_redirecionamento_acompanha_a_instancia_e_nada_esta_fixado_no_codigo(app, client):
    html = client.get("/definicoes/integracoes").get_data(as_text=True)
    # O endereço de redirecionamento vem do pedido, não do código.
    assert "http://localhost/email/oauth/callback" in html

    # Credenciais, endereços de servidores pessoais e modelos não podem estar
    # escritos no código: a única fonte é esta página.
    app_dir = Path(__file__).resolve().parent.parent / "app"
    fontes = "\n".join(
        ficheiro.read_text(encoding="utf-8")
        for ficheiro in app_dir.rglob("*")
        if ficheiro.suffix in (".py", ".html") and ficheiro.is_file()
    )
    # Endereços internos de servidores pessoais e modelos do utilizador não
    # aparecem no código: só existem se ele os escrever nesta página.
    assert not re.search(r"\b10\.\d+\.\d+\.\d+\b|\b192\.168\.\d+\.\d+\b", fontes), (
        "nenhum endereço interno pode estar escrito no código"
    )
    assert "apps.googleusercontent.com" not in fontes
    assert "qwen" not in fontes.lower()
