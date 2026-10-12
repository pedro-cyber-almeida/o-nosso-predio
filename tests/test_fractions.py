"""Ciclo de vida das frações: criar, validar código, desativar, eliminar e proteger o histórico."""

from __future__ import annotations

import re

import pytest

from app.units import format_permilagem
from conftest import ADMIN_TESTE, entrar

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')


def _protecao(client, path: str) -> dict[str, str]:
    """Token CSRF e revisões de concorrência tal como a página os apresenta."""
    html = client.get(path).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {path}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisão de concorrência em {path}"
    return {"csrf_token": match.group(1), **campos}


def formulario_atual(client, db, path: str = "/fractions/new", **extra) -> dict:
    form = {
        f"permilagem_{fracao['id']}": format_permilagem(fracao["permilagem"])
        for fracao in db.fractions()
    }
    form.update(_protecao(client, path))
    form["motivo"] = "Repartição de teste"
    form.update(extra)
    return form


def criar_fracao_i(client, db, permilagem: int = 3275, de_onde: str = "H") -> int:
    """Criar a fração I com repartição escrita e confirmada; devolve o seu id.

    Os testes de eliminação usam uma fração criada depois das frações de teste.
    """
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction(de_onde)['id']}"] = format_permilagem(
        db.permilagens()[de_onde] - permilagem
    )
    form["code"] = "I"
    form["permilagem"] = format_permilagem(permilagem)
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Nova fração de teste"
    assert client.post("/fractions/new", data=form).status_code == 302
    return db.fraction("I")["id"]


def test_criar_fracao_nova_e_aceite_quando_a_soma_continua_100000(app, client, db):
    """Criar a fração I exige repartição escrita e confirmada na mesma submissão."""
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('H')['id']}"] = "67,25"  # H liberta 32,75
    form["code"] = "I"
    form["permilagem"] = "32,75"
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert db.permilagens()["I"] == 3275
    assert db.permilagens()["H"] == 6725
    assert db.total_permilagem() == 100000


def test_criar_fracao_nao_exige_eliminar_uma_fracao_existente(app, client, db):
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('A')['id']}"] = "50,00"
    form["code"] = "I"
    form["permilagem"] = "50,00"
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert len(db.fractions()) == 9
    assert db.total_permilagem() == 100000


def test_criar_fracao_sem_confirmacao_nao_altera_as_permilagens_guardadas(app, client, db):
    """Sem confirmação explícita, nem a fração é criada nem as permilagens mudam."""
    permilagens_antes = db.permilagens()
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('H')['id']}"] = "67,25"
    form["code"] = "I"
    form["permilagem"] = "32,75"
    # sem confirmar_reparticao

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert "I" not in db.permilagens()
    assert db.permilagens() == permilagens_antes, "criar uma fração alterou as permilagens sem confirmação"
    assert "confirmação explícita" in client.get("/fractions/new").get_data(as_text=True)


def test_criar_fracao_que_faz_a_soma_exceder_100000_nao_guarda_nada(app, client, db):
    permilagens_antes = db.permilagens()
    form = formulario_atual(client, db)
    form["code"] = "I"
    form["permilagem"] = "10,00"
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert "I" not in db.permilagens()
    assert db.permilagens() == permilagens_antes, "uma repartição inválida não pode alterar nada"
    assert "não pode exceder" in client.get("/fractions/new").get_data(as_text=True)


