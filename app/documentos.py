"""Documentos locais e comprovativos (entrega 4).

- Uma cópia local só existe quando o administrador a pede ("guardar como
  documento") ou quando o condómino envia um comprovativo. Consultar email ou
  descarregar um anexo não cria nada.
- Os ficheiros vivem em `instance/documentos`, fora da pasta pública, com nomes
  internos gerados pela aplicação; a aplicação é o único acesso e aplica as
  permissões existentes.
- Associar um documento a um registo não cria nem altera movimentos financeiros:
  é só um rasto ("este comprovativo respeita a este registo").
- Comprovativos enviados pelo condómino ficam `pendente`: nunca alteram saldos
  sozinhos; o administrador confirma ou rejeita com motivo.
- Partilha com condóminos é sempre explícita, fração a fração. Emails, extratos
  e documentos financeiros privados não são partilhados automaticamente.
- "Marcado como tratado" é uma nota da aplicação: não é movimento financeiro.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .auth import (
    current_user,
    eh_administrador,
    fracoes_do_utilizador,
    recusar_se_nao_for_administrador,
)
from .db import execute_db, get_db, query_db, utc_now_iso
from .units import format_date, parse_date, parse_int_id

bp = Blueprint("documentos", __name__)

# Tipos aceites para cópias locais e uploads. Fora desta lista, o anexo pode
# ver-se no Gmail mas não é guardado como documento.
EXTENSOES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
# Comprovativos do condómino: só formatos que o administrador consegue ler à mesa.
COMPROVATIVOS_MIMES = {"application/pdf", "image/png", "image/jpeg"}
# Extensões aceites na biblioteca, por nome de ficheiro (o navegador nem sempre
# envia o tipo correto): a extensão manda e o tipo é confirmado por ela.
EXTENSOES_BIBLIOTECA = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"}
MIME_POR_EXTENSAO = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB por comprovativo
MAX_DOCUMENTO_BYTES = 10 * 1024 * 1024  # 10 MB por ficheiro da biblioteca
LIMITE_NOTA = 300
LIMITE_DESCRICAO = 1000

CATEGORIAS = (
    "Ata", "Fatura", "Extrato bancário", "Contrato", "Seguro",
    "Orçamento", "Comprovativo", "Outro",
)
ROTULOS_ORIGEM = {
    "email": "Gmail",
    "anexo": "Gmail (anexo)",
    "condomino": "Comprovativo",
    "upload": "Upload",
}

ALVOS = {
    "pagamento": ("payments", "pagamento"),
    "pagamento_despesa": ("expense_payments", "pagamento de despesa"),
    "despesa": ("expenses", "despesa"),
    "fracao": ("fractions", "fração"),
}
ROTULOS_ALVO = {
    "pagamento": "Pagamento de condómino",
    "pagamento_despesa": "Pagamento de despesa",
    "despesa": "Despesa",
    "fracao": "Fração",
}


# ---------------------------------------------------------------------------
# Armazenamento
# ---------------------------------------------------------------------------


def pasta_documentos() -> Path:
    pasta = Path(current_app.config["INSTANCE_PATH"]) / "documentos"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def guardar_documento(
    *,
    origem: str,
    dados: bytes,
    nome_original: str,
    mime: str,
    resumo: str = "",
    gmail_mensagem_id: str = "",
    gmail_anexo_id: str = "",
    decisao: str = "guardado",
    titulo: str = "",
    categoria: str = "Outro",
    data_documento: str = "",
    descricao: str = "",
    ata_numero: str = "",
    ata_data: str = "",
    versao: int = 1,
    versao_de: int | None = None,
) -> int:
    """Guardar bytes como documento local. Devolve o id; não cria movimentos."""
    extensao = EXTENSOES.get(mime)
    if extensao is None:
        raise ValueError(f"tipo de ficheiro não aceite como documento: {mime or '(sem tipo)'}")
    nome_interno = secrets.token_hex(16) + extensao
    caminho = pasta_documentos() / nome_interno
    caminho.write_bytes(dados)

    try:
        utilizador = current_user()
    except RuntimeError:
        # Chamado fora de um pedido (por exemplo por um teste): o autor fica em branco,
        # mas nunca é inventado.
        utilizador = None
    return execute_db(
        """
        INSERT INTO documentos (origem, nome_interno, nome_original, mime, tamanho_bytes,
                                sha256, gmail_mensagem_id, gmail_anexo_id, resumo, decisao,
                                titulo, categoria, data_documento, descricao,
                                ata_numero, ata_data, versao, versao_de,
                                criado_em, criado_por)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            origem,
            nome_interno,
            (nome_original or "")[:160],
            mime,
            len(dados),
            hashlib.sha256(dados).hexdigest(),
            gmail_mensagem_id,
            gmail_anexo_id,
            resumo[:300],
            decisao,
            (titulo or "")[:160],
            categoria if categoria in CATEGORIAS else "Outro",
            data_documento,
            descricao[:LIMITE_DESCRICAO],
            (ata_numero or "")[:60],
            ata_data,
            int(versao),
            versao_de,
            utc_now_iso(),
            str(utilizador["username"]) if utilizador else "",
        ),
    )


