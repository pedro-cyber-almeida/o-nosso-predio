"""Painel inicial de cada perfil (entregas 1 a 3).

- `/` mostra o painel correspondente ao perfil: administrador ou condómino.
- Só há números reais: sem movimentos registados não aparece nenhum valor,
  aparece o estado vazio com o próximo passo.
- Cobranças (valores emitidos) e recebimentos (dinheiro que entrou) são
  cartões separados; o fundo de reserva é uma afetação e não é somado outra
  vez à liquidez.
- Sem saldo inicial confirmado, a liquidez aparece como "por confirmar" com a
  variação dos movimentos conhecidos — nunca como saldo bancário reconciliado.
- O período do painel do administrador é um ano. Os fluxos (recebimentos e
  pagamentos de despesas) são os movimentos com data no ano escolhido. Os
  saldos (dívida de quotas, despesas por pagar, crédito, liquidez, fundo) são
  acumulados até à data limite: o fim do ano ou hoje, o que acontecer primeiro.
  Um ano passado mostra o estado que o prédio tinha nessa data, não o de hoje.
- Cobranças emitidas não são recebimentos; despesas registadas não são saídas.
- Os gráficos são SVG locais a partir dos dados guardados: recebimentos e
  pagamentos de despesas por mês, despesas pagas por categoria e evolução da
  liquidez — esta só quando o saldo inicial e a data de início da gestão estão
  confirmados.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from .auth import current_user, eh_administrador, fracoes_do_utilizador
from .db import get_all_settings, query_db
from .fractions import PERMILAGEM_TOTAL, _total_permilagem
from .payments import ROTULOS_MEIO, _estado_das_cobrancas, _totais, _totais_texto
from .units import (
    format_date,
    format_money,
    format_month,
    format_permilagem,
    parse_stored_cents,
    today_iso,
)

bp = Blueprint("dashboard", __name__)

NOME_MESES = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


@bp.route("/")
def painel():
    utilizador = current_user()
    if utilizador is None:
        return redirect(url_for("auth.login"))
    if utilizador["role"] == "administrador":
        return _painel_administrador()
    return _painel_condomino(utilizador)


# ---------------------------------------------------------------------------
# Administrador
# ---------------------------------------------------------------------------


def _anos_com_movimentos() -> list[str]:
    """Anos com recebimentos válidos ou pagamentos de despesa válidos."""
    linhas = query_db(
        """
        SELECT DISTINCT substr(paid_on, 1, 4) AS ano FROM payments
        WHERE cancelled_at IS NULL
        UNION
        SELECT DISTINCT substr(paid_on, 1, 4) AS ano FROM expense_payments
        WHERE cancelled_at IS NULL
        ORDER BY ano DESC
        """
    )
    return [linha["ano"] for linha in linhas]


def _soma(sql: str, params: tuple = ()) -> int:
    """Um único total de uma consulta que devolve a coluna `v`."""
    return int(query_db(sql, params, one=True)["v"])


def _movimentos_por_mes(ano: str) -> list[dict]:
    """Recebimentos e pagamentos de despesas em cada mês do ano, válidos só."""
    recebimentos = {
        linha["mes"]: int(linha["total"])
        for linha in query_db(
            "SELECT substr(paid_on, 1, 7) AS mes, SUM(amount_cents) AS total "
            "FROM payments WHERE cancelled_at IS NULL AND substr(paid_on, 1, 4) = ? "
            "GROUP BY mes",
            (ano,),
        )
    }
    pagamentos = {
        linha["mes"]: int(linha["total"])
        for linha in query_db(
            """
            SELECT substr(p.paid_on, 1, 7) AS mes, SUM(p.amount_cents) AS total
            FROM expense_payments AS p
            JOIN expenses AS e ON e.id = p.expense_id
            WHERE p.cancelled_at IS NULL AND e.cancelled_at IS NULL
              AND substr(p.paid_on, 1, 4) = ?
            GROUP BY mes
            """,
            (ano,),
        )
    }
    maximo = max(list(recebimentos.values()) + list(pagamentos.values()), default=0)
    meses: list[dict] = []
    for indice in range(1, 13):
        chave = f"{ano}-{indice:02d}"
        rec = recebimentos.get(chave, 0)
        pag = pagamentos.get(chave, 0)
        meses.append(
            {
                "mes": chave,
                "nome": NOME_MESES[indice - 1],
                "rec_cents": rec,
                "rec_texto": format_money(rec),
                "pag_cents": pag,
                "pag_texto": format_money(pag),
                # Altura percentual dentro do gráfico; a escala é comum às duas séries.
                "rec_altura": round(rec * 100 / maximo) if maximo > 0 else 0,
                "pag_altura": round(pag * 100 / maximo) if maximo > 0 else 0,
            }
        )
    return meses


def _cobrancas_ate(data_limite: str) -> dict:
    """Cobranças emitidas até à data limite, por rubrica. Emitido não é recebido."""
    linha = query_db(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(amount_cents), 0) AS total,
               COALESCE(SUM(CASE WHEN rubric = 'quota_ordinaria' THEN amount_cents END), 0) AS ordinario,
               COALESCE(SUM(CASE WHEN rubric = 'reforco_fundo' THEN amount_cents END), 0) AS reforco,
               COALESCE(SUM(CASE WHEN rubric = 'quota_extraordinaria' THEN amount_cents END), 0) AS extraordinario
        FROM charges
        WHERE period <= substr(?, 1, 7)
        """,
        (data_limite,),
        one=True,
    )
    return {
        "n": int(linha["n"]),
        "total": int(linha["total"]),
        "ordinario": int(linha["ordinario"]),
        "reforco": int(linha["reforco"]),
        "extraordinario": int(linha["extraordinario"]),
    }


