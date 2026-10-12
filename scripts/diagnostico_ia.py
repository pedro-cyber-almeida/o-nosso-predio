"""Diagnóstico da análise IA de um documento (só leitura; 1 chamada controlada).

Regista apenas: mime, nº de páginas, nº de caracteres extraídos, estado HTTP,
finish_reason, tipo/comprimento do conteúdo, presença de reasoning_content e
tokens de usage. Nunca imprime conteúdo do documento, valores, chaves, tokens.

Uso: python scripts/diagnostico_ia.py [id_documento] [--sem-api]
"""

from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sem_api = "--sem-api" in sys.argv
    doc_id = int(args[0]) if args else 2

    app = create_app()
    from app import ia
    from app.db import query_db
    from app.documentos import pasta_documentos
    from app.integracoes import ia_ativa, ia_timeout, obter_integracao

    with app.app_context():
        linha = query_db("SELECT * FROM documentos WHERE id = ?", (doc_id,), one=True)
        if linha is None:
            print(f"documento {doc_id}: não existe na base")
            return 1
        documento = dict(linha)
        caminho = pasta_documentos() / documento["nome_interno"]
        if not caminho.exists():
            print(f"documento {doc_id}: ficheiro em falta")
            return 1
        print(f"documento {doc_id}: mime={documento['mime']} "
              f"ficheiro={caminho.stat().st_size} bytes")

        # 1) Extração local (sem API): só páginas e quantidade, nunca conteúdo.
        texto, motivo = ia.extrair_texto(documento, caminho)
        if texto is None:
            print(f"extração: sem texto útil — {motivo}")
            return 0
        paginas = "n/a"
        if documento["mime"] == "application/pdf":
            from pypdf import PdfReader

            paginas = len(PdfReader(io.BytesIO(caminho.read_bytes())).pages)
        enviado = min(len(texto), ia.MAX_CONTEUDO_CHARS)
        print(f"extração: útil — páginas={paginas} caracteres={len(texto)} "
              f"enviado à API={enviado} (limite {ia.MAX_CONTEUDO_CHARS})")

        print(f"config IA: ativa={ia_ativa()} base_url={obter_integracao('ia_base_url')} "
              f"modelo={obter_integracao('ia_modelo')} timeout={ia_timeout()}s "
              f"chave={'sim' if obter_integracao('ia_chave_api') else 'não'}")
        if sem_api:
            return 0

        # 2) Reprodução controlada do pedido ORIGINAL (max_tokens=500) — UMA chamada.
        base_url = obter_integracao("ia_base_url").rstrip("/")
        cabecalhos = {"Content-Type": "application/json"}
        chave = obter_integracao("ia_chave_api")
        if chave:
            cabecalhos["Authorization"] = f"Bearer {chave}"
        corpo = texto[:ia.MAX_CONTEUDO_CHARS]
        estado, resposta = ia.pedido_json(
            f"{base_url}/chat/completions",
            headers=cabecalhos,
            payload={
                "model": obter_integracao("ia_modelo"),
                "temperature": 0,
                "max_tokens": 500,
                "messages": [
                    {"role": "system", "content": ia.AVISO_SISTEMA},
                    {"role": "user", "content": f"<documento>\n{corpo}\n</documento>"},
                ],
            },
            timeout=ia_timeout(),
        )
        print(f"\nestado HTTP: {estado or 'sem resposta'}")
        usage = resposta.get("usage") or {}
        if usage:
            print(f"usage: prompt={usage.get('prompt_tokens')} "
                  f"completion={usage.get('completion_tokens')}")
        escolha = {}
        choices = resposta.get("choices")
        if isinstance(choices, list) and choices:
            escolha = choices[0] if isinstance(choices[0], dict) else {}
        mensagem = escolha.get("message") if isinstance(escolha.get("message"), dict) else {}
        print(f"message keys: {sorted(mensagem.keys()) or '(sem message)'}")
        print(f"finish_reason: {escolha.get('finish_reason', 'n/a')}")
        conteudo = mensagem.get("content")
        print(f"content: tipo={type(conteudo).__name__} "
              f"comprimento={len(conteudo) if isinstance(conteudo, str) else 'n/a'}")
        rc = mensagem.get("reasoning_content")
        print(f"reasoning_content presente: {rc is not None} "
              f"comprimento={len(rc) if isinstance(rc, str) else 'n/a'}")

        # 3) Executar o parsing EXATO do código atual para capturar o traceback do 500.
        try:
            c = resposta["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            print(f"parsing atual: {type(exc).__name__} na extração do content (tratado)")
            return 0
        sugestao = ia._ler_json(c)
        if sugestao is None:
            try:
                resumo = c[:1500]  # linha do fluxo atual que rebenta se content for None
                print(f"parsing atual: sem exceção; sugestao=None; resumo_len={len(resumo)}")
            except Exception:
                print("TRACEBACK EXATO do 500 (fluxo atual):")
                traceback.print_exc()
                return 2
        else:
            print(f"parsing atual: JSON válido; chaves: {sorted(sugestao.keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
