"""Configuração: datas, dia de vencimento, saldos em cêntimos, 'por confirmar' e concorrência."""

from __future__ import annotations

import re

import pytest

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')


def _campos_de_protecao(html: str) -> dict[str, str]:
    """Token CSRF e revisões de concorrência tal como a página os apresenta."""
    match = CSRF_RE.search(html)
    assert match, "sem token CSRF na página"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, "sem revisão de concorrência na página"
    return {"csrf_token": match.group(1), **campos}


def formulario_config(client, db, **alteracoes) -> dict:
    """Formulário completo com os valores atuais, mais as alterações pedidas."""
    html = client.get("/config").get_data(as_text=True)
    valores = db.settings()
    form = {
        "condominio_nome": valores["condominio_nome"],
        "condominio_morada": valores["condominio_morada"],
        "dividas_abertura_notas": valores["dividas_abertura_notas"],
        "gestao_data_inicio": valores["gestao_data_inicio"],
        "quota_dia_vencimento": valores["quota_dia_vencimento"],
        "saldo_inicial_caixa_centimos": valores["saldo_inicial_caixa_centimos"],
        "saldo_inicial_fundo_centimos": valores["saldo_inicial_fundo_centimos"],
    }
    form.update(_campos_de_protecao(html))
    form.update(alteracoes)
    return form


def celula(html: str, rotulo: str) -> str:
    match = re.search(
        rf'<th scope="row">{rotulo}</th>\s*<td>(.*?)</td>', html, re.S
    )
    assert match, f"célula '{rotulo}' não encontrada na página"
    return match.group(1)


def valor_input(html: str, nome: str) -> str:
    """Valor que o navegador voltaria a enviar para esse campo."""
    match = re.search(rf'name="{nome}"[^>]*?value="([^"]*)"', html, re.S)
    assert match, f"campo '{nome}' não encontrado na página"
    return match.group(1)


def test_saldos_vazios_aparecem_como_por_confirmar_e_nao_como_zero(app, client):
    html = client.get("/config").get_data(as_text=True)

    assert celula(html, "Saldo inicial de caixa") == "por confirmar"
    assert celula(html, "Saldo inicial do fundo") == "por confirmar"
    assert celula(html, "Data de início da gestão") == "por confirmar"
    assert celula(html, "Dia de vencimento") == "por confirmar"
    assert "0,00 EUR" not in html


def test_valor_monetario_valido_fica_em_centimos_inteiros(app, client, db):
    resposta = client.post(
        "/config",
        data=formulario_config(
            client, db, saldo_inicial_caixa_centimos="1.234,56", saldo_inicial_fundo_centimos="40"
        ),
    )

    assert resposta.status_code == 302
    valores = db.settings()
    assert valores["saldo_inicial_caixa_centimos"] == "123456"
    assert valores["saldo_inicial_fundo_centimos"] == "4000"

    html = client.get("/config").get_data(as_text=True)
    assert celula(html, "Saldo inicial de caixa") == "1.234,56 EUR"
    assert celula(html, "Saldo inicial do fundo") == "40,00 EUR"


@pytest.mark.parametrize("valor", ["-40,00", "-1", "-0,01"])
def test_valor_monetario_negativo_e_rejeitado_e_nada_e_guardado(app, client, db, valor):
    antes = db.settings()

    client.post(
        "/config",
        data=formulario_config(client, db, saldo_inicial_caixa_centimos=valor),
    )

    assert db.settings()["saldo_inicial_caixa_centimos"] == ""
    assert db.settings() == antes
    assert celula(client.get("/config").get_data(as_text=True), "Saldo inicial de caixa") == (
        "por confirmar"
    )


@pytest.mark.parametrize("dia", ["0", "29", "31", "-1", "abc", "12,5", "1.5", "²"])
def test_dia_de_vencimento_fora_de_1_a_28_e_rejeitado(app, client, db, dia):
    """`²` passa em `str.isdigit()` mas `int()` recusa: tem de ser rejeitado, não 300."""
    client.post("/config", data=formulario_config(client, db, quota_dia_vencimento=dia))
    assert db.settings()["quota_dia_vencimento"] == ""


