"""Dados de demonstração: inteiramente fictícios, sempre numa base separada.

A demonstração é acionada de forma explícita (`python run.py demo`) e nunca é
executada pelo arranque normal. Cria um condomínio de exemplo completo para a
aplicação poder ser explorada: frações com permilagem, responsáveis fictícios,
regras de quota, cobranças emitidas, pagamentos com afetações, categorias e
despesas pagas e por pagar, e documentos PDF de exemplo.

Nada aqui pertence a um condomínio real: nomes, moradas, valores e documentos
foram inventados para este ficheiro.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import create_app
from .db import init_db, utc_now_iso
from .units import today_iso

# Nomes claramente fictícios: nunca correspondem a condóminos reais.
DEMO_OWNERS: dict[str, str] = {
    "A": "Responsável Fictício A",
    "B": "Responsável Fictício B",
    "C": "Responsável Fictício C",
    "D": "Responsável Fictício D",
    "E": "Responsável Fictício E",
    "F": "Responsável Fictício F",
    "G": "Responsável Fictício G",
    "H": "Responsável Fictício H",
}

# Frações e permilagens inventadas para a demonstração (soma 1000,00).
DEMO_FRACTIONS: tuple[tuple[str, int], ...] = (
    ("A", 12000),
    ("B", 11000),
    ("C", 13000),
    ("D", 10000),
    ("E", 12500),
    ("F", 12500),
    ("G", 10000),
    ("H", 9000),
)

# Esquema financeiro inventado: quotas 40/45 EUR e reforço de 3 EUR.
DEMO_QUOTA_GRUPO_1 = 4000  # A, C, E, G
DEMO_QUOTA_GRUPO_2 = 4500  # B, D, F, H
DEMO_REFORCO = 300
DEMO_GRUPO_1 = ("A", "C", "E", "G")
DEMO_GRUPO_2 = ("B", "D", "F", "H")

# Contas de acesso da demonstração, documentadas no README. São só da demonstração.
DEMO_ADMIN = ("demo.admin", "palavra-de-demonstracao-2026")
DEMO_CONDOMINO = ("demo.condomino", "palavra-de-demonstracao-2026")

DEMO_SETTINGS: tuple[tuple[str, str], ...] = (
    ("condominio_nome", "Condomínio das Acácias (demonstração fictícia)"),
    ("condominio_morada", "Rua das Acácias 12, 1000-000 Lisboa (fictícia)"),
    ("quota_dia_vencimento", "10"),
    ("dividas_abertura_notas", ""),
)

FONTE_DEMO = "demonstração fictícia"


def _has_data(path: Path) -> bool:
    if not path.exists():
        return False
    connection = sqlite3.connect(path)
    try:
        for table in ("fractions", "owners"):
            try:
                count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if count:
                return True
        return False
    finally:
        connection.close()


def _pdf_ficticio(titulo: str, linhas: list[str]) -> bytes:
    """PDF simples gerado localmente para os documentos de exemplo."""
    import pymupdf  # PyMuPDF: dependência declarada em requirements.txt

    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_text((56, 72), titulo, fontsize=16)
    y = 100
    for linha in linhas:
        pagina.insert_text((56, y), linha, fontsize=11)
        y += 18
    dados = documento.tobytes()
    documento.close()
    return dados


def create_demo_database(path: Path) -> tuple[bool, str]:
    """Criar a base de demonstração. Recusa sobrescrever uma base com dados.

    Devolve (sucesso, mensagem): o código de saída não depende da redação do texto.
    Os documentos de exemplo ficam numa pasta ao lado da base de demonstração,
    nunca na pasta `instance/` de uma instalação em uso.
    """
    if _has_data(path):
        return False, f"recusado: {path} já contém dados; a demonstração não sobrescreve bases com dados."

    app = create_app(
        instance_path=path.parent / f"{path.stem}_instance",
        database=path,
    )
    with app.app_context():
        init_db()
        from .auth import PAPEL_ADMIN, PAPEL_CONDOMINO, criar_utilizador
        from .db import get_db
        from .documentos import guardar_documento

        connection = get_db()
        now = utc_now_iso()
        today = today_iso()
        ano = today[:4]
        dia_vencimento = 10

        # 1) Configuração do condomínio fictício.
        valores = dict(DEMO_SETTINGS)
        valores["gestao_data_inicio"] = f"{ano}-01-01"
        valores["saldo_inicial_caixa_centimos"] = "250000"
        valores["saldo_inicial_fundo_centimos"] = "500000"
        for chave, valor in valores.items():
            connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (chave, valor, now),
            )

        # 2) Frações e permilagens inventadas.
        for code, permilagem in DEMO_FRACTIONS:
            connection.execute(
                "INSERT INTO fractions (code, permilagem, active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (code, permilagem, now, now),
            )
        fracoes = {
            row["code"]: row["id"]
            for row in connection.execute("SELECT id, code FROM fractions").fetchall()
        }

        # 3) Responsáveis fictícios.
        for code, nome in DEMO_OWNERS.items():
            connection.execute(
                "INSERT INTO owners (fraction_id, name, role, valid_from, source, created_at) "
                "VALUES (?, ?, 'proprietario', ?, ?, ?)",
                (fracoes[code], nome, f"{ano}-01-01", FONTE_DEMO, now),
            )

        # 4) Regras de quota inventadas.
        def _regra(codigos: tuple[str, ...], rubrica: str, valor: int, fim: str | None) -> None:
            for code in codigos:
                connection.execute(
                    "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, "
                    "valid_to, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fracoes[code], rubrica, valor, f"{ano}-01-01", fim, FONTE_DEMO, now, now),
                )

        _regra(DEMO_GRUPO_1, "quota_ordinaria", DEMO_QUOTA_GRUPO_1, None)
        _regra(DEMO_GRUPO_2, "quota_ordinaria", DEMO_QUOTA_GRUPO_2, None)
        _regra(
            tuple(code for code, _ in DEMO_FRACTIONS),
            "reforco_fundo",
            DEMO_REFORCO,
            f"{ano}-12-31",
        )
        regras = {
            (row["fraction_id"], row["rubric"]): row["id"]
            for row in connection.execute(
                "SELECT id, fraction_id, rubric FROM charge_rules"
            ).fetchall()
        }

        # 5) Cobranças emitidas mês a mês desde janeiro do ano corrente.
        meses = [f"{ano}-{m:02d}" for m in range(1, int(today[5:7]) + 1)]
        cobranca: dict[tuple[str, str, str], int] = {}
        for periodo in meses:
            for code, fracao_id in fracoes.items():
                quota = (
                    DEMO_QUOTA_GRUPO_1
                    if code in DEMO_GRUPO_1
                    else DEMO_QUOTA_GRUPO_2
                )
                for rubrica, valor in (
                    ("quota_ordinaria", quota),
                    ("reforco_fundo", DEMO_REFORCO),
                ):
                    cur = connection.execute(
                        "INSERT INTO charges (fraction_id, rule_id, rubric, period, "
                        "amount_cents, due_date, generated_at, descricao) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, '')",
                        (
                            fracao_id,
                            regras[(fracao_id, rubrica)],
                            rubrica,
                            periodo,
                            valor,
                            f"{periodo}-{dia_vencimento:02d}",
                            now,
                        ),
                    )
                    cobranca[(code, periodo, rubrica)] = cur.lastrowid

        # 6) Pagamentos: A a D pagam tudo, E paga só a quota, F a H deixam em falta.
        def _pagar(code: str, periodo: str, rubricas: tuple[str, ...]) -> None:
            quota = DEMO_QUOTA_GRUPO_1 if code in DEMO_GRUPO_1 else DEMO_QUOTA_GRUPO_2
            valores = [
                (rubrica, quota if rubrica == "quota_ordinaria" else DEMO_REFORCO)
                for rubrica in rubricas
            ]
            soma = sum(valor for _rubrica, valor in valores)
            cur = connection.execute(
                "INSERT INTO payments (fraction_id, amount_cents, paid_on, method, "
                "reference, source, created_at, updated_at, form_token) "
                "VALUES (?, ?, ?, 'transferencia', '', ?, ?, ?, ?)",
                (
                    fracoes[code],
                    soma,
                    f"{periodo}-{dia_vencimento + 2:02d}",
                    FONTE_DEMO,
                    now,
                    now,
                    f"demo:{periodo}:{code}",
                ),
            )
            pagamento_id = cur.lastrowid
            for rubrica, valor in valores:
                connection.execute(
                    "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (pagamento_id, cobranca[(code, periodo, rubrica)], valor, now),
                )

        for periodo in meses:
            for code in ("A", "B", "C", "D"):
                _pagar(code, periodo, ("quota_ordinaria", "reforco_fundo"))
            _pagar("E", periodo, ("quota_ordinaria",))

        # 7) Categorias, despesas e pagamentos de despesa.
        categorias: dict[str, int] = {}
        for nome in ("Limpeza", "Eletricidade", "Manutenção", "Seguro"):
            cur = connection.execute(
                "INSERT INTO expense_categories (nome, created_at, updated_at) VALUES (?, ?, ?)",
                (nome, now, now),
            )
            categorias[nome] = cur.lastrowid

        despesa_n = 0
        for periodo in meses:
            despesa_n += 1
            _despesa(
                connection,
                categorias["Limpeza"],
                "Limpeza das áreas comuns",
                "Limpezas Fictícias, Lda",
                f"{periodo}-05",
                18000,
                pago_em=f"{periodo}-20",
                token=f"demo:despesa:{despesa_n}",
                now=now,
            )
            despesa_n += 1
            _despesa(
                connection,
                categorias["Eletricidade"],
                "Eletricidade das áreas comuns",
                "Elétrica Fictícia, Lda",
                f"{periodo}-08",
                9500,
                pago_em=f"{periodo}-25",
                token=f"demo:despesa:{despesa_n}",
                now=now,
            )
            if int(periodo[5:7]) % 3 == 1:
                despesa_n += 1
                _despesa(
                    connection,
                    categorias["Manutenção"],
                    "Manutenção do elevador",
                    "Elevadores Fictícios, Lda",
                    f"{periodo}-15",
                    25000,
                    pago_em=None,
                    token=f"demo:despesa:{despesa_n}",
                    now=now,
                )
        _despesa(
            connection,
            categorias["Seguro"],
            "Seguro do edifício",
            "Seguros Fictícios, Cia",
            f"{ano}-01-20",
            60000,
            pago_em=f"{ano}-02-05",
            token=f"demo:despesa:{despesa_n + 1}",
            now=now,
            classificacao="fundo_reserva",
        )

        # 8) Contas de acesso da demonstração.
        # `criar_utilizador` e `guardar_documento` gerem as suas próprias
        # transações: o que foi escrito à mão até aqui tem de estar confirmado.
        connection.commit()
        user_id, erros = criar_utilizador(
            DEMO_ADMIN[0], DEMO_ADMIN[1], PAPEL_ADMIN, "Administrador de Demonstração"
        )
        assert not erros, erros
        user_id, erros = criar_utilizador(
            DEMO_CONDOMINO[0],
            DEMO_CONDOMINO[1],
            PAPEL_CONDOMINO,
            DEMO_OWNERS["A"],
            (fracoes["A"],),
        )
        assert not erros, erros

        # 9) Documentos PDF de exemplo, gerados localmente.
        guardar_documento(
            origem="upload",
            dados=_pdf_ficticio(
                "Ata n.º 12 (fictícia)",
                [
                    "Condomínio das Acácias — reunião de assembleia (fictícia).",
                    "Ponto único: aprovação do orçamento de demonstração.",
                    "Deliberação: quotas mensais de 40,00 EUR e 45,00 EUR,",
                    "com reforço do fundo de 3,00 EUR.",
                ],
            ),
            nome_original="ata-12-ficticia.pdf",
            mime="application/pdf",
            titulo="Ata n.º 12 (fictícia)",
            categoria="Ata",
            ata_numero="12",
            data_documento=f"{ano}-01-10",
            descricao="Documento de demonstração: não corresponde a nenhuma ata real.",
        )
        guardar_documento(
            origem="upload",
            dados=_pdf_ficticio(
                "Fatura Limpezas Fictícias, Lda (fictícia)",
                [
                    "Serviço de limpeza das áreas comuns.",
                    "Total: 180,00 EUR.",
                ],
            ),
            nome_original="fatura-limpezas-ficticia.pdf",
            mime="application/pdf",
            titulo="Fatura de limpeza (fictícia)",
            categoria="Fatura",
            data_documento=f"{ano}-01-05",
            descricao="Documento de demonstração: fatura inventada.",
        )
        guardar_documento(
            origem="upload",
            dados=_pdf_ficticio(
                "Extrato bancário fictício",
                [
                    "Movimentos de exemplo, sem qualquer relação com contas reais.",
                    "Crédito: quota ordinária fração A — 40,00 EUR.",
                    "Débito: limpeza das áreas comuns — 180,00 EUR.",
                ],
            ),
            nome_original="extrato-ficticio.pdf",
            mime="application/pdf",
            titulo="Extrato bancário (fictício)",
            categoria="Extrato bancário",
            data_documento=today,
            descricao="Documento de demonstração: extrato inventado.",
        )

        connection.commit()

    return True, f"base de demonstração criada em {path}"


def _despesa(
    connection: sqlite3.Connection,
    categoria_id: int,
    descricao: str,
    fornecedor: str,
    data: str,
    valor: int,
    *,
    pago_em: str | None,
    token: str,
    now: str,
    classificacao: str = "ordinario",
) -> None:
    cur = connection.execute(
        "INSERT INTO expenses (descricao, fornecedor, category_id, referencia, data, "
        "due_date, amount_cents, classificacao, notas, created_at, updated_at, form_token) "
        "VALUES (?, ?, ?, '', ?, ?, ?, ?, '', ?, ?, ?)",
        (descricao, fornecedor, categoria_id, data, data, valor, classificacao, now, now, token),
    )
    if pago_em:
        connection.execute(
            "INSERT INTO expense_payments (expense_id, amount_cents, paid_on, method, "
            "reference, created_at, updated_at, form_token) "
            "VALUES (?, ?, ?, 'transferencia', '', ?, ?, ?)",
            (cur.lastrowid, valor, pago_em, now, now, f"{token}:pago"),
        )
