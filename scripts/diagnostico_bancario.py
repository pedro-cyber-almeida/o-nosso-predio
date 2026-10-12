"""Diagnóstico de formatos bancários reais (só leitura; saída mascarada).

Descarrega anexos candidatos para %TEMP%\\opencode\\bancario\\ (fora da app e
fora do Git) e imprime apenas estrutura mascarada: banco detetado, páginas,
nº de caracteres, contagens de padrões e modelos de linha com dígitos→N e
letras→x. Nunca imprime IBANs, valores, nomes, assunto completo, tokens.

Uso: python scripts/diagnostico_bancario.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402

CANDIDATOS = Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "bancario"

BANCOS = [
    "millennium", "bcp", "caixa geral", "cgd", "santander", "bpi",
    "novo banco", "banco ctt", "activobank", "revolut", "dream bank",
    "openbank", "getnet", "multibanco", "credito agricola", "bankinter",
]

PALAVRAS = [
    "extrato", "movimento", "transferencia", "debito", "credito",
    "comprovativo", "recibo", "referencia", "saldo", "iban", "entidade",
    "data valor", "operacao", "agendada", "pendente", "anulada", "devolvida",
    "comissao", "custo", "mantencao", "pagamento",
]

RE_DATA = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
RE_VALOR = re.compile(r"\b\d{1,3}(?:\.\d{3})*,\d{2}\b")
RE_IBAN = re.compile(r"\bPT\d{2}[ ]?(?:[A-Z]{2} ?)?\d{4}[ ]?\d{4}[ ]?\d{2}\b", re.I)


def dominio_de(remetente: str) -> str:
    """Domínio a partir de 'Nome <a@b.pt>' ou 'a@b.pt' (nunca o endereço completo)."""
    parte = remetente.split("<")[-1].split(">")[0].strip()
    return parte.split("@")[-1].strip().lower()


def mascarar(linha: str) -> str:
    """Estrutura sem conteúdo: dígitos→N, letras→x, IBAN→IBAN****."""
    linha = RE_IBAN.sub("IBAN****", linha)
    linha = re.sub(r"\d", "N", linha)
    linha = re.sub(r"[A-Za-zÀ-ÿ]", "x", linha)
    return linha


def analisar_pdf(caminho: Path) -> None:
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # avisos de fontes não são diagnóstico
    from pypdf import PdfReader

    leitor = PdfReader(str(caminho))
    if leitor.is_encrypted:
        print("    PDF cifrado (sem texto acessível)")
        return
    paginas = list(leitor.pages)
    texto = "\n".join((p.extract_text() or "") for p in paginas)
    print(f"    PDF: {len(paginas)} páginas, {len(texto)} caracteres extraídos")
    baixo = texto.lower()
    bancos = [b for b in BANCOS if b in baixo]
    print(f"    banco detetado: {bancos or 'nenhum (por confirmar)'}")
    presencas = {p: baixo.count(p) for p in PALAVRAS if baixo.count(p)}
    print(f"    palavras: {presencas or 'nenhuma'}")
    print(f"    datas: {len(RE_DATA.findall(texto))} · valores: {len(RE_VALOR.findall(texto))} "
          f"· IBANs: {len(RE_IBAN.findall(texto))}")
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    print(f"    linhas não vazias: {len(linhas)}")
    # Modelos de linha (estrutura apenas, mascarada): as primeiras com data+valor.
    modelos = [mascarar(l) for l in linhas if RE_DATA.search(l) and RE_VALOR.search(l)][:8]
    for m in modelos:
        print(f"    linha: {m[:110]}")
    cabecalho = [mascarar(l) for l in linhas[:6]]
    for c in cabecalho:
        print(f"    cab: {c[:110]}")


def cabecalhos_de(msg: dict) -> dict:
    return {str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in msg.get("payload", {}).get("headers", [])}


def main() -> int:
    app = create_app()
    from app import gmail
    from app.integracoes import gmail_ligado

    CANDIDATOS.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        if not gmail_ligado():
            print("Sem ligação Gmail.")
            return 1
        token = gmail._cabecalho_autorizacao()
        if token is None:
            print("Token não obtido.")
            return 1

        def cab():
            return {"Authorization": f"Bearer {token}"}

        estado, corpo = gmail.pedido_json(
            f"{gmail.API_GMAIL}/me/messages?maxResults=40&fields=messages(id,threadId)",
            headers=cab(), timeout=gmail.TIMEOUT_LISTA,
        )
        if estado != 200:
            print("listagem recusada:", gmail._motivo_erro_gmail(corpo))
            return 1

        # 1) Quem fala comigo (só domínios, nunca endereços completos).
        info: dict[str, dict] = {}
        for item in corpo.get("messages", [])[:40]:
            mid = str(item.get("id", ""))
            estado_m, msg = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages/{mid}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=Subject",
                headers=cab(), timeout=gmail.TIMEOUT_METADATA,
            )
            if estado_m != 200:
                continue
            info[mid] = cabecalhos_de(msg)
            print(f"remetente: {dominio_de(info[mid].get('from', '')) or '(sem From)'}")

        # 2) Procura termos bancários (a pesquisa é feita no Gmail; só ids saem daqui).
        vistos = set(info)
        extra: list[str] = []
        for query in ("has:attachment (extrato OR comprovativo OR recibo OR fatura)",
                      "has:attachment (transferencia OR pagamento OR saldo OR conta)"):
            estado_q, corpo_q = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages?{urlencode({'maxResults': 15, 'q': query, 'fields': 'messages(id,threadId)'})}",
                headers=cab(), timeout=gmail.TIMEOUT_LISTA,
            )
            print(f"pesquisa {query!r}: estado {estado_q}, {len(corpo_q.get('messages', []))} resultados")
            if estado_q != 200:
                continue
            for item in corpo_q.get("messages", []):
                mid = str(item.get("id", ""))
                if mid in vistos:
                    continue
                vistos.add(mid)
                estado_m, msg = gmail.pedido_json(
                    f"{gmail.API_GMAIL}/me/messages/{mid}?format=metadata"
                    "&metadataHeaders=From&metadataHeaders=Subject",
                    headers=cab(), timeout=gmail.TIMEOUT_METADATA,
                )
                if estado_m == 200:
                    info[mid] = cabecalhos_de(msg)
                    extra.append(mid)

        candidatos: list[tuple[str, str]] = []
        for mid, cabecalhos in info.items():
            remetente = cabecalhos.get("from", "").lower()
            assunto = cabecalhos.get("subject", "").lower()
            dominio = dominio_de(remetente)
            eh_banco = any(b in remetente or b in assunto for b in BANCOS)
            eh_extrato = any(p in assunto for p in ("extrato", "comprovativo", "recibo", "movimento"))
            if mid in extra or eh_banco or eh_extrato:
                print(f"[{mid}] dominio={dominio} assunto_mascarado={mascarar(cabecalhos.get('subject', ''))[:60]}")
                candidatos.append((mid, dominio))

        print(f"\ncandidatos: {len(candidatos)}")
        guardados = 0
        for mid, dominio in candidatos:
            estado_m, msg = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages/{mid}?format=full",
                headers=cab(), timeout=gmail.TIMEOUT_MENSAGEM,
            )
            if estado_m != 200:
                continue
            anexos = gmail._anexos_da_mensagem(msg)
            print(f"[{mid}] anexos: {[(a['mime'], a['tamanho']) for a in anexos]}")
            for anexo in anexos:
                if guardados >= 12:
                    break
                if anexo["attachment_id"].startswith("parte:"):
                    continue
                estado_a, corpo_a = gmail.pedido_json(
                    f"{gmail.API_GMAIL}/me/messages/{mid}/attachments/{anexo['attachment_id']}",
                    headers=cab(), timeout=gmail.TIMEOUT_ANEXO,
                )
                if estado_a != 200 or "data" not in corpo_a:
                    print(f"    anexo recusado: estado {estado_a}")
                    continue
                dados = gmail._descodificar(str(corpo_a["data"]))
                destino = CANDIDATOS / f"{mid}_{guardados}.bin"
                destino.write_bytes(dados)
                print(f"    anexo {guardados}: {len(dados)} bytes magic={dados[:5]!r}")
                if dados[:5] == b"%PDF-":
                    try:
                        analisar_pdf(destino)
                    except Exception as exc:
                        print(f"    PDF ilegível: {type(exc).__name__}")
                guardados += 1
    print(f"\nficheiros guardados em {CANDIDATOS} (fora da app e do Git)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