@pytest.mark.parametrize("dia", ["1", "15", "28"])
def test_dia_de_vencimento_valido_e_guardado(app, client, db, dia):
    client.post("/config", data=formulario_config(client, db, quota_dia_vencimento=dia))
    assert db.settings()["quota_dia_vencimento"] == str(int(dia))
    assert celula(client.get("/config").get_data(as_text=True), "Dia de vencimento") == str(int(dia))


def test_data_de_inicio_valida_fica_em_iso_e_aparece_em_dia_mes_ano(app, client, db):
    client.post("/config", data=formulario_config(client, db, gestao_data_inicio="01/01/2027"))

    assert db.settings()["gestao_data_inicio"] == "2027-01-01"
    assert celula(client.get("/config").get_data(as_text=True), "Data de início da gestão") == (
        "01/01/2027"
    )


@pytest.mark.parametrize("data", ["31/02/2027", "abc", "2027-13-01", "99/99/9999"])
def test_data_invalida_rejeita_a_submissao_inteira_sem_guardar_nada(app, client, db, data):
    antes = db.settings()

    resposta = client.post(
        "/config",
        data=formulario_config(
            client,
            db,
            condominio_nome="Nome Novo Confirmado",
            gestao_data_inicio=data,
            saldo_inicial_caixa_centimos="100,00",
        ),
    )

    assert resposta.status_code == 200
    assert db.settings() == antes, "uma submissão inválida não pode guardar parte dos dados"


def test_config_valida_guarda_textos_e_nao_altera_fracoes(app, client, db):
    permilagens_antes = db.permilagens()

    client.post(
        "/config",
        data=formulario_config(
            client,
            db,
            condominio_nome="Prédio da Rua Alta",
            condominio_morada="Rua Alta 12, Lisboa",
            dividas_abertura_notas="Por confirmar com o administrador anterior.",
        ),
    )

    valores = db.settings()
    assert valores["condominio_nome"] == "Prédio da Rua Alta"
    assert valores["condominio_morada"] == "Rua Alta 12, Lisboa"
    assert valores["dividas_abertura_notas"] == "Por confirmar com o administrador anterior."
    assert db.permilagens() == permilagens_antes


def test_texto_longo_e_aceito_sem_cortar_a_configuracao(app, client, db):
    notas = "Nota de teste. " * 50
    client.post("/config", data=formulario_config(client, db, dividas_abertura_notas=notas))
    assert db.settings()["dividas_abertura_notas"] == notas.strip()


GATILHO_FALHA = """
CREATE TRIGGER falha_simulada BEFORE INSERT ON settings
WHEN NEW.key = 'saldo_inicial_fundo_centimos'
BEGIN
    SELECT RAISE(ABORT, 'falha simulada na gravação da configuração');
END;
"""


def test_set_settings_em_transacao_reverte_tudo_se_a_gravacao_falhar(app, db):
    """Uma falha a meio não pode deixar metade da configuração gravada."""
    import sqlite3

    from app.db import get_db, set_settings

    antes = db.settings()
    alteracoes = {
        "condominio_nome": "Nome Gravado Antes Da Falha",
        "condominio_morada": "Morada Gravada Antes Da Falha",
        "saldo_inicial_fundo_centimos": "30000",
    }

    db.execute(GATILHO_FALHA)
    with app.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            set_settings(alteracoes)
        get_db().rollback()

    assert db.settings() == antes, "a transação falhada deixou alterações parciais"


def test_falha_na_gravacao_da_configuracao_nao_deixa_alteracoes_parciais(app, client, db):
    antes = db.settings()

    db.execute(GATILHO_FALHA)
    resposta = client.post(
        "/config",
        data=formulario_config(
            client,
            db,
            condominio_nome="Nome Novo",
            condominio_morada="Rua Nova 12",
            saldo_inicial_fundo_centimos="300,00",
        ),
    )

    html = resposta.get_data(as_text=True)
    assert resposta.status_code == 200
    assert "Nenhuma alteração foi aplicada" in html
    assert db.settings() == antes


