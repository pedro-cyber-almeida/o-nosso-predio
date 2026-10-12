"""Bateria independente das invariantes financeiras da fase 3.

Estes testes não repetem os cenários de `test_pagamentos.py`, `test_afetacoes.py`,
`test_creditos.py`, `test_anulacoes.py` nem `test_saldos.py`. Partem das regras
declaradas em REQUISITOS.md e calculam os valores esperados por SQL próprio,
diretamente sobre as tabelas; depois comparam com o que a aplicação apresenta.
Quando os dois não batem, a invariante está a ser violada.

Invariantes verificadas:
1. conservação do dinheiro: recebido = afetado + crédito e dívida = emitido - afetado,
   com afetado <= recebido, crédito >= 0 e dívida >= 0;
2. um pagamento conta uma única vez como receita, qualquer que seja a distribuição;
3. as afetações não excedem o pagamento nem a dívida de cada cobrança;
4. anular reverte exatamente os efeitos desse pagamento, nem mais nem menos;
5. a receita não pode ser duplicada, nem por reenvio do formulário, nem por escrita
   direta na base;
6. uma transação recusada não deixa alterações parciais;
7. duas janelas abertas sobre o mesmo crédito: o total afetado não excede o pagamento;
8. dívida vencida só existe quando a cobrança tem `due_date` já passado;
9. quota ordinária e reforço do fundo nunca entram misturados num total.
"""

from __future__ import annotations

import random
import sqlite3
import threading

import pytest

from conftest import (
    ADMIN_TESTE,
    afetar_pagamento,
    alterar_config,
    anular_pagamento,
    configurar_regras_de_teste,
    entrar,
    gerar_mes,
    id_da_cobranca,
    liquidado_da_cobranca,
    protecao_de,
    registar_pagamento,
    registar_regra,
    regras_e_mes,
    ultimo_pagamento,
)

from app.payments import _estado_das_cobrancas, _totais

