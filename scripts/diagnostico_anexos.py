"""Diagnóstico da estrutura MIME de mensagens reais (só leitura, sem conteúdo).

Imprime apenas: id da mensagem e da conversa, caminho da parte, mimeType,
tamanho e se a parte tem attachmentId ou data. Nunca imprime corpo, nomes de
ficheiros, remetentes, tokens ou credenciais.

Uso: python scripts/diagnostico_anexos.py [id_da_mensagem ...]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def analisar(msg: dict, prefixo: str = "") -> list[str]:
    """Linhas de diagnóstico: caminho da parte, mime, tamanho, attachmentId/data."""
    linhas: list[str] = []
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return [f"{prefixo}raiz: SEM payload na resposta"]

    def tratar(parte, caminho: str):
        corpo = parte.get("body") or {}
        anexo = corpo.get("attachment") or {}
        mime = str(parte.get("mimeType", ""))
        tem_attachment = bool(anexo.get("attachmentId") or corpo.get("attachmentId"))
        tem_data = bool(corpo.get("data"))
        tamanho = int(anexo.get("size", 0) or corpo.get("size", 0) or 0)
        if tem_attachment or tem_data:
            linhas.append(
                f"{caminho}: mime={mime or '(vazio)'} attachmentId={tem_attachment} "
                f"data={tem_data} size={tamanho}"
            )
        for i, filho in enumerate(parte.get("parts") or []):
            tratar(filho, f"{caminho}/parts[{i}]")

    tratar({"mimeType": payload.get("mimeType"), "body": payload.get("body") or {},
            "parts": payload.get("parts")}, "payload")
    return linhas


def main() -> int:
    app = create_app()
    from app import gmail
    from app.integracoes import gmail_ligado

    alvos = sys.argv[1:]

    with app.app_context():
        if not gmail_ligado():
            print("Sem ligação Gmail.")
            return 1

        cabecalho = gmail._cabecalho_autorizacao()
        if cabecalho is None:
            print("Token não obtido.")
            return 1

        def cab():
            return {"Authorization": f"Bearer {cabecalho}"}

        if not alvos:
            estado, corpo = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages?{urlencode({'maxResults': 20, 'fields': 'messages(id,threadId)'})}",
                headers=cab(), timeout=gmail.TIMEOUT_LISTA,
            )
            if estado != 200:
                print("listagem recusada:", gmail._motivo_erro_gmail(corpo))
                return 1
            itens = list(corpo.get("messages", []))
            print(f"mensagens analisadas: {len(itens)} (primeira página)")
        else:
            itens = [{"id": i, "threadId": ""} for i in alvos]

        threads: dict[str, list[str]] = {}
        for item in itens:
            mid = str(item.get("id", ""))
            tid = str(item.get("threadId", ""))
            threads.setdefault(tid, []).append(mid)
            estado, msg = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages/{mid}?format=full",
                headers=cab(), timeout=gmail.TIMEOUT_MENSAGEM,
            )
            print(f"\n[mensagem {mid}] thread={tid} estado={estado}")
            if estado != 200:
                print("  recusa:", gmail._motivo_erro_gmail(msg) or "sem corpo error")
                continue
            linhas = analisar(msg)
            if not linhas:
                print("  sem partes com attachmentId ou data")
            for linha in linhas:
                print(f"  {linha}")
            time.sleep(0.1)

        multi = {t: m for t, m in threads.items() if len(m) > 1}
        if multi:
            print(f"\nconversas com várias mensagens: {len(multi)}")
            for tid, ids in multi.items():
                print(f"  conversa {tid}: {len(ids)} mensagens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
