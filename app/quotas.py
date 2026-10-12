"""Regras de quotas, geração mensal de cobranças e consulta do emitido (fase 2).

Regras aplicadas nesta fase:
- Uma regra de quota pertence a uma fração e a uma rubrica (quota ordinária ou
  reforço do fundo) e tem vigência por meses inteiros: começa no dia 1 e termina
  no último dia de um mês. `valid_to` vazio significa "em aberto".
- Não há duas regras a valer para a mesma fração e rubrica no mesmo mês: a
  aplicação recusa a sobreposição e a base de dados tem um travão para o caso de
  alguém escrever diretamente na base.
- Gerar um mês é sempre precedido de pré-visualização. O plano mostrado é o mesmo
  cálculo que grava, e a confirmação só é aceite se o estado não mudou entretanto.
- Gerar é transacional e idempotente: `UNIQUE (fraction_id, rubric, period)` na
  base faz com que repetir o mês não crie duplicados, e uma falha a meio não
  deixa metade do mês gravado.
- Uma cobrança guarda o valor, a rubrica, o mês e a regra de que veio. Alterar
  ou encerrar uma regra não reescreve as cobranças já emitidas.
- Sem dia de vencimento confirmado, a cobrança não tem data de vencimento e não
  é classificada como vencida.
- Valores emitidos não são dinheiro recebido: os pagamentos pertencem à fase 3.

Regras aplicadas na entrega 2:
- Além das rubricas que se repetem todos os meses, existe a quota extraordinária:
  uma decisão pontual, com montante total, forma de repartição pelas frações
  abrangidas e calendário de prestações. Não é gerada pela geração mensal.
- A repartição conserva exatamente o total, tanto entre frações como entre prestações.
- Emitir é idempotente: a mesma quota não é emitida duas vezes por duplo clique ou
  reenvio; duas quotas extraordinárias diferentes no mesmo mês são possíveis.
- Editar uma regra afeta só as emissões futuras. Mudar o valor de uma regra fica
  registado com motivo; as cobranças já emitidas conservam o valor que tinham.
- Corrigir uma cobrança emitida é ação explícita, com motivo, histórico e
  pré-visualização dos efeitos. Pagamentos e respetivas ligações conservam-se, e uma
  correção incompatível com o que já foi pago é recusada.
"""

from __future__ import annotations

import secrets
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .concurrency import (
    MENSAGEM_DESATUALIZADO,
    desatualizado,
    revisao_cobrancas,
    revisao_geracao,
    revisao_regras,
    transacao_de_escrita,
)
from .db import get_db, get_setting, query_db, utc_now_iso
from .reparticao import repartir, repartir_em_prestacoes, repartir_igual
from .units import (
    current_month,
    format_date,
    format_money,
    format_money_input,
    format_month,
    format_permilagem,
    month_end,
    month_start,
    next_month,
    parse_date,
    parse_money,
    parse_month,
    parse_int_id,
    previous_month,
    today_iso,
)

bp = Blueprint("quotas", __name__)


@bp.before_request
def _apenas_administrador():
    """Regras de quota e geração de cobranças são de administração."""
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()

# Rubricas que se repetem todos os meses: são estas que a geração mensal cobre.
RUBRICAS = ("quota_ordinaria", "reforco_fundo")
# Rubricas possíveis numa cobrança. A extraordinária é uma decisão pontual e é
# emitida pelo próprio formulário de quota extraordinária, nunca pela geração mensal.
RUBRICAS_TODAS = RUBRICAS + ("quota_extraordinaria",)
ROTULOS_RUBRICA = {
    "quota_ordinaria": "Quota ordinária",
    "reforco_fundo": "Reforço do fundo de reserva",
    "quota_extraordinaria": "Quota extraordinária",
}
SEM_FIM = "9999-12-31"

# Um valor mensal acima disto é quase de certeza uma gralha de escrita.
VALOR_MAXIMO_CENTIMOS = 1_000_000  # 10.000 EUR

# --- Limites da quota extraordinária (entrega 2) ---
REPARTICOES = ("igual", "permilagem", "manual")
ROTULOS_REPARTICAO = {
    "igual": "Igual por fração",
    "permilagem": "Proporcional à permilagem",
    "manual": "Manual, por fração",
}
MAXIMO_PRESTACOES = 12
LIMITE_DESCRICAO = 120
LIMITE_FINALIDADE = 400
LIMITE_MOTIVO = 300
VALOR_MAXIMO_EXTRAORDINARIA_CENTIMOS = 10_000_000  # 100.000 EUR


@bp.app_context_processor
def _contexto_quota():
    return {
        "RUBRICAS": RUBRICAS,
        "ROTULOS_RUBRICA": ROTULOS_RUBRICA,
        "LIMITE_DESCRICAO": LIMITE_DESCRICAO,
        "LIMITE_FINALIDADE": LIMITE_FINALIDADE,
        "LIMITE_MOTIVO": LIMITE_MOTIVO,
        "format_money": format_money,
        "format_month": format_month,
        "format_permilagem": format_permilagem,
        "revisao_regras": revisao_regras,
        "revisao_geracao": revisao_geracao,
        "revisao_cobrancas": revisao_cobrancas,
    }


def _recusar_se_desatualizado(chave: str) -> bool:
    if not desatualizado(request.form, chave):
        return False
    flash(MENSAGEM_DESATUALIZADO, "erro")
    return True


# ---------------------------------------------------------------------------
# Regras de quota
# ---------------------------------------------------------------------------


def _regra_view(row) -> dict:
    emitidas = query_db(
        "SELECT COUNT(*) AS n FROM charges WHERE rule_id = ?", (row["id"],), one=True
    )
    return {
        "id": row["id"],
        "code": row["code"],
        "fraction_active": bool(row["active"]),
        "rubric": row["rubric"],
        "rubrica_texto": ROTULOS_RUBRICA[row["rubric"]],
        "amount_cents": int(row["amount_cents"]),
        "amount_texto": format_money(int(row["amount_cents"])),
        "valid_from": format_date(row["valid_from"]),
        "valid_to": format_date(row["valid_to"]) if row["valid_to"] else "em aberto",
        "valid_from_iso": row["valid_from"],
        "valid_to_iso": row["valid_to"] or "",
        "valid_to_edicao": format_month(row["valid_to"][:7]) if row["valid_to"] else "",
        "source": row["source"],
        "emitidas": int(emitidas["n"]),
    }


@bp.route("/quotas")
def quotas():
    rows = query_db(
        """
        SELECT r.*, f.code, f.active
        FROM charge_rules r
        JOIN fractions f ON f.id = r.fraction_id
        WHERE r.rubric <> 'quota_extraordinaria'
        ORDER BY f.code, r.rubric, r.valid_from
        """
    )
    fracoes = query_db("SELECT id, code, active FROM fractions ORDER BY code")
    extraordinarias = query_db(
        """
        SELECT q.id, q.descricao, q.total_cents, q.reparticao, q.created_at,
               COUNT(DISTINCT r.id) AS regras,
               COUNT(c.id) AS cobrancas
        FROM extraordinary_quotas q
        LEFT JOIN charge_rules r ON r.quota_id = q.id
        LEFT JOIN charges c ON c.rule_id = r.id
        GROUP BY q.id
        ORDER BY q.id DESC
        """
    )
    return render_template(
        "quotas.html",
        regras=[_regra_view(row) for row in rows],
        fracoes=fracoes,
        extraordinarias=extraordinarias,
        cobrancas_existentes=query_db(
            "SELECT period, COUNT(*) AS n FROM charges GROUP BY period ORDER BY period"
        ),
    )


