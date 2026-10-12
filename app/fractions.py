"""Resumo e gestão das frações: criação, responsáveis, permilagens e proteção de histórico.

Regras aplicadas nesta fase:
- As permilagens não são redistribuídas automaticamente. Qualquer alteração
  exige valores escritos pelo administrador, confirmação explícita e motivo.
  Cada alteração fica registada em `permilagem_history`.
- Uma instalação nova começa sem frações: o administrador cria-as. Durante a
  configuração a soma das permilagens pode ser inferior a 1000,00 — o que
  falta é mostrado e nunca é preenchido automaticamente. A soma nunca pode
  exceder 1000,00.
- Frações com registos associados não podem ser eliminadas nem renomeadas:
  desativar preserva o histórico.
- Formulários de alteração transportam a revisão do estado guardado, que é confirmada
  dentro da transação de escrita. Uma versão desatualizada é recusada e os valores
  atuais são conservados.
- Responsáveis são registos append-only. Só o papel principal (`proprietario`) tem
  de ser único por fração em cada data; coproprietários, administradores,
  representantes e contactos podem coexistir.
- Convenção de datas: `valid_from` é o primeiro dia e `valid_to` é o último dia
  (ambos inclusivos); `valid_to` vazio significa em aberto e uma data de fim não pode
  ser futura.
"""

from __future__ import annotations

import re
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .concurrency import (
    MENSAGEM_DESATUALIZADO,
    desatualizado,
    revisao_configuracao,
    revisao_fracoes,
    revisao_responsaveis,
    transacao_de_escrita,
)
from .db import (
    fraction_references,
    get_db,
    get_setting,
    has_financial_history,
    has_owner_history,
    has_permilagem_history,
    query_db,
    utc_now_iso,
)
from .units import (
    format_date,
    format_money,
    format_permilagem,
    parse_date,
    parse_permilagem,
    parse_stored_cents,
    today_iso,
)
from .payments import resumo_financeiro

bp = Blueprint("fractions", __name__)


@bp.before_request
def _apenas_administrador():
    """Todas as rotas de frações são de administração: a proteção é no servidor."""
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,7}$")
PERMILAGEM_TOTAL = 100000
SEM_FIM = "9999-12-31"

OWNER_ROLES = ("proprietario", "coproprietario", "administrador", "representante", "contacto")
ROLE_LABELS = {
    "proprietario": "Proprietário",
    "coproprietario": "Coproprietário",
    "administrador": "Administrador",
    "representante": "Representante",
    "contacto": "Contacto",
}
# Só estes papéis têm de ser únicos por fração em cada data.
PAPEIS_PRINCIPAIS = ("proprietario",)
LIMITES_RESPONSAVEL = {
    "name": 120,
    "email": 120,
    "phone": 40,
    "address": 200,
    "source": 120,
}
ROTULOS_CAMPO = {
    "name": "Nome",
    "email": "Email",
    "phone": "Telefone",
    "address": "Morada",
    "source": "Fonte",
}


@bp.app_context_processor
def _contexto_comum():
    return {
        "ROLE_LABELS": ROLE_LABELS,
        "OWNER_ROLES": OWNER_ROLES,
        "PAPEIS_PRINCIPAIS": PAPEIS_PRINCIPAIS,
        "today": today_iso(),
        "format_date": format_date,
        "revisao_configuracao": revisao_configuracao,
        "revisao_fracoes": revisao_fracoes,
        "revisao_responsaveis": revisao_responsaveis,
    }


def _fraction_or_404(fraction_id: int):
    row = query_db("SELECT * FROM fractions WHERE id = ?", (fraction_id,), one=True)
    if row is None:
        abort(404)
    return row


def _total_permilagem() -> int:
    row = query_db("SELECT COALESCE(SUM(permilagem), 0) AS total FROM fractions", one=True)
    return int(row["total"])


