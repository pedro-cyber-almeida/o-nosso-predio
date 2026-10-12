"""Reprodução focada: o que a página Email mostra quando o Gmail recusa (400).

Imprime apenas a secção de resultados do HTML (nunca tokens nem conteúdo de
email). Uso: python scripts/repro_email_400.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["CONDOMINIO_SECRET_KEY"] = "chave-de-repro-isolada"

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="repro_email_"))
    app = create_app(instance_path=tmp / "instance", database=tmp / "repro.db", testing=True)
    from app.db import init_db
    from app.integracoes import guardar_integracao
    from app.auth import PAPEL_ADMIN, criar_utilizador
    from app import gmail

    with app.app_context():
        init_db()
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
        criar_utilizador("repro", "palavra-de-repro-10", PAPEL_ADMIN, "Repro")

    def falso(url, *, metodo="GET", headers=None, payload=None, timeout=20):
        if "/messages?" in url:
            print(f"[pedido lista] {url}")
            return 400, {
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "Invalid field selection for users.messages.list.",
                }
            }
        return 404, {}

    gmail.pedido_json = falso

    cliente = app.test_client()
    import re

    r = cliente.get("/login")
    token_csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    cliente.post("/login", data={"csrf_token": token_csrf, "utilizador": "repro",
                                 "palavra_passe": "palavra-de-repro-10"})

    resposta = cliente.get("/email")
    html = resposta.get_data(as_text=True)
    inicio = html.find("Atualizar</a>")
    fim = html.find("</main>") if "</main>" in html else len(html)
    saida = tmp / "resultados.html"
    saida.write_text(
        f"[estado] {resposta.status_code}\n[pedido lista registado acima]\n"
        + html[inicio:fim],
        encoding="utf-8",
    )
    print(f"estado {resposta.status_code}; secção gravada em {saida}")


if __name__ == "__main__":
    main()