def _ler_regra_do_form(form) -> tuple[dict | None, list[str]]:
    """Ler e validar o formulário de nova regra. Devolve (regra, erros)."""
    erros: list[str] = []

    rubrica = form.get("rubrica", "").strip()
    if rubrica not in RUBRICAS:
        erros.append("Rubrica: escolhe quota ordinária ou reforço do fundo de reserva.")

    raw_valor = form.get("valor", "").strip()
    valor = parse_money(raw_valor)
    if valor is None or valor <= 0:
        erros.append("Valor mensal: usa um valor positivo em euros, por exemplo 47,00.")
    elif valor > VALOR_MAXIMO_CENTIMOS:
        erros.append(
            "Valor mensal: acima de 10.000,00 EUR parece um erro de escrita. "
            "Confirma o valor e tenta de novo."
        )

    inicio = parse_month(form.get("inicio", ""))
    if inicio is None:
        erros.append("Início da vigência: usa um mês, por exemplo 01/2026.")

    fim_raw = form.get("fim", "").strip()
    fim: str | None = None
    if fim_raw:
        fim = parse_month(fim_raw)
        if fim is None:
            erros.append("Fim da vigência: usa um mês, por exemplo 12/2026, ou deixa vazio.")
        elif inicio and fim < inicio:
            erros.append("O fim da vigência não pode ser anterior ao início.")

    fonte = form.get("source", "").strip()
    if len(fonte) > 120:
        erros.append("Fonte: no máximo 120 caracteres.")

    ids: list[int] = []
    for campo, marca in form.items():
        if not (campo.startswith("fracao_") and marca == "on"):
            continue
        id_fracao = parse_int_id(campo.removeprefix("fracao_"))
        if id_fracao is not None:
            ids.append(id_fracao)
    ids = sorted(set(ids))
    if not ids:
        erros.append("Frações: escolhe pelo menos uma fração.")
    else:
        existentes = {
            int(row["id"])
            for row in query_db(
                f"SELECT id FROM fractions WHERE id IN ({', '.join(['?'] * len(ids))})",
                tuple(ids),
            )
        }
        desconhecidas = [str(id_) for id_ in ids if id_ not in existentes]
        if desconhecidas:
            erros.append(
                "Frações: "
                + ", ".join(desconhecidas)
                + " não corresponde a frações registadas. Nenhuma regra foi guardada."
            )
        ids = sorted(existentes)

    if erros:
        return None, erros

    return {
        "fraction_ids": ids,
        "rubric": rubrica,
        "amount_cents": valor,
        "valid_from": month_start(inicio),
        "valid_to": month_end(fim) if fim else None,
        "source": fonte,
    }, erros


def _sobreposicoes(regra: dict, ignorar_id: int | None = None) -> list[str]:
    """Códigos das frações em que a proposta colide com uma regra já registada."""
    codigos: list[str] = []
    for fraction_id in regra["fraction_ids"]:
        novo_fim = regra["valid_to"] or SEM_FIM
        colide = query_db(
            """
            SELECT 1 FROM charge_rules
            WHERE fraction_id = ?
              AND rubric = ?
              AND (? = 0 OR id != ?)
              AND valid_from <= ?
              AND COALESCE(valid_to, ?) >= ?
            LIMIT 1
            """,
            (
                fraction_id,
                regra["rubric"],
                0 if ignorar_id is None else 1,
                ignorar_id or 0,
                novo_fim,
                SEM_FIM,
                regra["valid_from"],
            ),
            one=True,
        )
        if colide is None:
            continue
        code = query_db("SELECT code FROM fractions WHERE id = ?", (fraction_id,), one=True)
        codigos.append(code["code"] if code else str(fraction_id))
    return codigos