def _recusar_se_desatualizado(chave: str, fraction_id: int | None = None) -> bool:
    """Devolve True quando o formulário não representa o estado guardado.

    É uma primeira verificação barata, antes de validar e de pedir bloqueio de
    escrita. A verificação decisiva é feita dentro da transação.
    """
    if not desatualizado(request.form, chave, fraction_id):
        return False
    flash(MENSAGEM_DESATUALIZADO, "erro")
    return True


def _ler_permilagens_do_form(rows, form) -> tuple[dict[int, int], list[str]]:
    """Ler e validar as permilagens enviadas para o conjunto de frações indicado."""
    values: dict[int, int] = {}
    errors: list[str] = []
    for row in rows:
        raw = form.get(f"permilagem_{row['id']}", "").strip()
        value = parse_permilagem(raw)
        if value is None or value <= 0:
            errors.append(
                f"Fração {row['code']}: permilagem inválida. Usa um valor positivo, por exemplo 12,50."
            )
        elif value > PERMILAGEM_TOTAL:
            errors.append(f"Fração {row['code']}: a permilagem não pode exceder 1000,00.")
        else:
            values[row["id"]] = value
    return values, errors


def _reparticao_valida(
    values: dict[int, int], total: int, *, exigir_completa: bool = False
) -> list[str]:
    """A soma das permilagens nunca pode exceder 1000,00.

    Durante a configuração do condomínio a soma pode ser inferior a 1000,00:
    o que falta é mostrado na interface e as permilagens nunca são preenchidas
    automaticamente. Quando a soma já estava completa — o caso de eliminar uma
    fração de um condomínio configurado — a redistribuição tem de a devolver
    exatamente a 1000,00.
    """
    if total > PERMILAGEM_TOTAL:
        return [
            f"A soma das permilagens fica em {format_permilagem(total)} e não pode exceder "
            f"{format_permilagem(PERMILAGEM_TOTAL)}. Nenhuma alteração foi guardada."
        ]
    if exigir_completa and total < PERMILAGEM_TOTAL:
        return [
            f"A soma das permilagens fica em {format_permilagem(total)} e tem de ser exatamente "
            f"{format_permilagem(PERMILAGEM_TOTAL)}. Reparte as permilagens pelas frações que ficam. "
            "Nenhuma alteração foi guardada."
        ]
    return []


def _alteracoes_de_permilagem(rows, values: dict[int, int]) -> list[tuple[int, str, int, int]]:
    """Diferenças reais entre o enviado e o guardado: (id, código, anterior, nova)."""
    guardadas = {row["id"]: (row["code"], int(row["permilagem"])) for row in rows}
    alteracoes: list[tuple[int, str, int, int]] = []
    for fraction_id, value in values.items():
        restante = guardadas.get(fraction_id)
        if restante is None:
            continue
        code, anterior = restante
        if anterior != value:
            alteracoes.append((fraction_id, code, anterior, value))
    return alteracoes


def _exigir_confirmacao(alteracoes: list[tuple[int, str, int, int]]) -> list[str]:
    """Redistribuir permilagens exige confirmação explícita e motivo escrito."""
    if not alteracoes:
        return []
    detalhes = "; ".join(
        f"{code}: {format_permilagem(anterior)} passa a {format_permilagem(nova)}"
        for _id, code, anterior, nova in alteracoes
    )
    erros: list[str] = []
    if request.form.get("confirmar_reparticao") != "on":
        erros.append(
            "Redistribuir permilagens exige confirmação explícita. "
            f"Alterações propostas — {detalhes}. "
            "Marca 'Confirmo a redistribuição' ou deixa os valores como estão. "
            "Nenhuma alteração foi guardada."
        )
    if not request.form.get("motivo", "").strip():
        erros.append(
            "Redistribuir permilagens exige um motivo escrito, que fica guardado no "
            "rasto das permilagens. Nenhuma alteração foi guardada."
        )
    return erros


def _vistas_reparticao(rows, form) -> list[dict]:
    """Linhas prontas para um formulário de repartição, com os valores submetidos ou os atuais."""
    vistas: list[dict] = []
    for row in rows:
        if form is None:
            valor = format_permilagem(int(row["permilagem"]))
        else:
            valor = form.get(f"permilagem_{row['id']}", "").strip()
        vistas.append(
            {
                "id": row["id"],
                "code": row["code"],
                "permilagem": valor,
            }
        )
    return vistas


