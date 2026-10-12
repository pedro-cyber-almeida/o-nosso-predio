"""Conversões de dinheiro, permilagem e datas.

Dinheiro: cêntimos inteiros.
Permilagem: centésimos de permilagem inteiros (12345 == 123,45).
Datas: armazenadas em ISO (YYYY-MM-DD), apresentadas em DD/MM/YYYY.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

DATE_INPUT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
PT_DATE_INPUT_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
MONTH_INPUT_RE = re.compile(r"^(\d{4})-(\d{2})$")
PT_MONTH_INPUT_RE = re.compile(r"^(\d{1,2})/(\d{4})$")

ANO_MINIMO = 1900
ANO_MAXIMO = 9999


def _normalize_number(raw: object) -> str | None:
    """Reduzir texto numérico português a uma string com ponto decimal.

    Aceita 1234, 1234,56, 1234.56, 1.234,56 e 1 234,56.
    Um ponto isolado com três casas finais é tratado como separador de milhares,
    porque em português o ponto separa milhares e a vírgula separa cêntimos:
    "1.234" vale 1234 euros, "12,345" é rejeitado por ter três casas de cêntimos.
    Devolve None para texto ambíguo ou inválido.
    """
    text = "" if raw is None else str(raw)
    text = text.strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return None
    if text.count(",") > 1 or text.count(".") > 1:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text:
        integer_part, _, fractional_part = text.partition(".")
        if len(fractional_part) == 3 and integer_part.lstrip("-").isdigit():
            text = text.replace(".", "")

    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", text):
        return None
    return text


def parse_money(raw: object) -> int | None:
    """Converter texto em cêntimos inteiros. Devolve None se inválido."""
    normalized = _normalize_number(raw)
    if normalized is None:
        return None
    negative = normalized.startswith("-")
    if negative:
        normalized = normalized[1:]
    integer_part, _, fractional_part = normalized.partition(".")
    cents = int(integer_part) * 100 + int((fractional_part + "00")[:2])
    return -cents if negative else cents


def format_money(cents: int | None) -> str:
    """Formatar cêntimos como 1.234,56 EUR."""
    if cents is None:
        return "por confirmar"
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    text = f"{value // 100}:{value % 100:02d}"
    integer_part, _, fractional_part = text.partition(":")
    grouped = f"{int(integer_part):,}".replace(",", ".")
    return f"{sign}{grouped},{fractional_part} EUR"


def format_money_input(cents: int | None) -> str:
    """Formatar cêntimos para um campo de edição em euros: 1.234,56 (sem a moeda).

    Devolve texto vazio quando o valor está por confirmar, para que o campo
    reapresentado volte a ser lido como euros e não como cêntimos.
    """
    if cents is None:
        return ""
    return format_money(cents).removesuffix(" EUR")


def parse_stored_cents(raw: object) -> int | None:
    """Ler cêntimos já guardados como texto. Devolve None se não forem um inteiro simples.

    Um valor corrompido ou escrito à mão na base aparece como "por confirmar",
    em vez de fazer a página falhar.
    """
    text = "" if raw is None else str(raw).strip()
    if not text or not re.fullmatch(r"\d+", text):
        return None
    return int(text)


def parse_int_id(raw: object) -> int | None:
    """Ler um identificador numérico a partir de texto de formulário.

    Só dígitos ASCII, e no máximo nove: `str.isdigit()` aceita caracteres como `²`,
    que `int()` depois recusa. Uma letra de fração ou um id vai sempre por aqui, para
    que texto estranho seja uma recusa com mensagem e não um erro interno.
    """
    text = "" if raw is None else str(raw).strip()
    if not re.fullmatch(r"[0-9]{1,9}", text):
        return None
    return int(text)


def parse_permilagem(raw: object) -> int | None:
    """Converter texto em centésimos de permilagem. Devolve None se inválido."""
    normalized = _normalize_number(raw)
    if normalized is None:
        return None
    integer_part, _, fractional_part = normalized.partition(".")
    if len(fractional_part) > 2:
        return None
    value = int(integer_part) * 100 + int((fractional_part + "00")[:2])
    if normalized.startswith("-"):
        return None
    return value


def format_permilagem(value: int | None) -> str:
    """Formatar centésimos de permilagem como 123,45."""
    if value is None:
        return "por confirmar"
    value = int(value)
    return f"{value // 100},{value % 100:02d}"


def parse_date(raw: object) -> str | None:
    """Aceitar ISO ou DD/MM/YYYY e devolver a data em ISO. None se inválida."""
    text = str(raw or "").strip()
    if not text:
        return None
    match = DATE_INPUT_RE.match(text) or PT_DATE_INPUT_RE.match(text)
    if match is None:
        return None
    if DATE_INPUT_RE.match(text):
        year, month, day = (int(part) for part in match.groups())
    else:
        day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def format_date(iso_date: object) -> str:
    """Apresentar uma data ISO em DD/MM/YYYY."""
    text = str(iso_date or "").strip()
    if not text:
        return ""
    match = DATE_INPUT_RE.match(text)
    if match is None:
        return text
    year, month, day = match.groups()
    return f"{day}/{month}/{year}"


def today_iso() -> str:
    return date.today().isoformat()


# --- Meses de cobrança -------------------------------------------------------
# Um período de cobrança é um mês, guardado como 'YYYY-MM'. As regras de quota
# cobrem meses inteiros: começam no dia 1 e terminam no último dia do mês.


def parse_month(raw: object) -> str | None:
    """Aceitar 'YYYY-MM' ou 'MM/YYYY' e devolver o mês em 'YYYY-MM'.

    Uma data completa (15/01/2027) é recusada: as regras e as cobranças cobrem
    meses inteiros, e um dia escrito num campo de mês é uma confusão que tem de
    ser corrigida por quem escreve, não adivinhada pela aplicação.
    """
    text = str(raw or "").strip()
    if not text:
        return None

    match = MONTH_INPUT_RE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        match = PT_MONTH_INPUT_RE.match(text)
        if match is None:
            return None
        month, year = int(match.group(1)), int(match.group(2))

    if not ANO_MINIMO <= year <= ANO_MAXIMO or not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def format_month(period: object) -> str:
    """Apresentar 'YYYY-MM' como MM/ANO."""
    text = str(period or "").strip()
    match = MONTH_INPUT_RE.match(text)
    if match is None:
        return text
    year, month = match.groups()
    return f"{month}/{year}"


def month_start(period: str) -> str:
    """Primeiro dia do mês (inclusivo)."""
    return f"{period}-01"


def month_end(period: str) -> str:
    """Último dia do mês (inclusivo)."""
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year}-12-31"
    primeiro_do_seguinte = date(year, month + 1, 1)
    return (primeiro_do_seguinte - timedelta(days=1)).isoformat()


def next_month(period: str) -> str:
    """Mês seguinte. Devolve texto vazio quando sairia do intervalo de anos aceite."""
    year, month = int(period[:4]), int(period[5:7])
    ano, mes = (year + 1, 1) if month == 12 else (year, month + 1)
    if not ANO_MINIMO <= ano <= ANO_MAXIMO:
        return ""
    return f"{ano:04d}-{mes:02d}"


def previous_month(period: str) -> str:
    """Mês anterior. Devolve texto vazio quando sairia do intervalo de anos aceite."""
    year, month = int(period[:4]), int(period[5:7])
    ano, mes = (year - 1, 12) if month == 1 else (year, month - 1)
    if not ANO_MINIMO <= ano <= ANO_MAXIMO:
        return ""
    return f"{ano:04d}-{mes:02d}"


def current_month() -> str:
    return date.today().isoformat()[:7]

