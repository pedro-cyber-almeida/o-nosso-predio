"""Responsáveis: papéis, períodos, duplicados, encerramento, escape de HTML e concorrência."""

from __future__ import annotations

import re

import pytest

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')


def _protecao(client, path: str) -> dict[str, str]:
    html = client.get(path).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {path}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisão de concorrência em {path}"
    return {"csrf_token": match.group(1), **campos}


def regista(client, db, codigo: str, **campos) -> object:
    id_fracao = db.fraction(codigo)["id"]
    dados = {"name": "Responsável Teste", "role": "proprietario", **campos}
    dados.update(_protecao(client, f"/fractions/{id_fracao}"))
    return client.post(f"/fractions/{id_fracao}/owners", data=dados)


def test_responsavel_valido_fica_registado_como_atual(app, client, db):
    resposta = regista(
        client,
        db,
        "A",
        name="Maria Silva",
        role="administrador",
        valid_from="01/01/2026",
        source="Ata n.º 23",
    )

    assert resposta.status_code == 302
    registos = db.owners("A")
    assert len(registos) == 1
    assert registos[0]["name"] == "Maria Silva"
    assert registos[0]["role"] == "administrador"
    assert registos[0]["valid_from"] == "2026-01-01"
    assert registos[0]["valid_to"] is None


@pytest.mark.parametrize("nome", ["", "   ", "\t"])
def test_nome_do_responsavel_e_obrigatorio(app, client, db, nome):
    id_a = db.fraction("A")["id"]

    resposta = regista(client, db, "A", name=nome)

    assert resposta.status_code == 302
    assert "obrigatório" in client.get(f"/fractions/{id_a}").get_data(as_text=True)
    assert db.owners("A") == []


@pytest.mark.parametrize("data", ["31/02/2027", "abc", "2027-13-01", "01-01-2027"])
def test_data_de_inicio_invalida_e_rejeitada(app, client, db, data):
    regista(client, db, "A", valid_from=data)
    assert db.owners("A") == []


@pytest.mark.parametrize("data", ["31/02/2027", "abc"])
def test_data_de_fim_invalida_e_rejeitada(app, client, db, data):
    regista(client, db, "A", valid_from="01/01/2026", valid_to=data)
    assert db.owners("A") == []


def test_data_de_fim_anterior_a_data_de_inicio_e_rejeitada(app, client, db):
    regista(client, db, "A", valid_from="01/06/2026", valid_to="01/05/2026")
    assert db.owners("A") == []


def test_duplicar_responsavel_atual_da_mesma_fracao_e_rejeitado(app, client, db):
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2026")

    resposta = regista(client, db, "A", name="Maria Silva", valid_from="01/01/2027")

    assert resposta.status_code == 302
    assert len(db.owners("A")) == 1, "não pode haver dois responsáveis atuais iguais"


def test_mesmo_nome_pode_ficar_em_fracoes_diferentes(app, client, db):
    regista(client, db, "A", name="João Lima")
    regista(client, db, "B", name="João Lima")

    assert len(db.owners("A")) == 1
    assert len(db.owners("B")) == 1


def test_encerrar_responsavel_conserva_o_registo_com_data_de_fim(app, client, db):
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]

    resposta = client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2025", **_protecao(client, f"/fractions/{id_a}")},
    )

    assert resposta.status_code == 302
    registos = db.owners("A")
    assert len(registos) == 1, "encerrar não apaga o histórico"
    assert registos[0]["name"] == "Maria Silva"
    assert registos[0]["valid_from"] == "2020-01-01"
    assert registos[0]["valid_to"] == "2025-12-31"

    html = client.get(f"/fractions/{id_a}").get_data(as_text=True)
    assert "Maria Silva" in html
    assert "31/12/2025" in html


def test_encerrar_com_data_anterior_ao_inicio_e_rejeitado(app, client, db):
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]

    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "01/01/2019", **_protecao(client, f"/fractions/{id_a}")},
    )

    assert db.owners("A")[0]["valid_to"] is None


def test_encerrar_duas_vezes_nao_altera_o_registo_encerrado(app, client, db):
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]

    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2025", **_protecao(client, f"/fractions/{id_a}")},
    )
    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "01/01/2026", **_protecao(client, f"/fractions/{id_a}")},
    )

    assert db.owners("A")[0]["valid_to"] == "2025-12-31"


