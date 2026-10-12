"""Medição da página Email: token, listagem, detalhes e renderização, separados.

Não mede com o Gmail real (sem credenciais): substitui o transporte por um Gmail
falso com latência fixa por pedido (0,30 s), como uma rede lenta faria. Não regista
tokens, credenciais nem conteúdo real — só tempos e contagens.

Uso: python scripts/medir_email.py [latencia_segundos]
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

LATENCIA = float(sys.argv[1]) if len(sys.argv) > 1 else 0.30
N_MENSAGENS = 25  # o que a lista pedia por página antes da correção

os.environ["CONDOMINIO_SECRET_KEY"] = "chave-de-medicao-isolada"

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402


def _urlsafe(dados: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(dados).decode("ascii").rstrip("=")


def montar_respostas():
    lista = {
        # Contrato real da listagem: só id + threadId por mensagem.
        "messages": [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(1, N_MENSAGENS + 1)],
        "nextPageToken": "pagina-2",
        "resultSizeEstimate": N_MENSAGENS + 1,
    }
    rotas = {
        "/profile": (200, {"emailAddress": "condominio@example.com"}),
        "/token": (200, {"access_token": "access-renovado", "expires_in": 3600}),
        "/messages?maxResults=": (200, lista),
    }
    corpo = _urlsafe("<p>corpo de ensaio</p>".encode("utf-8"))
    for i in range(1, N_MENSAGENS + 1):
        rotas[f"/messages/m{i}?format=metadata"] = (
            200,
            {
                # Contrato real do messages.get com format=metadata: só cabeçalhos.
                "id": f"m{i}",
                "payload": {"headers": [
                    {"name": "From", "value": f"rem{i}@example.com"},
                    {"name": "Subject", "value": f"Assunto {i}"},
                    {"name": "Date", "value": "10/01/2027"},
                ]},
            },
        )
        rotas[f"/messages/m{i}?format=full"] = (
            200,
            {
                "id": f"m{i}",
                "threadId": "m1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "rem1@example.com"},
                        {"name": "Subject", "value": "Assunto 1"},
                        {"name": "Date", "value": "10/01/2027"},
                    ],
                    "parts": [{"mimeType": "text/html", "body": {"data": corpo}}],
                },
            },
        )
    return rotas


class GmailMedido:
    """Gmail falso com latência fixa; regista (tipo, duração) de cada pedido."""

    def __init__(self, rotas):
        self.rotas = rotas
        self.registo: list[tuple[str, float]] = []

    def __call__(self, url, *, metodo="GET", headers=None, payload=None, timeout=20):
        inicio = time.perf_counter()
        time.sleep(LATENCIA)  # latência simulada da rede
        for chave, resposta in self.rotas.items():
            if chave in url:
                duracao = time.perf_counter() - inicio
                estado, corpo = resposta
                if "/me/messages?" in url and isinstance(corpo, dict) and "messages" in corpo:
                    # Como o Gmail real: devolve só até maxResults.
                    from urllib.parse import parse_qs, urlparse

                    max_resultados = int(parse_qs(urlparse(url).query).get("maxResults", ["20"])[0])
                    corpo = {**corpo, "messages": corpo["messages"][:max_resultados]}
                    resposta = (estado, corpo)
                tipo = "token" if "/token" in url else (
                    "lista" if "/messages?" in url else (
                        "detalhe-metadata" if "format=metadata" in url else (
                            "detalhe-full" if "format=full" in url else "outro"
                        )
                    )
                )
                self.registo.append((tipo, duracao))
                return resposta
        self.registo.append(("404", time.perf_counter() - inicio))
        return 404, {}

    def resumo(self, desde=0):
        tempos: dict[str, list[float]] = {}
        for tipo, duracao in self.registo[desde:]:
            tempos.setdefault(tipo, []).append(duracao)
        saida = []
        for tipo, valores in tempos.items():
            total = sum(valores)
            saida.append(f"{tipo}: {len(valores)} pedido(s), {total:.2f} s no total")
        return "; ".join(saida) if saida else "sem pedidos"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="medir_email_"))
    app = create_app(instance_path=tmp / "instance", database=tmp / "medir.db", testing=True)
    from app.db import init_db
    from app.integracoes import guardar_integracao
    from app import gmail

    with app.app_context():
        init_db()
        guardar_integracao("gmail_client_id", "cliente")
        guardar_integracao("gmail_client_secret", "segredo")
        guardar_integracao("gmail_refresh_token", "refresh")
        guardar_integracao("gmail_conta", "condominio@example.com")
        from app.auth import PAPEL_ADMIN, criar_utilizador

        criar_utilizador("medicao", "palavra-de-medicao-10", PAPEL_ADMIN, "Medição")

    falso = GmailMedido(montar_respostas())
    gmail.pedido_json = falso

    # Entrar como administrador (o acesso ao Email é só de administrador).
    cliente = app.test_client()
    r = cliente.get("/login")
    import re as _re

    token_csrf = _re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    cliente.post(
        "/login",
        data={"csrf_token": token_csrf, "utilizador": "medicao", "palavra_passe": "palavra-de-medicao-10"},
    )

    # --- token válido guardado: renovação deve ser evitada -------------------
    with app.app_context():
        guardar_integracao("gmail_access_token", "access-valido")
        guardar_integracao("gmail_access_token_expira", str(int(time.time()) + 3600))

    print(f"latência simulada por pedido: {LATENCIA:.2f} s")

    # 1) Primeira abertura da lista (token válido, sem cache)
    marca = len(falso.registo)
    inicio = time.perf_counter()
    r1 = cliente.get("/email"); r1.get_data()
    t1 = time.perf_counter() - inicio
    print(f"[lista 1.ª abertura] {t1:.2f} s ({r1.status_code}) — {falso.resumo(marca)}")

    # 2) Repetição imediata da mesma lista
    marca = len(falso.registo)
    inicio = time.perf_counter()
    r2 = cliente.get("/email"); r2.get_data()
    t2 = time.perf_counter() - inicio
    print(f"[lista repetição ] {t2:.2f} s ({r2.status_code}) — {falso.resumo(marca)}")

    # 3) Abrir uma mensagem (detalhe completo + conversa)
    marca = len(falso.registo)
    inicio = time.perf_counter()
    r3 = cliente.get("/email/m1"); r3.get_data()
    t3 = time.perf_counter() - inicio
    print(f"[mensagem m1      ] {t3:.2f} s ({r3.status_code}) — {falso.resumo(marca)}")

    # 4) Token expirado: renovação antes da lista
    with app.app_context():
        guardar_integracao("gmail_access_token_expira", "1")
    marca = len(falso.registo)
    inicio = time.perf_counter()
    r4 = cliente.get("/email"); r4.get_data()
    t4 = time.perf_counter() - inicio
    print(f"[lista c/ renovação] {t4:.2f} s ({r4.status_code}) — {falso.resumo(marca)}")

    # 5) Abrir anexo (metadado da mensagem repedido?)
    falso.rotas["/messages/m1/attachments/anexo1"] = (200, {"data": _urlsafe(b"%PDF-1.4 ensaio")})
    marca = len(falso.registo)
    inicio = time.perf_counter()
    r5 = cliente.get("/email/m1/anexo/anexo1"); r5.get_data()
    t5 = time.perf_counter() - inicio
    print(f"[anexo de m1      ] {t5:.2f} s ({r5.status_code}) — {falso.resumo(marca)}")


if __name__ == "__main__":
    main()
