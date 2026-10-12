"""Autenticação, perfis e gestão de utilizadores (entrega 1).

Regras aplicadas:
- Dois papéis: `administrador` e `condomino`. Sem registo público.
- O primeiro administrador é criado num percurso protegido, disponível apenas
  enquanto não existe nenhum utilizador, e sem palavra-passe predefinida.
- A proteção é no servidor: cada blueprint verifica o pedido, não basta esconder
  botões na interface.
- Palavras-passe guardadas como hash (scrypt, via Werkzeug). Nunca são
  apresentadas nem registadas em lado nenhum.
- Um utilizador não é apagado: é desativado. O rasto de correções dos pagamentos
  guarda o nome de quem corrigiu e esse rasto não se reescreve.
- O último administrador ativo não pode ser desativado nem mudar de papel:
  sem administrador a aplicação ficaria sem quem a gerisse.
- Associar utilizadores a frações é administrativo: várias frações por
  utilizador, vários utilizadores por fração.
"""

from __future__ import annotations

import re
from functools import wraps

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, query_db, utc_now_iso
from .units import parse_int_id

bp = Blueprint("auth", __name__)

PAPEL_ADMIN = "administrador"
PAPEL_CONDOMINO = "condomino"
PAPEIS = (PAPEL_ADMIN, PAPEL_CONDOMINO)
ROTULOS_PAPEL = {PAPEL_ADMIN: "Administrador", PAPEL_CONDOMINO: "Condómino"}

NOME_UTILIZADOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
PALAVRA_PASSE_MINIMA = 10
LIMITE_NOME_VISIVEL = 80


# ---------------------------------------------------------------------------
# Sessão e permissões
# ---------------------------------------------------------------------------


def current_user():
    """Utilizador da sessão atual, ou None. Uma leitura por pedido."""
    if "utilizador" not in g:
        user_id = session.get("user_id")
        g.utilizador = None
        if user_id is not None:
            g.utilizador = query_db(
                "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,), one=True
            )
            if g.utilizador is None:
                # A sessão aponta para um registo que deixou de ser válido:
                # não se mantém uma sessão órfã.
                session.pop("user_id", None)
    return g.utilizador


def eh_administrador() -> bool:
    utilizador = current_user()
    return utilizador is not None and utilizador["role"] == PAPEL_ADMIN


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        recusa = recusar_se_nao_for_administrador()
        if recusa is not None:
            return recusa
        return view(*args, **kwargs)

    return wrapped


def recusar_se_nao_for_administrador():
    """Resposta de recusa para rotas de administração, ou None se o acesso vale.

    Usado também como `before_request` dos blueprints de gestão: a proteção
    acontece antes de qualquer leitura ou escrita da rota.
    """
    utilizador = current_user()
    if utilizador is None:
        return redirect(url_for("auth.login"))
    if utilizador["role"] != PAPEL_ADMIN:
        flash(
            "Esta página pertence à administração do condomínio. O teu perfil "
            "consulta as tuas frações no painel.",
            "erro",
        )
        return redirect(url_for("dashboard.painel"))
    return None


def fracao_do_utilizador(user_id: int, fraction_id: int) -> bool:
    return (
        query_db(
            "SELECT 1 FROM user_fractions WHERE user_id = ? AND fraction_id = ? LIMIT 1",
            (user_id, fraction_id),
            one=True,
        )
        is not None
    )


def fracoes_do_utilizador(user_id: int) -> list:
    return query_db(
        """
        SELECT f.id, f.code, f.permilagem, f.active
        FROM user_fractions AS uf
        JOIN fractions AS f ON f.id = uf.fraction_id
        WHERE uf.user_id = ?
        ORDER BY f.code
        """,
        (user_id,),
    )


# ---------------------------------------------------------------------------
# Criar e alterar utilizadores
# ---------------------------------------------------------------------------


def _validar_nome(username: str) -> str | None:
    if not NOME_UTILIZADOR_RE.match(username):
        return (
            "Nome de utilizador: usa 3 a 32 caracteres, começando por letra ou "
            "número, só minúsculas, números, ponto, hífen e sublinhado."
        )
    return None


def _validar_palavra_passe(palavra: str) -> str | None:
    if len(palavra) < PALAVRA_PASSE_MINIMA:
        return (
            f"Palavra-passe: no mínimo {PALAVRA_PASSE_MINIMA} caracteres. "
            "Não há palavra-passe predefinida: escreve uma só para este registo."
        )
    return None