def _divida_vencida_ate(data_limite: str) -> int:
    """Quotas com vencimento já passado à data limite, menos o liquidado até lá."""
    return _soma(
        """
        SELECT COALESCE(SUM(c.amount_cents - COALESCE(a.ate, 0)), 0) AS v
        FROM charges AS c
        LEFT JOIN (
            SELECT a.charge_id AS charge_id, SUM(a.amount_cents) AS ate
            FROM payment_allocations AS a
            JOIN payments AS p ON p.id = a.payment_id
            WHERE p.cancelled_at IS NULL AND p.paid_on <= ?
            GROUP BY a.charge_id
        ) AS a ON a.charge_id = c.id
        WHERE c.due_date IS NOT NULL AND c.due_date < ?
        """,
        (data_limite, data_limite),
    )


def _despesas_em_falta_ate(data_limite: str) -> int:
    """Despesas registadas até à data limite menos os pagamentos válidos até lá."""
    return _soma(
        """
        SELECT COALESCE(SUM(e.amount_cents - COALESCE(p.ate, 0)), 0) AS v
        FROM expenses AS e
        LEFT JOIN (
            SELECT ep.expense_id AS expense_id, SUM(ep.amount_cents) AS ate
            FROM expense_payments AS ep
            WHERE ep.cancelled_at IS NULL AND ep.paid_on <= ?
            GROUP BY ep.expense_id
        ) AS p ON p.expense_id = e.id
        WHERE e.cancelled_at IS NULL AND e.data <= ?
        """,
        (data_limite, data_limite),
    )


def _despesas_pagas_por_categoria(ano: str) -> list[dict]:
    """Despesas pagas no período, agrupadas pela categoria definida pelo administrador."""
    linhas = query_db(
        """
        SELECT c.nome AS nome, SUM(p.amount_cents) AS total
        FROM expense_payments AS p
        JOIN expenses AS e ON e.id = p.expense_id
        JOIN expense_categories AS c ON c.id = e.category_id
        WHERE p.cancelled_at IS NULL AND e.cancelled_at IS NULL
          AND substr(p.paid_on, 1, 4) = ?
        GROUP BY c.nome
        ORDER BY total DESC, c.nome
        """,
        (ano,),
    )
    maximo = max((int(linha["total"]) for linha in linhas), default=0)
    return [
        {
            "nome": linha["nome"],
            "texto": format_money(int(linha["total"])),
            "largura_pct": round(int(linha["total"]) * 100 / maximo) if maximo > 0 else 0,
        }
        for linha in linhas
    ]


