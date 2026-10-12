"""Biblioteca de documentos (v10): upload, validação, duplicados, versões,
associações, arquivo e partilha — tudo em bases e ficheiros temporários."""

from __future__ import annotations

import io

import pytest

from conftest import (
    criar_condomino,
    csrf_de,
    entrar_condomino,
)

PDF_TESTE = b"%PDF-1.4 ata de teste"
PDF_CORRIGIDO = b"%PDF-1.4 ata de teste corrigida"
DOCX_TESTE = b"DOCX falso de teste"
XLSX_TESTE = b"XLSX falso de teste"


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """A biblioteca corre como se hoje fosse 01/02/2027."""


def _carregar(client, ficheiros: list[tuple[str, bytes]], **campos):
    dados = {"csrf_token": csrf_de(client, "/documentos/carregar")}
    dados.update(campos)
    dados["ficheiros"] = [(io.BytesIO(conteudo), nome) for nome, conteudo in ficheiros]
    return client.post("/documentos/carregar", data=dados, follow_redirects=True)


def _documento(db, documento_id: int) -> dict:
    return db.rows("SELECT * FROM documentos WHERE id = ?", (documento_id,))[0]


def _documento_por_nome(db, nome: str) -> dict:
    return db.rows("SELECT * FROM documentos WHERE nome_original = ?", (nome,))[0]


# ------------------------------------------------------------- upload básico --


def test_upload_de_ata_guarda_metadados_e_comeca_privado(app, client, db):
    criar_condomino(app, db, "condomino-a", ("A",))
    resposta = _carregar(
        client,
        [("ata-23.pdf", PDF_TESTE)],
        titulo="Ata n.º 23",
        categoria="Ata",
        data_documento="2026-10-05",
        descricao="Assembleia ordinária de outubro.",
        ata_numero="23",
        ata_data="2026-10-05",
    )
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert "Ata n.º 23" in html
    assert "1 documento(s) guardados" in html

    linha = _documento_por_nome(db, "ata-23.pdf")
    assert linha["origem"] == "upload"
    assert linha["titulo"] == "Ata n.º 23"
    assert linha["categoria"] == "Ata"
    assert linha["data_documento"] == "2026-10-05"
    assert linha["ata_numero"] == "23"
    assert linha["ata_data"] == "2026-10-05"
    assert linha["arquivado"] == 0
    assert linha["partilha_todos"] == 0
    assert db.scalar("SELECT COUNT(*) FROM documento_fracoes") == 0

    # Começa privado: nem a visualização nem a descarga direta passam.
    condomino = entrar_condomino(app, "condomino-a")
    assert condomino.get(f"/documentos/{linha['id']}").status_code == 404
    assert condomino.get(f"/documentos/{linha['id']}/descarregar").status_code == 404
    assert "ata-23.pdf" not in condomino.get("/documentos/meus").get_data(as_text=True)


def test_upload_multiplos_e_ficheiro_recusado_nao_passa(client, db):
    resposta = _carregar(
        client,
        [("fatura-a.pdf", PDF_TESTE), ("nota.png", b"\x89PNG fake"), ("virus.exe", b"MZ")],
        categoria="Fatura",
    )
    html = resposta.get_data(as_text=True)
    assert "2 documento(s) guardados" in html
    assert "「virus.exe」" in html
    assert "tipo não aceite" in html
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 2
    assert db.scalar("SELECT COUNT(*) FROM documentos WHERE nome_original = 'virus.exe'") == 0


def test_upload_acima_do_limite_e_recusado_com_mensagem_clara(client, db):
    grande = b"x" * (10 * 1024 * 1024 + 1)
    resposta = _carregar(client, [("enorme.pdf", grande)], categoria="Outro")
    html = resposta.get_data(as_text=True)
    assert "acima do limite de 10 MB" in html
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 0


