"""Fixtures partilhadas: app Flask com base temporária e isolamento da base real."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app  # noqa: E402
from app.db import init_db  # noqa: E402
from app.units import format_date, format_money_input  # noqa: E402

PROJECT_INSTANCE_DIR = PROJECT_ROOT / "instance"
CHAVE_DE_TESTE = "chave-de-teste-para-isolamento"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^"]*)"')
TOKEN_RE = re.compile(r'name="form_token" value="([^"]+)"')
EDIT_TOKEN_RE = re.compile(r'name="edit_token" value="([^"]+)"')


def _snapshot(directory: Path) -> dict[str, tuple[int, int]] | None:
    """Estado da pasta: None se não existir; senão nome -> (tamanho, mtime)."""
    if not directory.exists():
        return None
    return {
        item.name: (item.stat().st_size, item.stat().st_mtime_ns)
        for item in sorted(directory.iterdir())
    }


@pytest.fixture(autouse=True)
def instance_snapshot(monkeypatch):
    """Nenhum teste usa a base real nem os segredos do projeto."""
    monkeypatch.setenv("CONDOMINIO_SECRET_KEY", CHAVE_DE_TESTE)
    monkeypatch.delenv("CONDOMINIO_DATABASE", raising=False)
    antes = _snapshot(PROJECT_INSTANCE_DIR)
    yield antes
    depois = _snapshot(PROJECT_INSTANCE_DIR)
    assert depois == antes, (
        "os testes criaram ou alteraram ficheiros em instance/ do projeto: "
        f"{antes} -> {depois}"
    )


@pytest.fixture
def snapshot_instance():
    """Permite comparar o estado da pasta instance/ do projeto antes e depois."""
    return lambda: _snapshot(PROJECT_INSTANCE_DIR)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "teste.db"


@pytest.fixture
def app(db_path, tmp_path):
    """Aplicação com base e instance_path temporários, esquema criado.

    Uma base nova começa vazia (como na instalação real). As frações de teste
    são sintéticas e criadas aqui, explicitamente: servem os testes de gestão
    e não pertencem a nenhum condomínio real.
    """
    aplicacao = create_app(
        instance_path=tmp_path / "instance",
        database=db_path,
        testing=True,
    )
    with aplicacao.app_context():
        init_db()
        criar_fracoes_de_teste(aplicacao)
    return aplicacao


# --- Frações sintéticas de teste ----------------------------------------------
# Códigos e permilagens inventados para os testes: soma exatamente 1000,00,
# sem qualquer relação com permilagens reais de um condomínio.
FRACTIONS_DE_TESTE: tuple[tuple[str, int], ...] = (
    ("A", 10000),
    ("B", 12000),
    ("C", 11000),
    ("D", 13000),
    ("E", 12500),
    ("F", 12500),
    ("G", 19000),
    ("H", 10000),
)
PERMILAGENS_DE_TESTE = dict(FRACTIONS_DE_TESTE)


def criar_fracoes_de_teste(app) -> None:
    from app.db import get_db, utc_now_iso

    with app.app_context():
        connection = get_db()
        now = utc_now_iso()
        for code, permilagem in FRACTIONS_DE_TESTE:
            connection.execute(
                "INSERT INTO fractions (code, permilagem, active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (code, permilagem, now, now),
            )
        connection.commit()


# --- Ajuda para a entrega 1: autenticação -------------------------------------

ADMIN_TESTE = ("admin-teste", "palavra-passe-de-teste-10")


def criar_admin(app) -> int:
    """Criar o administrador de teste diretamente na base (o setup HTTP tem teste próprio)."""
    from app.auth import PAPEL_ADMIN, criar_utilizador

    with app.app_context():
        user_id, erros = criar_utilizador(
            ADMIN_TESTE[0], ADMIN_TESTE[1], PAPEL_ADMIN, "Administrador de teste"
        )
    assert not erros, erros
    return int(user_id)


def entrar(app, username: str, palavra_passe: str):
    """Cliente autenticado pelo percurso real de entrada."""
    cliente = app.test_client()
    html = cliente.get("/login").get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, "sem token CSRF em /login"
    cliente.post(
        "/login",
        data={"csrf_token": match.group(1), "utilizador": username, "palavra_passe": palavra_passe},
    )
    return cliente


@pytest.fixture
def admin_teste(app) -> int:
    return criar_admin(app)


@pytest.fixture
def client(app, admin_teste):
    """Cliente autenticado como administrador: as rotas de gestão valem.

    A entrega 1 protege as rotas no servidor. Os testes de gestão partem de uma
    sessão de administrador; os testes de proteção usam `cliente_anonimo` ou
    um cliente de condómino criado pelo próprio teste.
    """
    return entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])


@pytest.fixture
def cliente_anonimo(app):
    """Cliente sem sessão: para provar a proteção no servidor."""
    return app.test_client()


@pytest.fixture
def csrf(client):
    """Extrai o token CSRF do GET correspondente ao formulário."""

    def token(path: str) -> str:
        html = client.get(path).get_data(as_text=True)
        match = CSRF_RE.search(html)
        assert match, f"sem token CSRF em {path}"
        return match.group(1)

    return token


@pytest.fixture
def revisoes(client):
    """Extrai as revisões de concorrência do GET correspondente ao formulário.

    Devolve um dicionário pronto a juntar a um formulário, por exemplo
    `dict(csrf=..., **revisoes("/config"))`.
    """

    def obter(path: str) -> dict[str, str]:
        html = client.get(path).get_data(as_text=True)
        campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
        assert campos, f"sem revisões de concorrência em {path}"
        return campos

    return obter


class BaseDireta:
    """Leitura/escrita direta da base, independente do código da aplicação."""

    def __init__(self, caminho: Path) -> None:
        self.caminho = Path(caminho)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        connection = sqlite3.connect(self.caminho)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(self.caminho)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def scalar(self, sql: str, params: tuple = ()):
        rows = self._query(sql, params)
        if not rows:
            return None
        return list(rows[0].values())[0]

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Várias linhas de uma só leitura (o `scalar` fica para um único valor)."""
        return self._query(sql, params)

    def table_exists(self, nome: str) -> bool:
        row = self._query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (nome,),
        )
        return bool(row)

    def columns(self, tabela: str) -> list[str]:
        return [row["name"] for row in self._query(f"PRAGMA table_info({tabela})")]

    def triggers(self) -> list[str]:
        return [
            row["name"]
            for row in self._query("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ]

    def fractions(self) -> list[dict]:
        return self._query(
            "SELECT id, code, permilagem, active FROM fractions ORDER BY code"
        )

    def fraction(self, code: str) -> dict:
        rows = self._query("SELECT * FROM fractions WHERE code = ?", (code,))
        assert rows, f"a fração {code} não existe"
        return rows[0]

    def permilagens(self) -> dict[str, int]:
        return {row["code"]: row["permilagem"] for row in self.fractions()}

    def total_permilagem(self) -> int:
        return sum(self.permilagens().values())

    def owners(self, code: str | None = None) -> list[dict]:
        sql = (
            "SELECT owners.*, fractions.code AS fraction_code FROM owners "
            "JOIN fractions ON fractions.id = owners.fraction_id"
        )
        params: tuple = ()
        if code:
            sql += " WHERE fractions.code = ?"
            params = (code,)
        sql += " ORDER BY fractions.code, owners.id"
        return self._query(sql, params)

    def settings(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self._query("SELECT key, value FROM settings")
        }

    def cria_tabela_futura(self) -> None:
        """Cria uma tabela de uma fase ainda mais seguinte, para simular histórico.

        Não é `payments`, `charges` nem `expenses`: essas já existem no esquema, e o
        que se quer provar aqui é que a proteção de histórico descobre tabelas criadas
        depois, sem depender de uma lista de nomes escrita à mão.
        """
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS documentos_fase_futura (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fraction_id INTEGER NOT NULL REFERENCES fractions (id),
                amount_cents INTEGER NOT NULL,
                paid_on TEXT NOT NULL
            )
            """
        )

    def insere_registo_futuro(self, fraction_id: int) -> None:
        self.execute(
            "INSERT INTO documentos_fase_futura (fraction_id, amount_cents, paid_on) VALUES (?, ?, ?)",
            (fraction_id, 4700, "2027-01-10"),
        )

    def charges(self, period: str | None = None) -> list[dict]:
        sql = (
            "SELECT charges.*, fractions.code AS fraction_code FROM charges "
            "JOIN fractions ON fractions.id = charges.fraction_id"
        )
        params: tuple = ()
        if period:
            sql += " WHERE charges.period = ?"
            params = (period,)
        return self._query(sql + " ORDER BY charges.period, fractions.code, charges.rubric", params)

    def rules(self) -> list[dict]:
        return self._query(
            "SELECT charge_rules.*, fractions.code AS fraction_code FROM charge_rules "
            "JOIN fractions ON fractions.id = charge_rules.fraction_id "
            "ORDER BY fractions.code, charge_rules.rubric, charge_rules.valid_from"
        )

    def payments(self) -> list[dict]:
        return self._query(
            "SELECT payments.*, fractions.code AS fraction_code FROM payments "
            "JOIN fractions ON fractions.id = payments.fraction_id ORDER BY payments.id"
        )

    def allocations(self) -> list[dict]:
        return self._query(
            "SELECT a.*, "
            "       p.fraction_id AS payment_fraction_id, "
            "       c.period, c.rubric, c.amount_cents AS charge_amount_cents "
            "FROM payment_allocations a "
            "JOIN payments p ON p.id = a.payment_id "
            "JOIN charges c ON c.id = a.charge_id "
            "ORDER BY a.id"
        )

    def expenses(self) -> list[dict]:
        return self._query(
            "SELECT e.*, c.nome AS category_name FROM expenses e "
            "JOIN expense_categories c ON c.id = e.category_id ORDER BY e.id"
        )

    def expense_payments(self) -> list[dict]:
        return self._query("SELECT * FROM expense_payments ORDER BY id")


@pytest.fixture
def db(app, db_path) -> BaseDireta:
    """Base de teste aberta diretamente (depende do fixture app)."""
    return BaseDireta(db_path)


@pytest.fixture
def app_vazia(tmp_path):
    """Aplicação com base completamente vazia: o percurso de configuração inicial."""
    aplicacao = create_app(
        instance_path=tmp_path / "instancia-vazia",
        database=tmp_path / "vazia.db",
        testing=True,
    )
    with aplicacao.app_context():
        init_db()
    return aplicacao


@pytest.fixture
def db_vazia(app_vazia, tmp_path) -> BaseDireta:
    return BaseDireta(tmp_path / "vazia.db")


@pytest.fixture
def admin_vazio(app_vazia) -> int:
    return criar_admin(app_vazia)


@pytest.fixture
def base_direta():
    """Abre uma base qualquer (por exemplo a base de demonstração)."""
    return lambda caminho: BaseDireta(Path(caminho))


# --- Ajuda para a fase 2: regras de quotas e geração -------------------------


def protecao_de(client, caminho: str) -> dict[str, str]:
    """Token CSRF, revisões de concorrência e marca de formulário, tal como apresentados."""
    html = client.get(caminho).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {caminho}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisões de concorrência em {caminho}"
    marca = TOKEN_RE.search(html)
    if marca is not None:
        campos["form_token"] = marca.group(1)
    return {"csrf_token": match.group(1), **campos}


def csrf_de(client, caminho: str) -> str:
    """Só o token CSRF de uma página sem revisões de concorrência (entrada, conta)."""
    html = client.get(caminho).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {caminho}"
    return match.group(1)


PALAVRA_CONDOMINO_TESTE = "palavra-passe-de-condomino-10"


def criar_condomino(app, db, username: str, codigos: tuple[str, ...] = ()) -> int:
    """Criar um condómino de teste associado às frações indicadas."""
    from app.auth import PAPEL_CONDOMINO, criar_utilizador

    with app.app_context():
        user_id, erros = criar_utilizador(
            username,
            PALAVRA_CONDOMINO_TESTE,
            PAPEL_CONDOMINO,
            "",
            tuple(db.fraction(code)["id"] for code in codigos),
        )
    assert not erros, erros
    return int(user_id)


def entrar_condomino(app, username: str):
    return entrar(app, username, PALAVRA_CONDOMINO_TESTE)


def registar_regra(
    client,
    db,
    codigos: list[str],
    rubrica: str,
    valor: str,
    inicio: str,
    fim: str = "",
    fonte: str = "Ensaio de teste",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter o formulário de nova regra, com as frações pedidas."""
    form = {
        "rubrica": rubrica,
        "valor": valor,
        "inicio": inicio,
        "fim": fim,
        "source": fonte,
    }
    if confirmar:
        form["confirmar_vigencia"] = "on"
    for code in codigos:
        form[f"fracao_{db.fraction(code)['id']}"] = "on"
    form.update(protecao or protecao_de(client, "/quotas"))
    return client.post("/quotas/add", data=form)


def configurar_regras_de_teste(client, db, protecao: dict[str, str] | None = None):
    """Regras de quota sintéticas de teste: 40,00 / 45,00 e reforço de 3,00.

    Valores inventados para os testes, sem relação com quotas reais de um
    condomínio. Vigência 01/2027 a 12/2027, confirmada.
    """
    registar_regra(client, db, ["A", "C", "E", "G"], "quota_ordinaria", "40,00",
                   "01/2027", "", protecao=protecao)
    registar_regra(client, db, ["B", "D", "F", "H"], "quota_ordinaria", "45,00",
                   "01/2027", "")
    registar_regra(client, db, ["A", "B", "C", "D", "E", "F", "G", "H"], "reforco_fundo",
                   "3,00", "01/2027", "12/2027")


def gerar_mes(
    client,
    period: str,
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
    app=None,
):
    """Confirmar a geração de um mês, depois de a pré-visualizar.

    Quando a pré-visualização nem chega a oferecer a confirmação (porque o mês não
    pode ser gerado), a revisão é calculada diretamente: assim o pedido submetido
    está atualizado e a recusa acontece pelo motivo certo.
    """
    form = {"period": period}
    if confirmar:
        form["confirmar_geracao"] = "on"

    if protecao is None:
        # O token CSRF é da sessão: vale para qualquer página do mesmo pedido.
        protecao = protecao_de(client, "/quotas")
        html = client.get(f"/quotas/generate?period={period}").get_data(as_text=True)
        protecao.update({nome: valor for nome, valor in REVISAO_RE.findall(html)})
        if "revisao_geracao" not in protecao:
            from app.concurrency import revisao_geracao

            with app.app_context():
                protecao["revisao_geracao"] = revisao_geracao()

    return client.post("/quotas/generate", data={**form, **protecao})


def alterar_config(client, db, **alteracoes):
    """Submeter o formulário de configuração com os valores atuais, mais as alterações."""
    valores = db.settings()
    form = {
        chave: valores[chave]
        for chave in (
            "condominio_nome",
            "condominio_morada",
            "dividas_abertura_notas",
            "gestao_data_inicio",
            "quota_dia_vencimento",
            "saldo_inicial_caixa_centimos",
            "saldo_inicial_fundo_centimos",
        )
    }
    form.update(protecao_de(client, "/config"))
    form.update(alteracoes)
    return client.post("/config", data=form)


def previsualizar(client, period: str) -> str:
    return client.get(f"/quotas/generate?period={period}").get_data(as_text=True)


# --- Ajuda para a fase 3: pagamentos, afetações, créditos e anulações ---------


@pytest.fixture
def hoje(monkeypatch) -> str:
    """Fixar o "hoje" da aplicação.

    As datas de pagamento, o estado "vencida" e a data limite do painel dependem do
    dia real. Fixar o dia torna os testes determinísticos: 01/02/2027, depois das
    cobranças de janeiro.
    """
    data = "2027-02-01"
    monkeypatch.setattr("app.payments.today_iso", lambda: data)
    monkeypatch.setattr("app.despesas.today_iso", lambda: data)
    monkeypatch.setattr("app.dashboard.today_iso", lambda: data)
    return data


def regras_e_mes(client, db, period: str = "2027-01") -> str:
    """Ponto de partida dos testes de pagamentos: regras de teste e um mês gerado."""
    configurar_regras_de_teste(client, db)
    gerar_mes(client, period)
    return period


def registar_pagamento(
    client,
    db,
    code: str,
    valor: str,
    data: str = "15/01/2027",
    meio: str = "transferencia",
    referencia: str = "",
    nota: str = "",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter o formulário de novo pagamento."""
    form = {
        "fracao": db.fraction(code)["id"],
        "valor": valor,
        "data": data,
        "meio": meio,
        "referencia": referencia,
        "source": nota,
    }
    if confirmar:
        form["confirmar_pagamento"] = "on"
    form.update(protecao or protecao_de(client, "/payments/add"))
    return client.post("/payments/add", data=form)


def ultimo_pagamento(db) -> dict:
    rows = db.payments()
    assert rows, "não há pagamentos registados"
    return max(rows, key=lambda row: row["id"])


def id_da_cobranca(db, code: str, period: str, rubrica: str = "quota_ordinaria") -> int:
    linhas = [
        c
        for c in db.charges(period)
        if c["fraction_code"] == code and c["rubric"] == rubrica
    ]
    assert linhas, f"não há cobrança de {rubrica} da fração {code} em {period}"
    return int(linhas[0]["id"])


def liquidado_da_cobranca(db, charge_id: int) -> int:
    """Quanto de uma cobrança está liquidado por pagamentos que não foram anulados."""
    return int(
        db.scalar(
            """
            SELECT COALESCE(SUM(a.amount_cents), 0)
            FROM payment_allocations AS a
            JOIN payments AS p ON p.id = a.payment_id
            WHERE a.charge_id = ? AND p.cancelled_at IS NULL
            """,
            (charge_id,),
        )
        or 0
    )


def afetar_pagamento(
    client,
    payment_id: int,
    afetoes: dict[int, str],
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter a distribuição de um pagamento por cobranças: {charge_id: 'euros'}."""
    form = {f"afecao_{charge_id}": valor for charge_id, valor in afetoes.items()}
    if confirmar:
        form["confirmar_afetacao"] = "on"
    form.update(protecao or protecao_de(client, f"/payments/{payment_id}"))
    return client.post(f"/payments/{payment_id}/allocate", data=form)


def anular_pagamento(
    client,
    payment_id: int,
    motivo: str = "Movimento registado por engano",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    form = {"motivo": motivo}
    if confirmar:
        form["confirmar_anular"] = "on"
    form.update(protecao or protecao_de(client, f"/payments/{payment_id}"))
    return client.post(f"/payments/{payment_id}/cancel", data=form)


# --- Ajuda para a entrega 2: quota extraordinária ----------------------------


def registar_quota_extraordinaria(
    client,
    db,
    codigos: list[str],
    total: str,
    datas: tuple[str, ...] = ("15/03/2027",),
    descricao: str = "Reparação da cobertura",
    finalidade: str = "",
    reparticao: str = "igual",
    manuais: dict[str, str] | None = None,
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter o formulário de quota extraordinária.

    `confirmar=False` pré-visualiza: nada é gravado e a marca do formulário é a mesma
    que um envio confirmado usaria, o que permite provar o reenvio.
    """
    form = {
        "descricao": descricao,
        "finalidade": finalidade,
        "total": total,
        "reparticao": reparticao,
        "source": "Ensaio de teste",
    }
    for code in codigos:
        form[f"fracao_{db.fraction(code)['id']}"] = "on"
    for indice, data in enumerate(datas, start=1):
        form[f"prestacao_{indice}"] = data
    for code, valor in (manuais or {}).items():
        form[f"manual_{db.fraction(code)['id']}"] = valor
    if confirmar:
        form["confirmar_extraordinaria"] = "on"
        form["confirmar_emissao"] = "on"
    else:
        form["previsualizar"] = "on"
    form.update(protecao or protecao_de(client, "/quotas/extraordinaria"))
    return client.post("/quotas/extraordinaria", data=form)


def cobrancas_extraordinarias(db) -> list[dict]:
    return [c for c in db.charges() if c["rubric"] == "quota_extraordinaria"]


# --- Ajuda para a entrega 2: correção de cobranças ---------------------------


def corrigir_cobranca(
    client,
    db,
    charge_id: int,
    valor: str | None = None,
    vencimento: str | None = None,
    descricao: str | None = None,
    motivo: str | None = "Registada com valor errado",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter a correção de uma cobrança, partindo do que está guardado.

    Só muda o que for passado explicitamente, por isso uma recusa acontece pelo motivo
    que o teste quer provar. `confirmar=False` pré-visualiza os efeitos.
    """
    caminho = f"/charges/{charge_id}/corrigir"
    guardado = db.rows(
        "SELECT amount_cents, due_date, descricao FROM charges WHERE id = ?", (charge_id,)
    )
    assert guardado, f"não há cobrança {charge_id}"
    atual = guardado[0]

    form = {
        "valor": valor if valor is not None else format_money_input(int(atual["amount_cents"])),
        "vencimento": vencimento
        if vencimento is not None
        else (format_date(atual["due_date"]) if atual["due_date"] else ""),
        "descricao": descricao if descricao is not None else (atual["descricao"] or ""),
    }
    if motivo is not None:
        form["motivo"] = motivo
    if confirmar:
        form["confirmar_efectos"] = "on"
        form["confirmar_correcao"] = "on"
    else:
        form["previsualizar"] = "on"
    form.update(protecao or protecao_de(client, caminho))
    return client.post(caminho, data=form)


def mudar_valor_de_regra(client, db, rule_id: int, valor: str, motivo: str = "Deliberado em assembleia"):
    """Submeter a mudança de valor de uma regra, com motivo e confirmação."""
    return client.post(
        f"/quotas/{rule_id}/valor",
        data={
            **protecao_de(client, "/quotas"),
            "valor": valor,
            "motivo": motivo,
            "confirmar_valor": "on",
        },
    )


def id_da_regra_da_fracao(db, code: str, rubrica: str = "quota_ordinaria") -> int:
    linhas = [r for r in db.rules() if r["fraction_code"] == code and r["rubric"] == rubrica]
    assert linhas, f"não há regra de {rubrica} para a fração {code}"
    return int(linhas[0]["id"])


# --- Ajuda para a correção de pagamentos -------------------------------------


def protecao_de_edicao(client, payment_id: int) -> dict[str, str]:
    """CSRF, revisão de concorrência e marca do formulário de correção."""
    caminho = f"/payments/{payment_id}/edit"
    html = client.get(caminho).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {caminho}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisões de concorrência em {caminho}"
    marca = EDIT_TOKEN_RE.search(html)
    assert marca, f"sem marca de formulário em {caminho}"
    return {"csrf_token": match.group(1), "edit_token": marca.group(1), **campos}


def distribuicao_atual(db, payment_id: int) -> dict[int, str]:
    """Distribuição guardada de um pagamento, como o formulário a apresenta."""
    return {
        int(a["charge_id"]): format_money_input(int(a["amount_cents"]))
        for a in db.allocations()
        if a["payment_id"] == payment_id
    }


def editar_pagamento(
    client,
    db,
    payment_id: int,
    code: str | None = None,
    valor: str | None = None,
    data: str | None = None,
    meio: str | None = None,
    referencia: str | None = None,
    nota: str | None = None,
    motivo: str | None = "Correção de teste",
    afetoes: dict[int, str] | None = None,
    confirmar: bool = True,
    previsualizar: bool = False,
    confirmar_fracao: bool = True,
    confirmar_retirada: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter a correção de um pagamento, partindo do que está guardado.

    O formulário é montado como o navegador o envia: os campos vêm com os valores
    atuais e a distribuição atual vai preenchida. Só muda o que for passado
    explicitamente, por isso uma recusa acontece pelo motivo que o teste quer provar.
    `afetoes={}` equivale a deixar todos os campos de afetação vazios: retirar a
    distribuição inteira (com a confirmação própria, como a página exige).
    Uma cobrança em `antes` que não apareça em `afetoes` vai como campo vazio
    (retirada explícita), porque o navegador envia sempre todos os campos.
    """
    guardado = next((p for p in db.payments() if p["id"] == payment_id), None)
    assert guardado is not None, f"não há pagamento {payment_id}"

    form = {
        "fracao": db.fraction(code)["id"] if code else guardado["fraction_id"],
        "valor": valor if valor is not None else format_money_input(int(guardado["amount_cents"])),
        "data": data if data is not None else format_date(guardado["paid_on"]),
        "meio": meio if meio is not None else guardado["method"],
        "referencia": referencia if referencia is not None else (guardado["reference"] or ""),
        "source": nota if nota is not None else (guardado["source"] or ""),
    }
    if motivo is not None:
        form["motivo"] = motivo

    guardada = distribuicao_atual(db, payment_id)
    distribuicao = dict(guardada) if afetoes is None else {charge_id: "" for charge_id in guardada}
    if afetoes is not None:
        distribuicao.update(afetoes)
    for charge_id, valor_afetacao in distribuicao.items():
        form[f"afecao_{charge_id}"] = valor_afetacao

    if confirmar:
        form["confirmar_edicao"] = "on"
    if previsualizar:
        form["previsualizar"] = "on"
    if confirmar_fracao:
        form["confirmar_mudanca_de_fracao"] = "on"
    if confirmar_retirada:
        form["confirmar_retirada_distribuicao"] = "on"

    form.update(protecao or protecao_de_edicao(client, payment_id))
    return client.post(f"/payments/{payment_id}/edit", data=form)


# --- Ajuda para a entrega 3: despesas, pagamentos parciais e correções --------


def protecao_de_edicao_em(client, caminho: str) -> dict[str, str]:
    """CSRF, revisão de concorrência e marca de correção de uma página de edição."""
    html = client.get(caminho).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {caminho}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisões de concorrência em {caminho}"
    marca = EDIT_TOKEN_RE.search(html)
    assert marca, f"sem marca de correção em {caminho}"
    return {"csrf_token": match.group(1), "edit_token": marca.group(1), **campos}


def criar_categoria(client, nome: str, protecao: dict[str, str] | None = None):
    return client.post(
        "/despesas/categorias",
        data={
            "acao": "criar",
            "nome": nome,
            "categoria_id": "",
            **(protecao or protecao_de(client, "/despesas/categorias")),
        },
    )


def id_da_categoria(db, nome: str) -> int:
    linhas = db.rows("SELECT id FROM expense_categories WHERE nome = ?", (nome,))
    assert linhas, f"não há categoria «{nome}»"
    return int(linhas[0]["id"])


def categoria_de_trabalho(client, db, nome: str = "Limpeza") -> int:
    """Garantir que a categoria existe e devolver o seu id."""
    linhas = db.rows("SELECT id FROM expense_categories WHERE nome = ?", (nome,))
    if not linhas:
        criar_categoria(client, nome)
        linhas = db.rows("SELECT id FROM expense_categories WHERE nome = ?", (nome,))
    assert linhas
    return int(linhas[0]["id"])


def ultima_despesa(db) -> dict:
    rows = db.expenses()
    assert rows, "não há despesas registadas"
    return max(rows, key=lambda row: row["id"])


def ultimo_pagamento_de_despesa(db) -> dict:
    rows = db.expense_payments()
    assert rows, "não há pagamentos de despesa registados"
    return max(rows, key=lambda row: row["id"])


def registar_despesa(
    client,
    db,
    descricao: str = "Limpeza do hall",
    valor: str = "120,00",
    categoria: int | None = None,
    data: str = "15/01/2027",
    vencimento: str = "",
    fornecedor: str = "",
    referencia: str = "",
    classificacao: str = "ordinario",
    notas: str = "",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter o formulário de nova despesa."""
    form = {
        "descricao": descricao,
        "fornecedor": fornecedor,
        "categoria": categoria if categoria is not None else categoria_de_trabalho(client, db),
        "referencia": referencia,
        "data": data,
        "vencimento": vencimento,
        "valor": valor,
        "classificacao": classificacao,
        "notas": notas,
    }
    if confirmar:
        form["confirmar_despesa"] = "on"
    form.update(protecao or protecao_de(client, "/despesas/nova"))
    return client.post("/despesas/nova", data=form)


def pagar_despesa(
    client,
    db,
    expense_id: int,
    valor: str,
    data: str = "20/01/2027",
    meio: str = "transferencia",
    referencia: str = "",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    """Submeter um pagamento (parcial ou total) de uma despesa."""
    form = {"valor": valor, "data": data, "meio": meio, "referencia": referencia}
    if confirmar:
        form["confirmar_pagamento"] = "on"
    form.update(protecao or protecao_de(client, f"/despesas/{expense_id}"))
    return client.post(f"/despesas/{expense_id}/pagar", data=form)


def anular_despesa(
    client,
    expense_id: int,
    motivo: str = "Despesa registada por engano",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    form = {"motivo": motivo}
    if confirmar:
        form["confirmar_anular"] = "on"
    form.update(protecao or protecao_de(client, f"/despesas/{expense_id}"))
    return client.post(f"/despesas/{expense_id}/anular", data=form)


def editar_despesa(
    client,
    db,
    expense_id: int,
    descricao: str | None = None,
    valor: str | None = None,
    categoria: int | None = None,
    data: str | None = None,
    vencimento: str | None = None,
    fornecedor: str | None = None,
    referencia: str | None = None,
    classificacao: str | None = None,
    notas: str | None = None,
    motivo: str | None = "Registada com valor errado",
    confirmar: bool = True,
    previsualizar: bool = False,
    protecao: dict[str, str] | None = None,
):
    """Submeter a correção de uma despesa, partindo do que está guardado.

    Só muda o que for passado explicitamente; `confirmar=False` e `previsualizar=True`
    mostram os efeitos sem gravar.
    """
    caminho = f"/despesas/{expense_id}/editar"
    guardado = ultima_despesa(db) if expense_id is None else next(
        (e for e in db.expenses() if e["id"] == expense_id), None
    )
    assert guardado is not None, f"não há despesa {expense_id}"

    form = {
        "descricao": descricao if descricao is not None else guardado["descricao"],
        "fornecedor": fornecedor if fornecedor is not None else (guardado["fornecedor"] or ""),
        "categoria": categoria if categoria is not None else guardado["category_id"],
        "referencia": referencia if referencia is not None else (guardado["referencia"] or ""),
        "data": data if data is not None else format_date(guardado["data"]),
        "vencimento": (
            vencimento
            if vencimento is not None
            else (format_date(guardado["due_date"]) if guardado["due_date"] else "")
        ),
        "valor": valor if valor is not None else format_money_input(int(guardado["amount_cents"])),
        "classificacao": classificacao if classificacao is not None else guardado["classificacao"],
        "notas": notas if notas is not None else (guardado["notas"] or ""),
    }
    if motivo is not None:
        form["motivo"] = motivo
    if confirmar:
        form["confirmar_efectos"] = "on"
        form["confirmar_correcao"] = "on"
    if previsualizar:
        form["previsualizar"] = "on"
    form.update(protecao or protecao_de_edicao_em(client, caminho))
    return client.post(caminho, data=form)


def editar_pagamento_de_despesa(
    client,
    db,
    payment_id: int,
    valor: str | None = None,
    data: str | None = None,
    meio: str | None = None,
    referencia: str | None = None,
    motivo: str | None = "Pagamento registado com valor errado",
    confirmar: bool = True,
    previsualizar: bool = False,
    protecao: dict[str, str] | None = None,
):
    """Submeter a correção de um pagamento de despesa, partindo do que está guardado."""
    caminho = f"/despesas/pagamentos/{payment_id}/editar"
    guardado = next((p for p in db.expense_payments() if p["id"] == payment_id), None)
    assert guardado is not None, f"não há pagamento de despesa {payment_id}"

    form = {
        "valor": valor if valor is not None else format_money_input(int(guardado["amount_cents"])),
        "data": data if data is not None else format_date(guardado["paid_on"]),
        "meio": meio if meio is not None else guardado["method"],
        "referencia": referencia if referencia is not None else (guardado["reference"] or ""),
    }
    if motivo is not None:
        form["motivo"] = motivo
    if confirmar:
        form["confirmar_efectos"] = "on"
        form["confirmar_correcao"] = "on"
    if previsualizar:
        form["previsualizar"] = "on"
    form.update(protecao or protecao_de_edicao_em(client, caminho))
    return client.post(caminho, data=form)


def anular_pagamento_de_despesa(
    client,
    payment_id: int,
    motivo: str = "Pagamento registado por engano",
    confirmar: bool = True,
    protecao: dict[str, str] | None = None,
):
    form = {"motivo": motivo}
    if confirmar:
        form["confirmar_anular"] = "on"
    form.update(
        protecao or protecao_de(client, f"/despesas/pagamentos/{payment_id}/editar")
    )
    return client.post(f"/despesas/pagamentos/{payment_id}/anular", data=form)
