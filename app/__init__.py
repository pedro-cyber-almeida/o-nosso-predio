"""Aplicação Flask: O Nosso Prédio (protótipo local)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import click
from flask import Flask

from .db import close_db, init_db
from .security import csrf_protect, csrf_token

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
PRODUCTION_DB = "condominio.db"
DEMO_DB = "demo.db"


def _secret_key(instance_path: Path) -> str:
    """Chave de sessão local, gerada uma vez e guardada fora do Git."""
    from_env = os.environ.get("CONDOMINIO_SECRET_KEY")
    if from_env:
        return from_env
    instance_path.mkdir(parents=True, exist_ok=True)
    key_file = instance_path / "secret_key.txt"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    generated = secrets.token_hex(32)
    key_file.write_text(generated, encoding="utf-8")
    return generated


def database_path(instance_path: Path, use_demo: bool = False) -> Path:
    override = os.environ.get("CONDOMINIO_DATABASE")
    if override:
        return Path(override)
    return instance_path / (DEMO_DB if use_demo else PRODUCTION_DB)


def create_app(
    instance_path: Path | None = None,
    use_demo: bool = False,
    testing: bool = False,
    database: Path | None = None,
) -> Flask:
    instance_path = Path(instance_path) if instance_path else INSTANCE_DIR
    app = Flask(__name__, instance_path=str(instance_path))
    app.config.update(
        INSTANCE_PATH=str(instance_path),
        DATABASE=str(database) if database else str(database_path(instance_path, use_demo)),
        SECRET_KEY=_secret_key(instance_path),
        TESTING=testing,
        # Teto físico de um pedido: um ficheiro da biblioteca tem 10 MB de limite
        # (validado por rota), e o multipart acrescenta margem. Comprovativos e
        # restantes formulários têm limites próprios, mais baixos, no código.
        MAX_CONTENT_LENGTH=11 * 1024 * 1024,
        # Sessão só por cookie do navegador, inacessível a JavaScript e não
        # enviada a pedidos de outros sites. Em 127.0.0.1 não há HTTPS; a
        # preparação da instalação revê esta parte quando houver HTTPS.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    from .auth import bp as auth_bp
    from .automacao import bp as automacao_bp
    from .config import bp as config_bp
    from .dashboard import bp as dashboard_bp
    from .despesas import bp as despesas_bp
    from .documentos import bp as documentos_bp
    from .fractions import bp as fractions_bp
    from .gmail import bp as gmail_bp
    from .ia import bp as ia_bp
    from .integracoes import bp as integracoes_bp
    from .payments import bp as payments_bp
    from .portal import bp as portal_bp
    from .quotas import bp as quotas_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(fractions_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(quotas_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(despesas_bp)
    app.register_blueprint(integracoes_bp)
    app.register_blueprint(gmail_bp)
    app.register_blueprint(documentos_bp)
    app.register_blueprint(ia_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(automacao_bp)

    @app.before_request
    def _protect_forms() -> None:
        csrf_protect()

    @app.teardown_appcontext
    def _close(exception: Exception | None = None) -> None:
        close_db(exception)

    @app.context_processor
    def _inject() -> dict:
        from .auth import current_user

        return {"csrf_token": csrf_token, "utilizador_atual": current_user()}

    @app.errorhandler(413)
    def _limite_de_envio(error):
        """Envio acima do teto físico: mensagem clara, sem página técnica.

        Só formulários (POST) são encaminhados de volta com o aviso; um 413
        provocado por outra razão (por exemplo um anexo Gmail demasiado grande)
        responde com o 413 normal, como a rota pediu.
        """
        from flask import flash, redirect, request

        if request.method != "POST":
            return error
        flash(
            "O envio foi recusado: é maior do que a aplicação aceita "
            "(limite: 10 MB por ficheiro). Nada foi guardado.",
            "erro",
        )
        return redirect(request.referrer or "/")

    @app.cli.command("init-db")
    def _init_db_command() -> None:
        """Criar esquema e semente sem destruir dados existentes."""
        init_db()
        click.echo(f"Base de dados preparada em {app.config['DATABASE']}")

    return app
