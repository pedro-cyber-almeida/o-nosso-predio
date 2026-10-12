"""Geração mensal: pré-visualização, totais das regras, idempotência e transação."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (
    alterar_config,
    configurar_regras_de_teste,
    gerar_mes,
    previsualizar,
    protecao_de,
    registar_regra,
)


def _total(db, rubrica: str | None = None, period: str | None = None) -> int:
    sql = "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM charges WHERE 1 = 1"
    params: list = []
    if rubrica:
        sql += " AND rubric = ?"
        params.append(rubrica)
    if period:
        sql += " AND period = ?"
        params.append(period)
    return int(db.scalar(sql, tuple(params)))


def _numero(db, period: str | None = None) -> int:
    if period:
        return int(db.scalar("SELECT COUNT(*) FROM charges WHERE period = ?", (period,)))
    return int(db.scalar("SELECT COUNT(*) FROM charges"))


def test_previsualizacao_de_janeiro_de_2027_mostra_340_ordinarios_e_24_de_reforco(
    app, client, db
):
    configurar_regras_de_teste(client, db)
    html = previsualizar(client, "2027-01")

    assert "340,00 EUR" in html, "quota ordinária esperada: 4x40 + 4x45 = 340 EUR"
    assert "24,00 EUR" in html, "reforço esperado: 8x3 = 24 EUR"
    assert "364,00 EUR" in html
    assert _numero(db) == 0, "pré-visualizar não pode gravar cobranças"


def test_confirmar_janeiro_de_2027_gera_dezasseis_cobrancas_com_os_totais_das_regras(
    app, client, db
):
    configurar_regras_de_teste(client, db)
    resposta = gerar_mes(client, "2027-01")

    assert resposta.status_code == 302
    assert _numero(db) == 16
    assert _total(db, "quota_ordinaria", "2027-01") == 34000
    assert _total(db, "reforco_fundo", "2027-01") == 2400
    assert _total(db, None, "2027-01") == 36400

    valores = {c["fraction_code"]: c["amount_cents"] for c in db.charges("2027-01")
               if c["rubric"] == "quota_ordinaria"}
    assert valores == {"A": 4000, "C": 4000, "E": 4000, "G": 4000,
                       "B": 4500, "D": 4500, "F": 4500, "H": 4500}


def test_cada_fracao_recebe_a_cobranca_da_sua_regra_nao_apenas_o_total_do_mes(
    app, client, db
):
    """A correspondência fração a fração das regras tem de chegar às cobranças."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")

    esperada = {
        "A": {"quota_ordinaria": 4000, "reforco_fundo": 300},
        "C": {"quota_ordinaria": 4000, "reforco_fundo": 300},
        "E": {"quota_ordinaria": 4000, "reforco_fundo": 300},
        "G": {"quota_ordinaria": 4000, "reforco_fundo": 300},
        "B": {"quota_ordinaria": 4500, "reforco_fundo": 300},
        "D": {"quota_ordinaria": 4500, "reforco_fundo": 300},
        "F": {"quota_ordinaria": 4500, "reforco_fundo": 300},
        "H": {"quota_ordinaria": 4500, "reforco_fundo": 300},
    }

    recebidas: dict[str, dict[str, int]] = {}
    for cobranca in db.charges("2027-01"):
        por_rubrica = recebidas.setdefault(cobranca["fraction_code"], {})
        assert cobranca["rubric"] not in por_rubrica, (
            "uma fração recebeu a mesma rubrica duas vezes no mesmo mês"
        )
        por_rubrica[cobranca["rubric"]] = cobranca["amount_cents"]

    assert recebidas == esperada
    assert sum(v["quota_ordinaria"] for v in recebidas.values()) == 34000
    assert sum(v["reforco_fundo"] for v in recebidas.values()) == 2400


