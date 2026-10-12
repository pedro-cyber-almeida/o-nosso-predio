"""Proteção CSRF por token de sessão, sem dependências externas."""

from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_protect() -> None:
    if request.method not in UNSAFE_METHODS:
        return
    expected = session.get("_csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not provided or not hmac.compare_digest(str(provided), str(expected)):
        abort(400, "Token CSRF inválido ou em falta. Recarrega a página e tenta de novo.")
