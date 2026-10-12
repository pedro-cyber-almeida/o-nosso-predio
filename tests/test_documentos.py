"""Entrega 4: documentos locais, comprovativos do condómino, associações e partilha."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    criar_condomino,
    csrf_de,
    entrar_condomino,
    protecao_de,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)

PDF_TESTE = b"%PDF-1.4 conteudo de teste"


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Os documentos correm como se hoje fosse 01/02/2027."""


def _guardar_documento(client, db, nome="fatura-42.pdf", mime="application/pdf",
                       dados=PDF_TESTE, resumo="Fatura 42 limpeza", decisao="guardado"):
    from app.documentos import guardar_documento

    with client.application.app_context():
        return guardar_documento(
            origem="anexo", dados=dados, nome_original=nome, mime=mime,
            resumo=resumo, decisao=decisao,
        )


# ------------------------------------------------- comprovativos do condómino --


def test_condomino_envia_comprovativo_pendente_que_nao_altera_saldos(app, client, db):
    regras_e_mes(client, db)
    divida_antes = db.scalar(
        "SELECT COALESCE(SUM(amount_cents),0) FROM charges WHERE due_date IS NULL"
    )

    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")
    resposta = condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "A",
            "nota": "Transferência TRF-77 de janeiro",
        },
        content_type="multipart/form-data",
        buffered=True,
    )
    # Sem ficheiro: recusado.
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 0

    resposta = condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "A",
            "nota": "Transferência TRF-77 de janeiro",
            "ficheiro": (io_bytes(PDF_TESTE), "comprovativo.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resposta.status_code == 302
    linha = db.rows("SELECT * FROM documentos")[0]
    assert linha["origem"] == "condomino"
    assert linha["decisao"] == "pendente"
    assert linha["criado_por"] == "condomino.a"

    # O envio não altera saldos: continua tudo por receber.
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0
    assert db.scalar(
        "SELECT COALESCE(SUM(amount_cents),0) FROM charges WHERE due_date IS NULL"
    ) == divida_antes


def io_bytes(dados: bytes):
    import io

    return io.BytesIO(dados)


def _csrf(client, caminho):
    import re

    html = client.get(caminho).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_comprovativo_so_pode_ser_da_fracao_do_condomino(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "B",  # não é dele
            "ficheiro": (io_bytes(PDF_TESTE), "comprovativo.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 0


def test_upload_de_tipo_nao_aceite_e_recusado(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "A",
            "ficheiro": (io_bytes(b"MZ executavel"), "script.exe"),
        },
        content_type="multipart/form-data",
    )
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 0


def test_admin_confirma_e_rejeita_com_motivo_e_nada_muda_em_saldos(app, client, db):
    documento_id = _guardar_documento(client, db, decisao="pendente")

    # Rejeitar sem motivo: recusado.
    client.post(
        f"/documentos/{documento_id}/decidir",
        data={"csrf_token": _csrf(client, f"/documentos/{documento_id}"),
              "decisao": "rejeitado", "motivo": ""},
    )
    assert db.rows("SELECT decisao FROM documentos")[0]["decisao"] == "pendente"

    client.post(
        f"/documentos/{documento_id}/decidir",
        data={"csrf_token": _csrf(client, f"/documentos/{documento_id}"),
              "decisao": "rejeitado", "motivo": "Não legível."},
    )
    linha = db.rows("SELECT * FROM documentos")[0]
    assert linha["decisao"] == "rejeitado"
    assert linha["decisao_motivo"] == "Não legível."
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0  # decidir não movimenta


# ------------------------------------------------------ ver e partilhar --------


def test_documento_nao_e_visivel_ao_condomino_sem_partilha_explícita(app, client, db):
    documento_id = _guardar_documento(client, db)
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    assert condomino.get(f"/documentos/{documento_id}").status_code == 404
    resposta_lista = condomino.get("/documentos")
    assert resposta_lista.status_code == 302  # página da administração
    assert resposta_lista.headers["Location"].startswith("/")

    # O administrador partilha explicitamente com a fração A.
    client.post(
        f"/documentos/{documento_id}/partilhar",
        data={"csrf_token": _csrf(client, f"/documentos/{documento_id}"), "fracao_A": "on"},
    )
    resposta = condomino.get(f"/documentos/{documento_id}")
    assert resposta.status_code == 200
    assert "Fatura 42 limpeza" in resposta.get_data(as_text=True)

    # A fração B, não partilhada, continua sem acesso.
    criar_condomino(app, db, "condomino.b", ("B",))
    condomino_b = entrar_condomino(app, "condomino.b")
    assert condomino_b.get(f"/documentos/{documento_id}").status_code == 404


def test_condomino_ve_os_seus_comprovativos_mas_nao_os_dos_outros(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino_a = entrar_condomino(app, "condomino.a")
    condomino_a.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino_a, "/comprovativos"),
            "fracao": "A",
            "ficheiro": (io_bytes(PDF_TESTE), "meu.pdf"),
        },
        content_type="multipart/form-data",
    )

    html = condomino_a.get("/comprovativos").get_data(as_text=True)
    assert "meu.pdf" in html

    criar_condomino(app, db, "condomino.b", ("B",))
    condomino_b = entrar_condomino(app, "condomino.b")
    html_b = condomino_b.get("/comprovativos").get_data(as_text=True)
    assert "meu.pdf" not in html_b


