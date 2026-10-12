"""Repartição de permilagens: soma exata, confirmação explícita, rasto e concorrência."""

from __future__ import annotations

import re

import pytest

from app.units import format_permilagem

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')


def _protecao(client) -> dict[str, str]:
    html = client.get("/fractions/permilagens").get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, "sem token CSRF na página de permilagens"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, "sem revisão de concorrência na página de permilagens"
    return {"csrf_token": match.group(1), **campos}


def formulario_atual(client, db, **extra) -> dict:
    """Formulário com os valores atuais, pronto a modificar por fração."""
    form = {
        f"permilagem_{fracao['id']}": format_permilagem(fracao["permilagem"])
        for fracao in db.fractions()
    }
    form.update(_protecao(client))
    form["motivo"] = "Retificação de teste"
    form.update(extra)
    return form


def test_soma_diferente_de_100000_e_rejeitada_e_nada_muda(app, client, db):
    antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "200,00"  # soma passa a 109.122
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/permilagens", data=form)

    assert resposta.status_code == 200
    assert db.permilagens() == antes, "uma rejeição não pode deixar alterações parciais"
    assert db.total_permilagem() == 100000
    html = resposta.get_data(as_text=True)
    assert "Nenhuma alteração foi guardada" in html


@pytest.mark.parametrize("codigo", ["A", "D", "H"])
def test_permilagem_invalida_rejeita_todas_as_alteracoes(app, client, db, codigo):
    antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction(codigo)['id']}"] = "abc"
    form[f"permilagem_{db.fraction('B')['id']}"] = "140,00"  # tentativa de alteração
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/permilagens", data=form)

    assert resposta.status_code == 200
    assert db.permilagens() == antes


@pytest.mark.parametrize("valor", ["0", "0,00", "-10,00", "1000,01"])
def test_permilagem_fora_do_intervalo_valido_e_rejeitada(app, client, db, valor):
    antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = valor
    form["confirmar_reparticao"] = "on"

    client.post("/fractions/permilagens", data=form)

    assert db.permilagens() == antes


def test_permilagens_validas_sao_guardadas_e_a_soma_continua_100000(app, client, db):
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "110,00"  # +1000
    form[f"permilagem_{db.fraction('H')['id']}"] = "90,00"  # -1000
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Retificação aprovada em assembleia"

    resposta = client.post("/fractions/permilagens", data=form)

    assert resposta.status_code == 302
    valores = db.permilagens()
    assert valores["A"] == 11000
    assert valores["H"] == 9000
    assert sum(valores.values()) == 100000


def test_alteracao_de_permilagem_fica_registada_com_motivo(app, client, db):
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "110,00"
    form[f"permilagem_{db.fraction('H')['id']}"] = "90,00"
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Retificação aprovada em assembleia"

    client.post("/fractions/permilagens", data=form)

    rasto = db._query(
        "SELECT fractions.code AS code, permilagem_anterior, permilagem_nova, motivo "
        "FROM permilagem_history JOIN fractions ON fractions.id = permilagem_history.fraction_id "
        "ORDER BY fractions.code"
    )
    assert [(r["code"], r["permilagem_anterior"], r["permilagem_nova"], r["motivo"]) for r in rasto] == [
        ("A", 10000, 11000, "Retificação aprovada em assembleia"),
        ("H", 10000, 9000, "Retificação aprovada em assembleia"),
    ]


def test_redistribuir_sem_confirmacao_explícita_e_recusado(app, client, db):
    """Não há redistribuição automática: mudar permilagens exige confirmação do administrador."""
    antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "110,00"
    form[f"permilagem_{db.fraction('H')['id']}"] = "90,00"
    # sem confirmar_reparticao

    resposta = client.post("/fractions/permilagens", data=form)

    assert resposta.status_code == 200
    assert db.permilagens() == antes, "mudar permilagens sem confirmação alterou as permilagens"
    html = resposta.get_data(as_text=True)
    assert "exige confirmação explícita" in html
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 0


def test_redistribuir_sem_motivo_escrito_e_recusado(app, client, db):
    """O motivo fica no rasto das permilagens: sem ele a alteração não é aceite."""
    antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "110,00"
    form[f"permilagem_{db.fraction('H')['id']}"] = "90,00"
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "   "

    resposta = client.post("/fractions/permilagens", data=form)

    assert resposta.status_code == 200
    assert db.permilagens() == antes, "redistribuir sem motivo alterou as permilagens"
    assert "motivo escrito" in resposta.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 0


def test_guardar_sem_mudar_valores_nao_exige_confirmacao_nao_cria_rasto(app, client, db):
    ids_antes = [fracao["id"] for fracao in db.fractions()]
    estado_antes = {fracao["code"]: fracao["active"] for fracao in db.fractions()}
    permilagens_antes = db.permilagens()

    client.post("/fractions/permilagens", data=formulario_atual(client, db))

    assert [fracao["id"] for fracao in db.fractions()] == ids_antes
    assert {fracao["code"]: fracao["active"] for fracao in db.fractions()} == estado_antes
    assert db.permilagens() == permilagens_antes
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 0


def test_formulario_de_permilagens_desatualizado_nao_altera_nada(app, client, db):
    """Duas versões do mesmo formulário: a segunda é recusada e nada é gravado."""
    protecao = _protecao(client)

    primeira = formulario_atual(client, db)
    primeira[f"permilagem_{db.fraction('A')['id']}"] = "110,00"
    primeira[f"permilagem_{db.fraction('H')['id']}"] = "90,00"
    primeira["confirmar_reparticao"] = "on"
    assert client.post("/fractions/permilagens", data=primeira).status_code == 302
    guardado = db.permilagens()

    segunda = formulario_atual(client, db)
    segunda[f"permilagem_{db.fraction('A')['id']}"] = "150,00"
    segunda[f"permilagem_{db.fraction('H')['id']}"] = "91,53"
    segunda["confirmar_reparticao"] = "on"
    segunda.update(protecao)  # revisão antiga, de antes da primeira gravação

    resposta = client.post("/fractions/permilagens", data=segunda)

    assert resposta.status_code == 302
    assert db.permilagens() == guardado, "um formulário desatualizado alterou as permilagens"
    assert "desatualizada" in client.get("/fractions/permilagens").get_data(as_text=True)


def test_pagina_mostra_a_soma_atual(app, client):
    html = client.get("/fractions/permilagens").get_data(as_text=True)
    assert "1000,00" in html
    assert "completa" in html


def test_pagina_mostra_as_permilagens_como_estao_guardadas(app, client, db):
    """A página mostra as permilagens tal como estão: nada é apresentado como referência externa."""
    html = client.get("/fractions/permilagens").get_data(as_text=True)
    for valor in ("100,00", "130,00", "190,00"):
        assert valor in html
    assert "Ata" not in html
    assert "não são redistribuídas automaticamente" in html
