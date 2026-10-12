"""Deteção de formulários desatualizados (concorrência otimista).

Cada página de alteração envia uma revisão calculada a partir dos valores
atualmente guardados. Se outra versão do mesmo formulário tiver sido guardada
entretanto, a revisão já não bate e a submissão é recusada sem gravar nada:
os valores mais recentes são conservados.

A revisão depende só dos valores, não das marcas de tempo. Regravar exatamente
o que já está guardado continua a ser aceite; alterar sobre dados mais recentes não.

A revisão é confirmada dentro da própria transação de escrita (`transacao_de_escrita`):
o bloqueio de escrita é adquirido antes de reler o estado, por isso nenhum outro
processo pode alterar os valores entre a confirmação e a gravação.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from flask import request

from .db import get_db, get_setting, query_db

CAMPOS = {
    "configuracao": "revisao_configuracao",
    "fracoes": "revisao_fracoes",
    "responsaveis": "revisao_responsaveis",
    "regras": "revisao_regras",
    "geracao": "revisao_geracao",
    "pagamentos": "revisao_pagamentos",
    "cobrancas": "revisao_cobrancas",
    "despesas": "revisao_despesas",
    "integracoes": "revisao_integracoes",
}

MENSAGEM_DESATUALIZADO = (
    "Esta página está desatualizada: entretanto foi guardada outra alteração. "
    "Nada foi gravado e os valores atuais foram conservados. Recarrega a página "
    "e repete a alteração se ainda fizer sentido."
)


def _hash(linhas) -> str:
    """Serialização canónica: JSON, com chaves ordenadas.

    Concatenar campos com `|` não bastava: um nome com `|` ou salto de linha podia
    produzir a mesma string de outro conjunto de registos, e uma revisão envelhecida
    passaria.
    """
    return hashlib.sha256(
        json.dumps([dict(linha) for linha in linhas], sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def revisao_configuracao() -> str:
    return _hash(query_db("SELECT key, value FROM settings ORDER BY key"))


def revisao_fracoes() -> str:
    return _hash(
        query_db("SELECT id, code, permilagem, active FROM fractions ORDER BY id")
    )


def revisao_responsaveis(fraction_id: int | None = None) -> str:
    """Revisão dos responsáveis de uma fração, ou de todas se não for indicada.

    Por fração: registar um responsável numa fração não invalida um formulário
    aberto noutra fração.
    """
    sql = "SELECT id, fraction_id, name, role, valid_from, valid_to FROM owners"
    params: tuple = ()
    if fraction_id is not None:
        sql += " WHERE fraction_id = ?"
        params = (fraction_id,)
    return _hash(query_db(sql + " ORDER BY id", params))


def revisao_regras() -> str:
    """Revisão das regras de quota: o valor, a rubrica e a vigência de cada regra."""
    return _hash(
        query_db(
            "SELECT id, fraction_id, rubric, amount_cents, valid_from, valid_to, source "
            "FROM charge_rules ORDER BY id"
        )
    )


def revisao_pagamentos() -> str:
    """Revisão dos pagamentos, das suas afetações e do rasto de correções.

    Uma afetação depende do que ainda está por liquidar, e isso depende de todos os
    pagamentos e afetações: por isso a revisão abrange os dois, e não só o pagamento
    que se está a distribuir ou a corrigir. Duas janelas abertas a gastar o mesmo
    crédito, a liquidar duas vezes a mesma cobrança, ou a corrigir o mesmo pagamento,
    deixam de estar atualizadas a segunda.
    """
    linhas = [
        dict(row)
        for row in query_db(
            "SELECT id, fraction_id, amount_cents, paid_on, method, reference, source, "
            "cancelled_at, cancellation_reason FROM payments ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, payment_id, charge_id, amount_cents FROM payment_allocations ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, payment_id, changed_at, reason FROM payment_revisions ORDER BY id"
        )
    ]
    return _hash(linhas)


def revisao_geracao() -> str:
    """Estado de que depende gerar um mês: regras, frações ativas, datas de configuração
    e as cobranças já emitidas para esse mês.

    A pré-visualização é feita com estes valores; se algum mudar entre ver a
    pré-visualização e confirmar, a geração é recusada em vez de aplicar um plano
    que já não é o que foi mostrado. O mês vem do próprio pedido: o GET que mostra a
    pré-visualização traz-o em `period` no URL e o POST que confirma traz-o no formulário.
    """
    period = (request.form.get("period") or request.args.get("period") or "").strip()

    linhas = [
        dict(row)
        for row in query_db(
            "SELECT id, fraction_id, rubric, amount_cents, valid_from, valid_to "
            "FROM charge_rules ORDER BY id"
        )
    ]
    linhas += [
        dict(row) for row in query_db("SELECT id, code, active FROM fractions ORDER BY id")
    ]
    linhas += [
        {"key": chave, "value": get_setting(chave)}
        for chave in ("quota_dia_vencimento", "gestao_data_inicio")
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT fraction_id, rubric, amount_cents FROM charges WHERE period = ? ORDER BY id",
            (period,),
        )
    ]
    return _hash(linhas)


def revisao_cobrancas() -> str:
    """Revisão das cobranças emitidas: valor, vencimento, descrição, afetações e correções.

    Corrigir uma cobrança depende do que já está liquidado a ela, e o liquidado depende
    das afetações de pagamentos. Duas janelas abertas a corrigir a mesma cobrança, ou a
    corrigi-la enquanto outra distribui um pagamento por ela, deixam de estar atualizadas
    a segunda.
    """
    linhas = [
        dict(row)
        for row in query_db(
            "SELECT id, fraction_id, rule_id, rubric, period, amount_cents, due_date, descricao "
            "FROM charges ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, payment_id, charge_id, amount_cents FROM payment_allocations ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, charge_id, changed_at, motivo, amount_before, amount_after "
            "FROM charge_revisions ORDER BY id"
        )
    ]
    return _hash(linhas)


def revisao_despesas() -> str:
    """Revisão das despesas, dos seus pagamentos, categorias e rasto de correções.

    O valor em falta de uma despesa depende de todos os seus pagamentos, e pagar
    acima do valor em falta depende do que os outros pagamentos já cobriram: por
    isso a revisão abrange as duas tabelas e as categorias, que as despesas usam.
    Duas janelas abertas a pagar a mesma despesa, ou a corrigi-la enquanto outra
    paga, deixam de estar atualizadas a segunda.
    """
    linhas = [
        dict(row)
        for row in query_db(
            "SELECT id, descricao, fornecedor, category_id, referencia, data, due_date, "
            "amount_cents, classificacao, notas, cancelled_at, cancellation_reason "
            "FROM expenses ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, expense_id, amount_cents, paid_on, method, reference, "
            "cancelled_at, cancellation_reason FROM expense_payments ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, expense_id, changed_at, motivo FROM expense_revisions ORDER BY id"
        )
    ]
    linhas += [
        dict(row)
        for row in query_db(
            "SELECT id, payment_id, changed_at, motivo FROM expense_payment_revisions ORDER BY id"
        )
    ]
    linhas += [
        dict(row) for row in query_db("SELECT id, nome FROM expense_categories ORDER BY id")
    ]
    return _hash(linhas)


def revisao_integracoes() -> str:
    """Revisão das definições de integração (Gmail e IA).

    O hash não revela os valores: duas janelas abertas sobre as definições deixam
    de estar atualizadas a segunda se outra pessoa os alterar entretanto.
    """
    return _hash(query_db("SELECT chave, valor FROM integracoes ORDER BY chave"))


def estado_atual(chave: str, fraction_id: int | None = None) -> str:
    if chave == "responsaveis":
        return revisao_responsaveis(fraction_id)
    return {
        "configuracao": revisao_configuracao,
        "fracoes": revisao_fracoes,
        "regras": revisao_regras,
        "geracao": revisao_geracao,
        "pagamentos": revisao_pagamentos,
        "cobrancas": revisao_cobrancas,
        "despesas": revisao_despesas,
        "integracoes": revisao_integracoes,
    }[chave]()


def desatualizado(form, chave: str, fraction_id: int | None = None) -> bool:
    """True se o formulário não representa o estado atualmente guardado."""
    return form.get(CAMPOS[chave], "") != estado_atual(chave, fraction_id)


FORM_DO_PEDIDO = object()


def transacao_de_escrita(chave: str, fraction_id: int | None = None,
                         form=FORM_DO_PEDIDO) -> bool:
    """Abrir a transação de escrita e confirmar que o formulário ainda está atualizado.

    Devolve False (e desfaz tudo) quando o formulário está desatualizado. Confirmar
    dentro da transação é o que fecha a janela entre ler o estado e gravar: com o
    bloqueio de escrita tomado, outro pedido não pode alterar os valores entretanto.

    Se a base estiver ocupada e o bloqueio não for conseguido, devolve False: nada é
    escrito e os valores guardados ficam exatamente como estavam.

    `form=None` é a escrita de sistema (automatização bancária, sem formulário):
    não há revisão de formulário a comparar; o `BEGIN IMMEDIATE` já garante que
    nenhum outro pedido escreve ao mesmo tempo e as quantias em falta são sempre
    relidas dentro da transação.
    """
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        connection.rollback()
        return False
    if form is None:
        return True
    comparado = request.form if form is FORM_DO_PEDIDO else form
    if desatualizado(comparado, chave, fraction_id):
        connection.rollback()
        return False
    return True