def _documento_or_404(documento_id: int) -> dict:
    linha = query_db("SELECT * FROM documentos WHERE id = ?", (documento_id,), one=True)
    if linha is None:
        abort(404)
    return dict(linha)


def _pode_ver(documento: dict) -> bool:
    if eh_administrador():
        return True
    utilizador = current_user()
    if utilizador is None:
        return False
    if documento["origem"] == "condomino" and documento["criado_por"] == str(utilizador["username"]):
        return True
    if int(documento.get("partilha_todos") or 0) == 1:
        return True
    fracoes = {int(f["id"]) for f in fracoes_do_utilizador(int(utilizador["id"]))}
    if not fracoes:
        return False
    partilhadas = {
        int(linha["documento_id"])
        for linha in query_db(
            "SELECT documento_id FROM documento_fracoes WHERE fraction_id IN (%s)"
            % ", ".join("?" for _ in fracoes),
            tuple(fracoes),
        )
    }
    return documento["id"] in partilhadas


def _documento_repetido(sha256: str, exceto_ids: set[int] = frozenset()) -> dict | None:
    """Documento já guardado com exatamente os mesmos bytes (detete antes de duplicar)."""
    linha = query_db(
        "SELECT id, titulo, nome_original FROM documentos WHERE sha256 = ? AND id NOT IN (%s) "
        "ORDER BY id LIMIT 1"
        % ", ".join("?" for _ in exceto_ids) if exceto_ids else
        "SELECT id, titulo, nome_original FROM documentos WHERE sha256 = ? ORDER BY id LIMIT 1",
        tuple(exceto_ids) + (sha256,) if exceto_ids else (sha256,),
        one=True,
    )
    return dict(linha) if linha is not None else None


def _validar_ficheiro_biblioteca(ficheiro, limite: int) -> tuple[bytes, str, str] | str:
    """Devolve (bytes, nome, mime) ou uma mensagem de recusa, clara e acionável."""
    nome = (ficheiro.filename or "").strip()
    if not nome:
        return "Escolhe pelo menos um ficheiro (PDF, PNG, JPG, DOCX ou XLSX)."
    extensao = Path(nome).suffix.lower()
    if extensao not in EXTENSOES_BIBLIOTECA:
        return (
            f"「{nome}」: tipo não aceite. A biblioteca aceita PDF, PNG, JPG, DOCX e XLSX."
        )
    ficheiro.seek(0, 2)
    tamanho = ficheiro.tell()
    ficheiro.seek(0)
    if tamanho > limite:
        return (
            f"「{nome}」: {tamanho // 1024} KB, acima do limite de {limite // (1024 * 1024)} MB."
        )
    if tamanho == 0:
        return f"「{nome}」: o ficheiro está vazio."
    mime = ficheiro.mimetype if ficheiro.mimetype in EXTENSOES else MIME_POR_EXTENSAO[extensao]
    return ficheiro.read(), nome, mime


def _cadeia_versoes(documento_id: int) -> list[dict]:
    """Todas as versões de um documento, da mais antiga à mais atual.

    `versao_de` aponta para a versão anterior; a cadeia reconstrói-se nos dois
    sentidos. A versão atual é a última: as anteriores conservam-se sempre.
    """
    raiz = documento_id
    linha = query_db("SELECT versao_de FROM documentos WHERE id = ?", (raiz,), one=True)
    while linha is not None and linha["versao_de"] is not None:
        raiz = int(linha["versao_de"])
        linha = query_db("SELECT versao_de FROM documentos WHERE id = ?", (raiz,), one=True)
    cadeia: list[dict] = []
    fila = [raiz]
    while fila:
        atual = fila.pop(0)
        linha = query_db("SELECT * FROM documentos WHERE id = ?", (atual,), one=True)
        if linha is None:
            continue
        cadeia.append({**dict(linha), "criado_em_texto": format_date(linha["criado_em"])})
        fila.extend(
            int(filha["id"])
            for filha in query_db("SELECT id FROM documentos WHERE versao_de = ? ORDER BY id", (atual,))
        )
    cadeia.sort(key=lambda d: (int(d["versao"]), int(d["id"])))
    if cadeia:
        cadeia[-1]["atual"] = True
    return cadeia


def _nome_atual() -> str:
    try:
        utilizador = current_user()
    except RuntimeError:
        return ""
    return str(utilizador["username"]) if utilizador else ""


