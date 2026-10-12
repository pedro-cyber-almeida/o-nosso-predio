"""Extração de texto e de movimentos de documentos bancários — sem IA.

- Primeiro extrai o texto existente no PDF (PyMuPDF; pypdf como recurso).
- OCR local só nas páginas sem texto útil (pytesseract/Tesseract quando
  disponível; sem ele o documento vai para revisão — nunca se adivinha).
- Extratores por formato confirmado, com versão registada em cada extração
  (permite diagnóstico e reprocessamento quando um extrator melhora).
- Cada campo guarda a origem (página e linha do texto de onde foi lido).
- Formato desconhecido, leitura duvidosa ou documento ilegível → revisão.
  Nunca se inventam valores.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Texto das páginas
# ---------------------------------------------------------------------------


def texto_paginas(caminho: Path) -> tuple[list[str], list | None, str]:
    """(textos por página, palavras por página com posição, método).

    PyMuPDF primeiro (dá palavras com coordenadas, necessárias para colunas);
    pypdf como recurso (sem posições → palavras=None). Levanta ValueError
    quando o PDF está protegido por palavra-passe.
    """
    try:
        import pymupdf

        doc = pymupdf.open(str(caminho))
        try:
            if doc.needs_pass:
                raise ValueError("PDF protegido por palavra-passe")
            paginas = [page.get_text() for page in doc]
            palavras = [page.get_text("words") for page in doc]
            return paginas, palavras, "pymupdf"
        finally:
            doc.close()
    except ImportError:
        pass
    from pypdf import PdfReader

    leitor = PdfReader(str(caminho))
    if leitor.is_encrypted:
        raise ValueError("PDF protegido por palavra-passe")
    return [(pagina.extract_text() or "") for pagina in leitor.pages], None, "pypdf"


def pagina_util(texto: str) -> bool:
    """Uma página tem texto útil quando tem letras e pelo menos um número."""
    return bool(re.search(r"[A-Za-zÀ-ÿ]", texto)) and bool(re.search(r"\d", texto))


def ocr_disponivel() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_paginas(caminho: Path, indices: list[int]) -> dict[int, str] | None:
    """OCR local de páginas sem texto. Devolve {índice: texto} ou None se OCR
    indisponível. Renderiza e processa uma página de cada vez (memória)."""
    if not indices or not ocr_disponivel():
        return None
    import pymupdf
    import pytesseract
    from PIL import Image

    resultados: dict[int, str] = {}
    doc = pymupdf.open(str(caminho))
    try:
        for indice in indices:
            pix = doc[indice].get_pixmap(dpi=200)
            imagem = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            resultados[indice] = pytesseract.image_to_string(imagem, lang="por")
    finally:
        doc.close()
    return resultados


# ---------------------------------------------------------------------------
# Validação de campos (moeda, datas, valores, sentido, estado)
# ---------------------------------------------------------------------------

RE_DATA_ISO = re.compile(r"^(\d{2})/(\d{2})/(\d{2})$")          # yy/mm/dd (Millennium tabela)
RE_DATA_ANO = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")  # aaaa/mm/dd (Millennium formulário)
RE_DATA_LONGA = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")  # dd/mm/aaaa

# Formatos de valor observados: '1 234.56' e '1 234,56' (Milennium, milhares
# por espaço) e '1.234,56' (pt-PT).
RE_VALOR_ESPACO_PONTO = re.compile(r"^\d{1,3}(?: \d{3})*\.\d{2}$")
RE_VALOR_ESPACO_VIRGULA = re.compile(r"^\d{1,3}(?: \d{3})*,\d{2}$")
RE_VALOR_PONTO_VIRGULA = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
RE_VALOR_VIRGULA = re.compile(r"^\d+,\d{2}$")
RE_VALOR_PONTO = re.compile(r"^\d+\.\d{2}$")

PALAVRAS_NAO_EXECUTADO = ("agendad", "pendente", "recusad", "anulad", "devolvid", "estornad")
# 'custo' isolado não conta: as tabelas de preços do banco usam a palavra.
PALAVRAS_COMISSAO = ("comissa", "custos de", "mantencao", "manutencao", "spread")
PALAVRAS_RECEBIDO = ("recebimento", "recebida", "recebido", "deposito", "credito")
PALAVRAS_ENVIADO = ("transferencia enviada", "pagamento a ", "pagamento de ", "debito", "pagou")


def _normalizar(texto: str) -> str:
    """minúsculas, sem acentos e espaços colapsados — os documentos reais usam
    'Operação', 'crédito', 'débito' e as palavras-chave comparam sem acento."""
    import unicodedata

    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


def parse_data(token: str, hoje: date | None = None) -> str | None:
    """dd/mm/aaaa, aaaa/mm/dd, dd-mm-aaaa ou yy/mm/dd → ISO. Rejeita datas impossíveis."""
    token = token.strip()
    m = RE_DATA_ANO.match(token)
    if m:
        ano, mes, dia = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(ano, mes, dia).isoformat()
        except ValueError:
            return None
    m = RE_DATA_LONGA.match(token)
    if m:
        dia, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = RE_DATA_ISO.match(token)
        if not m:
            return None
        ano_curto, mes, dia = int(m.group(1)), int(m.group(2)), int(m.group(3))
        seculo = (hoje or date.today()).year // 100 * 100
        ano = seculo + ano_curto
        if ano > (hoje or date.today()).year + 1:  # yy/mm/dd nunca é futuro distante
            ano -= 100
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


def parse_valor(token: str) -> int | None:
    """'1 234.56', '1.234,56', '12,34' ou '12.34' → cêntimos. Exatamente 2
    casas decimais; moeda assumida EUR (validado pelo chamador). None se ambíguo."""
    token = token.strip()
    if RE_VALOR_ESPACO_PONTO.match(token) or RE_VALOR_ESPACO_VIRGULA.match(token):
        inteiro, decimal = token.rsplit(".", 1) if "." in token[-4:] else token.rsplit(",", 1)
        inteiro = inteiro.replace(" ", "")
    elif RE_VALOR_PONTO_VIRGULA.match(token):
        inteiro, decimal = token.rsplit(",", 1)
        inteiro = inteiro.replace(".", "")
    elif RE_VALOR_VIRGULA.match(token):
        inteiro, decimal = token.rsplit(",", 1)
    elif RE_VALOR_PONTO.match(token):
        inteiro, decimal = token.rsplit(".", 1)
    else:
        return None
    try:
        return int(inteiro) * 100 + int(decimal)
    except ValueError:
        return None


def sentido_do_movimento(texto: str) -> str | None:
    """'recebido', 'enviado', 'comissao' ou None (ambíguo → revisão, nunca adivinhar)."""
    baixo = _normalizar(texto)
    if any(p in baixo for p in PALAVRAS_COMISSAO):
        return "comissao"
    recebido = any(p in baixo for p in PALAVRAS_RECEBIDO)
    enviado = any(p in baixo for p in PALAVRAS_ENVIADO)
    if recebido and not enviado:
        return "recebido"
    if enviado and not recebido:
        return "enviado"
    return None


def executado(texto: str) -> bool:
    """Operações agendadas/pendentes/recusadas/anuladas não contam como executadas."""
    baixo = _normalizar(texto)
    return not any(p in baixo for p in PALAVRAS_NAO_EXECUTADO)


# ---------------------------------------------------------------------------
# Extratores por formato (cada um com versão registada na extração)
# ---------------------------------------------------------------------------
#
# Formatos confirmados por diagnóstico com documentos reais (2026-10-12):
# A) Extrato Millennium em formulário: blocos com rótulos 「Operação:」
#    (débito/crédito), 「Nossa Referência:」, data `aaaa/mm/dd`, valor `NN,NN`.
# B) Extrato Millennium em tabela: linha de data `aa/mm/dd`, colunas
#    「SALDOS CREDORES」/「SALDOS DEVEDORES」 — o sentido vem da posição da
#    palavra do valor; ID `#N…N#`; valor com milhares por espaço.
# O que não encaixa, ou tem sentido/valor ambíguo, fica para revisão.

RE_LINHA_DATA = re.compile(r"^\s*(\d{2}/\d{2}/\d{2}|\d{4}/\d{2}/\d{2})\b")
RE_ID_MILLENNIUM = re.compile(r"#\s*(\d{11,25})\s*#")
RE_VALOR_SOLTO = re.compile(
    r"\d{1,3}(?: \d{3})*[.,]\d{2}|\d{1,3}(?:\.\d{3})*,\d{2}|\d+[,.]\d{2}")


def _juntar_valores_com_esparcos(palavras):
    """Juntar palavras de um valor partido: '4' + '500.00' → '4 500.00'.

    Os extratos reais escrevem milhares separados por espaço e a extração por
    palavras divide-os. Só junta vizinhos na mesma linha (mesma altura, gap
    pequeno) quando o conjunto volta a ser um número de valor plausível.
    """
    if not palavras:
        return palavras
    ordenadas = sorted(palavras, key=lambda w: (round(w[1], 1), w[0]))
    saida = []
    i = 0
    while i < len(ordenadas):
        x0, y0, x1, y1, texto = ordenadas[i][:5]
        resto = ordenadas[i][5:]
        while i + 1 < len(ordenadas):
            nxt = ordenadas[i + 1]
            if abs(nxt[1] - y0) > 2 or nxt[0] - x1 > 10:
                break
            if re.fullmatch(r"\d{1,3}", texto) and re.fullmatch(r"\d{3}", nxt[4]):
                texto += " " + nxt[4]
                x1 = nxt[2]
                i += 1
                continue
            if re.fullmatch(r"\d{1,3}(?: \d{3})*", texto) and re.fullmatch(r"\d{3}[.,]\d{2}", nxt[4]):
                texto += " " + nxt[4]
                x1 = nxt[2]
                i += 1
                continue
            break
        saida.append((x0, y0, x1, y1, texto, *resto))
        i += 1
    return saida
RE_ROTULO = re.compile(r"^\s*([^:]{3,40}):\s*(.*)$")


def _reconhecer_millennium(texto: str) -> bool:
    baixo = texto.lower()
    return "millennium" in baixo or "banco comercial portugues" in baixo


def _rotulos(bloco: list[str]) -> dict[str, str]:
    saida: dict[str, str] = {}
    for linha in bloco:
        m = RE_ROTULO.match(linha)
        if m:
            saida[_normalizar(m.group(1))] = m.group(2).strip()
    return saida


def _sentido_operacao(valor_rotulo: str) -> str | None:
    baixo = _normalizar(valor_rotulo)
    if any(p in baixo for p in PALAVRAS_COMISSAO):
        return "comissao"
    credito = "credito" in baixo or "receb" in baixo
    debito = "debito" in baixo or "enviad" in baixo or "pagamento" in baixo
    if credito and not debito:
        return "recebido"
    if debito and not credito:
        return "enviado"
    return None


def _palavra_y(palavras_page, predicate) -> float | None:
    for w in palavras_page or []:
        if predicate(w[4]):
            return w[1]
    return None


def _valor_proximo(palavras_page, y_alvo, bloco_texto: str) -> int | None:
    """Valor mais próximo da linha-âncora (posição de palavra). Ambíguo → None."""
    if palavras_page is not None and y_alvo is not None:
        candidatos = [w for w in palavras_page
                      if RE_VALOR_SOLTO.fullmatch(w[4]) and abs(w[1] - y_alvo) <= 120]
        if len(candidatos) == 1:
            return parse_valor(candidatos[0][4])
        valores = {parse_valor(w[4]) for w in candidatos}
        valores.discard(None)
        if len(valores) == 1 and candidatos:
            return next(iter(valores))
        return None
    valores = {parse_valor(v) for v in RE_VALOR_SOLTO.findall(bloco_texto)}
    valores.discard(None)
    return next(iter(valores)) if len(valores) == 1 else None


def _extrair_millennium(paginas: list[str], palavras: list | None) -> list[dict]:
    movimentos: list[dict] = []
    for n_pagina, texto in enumerate(paginas, start=1):
        linhas = texto.splitlines()
        palavras_page = palavras[n_pagina - 1] if palavras else None
        palavras_page = _juntar_valores_com_esparcos(palavras_page) if palavras_page else palavras_page

        # A) Layout de formulário: blocos ancorados em 「Operação:」.
        blocos_operacao = [i for i, l in enumerate(linhas)
                           if ":" in l and _normalizar(l.split(":")[0]) == "operacao"]
        if blocos_operacao:
            for k, inicio in enumerate(blocos_operacao):
                fim = blocos_operacao[k + 1] if k + 1 < len(blocos_operacao) else len(linhas)
                bloco = linhas[inicio:fim]
                rotulos = _rotulos(bloco)
                bloco_texto = " ".join(bloco)
                y_op = _palavra_y(palavras_page,
                                  lambda t: _normalizar(t).rstrip(":") == "operacao")
                movimentos.append({
                    "data": next((d for token in
                                  re.findall(r"\b\d{4}[/-]\d{2}[/-]\d{2}\b|\b\d{2}/\d{2}/\d{2}\b"
                                             r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",
                                             bloco_texto)
                                  if (d := parse_data(token))), None),
                    "valor_cents": _valor_proximo(palavras_page, y_op, bloco_texto),
                    "moeda": "EUR",
                    "sentido": _sentido_operacao(rotulos.get("operacao", "")),
                    "executado_flag": executado(bloco_texto),
                    "referencia": (rotulos.get("nossa referencia", "") or "")[:40],
                    "contraparte": (rotulos.get("entidade devedora", "")
                                    or rotulos.get("entidade credora", ""))[:160],
                    "descricao": _normalizar(bloco_texto)[:300],
                    "origem": {"pagina": n_pagina, "linha": inicio + 1},
                })
            continue

        # B) Layout em tabela: a tabela-resumo 「SALDOS CREDORES / SALDOS
        #    DEVEDORES」 traz os valores; o sentido vem da coluna. A data e a
        #    referência só se associam quando há 1 valor e 1 data na página
        #    (nunca adivinhar o emparelhamento).
        col_cred = col_dev = header_y = None
        if palavras_page:
            for w in palavras_page:
                baixo = _normalizar(w[4])
                if baixo == "credores":
                    col_cred, header_y = w[0], w[1]
                elif baixo == "devedores":
                    col_dev = max(col_dev or 0, w[0])
        datas = [m.group(1) for m in (RE_LINHA_DATA.match(l) for l in linhas) if m]
        if col_cred is not None and col_dev is not None and header_y is not None and palavras_page:
            # Só a faixa logo abaixo dos cabeçalhos: mais abaixo vêm as tabelas
            # de preços do banco, que não são movimentos.
            valores_tab = [w for w in palavras_page
                           if RE_VALOR_SOLTO.fullmatch(w[4])
                           and header_y - 5 < w[1] <= header_y + 40
                           and w[0] >= col_cred - 30]
            ids = RE_ID_MILLENNIUM.findall(texto)
            unicos_id = set(ids)
            for w in valores_tab:
                movimentos.append({
                    "data": parse_data(datas[0]) if len(datas) == 1 and len(valores_tab) == 1 else None,
                    "valor_cents": parse_valor(w[4]),
                    "moeda": "EUR",
                    "sentido": "recebido" if w[0] < (col_cred + col_dev) / 2 else "enviado",
                    "executado_flag": executado(texto),
                    "referencia": ids[0] if len(unicos_id) == 1 and len(valores_tab) == 1 else "",
                    "contraparte": "",
                    "descricao": _normalizar(" ".join(datas) or "tabela-resumo")[:300],
                    "origem": {"pagina": n_pagina, "linha": None,
                               "palavra": [round(w[0]), round(w[1])]},
                })
            continue

        # C) Recurso genérico: blocos ancorados em linha de data; sentido só
        #    por palavras do bloco (sem colunas).
        i = 0
        while i < len(linhas):
            m = RE_LINHA_DATA.match(linhas[i])
            if not m:
                i += 1
                continue
            bloco = [linhas[i]]
            j = i + 1
            while j < len(linhas) and not RE_LINHA_DATA.match(linhas[j]):
                bloco.append(linhas[j])
                j += 1
                if len(bloco) > 30:
                    break
            bloco_texto = " ".join(bloco)
            ids = RE_ID_MILLENNIUM.findall(bloco_texto)
            y_data = _palavra_y(palavras_page, lambda t: bool(
                re.fullmatch(r"\d{2}/\d{2}/\d{2}|\d{4}/\d{2}/\d{2}", t)))
            valor = _valor_proximo(palavras_page, y_data, bloco_texto)
            sentido = sentido_do_movimento(bloco_texto)
            if sentido is None and palavras_page and col_cred is not None and col_dev is not None:
                # Sentido pela coluna onde está a palavra do valor escolhido.
                x_valor = None
                for w in palavras_page:
                    if RE_VALOR_SOLTO.fullmatch(w[4]) and y_data is not None \
                            and abs(w[1] - y_data) <= 120:
                        x_valor = w[0]
                        break
                if x_valor is not None:
                    sentido = "recebido" if x_valor < (col_cred + col_dev) / 2 else "enviado"
            movimentos.append({
                "data": parse_data(m.group(1)),
                "valor_cents": valor,
                "moeda": "EUR",
                "sentido": sentido,
                "executado_flag": executado(bloco_texto),
                "referencia": ids[0] if ids else "",
                "contraparte": "",
                "descricao": _normalizar(bloco_texto)[:300],
                "origem": {"pagina": n_pagina, "linha": i + 1},
            })
            i = j
    return movimentos


EXTRATORES: list[tuple] = [
    ("millennium_extrato", 2, _reconhecer_millennium, _extrair_millennium),
]


def hash_movimento(documento_banco: str, movimento: dict, ocorrencia: int) -> str:
    """Identidade estável do movimento para deduplicar comprovativo vs extrato.

    Inclui a ocorrência no documento: dois movimentos legítimos exatamente
    iguais têm hashes diferentes; o mesmo movimento em dois documentos tem o
    mesmo hash (ocorrência 1 em cada um).
    """
    base = "|".join([
        documento_banco,
        str(movimento.get("data") or ""),
        str(movimento.get("valor_cents")),
        str(movimento.get("sentido") or ""),
        _normalizar(movimento.get("contraparte", "")),
        str(movimento.get("referencia") or ""),
        str(ocorrencia),
    ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Portas de entrada
# ---------------------------------------------------------------------------


def extrair_documento(caminho: Path, mime: str) -> dict:
    """Extrai movimentos de um documento. Nunca inventa: o que não é lido ou
    não é reconhecido fica com nota e vai para revisão.

    Devolve {'estado': 'ok'|'revisao'|'falha', 'extrator', 'versao',
    'movimentos', 'nota', 'ocr_usado', 'paginas', 'caracteres'}.
    """
    if mime not in ("application/pdf", "text/plain"):
        return {"estado": "falha", "extrator": "", "versao": 0, "movimentos": [],
                "nota": f"tipo {mime or '(sem tipo)'} não analisável", "ocr_usado": False,
                "paginas": 0, "caracteres": 0}

    if mime == "text/plain":
        paginas = [caminho.read_text(encoding="utf-8", errors="replace")]
        ocr_usado = False
    else:
        paginas: list[str] = []
        palavras: list | None = None
        try:
            paginas, palavras, _ = texto_paginas(caminho)
        except ValueError as exc:  # protegido por palavra-passe
            return {"estado": "falha", "extrator": "", "versao": 0, "movimentos": [],
                    "nota": str(exc), "ocr_usado": False, "paginas": 0, "caracteres": 0}
        except Exception as exc:
            return {"estado": "falha", "extrator": "", "versao": 0, "movimentos": [],
                    "nota": f"falha técnica ao ler o PDF ({type(exc).__name__})",
                    "ocr_usado": False, "paginas": len(paginas), "caracteres": 0}

        sem_texto = [i for i, t in enumerate(paginas) if not pagina_util(t)]
        if sem_texto:
            ocr = ocr_paginas(caminho, sem_texto)
            if ocr is None:
                return {"estado": "revisao", "extrator": "", "versao": 0, "movimentos": [],
                        "nota": "páginas sem texto útil e OCR indisponível "
                                "(instalar Tesseract/pytesseract para tentar OCR).",
                        "ocr_usado": False, "paginas": len(paginas),
                        "caracteres": sum(len(t) for t in paginas)}
            paginas = [ocr.get(i, t) if i in ocr else t for i, t in enumerate(paginas)]
            ocr_usado = True
            palavras = None  # as posições da camada de texto partida não valem para páginas OCR
        else:
            ocr_usado = False

    texto_junto = "\n".join(paginas)
    for nome, versao, reconhecer, extrair in EXTRATORES:
        if not reconhecer(texto_junto):
            continue
        movimentos = extrair(paginas, palavras)
        if not movimentos:
            return {"estado": "sem_movimentos", "extrator": nome, "versao": versao,
                    "movimentos": [], "nota": "formato reconhecido mas sem movimentos legíveis",
                    "ocr_usado": ocr_usado, "paginas": len(paginas), "caracteres": len(texto_junto)}
        return {"estado": "ok", "extrator": nome, "versao": versao,
                "movimentos": movimentos, "nota": "",
                "ocr_usado": ocr_usado, "paginas": len(paginas), "caracteres": len(texto_junto)}

    return {"estado": "revisao", "extrator": "", "versao": 0, "movimentos": [],
            "nota": "formato não reconhecido por nenhum extrator", "ocr_usado": ocr_usado,
            "paginas": len(paginas), "caracteres": len(texto_junto)}
