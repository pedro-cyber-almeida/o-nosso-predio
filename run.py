"""Ponto de entrada: arrancar, inicializar a base ou criar a base de demonstração.

Comandos:
    python run.py                 -> servir em 127.0.0.1:5000
    python run.py initdb          -> criar esquema e semente sem destruir dados
    python run.py demo            -> criar instance/demo.db com dados fictícios
    python run.py serve --demo    -> servir usando a base de demonstração
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from app import INSTANCE_DIR, create_app
from app.db import init_db
from app.demo import create_demo_database


def _preparar_base(app) -> str | None:
    """Inicializar a base e devolver a mensagem de erro, quando houver uma."""
    try:
        with app.app_context():
            init_db()
    except sqlite3.OperationalError as erro:
        return (
            f"Não foi possível preparar a base ({erro}). "
            "Se outro processo estiver a usar a mesma base, fecha-o e tenta de novo."
        )
    except RuntimeError as erro:
        return f"Não foi possível preparar a base: {erro}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="O Nosso Prédio — protótipo local")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="servir a aplicação em 127.0.0.1")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("CONDOMINIO_PORT", "5000")))
    serve.add_argument("--demo", action="store_true", help="usar a base de demonstração")
    serve.add_argument("--debug", action="store_true")

    sub.add_parser("initdb", help="criar esquema e semente sem destruir dados existentes")

    demo = sub.add_parser("demo", help="criar a base de demonstração separada")
    demo.add_argument("--path", default=str(INSTANCE_DIR / "demo.db"))

    sub.add_parser(
        "processar-bancario",
        help="correr um ciclo da automatização bancária (para agendamento; só local)",
    )

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "initdb":
        app = create_app()
        erro = _preparar_base(app)
        if erro is not None:
            print(erro)
            return 1
        print(f"Base preparada em {app.config['DATABASE']}")
        return 0

    if command == "demo":
        sucesso, mensagem = create_demo_database(Path(args.path))
        print(mensagem)
        return 0 if sucesso else 1

    if command == "processar-bancario":
        from app.automacao import correr_ciclo_sync

        app = create_app()
        erro = _preparar_base(app)
        if erro is not None:
            print(erro)
            return 1
        resumo = correr_ciclo_sync(app, gatilho="agendado")
        print("Ciclo bancário:", json.dumps(resumo, ensure_ascii=False))
        return 0

    if args.host not in ("127.0.0.1", "localhost"):
        print("O protótipo só serve em 127.0.0.1 ou localhost.")
        return 1

    app = create_app(use_demo=args.demo)
    erro = _preparar_base(app)
    if erro is not None:
        print(erro)
        return 1
    print(f"O Nosso Prédio em http://{args.host}:{args.port} (base: {app.config['DATABASE']})")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