FRACOES = list("ABCDEFGH")


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Tudo corre como se hoje fosse 01/02/2027: as datas não dependem do dia real."""


# ---------------------------------------------------------------------------
# Contas feitas à margem da aplicação, só a partir das tabelas
# ---------------------------------------------------------------------------


def _soma(db, sql: str, params: tuple = ()) -> int:
    return int(db.scalar(sql, params) or 0)


def _recebido(db, code: str | None = None) -> int:
    if code is None:
        return _soma(
            db,
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NULL",
        )
    return _soma(
        db,
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments "
        "WHERE cancelled_at IS NULL AND fraction_id = ?",
        (db.fraction(code)["id"],),
    )


def _anulado(db, code: str | None = None) -> int:
    if code is None:
        return _soma(
            db,
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE cancelled_at IS NOT NULL",
        )
    return _soma(
        db,
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments "
        "WHERE cancelled_at IS NOT NULL AND fraction_id = ?",
        (db.fraction(code)["id"],),
    )


def _emitido(db, code: str | None = None, rubrica: str | None = None) -> int:
    sql = "SELECT COALESCE(SUM(amount_cents), 0) FROM charges WHERE 1 = 1"
    params: list = []
    if code is not None:
        sql += " AND fraction_id = ?"
        params.append(db.fraction(code)["id"])
    if rubrica is not None:
        sql += " AND rubric = ?"
        params.append(rubrica)
    return _soma(db, sql, tuple(params))


def _afetado(db, code: str | None = None) -> int:
    filtro = "" if code is None else "AND p.fraction_id = ?"
    params: tuple = () if code is None else (db.fraction(code)["id"],)
    return _soma(
        db,
        f"""
        SELECT COALESCE(SUM(a.amount_cents), 0)
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        WHERE p.cancelled_at IS NULL {filtro}
        """,
        params,
    )


def _ao_fundo(db, code: str | None = None) -> int:
    filtro = "" if code is None else "AND p.fraction_id = ?"
    params: tuple = () if code is None else (db.fraction(code)["id"],)
    return _soma(
        db,
        f"""
        SELECT COALESCE(SUM(a.amount_cents), 0)
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        JOIN charges AS c ON c.id = a.charge_id
        WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo' {filtro}
        """,
        params,
    )


def _afetado_do_pagamento(db, payment_id: int) -> int:
    return _soma(
        db,
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_allocations WHERE payment_id = ?",
        (payment_id,),
    )


def _credito_do_pagamento(db, payment_id: int) -> int:
    linha = next(p for p in db.payments() if p["id"] == payment_id)
    return int(linha["amount_cents"]) - _afetado_do_pagamento(db, payment_id)


def _pagamento(db, payment_id: int) -> dict:
    linha = next((p for p in db.payments() if p["id"] == payment_id), None)
    assert linha is not None, f"o pagamento {payment_id} não existe"
    return linha


def _cobrancas_por_liquidar(db, code: str) -> list[tuple[int, int]]:
    """[(id da cobrança, quanto falta)] de uma fração, calculado à mão."""
    resultado: list[tuple[int, int]] = []
    for cobranca in db.charges():
        if cobranca["fraction_code"] != code:
            continue
        falta = int(cobranca["amount_cents"]) - liquidado_da_cobranca(db, cobranca["id"])
        if falta > 0:
            resultado.append((int(cobranca["id"]), falta))
    return resultado


def _euros(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d}"


# ---------------------------------------------------------------------------
# Verificadores de invariante
# ---------------------------------------------------------------------------


def _invariantes_na_base(db) -> None:
    """O que tem de ser verdade nas tabelas, seja qual for a sequência de operações."""
    for pagamento in db.payments():
        afetado = _afetado_do_pagamento(db, pagamento["id"])
        assert afetado <= int(pagamento["amount_cents"]), (
            f"o pagamento {pagamento['id']} liquidou {afetado} e só recebeu "
            f"{pagamento['amount_cents']}"
        )

    for cobranca in db.charges():
        liquidado = liquidado_da_cobranca(db, cobranca["id"])
        assert liquidado <= int(cobranca["amount_cents"]), (
            f"a cobrança {cobranca['id']} de {cobranca['amount_cents']} ficou liquidada "
            f"em {liquidado}"
        )

    for code in [None, *FRACOES]:
        nome = code or "prédio"
        recebido, afetado, emitido = _recebido(db, code), _afetado(db, code), _emitido(db, code)
        assert afetado <= recebido, f"{nome}: afetado {afetado} acima do recebido {recebido}"
        assert recebido - afetado >= 0, f"{nome}: crédito negativo ({recebido} - {afetado})"
        assert emitido - afetado >= 0, f"{nome}: dívida negativa ({emitido} - {afetado})"

        # O fundo é uma afetação do dinheiro: nunca pode valer mais do que o reforço emitido.
        reforco_emitido = _emitido(db, code, "reforco_fundo")
        assert _ao_fundo(db, code) <= reforco_emitido, (
            f"{nome}: {code or ''} afetações ao fundo ({_ao_fundo(db, code)}) acima do "
            f"reforço do fundo emitido ({reforco_emitido})"
        )

    # As frações têm de somar o prédio: nenhum total pode contar duas vezes.
    for funcao in (_recebido, _anulado, _emitido, _afetado, _ao_fundo):
        soma_das_fracoes = sum(funcao(db, code) for code in FRACOES)
        assert soma_das_fracoes == funcao(db, None), (
            f"{funcao.__name__}: as frações somam {soma_das_fracoes}, o prédio diz "
            f"{funcao(db, None)}"
        )


def _totais_da_aplicacao(app, db, code: str | None = None) -> dict:
    fraction_id = None if code is None else db.fraction(code)["id"]
    with app.app_context():
        return _totais(fraction_id)


def _comparar_totais(app, db) -> None:
    """Os totais que a aplicação calcula têm de bater com a conta feita nas tabelas."""
    for code in [None, *FRACOES]:
        nome = code or "prédio"
        totais = _totais_da_aplicacao(app, db, code)
        recebido, afetado, emitido = _recebido(db, code), _afetado(db, code), _emitido(db, code)

        assert totais["recebido_cents"] == recebido, f"{nome}: recebido {totais['recebido_cents']} != {recebido}"
        assert totais["cobrado_cents"] == emitido, f"{nome}: emitido {totais['cobrado_cents']} != {emitido}"
        assert totais["afetado_cents"] == afetado, f"{nome}: afetado {totais['afetado_cents']} != {afetado}"
        assert totais["anulado_cents"] == _anulado(db, code), f"{nome}: anulados incorretos"
        assert totais["ao_fundo_cents"] == _ao_fundo(db, code), f"{nome}: fundo incorreto"

        # Identidades pedidas: recebido = afetado + crédito; dívida = emitido - afetado.
        assert totais["credito_cents"] == recebido - afetado, (
            f"{nome}: crédito {totais['credito_cents']} != recebido {recebido} - afetado {afetado}"
        )
        assert totais["divida_cents"] == emitido - afetado, (
            f"{nome}: dívida {totais['divida_cents']} != emitido {emitido} - afetado {afetado}"
        )
        assert totais["credito_cents"] >= 0, f"{nome}: crédito negativo"
        assert totais["divida_cents"] >= 0, f"{nome}: dívida negativa"
        assert totais["vencida_cents"] <= totais["divida_cents"], (
            f"{nome}: dívida vencida {totais['vencida_cents']} acima da dívida {totais['divida_cents']}"
        )


def _estado(app, db, code: str) -> dict[tuple[str, str], dict]:
    """Estado por cobrança da fração, indexado por (mês, rubrica)."""
    with app.app_context():
        linhas = _estado_das_cobrancas(db.fraction(code)["id"])
    return {(linha["period"], linha["rubric"]): linha for linha in linhas}


# ===========================================================================
# 1. Conservação do dinheiro
# ===========================================================================


def _proposta_de_distribuicao(db, pagamento: dict, aleatorio) -> dict[int, str]:
    """Distribui o crédito de um pagamento por cobranças por liquidar da sua fração."""
    sobra = _credito_do_pagamento(db, pagamento["id"])
    propostas: dict[int, str] = {}
    for charge_id, falta in _cobrancas_por_liquidar(db, pagamento["fraction_code"]):
        if sobra <= 0:
            break
        teto = min(falta, sobra)
        valor = teto if aleatorio.random() < 0.6 else max(1, teto // 2)
        propostas[charge_id] = _euros(valor)
        sobra -= valor
    return propostas


def test_uma_sequencia_de_registar_afetar_e_anular_conserva_sempre_o_dinheiro(app, client, db):
    """Depois de cada operação, e não só no fim, as identidades têm de bater."""
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")

    aleatorio = random.Random(20270201)
    historico: list[str] = []

    for passo in range(12):
        code = aleatorio.choice("ABCD")
        sorteio = aleatorio.random()

        if sorteio < 0.40:
            valor = aleatorio.choice(["3,00", "20,00", "30,00", "40,00", "43,00", "100,00"])
            data = f"{aleatorio.randint(1, 28):02d}/01/2027"
            registar_pagamento(client, db, code, valor, data=data)
            historico.append(f"{passo}: registar {code} {valor}")
        elif sorteio < 0.80:
            com_credito = [
                p for p in db.payments()
                if p["cancelled_at"] is None and _credito_do_pagamento(db, p["id"]) > 0
            ]
            if com_credito:
                pagamento = aleatorio.choice(com_credito)
                propostas = _proposta_de_distribuicao(db, pagamento, aleatorio)
                if propostas:
                    afetar_pagamento(client, pagamento["id"], propostas)
                    historico.append(f"{passo}: afetar {pagamento['id']} {propostas}")
        else:
            validos = [p for p in db.payments() if p["cancelled_at"] is None]
            if validos:
                pagamento = aleatorio.choice(validos)
                anular_pagamento(client, pagamento["id"], motivo=f"Anulação n.º {passo} da sequência")
                historico.append(f"{passo}: anular {pagamento['id']}")

        _invariantes_na_base(db)
        _comparar_totais(app, db)

    assert any("anular" in linha for linha in historico), "a sequência não anulou nada"
    assert any("afetar" in linha for linha in historico), "a sequência não afetou nada"
    assert db.payments(), "a sequência não registou pagamentos"


def test_os_totais_da_pagina_de_saldos_batem_com_a_conta_feita_nas_tabelas(app, client, db):
    """A página apresenta exatamente os números que as tabelas contêm."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )
    registar_pagamento(client, db, "B", "30,00")
    anular_pagamento(client, ultimo_pagamento(db)["id"], motivo="Movimento que não existia")

    _comparar_totais(app, db)

    html = client.get("/payments").get_data(as_text=True)
    assert "43,00 EUR" in html  # recebido da fração A
    assert "30,00 EUR" in html  # pagamento anulado, mostrado como anulado
    assert "Anulados: 30,00 EUR" in html