def test_input_de_saldo_mostra_euros_e_nao_a_representacao_interna(app, client, db):
    client.post(
        "/config",
        data=formulario_config(
            client, db, saldo_inicial_caixa_centimos="1.234,56", saldo_inicial_fundo_centimos="300,00"
        ),
    )

    html = client.get("/config").get_data(as_text=True)
    assert valor_input(html, "saldo_inicial_caixa_centimos") == "1.234,56"
    assert valor_input(html, "saldo_inicial_fundo_centimos") == "300,00"


def test_reabrir_e_guardar_a_configuracao_nao_altera_os_saldos(app, client, db):
    """Reenviar exatamente o que a página mostra não pode voltar a multiplicar por 100."""
    client.post(
        "/config",
        data=formulario_config(
            client, db, saldo_inicial_caixa_centimos="1.234,56", saldo_inicial_fundo_centimos="300,00"
        ),
    )
    primeiro = db.settings()

    html = client.get("/config").get_data(as_text=True)
    caixa = valor_input(html, "saldo_inicial_caixa_centimos")
    fundo = valor_input(html, "saldo_inicial_fundo_centimos")

    client.post(
        "/config",
        data=formulario_config(
            client, db, saldo_inicial_caixa_centimos=caixa, saldo_inicial_fundo_centimos=fundo
        ),
    )

    assert db.settings() == primeiro, "regravar a configuração alterou os saldos confirmados"


def test_nome_do_condominio_pode_ficar_por_definir(app, client, db):
    """Guardar saldos ou datas não depende do nome: ele pode ficar em branco."""
    resposta = client.post(
        "/config",
        data=formulario_config(
            client, db, condominio_nome="   ", gestao_data_inicio="01/01/2027"
        ),
    )

    assert resposta.status_code == 302
    assert db.settings()["condominio_nome"] == ""
    assert db.settings()["gestao_data_inicio"] == "2027-01-01"


def test_valor_corrompido_na_base_aparece_como_por_confirmar_em_vez_de_falhar(app, client, db):
    """Um valor escrito à mão na base não pode fazer as páginas falharem."""
    db.execute("UPDATE settings SET value = 'doze mil' WHERE key = 'saldo_inicial_caixa_centimos'")

    resposta = client.get("/config")
    assert resposta.status_code == 200
    assert celula(resposta.get_data(as_text=True), "Saldo inicial de caixa") == "por confirmar"
    assert client.get("/").status_code == 200


def test_formulario_desatualizado_nao_apaga_valores_mais_recentes(app, client, db):
    """Duas versões do mesmo formulário: a segunda não pode sobrescrever a primeira."""
    html = client.get("/config").get_data(as_text=True)
    protecao = _campos_de_protecao(html)
    valores_iniciais = db.settings()

    # Versão 1 do formulário: guarda a morada.
    primeira = dict(valores_iniciais)
    primeira["condominio_morada"] = "Rua Guardada Primeiro"
    primeira.update(protecao)
    resposta = client.post("/config", data=primeira)
    assert resposta.status_code == 302
    assert db.settings()["condominio_morada"] == "Rua Guardada Primeiro"

    # Versão 2, aberta antes da primeira: tenta limpar a morada e o nome.
    segunda = dict(valores_iniciais)
    segunda["condominio_morada"] = ""
    segunda["condominio_nome"] = "Nome De Uma Página Velha"
    segunda.update(protecao)
    resposta = client.post("/config", data=segunda)

    assert resposta.status_code == 302
    assert db.settings()["condominio_morada"] == "Rua Guardada Primeiro", (
        "um formulário desatualizado apagou um valor mais recente"
    )
    assert db.settings()["condominio_nome"] == valores_iniciais["condominio_nome"]
    assert "desatualizada" in client.get("/config").get_data(as_text=True)


def test_formulario_desatualizado_nao_altera_nada_na_base(app, client, db):
    antes = db.settings()
    protecao = _campos_de_protecao(client.get("/config").get_data(as_text=True))

    # Move a base para outro estado, com outra revisão.
    client.post("/config", data=formulario_config(client, db, condominio_nome="Estado Recente"))

    resposta = client.post(
        "/config",
        data={**antes, "condominio_nome": "Estado Antigo", **protecao},
    )

    assert resposta.status_code == 302
    assert db.settings() == {**antes, "condominio_nome": "Estado Recente"}