def test_upload_sem_ficheiro_e_com_categoria_inventada_nao_guarda(client, db):
    resposta = client.post(
        "/documentos/carregar",
        data={"csrf_token": csrf_de(client, "/documentos/carregar"), "categoria": "Fax"},
        follow_redirects=True,
    )
    html = resposta.get_data(as_text=True)
    assert "Categoria: escolhe uma das categorias da lista." in html
    assert "Escolhe pelo menos um ficheiro" in html
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 0


def test_upload_nao_cria_movimentos_financeiros(client, db):
    pagamentos_antes = db.scalar("SELECT COUNT(*) FROM payments")
    despesas_antes = db.scalar("SELECT COUNT(*) FROM expenses")
    cobrancas_antes = db.scalar("SELECT COUNT(*) FROM charges")
    _carregar(client, [("fatura.pdf", PDF_TESTE)], categoria="Fatura")
    assert db.scalar("SELECT COUNT(*) FROM payments") == pagamentos_antes
    assert db.scalar("SELECT COUNT(*) FROM expenses") == despesas_antes
    assert db.scalar("SELECT COUNT(*) FROM charges") == cobrancas_antes


def test_condomino_nao_accede_a_carregar(app, client, db):
    criar_condomino(app, db, "condomino-x", ("A",))
    condomino = entrar_condomino(app, "condomino-x")
    assert condomino.get("/documentos/carregar").status_code == 404


# ------------------------------------------------------------- duplicados -----


def test_ficheiro_identico_e_avisado_e_so_guardado_se_confirmado(client, db):
    _carregar(client, [("copia-1.pdf", PDF_TESTE)], titulo="Primeira cópia", categoria="Contrato")
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 1

    resposta = _carregar(client, [("copia-2.pdf", PDF_TESTE)], categoria="Contrato")
    html = resposta.get_data(as_text=True)
    assert "é idêntico ao documento n.º 1" in html
    assert "guardar mesmo assim" in html
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 1

    resposta = _carregar(
        client, [("copia-2.pdf", PDF_TESTE)], categoria="Contrato", repetido="on"
    )
    assert "1 documento(s) guardados" in resposta.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 2


# ------------------------------------------------- formatos e descarga --------