# ------------------------------------------------------- associações -----------


def test_associar_documento_a_pagamento_e_so_rasto_e_nao_altera_valores(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "52,00")
    pagamento = ultimo_pagamento(db)
    documento_id = _guardar_documento(client, db)

    totais_antes = db.scalar(
        "SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE cancelled_at IS NULL"
    )

    client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "pagamento", "alvo_id": str(pagamento["id"])},
    )
    ligacao = db.rows("SELECT * FROM documento_ligacoes")
    assert len(ligacao) == 1
    assert ligacao[0]["alvo"] == "pagamento"
    assert ligacao[0]["alvo_id"] == pagamento["id"]
    assert db.scalar(
        "SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE cancelled_at IS NULL"
    ) == totais_antes

    # Repetir a mesma associação não duplica.
    client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "pagamento", "alvo_id": str(pagamento["id"])},
    )
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes") == 1


def test_associar_registo_inexistente_e_recusado(app, client, db):
    documento_id = _guardar_documento(client, db)
    client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "pagamento", "alvo_id": "9999"},
    )
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes") == 0


def test_documento_ligado_nao_pode_ser_apagado_da_base(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "52,00")
    pagamento = ultimo_pagamento(db)
    documento_id = _guardar_documento(client, db)
    client.post(
        f"/documentos/{documento_id}/associar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"),
              "alvo": "pagamento", "alvo_id": str(pagamento["id"])},
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM documentos WHERE id = ?", (documento_id,))


def test_registar_pagamento_a_partir_de_documento_deixa_ligacao_e_avisa_duplicados(app, client, db):
    documento_id = _guardar_documento(client, db, resumo="Transferência TRF-77 janeiro")

    html = client.get(f"/payments/add?doc={documento_id}").get_data(as_text=True)
    assert "A registar a partir de" in html
    assert "Transferência TRF-77 janeiro" in html

    client.post(
        "/payments/add",
        data={
            **protecao_de(client, f"/payments/add?doc={documento_id}"),
            "fracao": id_fracao(db, "A"), "valor": "52,00", "data": "15/01/2027",
            "meio": "transferencia", "referencia": "TRF-77", "nota": "",
            "confirmar_pagamento": "on", "doc": str(documento_id),
        },
    )
    pagamento = ultimo_pagamento(db)
    ligacao = db.rows("SELECT * FROM documento_ligacoes")
    assert len(ligacao) == 1
    assert ligacao[0]["alvo_id"] == pagamento["id"]

    # Um segundo pagamento com a mesma referência: o formulário avisa.
    html = client.get(f"/payments/add?doc={documento_id}").get_data(as_text=True)
    assert "Possíveis duplicados" in html