def _gravar_permilagens(
    values: dict[int, int], alteracoes: list[tuple[int, str, int, int]]
) -> None:
    """Aplicar a repartição e o respetivo rasto numa única transação."""
    connection = get_db()
    try:
        now = utc_now_iso()
        motivo = request.form.get("motivo", "").strip()
        for fraction_id, value in values.items():
            connection.execute(
                "UPDATE fractions SET permilagem = ?, updated_at = ? WHERE id = ?",
                (value, now, fraction_id),
            )
        for fraction_id, _code, anterior, nova in alteracoes:
            connection.execute(
                """
                INSERT INTO permilagem_history
                    (fraction_id, permilagem_anterior, permilagem_nova, motivo, alterado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fraction_id, anterior, nova, motivo, now),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _fraction_view(row) -> dict:
    owners = query_db(
        """
        SELECT * FROM owners
        WHERE fraction_id = ?
        ORDER BY valid_from DESC, id DESC
        """,
        (row["id"],),
    )
    hoje = today_iso()
    # "Atual" só para quem já começou: um responsável com data futura não está em funções.
    current = [o for o in owners if o["valid_to"] is None and o["valid_from"] <= hoje]
    referencias = fraction_references(row["id"])
    return {
        "id": row["id"],
        "code": row["code"],
        "permilagem": int(row["permilagem"]),
        "permilagem_texto": format_permilagem(int(row["permilagem"])),
        "active": bool(row["active"]),
        "owners": owners,
        "current_owners": current,
        "created_at": format_date(row["created_at"][:10]),
        "referencias": referencias,
        "pode_eliminar": not referencias,
        "pode_renomear": not referencias,
    }


def resumo_cobrancas() -> dict:
    """Totais das cobranças emitidas, para o resumo.

    São valores emitidos por regra, não dinheiro recebido: o que entrou vê-se no
    cartão seguinte, alimentado por `resumo_financeiro()`. Sem cobranças não se mostra
    nenhum total, para não aparecer um 0,00 EUR como se fosse um saldo.
    """
    linha = query_db(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN rubric = 'quota_ordinaria' THEN amount_cents END), 0) AS ordinario,
               COALESCE(SUM(CASE WHEN rubric = 'reforco_fundo' THEN amount_cents END), 0) AS reforco,
               COALESCE(SUM(CASE WHEN rubric = 'quota_extraordinaria' THEN amount_cents END), 0) AS extraordinario
        FROM charges
        """,
        one=True,
    )
    emitidas = int(linha["n"])
    return {
        "cobrancas_emitidas": emitidas,
        "total_ordinario_texto": format_money(int(linha["ordinario"])),
        "total_reforco_texto": format_money(int(linha["reforco"])),
        "total_extraordinario_texto": format_money(int(linha["extraordinario"])),
        "tem_extraordinario": int(linha["extraordinario"]) > 0,
    }


@bp.route("/fractions")
def fractions_list():
    rows = query_db("SELECT * FROM fractions ORDER BY code")
    views = [_fraction_view(row) for row in rows]
    total = _total_permilagem()
    return render_template(
        "fractions_list.html",
        fractions=views,
        total_permilagem=total,
        total_permilagem_texto=format_permilagem(total),
        total_consistente=total == PERMILAGEM_TOTAL,
        total_em_falta_texto=format_permilagem(PERMILAGEM_TOTAL - total),
    )