def test_gerar_o_mesmo_mes_outra_vez_nao_cria_duplicados(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    primeira = db.charges("2027-01")

    gerar_mes(client, "2027-01")
    segunda = db.charges("2027-01")

    assert segunda == primeira, "repetir o mês criou ou alterou cobranças"
    assert _numero(db, "2027-01") == 16
    assert _total(db, None, "2027-01") == 36400


def test_previsualizacao_de_um_mes_ja_gerado_marca_as_cobrancas_como_emitidas(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    html = previsualizar(client, "2027-01")

    assert html.count(">já emitida<") == 16
    assert html.count(">nova<") == 0


def test_janeiro_de_2028_tem_340_ordinarios_e_nenhum_reforco(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2028-01")

    assert _numero(db, "2028-01") == 8, "só a quota ordinária: o reforço terminou em 12/2027"
    assert _total(db, "quota_ordinaria", "2028-01") == 34000
    assert _total(db, "reforco_fundo", "2028-01") == 0


def test_dezembro_de_2027_e_o_ultimo_mes_com_reforco(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-12")

    assert _numero(db, "2027-12") == 16
    assert _total(db, "reforco_fundo", "2027-12") == 2400


def test_gerar_mes_sem_regra_confirmada_e_recusado(app, client, db):
    configurar_regras_de_teste(client, db)
    resposta = gerar_mes(client, "2026-12", app=app)

    assert resposta.status_code == 302
    assert _numero(db) == 0, "não se gera um mês sem regra com vigência confirmada"
    html = client.get("/quotas/generate?period=2026-12").get_data(as_text=True)
    assert "não há regra de quota ordinária com vigência confirmada" in html
    assert "Regista a regra em Quotas" in html, (
        "a mensagem tem de explicar que falta registar a regra com a data confirmada"
    )


def test_regra_antiga_confirmada_passa_a_gerar_os_meses_anteriores(app, client, db):
    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "30,00", "01/2020")
    registar_regra(client, db, ["B", "D", "F", "H"], "quota_ordinaria", "32,00", "01/2020")

    gerar_mes(client, "2026-12")

    assert _numero(db, "2026-12") == 8
    assert _total(db, "quota_ordinaria", "2026-12") == 24800
    assert _total(db, "reforco_fundo", "2026-12") == 0


def test_gerar_sem_confirmacao_explícita_nao_grava(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01", confirmar=False)
    assert _numero(db) == 0


def test_mes_anterior_a_data_de_inicio_da_gestao_e_recusado(app, client, db):
    alterar_config(client, db, gestao_data_inicio="01/10/2026")
    configurar_regras_de_teste(client, db)

    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "30,00", "01/2020", "12/2026")
    registar_regra(client, db, ["B", "D", "F", "H"], "quota_ordinaria", "32,00", "01/2020", "12/2026")

    gerar_mes(client, "2026-09", app=app)
    assert _numero(db, "2026-09") == 0
    assert "anterior à data de início da gestão" in client.get(
        "/quotas/generate?period=2026-09"
    ).get_data(as_text=True)

    gerar_mes(client, "2026-10")
    assert _numero(db, "2026-10") == 8


def test_fracao_desativada_nao_recebe_cobranca(app, client, db):
    configurar_regras_de_teste(client, db)
    id_h = db.fraction("H")["id"]
    client.post(
        f"/fractions/{id_h}/edit",
        data={**protecao_de(client, f"/fractions/{id_h}"), "code": "H", "active": ""},
    )

    gerar_mes(client, "2027-01")

    codigos = {c["fraction_code"] for c in db.charges("2027-01")}
    assert "H" not in codigos
    assert _numero(db, "2027-01") == 14
    assert _total(db, "quota_ordinaria", "2027-01") == 29500  # 340 - 45


GATILHO_FALHA_H = """
CREATE TRIGGER falha_na_cobranca_de_H BEFORE INSERT ON charges
WHEN NEW.fraction_id = (SELECT id FROM fractions WHERE code = 'H')
BEGIN
    SELECT RAISE(ABORT, 'falha simulada ao gerar a cobrança de H');
END;
"""


def test_falha_a_meio_da_geracao_nao_deixa_metade_do_mes_gravado(app, client, db):
    """Uma geração é uma transação: ou fica o mês inteiro, ou não fica nada."""
    configurar_regras_de_teste(client, db)
    db.execute(GATILHO_FALHA_H)

    resposta = gerar_mes(client, "2027-01")

    assert _numero(db) == 0, "a transação falhada deixou cobranças gravadas"
    assert "Nenhuma cobrança foi gravada" in client.get("/quotas/generate?period=2027-01").get_data(
        as_text=True
    )
    assert resposta.status_code == 302


def test_dois_pedidos_de_geracao_iguais_nao_duplicam_o_mes(app, client, db):
    configurar_regras_de_teste(client, db)
    protecao = protecao_de(client, "/quotas/generate?period=2027-01")

    primeira = client.post("/quotas/generate", data={**protecao, "period": "2027-01",
                                                     "confirmar_geracao": "on"})
    segunda = client.post("/quotas/generate", data={**protecao, "period": "2027-01",
                                                    "confirmar_geracao": "on"})

    assert primeira.status_code == 302
    assert _numero(db, "2027-01") == 16
    assert _total(db, None, "2027-01") == 36400
    assert segunda.status_code == 302
    assert _numero(db, "2027-01") == 16, "o segundo pedido duplicou cobranças"


def test_previsualizacao_envelhecida_nao_pode_ser_confirmada(app, client, db):
    """Se o estado muda entre ver a pré-visualização e confirmar, a geração é recusada."""
    configurar_regras_de_teste(client, db)
    protecao = protecao_de(client, "/quotas/generate?period=2027-01")

    # Entre ver e confirmar, o administrador define o dia de vencimento.
    alterar_config(client, db, quota_dia_vencimento="15")

    client.post("/quotas/generate", data={**protecao, "period": "2027-01",
                                          "confirmar_geracao": "on"})

    assert _numero(db) == 0, "a geração com pré-visualização envelhecida não devia gravar"
    assert "desatualizada" in client.get("/quotas/generate?period=2027-01").get_data(as_text=True)


def test_regra_futura_nao_reescreve_as_cobrancas_ja_emitidas(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    janeiro = {c["fraction_code"]: c for c in db.charges("2027-01")
               if c["rubric"] == "quota_ordinaria"}
    assert janeiro["A"]["amount_cents"] == 4000

    # Nova decisão: a partir de julho a fração A passa a pagar 55 euros.
    regra_a = next(r for r in db.rules() if r["fraction_code"] == "A"
                   and r["rubric"] == "quota_ordinaria")
    client.post(
        f"/quotas/{regra_a['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "06/2027"},
    )
    registar_regra(client, db, ["A"], "quota_ordinaria", "48,00", "07/2027")

    gerar_mes(client, "2027-07")

    julho = {c["fraction_code"]: c for c in db.charges("2027-07")
             if c["rubric"] == "quota_ordinaria"}
    assert julho["A"]["amount_cents"] == 4800
    assert julho["B"]["amount_cents"] == 4500

    janeiro_depois = {c["fraction_code"]: c for c in db.charges("2027-01")
                      if c["rubric"] == "quota_ordinaria"}
    assert janeiro_depois == janeiro, "a regra nova alterou uma cobrança histórica"


def test_cobrancas_de_reforco_antiguas_continuam_no_historico_depois_de_a_regra_terminar(
    app, client, db
):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-03")
    reforcos_de_2027 = _total(db, "reforco_fundo", "2027-03")

    gerar_mes(client, "2028-01")

    assert _total(db, "reforco_fundo", "2028-01") == 0
    assert _total(db, "reforco_fundo", "2027-03") == reforcos_de_2027 == 2400
    assert _numero(db, "2027-03") == 16


def test_cobrancas_emitidas_nao_podem_ser_alteradas_nem_apagadas_na_base(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    id_cobranca = db.charges("2027-01")[0]["id"]

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE charges SET amount_cents = 1 WHERE id = ?", (id_cobranca,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM charges WHERE id = ?", (id_cobranca,))

    assert int(db.charges("2027-01")[0]["amount_cents"]) != 1
    assert _numero(db, "2027-01") == 16


def test_repetir_o_mes_nao_anuncia_valores_como_se_fossem_gerados_agora(app, client, db):
    """A mensagem de sucesso fala do que foi gravado, não do total do mês."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    html = client.get("/charges/2027-01").get_data(as_text=True)
    assert "16 cobrança(s) gerada(s)" in html

    protecao = protecao_de(client, "/quotas/generate?period=2027-01")
    client.post("/quotas/generate", data={**protecao, "period": "2027-01",
                                          "confirmar_geracao": "on"})
    html = client.get("/charges/2027-01").get_data(as_text=True)

    assert "0 cobrança(s) gerada(s)" in html
    assert "0,00 EUR de quota ordinária" in html
    assert "16 já existiam" in html
    assert _numero(db, "2027-01") == 16


def test_encerrar_uma_regra_abaixo_de_um_mes_ja_cobrado_e_recusado(app, client, db):
    """A vigência não pode encolher para deixar cobranças emitidas fora da regra."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-03")
    regra = next(r for r in db.rules() if r["fraction_code"] == "A"
                 and r["rubric"] == "quota_ordinaria")

    client.post(
        f"/quotas/{regra['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "02/2027"},
    )
    assert next(r for r in db.rules() if r["id"] == regra["id"])["valid_to"] is None, (
        "a vigência não pode encolher para antes de um mês já cobrado"
    )
    assert "fora do período desta regra" in client.get("/quotas").get_data(as_text=True)

    client.post(
        f"/quotas/{regra['id']}/vigencia",
        data={**protecao_de(client, "/quotas"), "fim": "03/2027"},
    )
    assert next(r for r in db.rules() if r["id"] == regra["id"])["valid_to"] == "2027-03-31"
    assert _numero(db, "2027-03") == 16


def test_data_de_inicio_da_gestao_invalida_uma_previsualizacao_antiga(app, client, db):
    """Mudar a data de início entre pré-visualizar e confirmar recusa a geração."""
    alterar_config(client, db, gestao_data_inicio="01/10/2026")
    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "30,00", "01/2020", "12/2026")
    registar_regra(client, db, ["B", "D", "F", "H"], "quota_ordinaria", "32,00", "01/2020", "12/2026")

    # A pré-visualização está bloqueada, mas mostra a revisão do estado atual.
    protecao = protecao_de(client, "/quotas/generate?period=2026-09")
    assert _numero(db, "2026-09") == 0

    # Entretanto o administrador corrige a data de início da gestão.
    alterar_config(client, db, gestao_data_inicio="01/01/2020")

    client.post("/quotas/generate", data={**protecao, "period": "2026-09",
                                          "confirmar_geracao": "on"})
    assert _numero(db, "2026-09") == 0, "a geração usou um plano que nunca foi mostrado"
    assert "desatualizada" in client.get("/quotas/generate?period=2026-09").get_data(as_text=True)


def test_regra_que_originou_cobrancas_nao_pode_ser_eliminada(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-01")
    regra = next(r for r in db.rules() if r["fraction_code"] == "A"
                 and r["rubric"] == "quota_ordinaria")

    client.post(
        f"/quotas/{regra['id']}/delete",
        data={**protecao_de(client, "/quotas"), "confirmar_eliminar": "on"},
    )

    assert len(db.rules()) == 16, "a regra com cobranças emitidas não pode desaparecer"
    assert "não pode ser eliminada" in client.get("/quotas").get_data(as_text=True)


@pytest.mark.parametrize("period", ["2027-01", "2027-06", "2027-12"])
def test_todos_os_meses_de_2027_repetem_os_mesmos_totais_das_regras(app, client, db, period):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, period)

    assert _total(db, "quota_ordinaria", period) == 34000
    assert _total(db, "reforco_fundo", period) == 2400