def test_formulario_de_nova_fracao_desatualizado_e_recusado(app, client, db):
    """Duas versões do mesmo formulário: a segunda não cria a fração nem mexe nas permilagens."""
    permilagens_antes = db.permilagens()
    protecao = _protecao(client, "/fractions/new")

    primeira = formulario_atual(client, db)
    primeira[f"permilagem_{db.fraction('H')['id']}"] = "67,25"
    primeira["code"] = "I"
    primeira["permilagem"] = "32,75"
    primeira["confirmar_reparticao"] = "on"
    assert client.post("/fractions/new", data=primeira).status_code == 302
    guardado = db.permilagens()

    segunda = formulario_atual(client, db)
    segunda[f"permilagem_{db.fraction('G')['id']}"] = "50,00"
    segunda["code"] = "J"
    segunda["permilagem"] = "80,71"
    segunda["confirmar_reparticao"] = "on"
    segunda.update(protecao)  # revisão de antes da primeira gravação

    resposta = client.post("/fractions/new", data=segunda)

    assert resposta.status_code == 302
    assert "J" not in db.permilagens()
    assert db.permilagens() == guardado
    assert "desatualizada" in client.get("/fractions/new").get_data(as_text=True)


def test_rejeitar_codigo_de_fracao_duplicado(app, client, db):
    form = formulario_atual(client, db)
    form["code"] = "A"
    form["permilagem"] = "10,00"

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert "já existe" in client.get("/fractions/new").get_data(as_text=True)
    assert len(db.fractions()) == 8


@pytest.mark.parametrize("codigo", ["", "   ", "ABCDEFGHI", "A B", "A/B", "À"])
def test_rejeitar_codigo_de_fracao_invalido(app, client, db, codigo):
    form = formulario_atual(client, db)
    form["code"] = codigo
    form["permilagem"] = "10,00"
    client.post("/fractions/new", data=form)
    assert len(db.fractions()) == 8


@pytest.mark.parametrize("codigo", ["B1", "12", "RC"])
def test_aceitar_codigo_curto_de_letras_e_numeros(app, client, db, codigo):
    form = formulario_atual(client, db)
    form[f"permilagem_{db.fraction('H')['id']}"] = "90,00"  # H liberta 10,00
    form["code"] = codigo
    form["permilagem"] = "10,00"
    form["confirmar_reparticao"] = "on"
    resposta = client.post("/fractions/new", data=form)
    assert resposta.status_code == 302
    assert codigo in db.permilagens()


@pytest.mark.parametrize("codigo", ["a", "h"])
def test_aceitar_letra_minuscula_normalizada_para_maiuscula_e_depois_rejeitada_por_duplicado(
    app, client, db, codigo
):
    form = formulario_atual(client, db)
    form["code"] = codigo
    form["permilagem"] = "10,00"
    client.post("/fractions/new", data=form)
    assert len(db.fractions()) == 8


@pytest.mark.parametrize("permilagem", ["", "abc", "0", "0,00", "-10,00"])
def test_rejeitar_permilagem_invalida_ou_nao_positiva_na_nova_fracao(app, client, db, permilagem):
    form = formulario_atual(client, db)
    form["code"] = "I"
    form["permilagem"] = permilagem
    client.post("/fractions/new", data=form)
    assert "I" not in db.permilagens()


def test_rejeitar_nova_fracao_que_faz_a_soma_deixar_de_ser_100000(app, client, db):
    form = formulario_atual(client, db)
    form["code"] = "I"
    form["permilagem"] = "10,00"
    form["confirmar_reparticao"] = "on"

    resposta = client.post("/fractions/new", data=form)

    assert resposta.status_code == 302
    assert "1000,00" in client.get("/fractions/new").get_data(as_text=True)
    assert "I" not in db.permilagens()
    assert db.total_permilagem() == 100000


def test_eliminar_fracao_de_configuracao_completa_sem_reparticao_e_recusado(app, client, db):
    """Com a soma completa, eliminar uma fração exige redistribuir: nada desaparece sem rasto."""
    id_h = db.fraction("H")["id"]
    permilagens_antes = db.permilagens()
    restantes = [f for f in db.fractions() if f["id"] != id_h]
    form = {
        f"permilagem_{f['id']}": format_permilagem(f["permilagem"]) for f in restantes
    }
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Sem redistribuição"
    form.update(_protecao(client, f"/fractions/{id_h}"))

    resposta = client.post(f"/fractions/{id_h}/delete", data=form)

    assert resposta.status_code == 302
    assert "H" in db.permilagens(), "eliminar sem redistribuir não pode ser aplicado"
    assert db.permilagens() == permilagens_antes
    assert db.total_permilagem() == 100000
    corpo = client.get(f"/fractions/{id_h}").get_data(as_text=True)
    assert "Nenhuma alteração foi guardada" in corpo
    assert "tem de ser exatamente" in corpo
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 0