@bp.route("/quotas/add", methods=["POST"])
def add_rule():
    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.quotas"))

    regra, erros = _ler_regra_do_form(request.form)
    if request.form.get("confirmar_vigencia") != "on":
        erros.append(
            "Registar uma regra exige confirmar que o valor e o período de vigência "
            "correspondem a uma decisão registada. Nenhuma regra foi guardada."
        )
    if regra is not None and not erros:
        colidem = _sobreposicoes(regra)
        if colidem:
            erros.append(
                f"Já existe uma regra para {ROTULOS_RUBRICA[regra['rubric']]} a valer no mesmo "
                f"período nas frações {', '.join(colidem)}. Encerra ou corrige a regra existente "
                "antes de registar outra. Nenhuma regra foi guardada."
            )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("quotas.quotas"))

    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.quotas"))

    connection = get_db()
    try:
        now = utc_now_iso()
        for fraction_id in regra["fraction_ids"]:
            connection.execute(
                """
                INSERT INTO charge_rules
                    (fraction_id, rubric, amount_cents, valid_from, valid_to, source,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fraction_id,
                    regra["rubric"],
                    regra["amount_cents"],
                    regra["valid_from"],
                    regra["valid_to"],
                    regra["source"],
                    now,
                    now,
                ),
            )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível registar a regra "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma regra foi guardada.",
            "erro",
        )
    else:
        flash(
            f"Regra registada: {ROTULOS_RUBRICA[regra['rubric']]} de "
            f"{format_money(regra['amount_cents'])} para {len(regra['fraction_ids'])} "
            f"fração(ões), com vigência confirmada.",
            "sucesso",
        )
    return redirect(url_for("quotas.quotas"))


@bp.route("/quotas/<int:rule_id>/vigencia", methods=["POST"])
def change_validity(rule_id: int):
    """Encerrar ou reabrir a vigência de uma regra, sem tocar nas cobranças emitidas."""
    row = query_db(
        "SELECT r.*, f.code FROM charge_rules r JOIN fractions f ON f.id = r.fraction_id "
        "WHERE r.id = ?",
        (rule_id,),
        one=True,
    )
    if row is None:
        abort(404)

    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.quotas"))

    fim_raw = request.form.get("fim", "").strip()
    fim = None if not fim_raw else parse_month(fim_raw)
    erros: list[str] = []
    if fim_raw and fim is None:
        erros.append("Fim da vigência: usa um mês, por exemplo 12/2026, ou deixa vazio.")
    if fim and month_end(fim) < row["valid_from"]:
        erros.append("O fim da vigência não pode ser anterior ao início da regra.")

    if not erros:
        proposta = {
            "fraction_ids": [row["fraction_id"]],
            "rubric": row["rubric"],
            "valid_from": row["valid_from"],
            "valid_to": month_end(fim) if fim else None,
        }
        colidem = _sobreposicoes(proposta, ignorar_id=rule_id)
        if colidem:
            erros.append(
                "A nova vigência sobrepõe-se a outra regra da mesma fração e rubrica. "
                "Nenhuma alteração foi guardada."
            )
        elif proposta["valid_to"]:
            fora = query_db(
                "SELECT COUNT(*) AS n FROM charges WHERE rule_id = ? AND period > ?",
                (rule_id, fim),
                one=True,
            )
            if int(fora["n"]) > 0:
                erros.append(
                    f"A nova vigência deixaria {int(fora['n'])} cobrança(s) já emitidas fora do "
                    "período desta regra. Encerra a regra no último mês já cobrado ou mais tarde; "
                    "as cobranças emitidas não são alteradas. Nenhuma alteração foi guardada."
                )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return redirect(url_for("quotas.quotas"))

    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.quotas"))

    connection = get_db()
    try:
        connection.execute(
            "UPDATE charge_rules SET valid_to = ?, updated_at = ? WHERE id = ?",
            (month_end(fim) if fim else None, utc_now_iso(), rule_id),
        )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível alterar a vigência "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
    else:
        emitidas = query_db(
            "SELECT COUNT(*) AS n FROM charges WHERE rule_id = ?", (rule_id,), one=True
        )
        flash(
            f"Vigência atualizada para a fração {row['code']} "
            f"({ROTULOS_RUBRICA[row['rubric']]}). As {int(emitidas['n'])} cobrança(s) já "
            "emitidas mantêm o valor, o mês e a regra de que vieram.",
            "sucesso",
        )
    return redirect(url_for("quotas.quotas"))


@bp.route("/quotas/<int:rule_id>/delete", methods=["POST"])
def delete_rule(rule_id: int):
    """Eliminar uma regra só enquanto nenhuma cobrança depende dela."""
    row = query_db(
        "SELECT r.*, f.code FROM charge_rules r JOIN fractions f ON f.id = r.fraction_id "
        "WHERE r.id = ?",
        (rule_id,),
        one=True,
    )
    if row is None:
        abort(404)

    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.quotas"))

    emitidas = query_db(
        "SELECT COUNT(*) AS n FROM charges WHERE rule_id = ?", (rule_id,), one=True
    )
    if int(emitidas["n"]) > 0:
        flash(
            f"A regra de {row['code']} ({ROTULOS_RUBRICA[row['rubric']]}) já originou "
            f"{int(emitidas['n'])} cobrança(s) e não pode ser eliminada: as cobranças "
            "perdem a referência de que vieram. Encerra a vigência em vez de eliminar.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    if request.form.get("confirmar_eliminar") != "on":
        flash(
            "Eliminar uma regra exige confirmação explícita. Nenhuma regra foi eliminada.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.quotas"))

    connection = get_db()
    try:
        connection.execute("DELETE FROM charge_rules WHERE id = ?", (rule_id,))
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        flash(
            "Não foi possível eliminar a regra: alguma cobrança depende dela. "
            "Nenhuma alteração foi aplicada.",
            "erro",
        )
    else:
        flash(
            f"Regra eliminada (fração {row['code']}, {ROTULOS_RUBRICA[row['rubric']]}): "
            "não tinha cobranças emitidas.",
            "sucesso",
        )
    return redirect(url_for("quotas.quotas"))


# ---------------------------------------------------------------------------
# Plano de geração de um mês
# ---------------------------------------------------------------------------


def _regras_do_mes(period: str) -> list[sqlite3.Row]:
    """Regras recorrentes a valer num mês.

    As quotas extraordinárias ficam de fora: não são uma regra que se repita, e são
    emitidas pelo formulário próprio, com as suas prestações.
    """
    return query_db(
        """
        SELECT r.id, r.fraction_id, r.rubric, r.amount_cents, r.valid_from, r.valid_to,
               f.code, f.active
        FROM charge_rules r
        JOIN fractions f ON f.id = r.fraction_id
        WHERE f.active = 1
          AND r.rubric <> 'quota_extraordinaria'
          AND r.valid_from <= ?
          AND (r.valid_to IS NULL OR r.valid_to >= ?)
        ORDER BY f.code, r.rubric, r.id
        """,
        (month_start(period), month_end(period)),
    )


def _dia_de_vencimento() -> int | None:
    """Dia de vencimento confirmado, ou None quando está por confirmar."""
    dia = parse_int_id(get_setting("quota_dia_vencimento"))
    if dia is None or not 1 <= dia <= 28:
        return None
    return dia


def plano_de_geracao(period: str) -> dict:
    """Calcular as cobranças de um mês a partir das regras em vigor.

    É o mesmo cálculo para a pré-visualização e para a gravação: o administrador
    confirma exatamente aquilo que viu. Devolve as linhas do plano, os totais por
    rubrica e os erros que impedem a geração.
    """
    erros: list[str] = []
    fracoes = query_db("SELECT id, code FROM fractions WHERE active = 1 ORDER BY code")
    if not fracoes:
        erros.append("Não há frações ativas: não há quotas a gerar.")

    agrupadas: dict[tuple[int, str], list[sqlite3.Row]] = {}
    for row in _regras_do_mes(period):
        agrupadas.setdefault((row["fraction_id"], row["rubric"]), []).append(row)

    for (fraction_id, rubrica), grupo in agrupadas.items():
        if len(grupo) > 1:
            code = query_db(
                "SELECT code FROM fractions WHERE id = ?", (fraction_id,), one=True
            )["code"]
            datas = ", ".join(
                f"{format_date(r['valid_from'])}–{format_date(r['valid_to']) if r['valid_to'] else 'em aberto'}"
                for r in grupo
            )
            erros.append(
                f"Fração {code}: {len(grupo)} regras de {ROTULOS_RUBRICA[rubrica]} a valer "
                f"em {format_month(period)} ({datas}). Corrige a sobreposição antes de gerar."
            )

    codigos_com_quota = {
        fraction_id
        for (fraction_id, rubrica), grupo in agrupadas.items()
        if rubrica == "quota_ordinaria" and len(grupo) == 1
    }
    for fracao in fracoes:
        if fracao["id"] in codigos_com_quota:
            continue
        erros.append(
            f"Fração {fracao['code']}: não há regra de quota ordinária com vigência "
            "confirmada para este mês. Regista a regra em Quotas com a data de início "
            "confirmada antes de gerar este mês."
        )

    inicio_gestao = get_setting("gestao_data_inicio").strip()
    if inicio_gestao and period < inicio_gestao[:7]:
        erros.append(
            f"{format_month(period)} é anterior à data de início da gestão "
            f"({format_date(inicio_gestao)}). Confirma a data em Configuração ou gera "
            "a partir do mês em que a gestão começou."
        )

    ja_existentes = {
        (row["fraction_id"], row["rubric"])
        for row in query_db("SELECT fraction_id, rubric FROM charges WHERE period = ?", (period,))
    }

    linhas: list[dict] = []
    totais = {rubrica: 0 for rubrica in RUBRICAS}
    totais_novas = {rubrica: 0 for rubrica in RUBRICAS}
    novas = 0
    repetidas = 0
    for (fraction_id, rubrica), grupo in sorted(
        agrupadas.items(), key=lambda item: (item[0][0], RUBRICAS.index(item[0][1]))
    ):
        if len(grupo) != 1:
            continue
        regra = grupo[0]
        code = query_db("SELECT code FROM fractions WHERE id = ?", (fraction_id,), one=True)["code"]
        valor = int(regra["amount_cents"])
        existe = (fraction_id, rubrica) in ja_existentes
        totais[rubrica] += valor
        if existe:
            repetidas += 1
        else:
            novas += 1
            totais_novas[rubrica] += valor
        linhas.append(
            {
                "fraction_id": fraction_id,
                "code": code,
                "rubric": rubrica,
                "rubrica_texto": ROTULOS_RUBRICA[rubrica],
                "amount_cents": valor,
                "amount_texto": format_money(valor),
                "rule_id": regra["id"],
                "nova": not existe,
            }
        )

    dia = _dia_de_vencimento()
    return {
        "period": period,
        "period_texto": format_month(period),
        "linhas": linhas,
        "erros": erros,
        "totais": totais,
        "total": sum(totais.values()),
        "totais_novas": totais_novas,
        "total_novas": sum(totais_novas.values()),
        "novas": novas,
        "repetidas": repetidas,
        "dia_vencimento": dia,
        "due_date": f"{period}-{dia:02d}" if dia else None,
        "vencimento_texto": (
            f"{dia:02d}/{period[5:7]}/{period[:4]}" if dia else "por confirmar"
        ),
    }


@bp.route("/quotas/generate", methods=["GET", "POST"])
def generate():
    """Pré-visualizar um mês e, depois de confirmado, gravá-lo."""
    metodo = request.method
    if metodo == "POST":
        period = parse_month(request.form.get("period", ""))
        if period is None:
            flash("Mês a gerar: usa um mês, por exemplo 01/2026.", "erro")
            return redirect(url_for("quotas.generate"))
    else:
        period = parse_month(request.args.get("period", "")) or current_month()

    if metodo == "POST" and _recusar_se_desatualizado("geracao"):
        return redirect(url_for("quotas.generate", period=period))

    if metodo == "POST" and request.form.get("confirmar_geracao") != "on":
        flash(
            "Gerar cobranças exige confirmação explícita do mês e dos valores "
            "apresentados na pré-visualização. Nada foi gerado.",
            "erro",
        )
        return redirect(url_for("quotas.generate", period=period))

    if metodo == "POST":
        if not transacao_de_escrita("geracao"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("quotas.generate", period=period))

        plano = plano_de_geracao(period)
        if plano["erros"]:
            get_db().rollback()
            for mensagem in plano["erros"]:
                flash(mensagem, "erro")
            flash("Nenhuma cobrança foi gerada.", "erro")
            return redirect(url_for("quotas.generate", period=period))

        connection = get_db()
        try:
            now = utc_now_iso()
            for linha in plano["linhas"]:
                if not linha["nova"]:
                    continue
                connection.execute(
                    """
                    INSERT INTO charges
                        (fraction_id, rule_id, rubric, period, amount_cents, due_date,
                         generated_at, descricao)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '')
                    ON CONFLICT (fraction_id, rubric, period)
                    WHERE rubric IN ('quota_ordinaria', 'reforco_fundo') DO NOTHING
                    """,
                    (
                        linha["fraction_id"],
                        linha["rule_id"],
                        linha["rubric"],
                        period,
                        linha["amount_cents"],
                        plano["due_date"],
                        now,
                    ),
                )
            connection.commit()
        except sqlite3.Error as erro:
            connection.rollback()
            flash(
                "Não foi possível gerar o mês "
                f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma cobrança foi gravada.",
                "erro",
            )
            return redirect(url_for("quotas.generate", period=period))

        flash(
            f"{plano['novas']} cobrança(s) gerada(s) para {plano['period_texto']}: "
            f"{format_money(plano['totais_novas']['quota_ordinaria'])} de quota ordinária e "
            f"{format_money(plano['totais_novas']['reforco_fundo'])} de reforço do fundo "
            f"({format_money(plano['total_novas'])} novos). "
            f"{plano['repetidas']} já existiam e não foram duplicadas. "
            "Valores emitidos, não dinheiro recebido.",
            "sucesso",
        )
        return redirect(url_for("quotas.charge_month", period=period))

    plano = plano_de_geracao(period)
    return render_template(
        "quota_generate.html",
        plano=plano,
        anterior=previous_month(period),
        seguinte=next_month(period),
        meses=query_db("SELECT DISTINCT period FROM charges ORDER BY period"),
    )


# ---------------------------------------------------------------------------
# Consulta das cobranças
# ---------------------------------------------------------------------------


@bp.route("/charges")
def charges():
    """Meses com cobranças emitidas, com o total separado por rubrica."""
    meses = query_db(
        """
        SELECT period,
               COUNT(*) AS n,
               SUM(CASE WHEN rubric = 'quota_ordinaria' THEN amount_cents ELSE 0 END) AS ordinario,
               SUM(CASE WHEN rubric = 'reforco_fundo' THEN amount_cents ELSE 0 END) AS reforco,
               SUM(CASE WHEN rubric = 'quota_extraordinaria' THEN amount_cents ELSE 0 END) AS extraordinaria,
               SUM(amount_cents) AS total
        FROM charges
        GROUP BY period
        ORDER BY period
        """
    )
    return render_template(
        "charges_list.html",
        meses=meses,
        total_geral=sum(int(mes["total"]) for mes in meses),
        total_emitidas=sum(int(mes["n"]) for mes in meses),
    )


@bp.route("/charges/<period>")
def charge_month(period: str):
    period = parse_month(period) or ""
    if not period:
        abort(404)

    # O liquidado vem de pagamentos que não foram anulados: é a mesma regra usada em
    # `app/payments.py`, escrita aqui em vez de importada para não criar um ciclo
    # entre os dois módulos.
    linhas = query_db(
        """
        SELECT c.id, c.rubric, c.amount_cents, c.due_date, c.generated_at, c.period,
               c.descricao, f.code, r.valid_from, r.valid_to, r.source,
               COALESCE((
                   SELECT SUM(a.amount_cents)
                   FROM payment_allocations AS a
                   JOIN payments AS p ON p.id = a.payment_id
                   WHERE a.charge_id = c.id AND p.cancelled_at IS NULL
               ), 0) AS pago
        FROM charges c
        JOIN fractions f ON f.id = c.fraction_id
        JOIN charge_rules r ON r.id = c.rule_id
        WHERE c.period = ?
        ORDER BY f.code, c.rubric, c.id
        """,
        (period,),
    )
    if not linhas:
        return render_template(
            "charges_month.html",
            period=period,
            period_texto=format_month(period),
            linhas=[],
            totais={rubrica: 0 for rubrica in RUBRICAS_TODAS},
            total=0,
            vencimento_texto="por confirmar",
            anterior=previous_month(period),
            seguinte=next_month(period),
        )

    hoje = today_iso()
    totais = {rubrica: 0 for rubrica in RUBRICAS_TODAS}
    vistas: list[dict] = []
    for linha in linhas:
        valor = int(linha["amount_cents"])
        pago = int(linha["pago"])
        em_divida = valor - pago
        totais[linha["rubric"]] += valor
        # Só está vencida se ainda tiver montante por liquidar: uma cobrança paga
        # não volta a estar em dívida, ainda que o vencimento já tenha passado.
        vencida = bool(linha["due_date"]) and linha["due_date"] < hoje and em_divida > 0
        vistas.append(
            {
                "id": linha["id"],
                "code": linha["code"],
                "rubric": linha["rubric"],
                "rubrica_texto": ROTULOS_RUBRICA[linha["rubric"]],
                "descricao": linha["descricao"] or "",
                "amount_cents": valor,
                "amount_texto": format_money(valor),
                "pago_texto": format_money(pago),
                "em_divida_texto": format_money(em_divida),
                "liquidada": em_divida == 0,
                "due_date": linha["due_date"] or "",
                "due_texto": format_date(linha["due_date"]) if linha["due_date"] else "por confirmar",
                "vencida": vencida,
                "regra": f"{format_date(linha['valid_from'])}–"
                f"{format_date(linha['valid_to']) if linha['valid_to'] else 'em aberto'}",
                "source": linha["source"],
            }
        )

    # O vencimento apresentado é o que está gravado nas cobranças deste mês, não a
    # definição atual: mudar o dia amanhã não reescreve o que já foi emitido.
    datas = {linha["due_date"] for linha in linhas if linha["due_date"]}
    if not datas:
        vencimento_texto = "por confirmar"
    elif len(datas) == 1:
        vencimento_texto = format_date(next(iter(datas)))
    else:
        vencimento_texto = "diferente entre cobranças"

    return render_template(
        "charges_month.html",
        period=period,
        period_texto=format_month(period),
        linhas=vistas,
        totais=totais,
        total=sum(totais.values()),
        vencimento_texto=vencimento_texto,
        anterior=previous_month(period),
        seguinte=next_month(period),
    )


# ---------------------------------------------------------------------------
# Entrega 2: quota extraordinária
# ---------------------------------------------------------------------------


def _novo_token_de_formulario() -> str:
    """Marca única deste formulário, gravada na quota que ele originar."""
    return secrets.token_urlsafe(16)


def _fracoes_selecionadas_do_form(form) -> tuple[list[sqlite3.Row], list[str]]:
    """Frações marcadas no formulário, e códigos das desativadas que foram marcadas."""
    selecionadas: list[sqlite3.Row] = []
    desativadas: list[str] = []
    for row in query_db("SELECT id, code, permilagem, active FROM fractions ORDER BY code"):
        if form.get(f"fracao_{row['id']}") != "on":
            continue
        if row["active"]:
            selecionadas.append(row)
        else:
            desativadas.append(row["code"])
    return selecionadas, desativadas


def _prestacoes_do_form(form) -> tuple[list[str], list[str]]:
    """Datas das prestações, por ordem, e os erros de leitura.

    Não há um campo "número de prestações": uma prestação única é uma só data
    preenchida, e um calendário é várias. As datas preenchidas têm de ser consecutivas
    e cada uma tem de cair num mês diferente, porque uma fração não pode ter duas
    cobranças do mesmo mês para a mesma quota.
    """
    datas: list[str] = []
    erros: list[str] = []
    preenchidas: list[int] = []
    for numero in range(1, MAXIMO_PRESTACOES + 1):
        bruto = form.get(f"prestacao_{numero}", "").strip()
        if not bruto:
            continue
        preenchidas.append(numero)
        data = parse_date(bruto)
        if data is None:
            erros.append(
                f"Prestação {numero}: escreve a data como DD/MM/AAAA, por exemplo 15/03/2027."
            )
            continue
        datas.append(data)

    faltam = [n for n in range(1, (max(preenchidas) if preenchidas else 0) + 1) if n not in preenchidas]
    if faltam:
        erros.append(
            "Prestações: preenche as datas por ordem, começando na primeira. "
            f"Falta a data da prestação {', '.join(str(n) for n in faltam)}."
        )
    if datas:
        meses: dict[str, list[int]] = {}
        for indice, data in enumerate(datas, start=1):
            meses.setdefault(data[:7], []).append(indice)
        repetidos = {mes for mes, numeros in meses.items() if len(numeros) > 1}
        if repetidos:
            erros.append(
                "Prestações: cada prestação tem de cair num mês diferente. Repetido em "
                + ", ".join(format_month(mes) for mes in sorted(repetidos))
                + "."
            )
    return datas, erros


def _ler_extraordinaria_do_form(form) -> tuple[dict | None, list[str]]:
    """Ler e validar o formulário de quota extraordinária. Devolve (dados, erros)."""
    erros: list[str] = []

    descricao = form.get("descricao", "").strip()
    if not descricao:
        erros.append(
            "Descrição: escreve o que é esta quota, por exemplo «Reparação da cobertura». "
            "É o texto que os condóminos veem junto da cobrança."
        )
    elif len(descricao) > LIMITE_DESCRICAO:
        erros.append(f"Descrição: no máximo {LIMITE_DESCRICAO} caracteres.")

    finalidade = form.get("finalidade", "").strip()
    if len(finalidade) > LIMITE_FINALIDADE:
        erros.append(f"Finalidade: no máximo {LIMITE_FINALIDADE} caracteres.")

    total = parse_money(form.get("total", ""))
    if total is None or total <= 0:
        erros.append("Montante total: usa um valor positivo em euros, por exemplo 1.250,00.")
    elif total > VALOR_MAXIMO_EXTRAORDINARIA_CENTIMOS:
        erros.append(
            "Montante total: acima de 100.000,00 EUR parece um erro de escrita. "
            "Confirma o valor na ata antes de o registar."
        )

    reparticao = form.get("reparticao", "").strip()
    if reparticao not in REPARTICOES:
        erros.append(
            "Forma de repartição: escolhe igual por fração, proporcional à permilagem, "
            "ou manual por fração."
        )

    fracoes, desativadas = _fracoes_selecionadas_do_form(form)
    if desativadas:
        erros.append(
            f"Frações desativadas não podem entrar numa quota extraordinária: {', '.join(desativadas)}."
        )
    if not fracoes:
        erros.append("Frações abrangidas: escolhe pelo menos uma fração.")

    prestacoes, erros_de_prestacoes = _prestacoes_do_form(form)
    erros.extend(erros_de_prestacoes)

    valores_manuais: dict[int, int] = {}
    if reparticao == "manual":
        for fracao in fracoes:
            valor = parse_money(form.get(f"manual_{fracao['id']}", ""))
            if valor is None or valor <= 0:
                erros.append(
                    f"Repartição manual: indica um valor em euros para a fração {fracao['code']}."
                )
            else:
                valores_manuais[int(fracao["id"])] = valor

    if erros:
        return None, erros

    return (
        {
            "descricao": descricao,
            "finalidade": finalidade,
            "total_cents": total,
            "reparticao": reparticao,
            "fracoes": fracoes,
            "prestacoes": prestacoes,
            "valores_manuais": valores_manuais,
            "source": form.get("source", "").strip(),
        },
        [],
    )


def _plano_extraordinaria(dados: dict) -> dict:
    """Calcular as quotas-parte por fração e as prestações de cada quota-parte.

    É o mesmo cálculo para a pré-visualização e para a emissão: o administrador
    confirma exatamente aquilo que viu. O total é conservado ao cêntimo nas duas
    divisões — entre frações e entre prestações.
    """
    erros: list[str] = []
    fracoes = dados["fracoes"]
    prestacoes = dados["prestacoes"]
    total = dados["total_cents"]

    quotas_parte: list[int] = []
    if dados["reparticao"] == "igual":
        quotas_parte = repartir_igual(total, len(fracoes))
    elif dados["reparticao"] == "permilagem":
        quotas_parte = repartir(total, [int(fracao["permilagem"]) for fracao in fracoes])
    else:
        quotas_parte = [dados["valores_manuais"].get(int(fracao["id"]), 0) for fracao in fracoes]
        soma = sum(quotas_parte)
        if soma != total:
            diferenca = soma - total
            erros.append(
                f"Repartição manual: as frações somam {format_money(soma)} e o montante total "
                f"da quota é {format_money(total)}; {'falta' if diferenca < 0 else 'sobra'} "
                f"{format_money(abs(diferenca))}. A soma tem de ser exatamente o total antes de "
                "emitir."
            )
            quotas_parte = []

    linhas: list[dict] = []
    for fracao, quota_parte in zip(fracoes, quotas_parte):
        try:
            partes = repartir_em_prestacoes(quota_parte, len(prestacoes))
        except ValueError:
            erros.append(f"Fração {fracao['code']}: a quota-parte não é um valor positivo.")
            continue
        linhas.append(
            {
                "fraction_id": int(fracao["id"]),
                "code": fracao["code"],
                "permilagem_texto": format_permilagem(int(fracao["permilagem"])),
                "amount_cents": quota_parte,
                "amount_texto": format_money(quota_parte),
                "prestacoes": [
                    {
                        "numero": indice,
                        "period": data[:7],
                        "due_date": data,
                        "due_texto": format_date(data),
                        "amount_cents": parte,
                        "amount_texto": format_money(parte),
                    }
                    for indice, (data, parte) in enumerate(zip(prestacoes, partes), start=1)
                ],
            }
        )

    colunas: list[dict] = []
    for indice, data in enumerate(prestacoes, start=1):
        total_prestacao = sum(linha["prestacoes"][indice - 1]["amount_cents"] for linha in linhas)
        colunas.append(
            {
                "numero": indice,
                "period": data[:7],
                "due_date": data,
                "due_texto": format_date(data),
                "amount_cents": total_prestacao,
                "amount_texto": format_money(total_prestacao),
            }
        )

    total_emitido = sum(linha["amount_cents"] for linha in linhas)
    if linhas and total_emitido != total:
        erros.append(
            f"A repartição daria {format_money(total_emitido)} em vez de {format_money(total)}. "
            "Nada foi emitido."
        )
    if linhas and any(linha["amount_cents"] <= 0 for linha in linhas):
        erros.append(
            f"{format_money(total)} repartido por {len(fracoes)} frações deixaria alguma fração "
            "com 0,00 EUR. Aumenta o montante total ou abrange menos frações."
        )
    if linhas and len(prestacoes) > 1 and any(
        parte["amount_cents"] <= 0 for linha in linhas for parte in linha["prestacoes"]
    ):
        erros.append(
            f"{len(prestacoes)} prestações deixariam alguma fração com uma prestação de 0,00 EUR. "
            "Reduz o número de prestações ou aumenta o montante total."
        )

    return {
        "fracoes": linhas,
        "prestacoes": colunas,
        "erros": erros,
        "total_cents": total,
        "total_texto": format_money(total),
        "total_emitido": total_emitido,
        "cobrancas": len(linhas) * len(prestacoes),
        "primeiro_mes": prestacoes[0][:7] if prestacoes else "",
        "ultimo_mes": prestacoes[-1][:7] if prestacoes else "",
    }


def _valores_reapresentados(form) -> dict:
    """O que o formulário tinha, para a página voltar a mostrá-lo sem perda."""
    fracoes, _ = _fracoes_selecionadas_do_form(form)
    prestacoes = {
        numero: form.get(f"prestacao_{numero}", "").strip()
        for numero in range(1, MAXIMO_PRESTACOES + 1)
    }
    preenchidas = sum(1 for data in prestacoes.values() if data)
    return {
        "descricao": form.get("descricao", "").strip(),
        "finalidade": form.get("finalidade", "").strip(),
        "total": form.get("total", "").strip(),
        "reparticao": form.get("reparticao", "igual"),
        "source": form.get("source", "").strip(),
        "selecionadas": {int(fracao["id"]) for fracao in fracoes},
        "prestacoes": prestacoes,
        # Uma slot livre a mais do que as datas preenchidas: é assim que se acrescenta
        # uma prestação sem JavaScript e sem mostrar doze campos de uma vez.
        "calendario": max(1, min(MAXIMO_PRESTACOES, preenchidas + 1)),
        "manuais": {
            chave.removeprefix("manual_"): form.get(chave, "")
            for chave in form
            if chave.startswith("manual_")
        },
    }


def _pagina_extraordinaria(valores: dict, plano: dict | None, token: str):
    return render_template(
        "quota_extraordinaria.html",
        valores=valores,
        plano=plano,
        token=token,
        fracoes=query_db("SELECT id, code, permilagem, active FROM fractions ORDER BY code"),
        REPARTICOES=REPARTICOES,
        ROTULOS_REPARTICAO=ROTULOS_REPARTICAO,
        MAXIMO_PRESTACOES=MAXIMO_PRESTACOES,
        calendario=valores.get("calendario", 1),
    )


@bp.route("/quotas/extraordinaria", methods=["GET", "POST"])
def extraordinary_quota():
    """Criar uma quota extraordinária: pré-visualizar a repartição e, confirmado o
    plano, emitir as cobranças de cada fração e de cada prestação."""
    if request.method == "GET":
        return _pagina_extraordinaria({}, None, _novo_token_de_formulario())

    token = request.form.get("form_token", "").strip()
    if not token:
        flash(
            "Este formulário não tem a marca de envio: reabre a página da quota "
            "extraordinária e tenta de novo. Nada foi gravado.",
            "erro",
        )
        return redirect(url_for("quotas.extraordinary_quota"))

    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.extraordinary_quota"))

    dados, erros = _ler_extraordinaria_do_form(request.form)
    valores = _valores_reapresentados(request.form)
    plano = _plano_extraordinaria(dados) if dados is not None else None
    if plano is not None:
        erros.extend(plano["erros"])

    previsualizar = request.form.get("previsualizar") == "on"
    confirmar = request.form.get("confirmar_emissao") == "on"
    if not previsualizar and not confirmar:
        erros.append(
            "Pré-visualiza a repartição antes de emitir: é na pré-visualização que se "
            "veem as quotas-parte e as prestações. Nada foi gravado."
        )
    if confirmar and request.form.get("confirmar_extraordinaria") != "on":
        erros.append(
            "Emitir uma quota extraordinária exige confirmar que o montante, a repartição "
            "e as datas correspondem a uma decisão registada. Nada foi gravado."
        )

    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return _pagina_extraordinaria(valores, plano, token)

    if previsualizar:
        return _pagina_extraordinaria(valores, plano, token)

    return _emitir_extraordinaria(dados, plano, token)


def _emitir_extraordinaria(dados: dict, plano: dict, token: str):
    """Gravar a quota, as prestações, as regras por fração e as cobranças, tudo junto."""
    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.extraordinary_quota"))

    connection = get_db()
    quota_id = 0
    try:
        now = utc_now_iso()
        quota_id = int(
            connection.execute(
                """
                INSERT INTO extraordinary_quotas
                    (descricao, finalidade, total_cents, reparticao, form_token,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dados["descricao"],
                    dados["finalidade"],
                    dados["total_cents"],
                    dados["reparticao"],
                    token,
                    now,
                    now,
                ),
            ).lastrowid
        )
        for prestacao in plano["prestacoes"]:
            connection.execute(
                "INSERT INTO quota_installments (quota_id, numero, due_date) VALUES (?, ?, ?)",
                (quota_id, prestacao["numero"], prestacao["due_date"]),
            )
        valid_from = month_start(plano["primeiro_mes"])
        valid_to = month_end(plano["ultimo_mes"])
        for linha in plano["fracoes"]:
            rule_id = int(
                connection.execute(
                    """
                    INSERT INTO charge_rules
                        (fraction_id, rubric, quota_id, descricao, amount_cents,
                         valid_from, valid_to, source, created_at, updated_at)
                    VALUES (?, 'quota_extraordinaria', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        linha["fraction_id"],
                        quota_id,
                        dados["descricao"],
                        linha["amount_cents"],
                        valid_from,
                        valid_to,
                        dados["source"],
                        now,
                        now,
                    ),
                ).lastrowid
            )
            for prestacao in linha["prestacoes"]:
                connection.execute(
                    """
                    INSERT INTO charges
                        (fraction_id, rule_id, rubric, period, amount_cents, due_date,
                         generated_at, descricao)
                    VALUES (?, ?, 'quota_extraordinaria', ?, ?, ?, ?, ?)
                    """,
                    (
                        linha["fraction_id"],
                        rule_id,
                        prestacao["period"],
                        prestacao["amount_cents"],
                        prestacao["due_date"],
                        now,
                        dados["descricao"],
                    ),
                )
        connection.commit()
    except sqlite3.IntegrityError as erro:
        connection.rollback()
        detalhe = getattr(erro, "sqlite_error", str(erro))
        if "form_token" in detalhe:
            flash(
                "Esta quota extraordinária já foi registada: o envio foi repetido e não "
                "foi emitida uma segunda cobrança. Consulta as quotas extraordinárias "
                "para ver as cobranças que já existem.",
                "erro",
            )
        else:
            flash(
                f"Não foi possível emitir a quota extraordinária ({detalhe}). "
                "Nenhuma cobrança foi gravada.",
                "erro",
            )
        return redirect(url_for("quotas.extraordinary_quotas"))
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível emitir a quota extraordinária "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma cobrança foi gravada.",
            "erro",
        )
        return redirect(url_for("quotas.extraordinary_quota"))

    flash(
        f"Quota extraordinária emitida: {dados['descricao']} — {format_money(dados['total_cents'])} "
        f"por {len(plano['fracoes'])} fração(ões) em {len(plano['prestacoes'])} prestação(ões), "
        f"{plano['cobrancas']} cobrança(s) emitida(s). Valores emitidos, não dinheiro recebido.",
        "sucesso",
    )
    return redirect(url_for("quotas.extraordinary_detail", quota_id=quota_id))


def _quota_extraordinaria_view(quota_id: int) -> dict | None:
    quota = query_db("SELECT * FROM extraordinary_quotas WHERE id = ?", (quota_id,), one=True)
    if quota is None:
        return None
    prestacoes = query_db(
        "SELECT numero, due_date FROM quota_installments WHERE quota_id = ? ORDER BY numero",
        (quota_id,),
    )
    regras = query_db(
        """
        SELECT r.id, r.fraction_id, r.amount_cents, r.valid_from, r.valid_to, f.code
        FROM charge_rules r
        JOIN fractions f ON f.id = r.fraction_id
        WHERE r.quota_id = ?
        ORDER BY f.code
        """,
        (quota_id,),
    )
    cobranca_por_regra = query_db(
        """
        SELECT c.rule_id, c.id, c.period, c.amount_cents, c.due_date
        FROM charges c
        JOIN charge_rules r ON r.id = c.rule_id
        WHERE r.quota_id = ?
        ORDER BY c.rule_id, c.due_date
        """,
        (quota_id,),
    )
    por_regra: dict[int, list[dict]] = {}
    for linha in cobranca_por_regra:
        por_regra.setdefault(int(linha["rule_id"]), []).append(
            {
                "charge_id": int(linha["id"]),
                "period_texto": format_month(linha["period"]),
                "amount_texto": format_money(int(linha["amount_cents"])),
                "due_texto": format_date(linha["due_date"]) if linha["due_date"] else "por confirmar",
            }
        )
    emitidas = sum(len(itens) for itens in por_regra.values())
    fonte = query_db(
        "SELECT source FROM charge_rules WHERE quota_id = ? AND source <> '' LIMIT 1",
        (quota_id,),
        one=True,
    )
    return {
        "id": int(quota["id"]),
        "descricao": quota["descricao"],
        "finalidade": quota["finalidade"] or "",
        "total_cents": int(quota["total_cents"]),
        "total_texto": format_money(int(quota["total_cents"])),
        "reparticao": quota["reparticao"],
        "reparticao_texto": ROTULOS_REPARTICAO[quota["reparticao"]],
        "source": fonte["source"] if fonte else "",
        "created_texto": format_date(quota["created_at"][:10]),
        "prestacoes": [
            {"numero": p["numero"], "due_texto": format_date(p["due_date"])} for p in prestacoes
        ],
        "fracoes": [
            {
                "code": regra["code"],
                "fraction_id": int(regra["fraction_id"]),
                "amount_texto": format_money(int(regra["amount_cents"])),
                "rule_id": int(regra["id"]),
                "cobrancas": por_regra.get(int(regra["id"]), []),
            }
            for regra in regras
        ],
        "emitidas": emitidas,
    }


@bp.route("/quotas/extraordinarias")
def extraordinary_quotas():
    """Lista das quotas extraordinárias decididas, com o estado de emissão."""
    linhas = []
    for row in query_db("SELECT id FROM extraordinary_quotas ORDER BY id DESC"):
        vista = _quota_extraordinaria_view(int(row["id"]))
        if vista is not None:
            linhas.append(vista)
    return render_template("extraordinary_list.html", quotas=linhas)


@bp.route("/quotas/extraordinarias/<int:quota_id>")
def extraordinary_detail(quota_id: int):
    vista = _quota_extraordinaria_view(quota_id)
    if vista is None:
        abort(404)
    return render_template("extraordinary_detail.html", quota=vista)


@bp.route("/quotas/extraordinarias/<int:quota_id>/delete", methods=["POST"])
def delete_extraordinary(quota_id: int):
    """Eliminar uma quota extraordinária só enquanto nenhuma cobrança foi emitida."""
    quota = query_db("SELECT * FROM extraordinary_quotas WHERE id = ?", (quota_id,), one=True)
    if quota is None:
        abort(404)
    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.extraordinary_quotas"))

    emitidas = query_db(
        """
        SELECT COUNT(*) AS n FROM charges c
        JOIN charge_rules r ON r.id = c.rule_id
        WHERE r.quota_id = ?
        """,
        (quota_id,),
        one=True,
    )
    if int(emitidas["n"]) > 0:
        flash(
            f"A quota «{quota['descricao']}» já originou {int(emitidas['n'])} cobrança(s) e não "
            "pode ser eliminada: as cobranças perderiam a referência de que vieram. Corrige as "
            "cobranças uma a uma, com motivo.",
            "erro",
        )
        return redirect(url_for("quotas.extraordinary_detail", quota_id=quota_id))

    if request.form.get("confirmar_eliminar") != "on":
        flash(
            "Eliminar uma quota extraordinária exige confirmação explícita. Nada foi eliminado.",
            "erro",
        )
        return redirect(url_for("quotas.extraordinary_detail", quota_id=quota_id))

    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.extraordinary_quotas"))

    connection = get_db()
    try:
        connection.execute("DELETE FROM quota_installments WHERE quota_id = ?", (quota_id,))
        connection.execute("DELETE FROM charge_rules WHERE quota_id = ?", (quota_id,))
        connection.execute("DELETE FROM extraordinary_quotas WHERE id = ?", (quota_id,))
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        flash(
            "Não foi possível eliminar a quota: alguma cobrança ou afetação depende dela. "
            "Nenhuma alteração foi aplicada.",
            "erro",
        )
    else:
        flash(
            f"Quota extraordinária «{quota['descricao']}» eliminada: não tinha cobranças emitidas.",
            "sucesso",
        )
    return redirect(url_for("quotas.extraordinary_quotas"))


# ---------------------------------------------------------------------------
# Entrega 2: editar o valor de uma regra (só emissões futuras)
# ---------------------------------------------------------------------------


@bp.route("/quotas/<int:rule_id>/valor", methods=["POST"])
def change_rule_value(rule_id: int):
    """Mudar o valor de uma regra para as emissões futuras, com motivo registado.

    As cobranças já emitidas não são tocadas: cada uma guarda o valor que tinha.
    """
    regra = query_db(
        """
        SELECT r.*, f.code FROM charge_rules r
        JOIN fractions f ON f.id = r.fraction_id
        WHERE r.id = ?
        """,
        (rule_id,),
        one=True,
    )
    if regra is None:
        abort(404)

    if _recusar_se_desatualizado("regras"):
        return redirect(url_for("quotas.quotas"))

    if regra["rubric"] == "quota_extraordinaria":
        flash(
            "O valor de uma quota extraordinária define-se na quota, não na regra de cada "
            "fração. Se ainda não foi emitida, elimina a quota e regista outra; se já foi "
            "emitida, corrige as cobranças uma a uma, com motivo.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    valor = parse_money(request.form.get("valor", ""))
    if valor is None or valor <= 0:
        flash("Valor mensal: usa um valor positivo em euros, por exemplo 47,00.", "erro")
        return redirect(url_for("quotas.quotas"))
    if valor > VALOR_MAXIMO_CENTIMOS:
        flash(
            "Valor mensal: acima de 10.000,00 EUR parece um erro de escrita. "
            "Nenhuma regra foi alterada.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash(
            "Motivo da mudança: escreve porque este valor passa a ser outro (por exemplo "
            "«deliberado na ata n.º 23»). O motivo fica guardado com os valores anteriores. "
            "Nenhuma regra foi alterada.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))
    if len(motivo) > LIMITE_MOTIVO:
        flash(f"Motivo da mudança: no máximo {LIMITE_MOTIVO} caracteres.", "erro")
        return redirect(url_for("quotas.quotas"))

    if request.form.get("confirmar_valor") != "on":
        flash(
            "Mudar o valor de uma regra exige confirmação explícita. Nenhuma regra foi alterada.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    if not transacao_de_escrita("regras"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.quotas"))

    connection = get_db()
    try:
        now = utc_now_iso()
        token = _novo_token_de_formulario()
        utilizador = _nome_do_utilizador_para_registo()
        connection.execute(
            """
            INSERT INTO charge_rule_revisions
                (rule_id, changed_at, motivo, changed_by, edit_token, amount_before,
                 amount_after, valid_to_before, valid_to_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                now,
                motivo,
                utilizador,
                token,
                int(regra["amount_cents"]),
                valor,
                regra["valid_to"],
                regra["valid_to"],
            ),
        )
        connection.execute(
            "UPDATE charge_rules SET amount_cents = ?, edit_token = ?, updated_at = ? WHERE id = ?",
            (valor, token, now, rule_id),
        )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível alterar o valor da regra "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma regra foi alterada.",
            "erro",
        )
        return redirect(url_for("quotas.quotas"))

    emitidas = int(
        query_db("SELECT COUNT(*) AS n FROM charges WHERE rule_id = ?", (rule_id,), one=True)["n"]
    )
    flash(
        f"Valor da regra de {regra['code']} alterado de {format_money(int(regra['amount_cents']))} "
        f"para {format_money(valor)}, com motivo registado. "
        + (
            f"As {emitidas} cobrança(s) já emitidas mantêm o valor que tinham: a mudança só "
            "vale para as emissões futuras."
            if emitidas
            else "Ainda não havia cobranças emitidas por esta regra."
        ),
        "sucesso",
    )
    return redirect(url_for("quotas.quotas"))


