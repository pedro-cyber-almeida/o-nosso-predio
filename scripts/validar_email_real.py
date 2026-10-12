"""Validação da listagem Email com a ligação Gmail real (só leitura).

Não imprime assuntos, remetentes, excertos, tokens nem credenciais — só
estados, contagens, nomes de campos e tempos. Não cria movimentos financeiros.

Uso: python scripts/validar_email_real.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from urllib.parse import urlencode  # noqa: E402

from app import create_app  # noqa: E402


def main() -> int:
    app = create_app()  # base real (instance/condominio.db)
    from app import gmail
    from app.integracoes import gmail_ligado

    with app.app_context():
        if not gmail_ligado():
            print("Sem ligação Gmail na base real: usa 「Testar ligação」 em "
                  "Definições → Integrações e volta a correr este script.")
            return 1

        # 1) Token: reutilização ou renovação (comportamento normal da app).
        t0 = time.perf_counter()
        cabecalho = gmail._cabecalho_autorizacao()
        t_token = time.perf_counter() - t0
        if cabecalho is None:
            print("[token] falhou: a ligação precisa de ser reautorizada.")
            return 1
        print(f"[token] ok em {t_token:.2f} s")

        # 2) Listagem: exatamente o que a página pede agora.
        parametros = {
            "maxResults": gmail.MENSAGENS_POR_PAGINA,
            "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate",
        }
        t0 = time.perf_counter()
        estado, corpo = gmail.pedido_json(
            f"{gmail.API_GMAIL}/me/messages?{urlencode(parametros)}",
            headers={"Authorization": f"Bearer {cabecalho}"},
            timeout=gmail.TIMEOUT_LISTA,
        )
        t_lista = time.perf_counter() - t0
        itens = list(corpo.get("messages", []))
        print(f"[listagem] estado {estado} em {t_lista:.2f} s — {len(itens)} mensagens")
        if estado != 200:
            print("[motivo estruturado]", gmail._motivo_erro_gmail(corpo) or "sem corpo error")
            return 1
        # Contrato: nomes de campos por mensagem (nunca valores).
        nomes = sorted({chave for item in itens for chave in item})
        print("[campos por mensagem]", nomes)

        # 3) Detalhes: só os cabeçalhos pedidos, em série para medir com clareza.
        t0 = time.perf_counter()
        ok = 0
        nomes_cabecalhos: set[str] = set()
        for item in itens[:5]:
            estado_msg, msg = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages/{item['id']}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
                headers={"Authorization": f"Bearer {cabecalho}"},
                timeout=gmail.TIMEOUT_METADATA,
            )
            if estado_msg == 200:
                ok += 1
                nomes_cabecalhos.update(
                    str(h.get("name", "")) for h in msg.get("payload", {}).get("headers", [])
                )
        t_detalhes = time.perf_counter() - t0
        print(f"[detalhes] {ok}/{min(5, len(itens))} ok em {t_detalhes:.2f} s — "
              f"cabeçalhos recebidos: {sorted(nomes_cabecalhos)}")
        print("Só pedidos de leitura: nenhum movimento financeiro criado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