def test_configuracao_progressiva_cria_fracoes_sem_completar_a_soma(
    app_vazia, admin_vazio, db_vazia
):
    """Frações e permilagens criam-se progressivamente; a soma incompleta é mostrada."""
    client = entrar(app_vazia, ADMIN_TESTE[0], ADMIN_TESTE[1])

    form = {"code": "A", "permilagem": "100,00", **_protecao(client, "/fractions/new")}
    assert client.post("/fractions/new", data=form).status_code == 302
    assert db_vazia.permilagens() == {"A": 10000}

    form = {"code": "B", "permilagem": "50,00",
            f"permilagem_{db_vazia.fraction('A')['id']}": "100,00",
            **_protecao(client, "/fractions/new")}
    assert client.post("/fractions/new", data=form).status_code == 302
    assert db_vazia.total_permilagem() == 15000

    corpo = client.get("/fractions").get_data(as_text=True)
    assert "incompleta" in corpo and "850,00" in corpo, "a página mostra quanto falta"

    # Eliminar B não exige redistribuição: a soma já estava incompleta.
    id_b = db_vazia.fraction("B")["id"]
    form = {f"permilagem_{db_vazia.fraction('A')['id']}": "100,00",
            **_protecao(client, f"/fractions/{id_b}")}
    resposta = client.post(f"/fractions/{id_b}/delete", data=form)
    assert resposta.status_code == 302
    assert db_vazia.permilagens() == {"A": 10000}


def test_renomear_fracao_sem_registos_e_aceite(app, client, db):
    """Sem registos associados, a identificação pode ser corrigida."""
    id_a = db.fraction("A")["id"]

    resposta = client.post(
        f"/fractions/{id_a}/edit",
        data={"code": "K", "active": "on", **_protecao(client, f"/fractions/{id_a}")},
    )

    assert resposta.status_code == 302
    assert db.fraction("K") is not None, "renomear uma fração sem registos devia ser aceite"
    assert "A" not in db.permilagens()


def test_eliminar_fracao_com_responsaveis_registados_e_bloqueado(app, client, db):
    id_i = criar_fracao_i(client, db)
    client.post(
        f"/fractions/{id_i}/owners",
        data={
            "name": "Responsável Real",
            "role": "proprietario",
            "valid_from": "01/01/2026",
            **_protecao(client, f"/fractions/{id_i}"),
        },
    )

    resposta = client.post(
        f"/fractions/{id_i}/delete", data=_protecao(client, f"/fractions/{id_i}")
    )

    assert resposta.status_code == 302
    assert "I" in db.permilagens(), "fração com responsáveis não pode ser eliminada"
    assert len(db.owners("I")) == 1
    assert db.total_permilagem() == 100000


def test_eliminar_fracao_com_historico_financeiro_e_bloqueado(app, client, db):
    id_i = criar_fracao_i(client, db)
    db.cria_tabela_futura()
    db.insere_registo_futuro(id_i)

    resposta = client.post(
        f"/fractions/{id_i}/delete", data=_protecao(client, f"/fractions/{id_i}")
    )

    assert resposta.status_code == 302
    assert "I" in db.permilagens(), "fração com histórico financeiro não pode ser eliminada"
    assert db.scalar("SELECT COUNT(*) FROM documentos_fase_futura") == 1