def _nome_do_utilizador_para_registo() -> str:
    """Nome de quem fez o registo. Não é inventado: vem da sessão, se houver."""
    from .auth import current_user

    utilizador = current_user()
    return str(utilizador["username"]) if utilizador else ""


# ---------------------------------------------------------------------------
# Entrega 2: corrigir uma cobrança emitida
# ---------------------------------------------------------------------------
#
# Uma cobrança emitida é um registo histórico, por isso não se apaga nem se reescreve
# à revelia. Corrigi-la é uma ação explícita: exige motivo, é registada com os valores
# anteriores e novos, é pré-visualizada antes de gravar, e nunca toca nos pagamentos nem
# nas suas ligações. Se o novo valor for incompatível com o que já foi pago, a correção
# é recusada e diz-se o que há de ajustar primeiro.

_ALOCADO_A_COBRANCA = """
    SELECT COALESCE(SUM(a.amount_cents), 0) AS total
    FROM payment_allocations AS a
    JOIN payments AS p ON p.id = a.payment_id
    WHERE a.charge_id = ? AND p.cancelled_at IS NULL
"""


def _cobranca_para_correcao(charge_id: int) -> dict | None:
    linha = query_db(
        """
        SELECT c.id, c.fraction_id, c.rule_id, c.rubric, c.period, c.amount_cents,
               c.due_date, c.descricao, c.generated_at, f.code, r.source
        FROM charges c
        JOIN fractions f ON f.id = c.fraction_id
        JOIN charge_rules r ON r.id = c.rule_id
        WHERE c.id = ?
        """,
        (charge_id,),
        one=True,
    )
    if linha is None:
        return None
    pago = int(query_db(_ALOCADO_A_COBRANCA, (charge_id,), one=True)["total"] or 0)
    pagamentos = query_db(
        """
        SELECT p.id, p.reference, p.amount_cents, p.paid_on, a.amount_cents AS afetado
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        WHERE a.charge_id = ? AND p.cancelled_at IS NULL
        ORDER BY p.id
        """,
        (charge_id,),
    )
    correcoes = query_db(
        """
        SELECT changed_at, motivo, changed_by, amount_before, amount_after,
               due_before, due_after, description_before, description_after
        FROM charge_revisions WHERE charge_id = ? ORDER BY id
        """,
        (charge_id,),
    )
    return {
        "id": int(linha["id"]),
        "fraction_id": int(linha["fraction_id"]),
        "code": linha["code"],
        "rubric": linha["rubric"],
        "rubrica_texto": ROTULOS_RUBRICA[linha["rubric"]],
        "period": linha["period"],
        "period_texto": format_month(linha["period"]),
        "amount_cents": int(linha["amount_cents"]),
        "amount_texto": format_money(int(linha["amount_cents"])),
        "due_date": linha["due_date"] or "",
        "due_texto": format_date(linha["due_date"]) if linha["due_date"] else "por confirmar",
        "descricao": linha["descricao"] or "",
        "pago_cents": pago,
        "pago_texto": format_money(pago),
        "divida_texto": format_money(int(linha["amount_cents"]) - pago),
        "pagamentos": [
            {
                "id": int(p["id"]),
                "referencia": p["reference"] or "",
                "valor_texto": format_money(int(p["amount_cents"])),
                "afetado_texto": format_money(int(p["afetado"])),
                "data_texto": format_date(p["paid_on"]),
            }
            for p in pagamentos
        ],
        "correcoes": [
            {
                "momento_texto": format_date(c["changed_at"][:10]),
                "motivo": c["motivo"],
                "por": c["changed_by"] or "escrita direta",
                "antes_texto": format_money(int(c["amount_before"])),
                "depois_texto": format_money(int(c["amount_after"])),
                "vencimento_antes": format_date(c["due_before"]) if c["due_before"] else "por confirmar",
                "vencimento_depois": format_date(c["due_after"]) if c["due_after"] else "por confirmar",
            }
            for c in correcoes
        ],
    }