# ===========================================================================
# 2. Uma receita contada uma única vez
# ===========================================================================


def test_um_pagamento_afetado_a_quatro_cobrancas_contin_a_contar_uma_sozinha(app, client, db):
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "86,00")
    pagamento = ultimo_pagamento(db)

    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
            id_da_cobranca(db, "A", "2027-02", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-02", "reforco_fundo"): "3,00",
        },
    )

    assert db.scalar("SELECT COUNT(*) FROM payments") == 1
    assert db.scalar("SELECT COUNT(*) FROM payment_allocations") == 4

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["recebido_cents"] == 8600, "quatro afetações criaram receita a mais"
    assert totais["afetado_cents"] == 8600
    assert totais["credito_cents"] == 0
    assert totais["cobrado_cents"] == 8600
    assert totais["divida_cents"] == 0

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["recebido_cents"] == 8600
    _invariantes_na_base(db)


def test_afetar_o_mesmo_pagamento_em_dois_envios_distintos_nao_duplica_receita(app, client, db):
    """Cada envio só pode gastar o que desse pagamento ainda não foi gasto."""
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)

    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"})
    assert _credito_do_pagamento(db, pagamento["id"]) == 6000

    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-02", "quota_ordinaria"): "40,00"})
    assert _credito_do_pagamento(db, pagamento["id"]) == 2000
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1

    # Só faltam 20,00 deste pagamento: 30,00 é recusado e nada é gravado.
    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-02", "reforco_fundo"): "30,00"})
    assert _afetado_do_pagamento(db, pagamento["id"]) == 8000
    assert _credito_do_pagamento(db, pagamento["id"]) == 2000

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["recebido_cents"] == 10000
    assert totais["afetado_cents"] == 8000
    assert totais["credito_cents"] == 2000
    _invariantes_na_base(db)


# ===========================================================================
# 3. Limite dos vários pagamentos e das várias cobranças
# ===========================================================================


def test_afetacoes_parciais_de_varias_cobrancas_nao_somam_mais_do_que_o_pagamento(app, client, db):
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    # 40,00 + 3,00 + 1,00 = 53,00 acima de um pagamento de 43,00.
    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
            id_da_cobranca(db, "A", "2027-02", "reforco_fundo"): "1,00",
        },
    )

    assert resposta.status_code == 302
    assert db.allocations() == [], "a distribuição excessiva ficou parcialmente gravada"
    assert _credito_do_pagamento(db, pagamento["id"]) == 4300
    _invariantes_na_base(db)


