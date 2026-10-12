"""Testes dos extratores bancários sem IA (entrega 7).

Os PDFs sintéticos reproduzem a estrutura CONFIRMADA por diagnóstico com
documentos reais do Millennium BCP (2026-10-12), com conteúdo fictício:
- formulário: 「Operação:」 (débito/crédito), 「Nossa Referência:」,
  data `aaaa/mm/dd`, valor `NN,NN`;
- tabela: linha de data `aa/mm/dd`, cabeçalhos 「SALDOS CREDORES」/
  「SALDOS DEVEDORES」, valor com milhares por espaço e decimais por ponto,
  ID entre `#`.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.extratores import (
    executado,
    extrair_documento,
    hash_movimento,
    ocr_disponivel,
    parse_data,
    parse_valor,
    sentido_do_movimento,
)


# ---------------------------------------------------------------------------
# Validação de campos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "esperado"),
    [
        ("15/01/2027", "2027-01-15"),
        ("2026/08/31", "2026-08-31"),
        ("26-08-2026", "2026-08-26"),
        ("26/08/31", "2026-08-31"),  # yy/mm/dd (tabela Millennium)
        ("31/02/2027", None),  # dia impossível
        ("13/13/2027", None),
        ("abc", None),
    ],
)
def test_parse_data_rejeita_impossiveis(token, esperado):
    assert parse_data(token, hoje=__import__("datetime").date(2027, 2, 1)) == esperado


@pytest.mark.parametrize(
    ("token", "esperado"),
    [
        ("1 234.56", 123456),   # formato dos extratos Millennium
        ("1.234,56", 123456),   # formato pt-PT
        ("12,34", 1234),
        ("12.34", 1234),
        ("4 500.00", 450000),
        ("1.234", None),        # milhares ou decimais? ambíguo → nunca adivinhar
        ("12", None),           # sem casas decimais não é valor de extrato
        ("12345678901234", None),
    ],
)
def test_parse_valor(token, esperado):
    assert parse_valor(token) == esperado


def test_sentido_nunca_adivinha():
    assert sentido_do_movimento("Transferência recebida quota janeiro") == "recebido"
    assert sentido_do_movimento("Transferência enviada fornecedor") == "enviado"
    assert sentido_do_movimento("Custos de manutenção conta") == "comissao"
    assert sentido_do_movimento("Comissão processamento") == "comissao"
    assert sentido_do_movimento("Movimento sem indicação") is None
    assert sentido_do_movimento("Recebimento e debito em conta") is None


def test_sentido_com_acentos():
    # Os documentos reais escrevem 'crédito', 'débito' e 'comissão' com acento.
    assert sentido_do_movimento("Recebimento crédito") == "recebido"
    assert sentido_do_movimento("Comissão bancária") == "comissao"


def test_operacoes_nao_executadas():
    assert executado("Transferência agendada") is False
    assert executado("Operação pendente") is False
    assert executado("Transferência recusada") is False
    assert executado("Movimento anulada") is False
    assert executado("Transferência executada") is True


# ---------------------------------------------------------------------------
# PDFs sintéticos com a estrutura real confirmada
# ---------------------------------------------------------------------------


def _pdf_pagina(texto: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 40
    for linha in texto.splitlines():
        page.insert_text((40, y), linha)
        y += 14
    dados = doc.tobytes()
    doc.close()
    return dados


def _pdf_posicoes(palavras: list[tuple[float, float, str]]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, texto in palavras:
        page.insert_text((x, y), texto)
    dados = doc.tobytes()
    doc.close()
    return dados


def _movimento_millennium_formulario() -> bytes:
    """Layout de formulário (extrato grande), conteúdo fictício."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 248), "Banco Comercial Portugues, S.A.")
    page.insert_text((40, 264), "Conta Depositos a Ordem: 12345678901")
    page.insert_text((40, 280), "IBAN: PT50000000000001234567890")
    page.insert_text((40, 300), "Operacao: Transferencia Credito")
    page.insert_text((40, 316), "Nossa Referencia: 98765432109876543210")
    page.insert_text((40, 332), "Data Valor: 2026/08/31")
    page.insert_text((491, 380), "450,00")
    dados = doc.tobytes()
    doc.close()
    return dados


def _movimento_millennium_tabela() -> bytes:
    """Layout em tabela (extrato pequeno), conteúdo fictício."""
    return _pdf_posicoes([
        (40, 30, "Millennium BCP - extrato de movimentos"),
        (40, 60, "26/08/31"),
        (300, 60, "Movimento: quota condominio"),
        (300, 76, "ID: #12345678901234567890#"),
        (340, 370, "SALDOS CREDORES"),
        (475, 370, "SALDOS DEVEDORES"),
        (383, 381, "4 500.00"),
    ])