def criar_utilizador(
    username: str,
    palavra_passe: str,
    papel: str,
    nome_visivel: str = "",
    fraction_ids: tuple[int, ...] = (),
) -> tuple[int | None, list[str]]:
    """Criar um utilizador e as suas associações, numa única transação.

    Devolve (id, erros). Uma falha a meio não deixa utilizador sem associações
    nem associações sem utilizador.
    """
    erros: list[str] = []
    username = (username or "").strip().lower()
    nome_visivel = (nome_visivel or "").strip()[:LIMITE_NOME_VISIVEL]

    erro = _validar_nome(username)
    if erro:
        erros.append(erro)
    erro = _validar_palavra_passe(palavra_passe)
    if erro:
        erros.append(erro)
    if papel not in PAPEIS:
        erros.append("Papel inválido: só administrador ou condómino.")

    ids_validos: list[int] = []
    for fraction_id in fraction_ids:
        fracao = query_db(
            "SELECT id, code FROM fractions WHERE id = ?", (fraction_id,), one=True
        )
        if fracao is None:
            erros.append(f"Fração {fraction_id}: não existe.")
        else:
            ids_validos.append(int(fracao["id"]))

    if erros:
        return None, erros

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existe = query_db("SELECT 1 FROM users WHERE username = ? LIMIT 1", (username,), one=True)
        if existe is not None:
            connection.rollback()
            return None, [
                f"O nome de utilizador «{username}» já existe. Escolhe outro; "
                "nomes não são reutilizados nem apagados."
            ]
        agora = utc_now_iso()
        cursor = connection.execute(
            """
            INSERT INTO users (username, password_hash, display_name, role, active,
                               created_at, updated_at, password_changed_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                username,
                generate_password_hash(palavra_passe),
                nome_visivel,
                papel,
                agora,
                agora,
                agora,
            ),
        )
        user_id = int(cursor.lastrowid)
        for fraction_id in ids_validos:
            connection.execute(
                "INSERT INTO user_fractions (user_id, fraction_id, created_at) VALUES (?, ?, ?)",
                (user_id, fraction_id, agora),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return user_id, []


def _administradores_ativos() -> int:
    linha = query_db(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'administrador' AND active = 1", one=True
    )
    return int(linha["n"])


def _apagar_associoes(connection, user_id: int) -> None:
    connection.execute("DELETE FROM user_fractions WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Páginas públicas: entrada e primeiro administrador
# ---------------------------------------------------------------------------


def _ha_utilizadores() -> bool:
    linha = query_db("SELECT COUNT(*) AS n FROM users", one=True)
    return int(linha["n"]) > 0


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not _ha_utilizadores():
        return redirect(url_for("auth.setup"))

    utilizador = current_user()
    if utilizador is not None:
        return redirect(url_for("dashboard.painel"))

    if request.method == "POST":
        username = (request.form.get("utilizador", "") or "").strip().lower()
        palavra_passe = request.form.get("palavra_passe", "") or ""
        registo = query_db("SELECT * FROM users WHERE username = ?", (username,), one=True)

        # Mensagem única: não se revela se o nome existe nem se está desativado.
        if registo is None or not registo["active"] or not check_password_hash(
            registo["password_hash"], palavra_passe
        ):
            flash(
                "Utilizador ou palavra-passe inválidos. Se perdeste a palavra-passe, "
                "pede ao administrador para a definir de novo.",
                "erro",
            )
            return render_template("login.html", utilizador_pedido=username)

        session.clear()  # sessão nova: a identidade não herda marcas anteriores
        session["user_id"] = int(registo["id"])
        return redirect(url_for("dashboard.painel"))

    return render_template("login.html", utilizador_pedido="")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Criar o primeiro administrador. Disponível só enquanto não há utilizadores."""
    if _ha_utilizadores():
        flash(
            "Já existem utilizadores: a criação do primeiro administrador fechou. "
            "Inicia sessão ou pede ao administrador para criar novos acessos.",
            "erro",
        )
        return redirect(url_for("auth.login"))

    erros: list[str] = []
    valores = {"utilizador": "", "nome": ""}
    if request.method == "POST":
        username = (request.form.get("utilizador", "") or "").strip().lower()
        nome_visivel = (request.form.get("nome", "") or "").strip()
        palavra = request.form.get("palavra_passe", "") or ""
        confirmacao = request.form.get("confirmar_palavra_passe", "") or ""
        valores = {"utilizador": username, "nome": nome_visivel}

        erro = _validar_nome(username)
        if erro:
            erros.append(erro)
        erro = _validar_palavra_passe(palavra)
        if erro:
            erros.append(erro)
        if palavra != confirmacao:
            erros.append("A confirmação não coincide com a palavra-passe. Escreve as duas iguais.")

        if not erros:
            user_id, erros = criar_utilizador(username, palavra, PAPEL_ADMIN, nome_visivel)
            if not erros:
                session.clear()
                session["user_id"] = user_id
                flash(
                    f"Primeiro administrador criado: {username}. Não havia palavra-passe "
                    "predefinida e não foi inventada nenhuma identidade. O painel indica "
                    "agora os primeiros passos: criar frações, registar regras de quota e "
                    "completar a configuração do condomínio.",
                    "sucesso",
                )
                return redirect(url_for("dashboard.painel"))

        for mensagem in erros:
            flash(mensagem, "erro")

    return render_template("setup.html", valores=valores)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Sessão terminada.", "sucesso")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Conta própria
# ---------------------------------------------------------------------------


@bp.route("/conta/palavra-passe", methods=["GET", "POST"])
def alterar_palavra_passe():
    utilizador = current_user()
    if utilizador is None:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        atual = request.form.get("palavra_passe_atual", "") or ""
        nova = request.form.get("palavra_passe_nova", "") or ""
        confirmacao = request.form.get("confirmar_palavra_passe", "") or ""

        erros: list[str] = []
        if not check_password_hash(utilizador["password_hash"], atual):
            erros.append("A palavra-passe atual não está certa: nada foi alterado.")
        erro = _validar_palavra_passe(nova)
        if erro:
            erros.append(erro)
        if nova != confirmacao:
            erros.append("A confirmação não coincide com a nova palavra-passe.")

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
            return render_template("conta_palavra_passe.html")

        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ?, updated_at = ? "
                "WHERE id = ? AND active = 1",
                (generate_password_hash(nova), utc_now_iso(), utc_now_iso(), utilizador["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                flash("Este registo de utilizador deixou de estar ativo: nada foi alterado.", "erro")
                return redirect(url_for("dashboard.painel"))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        flash("Palavra-passe alterada. Na próxima entrada usa a nova.", "sucesso")
        return redirect(url_for("dashboard.painel"))

    return render_template("conta_palavra_passe.html")


# ---------------------------------------------------------------------------
# Administração de utilizadores
# ---------------------------------------------------------------------------


def _utilizadores_para_lista() -> list[dict]:
    utilizadores = []
    for row in query_db("SELECT * FROM users ORDER BY username"):
        fracoes = fracoes_do_utilizador(int(row["id"]))
        utilizadores.append(
            {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"] or "",
                "role": row["role"],
                "role_texto": ROTULOS_PAPEL[row["role"]],
                "active": bool(row["active"]),
                "created_texto": str(row["created_at"])[:10],
                "password_changed_texto": (
                    str(row["password_changed_at"])[:10] if row["password_changed_at"] else ""
                ),
                "fracoes": [fracao["code"] for fracao in fracoes],
            }
        )
    return utilizadores


@bp.route("/utilizadores")
def users_list():
    recusa = recusar_se_nao_for_administrador()
    if recusa is not None:
        return recusa
    return render_template(
        "users_list.html",
        utilizadores=_utilizadores_para_lista(),
        administradores_ativos=_administradores_ativos(),
    )


def _ler_fracoes_do_form(form) -> list[int]:
    ids: list[int] = []
    for campo, valor in form.items():
        if not campo.startswith("fracao_") or valor != "on":
            continue
        fraction_id = parse_int_id(campo.removeprefix("fracao_"))
        if fraction_id is not None and fraction_id not in ids:
            ids.append(fraction_id)
    return ids


@bp.route("/utilizadores/novo", methods=["GET", "POST"])
def new_user():
    recusa = recusar_se_nao_for_administrador()
    if recusa is not None:
        return recusa

    valores = {"utilizador": "", "nome": "", "papel": PAPEL_CONDOMINO}
    if request.method == "POST":
        username = (request.form.get("utilizador", "") or "").strip().lower()
        nome_visivel = (request.form.get("nome", "") or "").strip()
        papel = request.form.get("papel", PAPEL_CONDOMINO)
        palavra = request.form.get("palavra_passe", "") or ""
        confirmacao = request.form.get("confirmar_palavra_passe", "") or ""
        fraction_ids = _ler_fracoes_do_form(request.form)
        valores = {"utilizador": username, "nome": nome_visivel, "papel": papel}

        erros: list[str] = []
        if papel not in PAPEIS:
            erros.append("Papel inválido: só administrador ou condómino.")
        if palavra != confirmacao:
            erros.append("A confirmação não coincide com a palavra-passe.")

        if not erros:
            user_id, erros = criar_utilizador(
                username, palavra, papel, nome_visivel, tuple(fraction_ids)
            )
            if not erros:
                flash(
                    f"Utilizador {username} criado como {ROTULOS_PAPEL[papel]} com "
                    f"{len(fraction_ids)} associação(ões) de fração. A palavra-passe "
                    "ficou guardada como hash: ninguém a volta a ver.",
                    "sucesso",
                )
                return redirect(url_for("auth.users_list"))

        for mensagem in erros:
            flash(mensagem, "erro")

    return render_template(
        "user_new.html",
        valores=valores,
        fracoes=query_db("SELECT id, code, active FROM fractions ORDER BY code"),
        associadas=set(),
    )


@bp.route("/utilizadores/<int:user_id>", methods=["GET", "POST"])
def edit_user(user_id: int):
    recusa = recusar_se_nao_for_administrador()
    if recusa is not None:
        return recusa

    alvo = query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if alvo is None:
        flash("Esse utilizador não existe.", "erro")
        return redirect(url_for("auth.users_list"))

    if request.method == "POST":
        nome_visivel = (request.form.get("nome", "") or "").strip()[:LIMITE_NOME_VISIVEL]
        papel = request.form.get("papel", alvo["role"])
        ativo = request.form.get("ativo") == "on"
        fraction_ids = _ler_fracoes_do_form(request.form)
        nova_palavra = request.form.get("palavra_passe_nova", "") or ""
        confirmacao = request.form.get("confirmar_palavra_passe", "") or ""

        erros: list[str] = []
        if papel not in PAPEIS:
            erros.append("Papel inválido: só administrador ou condómino.")
        if papel != PAPEL_ADMIN and alvo["role"] == PAPEL_ADMIN and _administradores_ativos() <= 1:
            erros.append(
                "Este é o único administrador ativo: mudar-lhe o papel deixaria a "
                "aplicação sem administração. Cria primeiro outro administrador."
            )
        if not ativo and alvo["role"] == PAPEL_ADMIN and alvo["active"] and _administradores_ativos() <= 1:
            erros.append(
                "Este é o único administrador ativo: desativá-lo deixaria a aplicação "
                "sem administração. Cria primeiro outro administrador."
            )
        if nova_palavra:
            erro = _validar_palavra_passe(nova_palavra)
            if erro:
                erros.append(erro)
            if nova_palavra != confirmacao:
                erros.append("A confirmação não coincide com a nova palavra-passe.")

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
            return _render_edicao_utilizador(alvo, nome_visivel, papel, ativo, fraction_ids)

        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            agora = utc_now_iso()
            if nova_palavra:
                cursor = connection.execute(
                    "UPDATE users SET display_name = ?, role = ?, active = ?, updated_at = ?, "
                    "password_hash = ?, password_changed_at = ? WHERE id = ?",
                    (nome_visivel, papel, 1 if ativo else 0, agora,
                     generate_password_hash(nova_palavra), agora, user_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE users SET display_name = ?, role = ?, active = ?, updated_at = ? "
                    "WHERE id = ?",
                    (nome_visivel, papel, 1 if ativo else 0, agora, user_id),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                flash("Este utilizador deixou de existir entretanto: nada foi gravado.", "erro")
                return redirect(url_for("auth.users_list"))

            _apagar_associoes(connection, user_id)
            for fraction_id in fraction_ids:
                fracao = query_db("SELECT id FROM fractions WHERE id = ?", (fraction_id,), one=True)
                if fracao is None:
                    connection.rollback()
                    flash(f"Fração {fraction_id}: não existe. Nada foi gravado.", "erro")
                    return redirect(url_for("auth.edit_user", user_id=user_id))
                connection.execute(
                    "INSERT INTO user_fractions (user_id, fraction_id, created_at) VALUES (?, ?, ?)",
                    (user_id, fraction_id, agora),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        # A sessão de quem editou não muda; se o alvo era a própria conta e ficou
        # inativa, a próxima leitura da sessão já devolve None e a entrada cai.
        flash(
            f"Utilizador {alvo['username']} atualizado: {ROTULOS_PAPEL[papel]}, "
            f"{'ativo' if ativo else 'desativado'}, {len(fraction_ids)} associação(ões). "
            + ("Palavra-passe definida de novo." if nova_palavra else "Palavra-passe conservada."),
            "sucesso",
        )
        return redirect(url_for("auth.users_list"))

    return _render_edicao_utilizador(
        alvo,
        alvo["display_name"] or "",
        alvo["role"],
        bool(alvo["active"]),
        [int(f["id"]) for f in fracoes_do_utilizador(user_id)],
    )


def _render_edicao_utilizador(alvo, nome_visivel: str, papel: str, ativo: bool, fraction_ids: list[int]):
    return render_template(
        "user_edit.html",
        alvo={
            "id": alvo["id"],
            "username": alvo["username"],
            "created_texto": str(alvo["created_at"])[:10],
            "password_changed_texto": (
                str(alvo["password_changed_at"])[:10] if alvo["password_changed_at"] else ""
            ),
        },
        valores={"nome": nome_visivel, "papel": papel, "ativo": ativo},
        fracoes=query_db("SELECT id, code, active FROM fractions ORDER BY code"),
        associadas=set(fraction_ids),
    )
