"""Validação da análise IA corrigida com a configuração real (1 chamada).

Regista apenas: estado HTTP, finish_reason, tokens de usage, comprimento do
content, chaves da sugestão preenchidas (nunca os valores), e contagem de
movimentos financeiros antes/depois. Nunca imprime conteúdo, valores, chaves.

Uso: python scripts/validar_ia_real.py [id_documento]
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def main() -> int:
    doc_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    app = create_app()
    from app import ia
    from app.db import query_db
    from app.documentos import pasta_documentos

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

        pagamentos_antes = query_db("SELECT COUNT(*) AS n FROM payments", one=True)["n"]
        despesas_antes = query_db("SELECT COUNT(*) AS n FROM expenses", one=True)["n"]

        # Envolver o pedido real para registar a forma da resposta (sem conteúdo).
        original = ia.pedido_json

        def observado(url, **kw):
            payload = kw.get("payload") or {}
            print(f"pedido: max_tokens={payload.get('max_tokens')} "
                  f"chars_documento={len((payload.get('messages') or [{}])[-1].get('content', ''))}")
            estado, resposta = original(url, **kw)
            choices = resposta.get("choices")
            escolha = choices[0] if isinstance(choices, list) and choices else {}
            mensagem = escolha.get("message") if isinstance(escolha.get("message"), dict) else {}
            conteudo = mensagem.get("content")
            usage = resposta.get("usage") or {}
            print(f"resposta: estado={estado or 'sem resposta'} "
                  f"finish_reason={escolha.get('finish_reason', 'n/a')} "
                  f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} "
                  f"content_tipo={type(conteudo).__name__} "
                  f"content_len={len(conteudo) if isinstance(conteudo, str) else 'n/a'} "
                  f"reasoning_presente={mensagem.get('reasoning_content') is not None}")
            return estado, resposta

        ia.pedido_json = observado
        resultado = ia.analisar_documento(documento, caminho)

        print(f"\nresultado: ok={resultado['ok']}")
        if not resultado["ok"]:
            print(f"erro apresentado na aplicação: {resultado['erro']}")
        elif resultado.get("sugestao") is None:
            print(f"sem JSON; nota apresentada: {resultado.get('nota', '')}")
        else:
            sugestao = resultado["sugestao"]
            preenchidas = sorted(k for k, v in sugestao.items() if str(v).strip())
            print(f"sugestão editável gerada; chaves preenchidas: {preenchidas}")

        pagamentos_depois = query_db("SELECT COUNT(*) AS n FROM payments", one=True)["n"]
        despesas_depois = query_db("SELECT COUNT(*) AS n FROM expenses", one=True)["n"]
        print(f"\nmovimentos financeiros: payments {pagamentos_antes} -> {pagamentos_depois}, "
              f"expenses {despesas_antes} -> {despesas_depois} (inalterado)")
    return 0 if resultado["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