def _divida_em_aberta_da_fracao(fraction_id: int) -> int:
    """Quanto está por liquidar nesta fração, somando todas as suas cobranças."""
    linhas = query_db(
        """
        SELECT c.amount_cents,
               COALESCE((
                   SELECT SUM(a.amount_cents)
                   FROM payment_allocations AS a
                   JOIN payments AS p ON p.id = a.payment_id
                   WHERE a.charge_id = c.id AND p.cancelled_at IS NULL
               ), 0) AS pago
        FROM charges AS c
        WHERE c.fraction_id = ?
        """,
        (fraction_id,),
    )
    return sum(int(linha["amount_cents"]) - int(linha["pago"]) for linha in linhas)


def _efeitos_da_correcao(cobranca: dict, novo_valor: int, novo_vencimento: str | None) -> dict:
    """O que muda com esta correção, antes de ela ser gravada."""
    pago = cobranca["pago_cents"]
    divida_antes = cobranca["amount_cents"] - pago
    divida_depois = novo_valor - pago
    divida_fracao = _divida_em_aberta_da_fracao(cobranca["fraction_id"])
    total_mes = int(
        query_db(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM charges WHERE period = ?",
            (cobranca["period"],),
            one=True,
        )["total"]
    )
    return {
        "valor_antes_texto": format_money(cobranca["amount_cents"]),
        "valor_depois_texto": format_money(novo_valor),
        "divida_antes_texto": format_money(divida_antes),
        "divida_depois_texto": format_money(divida_depois),
        "divida_mudou": divida_antes != divida_depois,
        "liquidada_depois": divida_depois == 0,
        "pago_texto": format_money(pago),
        "pagamentos": cobranca["pagamentos"],
        "fracao_antes_texto": format_money(divida_fracao),
        "fracao_depois_texto": format_money(divida_fracao - divida_antes + divida_depois),
        "mes_antes_texto": format_money(total_mes),
        "mes_depois_texto": format_money(total_mes - cobranca["amount_cents"] + novo_valor),
        "vencimento_antes_texto": cobranca["due_texto"],
        "vencimento_depois_texto": (
            format_date(novo_vencimento) if novo_vencimento else "por confirmar"
        ),
    }