def ligar_documento(documento_id: int, alvo: str, alvo_id: int, notas: str = "") -> bool:
    """Criar a ligação documento↔registo. Devolve False se já existia ou é inválida."""
    if alvo not in ALVOS:
        return False
    tabela, _ = ALVOS[alvo]
    if query_db(f"SELECT id FROM {tabela} WHERE id = ?", (alvo_id,), one=True) is None:
        return False
    try:
        get_db().execute(
            "INSERT INTO documento_ligacoes (documento_id, alvo, alvo_id, criado_em, criado_por, notas) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (documento_id, alvo, alvo_id, utc_now_iso(), _nome_atual(), notas[:300]),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def _ligacoes(documento_id: int) -> list[dict]:
    return [
        {
            "alvo": linha["alvo"],
            "alvo_id": int(linha["alvo_id"]),
            "alvo_texto": ROTULOS_ALVO[linha["alvo"]],
            "criado_em": format_date(linha["criado_em"]),
            "criado_por": linha["criado_por"],
            "notas": linha["notas"],
        }
        for linha in query_db(
            "SELECT * FROM documento_ligacoes WHERE documento_id = ? ORDER BY id",
            (documento_id,),
        )
    ]


def _possiveis_duplicados(documento: dict) -> list[dict]:
    """Registos com a mesma referência escrita no nome ou resumo do documento.

    É um aviso, não uma certezas: quem confirma é sempre o administrador.
    """
    texto = f"{documento['nome_original']} {documento['resumo']}".lower()
    tokens = {t for t in re.findall(r"[a-z0-9][a-z0-9._/-]{3,}", texto) if not t.endswith(".txt")}
    suspeitos: list[dict] = []
    for linha in query_db(
        "SELECT id, reference FROM payments WHERE reference <> '' ORDER BY id DESC LIMIT 200"
    ):
        referencia = str(linha["reference"]).strip().lower()
        if referencia and (referencia in tokens or referencia in texto):
            suspeitos.append({"tipo": "pagamento", "id": int(linha["id"]), "referencia": linha["reference"]})
    for linha in query_db(
        "SELECT id, referencia FROM expenses WHERE referencia <> '' ORDER BY id DESC LIMIT 200"
    ):
        referencia = str(linha["referencia"]).strip().lower()
        if referencia and (referencia in tokens or referencia in texto):
            suspeitos.append({"tipo": "despesa", "id": int(linha["id"]), "referencia": linha["referencia"]})
    return suspeitos[:10]


def documento_para_formulario(doc_bruto: str) -> dict | None:
    """Painel de documento para os formulários de registo (?doc=<id>).

    Só o administrador usa documentos como origem de registo.
    """
    if not eh_administrador():
        return None
    documento_id = parse_int_id((doc_bruto or "").strip())
    if documento_id is None:
        return None
    linha = query_db("SELECT * FROM documentos WHERE id = ?", (documento_id,), one=True)
    if linha is None:
        return None
    documento = dict(linha)
    return {
        "id": documento["id"],
        "resumo": documento["resumo"] or documento["nome_original"],
        "origem_texto": {
            "email": "email",
            "anexo": "anexo do email",
            "condomino": "comprovativo do condómino",
            "upload": "upload do administrador",
        }[documento["origem"]],
        "duplicados": _possiveis_duplicados(documento),
    }


# ---------------------------------------------------------------------------
# Biblioteca do administrador: lista compacta com pesquisa e filtros
# ---------------------------------------------------------------------------


def _partilha_texto(documento: dict) -> str:
    if int(documento.get("partilha_todos") or 0) == 1:
        return "todos os condóminos"
    codigos = [
        linha["code"]
        for linha in query_db(
            "SELECT f.code FROM documento_fracoes df JOIN fractions f ON f.id = df.fraction_id "
            "WHERE df.documento_id = ? ORDER BY f.code",
            (documento["id"],),
        )
    ]
    return f"frações: {', '.join(codigos)}" if codigos else "privado"


def _eh_versao_atual(documento_id: int) -> bool:
    """Atual é a versão de que nenhuma outra nasceu."""
    return (
        query_db(
            "SELECT COUNT(*) AS n FROM documentos WHERE versao_de = ?", (documento_id,), one=True
        )["n"]
        == 0
    )


@bp.route("/documentos")
def documentos_lista():
    recusar = recusar_se_nao_for_administrador()
    if recusar is not None:
        return recusar

    q = (request.args.get("q") or "").strip()
    categoria = (request.args.get("categoria") or "").strip()
    data_de = parse_date(request.args.get("data_de") or "")
    data_ate = parse_date(request.args.get("data_ate") or "")
    mostrar_arquivados = request.args.get("arquivados") == "1"

    clausulas: list[str] = []
    parametros: list[object] = []
    if q:
        clausulas.append("(d.titulo LIKE ? OR d.nome_original LIKE ? OR d.resumo LIKE ?)")
        parametros.extend([f"%{q}%"] * 3)
    if categoria in CATEGORIAS:
        clausulas.append("d.categoria = ?")
        parametros.append(categoria)
    data_efetiva = "COALESCE(NULLIF(d.data_documento, ''), substr(d.criado_em, 1, 10))"
    if data_de:
        clausulas.append(f"{data_efetiva} >= ?")
        parametros.append(data_de)
    if data_ate:
        clausulas.append(f"{data_efetiva} <= ?")
        parametros.append(data_ate)
    if not mostrar_arquivados:
        clausulas.append("d.arquivado = 0")
    where = (" WHERE " + " AND ".join(clausulas)) if clausulas else ""

    linhas = []
    for linha in query_db(
        f"SELECT d.* FROM documentos d{where} ORDER BY {data_efetiva} DESC, d.id DESC",
        tuple(parametros),
    ):
        versao = int(linha["versao"])
        linhas.append(
            {
                "id": int(linha["id"]),
                "titulo": linha["titulo"] or linha["resumo"] or linha["nome_original"],
                "nome_original": linha["nome_original"],
                "categoria": linha["categoria"],
                "data": format_date(linha["data_documento"] or str(linha["criado_em"])[:10]),
                "origem": ROTULOS_ORIGEM[linha["origem"]],
                "tamanho_texto": f"{int(linha['tamanho_bytes']) // 1024} KB",
                "decisao": linha["decisao"],
                "tratado": linha["tratado_em"] is not None,
                "arquivado": int(linha["arquivado"]) == 1,
                "partilha": _partilha_texto(dict(linha)),
                "versao": versao,
                "versao_texto": (
                    f"v{versao} · {'atual' if _eh_versao_atual(int(linha['id'])) else 'antiga'}"
                    if versao > 1
                    else ""
                ),
                "ligacoes": int(
                    query_db(
                        "SELECT COUNT(*) AS n FROM documento_ligacoes WHERE documento_id = ?",
                        (linha["id"],),
                        one=True,
                    )["n"]
                ),
            }
        )

    pendentes = query_db("SELECT id FROM documentos WHERE decisao = 'pendente'")
    arquivados_total = int(
        query_db("SELECT COUNT(*) AS n FROM documentos WHERE arquivado = 1", one=True)["n"]
    )
    return render_template(
        "documentos_list.html",
        documentos=linhas,
        comprovativos_pendentes=pendentes,
        categorias=CATEGORIAS,
        filtros={
            "q": q,
            "categoria": categoria,
            "data_de": request.args.get("data_de") or "",
            "data_ate": request.args.get("data_ate") or "",
            "arquivados": mostrar_arquivados,
        },
        arquivados_total=arquivados_total,
    )


# ---------------------------------------------------------------------------
# Documentos visíveis ao condómino (partilhados com as suas frações)
# ---------------------------------------------------------------------------


@bp.route("/documentos/meus")
def documentos_meus():
    """Lista do condómino: o que o administrador partilhou com as suas frações
    e os seus próprios comprovativos. Nada é partilhado automaticamente."""
    utilizador = current_user()
    if utilizador is None:
        return redirect(url_for("auth.login"))
    if eh_administrador():
        return redirect(url_for("documentos.documentos_lista"))

    fracoes = fracoes_do_utilizador(int(utilizador["id"]))
    fracao_ids = tuple(int(f["id"]) for f in fracoes)
    if fracao_ids:
        partilhados = query_db(
            "SELECT DISTINCT d.* FROM documentos d "
            "LEFT JOIN documento_fracoes df ON df.documento_id = d.id "
            "WHERE d.partilha_todos = 1 OR df.fraction_id IN (%s) ORDER BY d.id DESC"
            % ", ".join("?" for _ in fracao_ids),
            fracao_ids,
        )
    else:
        partilhados = query_db("SELECT * FROM documentos WHERE partilha_todos = 1 ORDER BY id DESC")
    proprios = query_db(
        "SELECT * FROM documentos WHERE origem = 'condomino' AND criado_por = ? ORDER BY id DESC",
        (str(utilizador["username"]),),
    )
    vistos = {int(linha["id"]) for linha in partilhados}
    linhas = [
        {
            "id": int(linha["id"]),
            "nome": linha["titulo"] or linha["resumo"] or linha["nome_original"],
            "categoria": linha["categoria"],
            "data": format_date(linha["data_documento"] or str(linha["criado_em"])[:10]),
            "origem": ROTULOS_ORIGEM[linha["origem"]],
            "arquivado": int(linha["arquivado"]) == 1,
            "criado_em": format_date(linha["criado_em"]),
            "decisao": linha["decisao"],
            "motivo": linha["decisao_motivo"],
            "nos_meus": int(linha["id"]) in vistos,
        }
        for linha in partilhados
    ] + [
        {
            "id": int(linha["id"]),
            "nome": linha["nome_original"],
            "categoria": linha["categoria"],
            "data": format_date(linha["data_documento"] or str(linha["criado_em"])[:10]),
            "origem": ROTULOS_ORIGEM[linha["origem"]],
            "arquivado": int(linha["arquivado"]) == 1,
            "criado_em": format_date(linha["criado_em"]),
            "decisao": linha["decisao"],
            "motivo": linha["decisao_motivo"],
            "nos_meus": False,
        }
        for linha in proprios
    ]
    return render_template("documentos_meus.html", documentos=linhas, fracoes=fracoes)


# ---------------------------------------------------------------------------
# Ver e descarregar (com as permissões existentes)
# ---------------------------------------------------------------------------


@bp.route("/documentos/<int:documento_id>")
def documento_ver(documento_id: int):
    documento = _documento_or_404(documento_id)
    if not _pode_ver(documento):
        abort(404)

    fracao_ids_partilhadas = {
        int(linha["fraction_id"])
        for linha in query_db("SELECT fraction_id FROM documento_fracoes WHERE documento_id = ?", (documento_id,))
    }
    fracoes = query_db("SELECT id, code FROM fractions ORDER BY code")
    sugestao = None
    if eh_administrador():
        # Resultado de uma análise anterior, se acabou de ser pedida (via sessão).
        sugestao = session_sugestao(documento_id)

    return render_template(
        "documento_view.html",
        documento={
            **documento,
            "id": int(documento["id"]),
            "tamanho_texto": f"{int(documento['tamanho_bytes']) // 1024} KB",
            "criado_em_texto": format_date(documento["criado_em"]),
            "decidido_em_texto": format_date(documento["decidido_em"]) if documento["decidido_em"] else "",
            "tratado_texto": format_date(documento["tratado_em"]) if documento["tratado_em"] else "",
            "data_documento_texto": format_date(documento["data_documento"]),
            "ata_data_texto": format_date(documento["ata_data"]),
            "origem_texto": ROTULOS_ORIGEM[documento["origem"]],
            "arquivado": int(documento["arquivado"]) == 1,
            "partilha_todos": int(documento["partilha_todos"]) == 1,
            "inline": documento["mime"] in ("application/pdf", "image/png", "image/jpeg", "text/plain"),
        },
        versoes=_cadeia_versoes(documento_id),
        ligacoes=_ligacoes(documento_id),
        duplicados=_possiveis_duplicados(documento) if eh_administrador() else [],
        fracoes=[
            {"code": f["code"], "partilhada": int(f["id"]) in fracao_ids_partilhadas}
            for f in fracoes
        ],
        eh_admin=eh_administrador(),
        sugestao=sugestao,
    )


@bp.route("/documentos/<int:documento_id>/descarregar")
def documento_descarregar(documento_id: int):
    documento = _documento_or_404(documento_id)
    if not _pode_ver(documento):
        abort(404)
    caminho = pasta_documentos() / Path(documento["nome_interno"]).name
    if not caminho.exists():
        abort(404, description="ficheiro não encontrado")
    inline = documento["mime"] in ("application/pdf", "image/png", "image/jpeg", "text/plain")
    return send_file(
        caminho,
        mimetype=documento["mime"] or "application/octet-stream",
        as_attachment=not inline,
        download_name=documento["nome_original"] or caminho.name,
    )


# ---------------------------------------------------------------------------
# Ações do administrador: associar, tratar, decidir, partilhar
# ---------------------------------------------------------------------------


def _apenas_administrador():
    if not eh_administrador():
        abort(404)


@bp.route("/documentos/<int:documento_id>/associar", methods=["POST"])
def documento_associar(documento_id: int):
    _apenas_administrador()
    documento = _documento_or_404(documento_id)
    alvo = (request.form.get("alvo") or "").strip()
    alvo_id = parse_int_id(request.form.get("alvo_id", ""))
    if alvo not in ALVOS or alvo_id is None:
        flash("Associação: escolhe o tipo de registo e escreve o número do registo. Nada foi alterado.", "erro")
        return redirect(url_for("documentos.documento_ver", documento_id=documento_id))
    if ligar_documento(documento_id, alvo, alvo_id, request.form.get("notas", "")):
        get_db().commit()
        artigo = "à" if alvo in ("despesa", "fracao") else "ao"
        flash(
            f"Documento associado {artigo} {ALVOS[alvo][1]} n.º {alvo_id}. A associação não cria "
            "nem altera movimentos financeiros.",
            "sucesso",
        )
    else:
        flash(
            f"Não foi possível associar: o {ALVOS.get(alvo, ('', 'registo'))[1]} n.º {alvo_id} não existe, "
            "ou este documento já estava associado a ele.",
            "erro",
        )
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/tratado", methods=["POST"])
def documento_tratado(documento_id: int):
    _apenas_administrador()
    _documento_or_404(documento_id)
    execute_db(
        "UPDATE documentos SET tratado_em = ? WHERE id = ? AND tratado_em IS NULL",
        (utc_now_iso(), documento_id),
    )
    flash(
        "Marcado como tratado nesta aplicação. É uma nota de trabalho: não é movimento financeiro.",
        "sucesso",
    )
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/decidir", methods=["POST"])
def documento_decidir(documento_id: int):
    _apenas_administrador()
    documento = _documento_or_404(documento_id)
    decisao = (request.form.get("decisao") or "").strip()
    motivo = (request.form.get("motivo") or "").strip()
    if decisao not in ("confirmado", "rejeitado"):
        flash("Decisão inválida. Nada foi alterado.", "erro")
        return redirect(url_for("documentos.documento_ver", documento_id=documento_id))
    if decisao == "rejeitado" and not motivo:
        flash("Rejeitar um comprovativo exige um motivo escrito. Nada foi alterado.", "erro")
        return redirect(url_for("documentos.documento_ver", documento_id=documento_id))
    utilizador = current_user()
    execute_db(
        "UPDATE documentos SET decisao = ?, decisao_motivo = ?, decidido_em = ?, decidido_por = ? WHERE id = ?",
        (
            decisao,
            motivo[:300],
            utc_now_iso(),
            str(utilizador["username"]) if utilizador else "",
            documento_id,
        ),
    )
    flash(
        f"Comprovativo {decisao}. Confirmar um comprovativo não altera saldos: "
        "o movimento continua a ser registado à parte.",
        "sucesso",
    )
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/partilhar", methods=["POST"])
def documento_partilhar(documento_id: int):
    _apenas_administrador()
    _documento_or_404(documento_id)
    escolhidas = {
        int(f["id"])
        for f in query_db("SELECT id, code FROM fractions")
        if request.form.get(f"fracao_{f['code']}") == "on"
    }
    atuais = {
        int(linha["fraction_id"])
        for linha in query_db("SELECT fraction_id FROM documento_fracoes WHERE documento_id = ?", (documento_id,))
    }
    connection = get_db()
    for fraction_id in atuais - escolhidas:
        connection.execute(
            "DELETE FROM documento_fracoes WHERE documento_id = ? AND fraction_id = ?",
            (documento_id, fraction_id),
        )
    for fraction_id in escolhidas - atuais:
        connection.execute(
            "INSERT INTO documento_fracoes (documento_id, fraction_id, criado_em) VALUES (?, ?, ?)",
            (documento_id, fraction_id, utc_now_iso()),
        )
    todos = 1 if request.form.get("todos_condominos") == "on" else 0
    connection.execute(
        "UPDATE documentos SET partilha_todos = ? WHERE id = ?", (todos, documento_id)
    )
    connection.commit()
    flash(
        "Partilha atualizada. "
        + (
            "Todos os condóminos passam a ver este documento. "
            if todos
            else "Só as frações marcadas passam a ver este documento. "
        )
        + "Nada é partilhado automaticamente.",
        "sucesso",
    )
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


# ---------------------------------------------------------------------------
# Biblioteca: carregar, editar metadados, arquivar, versões
# ---------------------------------------------------------------------------


@bp.route("/documentos/carregar", methods=["GET", "POST"])
def documentos_carregar():
    _apenas_administrador()
    limite_mb = MAX_DOCUMENTO_BYTES // (1024 * 1024)

    def _formulario(codigo: int = 200):
        return (
            render_template(
                "documento_upload.html",
                categorias=CATEGORIAS,
                limite_mb=limite_mb,
                categoria=request.form.get("categoria") or "Outro",
            ),
            codigo,
        )

    if request.method == "GET":
        return _formulario()

    titulo = (request.form.get("titulo") or "").strip()
    categoria = (request.form.get("categoria") or "").strip()
    data_documento = parse_date(request.form.get("data_documento") or "") or ""
    descricao = (request.form.get("descricao") or "").strip()
    ata_numero = (request.form.get("ata_numero") or "").strip()
    ata_data = parse_date(request.form.get("ata_data") or "") or ""
    confirmar_repetido = request.form.get("repetido") == "on"

    erros: list[str] = []
    if categoria not in CATEGORIAS:
        erros.append("Categoria: escolhe uma das categorias da lista.")
    if len(titulo) > 160:
        erros.append("Título: no máximo 160 caracteres.")
    if len(descricao) > LIMITE_DESCRICAO:
        erros.append(f"Descrição: no máximo {LIMITE_DESCRICAO} caracteres.")
    ficheiros = [f for f in request.files.getlist("ficheiros") if (f.filename or "").strip()]
    if not ficheiros:
        erros.append("Escolhe pelo menos um ficheiro (PDF, PNG, JPG, DOCX ou XLSX).")
    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return _formulario()

    guardados: list[int] = []
    recusados = 0
    for ficheiro in ficheiros:
        resultado = _validar_ficheiro_biblioteca(ficheiro, MAX_DOCUMENTO_BYTES)
        if isinstance(resultado, str):
            flash(resultado, "erro")
            recusados += 1
            continue
        dados, nome, mime = resultado
        repetido = _documento_repetido(hashlib.sha256(dados).hexdigest())
        if repetido is not None and not confirmar_repetido:
            flash(
                f"「{nome}」 é idêntico ao documento n.º {repetido['id']} "
                f"(「{repetido['titulo'] or repetido['nome_original']}」): não foi guardado outra vez. "
                "Marca 「guardar mesmo assim」 se queres ficar com os dois.",
                "erro",
            )
            recusados += 1
            continue
        nome_titulo = titulo if len(ficheiros) == 1 else Path(nome).stem
        try:
            guardados.append(
                guardar_documento(
                    origem="upload",
                    dados=dados,
                    nome_original=nome,
                    mime=mime,
                    resumo=nome_titulo[:300],
                    titulo=nome_titulo,
                    categoria=categoria,
                    data_documento=data_documento,
                    descricao=descricao,
                    ata_numero=ata_numero if categoria == "Ata" else "",
                    ata_data=ata_data if categoria == "Ata" else "",
                )
            )
        except ValueError as erro:
            flash(f"「{nome}」: {erro}", "erro")
            recusados += 1

    if not guardados:
        return _formulario()
    flash(
        f"{len(guardados)} documento(s) guardados. Começam privados: "
        "partilha quando quiseres. Nenhum cria movimentos financeiros.",
        "sucesso",
    )
    if recusados:
        flash(f"{recusados} ficheiro(s) recusado(s): vê as mensagens acima.", "erro")
    if len(guardados) == 1:
        return redirect(url_for("documentos.documento_ver", documento_id=guardados[0]))
    return redirect(url_for("documentos.documentos_lista"))


@bp.route("/documentos/<int:documento_id>/editar", methods=["GET", "POST"])
def documento_editar(documento_id: int):
    _apenas_administrador()
    documento = _documento_or_404(documento_id)
    if request.method == "GET":
        return render_template("documento_editar.html", documento=documento, categorias=CATEGORIAS)

    titulo = (request.form.get("titulo") or "").strip()
    categoria = (request.form.get("categoria") or "").strip()
    data_documento = parse_date(request.form.get("data_documento") or "") or ""
    descricao = (request.form.get("descricao") or "").strip()
    ata_numero = (request.form.get("ata_numero") or "").strip()
    ata_data = parse_date(request.form.get("ata_data") or "") or ""

    erros: list[str] = []
    if categoria not in CATEGORIAS:
        erros.append("Categoria: escolhe uma das categorias da lista.")
    if not titulo:
        erros.append("Título: escreve um título para o documento.")
    if len(titulo) > 160:
        erros.append("Título: no máximo 160 caracteres.")
    if len(descricao) > LIMITE_DESCRICAO:
        erros.append(f"Descrição: no máximo {LIMITE_DESCRICAO} caracteres.")
    if erros:
        for mensagem in erros:
            flash(mensagem, "erro")
        return render_template("documento_editar.html", documento=documento, categorias=CATEGORIAS)

    execute_db(
        "UPDATE documentos SET titulo = ?, categoria = ?, data_documento = ?, descricao = ?, "
        "ata_numero = ?, ata_data = ? WHERE id = ?",
        (
            titulo[:160],
            categoria,
            data_documento,
            descricao[:LIMITE_DESCRICAO],
            ata_numero[:60] if categoria == "Ata" else "",
            ata_data if categoria == "Ata" else "",
            documento_id,
        ),
    )
    flash("Metadados atualizados. O ficheiro em si não mudou.", "sucesso")
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/arquivar", methods=["POST"])
def documento_arquivar(documento_id: int):
    _apenas_administrador()
    _documento_or_404(documento_id)
    execute_db("UPDATE documentos SET arquivado = 1 WHERE id = ?", (documento_id,))
    flash(
        "Documento arquivado: as associações e o histórico conservam-se; deixa de "
        "aparecer na lista principal. Nada foi apagado.",
        "sucesso",
    )
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/repor", methods=["POST"])
def documento_repor(documento_id: int):
    _apenas_administrador()
    _documento_or_404(documento_id)
    execute_db("UPDATE documentos SET arquivado = 0 WHERE id = ?", (documento_id,))
    flash("Documento reposto: volta a aparecer na lista principal.", "sucesso")
    return redirect(url_for("documentos.documento_ver", documento_id=documento_id))


@bp.route("/documentos/<int:documento_id>/nova-versao", methods=["POST"])
def documento_nova_versao(documento_id: int):
    _apenas_administrador()
    documento = _documento_or_404(documento_id)
    ficheiro = request.files.get("ficheiro")
    resultado = (
        _validar_ficheiro_biblioteca(ficheiro, MAX_DOCUMENTO_BYTES)
        if ficheiro is not None
        else "Escolhe o ficheiro da nova versão (PDF, PNG, JPG, DOCX ou XLSX)."
    )
    if isinstance(resultado, str):
        flash(resultado, "erro")
        return redirect(url_for("documentos.documento_ver", documento_id=documento_id))
    dados, nome, mime = resultado

    cadeia = _cadeia_versoes(documento_id)
    sha = hashlib.sha256(dados).hexdigest()
    if any(str(versao["sha256"]) == sha for versao in cadeia):
        flash(
            "A nova versão tem de ser um ficheiro diferente: estes bytes já são uma "
            "versão deste documento. Nada foi alterado.",
            "erro",
        )
        return redirect(url_for("documentos.documento_ver", documento_id=documento_id))

    nova_versao = max(int(versao["versao"]) for versao in cadeia) + 1
    novo_id = guardar_documento(
        origem="upload",
        dados=dados,
        nome_original=nome,
        mime=mime,
        resumo=(documento["resumo"] or nome)[:300],
        titulo=documento["titulo"] or Path(nome).stem,
        categoria=documento["categoria"],
        data_documento=documento["data_documento"],
        descricao=documento["descricao"],
        ata_numero=documento["ata_numero"],
        ata_data=documento["ata_data"],
        versao=nova_versao,
        versao_de=documento_id,
    )
    execute_db(
        "UPDATE documentos SET partilha_todos = ? WHERE id = ?",
        (int(documento["partilha_todos"]), novo_id),
    )
    for linha in query_db(
        "SELECT fraction_id FROM documento_fracoes WHERE documento_id = ?", (documento_id,)
    ):
        execute_db(
            "INSERT INTO documento_fracoes (documento_id, fraction_id, criado_em) VALUES (?, ?, ?)",
            (novo_id, int(linha["fraction_id"]), utc_now_iso()),
        )
    flash(
        f"Versão {nova_versao} guardada. A versão anterior conserva-se; esta passa a "
        "ser a atual. Nada foi substituído silenciosamente.",
        "sucesso",
    )
    return redirect(url_for("documentos.documento_ver", documento_id=novo_id))


# ---------------------------------------------------------------------------
# Comprovativos do condómino
# ---------------------------------------------------------------------------


@bp.route("/comprovativos", methods=["GET", "POST"])
def comprovativos():
    utilizador = current_user()
    if utilizador is None:
        return redirect(url_for("auth.login"))
    if eh_administrador():
        return redirect(url_for("documentos.documentos_lista"))

    fracoes = fracoes_do_utilizador(int(utilizador["id"]))
    erros: list[str] = []

    if request.method == "POST":
        ficheiro = request.files.get("ficheiro")
        codigo = (request.form.get("fracao") or "").strip()
        nota = (request.form.get("nota") or "").strip()
        fracao = next((f for f in fracoes if f["code"] == codigo), None)
        if ficheiro is None or not ficheiro.filename:
            erros.append("Escolhe o ficheiro do comprovativo (PDF, PNG ou JPG).")
        elif ficheiro.mimetype not in COMPROVATIVOS_MIMES:
            erros.append("Tipo não aceite: usa PDF, PNG ou JPG.")
        if ficheiro is not None and ficheiro.mimetype in COMPROVATIVOS_MIMES:
            ficheiro.seek(0, 2)
            tamanho = ficheiro.tell()
            ficheiro.seek(0)
            if tamanho > MAX_UPLOAD_BYTES:
                erros.append("Comprovativo demasiado grande: o limite é 5 MB.")
        if fracao is None:
            erros.append("Fração: escolhe uma das tuas frações.")
        if len(nota) > LIMITE_NOTA:
            erros.append(f"Nota: no máximo {LIMITE_NOTA} caracteres.")

        if erros:
            for mensagem in erros:
                flash(mensagem, "erro")
        else:
            try:
                documento_id = guardar_documento(
                    origem="condomino",
                    dados=ficheiro.read(),
                    nome_original=ficheiro.filename,
                    mime=ficheiro.mimetype,
                    resumo=f"Comprovativo {codigo}: {nota}"[:300],
                    decisao="pendente",
                )
            except ValueError as erro:
                flash(str(erro), "erro")
                return redirect(url_for("documentos.comprovativos"))
            flash(
                f"Comprovativo enviado para a fração {codigo}: fica pendente de confirmação "
                "do administrador e não altera saldos automaticamente.",
                "sucesso",
            )
            return redirect(url_for("documentos.documento_ver", documento_id=documento_id))

    meus = [
        {
            "id": int(linha["id"]),
            "nome": linha["nome_original"],
            "decisao": linha["decisao"],
            "motivo": linha["decisao_motivo"],
            "criado_em": format_date(linha["criado_em"]),
        }
        for linha in query_db(
            "SELECT * FROM documentos WHERE origem = 'condomino' AND criado_por = ? ORDER BY id DESC",
            (str(utilizador["username"]),),
        )
    ]
    return render_template(
        "comprovativos.html",
        fracoes=fracoes,
        meus=meus,
    )


# ---------------------------------------------------------------------------
# Sugestão de IA (guardada só na sessão, para mostrar no documento)
# ---------------------------------------------------------------------------


def session_sugestao(documento_id: int):
    from flask import session

    guardada = session.get("ia_sugestao")
    if guardada and guardada.get("documento_id") == documento_id:
        session.pop("ia_sugestao", None)
        return guardada
    return None


def guardar_sugestao_na_sessao(documento_id: int, sugestao: dict) -> None:
    from flask import session

    session["ia_sugestao"] = {"documento_id": documento_id, **sugestao}
