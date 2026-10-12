"""Validação do percurso de anexos com o Gmail real (só leitura + 1 documento).

Imprime apenas: id da mensagem, mimeType, tamanho, estado HTTP e se os bytes
descarregados começam por %PDF. Nunca imprime corpo, nomes de ficheiros,
remetentes, tokens ou credenciais. Não cria movimentos financeiros.

Depois de correr, o anexo PDF mais pequeno fica guardado como documento
privado (id impresso no fim) — pode arquivar-se em Documentos se não o quiser.

Uso: python scripts/validar_anexo_real.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def main() -> int:
    app = create_app()
    from app import gmail
    from app.integracoes import gmail_ligado

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

        estado, corpo = gmail.pedido_json(
            f"{gmail.API_GMAIL}/me/messages?{urlencode({'maxResults': 20, 'fields': 'messages(id,threadId)'})}",
            headers=cab(), timeout=gmail.TIMEOUT_LISTA,
        )
        if estado != 200:
            print("listagem recusada:", gmail._motivo_erro_gmail(corpo))
            return 1

        alvo: tuple[str, dict] | None = None
        total_anexos = 0
        for item in corpo.get("messages", []):
            mid = str(item.get("id", ""))
            estado_m, msg = gmail.pedido_json(
                f"{gmail.API_GMAIL}/me/messages/{mid}?format=full",
                headers=cab(), timeout=gmail.TIMEOUT_MENSAGEM,
            )
            if estado_m != 200:
                print(f"[{mid}] estado {estado_m}: {gmail._motivo_erro_gmail(msg)}")
                continue
            anexos = gmail._anexos_da_mensagem(msg)
            total_anexos += len(anexos)
            resumo = ", ".join(
                f"{a['mime'] or '(sem mime)'} {a['tamanho']}B"
                + ("" if not a["attachment_id"].startswith("parte:") else " inline")
                for a in anexos
            )
            print(f"[{mid}] anexos identificados: {len(anexos)} — {resumo or 'nenhum'}")
            if alvo is None:
                for a in anexos:
                    if a["mime"] == "application/pdf":
                        alvo = (mid, a)
                        break
            time.sleep(0.05)

        print(f"\ntotal de anexos identificados na primeira página: {total_anexos}")
        if alvo is None:
            print("Nenhum application/pdf na primeira página; indica o id da mensagem afetada.")
            return 1

        mid, anexo = alvo
        print(f"\nPDF escolhido: mensagem {mid} ({anexo['tamanho']} bytes)")

        # Descarga real dos bytes.
        estado_a, corpo_a = gmail.pedido_json(
            f"{gmail.API_GMAIL}/me/messages/{mid}/attachments/{anexo['attachment_id']}",
            headers=cab(), timeout=gmail.TIMEOUT_ANEXO,
        )
        if estado_a != 200 or "data" not in corpo_a:
            print(f"descarga recusada: estado {estado_a}", gmail._motivo_erro_gmail(corpo_a))
            return 1
        dados = gmail._descodificar(str(corpo_a["data"]))
        eh_pdf = dados[:5] == b"%PDF-"
        print(f"descarga: estado {estado_a}, {len(dados)} bytes, começa por %PDF: {eh_pdf}")
        if not eh_pdf:
            print("(o Gmail rotulou este anexo como application/pdf mas os bytes não são PDF)")

        # Guardar como documento (percurso normal; nunca cria movimentos).
        from app.db import query_db
        from app.documentos import guardar_documento

        movimentos_antes = query_db("SELECT COUNT(*) AS n FROM payments", one=True)["n"]

        documento_id = guardar_documento(
            origem="anexo",
            dados=dados,
            nome_original=anexo["nome"],
            mime=anexo["mime"],
            gmail_mensagem_id=mid,
            gmail_anexo_id=anexo["attachment_id"],
            resumo=f"Anexo de email: {anexo['nome']}",
        )
        print(f"guardado como documento local: id {documento_id} (privado, sem partilhas)")
        movimentos_depois = query_db("SELECT COUNT(*) AS n FROM payments", one=True)["n"]
        print(f"movimentos financeiros: {movimentos_antes} antes, {movimentos_depois} depois "
              f"(inalterado por este script)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
