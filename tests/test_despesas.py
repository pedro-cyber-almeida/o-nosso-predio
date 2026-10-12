"""Entrega 3: categorias do administrador, despesas, estados, correções e travões."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    anular_despesa,
    categoria_de_trabalho,
    criar_categoria,
    editar_despesa,
    id_da_categoria,
    pagar_despesa,
    protecao_de,
    protecao_de_edicao_em,
    registar_despesa,
    ultima_despesa,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """As despesas correm como se hoje fosse 01/02/2027."""


# ------------------------------------------------------------- categorias ----


def test_categorias_sao_definidas_pelo_administrador_e_aparecem_na_lista(app, client, db):
    criar_categoria(client, "Limpeza")
    criar_categoria(client, "Jardim e exteriores")

    html = client.get("/despesas/categorias").get_data(as_text=True)
    assert "Limpeza" in html and "Jardim e exteriores" in html
    assert db.scalar("SELECT COUNT(*) FROM expense_categories") == 2


def test_categoria_repetida_nao_e_criada_duas_vezes(app, client, db):
    criar_categoria(client, "Limpeza")
    criar_categoria(client, "limpeza")  # o mesmo nome, em minúsculas
    assert db.scalar("SELECT COUNT(*) FROM expense_categories") == 1


def test_categoria_em_uso_nao_e_apagada_nem_renomeada(app, client, db):
    categoria = categoria_de_trabalho(client, db, "Limpeza")
    registar_despesa(client, db, descricao="Limpeza do hall", categoria=categoria)

    client.post(
        "/despesas/categorias",
        data={**protecao_de(client, "/despesas/categorias"), "acao": "eliminar",
              "categoria_id": categoria, "nome": "Limpeza"},
    )
    assert db.scalar("SELECT COUNT(*) FROM expense_categories WHERE id = ?", (categoria,)) == 1

    client.post(
        "/despesas/categorias",
        data={**protecao_de(client, "/despesas/categorias"), "acao": "renomear",
              "categoria_id": categoria, "nome": "Limpeza semanal"},
    )
    assert db.scalar("SELECT nome FROM expense_categories WHERE id = ?", (categoria,)) == "Limpeza"

    html = client.get("/despesas/categorias").get_data(as_text=True)
    assert "não é renomeada nem apagada" in html


def test_categoria_sem_despesas_pode_ser_eliminada(app, client, db):
    categoria = categoria_de_trabalho(client, db, "Temporária")
    client.post(
        "/despesas/categorias",
        data={**protecao_de(client, "/despesas/categorias"), "acao": "eliminar",
              "categoria_id": categoria, "nome": "Temporária"},
    )
    assert db.scalar("SELECT COUNT(*) FROM expense_categories WHERE id = ?", (categoria,)) == 0


# ---------------------------------------------------------------- despesas ----


def test_despesa_registada_nao_e_saida_de_dinheiro(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    assert despesa["amount_cents"] == 12000
    assert despesa["cancelled_at"] is None

    html = client.get("/").get_data(as_text=True)
    assert "120,00 EUR" in html  # despesas por pagar
    assert "Sem pagamentos de despesas registados" in html  # registar não é pagar


def test_despesa_sem_confirmacao_nao_e_guardada(app, client, db):
    registar_despesa(client, db, confirmar=False)
    assert db.scalar("SELECT COUNT(*) FROM expenses") == 0


def test_duplo_envio_do_mesmo_formulario_nao_cria_duas_despesas(app, client, db):
    categoria_de_trabalho(client, db)
    protecao = protecao_de(client, "/despesas/nova")
    registar_despesa(client, db, protecao=protecao)
    registar_despesa(client, db, protecao=protecao)  # reenvio do mesmo formulário
    assert db.scalar("SELECT COUNT(*) FROM expenses") == 1


def test_estado_da_despesa_por_pagar_parcial_e_paga(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)

    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "por pagar" in html.lower()

    pagar_despesa(client, db, despesa["id"], "50,00")
    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "50,00 EUR" in html and "70,00 EUR" in html  # pago e em falta

    pagar_despesa(client, db, despesa["id"], "70,00")
    estado = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "paga" in estado.lower()


def test_despesa_vencida_so_com_vencimento_passado_e_valor_por_pagar(app, client, db):
    registar_despesa(client, db, valor="120,00", vencimento="15/01/2027")
    despesa = ultima_despesa(db)
    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert '<strong class="erro">vencida</strong>' in html

    # Sem vencimento não há classificação de vencida, por muito antiga que a data seja.
    registar_despesa(client, db, descricao="Jardim", valor="30,00", data="01/01/2027")
    sem_vencimento = ultima_despesa(db)
    html = client.get(f"/despesas/{sem_vencimento['id']}").get_data(as_text=True)
    assert '<strong class="erro">vencida</strong>' not in html


def test_despesa_nao_pode_ser_apagada_diretamente_da_base(app, client, db):
    registar_despesa(client, db)
    despesa = ultima_despesa(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM expenses WHERE id = ?", (despesa["id"],))


def test_anular_despesa_exige_motivo_e_conserva_o_registo(app, client, db):
    registar_despesa(client, db, descricao="Reparação errada")
    despesa = ultima_despesa(db)

    anular_despesa(client, despesa["id"], motivo="", confirmar=False)
    assert db.scalar("SELECT cancelled_at FROM expenses WHERE id = ?", (despesa["id"],)) is None

    anular_despesa(client, despesa["id"], motivo="Registada por engano")
    guardado = next(e for e in db.expenses() if e["id"] == despesa["id"])
    assert guardado["cancelled_at"] == "2027-02-01"
    assert guardado["cancellation_reason"] == "Registada por engano"

    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "anulada" in html.lower() and "Registada por engano" in html


def test_despesa_com_pagamentos_validos_nao_pode_ser_anulada(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "50,00")

    anular_despesa(client, despesa["id"], motivo="Já paguei metade")
    assert db.scalar("SELECT cancelled_at FROM expenses WHERE id = ?", (despesa["id"],)) is None
    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "anula primeiro cada pagamento" in html


def test_anulacao_de_despesa_nao_se_repete(app, client, db):
    registar_despesa(client, db)
    despesa = ultima_despesa(db)
    anular_despesa(client, despesa["id"], motivo="Engano")
    # Página de categorias serve de fonte fresca de CSRF e revisão de concorrência.
    anular_despesa(
        client, despesa["id"], motivo="Outro motivo",
        protecao=protecao_de(client, "/despesas/categorias"),
    )
    assert db.scalar(
        "SELECT cancellation_reason FROM expenses WHERE id = ?", (despesa["id"],)
    ) == "Engano"


# ------------------------------------------------------- correção de despesa ----


def test_edicao_de_despesa_previsualiza_sem_gravar_e_exige_motivo(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)

    resposta = editar_despesa(
        client, db, despesa["id"], valor="150,00", previsualizar=True, confirmar=False
    )
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 12000
    html = resposta.get_data(as_text=True)
    assert "150,00 EUR" in html  # a pré-visualização mostra o efeito proposto

    editar_despesa(client, db, despesa["id"], valor="150,00", motivo=None)
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 12000


def test_edicao_confirmada_guarda_revisao_com_antes_e_depois(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)

    editar_despesa(client, db, despesa["id"], valor="150,00", motivo="Fatura veio mais cara")
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 15000
    revisoes = db.rows("SELECT * FROM expense_revisions WHERE expense_id = ?", (despesa["id"],))
    assert len(revisoes) == 1
    assert revisoes[0]["amount_before"] == 12000
    assert revisoes[0]["amount_after"] == 15000
    assert revisoes[0]["motivo"] == "Fatura veio mais cara"

    html = client.get(f"/despesas/{despesa['id']}").get_data(as_text=True)
    assert "Fatura veio mais cara" in html


def test_reenvio_do_formulario_de_correcao_nao_duplica_o_efeito(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)

    protecao = protecao_de_edicao_em(client, f"/despesas/{despesa['id']}/editar")
    editar_despesa(client, db, despesa["id"], valor="150,00", protecao=protecao)
    editar_despesa(client, db, despesa["id"], valor="150,00", protecao=protecao)
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 15000
    assert db.scalar("SELECT COUNT(*) FROM expense_revisions") == 1


def test_despesa_nao_pode_ficar_abaixo_do_ja_pago(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    pagar_despesa(client, db, despesa["id"], "80,00")

    editar_despesa(client, db, despesa["id"], valor="60,00", motivo="Tenta reduzir abaixo do pago")
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 12000
    html = client.get(f"/despesas/{despesa['id']}/editar").get_data(as_text=True)
    assert "80,00 EUR" in html  # explica o que já está pago


def test_categoria_muda_por_reafectacao_na_edicao_justificada(app, client, db):
    antiga = categoria_de_trabalho(client, db, "Limpeza")
    registar_despesa(client, db, descricao="Limpeza do hall", categoria=antiga)
    despesa = ultima_despesa(db)

    nova = criar_categoria(client, "Serviços gerais")
    editar_despesa(
        client, db, despesa["id"],
        categoria=id_da_categoria(db, "Serviços gerais"),
        motivo="A categoria antiga já não se usa",
    )
    assert db.scalar("SELECT category_id FROM expenses WHERE id = ?", (despesa["id"],)) == id_da_categoria(db, "Serviços gerais")
    revisao = db.rows("SELECT * FROM expense_revisions WHERE expense_id = ?", (despesa["id"],))[0]
    assert revisao["category_before"] == antiga
    assert revisao["category_after"] == id_da_categoria(db, "Serviços gerais")


def test_despesa_anulada_nao_e_editavel(app, client, db):
    registar_despesa(client, db)
    despesa = ultima_despesa(db)
    anular_despesa(client, despesa["id"], motivo="Engano")

    resposta = editar_despesa(
        client, db, despesa["id"], valor="999,00",
        protecao=protecao_de(client, "/despesas/categorias"),
    )
    assert resposta.status_code == 302  # a edição de uma anulação é recusada
    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 12000


def test_formulario_desatualizado_de_despesa_nao_altera_nada(app, client, db):
    registar_despesa(client, db, valor="120,00")
    despesa = ultima_despesa(db)
    protecao_velha = protecao_de(client, f"/despesas/{despesa['id']}/editar")

    # Outra correção, entretanto aplicada, envelhece a revisão do formulário velho.
    editar_despesa(client, db, despesa["id"], valor="150,00", motivo="Primeira correção")
    editar_despesa(client, db, despesa["id"], valor="200,00", protecao=protecao_velha)

    assert db.scalar("SELECT amount_cents FROM expenses WHERE id = ?", (despesa["id"],)) == 15000


# ------------------------------------------------------------- permissões ----


def test_condomino_nao_acede_a_despesas(app, client, db):
    from conftest import criar_condomino, entrar_condomino

    criar_condomino(app, db, "condomino.leitor", ("A",))
    condomino = entrar_condomino(app, "condomino.leitor")

    for caminho in ("/despesas", "/despesas/nova", "/despesas/categorias"):
        resposta = condomino.get(caminho)
        assert resposta.status_code == 302, caminho
        assert resposta.headers["Location"].startswith("/")

    html = condomino.get("/").get_data(as_text=True)
    assert 'href="/despesas' not in html


def test_anonimo_nao_acede_a_despesas(app, cliente_anonimo):
    resposta = cliente_anonimo.get("/despesas")
    assert resposta.status_code == 302
