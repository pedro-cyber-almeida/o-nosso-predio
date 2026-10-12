#!/usr/bin/env python3
"""Cópia de segurança consistente da base SQLite e dos documentos locais.

A base é copiada com a API de backup do SQLite (funciona mesmo com a aplicação
a correr e deixa o ficheiro de destino íntegro). Os documentos em
`instance/documentos` são copiados tal e qual. No fim a cópia é verificada
(`PRAGMA quick_check`) antes de o script dizer que está boa.

Uso normal (na pasta do projeto):
    .venv\\Scripts\\python.exe scripts\\backup_local.py
    .venv\\Scripts\\python.exe scripts\\backup_local.py --simular
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--base", type=Path, default=RAIZ / "instance" / "condominio.db",
                   help="ficheiro da base a copiar")
    p.add_argument("--documentos", type=Path, default=RAIZ / "instance" / "documentos",
                   help="pasta de documentos locais a copiar")
    p.add_argument("-o", "--dest", type=Path, default=RAIZ / "backups",
                   help="pasta de destino das cópias")
    p.add_argument("--simular", action="store_true",
                   help="mostra o que faria sem escrever nada")
    p.add_argument("-v", "--verboso", action="count", default=0,
                   help="repete para mais detalhe")
    return p.parse_args(argv)


def _copiar_base(origem: Path, destino: Path) -> None:
    """Cópia a frio consistente com a API de backup do SQLite."""
    origem_sqlite = sqlite3.connect(origem, timeout=30)
    destino_sqlite = sqlite3.connect(destino)
    try:
        origem_sqlite.backup(destino_sqlite)
    finally:
        destino_sqlite.close()
        origem_sqlite.close()


def _verificar_copia(destino: Path) -> tuple[bool, list[str]]:
    """quick_check + contagens das tabelas principais da cópia."""
    linhas: list[str] = []
    ligacao = sqlite3.connect(destino)
    try:
        ok = ligacao.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        versao = ligacao.execute("PRAGMA user_version").fetchone()[0]
        linhas.append(f"quick_check: {'ok' if ok else 'FALHOU'} · esquema v{versao}")
        for tabela in ("charges", "payments", "expenses", "expense_payments",
                       "documentos", "integracoes"):
            try:
                n = ligacao.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
                linhas.append(f"{tabela}: {n} linha(s)")
            except sqlite3.OperationalError:
                linhas.append(f"{tabela}: (tabela não existe nesta base)")
        return ok, linhas
    finally:
        ligacao.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verboso, 2)],
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if not args.base.exists():
        log.error("base não encontrada: %s", args.base)
        return 1

    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    pasta = args.dest / marca
    destino_base = pasta / args.base.name

    print(f"base:       {args.base}")
    print(f"documentos: {args.documentos if args.documentos.exists() else '(sem pasta de documentos)'}")
    print(f"destino:    {pasta}")
    if args.simular:
        print("(simulação: nada foi escrito)")
        return 0

    pasta.mkdir(parents=True, exist_ok=True)
    try:
        _copiar_base(args.base, destino_base)
    except sqlite3.Error as erro:
        log.error("backup da base falhou: %s", erro)
        return 1

    documentosCopiados = 0
    if args.documentos.exists():
        try:
            shutil.copytree(args.documentos, pasta / args.documentos.name)
            documentosCopiados = sum(1 for f in (pasta / args.documentos.name).iterdir() if f.is_file())
        except OSError as erro:
            log.error("cópia dos documentos falhou: %s", erro)
            return 1

    ok, linhas = _verificar_copia(destino_base)
    for linha in linhas:
        print(linha)
    print(f"documentos copiados: {documentosCopiados}")

    if not ok:
        log.error("a cópia não passou na verificação; o destino fica no sítio para inspeção")
        return 1

    print(f"cópia verificada em {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