def _liquidez_por_mes(ano: str, data_limite: str, saldo_caixa: int, gestao_inicio: str) -> list[dict]:
    """Liquidez acumulada no fim de cada mês do ano. Só com saldo e gestão confirmados.

    É a variação registada a partir do saldo inicial confirmado — não é saldo
    bancário reconciliado. Meses anteriores ao início da gestão não têm ponto.
    """
    recebimentos = {
        linha["mes"]: int(linha["total"])
        for linha in query_db(
            "SELECT substr(paid_on, 1, 7) AS mes, SUM(amount_cents) AS total "
            "FROM payments WHERE cancelled_at IS NULL AND paid_on <= ? GROUP BY mes",
            (data_limite,),
        )
    }
    pagamentos = {
        linha["mes"]: int(linha["total"])
        for linha in query_db(
            "SELECT substr(paid_on, 1, 7) AS mes, SUM(amount_cents) AS total "
            "FROM expense_payments WHERE cancelled_at IS NULL AND paid_on <= ? GROUP BY mes",
            (data_limite,),
        )
    }
    todas = sorted(set(recebimentos) | set(pagamentos))
    pontos: list[dict] = []
    for indice in range(1, 13):
        chave = f"{ano}-{indice:02d}"
        if chave < gestao_inicio[:7]:
            continue  # antes do início da gestão não há estado para mostrar.
        valor = saldo_caixa
        for mes in todas:
            # Só contam os meses fechados: até ao fim do mês e até à data limite.
            if mes > chave or mes > data_limite[:7]:
                break
            valor += recebimentos.get(mes, 0) - pagamentos.get(mes, 0)
        pontos.append(
            {"mes": chave, "nome": NOME_MESES[indice - 1], "cents": valor, "texto": format_money(valor)}
        )
    if len(pontos) >= 2:
        minimo = min(p["cents"] for p in pontos)
        maximo = max(p["cents"] for p in pontos)
        amplitude = maximo - minimo or 1
        for ponto in pontos:
            ponto["altura_pct"] = round((ponto["cents"] - minimo) * 100 / amplitude)
    else:
        for ponto in pontos:
            ponto["altura_pct"] = 50
    return pontos


def _pagamentos_recentes(fraction_ids: list[int] | None, limite: int = 6) -> list[dict]:
    if fraction_ids is not None and not fraction_ids:
        return []
    filtro = ""
    params: list = []
    if fraction_ids is not None:
        marcadores = ", ".join("?" for _ in fraction_ids)
        filtro = f"AND p.fraction_id IN ({marcadores})"
        params = list(fraction_ids)
    linhas = query_db(
        f"""
        SELECT p.id, p.amount_cents, p.paid_on, p.method, p.reference,
               p.cancelled_at, f.code
        FROM payments AS p
        JOIN fractions AS f ON f.id = p.fraction_id
        WHERE 1 = 1 {filtro}
        ORDER BY p.paid_on DESC, p.id DESC
        LIMIT ?
        """,
        (*params, limite),
    )
    return [
        {
            "id": linha["id"],
            "code": linha["code"],
            "valor_texto": format_money(int(linha["amount_cents"])),
            "data_texto": format_date(linha["paid_on"]),
            "meio_texto": ROTULOS_MEIO[linha["method"]],
            "referencia": linha["reference"] or "",
            "anulado": linha["cancelled_at"] is not None,
        }
        for linha in linhas
    ]