def id_fracao(db, code: str) -> str:
    return str(db.fraction(code)["id"])


def test_marcar_como_tratado_nao_cria_movimento(app, client, db):
    documento_id = _guardar_documento(client, db)
    client.post(
        f"/documentos/{documento_id}/tratado",
        data={"csrf_token": _csrf(client, f"/documentos/{documento_id}")},
    )
    linha = db.rows("SELECT tratado_em FROM documentos")[0]
    assert linha["tratado_em"] is not None
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0
    assert db.scalar("SELECT COUNT(*) FROM charges") == 0


def test_extrato_guardado_nao_cria_despesa(app, client, db):
    """Consultar/guardar um extrato bancário não cria despesas nem pagamentos."""
    _guardar_documento(client, db, nome="extrato-janeiro.pdf",
                       resumo="Extrato bancário janeiro")
    assert db.scalar("SELECT COUNT(*) FROM expenses") == 0
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0


# --------------------------------------------- Entrega 5: os meus documentos --


def test_condomino_ve_na_sua_pagina_o_que_foi_partilhado_com_a_sua_fracao(app, client, db):
    documento_id = _guardar_documento(client, db, resumo="Regulamento aprovado")
    criar_condomino(app, db, "condomino.a", ("A",))
    criar_condomino(app, db, "condomino.b", ("B",))
    condomino_a = entrar_condomino(app, "condomino.a")
    condomino_b = entrar_condomino(app, "condomino.b")

    # Nada é partilhado automaticamente.
    html = condomino_a.get("/documentos/meus").get_data(as_text=True)
    assert "Ainda não tens documentos" in html

    client.post(
        f"/documentos/{documento_id}/partilhar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"), "fracao_A": "on"},
    )
    html_a = condomino_a.get("/documentos/meus").get_data(as_text=True)
    assert "Regulamento aprovado" in html_a
    assert "partilhado" in html_a
    html_b = condomino_b.get("/documentos/meus").get_data(as_text=True)
    assert "Regulamento aprovado" not in html_b


def test_comprovativos_proprios_aparecem_na_lista_com_o_estado(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")
    condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "A",
            "nota": "quota de janeiro",
            "ficheiro": (io_bytes(PDF_TESTE), "comprovativo-jan.pdf"),
        },
        content_type="multipart/form-data",
    )

    html = condomino.get("/documentos/meus").get_data(as_text=True)
    assert "comprovativo-jan.pdf" in html
    assert "à espera de confirmação" in html


def test_admin_e_encaminhado_dos_meus_documentos_para_a_lista_completa(app, client, db):
    resposta = client.get("/documentos/meus")
    assert resposta.status_code == 302
    assert "documentos" in resposta.headers["Location"]


def test_painel_do_condomino_mostra_comprovativos_pendentes_e_partilhados(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    # Sem nada: a secção não aparece.
    html = condomino.get("/").get_data(as_text=True)
    assert "comprovativo(s) à espera" not in html
    assert "documento(s) partilhado" not in html

    condomino.post(
        "/comprovativos",
        data={
            "csrf_token": _csrf(condomino, "/comprovativos"),
            "fracao": "A",
            "nota": "quota de janeiro",
            "ficheiro": (io_bytes(PDF_TESTE), "comprovativo-jan.pdf"),
        },
        content_type="multipart/form-data",
    )
    documento_id = _guardar_documento(client, db, resumo="Regulamento aprovado")
    client.post(
        f"/documentos/{documento_id}/partilhar",
        data={"csrf_token": csrf_de(client, f"/documentos/{documento_id}"), "fracao_A": "on"},
    )

    html = condomino.get("/").get_data(as_text=True)
    assert "1 comprovativo(s) à espera de confirmação" in html
    assert "1 documento(s) partilhado" in html