def test_afetacoes_parciais_nao_somam_mais_do_que_a_divida_de_cada_cobranca(app, client, db):
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "30,00")
    primeiro = ultimo_pagamento(db)
    afetar_pagamento(client, primeiro["id"], {quota: "30,00"})
    assert liquidado_da_cobranca(db, quota) == 3000

    registar_pagamento(client, db, "A", "30,00")
    segundo = ultimo_pagamento(db)

    # Falta 10,00; a proposta de 20,00 é recusada na totalidade, não em parte.
    afetar_pagamento(client, segundo["id"], {quota: "20,00"})
    assert liquidado_da_cobranca(db, quota) == 3000
    assert _credito_do_pagamento(db, segundo["id"]) == 3000

    # A segunda proposta da mesma distribuição (40,00 no total) também é recusada:
    # 30,00 já afetados + 40,00 propostos excederiam a cobrança de 40,00.
    afetar_pagamento(client, segundo["id"], {quota: "40,00"})
    assert liquidado_da_cobranca(db, quota) == 3000

    # O que falta é exatamente o que pode ser afetado.
    afetar_pagamento(client, segundo["id"], {quota: "10,00"})
    assert liquidado_da_cobranca(db, quota) == 4000
    assert _credito_do_pagamento(db, segundo["id"]) == 2000
    _invariantes_na_base(db)


def test_dois_campos_para_a_mesma_cobranca_no_mesmo_envio_nao_duplicam_a_afetacao(app, client, db):
    """Um formulário manipulado pode propor a mesma cobrança duas vezes."""
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    protecao = protecao_de(client, f"/payments/{pagamento['id']}")
    resposta = client.post(
        f"/payments/{pagamento['id']}/allocate",
        data={
            **protecao,
            "confirmar_afetacao": "on",
            f"afecao_{quota}": "20,00",
            f"afecao_0{quota}": "27,00",
        },
    )

    assert resposta.status_code == 302
    assert db.allocations() == [], "a mesma cobrança foi liquidada duas vezes pelo mesmo pagamento"
    assert liquidado_da_cobranca(db, quota) == 0
    _invariantes_na_base(db)


def test_afetacao_direta_na_base_para_no_limite_exato_e_o_que_passa_e_recusado(app, client, db):
    """A fronteira é exata: liquidar o que falta entra, um cêntimo a mais não."""
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "30,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {quota: "30,00"})

    registar_pagamento(client, db, "A", "30,00")
    segundo = ultimo_pagamento(db)["id"]
    db.execute(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
        "VALUES (?, ?, 1000, '2027-01-16T00:00:00+00:00')",
        (segundo, quota),
    )
    assert liquidado_da_cobranca(db, quota) == 4000

    registar_pagamento(client, db, "A", "30,00")
    terceiro = ultimo_pagamento(db)["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 1, '2027-01-17T00:00:00+00:00')",
            (terceiro, quota),
        )
    assert liquidado_da_cobranca(db, quota) == 4000
    _invariantes_na_base(db)


# ===========================================================================
# 4. Anulação reverte exatamente um pagamento
# ===========================================================================


def test_anular_devolve_exatamente_o_que_o_pagamento_liquidou_e_nada_mais(app, client, db):
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "20,00")
    primeiro = ultimo_pagamento(db)
    afetar_pagamento(client, primeiro["id"], {quota: "20,00"})

    registar_pagamento(client, db, "A", "20,00")
    segundo = ultimo_pagamento(db)
    afetar_pagamento(client, segundo["id"], {quota: "20,00"})
    assert liquidado_da_cobranca(db, quota) == 4000

    anular_pagamento(client, primeiro["id"], motivo="Os 20 EUR eram de outra fração")

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["recebido_cents"] == 2000
    assert totais["afetado_cents"] == 2000
    assert totais["credito_cents"] == 0
    assert totais["anulado_cents"] == 2000
    # Emitido de A: 40,00 + 3,00. Liquidado: 20,00.
    assert totais["divida_cents"] == 4300 - 2000
    assert liquidado_da_cobranca(db, quota) == 2000
    _comparar_totais(app, db)


def test_anular_so_devolve_a_parte_das_cobrancas_que_efetivamente_afetou(app, client, db):
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    registar_pagamento(client, db, "A", "30,00")
    alvo = ultimo_pagamento(db)
    afetar_pagamento(client, alvo["id"], {quota: "25,00", reforco: "3,00"})

    registar_pagamento(client, db, "A", "15,00")
    outro = ultimo_pagamento(db)
    afetar_pagamento(client, outro["id"], {quota: "15,00"})
    assert liquidado_da_cobranca(db, quota) == 4000
    assert liquidado_da_cobranca(db, reforco) == 300

    anular_pagamento(client, alvo["id"], motivo="Distribuição errada")

    # O reforço volta todo (só aquele pagamento o liquidava); a quota perde 25,00.
    assert liquidado_da_cobranca(db, quota) == 1500
    assert liquidado_da_cobranca(db, reforco) == 0
    _comparar_totais(app, db)