def test_eliminar_fracao_sem_reparticao_nao_pode_quebrar_a_soma(app, client, db):
    """Eliminar uma fração sem redistribuir a permilagem deixaria o prédio abaixo de 1000,00."""
    id_i = criar_fracao_i(client, db)
    restantes = [f for f in db.fractions() if f["id"] != id_i]
    form = {
        f"permilagem_{f['id']}": format_permilagem(f["permilagem"]) for f in restantes
    }
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Sem repartição"
    form.update(_protecao(client, f"/fractions/{id_i}"))

    resposta = client.post(f"/fractions/{id_i}/delete", data=form)

    assert resposta.status_code == 302
    assert "I" in db.permilagens(), "a eliminação sem repartição não pode ser aplicada"
    assert db.total_permilagem() == 100000
    corpo = client.get(f"/fractions/{id_i}").get_data(as_text=True)
    assert "Nenhuma alteração foi guardada" in corpo
    assert "967,25" in corpo, "a mensagem mostra a soma que resultaria da eliminação"


def test_eliminar_fracao_com_reparticao_valida_e_confirmada_e_permitido(app, client, db):
    id_i = criar_fracao_i(client, db)
    id_h = db.fraction("H")["id"]
    restantes = [f for f in db.fractions() if f["id"] != id_i]
    form = {
        f"permilagem_{f['id']}": format_permilagem(f["permilagem"]) for f in restantes
    }
    # a permilagem de I (32,75) volta para H, escrita pelo administrador
    form[f"permilagem_{id_h}"] = format_permilagem(db.permilagens()["H"] + 3275)
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Fração extinta por decisão de assembleia"
    form.update(_protecao(client, f"/fractions/{id_i}"))

    resposta = client.post(f"/fractions/{id_i}/delete", data=form)

    assert resposta.status_code == 302
    assert "I" not in db.permilagens()
    assert len(db.fractions()) == 8
    assert db.permilagens()["H"] == 10000
    assert db.total_permilagem() == 100000
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 2


def test_eliminar_fracao_sem_confirmacao_nao_elimina_nem_muda_permilagens(app, client, db):
    id_i = criar_fracao_i(client, db)
    permilagens_antes = db.permilagens()
    id_h = db.fraction("H")["id"]
    form = formulario_atual(client, db, path=f"/fractions/{id_i}")
    form.pop(f"permilagem_{id_i}", None)
    form[f"permilagem_{id_h}"] = format_permilagem(db.permilagens()["H"] + 3275)
    # sem confirmar_reparticao

    resposta = client.post(f"/fractions/{id_i}/delete", data=form)

    assert resposta.status_code == 302
    assert "I" in db.permilagens()
    assert db.permilagens() == permilagens_antes
    assert "confirmação explícita" in client.get(f"/fractions/{id_i}").get_data(as_text=True)


def test_fracao_com_rasto_de_permilagem_nao_pode_ser_eliminada(app, client, db):
    """O rasto de permilagem é histórico: não pode ficar órfão."""
    id_i = criar_fracao_i(client, db)
    id_h = db.fraction("H")["id"]
    form = formulario_atual(client, db)
    form[f"permilagem_{id_i}"] = "42,75"
    form[f"permilagem_{id_h}"] = format_permilagem(db.permilagens()["H"] - 1000)
    form["confirmar_reparticao"] = "on"
    assert client.post("/fractions/permilagens", data=form).status_code == 302

    resposta = client.post(
        f"/fractions/{id_i}/delete", data=_protecao(client, f"/fractions/{id_i}")
    )

    assert resposta.status_code == 302
    assert "I" in db.permilagens()
    assert "alterações de permilagem registadas" in client.get(f"/fractions/{id_i}").get_data(
        as_text=True
    )