def test_extrato_reconhece_formulario_millennium(tmp_path):
    caminho = tmp_path / "extrato_formulario.pdf"
    caminho.write_bytes(_movimento_millennium_formulario())
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["estado"] == "ok"
    assert resultado["extrator"] == "millennium_extrato"
    assert len(resultado["movimentos"]) == 1
    movimento = resultado["movimentos"][0]
    assert movimento["data"] == "2026-08-31"
    assert movimento["valor_cents"] == 45000
    assert movimento["sentido"] == "recebido"  # 「Operação: Transferência Crédito」
    assert movimento["referencia"] == "98765432109876543210"
    assert movimento["executado_flag"] is True
    assert movimento["origem"]["pagina"] == 1


def test_extrato_reconhece_tabela_millennium(tmp_path):
    caminho = tmp_path / "extrato_tabela.pdf"
    caminho.write_bytes(_movimento_millennium_tabela())
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["estado"] == "ok"
    movimento = resultado["movimentos"][0]
    assert movimento["data"] == "2026-08-31"  # aa/mm/dd lido da linha de data
    assert movimento["valor_cents"] == 450000
    # Valor sob a coluna 「SALDOS CREDORES」 → entrada de dinheiro.
    assert movimento["sentido"] == "recebido"
    assert movimento["referencia"] == "12345678901234567890"


def test_tabela_valor_na_coluna_devedores_e_enviado(tmp_path):
    doc_bytes = _pdf_posicoes([
        (40, 30, "Millennium BCP - extrato de movimentos"),
        (40, 60, "26/08/31"),
        (340, 370, "SALDOS CREDORES"),
        (475, 370, "SALDOS DEVEDORES"),
        (520, 381, "120,00"),  # coluna da direita → saída
    ])
    caminho = tmp_path / "extrato_saida.pdf"
    caminho.write_bytes(doc_bytes)
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["movimentos"][0]["sentido"] == "enviado"
    assert resultado["movimentos"][0]["valor_cents"] == 12000


def test_formato_desconhecido_vai_para_revisao(tmp_path):
    caminho = tmp_path / "desconhecido.pdf"
    caminho.write_bytes(_pdf_pagina("Qualquer documento que não é extrato bancário.\n2027"))
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["estado"] == "revisao"
    assert "não reconhecido" in resultado["nota"]
    assert resultado["movimentos"] == []


def test_pdf_protegido_e_falha_tecnica(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((40, 40), "conteudo")
    caminho = tmp_path / "protegido.pdf"
    doc.save(str(caminho), owner_pw="dono", user_pw="usar",
             encryption=pymupdf.PDF_ENCRYPT_AES_256)
    doc.close()
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["estado"] == "falha"
    assert "palavra-passe" in resultado["nota"]


def test_pagina_sem_texto_sem_ocr_vai_para_revisao(tmp_path):
    assert ocr_disponivel() is False  # Tesseract não está instalado no ambiente de teste
    doc = pymupdf.open()
    doc.new_page()  # página em branco
    dados = doc.tobytes()
    doc.close()
    caminho = tmp_path / "scaneado.pdf"
    caminho.write_bytes(dados)
    resultado = extrair_documento(caminho, "application/pdf")
    assert resultado["estado"] == "revisao"
    assert "OCR indisponível" in resultado["nota"]


# ---------------------------------------------------------------------------
# Identidade do movimento (dedup comprovativo vs extrato)
# ---------------------------------------------------------------------------


def _movimento(**alterar):
    base = {
        "data": "2027-01-15",
        "valor_cents": 4700,
        "sentido": "recebido",
        "contraparte": "JOAO DA SILVA",
        "referencia": "REF-42",
        "descricao": "quota janeiro",
    }
    base.update(alterar)
    return base


def test_mesmo_movimento_em_dois_documentos_da_mesmo_hash():
    h1 = hash_movimento("millennium_extrato", _movimento(), 1)
    h2 = hash_movimento("millennium_extrato", _movimento(), 1)
    assert h1 == h2  # comprovativo e extrato: conta uma única vez


def test_movimentos_iguais_legitimos_tem_hashes_diferentes():
    h1 = hash_movimento("millennium_extrato", _movimento(), 1)
    h2 = hash_movimento("millennium_extrato", _movimento(), 2)
    assert h1 != h2  # dois pagamentos iguais legítimos contam cada um


def test_valores_diferentes_nao_confundem():
    assert (hash_movimento("millennium_extrato", _movimento(valor_cents=4701), 1)
            != hash_movimento("millennium_extrato", _movimento(), 1))