def test_anular_nao_altera_outros_pagamentos_nem_as_suas_afetoes(app, client, db):
    regras_e_mes(client, db)
    quota_a = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    quota_b = id_da_cobranca(db, "B", "2027-01", "quota_ordinaria")

    registar_pagamento(client, db, "A", "40,00")
    alvo = ultimo_pagamento(db)
    afetar_pagamento(client, alvo["id"], {quota_a: "40,00"})

    registar_pagamento(client, db, "B", "45,00")
    vizinho = ultimo_pagamento(db)
    afetar_pagamento(client, vizinho["id"], {quota_b: "45,00"})

    pagamentos_antes = {p["id"]: dict(p) for p in db.payments()}
    afetoes_antes = list(db.allocations())

    anular_pagamento(client, alvo["id"], motivo="Registado por engano")

    pagamentos_depois = {p["id"]: dict(p) for p in db.payments()}
    assert pagamentos_depois.keys() == pagamentos_antes.keys(), "a anulação criou ou apagou registos"
    for payment_id, registo in pagamentos_antes.items():
        if payment_id == alvo["id"]:
            continue
        assert pagamentos_depois[payment_id] == registo, f"o pagamento {payment_id} foi alterado"

    afetoes_depois = list(db.allocations())
    assert [a for a in afetoes_depois if a["payment_id"] != alvo["id"]] == [
        a for a in afetoes_antes if a["payment_id"] != alvo["id"]
    ], "as afetações de outros pagamentos mudaram"

    assert liquidado_da_cobranca(db, quota_b) == 4500
    _comparar_totais(app, db)


