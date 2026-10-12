"""Testes da automatização bancária (entrega 7).

Cobrem as regras fixadas: desativada por defeito, modo «propor» como
pré-visualização, dedup comprovativo/extrato pelo hash do movimento,
movimentos legítimos iguais contam cada um, operações não executadas nunca
são registadas, associação ambígua vai para revisão, comissões só com regra
explícita, autenticação SPF+DKIM, execução única com retoma seguro, e os
registos passam pelas mesmas funções dos formulários manuais.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.automacao import (
    _autenticacao_aprovada,
    _processar_movimento,
    config_auto,
    executar_agora,
    execucao_em_curso,
)
from app.db import utc_now_iso
from conftest import csrf_de, entrar_condomino, criar_condomino


# ---------------------------------------------------------------------------
# Ajuda: montar extrações e movimentos como o ciclo os produz
# ---------------------------------------------------------------------------


def _documento(app, dados: bytes = b"%PDF-1.4 teste", nome: str = "extrato.pdf") -> int:
    with app.app_context():
        from app.documentos import guardar_documento

        return guardar_documento(
            origem="anexo", dados=dados, nome_original=nome,
            mime="application/pdf", categoria="Extrato",
        )


def _extracao(app, documento_id: int, movimentos: list[dict]) -> int:
    with app.app_context():
        from app.db import execute_db

        return execute_db(
            "INSERT INTO extracoes (documento_id, extrator, versao_extrator, campos_json, "
            "origem_json, estado, nota, criado_em) VALUES (?, 'millennium_extrato', 2, ?, "
            "'[]', 'reconhecido', '', ?)",
            (documento_id, json.dumps(movimentos), utc_now_iso()),
        )


def _movimento(**alterar) -> dict:
    base = {
        "data": "2027-01-15",
        "valor_cents": 4700,
        "moeda": "EUR",
        "sentido": "recebido",
        "executado_flag": True,
        "referencia": "REF-42",
        "contraparte": "JOAO DA SILVA",
        "descricao": "quota condominio janeiro",
    }
    base.update(alterar)
    return base


def _config(modo: str = "registar") -> dict:
    return {"ativa": True, "remetentes": ["millenniumbcp.pt"], "desde": "",
            "modo": modo, "ultima_consulta": ""}


def _resumo() -> dict:
    return {"mensagens": 0, "documentos": 0, "duplicados": 0, "extracoes": 0,
            "movimentos": 0, "registados": 0, "revisao": 0, "autenticacao_recusada": 0}


def _correspondencia(db, tipo: str, padrao: str, alvo_tipo: str, alvo_id=None,
                     alvo_texto: str = "", comissao: int = 0) -> None:
    db.execute(
        "INSERT INTO auto_correspondencias (tipo, padrao, alvo_tipo, alvo_id, alvo_texto, "
        "comissao, ativo, criado_em) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (tipo, padrao, alvo_tipo, alvo_id, alvo_texto, comissao, utc_now_iso()),
    )


def _processar(app, db, movimento: dict, config=None, documento_id=None,
               resumo=None, ocorrencia: int = 1) -> dict:
    doc_id = documento_id if documento_id is not None else _documento(app)
    extracao_id = _extracao(app, doc_id, [movimento])
    resumo_local = resumo if resumo is not None else _resumo()
    with app.app_context():
        _processar_movimento(
            movimento, ocorrencia, extracao_id, doc_id,
            {"extrator": "millennium_extrato", "versao": 2},
            config or _config(), resumo_local,
        )
    return resumo_local


# ---------------------------------------------------------------------------
# Configuração e proteção
# ---------------------------------------------------------------------------


def test_automatizacao_comeca_desativada(app, db):
    with app.app_context():
        config = config_auto()
    assert config["ativa"] is False
    assert config["modo"] == "propor"  # pré-visualização é o defeito


def test_pagina_visivel_para_admin(client, csrf):
    html = client.get("/automacao").get_data(as_text=True)
    assert "Automatização bancária" in html
    assert "Executar agora" in html


def test_condomino_nao_acede(client, app, db):
    criar_condomino(app, db, "condomino-auto")
    cond = entrar_condomino(app, "condomino-auto")
    resposta = cond.get("/automacao")
    assert resposta.status_code in (302, 403)
    assert b"Automatiza" not in resposta.data


def test_guardar_config_valida(app, client, db):
    token = csrf_de(client, "/automacao")
    client.post("/automacao/config", data={
        "csrf_token": token,
        "auto_ativa": "on",
        "auto_remetentes": "millenniumbcp.pt , EDP.pt",
        "auto_desde": "2027-01-01",
        "auto_modo": "propor",
    })
    with app.app_context():
        config = config_auto()
    assert config["ativa"] is True
    assert config["remetentes"] == ["millenniumbcp.pt", "edp.pt"]
    assert config["desde"] == "2027-01-01"


def test_guardar_config_recusa_remetente_invalido(app, client, db):
    token = csrf_de(client, "/automacao")
    client.post("/automacao/config", data={
        "csrf_token": token,
        "auto_ativa": "on",
        "auto_remetentes": "millenniumbcp.pt; injeccao",
        "auto_desde": "2027-01-01",
        "auto_modo": "propor",
    })
    with app.app_context():
        assert config_auto()["ativa"] is False  # nada guardado


def test_modo_registar_exige_correspondencia(app, client, db):
    token = csrf_de(client, "/automacao")
    resposta = client.post("/automacao/config", data={
        "csrf_token": token,
        "auto_ativa": "on",
        "auto_remetentes": "millenniumbcp.pt",
        "auto_desde": "",
        "auto_modo": "registar",
    })
    html = client.get("/automacao").get_data(as_text=True)
    assert "correspondência" in html  # mensagem de erro visível
    with app.app_context():
        assert config_auto()["modo"] == "propor"


# ---------------------------------------------------------------------------
# Registo de movimentos
# ---------------------------------------------------------------------------


def test_modo_propor_nao_regista(app, db, admin_teste):
    fracao = db.fraction("A")
    _correspondencia(db, "ordenante", "joao da silva", "fracao", fracao["id"])
    resumo = _processar(app, db, _movimento(), config=_config("propor"))
    assert resumo["registados"] == 0
    assert db.payments() == []
    linha = db.rows("SELECT estado FROM auto_movimentos")[0]
    assert linha["estado"] == "proposta"


def test_recebido_valido_regista_pagamento_com_rasto(app, db, admin_teste):
    fracao = db.fraction("A")
    _correspondencia(db, "ordenante", "joao da silva", "fracao", fracao["id"])
    resumo = _processar(app, db, _movimento())
    assert resumo["registados"] == 1
    pagamentos = db.payments()
    assert len(pagamentos) == 1
    assert pagamentos[0]["amount_cents"] == 4700
    assert pagamentos[0]["paid_on"] == "2027-01-15"
    assert pagamentos[0]["form_token"].startswith("auto:")
    assert "automático" in pagamentos[0]["source"]
    # O registo fica ligado ao documento de origem.
    ligacoes = db.rows(
        "SELECT alvo FROM documento_ligacoes WHERE alvo = 'pagamento' AND alvo_id = ?",
        (pagamentos[0]["id"],),
    )
    assert len(ligacoes) == 1
    # O dinheiro recebido não é afetado a cobranças automaticamente.
    assert db.allocations() == []


def test_dedup_comprovativo_e_extrato_conta_uma_vez(app, db, admin_teste):
    fracao = db.fraction("A")
    _correspondencia(db, "ordenante", "joao da silva", "fracao", fracao["id"])
    movimento = _movimento()
    _processar(app, db, movimento)  # comprovativo
    resumo = _processar(app, db, movimento)  # extrato, mesmo movimento
    assert resumo["duplicados"] == 1
    assert len(db.payments()) == 1


def test_movimentos_iguais_legitimos_contam_cada_um(app, db, admin_teste):
    fracao = db.fraction("A")
    _correspondencia(db, "ordenante", "joao da silva", "fracao", fracao["id"])
    movimento = _movimento()
    _processar(app, db, movimento, ocorrencia=1)
    _processar(app, db, movimento, ocorrencia=2)
    assert len(db.payments()) == 2  # dois pagamentos iguais legítimos


def test_operacao_pendente_nunca_e_registada(app, db, admin_teste):
    fracao = db.fraction("A")
    _correspondencia(db, "ordenante", "joao da silva", "fracao", fracao["id"])
    resumo = _processar(app, db, _movimento(executado_flag=False))
    assert db.payments() == []
    assert resumo["revisao"] == 1
    linha = db.rows("SELECT estado, nota FROM auto_movimentos")[0]
    assert linha["estado"] == "revisao"
    assert "não executada" in linha["nota"]


def test_campos_incompletos_vao_para_revisao_sem_adivinhar(app, db, admin_teste):
    resumo = _processar(app, db, _movimento(valor_cents=None))
    assert db.payments() == []
    assert resumo["revisao"] == 1
    linha = db.rows("SELECT nota FROM auto_movimentos")[0]
    assert "incompletos" in linha["nota"]


def test_associacao_ambigua_de_fracoes_vai_para_revisao(app, db, admin_teste):
    _correspondencia(db, "ordenante", "joao", "fracao", db.fraction("A")["id"])
    _correspondencia(db, "referencia", "REF-42", "fracao", db.fraction("B")["id"])
    resumo = _processar(app, db, _movimento())
    assert db.payments() == []
    assert resumo["revisao"] == 1
    linha = db.rows("SELECT nota FROM auto_movimentos")[0]
    assert "amb" in linha["nota"].lower() or "sem correspondência" in linha["nota"]


def test_comissao_sem_regra_explcita_nao_cria_despesa(app, db, admin_teste):
    resumo = _processar(app, db, _movimento(sentido="comissao", valor_cents=350,
                                            descricao="comissao manutencao conta"))
    assert db.expenses() == []
    assert resumo["revisao"] == 1
    linha = db.rows("SELECT nota FROM auto_movimentos")[0]
    assert "regra explícita" in linha["nota"]


def test_comissao_com_regra_explcita_cria_despesa_paga(app, db, admin_teste, client):
    from conftest import categoria_de_trabalho

    categoria_de_trabalho(client, db, "Comissões bancárias")
    _correspondencia(db, "ordenante", "millennium", "fornecedor",
                     alvo_texto="Millennium BCP", comissao=1)
    resumo = _processar(app, db, _movimento(sentido="comissao", valor_cents=350,
                                            contraparte="MILLENNIUM BCP",
                                            descricao="comissao manutencao conta"))
    assert resumo["registados"] == 1
    despesas = db.expenses()
    assert len(despesas) == 1
    assert despesas[0]["amount_cents"] == 350
    assert despesas[0]["fornecedor"] == "Millennium BCP"
    pagamentos = db.expense_payments()
    assert len(pagamentos) == 1
    assert pagamentos[0]["amount_cents"] == 350


def test_enviado_associado_a_despesa_unica(app, db, admin_teste, client):
    from conftest import registar_despesa

    registar_despesa(client, db, descricao="Limpeza do hall", valor="120,00",
                     fornecedor="Ascensul", data="15/01/2027")
    _correspondencia(db, "ordenante", "ascensul", "fornecedor", alvo_texto="Ascensul")
    resumo = _processar(app, db, _movimento(sentido="enviado", valor_cents=12000,
                                            contraparte="ASCENSUL LDA",
                                            referencia="", descricao="transferencia enviada ascensul"))
    assert resumo["registados"] == 1
    pagamentos = db.expense_payments()
    assert len(pagamentos) == 1
    assert pagamentos[0]["amount_cents"] == 12000


def test_enviado_sem_despesa_correspondente_vai_para_revisao(app, db, admin_teste):
    _correspondencia(db, "ordenante", "ascensul", "fornecedor", alvo_texto="Ascensul")
    resumo = _processar(app, db, _movimento(sentido="enviado", valor_cents=12000,
                                            contraparte="ASCENSUL LDA"))
    assert db.expense_payments() == []
    assert resumo["revisao"] == 1


# ---------------------------------------------------------------------------
# Autenticação (o endereço visível não chega)
# ---------------------------------------------------------------------------


def test_autenticacao_exige_spf_e_dkim():
    assert _autenticacao_aprovada("spf=pass; dkim=pass; dmarc=pass") is True
    assert _autenticacao_aprovada("spf=pass; dkim=fail") is False
    assert _autenticacao_aprovada("spf=fail; dkim=pass") is False
    assert _autenticacao_aprovada("") is False


# ---------------------------------------------------------------------------
# Execução única, trava e retoma
# ---------------------------------------------------------------------------


def test_execucao_em_curso_bloira_segunda(app, db):
    db.execute(
        "INSERT INTO processamentos (iniciado_em, estado, gatilho) VALUES (?, 'a_decorrer', 'manual')",
        (utc_now_iso(),),
    )
    with app.app_context():
        processamento_id, motivo = executar_agora(app)
    assert processamento_id is None
    assert "curso" in motivo


def test_execucao_presa_e_marcada_interrompida_e_o_retoma_avanca(app, db):
    presa = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.execute(
        "INSERT INTO processamentos (iniciado_em, estado, gatilho) VALUES (?, 'a_decorrer', 'manual')",
        (presa,),
    )
    with app.app_context():
        assert execucao_em_curso() is None
    linha = db.rows("SELECT estado FROM processamentos")[0]
    assert linha["estado"] == "interrompido"


def test_executar_agora_sem_gmail_nao_corre(app, db):
    with app.app_context():
        processamento_id, motivo = executar_agora(app)
    assert processamento_id is None
    assert "Gmail" in motivo
    assert db.rows("SELECT id FROM processamentos") == []


# ---------------------------------------------------------------------------
# Ciclo completo com Gmail simulado (documentos sintéticos no formato real)
# ---------------------------------------------------------------------------


def _pdf_tabela_extrato() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 30), "Millennium BCP - extrato de movimentos")
    page.insert_text((40, 60), "26/08/31")
    page.insert_text((300, 60), "Movimento: quota condominio")
    page.insert_text((340, 370), "SALDOS CREDORES")
    page.insert_text((475, 370), "SALDOS DEVEDORES")
    page.insert_text((383, 381), "4 500.00")
    dados = doc.tobytes()
    doc.close()
    return dados


def test_ciclo_completo_guarda_extrai_e_propoe(app, db, monkeypatch):
    import base64

    dados = _pdf_tabela_extrato()
    msg = {
        "id": "m1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Banco <no-reply@millenniumbcp.pt>"},
                {"name": "Authentication-Results", "value": "spf=pass; dkim=pass"},
            ],
            "parts": [{
                "mimeType": "application/octet-stream",
                "filename": "extrato_2026-08.pdf",
                "body": {"attachmentId": "att1", "size": len(dados)},
            }],
        },
    }

    def pedido_json_fake(url, **kwargs):
        if "/me/messages?" in url:
            return 200, {"messages": [{"id": "m1"}]}
        if "/me/messages/m1?" in url:
            return 200, msg
        if "/attachments/att1" in url:
            return 200, {"data": base64.urlsafe_b64encode(dados).decode()}
        raise AssertionError(f"pedido inesperado: {url}")

    monkeypatch.setattr("app.automacao.pedido_json", pedido_json_fake)
    monkeypatch.setattr("app.gmail._cabecalho_autorizacao", lambda: "token-falso")
    for chave, valor in (("gmail_refresh_token", "fake"), ("auto_ativa", "1"),
                         ("auto_remetentes", "millenniumbcp.pt"), ("auto_modo", "propor"),
                         ("auto_desde", "2026-08-01")):
        db.execute("INSERT INTO integracoes (chave, valor, atualizado_em) VALUES (?, ?, ?)",
                   (chave, valor, "2027-01-01"))

    from app.automacao import correr_ciclo_sync

    resumo = correr_ciclo_sync(app, gatilho="manual")
    assert resumo["mensagens"] == 1
    assert resumo["documentos"] == 1
    assert resumo["extracoes"] == 1
    assert resumo["movimentos"] == 1
    assert resumo["registados"] == 0  # modo propor: nada registado

    documentos = db.rows("SELECT nome_original, origem, categoria FROM documentos")
    assert len(documentos) == 1
    assert documentos[0]["nome_original"] == "extrato_2026-08.pdf"
    assert documentos[0]["origem"] == "anexo"

    movimentos = db.rows("SELECT estado, sentido FROM auto_movimentos")
    assert len(movimentos) == 1
    assert movimentos[0]["estado"] == "proposta"
    assert movimentos[0]["sentido"] == "recebido"

    # O segundo ciclo não repete nada (mensagens e movimentos já vistos).
    resumo2 = correr_ciclo_sync(app, gatilho="manual")
    assert resumo2["mensagens"] == 0
    assert resumo2["documentos"] == 0
    assert len(db.rows("SELECT id FROM documentos")) == 1
    assert len(db.payments()) == 0


def test_ciclo_recusa_email_sem_autenticacao(app, db, monkeypatch):
    import base64

    dados = _pdf_tabela_extrato()
    msg = {
        "id": "m2",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Banco <no-reply@millenniumbcp.pt>"},
                {"name": "Authentication-Results", "value": "spf=pass; dkim=fail"},
            ],
            "parts": [{
                "mimeType": "application/octet-stream",
                "filename": "falso_extrato.pdf",
                "body": {"attachmentId": "att2", "size": len(dados)},
            }],
        },
    }

    def pedido_json_fake(url, **kwargs):
        if "/me/messages?" in url:
            return 200, {"messages": [{"id": "m2"}]}
        if "/me/messages/m2?" in url:
            return 200, msg
        raise AssertionError("o anexo não pode ser descarregado sem autenticação")

    monkeypatch.setattr("app.automacao.pedido_json", pedido_json_fake)
    monkeypatch.setattr("app.gmail._cabecalho_autorizacao", lambda: "token-falso")
    for chave, valor in (("gmail_refresh_token", "fake"), ("auto_ativa", "1"),
                         ("auto_remetentes", "millenniumbcp.pt"), ("auto_modo", "registar"),
                         ("auto_desde", "2026-08-01")):
        db.execute("INSERT INTO integracoes (chave, valor, atualizado_em) VALUES (?, ?, ?)",
                   (chave, valor, "2027-01-01"))

    from app.automacao import correr_ciclo_sync

    resumo = correr_ciclo_sync(app, gatilho="manual")
    assert resumo["autenticacao_recusada"] == 1
    assert resumo["documentos"] == 0
    assert db.rows("SELECT id FROM documentos") == []
    assert db.payments() == []
    linha = db.rows("SELECT estado FROM auto_mensagens")[0]
    assert linha["estado"] == "autenticacao_recusada"


# ---------------------------------------------------------------------------
# Página de revisão
# ---------------------------------------------------------------------------


def _proposta(app, db) -> int:
    movimento = _movimento()
    _processar(app, db, movimento, config=_config("propor"))
    return int(db.rows("SELECT id FROM auto_movimentos")[0]["id"])


def test_revisao_lista_propostas(client, app, db):
    _proposta(app, db)
    html = client.get("/automacao/revisao").get_data(as_text=True)
    assert "extrato.pdf" in html
    assert "Confirmar registo" in html
    assert "Ignorar" in html


def test_revisao_confirmar_regista_pagamento(client, app, db):
    movimento_id = _proposta(app, db)
    fracao = db.fraction("A")
    token = csrf_de(client, "/automacao/revisao")
    client.post(f"/automacao/revisao/{movimento_id}/confirmar", data={
        "csrf_token": token,
        "data": "2027-01-15",
        "valor": "47,00",
        "sentido": "recebido",
        "fracao_id": str(fracao["id"]),
    })
    pagamentos = db.payments()
    assert len(pagamentos) == 1
    assert pagamentos[0]["amount_cents"] == 4700
    assert pagamentos[0]["fraction_id"] == fracao["id"]
    linha = db.rows("SELECT estado FROM auto_movimentos WHERE id = ?", (movimento_id,))[0]
    assert linha["estado"] == "registado"


def test_revisao_ignorar_nao_regista(client, app, db):
    movimento_id = _proposta(app, db)
    token = csrf_de(client, "/automacao/revisao")
    client.post(f"/automacao/revisao/{movimento_id}/ignorar", data={"csrf_token": token})
    assert db.payments() == []
    linha = db.rows("SELECT estado FROM auto_movimentos WHERE id = ?", (movimento_id,))[0]
    assert linha["estado"] == "ignorado"


def test_revisao_confirma_com_valor_corrigido(client, app, db):
    movimento_id = _proposta(app, db)
    fracao = db.fraction("A")
    token = csrf_de(client, "/automacao/revisao")
    client.post(f"/automacao/revisao/{movimento_id}/confirmar", data={
        "csrf_token": token,
        "data": "2027-01-16",
        "valor": "50,00",  # corrigido face aos 47,00 extraídos
        "sentido": "recebido",
        "fracao_id": str(fracao["id"]),
    })
    pagamentos = db.payments()
    assert len(pagamentos) == 1
    assert pagamentos[0]["amount_cents"] == 5000
    assert pagamentos[0]["paid_on"] == "2027-01-16"