def test_encerrar_responsavel_de_outra_fracao_devolve_404(app, client, db):
    id_a = db.fraction("A")["id"]
    id_b = db.fraction("B")["id"]
    regista(client, db, "A", name="Maria Silva")
    id_responsavel = db.owners("A")[0]["id"]

    resposta = client.post(
        f"/fractions/{id_b}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2025", **_protecao(client, f"/fractions/{id_b}")},
    )

    assert resposta.status_code == 404
    assert db.owners("A")[0]["valid_to"] is None


def test_nome_com_markup_e_apresentado_escapado_e_guardado_literal(app, client, db):
    id_a = db.fraction("A")["id"]
    nome = "<script>alert(1)</script>"

    regista(client, db, "A", name=nome, valid_from="01/01/2026")

    assert db.owners("A")[0]["name"] == nome, "o texto é guardado, escapado só na saída"
    html = client.get(f"/fractions/{id_a}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    lista = client.get("/fractions").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in lista


def test_funcao_invalida_e_rejeitada(app, client, db):
    regista(client, db, "A", name="Maria Silva", role="inexistente")
    assert db.owners("A") == []


def test_nome_demasiado_longo_e_rejeitado(app, client, db):
    id_a = db.fraction("A")["id"]

    resposta = regista(client, db, "A", name="M" * 121)

    assert resposta.status_code == 302
    assert "no máximo 120 caracteres" in client.get(f"/fractions/{id_a}").get_data(as_text=True)
    assert db.owners("A") == []


def test_contacto_demasiado_longo_e_rejeitado(app, client, db):
    regista(client, db, "A", name="Maria Silva", email="e" * 121)
    assert db.owners("A") == []


def test_registo_igual_com_o_mesmo_periodo_nao_e_duplicado(app, client, db):
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020", valid_to="31/12/2024")
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020", valid_to="31/12/2024")

    assert len(db.owners("A")) == 1, "o mesmo período não pode ser registado duas vezes"


def test_responsavel_com_data_futura_nao_aparece_como_atual(app, client, db):
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Proprietário Futuro", valid_from="31/12/2099")

    html = client.get(f"/fractions/{id_a}").get_data(as_text=True)
    assert "Nenhum responsável atual" in html
    assert "Proprietário Futuro" in html, "o registo futuro continua no histórico"
    assert len(db.owners("A")) == 1


def test_dois_proprietarios_simultaneos_sao_rejeitados(app, client, db):
    """Só pode haver um proprietário (papel principal) por fração em cada data."""
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", role="proprietario", valid_from="01/01/2020")

    resposta = regista(
        client, db, "A", name="José Silva", role="proprietario", valid_from="01/06/2023"
    )

    assert resposta.status_code == 302
    assert len(db.owners("A")) == 1, "dois proprietários em simultâneo não são aceites"
    assert "Só pode haver um Proprietário" in client.get(f"/fractions/{id_a}").get_data(as_text=True)


def test_proprietarios_sucessivos_sem_sobreposicao_sao_aceites(app, client, db):
    """Convenção: valid_to é o último dia; o período seguinte começa no dia a seguir."""
    regista(client, db, "A", name="Maria Silva", role="proprietario",
            valid_from="01/01/2020", valid_to="31/12/2024")
    regista(client, db, "A", name="José Silva", role="proprietario", valid_from="01/01/2025")

    registos = db.owners("A")
    assert len(registos) == 2
    assert {r["name"] for r in registos} == {"Maria Silva", "José Silva"}


def test_proprietario_que_comeca_no_dia_de_fim_do_anterior_e_rejeitado(app, client, db):
    """Os dois intervalos inclusivos sobrepor-se-iam nesse dia."""
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", role="proprietario",
            valid_from="01/01/2020", valid_to="31/12/2024")

    regista(client, db, "A", name="José Silva", role="proprietario", valid_from="31/12/2024")

    assert len(db.owners("A")) == 1