def test_renomear_fracao_com_responsaveis_nao_altera_a_letra(app, client, db):
    """Uma fração criada depois da ata também não é renomeada quando já tem registos."""
    id_i = criar_fracao_i(client, db)
    client.post(
        f"/fractions/{id_i}/owners",
        data={
            "name": "Responsável Antigo",
            "role": "proprietario",
            "valid_from": "01/01/2020",
            "valid_to": "31/12/2024",
            **_protecao(client, f"/fractions/{id_i}"),
        },
    )

    resposta = client.post(
        f"/fractions/{id_i}/edit",
        data={"code": "Z", "active": "on", **_protecao(client, f"/fractions/{id_i}")},
    )

    assert resposta.status_code == 302
    assert db.fraction("I")["code"] == "I", "renomear uma fração com histórico não é aceite"
    assert len(db.owners("I")) == 1
    assert "registos associados" in client.get(f"/fractions/{id_i}").get_data(as_text=True)


def test_desativar_fracao_preserva_permilagem_responsaveis_e_historico(app, client, db):
    id_c = db.fraction("C")["id"]
    client.post(
        f"/fractions/{id_c}/owners",
        data={
            "name": "Responsável Antigo",
            "role": "representante",
            "valid_from": "01/01/2020",
            "valid_to": "31/12/2024",
            **_protecao(client, f"/fractions/{id_c}"),
        },
    )
    permilagem_antes = db.permilagens()["C"]

    resposta = client.post(
        f"/fractions/{id_c}/edit",
        data={"code": "C", **_protecao(client, f"/fractions/{id_c}")},  # sem "active"
    )

    assert resposta.status_code == 302
    fracao = db.fraction("C")
    assert fracao["active"] == 0
    assert fracao["permilagem"] == permilagem_antes == 11000
    historico = db.owners("C")
    assert len(historico) == 1
    assert historico[0]["name"] == "Responsável Antigo"
    assert historico[0]["valid_to"] == "2024-12-31"
    assert db.total_permilagem() == 100000

    html = client.get(f"/fractions/{id_c}").get_data(as_text=True)
    assert "desativada" in html


def test_editar_fracao_com_codigo_duplicado_e_rejeitado(app, client, db):
    id_c = db.fraction("C")["id"]

    resposta = client.post(
        f"/fractions/{id_c}/edit",
        data={"code": "A", "active": "on", **_protecao(client, f"/fractions/{id_c}")},
    )

    assert resposta.status_code == 302
    assert db.fraction("C")["code"] == "C"
    assert len(db.fractions()) == 8


def test_editar_fracao_com_codigo_invalido_e_rejeitado(app, client, db):
    id_c = db.fraction("C")["id"]

    client.post(
        f"/fractions/{id_c}/edit",
        data={"code": "ABCDEFGHI", "active": "on", **_protecao(client, f"/fractions/{id_c}")},
    )

    assert db.fraction("C")["code"] == "C"


def test_formulario_de_edicao_desatualizado_nao_desativa_a_fracao(app, client, db):
    id_c = db.fraction("C")["id"]
    protecao = _protecao(client, f"/fractions/{id_c}")

    # Outra alteração move o estado: a letra B passa a desativada.
    id_b = db.fraction("B")["id"]
    client.post(
        f"/fractions/{id_b}/edit",
        data={"code": "B", **_protecao(client, f"/fractions/{id_b}")},
    )

    resposta = client.post(
        f"/fractions/{id_c}/edit",
        data={"code": "C", **protecao},  # revisão de antes da outra gravação
    )

    assert resposta.status_code == 302
    assert db.fraction("C")["active"] == 1, "um formulário desatualizado alterou a fração"
    assert db.fraction("B")["active"] == 0
    assert "desatualizada" in client.get(f"/fractions/{id_c}").get_data(as_text=True)


def test_fracoes_da_lista_estao_ordenadas_e_com_soma_mostrada(app, client):
    html = client.get("/fractions").get_data(as_text=True)
    for codigo in "ABCDEFGH":
        assert f">{codigo}<" in html
    assert "1000,00" in html


def test_fracao_inexistente_devolve_404(app, client):
    assert client.get("/fractions/9999").status_code == 404
