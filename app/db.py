"""Ligação ao SQLite, criação do esquema e semente inicial não destrutiva."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Uma base nova começa vazia: sem frações, sem permilagens, sem regras, sem
# movimentos. Tudo o que diz respeito a um condomínio concreto é configurado
# pelo administrador através da interface. Os dados sintéticos de teste e de
# demonstração vivem nos testes e em `app/demo.py`, nunca aqui.

# Valor vazio significa "por confirmar": nunca é apresentado como valor definitivo.
DEFAULT_SETTINGS: dict[str, str] = {
    "condominio_nome": "",
    "condominio_morada": "",
    "quota_dia_vencimento": "",
    "gestao_data_inicio": "",
    "saldo_inicial_caixa_centimos": "",
    "saldo_inicial_fundo_centimos": "",
    "dividas_abertura_notas": "",
}

# Identificadores de tabela aceites na deteção de referências (nunca texto do utilizador).
IDENTIFICADOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Versão do esquema, guardada em `PRAGMA user_version`. Sobe sempre que o esquema
# muda, porque `CREATE TABLE IF NOT EXISTS` nunca corrige uma tabela já existente:
# sem versão, uma base antiga ficaria com restrições antigas sem qualquer aviso.
# v8 (entrega 3): despesas, pagamentos de despesas, categorias e respetivos rastos.
# Só tabelas e travões novos: nenhuma tabela existente é tocada pela migração.
ESQUEMA_VERSAO = 11

# `PRAGMA foreign_keys` é por ligação, não parte do esquema: é ativado aqui,
# em cada ligação, e por isso não está em `schema.sql`.
CONNECT_TIMEOUT_SEGUNDOS = 10


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = Path(current_app.config["DATABASE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=CONNECT_TIMEOUT_SEGUNDOS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        g.db = connection
    return g.db


def close_db(exception: Exception | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def query_db(sql: str, params: tuple = (), one: bool = False):
    cursor = get_db().execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute_db(sql: str, params: tuple = ()) -> int:
    connection = get_db()
    cursor = connection.execute(sql, params)
    connection.commit()
    lastrowid = cursor.lastrowid
    cursor.close()
    return lastrowid


def table_exists(table: str) -> bool:
    row = query_db(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
        one=True,
    )
    return row is not None


def get_setting(key: str) -> str:
    row = query_db("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is None:
        return DEFAULT_SETTINGS.get(key, "")
    return row["value"]


def get_all_settings() -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for row in query_db("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


_UPSERT_SETTING = """
    INSERT INTO settings (key, value, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """


def set_settings(values: dict[str, str]) -> None:
    """Gravar vários valores de configuração numa única transação.

    Uma falha a meio reverte tudo: não ficam alterações parciais.
    """
    connection = get_db()
    now = utc_now_iso()
    try:
        for key, value in values.items():
            connection.execute(_UPSERT_SETTING, (key, value, now))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def fraction_references(fraction_id: int) -> list[str]:
    """Tabelas com registos a apontar para esta fração, descobertas pelas chaves estrangeiras.

    Não depende de uma lista de nomes mantida à mão: qualquer tabela criada numa fase
    seguinte com `REFERENCES fractions (id)` passa a ser considerada automaticamente.
    """
    referencias: list[str] = []
    tabelas = query_db(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('fractions')"
    )
    for tabela in tabelas:
        nome = tabela["name"]
        if not IDENTIFICADOR_RE.match(nome):
            continue
        for chave in query_db(f'PRAGMA foreign_key_list("{nome}")'):
            if chave["table"] != "fractions" or not chave["from"]:
                continue
            # `from` é a coluna desta tabela; `to` é a coluna de fractions.
            coluna = chave["from"]
            if not IDENTIFICADOR_RE.match(coluna):
                continue
            existe = query_db(
                f'SELECT 1 FROM "{nome}" WHERE "{coluna}" = ? LIMIT 1',
                (fraction_id,),
                one=True,
            )
            if existe is not None:
                referencias.append(nome)
                break
    return referencias


# Tabelas de histórico próprio da fase 1: contam como registo associado,
# mas não como movimento financeiro. As associações de utilizadores são
# administrativas: também não são movimento financeiro.
HISTORICO_PROPRIO = ("owners", "permilagem_history", "user_fractions")


def has_financial_history(fraction_id: int) -> bool:
    """Verificar se a fração participa em movimentos financeiros registados."""
    return any(nome not in HISTORICO_PROPRIO for nome in fraction_references(fraction_id))


def has_permilagem_history(fraction_id: int) -> bool:
    return "permilagem_history" in fraction_references(fraction_id)


def has_owner_history(fraction_id: int) -> bool:
    return "owners" in fraction_references(fraction_id)


def _sem_comentarios(linha: str) -> str:
    """Remover um comentário `--`, sem tocar em texto dentro de aspas simples."""
    partes: list[str] = []
    em_aspas = False
    indice = 0
    while indice < len(linha):
        caractere = linha[indice]
        if caractere == "'":
            em_aspas = not em_aspas
            partes.append(caractere)
        elif caractere == "-" and linha[indice : indice + 2] == "--" and not em_aspas:
            break
        else:
            partes.append(caractere)
        indice += 1
    return "".join(partes)


def declaracoes_do_esquema(sql: str) -> list[str]:
    """Dividir o ficheiro do esquema em declarações completas.

    `executescript` corre statement a statement e confirma antes de cada DDL,
    por isso não serve para uma inicialização atómica.
    """
    declaracoes: list[str] = []
    atual = ""
    for linha in sql.splitlines():
        atual += _sem_comentarios(linha) + "\n"
        if sqlite3.complete_statement(atual):
            if atual.strip():
                declaracoes.append(atual.strip())
            atual = ""
    if atual.strip():
        declaracoes.append(atual.strip())
    return declaracoes


def _normalizar_ddl(texto: str) -> str:
    """Comparar DDL sem depender de espaçamento, `IF NOT EXISTS` ou ponto e vírgula."""
    return " ".join(texto.replace("IF NOT EXISTS", "").strip().rstrip(";").split())


def _ddl_no_esquema(declaracoes: list[str], tabela: str) -> str | None:
    for declaracao in declaracoes:
        if re.match(rf"CREATE TABLE (IF NOT EXISTS )?{tabela}\b", declaracao, re.IGNORECASE):
            return declaracao
    return None


def _colunas(connection: sqlite3.Connection, tabela: str) -> list[str]:
    return [row["name"] for row in connection.execute(f'PRAGMA table_info("{tabela}")')]


def _soltar_dependencias(connection: sqlite3.Connection, tabela: str) -> None:
    """Remover os índices e travões da tabela antes de ela ser renomeada.

    Renomear leva os índices e travões consigo para a tabela `_antiga`, e `DROP`
    apaga-os. Sem este passo, o `CREATE ... IF NOT EXISTS` que vem a seguir vê o nome
    ainda ocupado, não cria nada, e a tabela nova fica sem proteção.
    Os índices implícitos de `UNIQUE` e de chave primária não são removíveis à mão.
    """
    # As linhas são lidas todas antes de qualquer `DROP`: mexer no esquema enquanto
    # uma leitura de `sqlite_master` está aberta fecha com "database table is locked".
    dependencias = list(
        connection.execute(
            "SELECT name, type FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger')",
            (tabela,),
        )
    )
    for linha in dependencias:
        nome = linha["name"]
        if not IDENTIFICADOR_RE.match(nome) or nome.startswith("sqlite_autoindex"):
            continue
        comando = "DROP INDEX" if linha["type"] == "index" else "DROP TRIGGER"
        connection.execute(f'{comando} "{nome}"')


def _reconstruir_tabela(connection: sqlite3.Connection, tabela: str, ddl_novo: str) -> None:
    """Recriar uma tabela com o DDL atual, conservando as linhas e colunas comuns.

    SQLite não permite alterar um CHECK existente; reconstruir é o único caminho.
    """
    _soltar_dependencias(connection, tabela)
    colunas_antigas = _colunas(connection, tabela)
    connection.execute(f'ALTER TABLE "{tabela}" RENAME TO "{tabela}_antiga"')
    connection.execute(ddl_novo)
    colunas_novas = _colunas(connection, tabela)
    comuns = [coluna for coluna in colunas_antigas if coluna in colunas_novas]
    lista = ", ".join(f'"{coluna}"' for coluna in comuns)
    connection.execute(f'INSERT INTO "{tabela}" ({lista}) SELECT {lista} FROM "{tabela}_antiga"')
    connection.execute(f'DROP TABLE "{tabela}_antiga"')


# Nenhuma outra tabela tem chave estrangeira para estas: reconstruir é seguro.
TABELAS_RECONSTRUIVEIS = ("settings", "owners", "permilagem_history", "documento_ligacoes")

# Estas são referenciadas por chaves estrangeiras de outras tabelas
# (`charges.rule_id`, `payment_allocations.charge_id`, `documento_*.documento_id`).
# Reconstruí-las exige `PRAGMA legacy_alter_table = ON` com as chaves estrangeiras
# desligadas, para que as tabelas-filhas continuem a apontar pelo nome original.
# Ver `init_db`.
TABELAS_RECONSTRUIDAS_COM_FILHAS = ("charge_rules", "charges", "documentos")

# Gatilhos aposentados na v5: a imutabilidade absoluta passou a correção justificada.
# Retirá-los sem mais nada deixaria a base sem proteção; as substituições estão todas
# em `schema.sql` e são criadas logo a seguir por `init_db`.
# Na v7, `charges_sem_update` (que recusava qualquer UPDATE) dá lugar a
# `cobrancas_so_se_corrigem_com_motivo`, que só deixa passar a correção justificada.
GATILHOS_APOSENTADOS = {
    5: ("pagamentos_sem_alterar", "afecoes_sem_update", "afecoes_sem_delete"),
    7: ("charges_sem_update",),
}

# Colunas acrescentadas a tabelas que outra tabela referencia por chave estrangeira:
# reconstruir não é possível, e `ALTER TABLE ... ADD COLUMN` é não destrutivo.
COLUNAS_ACRESCENTADAS = {
    5: {"payments": (("edited_at", "TEXT"), ("edit_reason", "TEXT"))},
}


def _migrar_colunas(
    connection: sqlite3.Connection, versao: int, declaracoes: list[str]
) -> list[str]:
    """Acrescentar colunas novas a tabelas existentes, sem as reconstruir.

    `CREATE TABLE IF NOT EXISTS` nunca acrescenta uma coluna a uma tabela já criada.
    Só é feito nas versões estritamente posteriores à guardada: uma base da v4 ganha
    as colunas da v5; uma base da v5 não volta a tentar acrescentá-las.
    """
    feitos: list[str] = []
    for alvo, colunas in sorted(COLUNAS_ACRESCENTADAS.items()):
        if versao >= alvo:
            continue
        for tabela, novas in colunas.items():
            guardado = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (tabela,),
            ).fetchone()
            if guardado is None:
                continue  # ainda não existe: as declarações criam-na com as colunas novas
            existentes = _colunas(connection, tabela)
            for nome, tipo in novas:
                if nome in existentes:
                    continue
                connection.execute(f'ALTER TABLE "{tabela}" ADD COLUMN "{nome}" {tipo}')
            feitos.append(tabela)
    return feitos


def _migrar_gatilhos_aposentados(connection: sqlite3.Connection, versao: int) -> list[str]:
    """Remover travões que a versão nova substituiu por travões equivalentes.

    A ordem importa: `init_db` executa estas remoções antes das declarações do
    esquema, que criam os travões novos na mesma transação. Se qualquer passo
    falhar, a transação é anulada e a base fica exatamente como estava.
    """
    removidos: list[str] = []
    for alvo, nomes in sorted(GATILHOS_APOSENTADOS.items()):
        if versao >= alvo:
            continue
        for nome in nomes:
            existe = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (nome,),
            ).fetchone()
            if existe is None:
                continue
            connection.execute(f"DROP TRIGGER {nome}")
            removidos.append(nome)
    return removidos


def _reconstrucoes_necessarias(
    connection: sqlite3.Connection, declaracoes: list[str]
) -> list[tuple[str, str]]:
    """Tabelas existentes cujo DDL guardado já não é o do esquema atual.

    É uma leitura: pode ser feita antes da transação, porque é ela que decide se a
    migração precisa de mexer nas chaves estrangeiras.
    """
    necessarias: list[tuple[str, str]] = []
    for tabela in TABELAS_RECONSTRUIVEIS + TABELAS_RECONSTRUIDAS_COM_FILHAS:
        guardado = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (tabela,),
        ).fetchone()
        if guardado is None or guardado[0] is None:
            continue  # ainda não existe: as declarações criam-na com o DDL atual
        esperado = _ddl_no_esquema(declaracoes, tabela)
        if esperado is None or _normalizar_ddl(guardado[0]) == _normalizar_ddl(esperado):
            continue
        necessarias.append((tabela, esperado))
    return necessarias


def _migrar_esquema(
    connection: sqlite3.Connection,
    declaracoes: list[str],
    reconstrucoes: list[tuple[str, str]],
) -> list[str]:
    """Aplicar as migrações: colunas novas, travões aposentados, tabelas reconstruídas.

    `CREATE TABLE IF NOT EXISTS` nunca corrige uma tabela já criada: sem esta
    verificação, uma base antiga ficaria com as restrições antigas sem aviso.
    Devolve os nomes das tabelas reconstruídas.
    """
    versao = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if versao > ESQUEMA_VERSAO:
        raise RuntimeError(
            f"A base tem esquema v{versao}, mais recente do que esta aplicação "
            f"(v{ESQUEMA_VERSAO}); nada foi alterado."
        )

    _migrar_colunas(connection, versao, declaracoes)
    _migrar_gatilhos_aposentados(connection, versao)
    # Ordem com sentido: primeiro os pais (`charge_rules`), depois quem os referencia.
    ordem = {nome: indice for indice, nome in enumerate(TABELAS_RECONSTRUIVEIS + TABELAS_RECONSTRUIDAS_COM_FILHAS)}
    migradas: list[str] = []
    for tabela, ddl_novo in sorted(reconstrucoes, key=lambda item: ordem.get(item[0], 999)):
        _reconstruir_tabela(connection, tabela, ddl_novo)
        migradas.append(tabela)
    return migradas


def _nome_de_tabela(declaracao: str) -> str | None:
    """Nome da tabela criada por uma declaração `CREATE TABLE`, se for uma."""
    match = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?([A-Za-z_][A-Za-z0-9_]*)", declaracao, re.IGNORECASE)
    return match.group(1) if match else None


def _tabela_existe(connection: sqlite3.Connection, tabela: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tabela,)
        ).fetchone()
        is not None
    )


def init_db() -> None:
    """Criar esquema, migrar e semear numa única transação, sem destruir dados existentes.

    Se qualquer passo falhar, nada fica criado nem alterado: nem tabelas a meio do
    esquema, nem metade da semente, nem uma migração a meio. As tabelas existentes
    ficam exatamente como estavam.

    Reconstruir `charge_rules` ou `charges` mexe em tabelas que outra tabela
    referencia. Os dois `PRAGMA` necessários só têm efeito fora da transação — dentro
    são ignorados — por isso são postos antes de `BEGIN` e repostos no fim:
      * `foreign_keys = OFF`: sem ele, `DROP` da tabela antiga tenta apagar as linhas
        que as tabelas-filhas ainda referem e falha;
      * `legacy_alter_table = ON`: com as chaves estrangeiras desligadas, é o que faz
        as tabelas-filhas continuarem a apontar pelo nome `charges` em vez de
        `charges_antiga`.
    A coerência é confirmada com `PRAGMA foreign_key_check` dentro da transação: se
    alguma referência tiver ficado partida, tudo é anulado e a base não muda.
    """
    connection = get_db()
    declaracoes = declaracoes_do_esquema(SCHEMA_FILE.read_text(encoding="utf-8"))
    reconstrucoes = _reconstrucoes_necessarias(connection, declaracoes)
    mexe_em_filhas = any(tabela in TABELAS_RECONSTRUIDAS_COM_FILHAS for tabela, _ in reconstrucoes)

    if mexe_em_filhas:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            # As tabelas novas primeiro: `charge_rules` passa a ter chave estrangeira
            # para `extraordinary_quotas`, e uma tabela não se cria a apontar para um
            # pai que só aparece mais abaixo no mesmo ficheiro. Só as que ainda não
            # existem: as existentes são tratadas pela migração, não por aqui.
            for declaracao in declaracoes:
                tabela = _nome_de_tabela(declaracao)
                if tabela is not None and not _tabela_existe(connection, tabela):
                    connection.execute(declaracao)

            _migrar_esquema(connection, declaracoes, reconstrucoes)

            # Índices e travões depois das reconstruções: é neles que as tabelas
            # recriadas precisam de ficar.
            for declaracao in declaracoes:
                if _nome_de_tabela(declaracao) is None:
                    connection.execute(declaracao)

            violacoes = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violacoes:
                detalhe = "; ".join(
                    f"{linha['table']}.{linha['cols']} -> {linha['referenced']}" for linha in violacoes
                )
                raise RuntimeError(
                    f"a migração deixou referências partidas em: {detalhe}; nada foi gravado"
                )

            now = utc_now_iso()
            for key, value in DEFAULT_SETTINGS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

            # Sem semente de frações: uma base nova começa sem frações nem
            # permilagens. O administrador cria-as em Frações. Bases existentes
            # não são tocadas.

            connection.execute(f"PRAGMA user_version = {ESQUEMA_VERSAO}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        if mexe_em_filhas:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