def test_docx_e_xlsx_descarregam_sem_conversao(client, db):
    _carregar(client, [("contrato.docx", DOCX_TESTE)], categoria="Contrato")
    _carregar(client, [("extrato.xlsx", XLSX_TESTE)], categoria="Extrato bancário")
    docx = _documento_por_nome(db, "contrato.docx")
    xlsx = _documento_por_nome(db, "extrato.xlsx")
    assert docx["mime"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert xlsx["mime"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    pagina = client.get(f"/documentos/{docx['id']}").get_data(as_text=True)
    assert "não se pré-visualiza" in pagina

    for documento_id, nome in ((docx["id"], "contrato.docx"), (xlsx["id"], "extrato.xlsx")):
        resposta = client.get(f"/documentos/{documento_id}/descarregar")
        assert resposta.status_code == 200
        assert "attachment" in resposta.headers["Content-Disposition"]
        assert nome in resposta.headers["Content-Disposition"]
        assert resposta.data in (DOCX_TESTE, XLSX_TESTE)


# ------------------------------------------------- pesquisa e filtros ---------


def test_pesquisa_e_filtros_da_biblioteca(client, db):
    _carregar(client, [("ata-outubro.pdf", PDF_TESTE)], titulo="Ata de outubro",
              categoria="Ata", data_documento="2026-10-05")
    _carregar(client, [("fatura-luz.pdf", b"%PDF-1.4 fatura luz")], titulo="Fatura de luz",
              categoria="Fatura", data_documento="2027-01-20")

    html = client.get("/documentos?q=luz").get_data(as_text=True)
    assert "Fatura de luz" in html
    assert "Ata de outubro" not in html

    html = client.get("/documentos?q=ata-outubro").get_data(as_text=True)
    assert "Ata de outubro" in html  # pesquisa também pelo nome original

    html = client.get("/documentos?categoria=Ata").get_data(as_text=True)
    assert "Ata de outubro" in html
    assert "Fatura de luz" not in html

    html = client.get("/documentos?data_de=2027-01-01&data_ate=2027-01-31").get_data(as_text=True)
    assert "Fatura de luz" in html
    assert "Ata de outubro" not in html


# ------------------------------------------------------ editar metadados ------


def test_editar_metadados_muda_campos_e_limpa_atas(client, db):
    _carregar(client, [("ata-24.pdf", PDF_TESTE)], titulo="Ata n.º 24", categoria="Ata",
              ata_numero="24", ata_data="2026-11-02")
    documento_id = _documento_por_nome(db, "ata-24.pdf")["id"]

    resposta = client.post(
        f"/documentos/{documento_id}/editar",
        data={
            "csrf_token": csrf_de(client, f"/documentos/{documento_id}/editar"),
            "titulo": "Ata n.º 24 (revisada)",
            "categoria": "Outro",
            "data_documento": "2026-11-02",
            "descricao": "Guardada como Outro por engano.",
            "ata_numero": "24",
            "ata_data": "2026-11-02",
        },
        follow_redirects=True,
    )
    assert "Metadados atualizados" in resposta.get_data(as_text=True)
    linha = _documento(db, documento_id)
    assert linha["titulo"] == "Ata n.º 24 (revisada)"
    assert linha["categoria"] == "Outro"
    assert linha["ata_numero"] == ""  # fora da categoria Ata, os campos de ata esvaziam
    assert linha["ata_data"] == ""


# ---------------------------------------------------- arquivo e associações ---


def test_arquivar_mantem_associacoes_e_historico(client, db):
    _carregar(client, [("contrato-a.pdf", PDF_TESTE)], titulo="Contrato fração A",
              categoria="Contrato")
    documento_id = _documento_por_nome(db, "contrato-a.pdf")["id"]
    fracao_a = db.fraction("A")["id"]

    client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "fracao", "alvo_id": str(fracao_a)},
        follow_redirects=True,
    )
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes WHERE alvo = 'fracao'") == 1

    client.post(
        f"/documentos/{documento_id}/arquivar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}")},
        follow_redirects=True,
    )
    assert _documento(db, documento_id)["arquivado"] == 1
    # A associação e o documento conservam-se; sai só da lista principal.
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes WHERE documento_id = ?",
                     (documento_id,)) == 1
    html = client.get("/documentos").get_data(as_text=True)
    assert "Contrato fração A" not in html
    assert "1 documento(s) arquivado(s)" in html
    html = client.get("/documentos?arquivados=1").get_data(as_text=True)
    assert "Contrato fração A" in html

    client.post(
        f"/documentos/{documento_id}/repor",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}")},
        follow_redirects=True,
    )
    assert _documento(db, documento_id)["arquivado"] == 0


def test_associar_a_fracao_nao_cria_movimentos(client, db):
    _carregar(client, [("seguro.pdf", PDF_TESTE)], titulo="Apólite de seguro",
              categoria="Seguro")
    documento_id = _documento_por_nome(db, "seguro.pdf")["id"]
    pagamentos_antes = db.scalar("SELECT COUNT(*) FROM payments")
    despesas_antes = db.scalar("SELECT COUNT(*) FROM expenses")

    resposta = client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "fracao", "alvo_id": str(db.fraction("B")["id"])},
        follow_redirects=True,
    )
    html = resposta.get_data(as_text=True)
    assert "Fração n.º" in html
    assert "A associação não cria nem altera movimentos financeiros." in html
    assert db.scalar("SELECT COUNT(*) FROM payments") == pagamentos_antes
    assert db.scalar("SELECT COUNT(*) FROM expenses") == despesas_antes


# ------------------------------------------------------------- versões --------


