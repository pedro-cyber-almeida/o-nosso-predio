"""Configuração do condomínio e saldos iniciais."""

from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .concurrency import MENSAGEM_DESATUALIZADO, desatualizado, transacao_de_escrita
from .db import get_all_settings, get_db, set_settings
from .units import (
    format_date,
    format_money,
    format_money_input,
    parse_date,
    parse_int_id,
    parse_money,
    parse_stored_cents,
)

bp = Blueprint("config", __name__)


@bp.before_request
def _apenas_administrador():
    """Configuração, saldos iniciais e (mais tarde) integrações são de administração."""
    from .auth import recusar_se_nao_for_administrador

    return recusar_se_nao_for_administrador()

TEXT_SETTINGS = (
    ("condominio_nome", "Nome do condomínio", "text"),
    ("condominio_morada", "Morada", "text"),
    ("dividas_abertura_notas", "Notas sobre dívidas de abertura", "textarea"),
)
DATE_SETTINGS = (
    ("gestao_data_inicio", "Data de início da gestão"),
)
MONEY_SETTINGS = (
    ("saldo_inicial_caixa_centimos", "Saldo inicial de caixa"),
    ("saldo_inicial_fundo_centimos", "Saldo inicial do fundo comum de reserva"),
)


@bp.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        # Uma versão desatualizada do formulário não pode apagar valores mais recentes.
        if desatualizado(request.form, "configuracao"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
            return redirect(url_for("config.config"))

        errors: list[str] = []
        updates: dict[str, str] = {}

        for key, label, _kind in TEXT_SETTINGS:
            updates[key] = request.form.get(key, "").strip()

        # O nome pode ficar por definir: guardar saldos ou datas não depende dele.
        # O painel mostra o nome em falta no percurso de configuração.

        for key, label in DATE_SETTINGS:
            raw = request.form.get(key, "").strip()
            if not raw:
                updates[key] = ""
                continue
            iso = parse_date(raw)
            if iso is None:
                errors.append(f"{label}: data inválida. Usa DD/MM/YYYY.")
            else:
                updates[key] = iso

        raw_day = request.form.get("quota_dia_vencimento", "").strip()
        if not raw_day:
            updates["quota_dia_vencimento"] = ""
        else:
            dia = parse_int_id(raw_day)
            if dia is None or not 1 <= dia <= 28:
                errors.append("Dia de vencimento: usa um número entre 1 e 28.")
            else:
                updates["quota_dia_vencimento"] = str(dia)

        for key, label in MONEY_SETTINGS:
            raw = request.form.get(key, "").strip()
            if not raw:
                updates[key] = ""
                continue
            cents = parse_money(raw)
            if cents is None or cents < 0:
                errors.append(f"{label}: indica um valor em euros ou deixa vazio para 'por confirmar'.")
            else:
                updates[key] = str(cents)

        if errors:
            for message in errors:
                flash(message, "erro")
        elif not transacao_de_escrita("configuracao"):
            flash(MENSAGEM_DESATUALIZADO, "erro")
        else:
            try:
                set_settings(updates)
            except sqlite3.IntegrityError:
                get_db().rollback()
                flash(
                    "Não foi possível guardar a configuração. Nenhuma alteração foi aplicada.",
                    "erro",
                )
            else:
                flash("Configuração guardada.", "sucesso")
                return redirect(url_for("config.config"))

    settings = get_all_settings()
    caixa = parse_stored_cents(settings["saldo_inicial_caixa_centimos"])
    fundo = parse_stored_cents(settings["saldo_inicial_fundo_centimos"])
    view = {
        "condominio_nome": settings["condominio_nome"],
        "condominio_morada": settings["condominio_morada"],
        "dividas_abertura_notas": settings["dividas_abertura_notas"],
        "gestao_data_inicio": settings["gestao_data_inicio"],
        "gestao_data_inicio_texto": format_date(settings["gestao_data_inicio"])
        or "por confirmar",
        "quota_dia_vencimento": settings["quota_dia_vencimento"],
        # O campo de edição mostra euros; a representação interna em cêntimos não é editável.
        "saldo_caixa_edicao": format_money_input(caixa),
        "saldo_caixa_texto": format_money(caixa),
        "saldo_fundo_edicao": format_money_input(fundo),
        "saldo_fundo_texto": format_money(fundo),
    }
    return render_template("config.html", view=view)