def _painel_administrador():
    fractions = query_db("SELECT id, code, permilagem, active FROM fractions ORDER BY code")
    total = _total_permilagem()
    settings = get_all_settings()

    saldo_caixa = parse_stored_cents(settings.get("saldo_inicial_caixa_centimos", ""))
    saldo_fundo = parse_stored_cents(settings.get("saldo_inicial_fundo_centimos", ""))
    gestao_inicio = (settings.get("gestao_data_inicio") or "").strip()

    hoje = today_iso()
    anos = _anos_com_movimentos()
    ano_pedido = (request.args.get("ano") or "").strip()
    ano = ano_pedido if ano_pedido in anos else (anos[0] if anos else hoje[:4])
    # Data limite do período: o fim do ano escolhido, ou hoje se o ano ainda não terminou.
    data_limite = min(f"{ano}-12-31", hoje)

    # --- Fluxos do período (movimentos com data no ano escolhido). ---
    meses = _movimentos_por_mes(ano)
    recebido_ano = sum(mes["rec_cents"] for mes in meses)
    pagamentos_despesa_ano = sum(mes["pag_cents"] for mes in meses)
    tem_pagamentos_despesa_ano = pagamentos_despesa_ano > 0

    # --- Saldos acumulados até à data limite. ---
    recebido_ate = _soma(
        "SELECT COALESCE(SUM(amount_cents), 0) AS v FROM payments "
        "WHERE cancelled_at IS NULL AND paid_on <= ?",
        (data_limite,),
    )
    afetado_ate = _soma(
        "SELECT COALESCE(SUM(a.amount_cents), 0) AS v FROM payment_allocations AS a "
        "JOIN payments AS p ON p.id = a.payment_id "
        "WHERE p.cancelled_at IS NULL AND p.paid_on <= ?",
        (data_limite,),
    )
    ao_fundo_ate = _soma(
        "SELECT COALESCE(SUM(a.amount_cents), 0) AS v FROM payment_allocations AS a "
        "JOIN payments AS p ON p.id = a.payment_id "
        "JOIN charges AS c ON c.id = a.charge_id "
        "WHERE p.cancelled_at IS NULL AND c.rubric = 'reforco_fundo' AND p.paid_on <= ?",
        (data_limite,),
    )
    cobrancas = _cobrancas_ate(data_limite)
    cobrancas_globais = _cobrancas_ate("9999-12-31")
    vencida_ate = _divida_vencida_ate(data_limite)

    pagamentos_despesa_ate = _soma(
        "SELECT COALESCE(SUM(p.amount_cents), 0) AS v FROM expense_payments AS p "
        "JOIN expenses AS e ON e.id = p.expense_id "
        "WHERE p.cancelled_at IS NULL AND e.cancelled_at IS NULL AND p.paid_on <= ?",
        (data_limite,),
    )
    pagamentos_fundo_ate = _soma(
        "SELECT COALESCE(SUM(p.amount_cents), 0) AS v FROM expense_payments AS p "
        "JOIN expenses AS e ON e.id = p.expense_id "
        "WHERE p.cancelled_at IS NULL AND e.cancelled_at IS NULL "
        "AND e.classificacao = 'fundo_reserva' AND p.paid_on <= ?",
        (data_limite,),
    )
    despesas_em_falta_ate = _despesas_em_falta_ate(data_limite)
    despesas_ate = _soma(
        "SELECT COUNT(*) AS v FROM expenses WHERE cancelled_at IS NULL AND data <= ?",
        (data_limite,),
    )

    # Contagens globais: um registo anulado continua a ser um registo, mostra-se.
    pagamentos_validos = _soma(
        "SELECT COUNT(*) AS v FROM payments WHERE cancelled_at IS NULL"
    )
    pagamentos_anulados = _soma(
        "SELECT COUNT(*) AS v FROM payments WHERE cancelled_at IS NOT NULL"
    )
    pagamentos_despesa_validos = _soma(
        "SELECT COUNT(*) AS v FROM expense_payments WHERE cancelled_at IS NULL"
    )
    pagamentos_despesa_anulados = _soma(
        "SELECT COUNT(*) AS v FROM expense_payments WHERE cancelled_at IS NOT NULL"
    )
    tem_pagamentos = pagamentos_validos + pagamentos_anulados > 0
    tem_pagamentos_despesa = pagamentos_despesa_validos + pagamentos_despesa_anulados > 0
    tem_movimentos = (
        recebido_ate > 0 or cobrancas["total"] > 0 or pagamentos_anulados > 0
    )

    # Sem despesas registadas não há saídas: a variação conhecida é o que entrou
    # menos o que saiu. Confirmado o saldo inicial, a liquidez é ele mais essa variação.
    variacao_conhecida = recebido_ate - pagamentos_despesa_ate
    liquidez_conhecida = (saldo_caixa or 0) + variacao_conhecida
    # O fundo é uma afetação dentro da caixa: reforços menos despesas pagas pelo fundo.
    fundo_conhecido = (saldo_fundo or 0) + ao_fundo_ate - pagamentos_fundo_ate

    categorias_pagas = _despesas_pagas_por_categoria(ano)
    liquidez_evolucao = (
        _liquidez_por_mes(ano, data_limite, saldo_caixa, gestao_inicio)
        if saldo_caixa is not None and gestao_inicio
        else []
    )

    # Percurso de configuração: o que ainda falta, na ordem em que faz sentido.
    # A aplicação nunca preenche nada automaticamente; mostra o que falta.
    regras_registadas = int(
        query_db("SELECT COUNT(*) AS n FROM charge_rules", (), one=True)["n"]
    )
    passos: list[tuple[str, str, str]] = []
    if not fractions:
        passos.append((
            "Criar frações",
            "Cria a primeira fração e escreve a sua permilagem; podes acrescentar "
            "as outras progressivamente.",
            url_for("fractions.new_fraction"),
        ))
    elif total < PERMILAGEM_TOTAL:
        passos.append((
            "Completar permilagens",
            f"Falta {format_permilagem(PERMILAGEM_TOTAL - total)} para chegar a "
            "1000,00; nada é preenchido automaticamente.",
            url_for("fractions.permilagens"),
        ))
    if regras_registadas == 0:
        passos.append((
            "Registar regras de quota",
            "Define as regras de quota ordinária (e reforço do fundo, se houver) "
            "com a vigência confirmada.",
            url_for("quotas.quotas"),
        ))
    if not gestao_inicio or not (settings.get("condominio_nome") or "").strip():
        passos.append((
            "Configurar o condomínio",
            "Nome, morada, data de início da gestão e dia de vencimento das quotas.",
            url_for("config.config"),
        ))

    return render_template(
        "resumo.html",
        fractions=fractions,
        total_permilagem=total,
        total_permilagem_texto=format_permilagem(total),
        total_consistente=total == PERMILAGEM_TOTAL,
        total_em_falta_texto=format_permilagem(PERMILAGEM_TOTAL - total),
        passos_configuracao=passos,
        settings=settings,
        quota_dia_vencimento=settings.get("quota_dia_vencimento") or "por confirmar",
        gestao_data_inicio=format_date(gestao_inicio) or "por confirmar",
        # Período escolhido.
        periodo_ano=ano,
        periodo_anos=anos,
        data_limite_texto=format_date(data_limite),
        # Recebimentos do período.
        tem_pagamentos=tem_pagamentos,
        recebido_ano_texto=format_money(recebido_ano),
        recebido_ate_texto=format_money(recebido_ate),
        pagamentos_registados=pagamentos_validos,
        pagamentos_anulados=pagamentos_anulados,
        afetado_ate_texto=format_money(afetado_ate),
        # Despesas do período e por pagar à data limite.
        tem_pagamentos_despesa=tem_pagamentos_despesa,
        pagamentos_despesa_ano_texto=format_money(pagamentos_despesa_ano),
        pagamentos_despesa_ate_texto=format_money(pagamentos_despesa_ate),
        pagamentos_despesa_n=pagamentos_despesa_validos,
        pagamentos_despesa_anulados=pagamentos_despesa_anulados,
        tem_despesas=despesas_ate > 0 or tem_pagamentos_despesa,
        despesas_em_falta_texto=format_money(despesas_em_falta_ate),
        # Quotas emitidas e por receber à data limite.
        cobrancas_emitidas=cobrancas["n"],
        total_ordinario_texto=format_money(cobrancas["ordinario"]),
        total_reforco_texto=format_money(cobrancas["reforco"]),
        total_extraordinario_texto=format_money(cobrancas["extraordinario"]),
        tem_extraordinario=cobrancas["extraordinario"] > 0,
        # Cobranças já emitidas mas com mês posterior à data limite: não são
        # recebíveis à data, e diz-se isso em vez de esconder que existem.
        cobrancas_futuras_n=cobrancas_globais["n"] - cobrancas["n"],
        total_ordinario_futuro_texto=format_money(
            cobrancas_globais["ordinario"] - cobrancas["ordinario"]
        ),
        total_reforco_futuro_texto=format_money(
            cobrancas_globais["reforco"] - cobrancas["reforco"]
        ),
        total_extraordinario_futuro_texto=format_money(
            cobrancas_globais["extraordinario"] - cobrancas["extraordinario"]
        ),
        tem_extraordinario_futuro=(
            cobrancas_globais["extraordinario"] - cobrancas["extraordinario"] > 0
        ),
        divida_texto=format_money(cobrancas["total"] - afetado_ate),
        vencida_texto=format_money(vencida_ate),
        tem_divida_vencida=vencida_ate > 0,
        # Crédito e liquidez à data limite.
        credito_texto=format_money(recebido_ate - afetado_ate),
        tem_movimentos=tem_movimentos,
        liquidez_por_confirmar=saldo_caixa is None,
        liquidez_texto=format_money(liquidez_conhecida),
        variacao_conhecida_texto=format_money(variacao_conhecida),
        tem_movimentos_conhecidos=variacao_conhecida != 0,
        # Fundo de reserva à data limite.
        tem_fundo_dados=(
            ao_fundo_ate > 0 or pagamentos_fundo_ate > 0 or saldo_fundo is not None
        ),
        tem_fundo_inicial=saldo_fundo is not None,
        fundo_texto=format_money(fundo_conhecido),
        fundo_reforcos_texto=format_money(ao_fundo_ate),
        fundo_despesas_texto=format_money(pagamentos_fundo_ate),
        # Gráficos.
        tem_recebimentos=bool(anos),
        meses_movimentos=meses,
        tem_pagamentos_despesa_ano=tem_pagamentos_despesa_ano,
        total_ano_texto=format_money(recebido_ano),
        total_ano_pagamentos_texto=format_money(pagamentos_despesa_ano),
        categorias_pagas=categorias_pagas,
        liquidez_evolucao=liquidez_evolucao,
        pagamentos_recentes=_pagamentos_recentes(None),
    )