def test_coproprietarios_e_contactos_simultaneos_sao_aceites(app, client, db):
    """A unicidade só travava o papel principal: outros papéis coexistem livremente."""
    regista(client, db, "A", name="Maria Silva", role="proprietario", valid_from="01/01/2020")
    regista(client, db, "A", name="Ana Silva", role="coproprietario", valid_from="01/01/2020")
    regista(client, db, "A", name="José Silva", role="coproprietario", valid_from="01/01/2020")
    regista(client, db, "A", name="Porteiro", role="contacto", valid_from="01/01/2020")
    regista(client, db, "A", name="Administrador do Prédio", role="administrador",
            valid_from="01/01/2020")

    registos = db.owners("A")
    assert len(registos) == 5
    assert {r["role"] for r in registos} == {
        "proprietario",
        "coproprietario",
        "contacto",
        "administrador",
    }


def test_dois_administradores_ou_representantes_em_simultaneo_sao_aceites(app, client, db):
    regista(client, db, "A", name="Ana", role="administrador", valid_from="01/01/2026")
    regista(client, db, "A", name="Bruno", role="administrador", valid_from="01/01/2026")
    regista(client, db, "A", name="Carla", role="representante", valid_from="01/01/2026")
    regista(client, db, "A", name="Duarte", role="representante", valid_from="01/01/2026")

    assert len(db.owners("A")) == 4


def test_proprietario_encerrado_liberta_o_papel_para_o_sucessor(app, client, db):
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", role="proprietario", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]
    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2024", **_protecao(client, f"/fractions/{id_a}")},
    )

    regista(client, db, "A", name="José Silva", role="proprietario", valid_from="01/01/2025")

    assert len(db.owners("A")) == 2
    atuais = [o for o in db.owners("A") if o["valid_to"] is None]
    assert [o["name"] for o in atuais] == ["José Silva"]


def test_encerrar_com_data_futura_e_rejeitado(app, client, db):
    """Um período em curso fica sem data de fim: encerrar com data futura criaria um beco sem saída."""
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]

    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2099", **_protecao(client, f"/fractions/{id_a}")},
    )

    assert db.owners("A")[0]["valid_to"] is None
    assert "não pode ser futura" in client.get(f"/fractions/{id_a}").get_data(as_text=True)


def test_encerrar_com_data_futura_nao_bloqueia_o_sucessor(app, client, db):
    """Sem datas de fim futuras, o papel principal fica disponível para quem entra hoje."""
    id_a = db.fraction("A")["id"]
    regista(client, db, "A", name="Maria Silva", role="proprietario", valid_from="01/01/2020")
    id_responsavel = db.owners("A")[0]["id"]
    client.post(
        f"/fractions/{id_a}/owners/{id_responsavel}/close",
        data={"valid_to": "31/12/2099", **_protecao(client, f"/fractions/{id_a}")},
    )

    regista(client, db, "A", name="José Silva", role="proprietario", valid_from="01/01/2026")

    assert len(db.owners("A")) == 1, "o responsável atual devia estar disponível"


def test_data_de_fim_futura_e_rejeitada_ao_registar(app, client, db):
    id_a = db.fraction("A")["id"]

    resposta = regista(client, db, "A", name="Maria Silva", valid_from="01/01/2020",
                       valid_to="31/12/2099")

    assert resposta.status_code == 302
    assert db.owners("A") == []
    assert "não pode ser futura" in client.get(f"/fractions/{id_a}").get_data(as_text=True)


def test_formulario_de_responsavel_desatualizado_nao_regista_nada(app, client, db):
    """Duas versões do mesmo formulário: a segunda é recusada e nada é gravado."""
    id_a = db.fraction("A")["id"]
    protecao = _protecao(client, f"/fractions/{id_a}")

    primeira = {
        "name": "Maria Silva",
        "role": "proprietario",
        "valid_from": "01/01/2020",
        **_protecao(client, f"/fractions/{id_a}"),
    }
    assert client.post(f"/fractions/{id_a}/owners", data=primeira).status_code == 302
    registos_guardados = db.owners("A")

    segunda = {
        "name": "José Silva",
        "role": "administrador",
        "valid_from": "01/01/2026",
        **protecao,  # revisão de antes da primeira gravação
    }
    resposta = client.post(f"/fractions/{id_a}/owners", data=segunda)

    assert resposta.status_code == 302
    assert db.owners("A") == registos_guardados, "um formulário desatualizado registou um responsável"
    assert "desatualizada" in client.get(f"/fractions/{id_a}").get_data(as_text=True)