@bp.route("/fractions/new", methods=["GET", "POST"])
def new_fraction():
    """Criar uma fração com a repartição escrita pelo administrador, não calculada.

    As permilagens das frações existentes são apresentadas tal como estão guardadas.
    Criar, ajustar e registar o rasto é uma única transação: uma submissão inválida
    ou não confirmada não deixa metade da repartição aplicada.
    """
    existentes = query_db("SELECT * FROM fractions ORDER BY code")

    if request.method == "POST":
        if _recusar_se_desatualizado("fracoes"):
            return redirect(url_for("fractions.new_fraction"))

        code = request.form.get("code", "").strip().upper()
        raw_permilagem = request.form.get("permilagem", "").strip()
        errors: list[str] = []

        if not CODE_RE.match(code):
            errors.append(
                "Identificação da fração: até 8 caracteres, letras e/ou números "
                "(por exemplo, A, B1, 12)."
            )
        elif query_db("SELECT 1 FROM fractions WHERE code = ?", (code,), one=True):
            errors.append(f"A fração {code} já existe.")

        permilagem = parse_permilagem(raw_permilagem)
        if permilagem is None or permilagem <= 0:
            errors.append("Permilagem: usa um valor positivo, por exemplo 12,50.")
        elif permilagem > PERMILAGEM_TOTAL:
            errors.append("Permilagem: não pode exceder 1000,00.")

        valores, erros_reparticao = _ler_permilagens_do_form(existentes, request.form)
        errors.extend(erros_reparticao)
        if not errors:
            errors.extend(_reparticao_valida(valores, sum(valores.values()) + permilagem))
            errors.extend(_exigir_confirmacao(_alteracoes_de_permilagem(existentes, valores)))

        if errors:
            for message in errors:
                flash(message, "erro")
            return redirect(url_for("fractions.new_fraction"))

        alteracoes = _alteracoes_de_permilagem(existentes, valores)
        if not transacao_de_escrita("fracoes"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("fractions.new_fraction"))

        connection = get_db()
        try:
            now = utc_now_iso()
            for fraction_id, value in valores.items():
                connection.execute(
                    "UPDATE fractions SET permilagem = ?, updated_at = ? WHERE id = ?",
                    (value, now, fraction_id),
                )
            connection.execute(
                """
                INSERT INTO fractions (code, permilagem, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (code, permilagem, now, now),
            )
            motivo = request.form.get("motivo", "").strip()
            for fraction_id, _code, anterior, nova in alteracoes:
                connection.execute(
                    """
                    INSERT INTO permilagem_history
                        (fraction_id, permilagem_anterior, permilagem_nova, motivo, alterado_em)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (fraction_id, anterior, nova, motivo, now),
                )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            flash("Não foi possível criar a fração. Nenhuma alteração foi aplicada.", "erro")
        else:
            flash(f"Fração {code} criada com a repartição confirmada pelo administrador.", "sucesso")
            nova_soma = _total_permilagem()
            if nova_soma < PERMILAGEM_TOTAL:
                flash(
                    f"Repartição incompleta: falta {format_permilagem(PERMILAGEM_TOTAL - nova_soma)} "
                    "para chegar a 1000,00. Completa-a em Permilagens quando confirmares os valores; "
                    "nada é preenchido automaticamente.",
                    "aviso",
                )
            return redirect(url_for("fractions.fractions_list"))

    return render_template(
        "fraction_new.html",
        code="",
        permilagem="",
        existentes=_vistas_reparticao(existentes, None),
    )


@bp.route("/fractions/<int:fraction_id>")
def fraction_detail(fraction_id: int):
    row = _fraction_or_404(fraction_id)
    restantes = query_db("SELECT * FROM fractions WHERE id != ? ORDER BY code", (fraction_id,))
    return render_template(
        "fraction_detail.html",
        fraction=_fraction_view(row),
        restantes=_vistas_reparticao(restantes, None),
    )