def _ler_correcao_do_form(cobranca: dict, form) -> tuple[dict | None, list[str]]:
    """Ler e validar a correção proposta. Devolve (valores, erros)."""
    erros: list[str] = []

    novo_valor = parse_money(form.get("valor", ""))
    if novo_valor is None or novo_valor <= 0:
        erros.append("Novo valor: usa um valor positivo em euros, por exemplo 47,00.")
    elif novo_valor > VALOR_MAXIMO_EXTRAORDINARIA_CENTIMOS:
        erros.append(
            "Novo valor: acima de 100.000,00 EUR parece um erro de escrita. Nenhuma cobrança foi corrigida."
        )
    elif novo_valor < cobranca["pago_cents"]:
        erros.append(
            f"Já foram pagos e afetados a esta cobrança {format_money(cobranca['pago_cents'])}. "
            f"Corrigi-la para {format_money(novo_valor) if novo_valor > 0 else 'esse valor'} deixá-la-ia "
            "abaixo do que está pago. Ajusta primeiro a distribuição dos pagamentos "
            "(ou anula-os com motivo) e volta a corrigir a cobrança."
        )

    bruto_vencimento = form.get("vencimento", "").strip()
    novo_vencimento: str | None = None
    if bruto_vencimento:
        novo_vencimento = parse_date(bruto_vencimento)
        if novo_vencimento is None:
            erros.append("Vencimento: escreve a data como DD/MM/AAAA, por exemplo 10/01/2027, ou deixa vazio.")
        elif novo_vencimento[:7] != cobranca["period"]:
            erros.append(
                f"Vencimento: uma cobrança de {format_month(cobranca['period'])} vence nesse mês. "
                "Se o vencimento é noutro mês, o que falta é emitir uma cobrança desse mês, não "
                "corrigir esta."
            )

    nova_descricao = form.get("descricao", "").strip()
    if len(nova_descricao) > LIMITE_DESCRICAO:
        erros.append(f"Descrição: no máximo {LIMITE_DESCRICAO} caracteres.")

    motivo = form.get("motivo", "").strip()
    if not motivo:
        erros.append(
            "Motivo da correção: escreve porque esta cobrança está errada. O motivo fica "
            "guardado com os valores anteriores e nenhuma cobrança é corrigida sem ele."
        )
    elif len(motivo) > LIMITE_MOTIVO:
        erros.append(f"Motivo da correção: no máximo {LIMITE_MOTIVO} caracteres.")

    if erros:
        return None, erros

    return (
        {
            "amount_cents": novo_valor,
            "due_date": novo_vencimento,
            "descricao": nova_descricao,
            "motivo": motivo,
        },
        [],
    )