# ---------------------------------------------------------------------------
# Condómino
# ---------------------------------------------------------------------------


def _painel_condomino(utilizador):
    fracoes = fracoes_do_utilizador(int(utilizador["id"]))
    por_fracao = [
        {"code": fracao["code"], "totais": _totais_texto(_totais(int(fracao["id"])))}
        for fracao in fracoes
    ]

    agregado = {"divida_cents": 0, "vencida_cents": 0, "credito_cents": 0}
    proximas: list[dict] = []
    hoje = today_iso()
    for fracao in fracoes:
        totais = _totais(int(fracao["id"]))
        agregado["divida_cents"] += totais["divida_cents"]
        agregado["vencida_cents"] += totais["vencida_cents"]
        agregado["credito_cents"] += totais["credito_cents"]
        for cobranca in _estado_das_cobrancas(int(fracao["id"])):
            if cobranca["em_divida_cents"] > 0:
                proximas.append(
                    {
                        **cobranca,
                        "code": fracao["code"],
                        # Sem vencimento definido não há classificação de vencida;
                        # no painel vai para o fim da lista, marcada como por confirmar.
                        "ordenar_por": cobranca["due_date"] or "9999-12-31",
                    }
                )
    proximas.sort(key=lambda linha: (linha["ordenar_por"], linha["period"], linha["rubric"]))
    proximas = proximas[:8]

    # Comprovativos pendentes e documentos partilhados (entregas 4/5): só aparecem
    # quando existem; nenhum altera saldos.
    comprovativos_pendentes = int(
        query_db(
            "SELECT COUNT(*) AS n FROM documentos "
            "WHERE origem = 'condomino' AND criado_por = ? AND decisao = 'pendente'",
            (str(utilizador["username"]),),
            one=True,
        )["n"]
    )
    fracao_ids = tuple(int(f["id"]) for f in fracoes)
    documentos_partilhados = int(
        query_db(
            "SELECT COUNT(DISTINCT d.id) AS n FROM documentos d "
            "LEFT JOIN documento_fracoes df ON df.documento_id = d.id "
            "WHERE d.partilha_todos = 1"
            + (" OR df.fraction_id IN (%s)" % ", ".join("?" for _ in fracao_ids) if fracao_ids else ""),
            fracao_ids,
            one=True,
        )["n"]
    )

    return render_template(
        "dashboard_condomino.html",
        por_fracao=por_fracao,
        divida_texto=format_money(agregado["divida_cents"]),
        vencida_texto=format_money(agregado["vencida_cents"]),
        credito_texto=format_money(agregado["credito_cents"]),
        tem_divida=agregado["divida_cents"] > 0,
        tem_divida_vencida=agregado["vencida_cents"] > 0,
        tem_credito=agregado["credito_cents"] > 0,
        proximas_cobrancas=proximas,
        tem_cobrancas=bool(proximas),
        pagamentos_recentes=_pagamentos_recentes([int(f["id"]) for f in fracoes]),
        tem_fracoes=bool(fracoes),
        comprovativos_pendentes=comprovativos_pendentes,
        documentos_partilhados=documentos_partilhados,
        hoje_texto=format_date(hoje),
    )