@bp.route("/fractions/<int:fraction_id>/edit", methods=["POST"])
def edit_fraction(fraction_id: int):
    """Ativar/desativar uma fração. A letra não se muda quando é referência da ata
    ou quando já há registos associados."""
    row = _fraction_or_404(fraction_id)
    if _recusar_se_desatualizado("fracoes"):
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    code = request.form.get("code", "").strip().upper()
    active = request.form.get("active") == "on"

    if not CODE_RE.match(code):
        flash(
            "Identificação da fração: até 8 caracteres, letras e/ou números "
            "(por exemplo, A, B1, 12).",
            "erro",
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    other = query_db(
        "SELECT id FROM fractions WHERE code = ? AND id != ?",
        (code, fraction_id),
        one=True,
    )
    if other is not None:
        flash(f"A fração {code} já existe.", "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    if code != row["code"]:
        if fraction_references(fraction_id):
            flash(
                f"A fração {row['code']} já tem registos associados e a letra não pode ser mudada. "
                "Desativa a fração se for preciso corrigi-la.",
                "erro",
            )
            return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    if not transacao_de_escrita("fracoes"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    connection = get_db()
    try:
        connection.execute(
            "UPDATE fractions SET code = ?, active = ?, updated_at = ? WHERE id = ?",
            (code, 1 if active else 0, utc_now_iso(), fraction_id),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        flash("Não foi possível guardar a fração. Nenhuma alteração foi aplicada.", "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    flash("Fração atualizada.", "sucesso")
    return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))


@bp.route("/fractions/<int:fraction_id>/delete", methods=["POST"])
def delete_fraction(fraction_id: int):
    """Eliminar uma fração, com a permilagem redistribuída pelo administrador.

    Não há redistribuição automática: as novas permilagens das frações que ficam
    têm de ser escritas, somar exatamente 1000,00 quando a soma estava completa,
    ser confirmadas e ter motivo. Eliminar, redistribuir e registar o rasto são
    uma única transação. Durante a configuração inicial (soma ainda incompleta)
    eliminar não exige redistribuição: a soma continua incompleta e visível.
    """
    row = _fraction_or_404(fraction_id)
    if _recusar_se_desatualizado("fracoes"):
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    soma_antes = _total_permilagem()

    if has_financial_history(fraction_id):
        flash(
            f"A fração {row['code']} tem movimentos financeiros registados e não pode ser eliminada. "
            "Desativa-a em vez de eliminar.",
            "erro",
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    if has_owner_history(fraction_id):
        flash(
            f"A fração {row['code']} tem responsáveis registados. "
            "Desativa-a em vez de eliminar.",
            "erro",
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    if has_permilagem_history(fraction_id):
        flash(
            f"A fração {row['code']} tem alterações de permilagem registadas e o histórico não pode "
            "ficar órfão. Desativa-a em vez de eliminar.",
            "erro",
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    associadas = query_db(
        "SELECT COUNT(*) AS n FROM user_fractions WHERE fraction_id = ?",
        (fraction_id,),
        one=True,
    )
    if int(associadas["n"]) > 0:
        flash(
            f"A fração {row['code']} tem {int(associadas['n'])} utilizador(es) associado(s). "
            "Remove primeiro as associações em Utilizadores e desativa a fração em vez de eliminar.",
            "erro",
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    restantes = query_db("SELECT * FROM fractions WHERE id != ? ORDER BY code", (fraction_id,))
    valores, errors = _ler_permilagens_do_form(restantes, request.form)
    if not errors:
        errors.extend(
            _reparticao_valida(
                valores, sum(valores.values()), exigir_completa=(soma_antes == PERMILAGEM_TOTAL)
            )
        )
        errors.extend(_exigir_confirmacao(_alteracoes_de_permilagem(restantes, valores)))

    if errors:
        for message in errors:
            flash(message, "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    alteracoes = _alteracoes_de_permilagem(restantes, valores)
    if not transacao_de_escrita("fracoes"):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    connection = get_db()
    try:
        now = utc_now_iso()
        motivo = request.form.get("motivo", "").strip()
        for id_restante, value in valores.items():
            connection.execute(
                "UPDATE fractions SET permilagem = ?, updated_at = ? WHERE id = ?",
                (value, now, id_restante),
            )
        for id_restante, _code, anterior, nova in alteracoes:
            connection.execute(
                """
                INSERT INTO permilagem_history
                    (fraction_id, permilagem_anterior, permilagem_nova, motivo, alterado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (id_restante, anterior, nova, motivo, now),
            )
        connection.execute("DELETE FROM fractions WHERE id = ?", (fraction_id,))
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        flash(
            f"Não foi possível eliminar a fração {row['code']}. Nenhuma alteração foi aplicada.",
            "erro",
        )
    else:
        flash(
            f"Fração {row['code']} eliminada com a redistribuição confirmada pelo administrador.",
            "sucesso",
        )
        return redirect(url_for("fractions.fractions_list"))

    return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))


@bp.route("/fractions/permilagens", methods=["GET", "POST"])
def permilagens():
    rows = query_db("SELECT * FROM fractions ORDER BY code")

    if request.method == "POST":
        if _recusar_se_desatualizado("fracoes"):
            return redirect(url_for("fractions.permilagens"))

        values, errors = _ler_permilagens_do_form(rows, request.form)
        if not errors:
            errors.extend(
                _reparticao_valida(
                    values,
                    sum(values.values()),
                    exigir_completa=(_total_permilagem() == PERMILAGEM_TOTAL),
                )
            )
            errors.extend(_exigir_confirmacao(_alteracoes_de_permilagem(rows, values)))

        if errors:
            for message in errors:
                flash(message, "erro")
        else:
            if not transacao_de_escrita("fracoes"):
                flash(MENSAGEM_DESATUALIZADO, "erro")
                return redirect(url_for("fractions.permilagens"))
            try:
                _gravar_permilagens(values, _alteracoes_de_permilagem(rows, values))
            except sqlite3.IntegrityError:
                get_db().rollback()
                flash(
                    "Não foi possível guardar as permilagens. Nenhuma alteração foi aplicada.",
                    "erro",
                )
            else:
                flash("Permilagens atualizadas.", "sucesso")
                return redirect(url_for("fractions.permilagens"))

    return render_template(
        "permilagens.html",
        fractions=_vistas_reparticao(rows, None),
        total_permilagem=_total_permilagem(),
        total_permilagem_texto=format_permilagem(_total_permilagem()),
        total_consistente=_total_permilagem() == PERMILAGEM_TOTAL,
        total_em_falta_texto=format_permilagem(PERMILAGEM_TOTAL - _total_permilagem()),
    )


def _sobreposicao_de_papel_principal(
    fraction_id: int, papel: str, from_iso: str, to_iso: str | None, ignorar_id: int | None = None
):
    """Devolve o registo do papel principal que colide com o período proposto, se existir.

    Intervalos inclusivos; `valid_to` vazio é tratado como em aberto.
    """
    if not papel:
        return None
    novo_fim = to_iso or SEM_FIM
    concorrentes = query_db(
        "SELECT * FROM owners WHERE fraction_id = ? AND role = ?",
        (fraction_id, papel),
    )
    for registo in concorrentes:
        if ignorar_id is not None and registo["id"] == ignorar_id:
            continue
        fim_atual = registo["valid_to"] or SEM_FIM
        if from_iso <= fim_atual and registo["valid_from"] <= novo_fim:
            return registo
    return None


@bp.route("/fractions/<int:fraction_id>/owners", methods=["POST"])
def add_owner(fraction_id: int):
    row = _fraction_or_404(fraction_id)
    if _recusar_se_desatualizado("responsaveis", fraction_id):
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "proprietario").strip()
    valid_from = request.form.get("valid_from", "").strip() or today_iso()
    valid_to_raw = request.form.get("valid_to", "").strip()
    errors: list[str] = []

    if not name:
        errors.append("Nome do responsável: obrigatório.")
    if role not in OWNER_ROLES:
        errors.append("Função inválida.")

    contactos = {
        campo: request.form.get(campo, "").strip()
        for campo in ("email", "phone", "address", "source")
    }
    for campo, limite in LIMITES_RESPONSAVEL.items():
        valor = name if campo == "name" else contactos[campo]
        if len(valor) > limite:
            errors.append(f"{ROTULOS_CAMPO[campo]}: no máximo {limite} caracteres.")

    from_iso = parse_date(valid_from)
    if from_iso is None:
        errors.append("Data de início: usa DD/MM/YYYY.")

    to_iso: str | None = None
    if valid_to_raw:
        to_iso = parse_date(valid_to_raw)
        if to_iso is None:
            errors.append("Data de fim: usa DD/MM/YYYY ou deixa vazia.")
        elif from_iso and to_iso < from_iso:
            errors.append("A data de fim não pode ser anterior à data de início.")
        elif to_iso > today_iso():
            errors.append(
                "Data de fim: não pode ser futura. Um período que ainda está em curso fica "
                "sem data de fim e é encerrado na data em que terminar."
            )

    if not errors and role in PAPEIS_PRINCIPAIS:
        colide = _sobreposicao_de_papel_principal(fraction_id, role, from_iso, to_iso)
        if colide is not None:
            fim = format_date(colide["valid_to"]) if colide["valid_to"] else "em aberto"
            errors.append(
                f"Já existe {colide['name']} como {ROLE_LABELS[role]} desta fração entre "
                f"{format_date(colide['valid_from'])} e {fim}. Só pode haver um {ROLE_LABELS[role]} "
                "por fração em cada data: encerra o período anterior ou usa outra função "
                "(coproprietário, administrador, representante ou contacto)."
            )

    if not errors and query_db(
        """
        SELECT 1 FROM owners
        WHERE fraction_id = ? AND name = ?
              AND valid_from = ?
              AND valid_to IS ?
        LIMIT 1
        """,
        (fraction_id, name, from_iso, to_iso),
        one=True,
    ):
        errors.append(f"{name} já está registado nesta fração com exatamente o mesmo período.")
    elif not errors and to_iso is None and query_db(
        "SELECT 1 FROM owners WHERE fraction_id = ? AND name = ? AND valid_to IS NULL",
        (fraction_id, name),
        one=True,
    ):
        errors.append(f"{name} já está registado como responsável atual desta fração.")

    if errors:
        for message in errors:
            flash(message, "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    if not transacao_de_escrita("responsaveis", fraction_id):
        flash(MENSAGEM_DESATUALIZADO, "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    connection = get_db()
    try:
        connection.execute(
            """
            INSERT INTO owners (fraction_id, name, role, email, phone, address,
                                valid_from, valid_to, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fraction_id,
                name,
                role,
                contactos["email"],
                contactos["phone"],
                contactos["address"],
                from_iso,
                to_iso,
                contactos["source"],
                utc_now_iso(),
            ),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        flash(
            "Não foi possível registar o responsável. Nenhuma alteração foi aplicada.", "erro"
        )
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    flash(f"Responsável registado na fração {row['code']}.", "sucesso")
    return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))


@bp.route("/fractions/<int:fraction_id>/owners/<int:owner_id>/close", methods=["POST"])
def close_owner(fraction_id: int, owner_id: int):
    row = _fraction_or_404(fraction_id)
    if _recusar_se_desatualizado("responsaveis", fraction_id):
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    owner = query_db(
        "SELECT * FROM owners WHERE id = ? AND fraction_id = ?",
        (owner_id, fraction_id),
        one=True,
    )
    if owner is None:
        abort(404)

    if owner["valid_to"] is not None:
        flash("Este registo de responsável já está encerrado.", "erro")
        return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

    valid_to = parse_date(request.form.get("valid_to", "").strip() or today_iso())
    if valid_to is None:
        flash("Data de fim: usa DD/MM/YYYY.", "erro")
    elif valid_to < owner["valid_from"]:
        flash("A data de fim não pode ser anterior ao início do responsável.", "erro")
    elif valid_to > today_iso():
        flash(
            "Data de fim: não pode ser futura. Encerra na data em que o papel terminou; "
            "um período em curso fica sem data de fim.",
            "erro",
        )
    else:
        if not transacao_de_escrita("responsaveis", fraction_id):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

        connection = get_db()
        try:
            connection.execute("UPDATE owners SET valid_to = ? WHERE id = ?", (valid_to, owner_id))
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            flash(
                "Não foi possível encerrar o responsável. Nenhuma alteração foi aplicada.", "erro"
            )
            return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))

        flash("Responsável encerrado. O histórico foi conservado.", "sucesso")

    return redirect(url_for("fractions.fraction_detail", fraction_id=fraction_id))