@bp.route("/charges/<int:charge_id>/corrigir", methods=["GET", "POST"])
def correct_charge(charge_id: int):
    """Corrigir o valor, o vencimento ou a descrição de uma cobrança emitida."""
    cobranca = _cobranca_para_correcao(charge_id)
    if cobranca is None:
        abort(404)

    def pagina(valores: dict, efeitos: dict | None, erros: list[str]):
        for mensagem in erros:
            flash(mensagem, "erro")
        return render_template(
            "charge_correct.html",
            cobranca=cobranca,
            valores=valores,
            efeitos=efeitos,
        )

    if request.method == "GET":
        return pagina(
            {
                "valor": format_money_input(cobranca["amount_cents"]),
                "vencimento": format_date(cobranca["due_date"]) if cobranca["due_date"] else "",
                "descricao": cobranca["descricao"],
                "motivo": "",
            },
            None,
            [],
        )

    if _recusar_se_desatualizado("cobrancas"):
        return redirect(url_for("quotas.correct_charge", charge_id=charge_id))

    valores, erros = _ler_correcao_do_form(cobranca, request.form)
    efeitos = (
        _efeitos_da_correcao(cobranca, valores["amount_cents"], valores["due_date"])
        if valores is not None
        else None
    )

    previsualizar = request.form.get("previsualizar") == "on"
    confirmar = request.form.get("confirmar_correcao") == "on"
    if not previsualizar and not confirmar:
        erros.append(
            "Pré-visualiza os efeitos da correção antes de a gravar. Nada foi alterado."
        )
    if confirmar and request.form.get("confirmar_efectos") != "on":
        erros.append(
            "Gravar a correção exige confirmar os efeitos mostrados na pré-visualização. "
            "Nada foi alterado."
        )
    if valores is not None and (
        valores["amount_cents"] == cobranca["amount_cents"]
        and (valores["due_date"] or None) == (cobranca["due_date"] or None)
        and valores["descricao"] == cobranca["descricao"]
    ):
        erros.append("Não há nada para corrigir: o valor, o vencimento e a descrição são os mesmos.")

    if erros:
        return pagina(
            {
                "valor": request.form.get("valor", "").strip(),
                "vencimento": request.form.get("vencimento", "").strip(),
                "descricao": request.form.get("descricao", "").strip(),
                "motivo": request.form.get("motivo", "").strip(),
            },
            efeitos,
            erros,
        )

    if previsualizar:
        return pagina(
            {
                "valor": request.form.get("valor", "").strip(),
                "vencimento": request.form.get("vencimento", "").strip(),
                "descricao": request.form.get("descricao", "").strip(),
                "motivo": valores["motivo"],
            },
            efeitos,
            [],
        )

    if not transacao_de_escrita("cobrancas"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("quotas.correct_charge", charge_id=charge_id))

    connection = get_db()
    try:
        now = utc_now_iso()
        token = _novo_token_de_formulario()
        connection.execute(
            """
            INSERT INTO charge_revisions
                (charge_id, changed_at, motivo, changed_by, edit_token, amount_before,
                 amount_after, due_before, due_after, description_before, description_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                charge_id,
                now,
                valores["motivo"],
                _nome_do_utilizador_para_registo(),
                token,
                cobranca["amount_cents"],
                valores["amount_cents"],
                cobranca["due_date"] or None,
                valores["due_date"],
                cobranca["descricao"],
                valores["descricao"],
            ),
        )
        connection.execute(
            """
            UPDATE charges
            SET amount_cents = ?, due_date = ?, descricao = ?, edit_token = ?
            WHERE id = ?
            """,
            (
                valores["amount_cents"],
                valores["due_date"],
                valores["descricao"],
                token,
                charge_id,
            ),
        )
        connection.commit()
    except sqlite3.Error as erro:
        connection.rollback()
        flash(
            "Não foi possível corrigir a cobrança "
            f"({getattr(erro, 'sqlite_error', erro)}). Nenhuma alteração foi aplicada.",
            "erro",
        )
        return redirect(url_for("quotas.correct_charge", charge_id=charge_id))

    flash(
        f"Cobrança de {cobranca['code']} corrigida: {cobranca['amount_texto']} passou a "
        f"{format_money(valores['amount_cents'])}, com motivo registado. Os "
        f"{len(cobranca['pagamentos'])} pagamento(s) afetado(s) mantêm-se e as ligações "
        "não foram tocadas.",
        "sucesso",
    )
    return redirect(url_for("quotas.correct_charge", charge_id=charge_id))