def test_anular_um_pagamento_so_de_credito_reduz_o_recebido_mas_nao_toca_na_divida(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    antes = _totais_da_aplicacao(app, db, "A")
    assert antes["credito_cents"] == 4300
    assert antes["divida_cents"] == 4300

    anular_pagamento(client, pagamento["id"], motivo="Movimento que não existia")

    depois = _totais_da_aplicacao(app, db, "A")
    assert depois["recebido_cents"] == 0
    assert depois["credito_cents"] == 0, "um pagamento anulado deixou crédito no registo"
    assert depois["afetado_cents"] == 0
    assert depois["divida_cents"] == antes["divida_cents"] == 4300
    assert depois["anulado_cents"] == 4300
    _comparar_totais(app, db)


def test_anular_e_registar_de_novo_deixa_a_receita_correta_e_o_historico_inteiro(app, client, db):
    regras_e_mes(client, db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    registar_pagamento(client, db, "A", "43,00")
    errado = ultimo_pagamento(db)
    afetar_pagamento(client, errado["id"], {quota: "40,00", reforco: "3,00"})
    anular_pagamento(client, errado["id"], motivo="Valor errado")

    registar_pagamento(client, db, "A", "40,00", referencia="TRF-corrigido")
    certo = ultimo_pagamento(db)
    afetar_pagamento(client, certo["id"], {quota: "40,00"})

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["recebido_cents"] == 4000, "a receita corrigida contou o pagamento anulado"
    assert totais["afetado_cents"] == 4000
    assert totais["credito_cents"] == 0
    assert totais["divida_cents"] == 300
    assert totais["anulado_cents"] == 4300

    # Nada foi apagado: dois pagamentos e três afetações continuam lá.
    assert db.scalar("SELECT COUNT(*) FROM payments") == 2
    assert db.scalar("SELECT COUNT(*) FROM payment_allocations") == 3
    _comparar_totais(app, db)


# ===========================================================================
# 5. Receita impossível de duplicar
# ===========================================================================


def test_reenviar_exatamente_o_mesmo_formulario_regista_um_unico_pagamento(app, client, db):
    protecao = protecao_de(client, "/payments/add")
    form = {
        **protecao,
        "confirmar_pagamento": "on",
        "fracao": db.fraction("A")["id"],
        "valor": "43,00",
        "data": "15/01/2027",
        "meio": "transferencia",
        "referencia": "TRF-duplo-clique",
    }

    primeira = client.post("/payments/add", data=form)
    segunda = client.post("/payments/add", data=form)

    assert primeira.status_code == 302
    assert segunda.status_code == 302
    pagamentos = db.payments()
    assert len(pagamentos) == 1, "o reenvio criou uma segunda receita"
    assert pagamentos[0]["amount_cents"] == 4300
    _invariantes_na_base(db)


def test_reenviar_a_marca_de_um_pagamento_com_outro_valor_nao_cria_receita_nem_altera_o_original(app, client, db):
    protecao = protecao_de(client, "/payments/add")
    form = {
        **protecao,
        "confirmar_pagamento": "on",
        "fracao": db.fraction("A")["id"],
        "valor": "43,00",
        "data": "15/01/2027",
        "meio": "transferencia",
        "referencia": "TRF-1",
    }

    client.post("/payments/add", data=form)
    assert len(db.payments()) == 1

    # Mesmo formulário reenviado, com revisão atualizada e valor diferente.
    form["valor"] = "60,00"
    form["revisao_pagamentos"] = protecao_de(client, "/payments/add")["revisao_pagamentos"]
    client.post("/payments/add", data=form)

    pagamentos = db.payments()
    assert len(pagamentos) == 1, "a marca repetida criou receita nova"
    assert pagamentos[0]["amount_cents"] == 4300, "o reenvio alterou o valor do pagamento original"
    assert pagamentos[0]["reference"] == "TRF-1"

    detalhe = client.get(f"/payments/{pagamentos[0]['id']}").get_data(as_text=True)
    assert "já está registado" in detalhe
    _invariantes_na_base(db)


def test_marca_de_formulario_repetida_e_travada_na_base_por_indice_unico(app, client, db):
    id_a = db.fraction("A")["id"]
    db.execute(
        """
        INSERT INTO payments
            (fraction_id, amount_cents, paid_on, method, created_at, updated_at, form_token)
        VALUES (?, 4000, '2027-01-15', 'dinheiro', '2027-01-15T00:00:00+00:00',
                '2027-01-15T00:00:00+00:00', 'MARCA-UNICA')
        """,
        (id_a,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO payments
                (fraction_id, amount_cents, paid_on, method, created_at, updated_at, form_token)
            VALUES (?, 4000, '2027-01-15', 'dinheiro', '2027-01-15T00:00:00+00:00',
                    '2027-01-15T00:00:00+00:00', 'MARCA-UNICA')
            """,
            (id_a,),
        )
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1

    # Movimentos sem marca continuam a ser movimentos distintos e legítimos.
    for _ in range(2):
        db.execute(
            """
            INSERT INTO payments
                (fraction_id, amount_cents, paid_on, method, created_at, updated_at)
            VALUES (?, 4000, '2027-01-16', 'dinheiro', '2027-01-16T00:00:00+00:00',
                    '2027-01-16T00:00:00+00:00')
            """,
            (id_a,),
        )
    assert db.scalar("SELECT COUNT(*) FROM payments") == 3
    _invariantes_na_base(db)


# ===========================================================================
# 6. Transações recusadas não deixam alterações parciais
# ===========================================================================


GATILHO_FALHA_NO_REFORCO = """
CREATE TRIGGER falha_na_afetacao_ao_reforco BEFORE INSERT ON payment_allocations
WHEN NEW.charge_id IN (SELECT id FROM charges WHERE rubric = 'reforco_fundo')
BEGIN
    SELECT RAISE(ABORT, 'falha simulada ao afetar o reforço do fundo');
END;
"""


def test_falha_a_meio_da_distribuicao_nao_deixa_metade_das_afetoes_gravada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    reforco = id_da_cobranca(db, "A", "2027-01", "reforco_fundo")

    db.execute(GATILHO_FALHA_NO_REFORCO)

    resposta = afetar_pagamento(client, pagamento["id"], {quota: "40,00", reforco: "3,00"})

    assert resposta.status_code == 302
    assert db.allocations() == [], "a distribuição falhada deixou a quota liquidada a meio"
    assert liquidado_da_cobranca(db, quota) == 0
    assert _credito_do_pagamento(db, pagamento["id"]) == 4300
    assert _pagamento(db, pagamento["id"])["cancelled_at"] is None

    detalhe = client.get(f"/payments/{pagamento['id']}").get_data(as_text=True)
    assert "Nenhuma afetação foi gravada" in detalhe
    _comparar_totais(app, db)


def test_distribuicao_com_uma_afetacao_valida_e_outra_invalida_nao_grava_nada(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)

    resposta = afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "B", "2027-01", "quota_ordinaria"): "3,00",
        },
    )

    assert resposta.status_code == 302
    assert db.allocations() == [], "a parte válida ficou gravada sozinha"
    assert liquidado_da_cobranca(db, id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")) == 0
    assert _totais_da_aplicacao(app, db, "A")["credito_cents"] == 4300
    _invariantes_na_base(db)


def test_submissao_recusada_nao_consome_a_marca_do_formulario(app, client, db):
    """Um envio inválido não pode queimar a marca e impedir o envio correto."""
    protecao = protecao_de(client, "/payments/add")
    form = {
        **protecao,
        "confirmar_pagamento": "on",
        "fracao": db.fraction("A")["id"],
        "valor": "40,00",
        "data": "31/12/2099",
        "meio": "transferencia",
    }

    client.post("/payments/add", data=form)
    assert db.payments() == []

    form["data"] = "15/01/2027"
    form["revisao_pagamentos"] = protecao_de(client, "/payments/add")["revisao_pagamentos"]
    client.post("/payments/add", data=form)

    assert len(db.payments()) == 1
    assert db.payments()[0]["amount_cents"] == 4000
    _invariantes_na_base(db)


# ===========================================================================
# 7. Duas janelas abertas sobre o mesmo crédito
# ===========================================================================


def test_duas_janelas_a_distribuir_o_mesmo_credito_nao_somam_mais_do_que_o_pagamento(app, client, db):
    """Dois formulários abertos sobre o mesmo pagamento: o total afetado não excede o dinheiro."""
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "100,00")
    pagamento = ultimo_pagamento(db)

    proposta_a = {
        id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
        id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
    }
    proposta_b = {
        id_da_cobranca(db, "A", "2027-02", "quota_ordinaria"): "40,00",
        id_da_cobranca(db, "A", "2027-02", "reforco_fundo"): "3,00",
    }

    janela_a = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    janela_b = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    protecao_a = protecao_de(janela_a, f"/payments/{pagamento['id']}")
    protecao_b = protecao_de(janela_b, f"/payments/{pagamento['id']}")
    assert protecao_a["revisao_pagamentos"] == protecao_b["revisao_pagamentos"], (
        "as duas janelas deviam partir do mesmo estado guardado"
    )

    barreira = threading.Barrier(2, timeout=15)
    resultados: list[tuple[object, int]] = []
    erros: list[BaseException] = []

    def distribuir(janela, protecao, afetoes):
        barreira.wait()
        try:
            resposta = afetar_pagamento(janela, pagamento["id"], afetoes, protecao=protecao)
            resultados.append((janela, resposta.status_code))
        except BaseException as erro:
            erros.append(erro)

    threads = [
        threading.Thread(target=distribuir, args=(janela_a, protecao_a, proposta_a)),
        threading.Thread(target=distribuir, args=(janela_b, protecao_b, proposta_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not erros, f"um pedido concorrente rebentou em vez de ser recusado: {erros!r}"
    assert len(resultados) == 2, "uma das janelas não completou o pedido"
    assert sorted(codigo for _janela, codigo in resultados) == [302, 302], (
        f"uma janela concorrente não recebeu a recusa por redirect: {resultados}"
    )

    total_afetado = _afetado_do_pagamento(db, pagamento["id"])
    assert total_afetado <= 10000, "as afetações somam mais do que o pagamento de 100,00"
    assert total_afetado == 4300, "as duas janelas gastaram o mesmo crédito duas vezes"
    assert _credito_do_pagamento(db, pagamento["id"]) == 5700

    paginas = [
        janela.get(f"/payments/{pagamento['id']}").get_data(as_text=True)
        for janela, _codigo in resultados
    ]
    aceites = [corpo for corpo in paginas if "Distribuição confirmada" in corpo]
    recusadas = [corpo for corpo in paginas if "desatualizada" in corpo or "Não foi possível" in corpo]
    assert len(aceites) == 1, f"mais de uma janela distribuiu o mesmo dinheiro: {len(aceites)}"
    assert len(recusadas) == 1, "a janela concorrente não explicou a recusa"

    _invariantes_na_base(db)
    _comparar_totais(app, db)


# ===========================================================================
# 8. Dívida vencida só com vencimento confirmado
# ===========================================================================


def test_cobrancas_antigas_sem_vencimento_confirmado_nunca_estao_vencidas(app, client, db):
    registar_regra(client, db, list("ACEG"), "quota_ordinaria", "30,00", "09/2026", "12/2026")
    registar_regra(client, db, list("BDFH"), "quota_ordinaria", "32,00", "09/2026", "12/2026")
    gerar_mes(client, "2026-09")

    assert all(c["due_date"] is None for c in db.charges("2026-09"))

    for code in [None, *FRACOES]:
        totais = _totais_da_aplicacao(app, db, code)
        assert totais["vencida_cents"] == 0, f"{code or 'prédio'}: dívida vencida sem vencimento"

    estado = _estado(app, db, "A")
    assert estado[("2026-09", "quota_ordinaria")]["vencida"] is False
    assert estado[("2026-09", "quota_ordinaria")]["due_texto"] == "por confirmar"

    _comparar_totais(app, db)


def test_a_divida_vencida_so_inclui_cobrancas_com_vencimento_passado_e_por_liquidar(app, client, db):
    configurar_regras_de_teste(client, db)
    alterar_config(client, db, quota_dia_vencimento="15")
    gerar_mes(client, "2027-01")  # vencimento 15/01/2027: já passou
    gerar_mes(client, "2027-03")  # vencimento 15/03/2027: ainda não passou

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["cobrado_cents"] == 2 * 36400
    assert globais["vencida_cents"] == 36400, "as cobranças de março entraram na dívida vencida"

    # Liquidar a quota de março não altera a dívida vencida: março ainda não venceu.
    registar_pagamento(client, db, "A", "40,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {id_da_cobranca(db, "A", "2027-03", "quota_ordinaria"): "40,00"})

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["divida_cents"] == 2 * 36400 - 4000
    assert globais["vencida_cents"] == 36400

    # Liquidar a quota de janeiro, essa sí, baixa a dívida vencida.
    registar_pagamento(client, db, "A", "40,00")
    afetar_pagamento(client, ultimo_pagamento(db)["id"], {id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00"})

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["divida_cents"] == 2 * 36400 - 8000
    assert globais["vencida_cents"] == 36400 - 4000

    estado = _estado(app, db, "A")
    assert estado[("2027-01", "quota_ordinaria")]["liquidada"] is True
    assert estado[("2027-03", "quota_ordinaria")]["liquidada"] is True
    assert estado[("2027-01", "reforco_fundo")]["vencida"] is True
    assert estado[("2027-03", "reforco_fundo")]["vencida"] is False

    _comparar_totais(app, db)


def test_anular_um_pagamento_reabre_a_divida_vencida_que_ele_tinha_liquidado(app, client, db):
    configurar_regras_de_teste(client, db)
    alterar_config(client, db, quota_dia_vencimento="15")
    gerar_mes(client, "2027-01")

    quota = id_da_cobranca(db, "A", "2027-01", "quota_ordinaria")
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(client, pagamento["id"], {quota: "40,00"})

    depois_de_pagar = _totais_da_aplicacao(app, db, None)
    assert depois_de_pagar["vencida_cents"] == 36400 - 4000

    anular_pagamento(client, pagamento["id"], motivo="A transferência não aconteceu")

    depois_de_anular = _totais_da_aplicacao(app, db, None)
    assert depois_de_anular["vencida_cents"] == 36400, "a anulação não reabriu a dívida vencida"
    assert liquidado_da_cobranca(db, quota) == 0
    _comparar_totais(app, db)


# ===========================================================================
# 9. Rubricas separadas
# ===========================================================================


def test_reforco_do_fundo_nunca_se_soma_com_a_quota_ordinaria_num_total(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["afetado_cents"] == 4300
    assert totais["ao_fundo_cents"] == 300, "o fundo não corresponde só ao reforço do fundo"
    assert totais["ao_fundo_cents"] < totais["afetado_cents"], "o fundo contou a quota ordinária"
    assert totais["ao_fundo_cents"] <= totais["recebido_cents"]

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["ao_fundo_cents"] == 300
    assert globais["recebido_cents"] == 4300
    assert globais["ao_fundo_cents"] + globais["recebido_cents"] != globais["afetado_cents"], (
        "o fundo foi somado ao recebido como se fosse receita à parte"
    )

    html = client.get("/payments").get_data(as_text=True)
    assert "Afeto ao fundo" in html
    assert "3,00 EUR" in html
    _comparar_totais(app, db)


def test_afetar_o_reforco_de_um_mes_nao_liquida_o_reforco_de_outro_mes_nem_a_quota(app, client, db):
    regras_e_mes(client, db)
    gerar_mes(client, "2027-02")
    registar_pagamento(client, db, "A", "3,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00"})

    estado = _estado(app, db, "A")
    assert estado[("2027-01", "reforco_fundo")]["liquidada"] is True
    assert estado[("2027-02", "reforco_fundo")]["em_divida_cents"] == 300
    assert estado[("2027-01", "quota_ordinaria")]["em_divida_cents"] == 4000
    assert estado[("2027-02", "quota_ordinaria")]["em_divida_cents"] == 4000

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["ao_fundo_cents"] == 300
    assert totais["divida_cents"] == 2 * 4000 + 300
    _comparar_totais(app, db)


def test_divida_de_reforco_de_2027_continua_depois_de_a_regra_terminar(app, client, db):
    configurar_regras_de_teste(client, db)
    gerar_mes(client, "2027-12")
    registar_pagamento(client, db, "A", "40,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(client, pagamento["id"], {id_da_cobranca(db, "A", "2027-12", "quota_ordinaria"): "40,00"})

    gerar_mes(client, "2028-01")

    estado = _estado(app, db, "A")
    assert estado[("2027-12", "reforco_fundo")]["em_divida_cents"] == 300, (
        "a dívida de reforço desapareceu quando a regra terminou"
    )
    assert ("2028-01", "reforco_fundo") not in estado, "2028 não tem reforço do fundo"

    totais = _totais_da_aplicacao(app, db, "A")
    assert totais["cobrado_cents"] == 4300 + 4000
    # Ficam por liquidar o reforço de 12/2027 e a quota ordinária de 01/2028.
    assert totais["divida_cents"] == 300 + 4000
    assert totais["ao_fundo_cents"] == 0
    _comparar_totais(app, db)


def test_a_divida_de_uma_fracao_nao_inclui_cobrancas_de_outra_fracao(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )

    a = _totais_da_aplicacao(app, db, "A")
    assert (a["cobrado_cents"], a["recebido_cents"], a["afetado_cents"]) == (4300, 4300, 4300)
    assert a["credito_cents"] == 0 and a["divida_cents"] == 0

    b = _totais_da_aplicacao(app, db, "B")
    assert (b["cobrado_cents"], b["recebido_cents"], b["afetado_cents"]) == (4800, 0, 0)
    assert b["divida_cents"] == 4800, "a dívida de B contou o pagamento de A"

    globais = _totais_da_aplicacao(app, db, None)
    assert globais["cobrado_cents"] == 36400
    assert globais["recebido_cents"] == 4300
    assert globais["divida_cents"] == 36400 - 4300
    _comparar_totais(app, db)