def test_nova_versao_conserva_anterior_e_herda_partilha(app, client, db):
    criar_condomino(app, db, "condomino-a", ("A",))
    _carregar(client, [("ata-25.pdf", PDF_TESTE)], titulo="Ata n.º 25", categoria="Ata",
              ata_numero="25")
    antigo = _documento_por_nome(db, "ata-25.pdf")

    # Partilhar com a fração A antes da correção.
    client.post(
        f"/documentos/{antigo['id']}/partilhar",
        data={"csrf_token": csrf_de(client, f"/documentos/{antigo['id']}"), "fracao_A": "on"},
        follow_redirects=True,
    )

    resposta = client.post(
        f"/documentos/{antigo['id']}/nova-versao",
        data={
            "csrf_token": csrf_de(client, f"/documentos/{antigo['id']}"),
            "ficheiro": (io.BytesIO(PDF_CORRIGIDO), "ata-25-corrigida.pdf"),
        },
        follow_redirects=True,
    )
    html = resposta.get_data(as_text=True)
    assert "Versão 2 guardada" in html
    assert "A versão anterior conserva-se" in html

    novo_id = db.scalar(
        "SELECT id FROM documentos WHERE nome_original = 'ata-25-corrigida.pdf'"
    )
    novo = _documento(db, novo_id)
    assert novo["versao"] == 2
    assert novo["versao_de"] == antigo["id"]
    assert novo["categoria"] == "Ata"
    assert novo["ata_numero"] == "25"

    # A anterior continua lá, marcada como antiga; a nova mostra a cadeia.
    pagina_antiga = client.get(f"/documentos/{antigo['id']}").get_data(as_text=True)
    assert "v1" in pagina_antiga and "v2" in pagina_antiga
    assert "atual" in pagina_antiga
    assert "v2 · atual" in client.get("/documentos").get_data(as_text=True)

    # A partilha herda-se: o condómino da fração A passa a ver a versão nova.
    condomino = entrar_condomino(app, "condomino-a")
    assert condomino.get(f"/documentos/{novo_id}").status_code == 200
    assert condomino.get(f"/documentos/{novo_id}/descarregar").status_code == 200


def test_nova_versao_identica_a_uma_existente_e_recusada(client, db):
    _carregar(client, [("ata-26.pdf", PDF_TESTE)], titulo="Ata n.º 26", categoria="Ata")
    documento_id = _documento_por_nome(db, "ata-26.pdf")["id"]
    resposta = client.post(
        f"/documentos/{documento_id}/nova-versao",
        data={
            "csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
            "ficheiro": (io.BytesIO(PDF_TESTE), "mesma-coisa.pdf"),
        },
        follow_redirects=True,
    )
    assert "tem de ser um ficheiro diferente" in resposta.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 1


# ------------------------------------------------- partilha com todos ---------


def test_partilha_todos_os_condominos_abre_visualizacao_e_descarga(app, client, db):
    criar_condomino(app, db, "condomino-a", ("A",))
    criar_condomino(app, db, "condomino-b", ("B",))
    _carregar(client, [("regulamento.pdf", PDF_TESTE)], titulo="Regulamento",
              categoria="Contrato")
    documento_id = _documento_por_nome(db, "regulamento.pdf")["id"]

    client.post(
        f"/documentos/{documento_id}/partilhar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "todos_condominos": "on"},
        follow_redirects=True,
    )
    assert _documento(db, documento_id)["partilha_todos"] == 1
    assert "todos os condóminos" in client.get("/documentos").get_data(as_text=True)

    for username in ("condomino-a", "condomino-b"):
        condomino = entrar_condomino(app, username)
        assert condomino.get(f"/documentos/{documento_id}").status_code == 200
        assert condomino.get(f"/documentos/{documento_id}/descarregar").status_code == 200
        html = condomino.get("/documentos/meus").get_data(as_text=True)
        assert "Regulamento" in html
        assert "partilhado" in html

    # Retirar a partilha volta a fechar tudo.
    client.post(
        f"/documentos/{documento_id}/partilhar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}")},
        follow_redirects=True,
    )
    condomino = entrar_condomino(app, "condomino-b")
    assert condomino.get(f"/documentos/{documento_id}").status_code == 404
