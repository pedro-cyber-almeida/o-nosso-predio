"""Inicialização: uma base nova começa vazia e init_db não destrói dados."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import init_db
from conftest import PERMILAGENS_DE_TESTE


def test_instalacao_nova_comeca_sem_fracoes_nem_dados(tmp_path):
    """Nada do condomínio é inventado: nem frações, nem regras, nem movimentos."""
    from app import create_app

    aplicacao = create_app(
        instance_path=tmp_path / "instancia",
        database=tmp_path / "nova.db",
        testing=True,
    )
    with aplicacao.app_context():
        init_db()
        from app.db import query_db

        for tabela in ("fractions", "owners", "charge_rules", "charges", "payments",
                       "expenses", "users"):
            assert query_db(f"SELECT * FROM {tabela}") == [], f"{tabela} não está vazia"
        valores = {
            linha["key"]: linha["value"]
            for linha in query_db("SELECT key, value FROM settings")
        }
        assert valores, "as chaves de configuração devem existir"
        assert all(valor == "" for valor in valores.values()), (
            "nenhum valor de configuração pode ser inventado"
        )


def test_fracoes_de_teste_sao_sinteticas_e_somam_100000(db):
    assert db.permilagens() == PERMILAGENS_DE_TESTE


def test_soma_exata_das_permilagens_e_100000(db):
    assert db.total_permilagem() == 100000


def test_init_db_cria_o_esquema_numa_base_nova(db):
    for tabela in ("settings", "fractions", "owners"):
        assert db.table_exists(tabela), f"falta a tabela {tabela}"


def test_init_db_duas_vezes_nao_duplica_fracoes(app, db):
    with app.app_context():
        init_db()
        init_db()
        init_db()
    assert len(db.fractions()) == 8
    assert db.total_permilagem() == 100000


def test_init_db_preserva_permilagem_alterada_pelo_utilizador(app, db):
    with app.app_context():
        db.execute("UPDATE fractions SET permilagem = 12000 WHERE code = 'A'")
        init_db()
    assert db.permilagens()["A"] == 12000


def test_init_db_preserva_settings_personalizadas(app, db):
    with app.app_context():
        db.execute(
            "UPDATE settings SET value = 'Prédio da Rua Alta' WHERE key = 'condominio_nome'"
        )
        db.execute(
            "UPDATE settings SET value = '15000' WHERE key = 'saldo_inicial_caixa_centimos'"
        )
        db.execute("UPDATE settings SET value = '' WHERE key = 'condominio_morada'")
        init_db()
    valores = db.settings()
    assert valores["condominio_nome"] == "Prédio da Rua Alta"
    assert valores["saldo_inicial_caixa_centimos"] == "15000"


def test_init_db_nao_apaga_fracoes_nem_responsaveis_registados(app, db):
    with app.app_context():
        db.execute(
            "INSERT INTO fractions (code, permilagem, active, created_at, updated_at) "
            "VALUES ('I', 1, 1, '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO owners (fraction_id, name, role, valid_from, created_at) "
            "VALUES (?, 'Responsável Antigo', 'proprietario', '2020-01-01', "
            "'2026-10-09T00:00:00+00:00')",
            (db.fraction("A")["id"],),
        )
        init_db()

    assert "I" in db.permilagens()
    assert len(db.fractions()) == 9
    responsaveis = db.owners("A")
    assert len(responsaveis) == 1
    assert responsaveis[0]["name"] == "Responsável Antigo"
    assert responsaveis[0]["valid_from"] == "2020-01-01"


def test_init_db_mantem_settings_por_confirmar_como_valor_vazio(db):
    valores = db.settings()
    for chave in (
        "condominio_morada",
        "quota_dia_vencimento",
        "gestao_data_inicio",
        "saldo_inicial_caixa_centimos",
        "saldo_inicial_fundo_centimos",
    ):
        assert valores[chave] == "", f"{chave} não deve ter valor inventado"


def test_fraction_references_usa_a_coluna_da_chave_estrangeira(app, db):
    """Um responsável da fração B não pode contar como histórico da fração A."""
    from app.db import fraction_references, has_owner_history

    id_a = db.fraction("A")["id"]
    id_b = db.fraction("B")["id"]
    db.execute(
        "INSERT INTO owners (fraction_id, name, role, valid_from, created_at) "
        "VALUES (?, 'Responsável Só de B', 'proprietario', '2020-01-01', "
        "'2026-10-09T00:00:00+00:00')",
        (id_b,),
    )

    with app.app_context():
        assert fraction_references(id_b) == ["owners"]
        assert fraction_references(id_a) == [], "a referência não pode depender de coincidência de ids"
        assert has_owner_history(id_b) is True
        assert has_owner_history(id_a) is False


def test_fraction_references_detecta_tabelas_das_fases_seguintes(app, db):
    """A proteção de histórico não depende de uma lista de nomes mantida à mão."""
    from app.db import fraction_references, has_financial_history

    db.cria_tabela_futura()
    id_d = db.fraction("D")["id"]
    db.insere_registo_futuro(id_d)

    with app.app_context():
        assert fraction_references(id_d) == ["documentos_fase_futura"]
        assert has_financial_history(id_d) is True
        assert has_financial_history(db.fraction("E")["id"]) is False


ESQUEMA_COM_FALHA_A_MEIO = """
-- Esquema válido até à última linha, que viola NOT NULL de propósito.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    permilagem INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id)
);
CREATE TABLE IF NOT EXISTS permilagem_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id)
);
INSERT INTO settings (key, value, updated_at) VALUES ('chave_inventada', NULL, 'agora');
"""


def _app_com_base_vazia(tmp_path):
    """Aplicação cuja base ainda não tem esquema nem semente."""
    from app import create_app

    return create_app(
        instance_path=tmp_path / "inst-vazia",
        database=tmp_path / "vazia.db",
        testing=True,
    )


def test_init_db_com_falha_nao_deixa_tabelas_parciais(tmp_path, monkeypatch):
    """Uma falha a meio do esquema não pode deixar estrutura parcialmente criada."""
    import sqlite3

    from app import db as modulo_db

    esquema_partido = tmp_path / "esquema_partido.sql"
    esquema_partido.write_text(ESQUEMA_COM_FALHA_A_MEIO, encoding="utf-8")
    monkeypatch.setattr(modulo_db, "SCHEMA_FILE", esquema_partido)

    aplicacao = _app_com_base_vazia(tmp_path)
    with aplicacao.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            modulo_db.init_db()

    caminho = Path(aplicacao.config["DATABASE"])
    tabelas: set[str] = set()
    if caminho.exists():
        ligacao = sqlite3.connect(caminho)
        try:
            tabelas = {
                linha[0]
                for linha in ligacao.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            ligacao.close()

    assert tabelas == set(), f"o rollback deixou tabelas criadas: {tabelas}"


def test_init_db_com_falha_na_semente_nao_deixa_esquema_nem_semente(tmp_path, monkeypatch):
    """Se a semente falhar, as tabelas do esquema também não ficam criadas."""
    import sqlite3

    from app import db as modulo_db

    aplicacao = _app_com_base_vazia(tmp_path)
    caminho = Path(aplicacao.config["DATABASE"])
    caminho.parent.mkdir(parents=True, exist_ok=True)

    # settings já existe, com o DDL atual do esquema, e um gatilho que faz a
    # semente falhar no último valor. O DDL tem de ser o do esquema, senão a
    # migração reconstruiria a tabela e levava o gatilho consigo.
    declaracoes = modulo_db.declaracoes_do_esquema(modulo_db.SCHEMA_FILE.read_text(encoding="utf-8"))
    ddl_settings = modulo_db._ddl_no_esquema(declaracoes, "settings").replace("IF NOT EXISTS", "")
    ligacao = sqlite3.connect(caminho)
    ligacao.execute(ddl_settings)
    ligacao.execute(
        "CREATE TRIGGER falha_na_semente BEFORE INSERT ON settings "
        "WHEN NEW.key = 'dividas_abertura_notas' "
        "BEGIN SELECT RAISE(ABORT, 'falha simulada na semente'); END;"
    )
    ligacao.commit()
    ligacao.close()

    with aplicacao.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            modulo_db.init_db()

    ligacao = sqlite3.connect(caminho)
    try:
        tabelas = {
            linha[0]
            for linha in ligacao.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        linhas = ligacao.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    finally:
        ligacao.close()

    assert "fractions" not in tabelas, f"o esquema ficou criado apesar da falha: {tabelas}"
    assert "owners" not in tabelas, f"o esquema ficou criado apesar da falha: {tabelas}"
    assert linhas == 0, "a semente parcial ficou gravada"


def test_init_db_com_falha_preserva_base_existente_inteira(app, db, monkeypatch, tmp_path):
    """Uma inicialização que falha não pode alterar uma base já em uso."""
    import sqlite3

    from app import db as modulo_db

    id_a = db.fraction("A")["id"]
    db.execute(
        "INSERT INTO owners (fraction_id, name, role, valid_from, created_at) "
        "VALUES (?, 'Responsável Existente', 'proprietario', '2020-01-01', "
        "'2026-10-09T00:00:00+00:00')",
        (id_a,),
    )
    db.execute("UPDATE settings SET value = 'Prédio Atual' WHERE key = 'condominio_nome'")
    db.execute("UPDATE fractions SET permilagem = 12000 WHERE code = 'A'")

    permilagens_antes = db.permilagens()
    responsaveis_antes = db.owners()
    settings_antes = db.settings()

    esquema_partido = tmp_path / "esquema_partido.sql"
    esquema_partido.write_text(ESQUEMA_COM_FALHA_A_MEIO, encoding="utf-8")
    monkeypatch.setattr(modulo_db, "SCHEMA_FILE", esquema_partido)

    with app.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            modulo_db.init_db()

    assert db.permilagens() == permilagens_antes
    assert db.owners() == responsaveis_antes
    assert db.settings() == settings_antes


def test_init_db_repetido_continua_idempotente_com_o_esquema_novo(app, db):
    """O esquema novo inclui permilagem_history e continua a não duplicar nada."""
    from app.db import init_db

    with app.app_context():
        init_db()
        init_db()

    assert len(db.fractions()) == 8
    assert db.total_permilagem() == 100000
    assert db.table_exists("permilagem_history")
    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 0


def test_declaracoes_do_esquema_separam_as_tabelas_sem_perder_comentarios():
    from app.db import declaracoes_do_esquema

    declaracoes = declaracoes_do_esquema(
        "-- comentário de topo\n"
        "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE IF NOT EXISTS b (\n    id INTEGER PRIMARY KEY,\n    a_id INTEGER\n);\n"
    )
    assert len(declaracoes) == 2
    assert "comentário" not in declaracoes[0]
    assert "a_id INTEGER" in declaracoes[1]


def test_comentario_dentro_de_aspas_nao_parte_a_declaracao():
    """`--` dentro de texto não é um comentário: não pode partir o esquema."""
    from app.db import declaracoes_do_esquema

    declaracoes = declaracoes_do_esquema(
        "CREATE TABLE IF NOT EXISTS a (\n    id INTEGER,\n    x TEXT DEFAULT 'a--b'\n);\n"
    )
    assert len(declaracoes) == 1
    assert "'a--b'" in declaracoes[0]


DDL_OWNERS_ANTIGO = """
CREATE TABLE owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'proprietario'
        CHECK (role IN ('proprietario', 'administrador', 'representante')),
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
)
"""


def _base_com_owners_antigo(tmp_path):
    """Base criada antes da migração: owners só com três papéis e um registo."""
    import sqlite3

    from app import db as modulo_db

    aplicacao = _app_com_base_vazia(tmp_path)
    caminho = Path(aplicacao.config["DATABASE"])
    caminho.parent.mkdir(parents=True, exist_ok=True)

    declaracoes = modulo_db.declaracoes_do_esquema(modulo_db.SCHEMA_FILE.read_text(encoding="utf-8"))
    ddl_fractions = modulo_db._ddl_no_esquema(declaracoes, "fractions").replace("IF NOT EXISTS", "")

    ligacao = sqlite3.connect(caminho)
    ligacao.execute(ddl_fractions)
    ligacao.execute(DDL_OWNERS_ANTIGO)
    ligacao.execute("CREATE INDEX idx_owners_fraction ON owners (fraction_id)")
    ligacao.execute(
        "INSERT INTO fractions (code, permilagem, active, created_at, updated_at) "
        "VALUES ('A', 100000, 1, '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')"
    )
    ligacao.execute(
        "INSERT INTO owners (fraction_id, name, role, valid_from, created_at) "
        "VALUES (1, 'Responsável Antigo', 'representante', '2020-01-01', '2026-10-09T00:00:00+00:00')"
    )
    ligacao.commit()
    ligacao.close()
    return aplicacao


def test_base_antiga_e_migrada_e_os_responsaveis_sao_preservados(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` não corrige uma tabela antiga: a base é migrada."""
    import sqlite3

    from app import db as modulo_db

    aplicacao = _base_com_owners_antigo(tmp_path)

    with aplicacao.app_context():
        modulo_db.init_db()

    ligacao = sqlite3.connect(aplicacao.config["DATABASE"])
    ligacao.row_factory = sqlite3.Row
    try:
        ddl = ligacao.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owners'").fetchone()[0]
        registos = [dict(r) for r in ligacao.execute("SELECT name, role FROM owners")]
        indices = {r[0] for r in ligacao.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        versao = ligacao.execute("PRAGMA user_version").fetchone()[0]
        try:
            ligacao.execute(
                "INSERT INTO owners (fraction_id, name, role, valid_from, created_at) "
                "VALUES (1, 'Contacto Novo', 'contacto', '2026-01-01', '2026-10-09T00:00:00+00:00')"
            )
            novo_papel_aceite = True
        except sqlite3.IntegrityError:
            novo_papel_aceite = False
    finally:
        ligacao.close()

    assert "coproprietario" in ddl and "contacto" in ddl, "o CHECK antigo ficou na base migrada"
    assert registos == [{"name": "Responsável Antigo", "role": "representante"}], (
        "a migração não preservou os responsáveis"
    )
    assert "idx_owners_fraction" in indices, "a migração não recriou o índice"
    assert novo_papel_aceite, "os papéis novos continuam bloqueados depois da migração"
    assert versao == modulo_db.ESQUEMA_VERSAO


def test_migracao_do_esquema_e_registada_na_base(app, db):
    """A versão do esquema fica guardada: uma base antiga é reconhecida, não adivinhada."""
    from app.db import ESQUEMA_VERSAO, init_db

    with app.app_context():
        init_db()

    assert db.scalar("PRAGMA user_version") == ESQUEMA_VERSAO


def test_migracao_de_v3_para_v5_acrescenta_pagamentos_e_preserva_o_resto(app, db):
    """Uma base da fase 2 ganha pagamentos, correções e travões sem perder nada."""
    from app import db as modulo_db

    gatilhos_do_esquema = (
        "afecoes_da_mesma_fracao",
        "afecoes_da_mesma_fracao_update",
        "afecoes_nao_mudam_de_cobranca",
        "afecoes_sem_pagamento_anulado",
        "afecoes_sem_pagamento_anulado_update",
        "afecoes_nao_excedem_pagamento",
        "afecoes_nao_excedem_pagamento_update",
        "afecoes_nao_excedem_cobranca",
        "afecoes_nao_excedem_cobranca_update",
        "correcoes_sem_update",
        "correcoes_sem_delete",
        "correcoes_de_afecoes_sem_update",
        "correcoes_de_afecoes_sem_delete",
        "pagamentos_so_se_corrigem_com_motivo",
        "pagamentos_sem_alterar_auditoria",
        "pagamentos_sem_editar_anulado",
        "pagamentos_sem_delete",
        "pagamentos_nao_ficam_com_afecoes_a_mais",
    )
    for gatilho in gatilhos_do_esquema:
        db.execute(f"DROP TRIGGER IF EXISTS {gatilho}")
    for gatilho in modulo_db.GATILHOS_APOSENTADOS[5]:
        db.execute(f"DROP TRIGGER IF EXISTS {gatilho}")
    db.execute("DROP TABLE IF EXISTS payment_allocation_revisions")
    db.execute("DROP TABLE IF EXISTS payment_revisions")
    db.execute("DROP TABLE IF EXISTS payment_allocations")
    db.execute("DROP TABLE IF EXISTS payments")
    db.execute("PRAGMA user_version = 3")

    id_a = db.fraction("A")["id"]
    id_b = db.fraction("B")["id"]
    for id_fracao in (id_a, id_b):
        db.execute(
            """
            INSERT INTO charge_rules
                (fraction_id, rubric, amount_cents, valid_from, created_at, updated_at)
            VALUES (?, 'quota_ordinaria', 4700, '2027-01-01',
                    '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')
            """,
            (id_fracao,),
        )
        regra_id = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
        db.execute(
            """
            INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at)
            VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, '2027-01-01T00:00:00+00:00')
            """,
            (id_fracao, regra_id),
        )
    cobranca_de_b = int(
        db.scalar("SELECT id FROM charges WHERE fraction_id = ?", (id_b,))
    )

    regras_antes = db.rules()
    cobrancas_antes = db.charges("2027-01")
    permilagens_antes = db.permilagens()

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO
    assert db.table_exists("payments")
    assert db.table_exists("payment_allocations")
    assert db.table_exists("payment_revisions")
    assert db.table_exists("payment_allocation_revisions")
    for gatilho in gatilhos_do_esquema:
        assert db.scalar(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (gatilho,)
        ) == 1, f"o travão {gatilho} não foi recriado"
    for coluna in ("edited_at", "edit_reason"):
        assert coluna in db.columns("payments"), f"a coluna {coluna} não foi acrescentada"

    # O que já lá estava não mudou.
    assert db.rules() == regras_antes
    assert db.charges("2027-01") == cobrancas_antes
    assert db.permilagens() == permilagens_antes

    # Os travões novos estão ativos: o pagamento de A não pode liquidar a cobrança de B.
    db.execute(
        """
        INSERT INTO payments (fraction_id, amount_cents, paid_on, method, created_at, updated_at)
        VALUES (?, 4700, '2027-01-15', 'transferencia',
                '2027-01-15T00:00:00+00:00', '2027-01-15T00:00:00+00:00')
        """,
        (id_a,),
    )
    pagamento_id = int(db.scalar("SELECT id FROM payments ORDER BY id DESC LIMIT 1"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
            "VALUES (?, ?, 4700, '2027-01-15T00:00:00+00:00')",
            (pagamento_id, cobranca_de_b),
        )
    assert db.allocations() == []


DDL_PAYMENTS_DA_FASE_3 = """
CREATE TABLE payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_on TEXT NOT NULL CHECK (date(paid_on) IS NOT NULL),
    method TEXT NOT NULL DEFAULT 'outro'
        CHECK (method IN ('transferencia', 'dinheiro', 'cheque', 'debito_direto', 'outro')),
    reference TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    form_token TEXT,
    CHECK (
        (cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (cancelled_at IS NOT NULL AND cancellation_reason <> '')
    )
)
"""

DDL_ALLOCACOES_DA_FASE_3 = """
CREATE TABLE payment_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments (id),
    charge_id INTEGER NOT NULL REFERENCES charges (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at TEXT NOT NULL,
    UNIQUE (payment_id, charge_id)
)
"""


def test_migracao_de_v4_para_v5_adapta_a_imutabilidade_a_correcao_justificada(app, db):
    """Uma base da fase 3 ganha o rasto de correções sem perder os movimentos.

    A migração não retira simplesmente a proteção financeira: troca a imutabilidade
    absoluta por correção justificada, e uma escrita silenciosa continua bloqueada.
    """
    from app import db as modulo_db

    # Recriar a base como estava na v4: sem colunas de correção, sem rasto,
    # com a imutabilidade absoluta.
    db.execute("DROP TABLE IF EXISTS payment_allocations")
    db.execute("DROP TABLE IF EXISTS payments")
    db.execute(DDL_PAYMENTS_DA_FASE_3)
    db.execute(DDL_ALLOCACOES_DA_FASE_3)
    db.execute("PRAGMA user_version = 4")

    id_a = db.fraction("A")["id"]
    db.execute(
        """
        INSERT INTO charge_rules
            (fraction_id, rubric, amount_cents, valid_from, created_at, updated_at)
        VALUES (?, 'quota_ordinaria', 4700, '2027-01-01',
                '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')
        """,
        (id_a,),
    )
    regra_id = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
    db.execute(
        """
        INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at)
        VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, '2027-01-01T00:00:00+00:00')
        """,
        (id_a, regra_id),
    )
    cobranca = int(db.scalar("SELECT id FROM charges WHERE fraction_id = ?", (id_a,)))

    db.execute(
        """
        INSERT INTO payments (fraction_id, amount_cents, paid_on, method, reference, source,
                              created_at, updated_at, form_token)
        VALUES (?, 5200, '2027-01-15', 'transferencia', 'TRN-77', '',
                '2027-01-15T00:00:00+00:00', '2027-01-15T00:00:00+00:00', 'marca-antiga')
        """,
        (id_a,),
    )
    pagamento_id = int(db.scalar("SELECT id FROM payments"))
    db.execute(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
        "VALUES (?, ?, 4700, '2027-01-15T00:00:00+00:00')",
        (pagamento_id, cobranca),
    )

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO

    # O movimento continua lá, com os mesmos valores.
    guardado = db.payments()[0]
    assert guardado["amount_cents"] == 5200
    assert guardado["paid_on"] == "2027-01-15"
    assert guardado["form_token"] == "marca-antiga"
    assert guardado["edited_at"] is None and guardado["edit_reason"] is None
    assert db.allocations()[0]["amount_cents"] == 4700

    # As colunas novas existem e os travões aposentados desapareceram.
    assert "edited_at" in db.columns("payments")
    assert "edit_reason" in db.columns("payments")
    assert db.table_exists("payment_revisions")
    assert db.table_exists("payment_allocation_revisions")
    travoes = db.triggers()
    for aposentado in modulo_db.GATILHOS_APOSENTADOS[5]:
        assert aposentado not in travoes, f"o travão {aposentado} devia ter sido substituído"
    assert "pagamentos_so_se_corrigem_com_motivo" in travoes

    # A proteção financeira continua: alterar sem motivo é recusado.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE payments SET amount_cents = 6000 WHERE id = ?", (pagamento_id,))
    assert db.payments()[0]["amount_cents"] == 5200

    # E a correção justificada é o caminho permitido.
    db.execute(
        "UPDATE payments SET amount_cents = 6000, edited_at = ?, edit_reason = ?, updated_at = ? "
        "WHERE id = ?",
        ("2027-02-01T10:00:00+00:00", "o valor era 60,00", "2027-02-01T10:00:00+00:00", pagamento_id),
    )
    assert db.payments()[0]["amount_cents"] == 6000

    # Baixar o valor abaixo do que está distribuído continua travado.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE payments SET amount_cents = 3000, edited_at = ?, edit_reason = ?, "
            "updated_at = ? WHERE id = ?",
            (
                "2027-02-01T10:00:00+00:00",
                "valor errado",
                "2027-02-01T10:00:00+00:00",
                pagamento_id,
            ),
        )
    assert db.payments()[0]["amount_cents"] == 6000


def test_base_com_esquema_mais_recente_e_recusada_sem_nada_alterar(app, db):
    from app import db as modulo_db

    db.execute("PRAGMA user_version = 99")
    permilagens_antes = db.permilagens()
    settings_antes = db.settings()

    with app.app_context():
        with pytest.raises(RuntimeError):
            modulo_db.init_db()

    assert db.permilagens() == permilagens_antes
    assert db.settings() == settings_antes


# --- Migração v6 -> v7: quotas extraordinárias e correção de cobranças ---------

DDL_CHARGE_RULES_DA_V6 = """
CREATE TABLE charge_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    rubric TEXT NOT NULL CHECK (rubric IN ('quota_ordinaria', 'reforco_fundo')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        date(valid_from) IS NOT NULL
        AND strftime('%d', valid_from) = '01'
        AND (
            valid_to IS NULL
            OR (
                date(valid_to) IS NOT NULL
                AND strftime('%d', date(valid_to, '+1 day')) = '01'
                AND valid_to >= valid_from
            )
        )
    )
)
"""

DDL_CHARGES_DA_V6 = """
CREATE TABLE charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    rule_id INTEGER NOT NULL REFERENCES charge_rules (id),
    rubric TEXT NOT NULL CHECK (rubric IN ('quota_ordinaria', 'reforco_fundo')),
    period TEXT NOT NULL CHECK (
        period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
        AND substr(period, 6, 2) BETWEEN '01' AND '12'
    ),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_date TEXT,
    generated_at TEXT NOT NULL,
    UNIQUE (fraction_id, rubric, period)
)
"""

# Os travões de sobreposição como eram na v6: sem a exceção das quotas extraordinárias.
DDL_SOBREPOSICAO_DA_V6 = """
CREATE TRIGGER charge_rules_sem_sobreposicao_insert
BEFORE INSERT ON charge_rules
BEGIN
    SELECT RAISE(ABORT, 'regra de quota sobreposta')
    WHERE EXISTS (
        SELECT 1 FROM charge_rules AS existente
        WHERE existente.fraction_id = NEW.fraction_id
          AND existente.rubric = NEW.rubric
          AND existente.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
          AND COALESCE(existente.valid_to, '9999-12-31') >= NEW.valid_from
    );
END
"""

# Só as colunas que já existiam na v6: é com elas que se compara o que foi conservado.
COLUNAS_DE_REGRA_DA_V6 = (
    "id, fraction_id, rubric, amount_cents, valid_from, valid_to, source, created_at, updated_at"
)
COLUNAS_DE_COBRANCA_DA_V6 = (
    "id, fraction_id, rule_id, rubric, period, amount_cents, due_date, generated_at"
)


def _reduzir_a_base_a_v6(caminho) -> None:
    """Recriar `charge_rules` e `charges` como estavam na v6, à volta de dados reais.

    Usa a mesma receita da migração: `legacy_alter_table = ON` com as chaves
    estrangeiras desligadas, para que `payment_allocations` continue a apontar
    para `charges` e não para a tabela temporária.
    """
    ligacao = sqlite3.connect(caminho)
    try:
        ligacao.execute("PRAGMA foreign_keys = OFF")
        ligacao.execute("PRAGMA legacy_alter_table = ON")
        for nome in (
            "cobrancas_so_se_corrigem_com_motivo",
            "cobrancas_sem_apagar_a_origem",
            "cobrancas_nao_ficam_abaixo_do_pago",
            "charges_sem_delete",
            "correcoes_de_cobranca_sem_update",
            "correcoes_de_cobranca_sem_delete",
            "quotas_extraordinarias_sem_mudar_a_emitida",
            "quotas_extraordinarias_sem_apagar_a_emitida",
            "prestacoes_sem_mudar_as_de_quota_emitida",
            "prestacoes_sem_apagar_as_de_quota_emitida",
            "charge_rules_sem_sobreposicao_insert",
            "charge_rules_sem_sobreposicao_update",
            "regras_sem_mudar_valor_sem_motivo",
            "correcoes_de_regra_sem_update",
            "correcoes_de_regra_sem_delete",
        ):
            ligacao.execute(f"DROP TRIGGER IF EXISTS {nome}")
        for nome in (
            "idx_charges_rule",
            "idx_charges_recorrentes_unicas",
            "idx_charge_revisions_charge",
            "idx_charge_rule_revisions_rule",
        ):
            ligacao.execute(f"DROP INDEX IF EXISTS {nome}")
        for nome in (
            "charge_revisions",
            "quota_installments",
            "extraordinary_quotas",
            "charge_rule_revisions",
        ):
            ligacao.execute(f"DROP TABLE IF EXISTS {nome}")

        for tabela, ddl in (("charge_rules", DDL_CHARGE_RULES_DA_V6), ("charges", DDL_CHARGES_DA_V6)):
            colunas = [r[1] for r in ligacao.execute(f"PRAGMA table_info({tabela})")]
            ligacao.execute(f'ALTER TABLE "{tabela}" RENAME TO "{tabela}_v6"')
            ligacao.execute(ddl)
            comuns = ", ".join(f'"{c}"' for c in colunas if c in {r[1] for r in ligacao.execute(f"PRAGMA table_info({tabela})")})
            ligacao.execute(f'INSERT INTO "{tabela}" ({comuns}) SELECT {comuns} FROM "{tabela}_v6"')
            ligacao.execute(f'DROP TABLE "{tabela}_v6"')

        ligacao.execute(DDL_SOBREPOSICAO_DA_V6)
        ligacao.execute(
            "CREATE TRIGGER charges_sem_update BEFORE UPDATE ON charges "
            "BEGIN SELECT RAISE(ABORT, 'cobranças emitidas não podem ser alteradas'); END"
        )
        ligacao.execute(
            "CREATE UNIQUE INDEX idx_charge_rules_aberta ON charge_rules (fraction_id, rubric) "
            "WHERE valid_to IS NULL"
        )
        ligacao.execute("CREATE INDEX idx_charges_period ON charges (period)")
        ligacao.execute("CREATE INDEX idx_charges_fraction ON charges (fraction_id)")
        ligacao.execute("PRAGMA user_version = 6")
        ligacao.commit()
    finally:
        ligacao.close()


def test_migracao_de_v6_para_v7_preserva_cobrancas_e_afecoes(app, db):
    """Uma base com cobranças, pagamentos e afetações ganha a entrega 2 sem perder nada."""
    from app import db as modulo_db

    id_a = db.fraction("A")["id"]
    id_b = db.fraction("B")["id"]
    for id_fracao in (id_a, id_b):
        db.execute(
            """
            INSERT INTO charge_rules
                (fraction_id, rubric, amount_cents, valid_from, valid_to, created_at, updated_at)
            VALUES (?, 'quota_ordinaria', 4700, '2027-01-01', '2027-06-30',
                    '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')
            """,
            (id_fracao,),
        )
        regra_id = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
        db.execute(
            """
            INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, due_date, generated_at)
            VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, '2027-01-10',
                    '2027-01-01T00:00:00+00:00')
            """,
            (id_fracao, regra_id),
        )
    cobranca_de_a = int(db.scalar("SELECT id FROM charges WHERE fraction_id = ?", (id_a,)))
    db.execute(
        """
        INSERT INTO payments (fraction_id, amount_cents, paid_on, method, reference, source,
                              created_at, updated_at, form_token)
        VALUES (?, 5200, '2027-01-15', 'transferencia', 'TRN-77', '',
                '2027-01-15T00:00:00+00:00', '2027-01-15T00:00:00+00:00', 'marca-antiga')
        """,
        (id_a,),
    )
    pagamento_id = int(db.scalar("SELECT id FROM payments"))
    db.execute(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
        "VALUES (?, ?, 4700, '2027-01-15T00:00:00+00:00')",
        (pagamento_id, cobranca_de_a),
    )

    _reduzir_a_base_a_v6(db.caminho)
    assert db.scalar("PRAGMA user_version") == 6
    assert "descricao" not in db.columns("charges")

    regras_antes = db.rows(f"SELECT {COLUNAS_DE_REGRA_DA_V6} FROM charge_rules ORDER BY id")
    cobrancas_antes = db.rows(f"SELECT {COLUNAS_DE_COBRANCA_DA_V6} FROM charges ORDER BY id")
    pagamentos_antes = db.payments()
    afetoes_antes = db.allocations()

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO

    # Nada foi reescrito: as linhas são as mesmas, com os mesmos ids e valores.
    assert db.rows(f"SELECT {COLUNAS_DE_REGRA_DA_V6} FROM charge_rules ORDER BY id") == regras_antes
    assert db.rows(f"SELECT {COLUNAS_DE_COBRANCA_DA_V6} FROM charges ORDER BY id") == cobrancas_antes
    assert db.payments() == pagamentos_antes
    assert db.allocations() == afetoes_antes

    # As colunas novas existem e as antigas ficaram com o valor por defeito.
    assert {"descricao", "edit_token"} <= set(db.columns("charges"))
    assert {"quota_id", "descricao", "edit_token"} <= set(db.columns("charge_rules"))
    assert db.scalar("SELECT descricao FROM charges WHERE id = ?", (cobranca_de_a,)) == ""
    assert db.scalar("SELECT quota_id FROM charge_rules WHERE id = ?", (regras_antes[0]["id"],)) is None

    # As tabelas novas da entrega 2 existem e os travões antigos foram substituídos.
    for tabela in ("extraordinary_quotas", "quota_installments", "charge_revisions", "charge_rule_revisions"):
        assert db.table_exists(tabela), f"falta a tabela {tabela}"
    travoes = db.triggers()
    assert "charges_sem_update" not in travoes, "a imutabilidade absoluta deu lugar à correção justificada"
    for travao in (
        "cobrancas_so_se_corrigem_com_motivo",
        "cobrancas_sem_apagar_a_origem",
        "cobrancas_nao_ficam_abaixo_do_pago",
        "quotas_extraordinarias_sem_mudar_a_emitida",
        "prestacoes_sem_apagar_as_de_quota_emitida",
    ):
        assert travao in travoes, f"o travão {travao} não foi criado"
    for indice in ("idx_charges_recorrentes_unicas", "idx_charges_rule", "idx_charge_revisions_charge"):
        assert db.scalar(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (indice,)
        ) == 1, f"o índice {indice} não foi criado"
    assert db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'charges_antiga'") is None

    # As tabelas-filhas continuam a apontar para as tabelas reconstruídas, e nenhuma
    # referência ficou partida.
    filhas = {linha["table"] for linha in db.rows("PRAGMA foreign_key_list(payment_allocations)")}
    assert filhas == {"payments", "charges"}, filhas
    assert db.rows("PRAGMA foreign_key_check") == []

    # Com as chaves estrangeiras ativas, como a aplicação as usa: uma afetação para
    # uma cobrança que não existe é recusada.
    ligacao = sqlite3.connect(db.caminho)
    ligacao.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            ligacao.execute(
                "INSERT INTO payment_allocations (payment_id, charge_id, amount_cents, created_at) "
                "VALUES (?, 9999, 100, '2027-01-15T00:00:00+00:00')",
                (pagamento_id,),
            )
    finally:
        ligacao.close()


def test_migracao_de_v6_para_v7_muda_a_unicidade_das_cobrancas(app, db):
    """Depois da migração, duas quotas extraordinárias no mesmo mês coexistem; duplicados continuam travados."""
    from app import db as modulo_db

    id_a = db.fraction("A")["id"]
    db.execute(
        "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, valid_to, created_at, updated_at) "
        "VALUES (?, 'quota_ordinaria', 4700, '2027-01-01', '2027-06-30', 'x', 'x')",
        (id_a,),
    )
    regra = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
    db.execute(
        "INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at) "
        "VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, 'x')",
        (id_a, regra),
    )
    _reduzir_a_base_a_v6(db.caminho)
    with app.app_context():
        modulo_db.init_db()

    # Regras extraordinárias: precisam de quota e fim, e não colidem entre si.
    for indice, valor in enumerate((3000, 2000), start=1):
        db.execute(
            "INSERT INTO extraordinary_quotas (descricao, total_cents, reparticao, form_token, "
            "created_at, updated_at) VALUES (?, ?, 'igual', ?, 'x', 'x')",
            (f"Obra de {valor}", valor, f"marca-quota-{indice}"),
        )
        quota = int(db.scalar("SELECT id FROM extraordinary_quotas ORDER BY id DESC LIMIT 1"))
        db.execute(
            "INSERT INTO charge_rules (fraction_id, rubric, quota_id, amount_cents, valid_from, valid_to, created_at, updated_at) "
            "VALUES (?, 'quota_extraordinaria', ?, ?, '2027-01-01', '2027-01-31', 'x', 'x')",
            (id_a, quota, valor),
        )
        regra_extra = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
        db.execute(
            "INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at) "
            "VALUES (?, ?, 'quota_extraordinaria', '2027-01', ?, 'x')",
            (id_a, regra_extra, valor),
        )
    assert len([c for c in db.charges("2027-01") if c["rubric"] == "quota_extraordinaria"]) == 2

    # A mesma quota emitida outra vez é recusada: mesma fração, mês e regra.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at) "
            "VALUES (?, ?, 'quota_extraordinaria', '2027-01', 3000, 'x')",
            (id_a, int(db.scalar("SELECT rule_id FROM charges WHERE rubric = 'quota_extraordinaria' LIMIT 1"))),
        )

    # Uma segunda cobrança ordinária do mesmo mês continua travada, como antes.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, generated_at) "
            "VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, 'x')",
            (id_a, regra),
        )

    # Regra extraordinária sem fim definido não entra.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charge_rules (fraction_id, rubric, quota_id, amount_cents, valid_from, valid_to, created_at, updated_at) "
            "VALUES (?, 'quota_extraordinaria', 1, 100, '2027-02-01', NULL, 'x', 'x')",
            (id_a,),
        )


def test_migracao_de_v6_para_v7_abre_a_correcao_justificada_de_cobrancas(app, db):
    """A migração troca a imutabilidade absoluta por correção com motivo registado."""
    from app import db as modulo_db

    id_a = db.fraction("A")["id"]
    db.execute(
        "INSERT INTO charge_rules (fraction_id, rubric, amount_cents, valid_from, valid_to, created_at, updated_at) "
        "VALUES (?, 'quota_ordinaria', 4700, '2027-01-01', '2027-06-30', 'x', 'x')",
        (id_a,),
    )
    regra = int(db.scalar("SELECT id FROM charge_rules ORDER BY id DESC LIMIT 1"))
    db.execute(
        "INSERT INTO charges (fraction_id, rule_id, rubric, period, amount_cents, due_date, generated_at) "
        "VALUES (?, ?, 'quota_ordinaria', '2027-01', 4700, '2027-01-10', 'x')",
        (id_a, regra),
    )
    cobranca = int(db.scalar("SELECT id FROM charges"))
    _reduzir_a_base_a_v6(db.caminho)
    with app.app_context():
        modulo_db.init_db()

    # Escrita direta à base, sem revisão: recusado, como na v6.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE charges SET amount_cents = 5000 WHERE id = ?", (cobranca,))
    assert db.charges()[0]["amount_cents"] == 4700

    # Correção autorizada por uma revisão com motivo: passa.
    db.execute(
        "INSERT INTO charge_revisions (charge_id, changed_at, motivo, edit_token, amount_before, "
        "amount_after, due_before, due_after) VALUES (?, '2027-02-01T10:00:00+00:00', "
        "'a ata fixou 50,00', 'marca-1', 4700, 5000, '2027-01-10', '2027-01-15')",
        (cobranca,),
    )
    db.execute(
        "UPDATE charges SET amount_cents = 5000, due_date = '2027-01-15', edit_token = 'marca-1' "
        "WHERE id = ?",
        (cobranca,),
    )
    depois = db.charges()[0]
    assert depois["amount_cents"] == 5000 and depois["due_date"] == "2027-01-15"

    # O que diz de onde a cobrança veio não se mexe, mesmo com revisão válida.
    db.execute(
        "INSERT INTO charge_revisions (charge_id, changed_at, motivo, edit_token, amount_before, "
        "amount_after) VALUES (?, '2027-02-01T10:00:00+00:00', 'trocar de fração', 'marca-2', 5000, 5000)",
        (cobranca,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE charges SET fraction_id = ?, edit_token = 'marca-2' WHERE id = ?",
            (db.fraction("B")["id"], cobranca),
        )
    assert db.charges()[0]["fraction_id"] == id_a

    # Reenvio da mesma marca não cria uma segunda correção.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO charge_revisions (charge_id, changed_at, motivo, edit_token, amount_before, "
            "amount_after) VALUES (?, '2027-02-01T10:00:00+00:00', 'reenvio', 'marca-1', 5000, 5000)",
            (cobranca,),
        )


def test_migracao_que_falha_nao_deixa_tabela_partida(tmp_path, monkeypatch):
    """Uma migração interrompida deixa a tabela antiga exatamente como estava."""
    import sqlite3

    from app import db as modulo_db

    aplicacao = _base_com_owners_antigo(tmp_path)

    # Esquema novo cujo CHECK rejeita o registo existente: a cópia falha a meio.
    esquema_rigido = tmp_path / "esquema_rigido.sql"
    esquema_rigido.write_text(
        DDL_OWNERS_ANTIGO.replace(
            "CHECK (role IN ('proprietario', 'administrador', 'representante'))",
            "CHECK (role IN ('proprietario'))",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(modulo_db, "SCHEMA_FILE", esquema_rigido)

    with aplicacao.app_context():
        with pytest.raises(sqlite3.IntegrityError):
            modulo_db.init_db()

    ligacao = sqlite3.connect(aplicacao.config["DATABASE"])
    ligacao.row_factory = sqlite3.Row
    try:
        tabelas = {r[0] for r in ligacao.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ddl = ligacao.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owners'").fetchone()[0]
        registos = [dict(r) for r in ligacao.execute("SELECT name, role FROM owners")]
        versao = ligacao.execute("PRAGMA user_version").fetchone()[0]
    finally:
        ligacao.close()

    assert "owners_antiga" not in tabelas, f"a migração falhada deixou restos: {tabelas}"
    assert "representante" in ddl, "a tabela antiga foi substituída apesar da falha"
    assert registos == [{"name": "Responsável Antigo", "role": "representante"}]
    assert versao == 0, "a versão foi gravada apesar da migração falhada"


# --- Entrega 3: migração v7 → v8 ----------------------------------------------

TABELAS_DA_ENTREGA_3 = (
    "expense_payment_revisions",
    "expense_revisions",
    "expense_payments",
    "expenses",
    "expense_categories",
)

TRAVOES_DA_ENTREGA_3 = (
    "categorias_sem_apagar_as_usadas",
    "categorias_sem_mudar_as_usadas",
    "pagamentos_de_despesa_nao_excedem_despesa",
    "pagamentos_de_despesa_nao_excedem_despesa_update",
    "pagamentos_de_despesa_sem_despesa_anulada",
    "despesas_so_se_corrigem_com_motivo",
    "despesas_nao_ficam_abaixo_do_pago",
    "despesas_sem_anular_com_pagamentos",
    "despesas_sem_editar_anulada",
    "despesas_sem_delete",
    "despesas_sem_alterar_auditoria",
    "pagamentos_de_despesa_so_se_corrigem_com_motivo",
    "pagamentos_de_despesa_sem_editar_anulado",
    "pagamentos_de_despesa_sem_delete",
    "pagamentos_de_despesa_sem_alterar_auditoria",
    "correcoes_de_despesa_sem_update",
    "correcoes_de_despesa_sem_delete",
    "correcoes_de_pagamento_de_despesa_sem_update",
    "correcoes_de_pagamento_de_despesa_sem_delete",
)

INDICES_DA_ENTREGA_3 = (
    "idx_expenses_form_token",
    "idx_expenses_category",
    "idx_expenses_data",
    "idx_expense_payments_expense",
    "idx_expense_payments_period",
    "idx_expense_payments_form_token",
    "idx_expense_revisions_expense",
    "idx_expense_payment_revisions_payment",
)


def _reduzir_a_base_a_v7(db) -> None:
    """Apagar as tabelas, travões e índices que a entrega 3 acrescentou."""
    for nome in TRAVOES_DA_ENTREGA_3:
        db.execute(f"DROP TRIGGER IF EXISTS {nome}")
    for nome in INDICES_DA_ENTREGA_3:
        db.execute(f"DROP INDEX IF EXISTS {nome}")
    for nome in TABELAS_DA_ENTREGA_3:
        db.execute(f"DROP TABLE IF EXISTS {nome}")
    db.execute("PRAGMA user_version = 7")


def test_migracao_de_v7_para_v8_acrescenta_despesas_sem_tocar_no_resto(app, db):
    """A entrega 3 só acrescenta: nenhuma tabela existente é reconstruída."""
    from app import db as modulo_db

    id_a = db.fraction("A")["id"]
    db.execute(
        """
        INSERT INTO payments (fraction_id, amount_cents, paid_on, method, reference, source,
                              created_at, updated_at, form_token)
        VALUES (?, 5200, '2027-01-15', 'transferencia', 'TRN-77', '',
                '2027-01-15T00:00:00+00:00', '2027-01-15T00:00:00+00:00', 'marca-antiga')
        """,
        (id_a,),
    )

    _reduzir_a_base_a_v7(db)
    assert db.scalar("PRAGMA user_version") == 7
    for tabela in TABELAS_DA_ENTREGA_3:
        assert not db.table_exists(tabela), f"a base v7 já tinha {tabela}"

    pagamentos_antes = db.payments()

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO
    assert db.payments() == pagamentos_antes, "a migração reescreveu pagamentos existentes"
    for tabela in TABELAS_DA_ENTREGA_3:
        assert db.table_exists(tabela), f"falta a tabela {tabela}"
    travoes = db.triggers()
    for nome in TRAVOES_DA_ENTREGA_3:
        assert nome in travoes, f"falta o travão {nome}"

    # Os travões novos valem logo após a migração: apagar uma despesa é recusado.
    db.execute(
        """
        INSERT INTO expense_categories (id, nome, created_at, updated_at)
        VALUES (1, 'Limpeza', '2027-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00')
        """
    )
    db.execute(
        """
        INSERT INTO expenses (id, descricao, category_id, data, amount_cents,
                              created_at, updated_at)
        VALUES (1, 'Hall', 1, '2027-01-10', 12000,
                '2027-01-10T00:00:00+00:00', '2027-01-10T00:00:00+00:00')
        """
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM expenses WHERE id = 1")

    # Idempotência: repetir não duplica nem recria nada.
    with app.app_context():
        modulo_db.init_db()
    assert db.scalar("SELECT COUNT(*) FROM expense_categories") == 1
    assert db.scalar("SELECT COUNT(*) FROM expenses") == 1


# --- Entrega 4: migração v8 → v9 ----------------------------------------------

TABELAS_DA_ENTREGA_4 = (
    "documento_fracoes",
    "documento_ligacoes",
    "documentos",
    "integracoes",
)


def test_migracao_de_v8_para_v9_acrescenta_integracoes_e_documentos_sem_tocar_no_resto(app, db):
    """A entrega 4 só acrescenta: nenhuma tabela existente é reconstruída."""
    import sqlite3

    from app import db as modulo_db

    _reduzir_a_base_a_v8(db)
    assert db.scalar("PRAGMA user_version") == 8
    for tabela in TABELAS_DA_ENTREGA_4:
        assert not db.table_exists(tabela), f"a base v8 já tinha {tabela}"

    despesas_antes = db.expenses()

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO
    for tabela in TABELAS_DA_ENTREGA_4:
        assert db.table_exists(tabela), f"falta a tabela {tabela}"
    travoes = db.triggers()
    assert "documentos_sem_apagar_os_ligados" in travoes
    assert "documento_ligacoes_sem_apagar" in travoes

    # Os dados anteriores (entrega 3) continuam exatamente como estavam.
    assert db.expenses() == despesas_antes

    # Os travões novos valem logo após a migração.
    db.execute(
        "INSERT INTO documentos (origem, nome_interno, mime, tamanho_bytes, criado_em) "
        "VALUES ('anexo', 'abc123.pdf', 'application/pdf', 10, '2027-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO documento_ligacoes (documento_id, alvo, alvo_id, criado_em) "
        "VALUES (1, 'despesa', 1, '2027-01-01T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM documentos WHERE id = 1")

    with app.app_context():
        modulo_db.init_db()
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 1


def _reduzir_a_base_a_v8(db) -> None:
    """Apagar as tabelas, travões e índices que a entrega 4 acrescentou."""
    for nome in ("documentos_sem_apagar_os_ligados", "documento_ligacoes_sem_apagar"):
        db.execute(f"DROP TRIGGER IF EXISTS {nome}")
    for nome in ("idx_documentos_origem", "idx_documentos_decisao", "idx_documento_ligacoes_alvo"):
        db.execute(f"DROP INDEX IF EXISTS {nome}")
    for nome in TABELAS_DA_ENTREGA_4:
        db.execute(f"DROP TABLE IF EXISTS {nome}")
    db.execute("PRAGMA user_version = 8")


# --- Biblioteca de documentos: migração v9 → v10 -------------------------------


def _reduzir_a_base_a_v9(db) -> None:
    """Recriar as tabelas de documentos com o DDL exatamente como era na v9."""
    for nome in ("documentos_sem_apagar_os_ligados", "documento_ligacoes_sem_apagar"):
        db.execute(f"DROP TRIGGER IF EXISTS {nome}")
    for nome in (
        "idx_documentos_origem", "idx_documentos_decisao", "idx_documento_ligacoes_alvo",
    ):
        db.execute(f"DROP INDEX IF EXISTS {nome}")
    for nome in ("documento_fracoes", "documento_ligacoes", "documentos"):
        db.execute(f"DROP TABLE IF EXISTS {nome}")
    db.execute(
        """
        CREATE TABLE documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origem TEXT NOT NULL CHECK (origem IN ('email', 'anexo', 'condomino')),
            nome_interno TEXT NOT NULL UNIQUE CHECK (length(trim(nome_interno)) > 0),
            nome_original TEXT NOT NULL DEFAULT '',
            mime TEXT NOT NULL DEFAULT '',
            tamanho_bytes INTEGER NOT NULL CHECK (tamanho_bytes >= 0),
            sha256 TEXT NOT NULL DEFAULT '',
            gmail_mensagem_id TEXT NOT NULL DEFAULT '',
            gmail_anexo_id TEXT NOT NULL DEFAULT '',
            resumo TEXT NOT NULL DEFAULT '',
            tratado_em TEXT,
            decisao TEXT NOT NULL DEFAULT 'guardado'
                CHECK (decisao IN ('guardado', 'pendente', 'confirmado', 'rejeitado')),
            decisao_motivo TEXT NOT NULL DEFAULT '',
            decidido_em TEXT,
            decidido_por TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL,
            criado_por TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db.execute(
        """
        CREATE TABLE documento_ligacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            documento_id INTEGER NOT NULL REFERENCES documentos (id),
            alvo TEXT NOT NULL CHECK (alvo IN ('pagamento', 'pagamento_despesa', 'despesa')),
            alvo_id INTEGER NOT NULL,
            criado_em TEXT NOT NULL,
            criado_por TEXT NOT NULL DEFAULT '',
            notas TEXT NOT NULL DEFAULT '',
            UNIQUE (documento_id, alvo, alvo_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE documento_fracoes (
            documento_id INTEGER NOT NULL REFERENCES documentos (id),
            fraction_id INTEGER NOT NULL REFERENCES fractions (id),
            criado_em TEXT NOT NULL,
            UNIQUE (documento_id, fraction_id)
        )
        """
    )
    db.execute("PRAGMA user_version = 9")


def test_migracao_de_v9_para_v10_reconstroi_documentos_sem_perder_nada(app, db):
    """A biblioteca acrescenta colunas e formatos: as linhas antigas conservam-se."""
    import sqlite3

    from app import db as modulo_db

    _reduzir_a_base_a_v9(db)
    assert db.scalar("PRAGMA user_version") == 9

    id_a = db.fraction("A")["id"]
    db.execute(
        "INSERT INTO documentos (origem, nome_interno, nome_original, mime, tamanho_bytes, "
        "sha256, resumo, criado_em) "
        "VALUES ('anexo', 'antigo.pdf', 'fatura-antiga.pdf', 'application/pdf', 10, 'aa', "
        "'Fatura antiga', '2027-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO documento_ligacoes (documento_id, alvo, alvo_id, criado_em) "
        "VALUES (1, 'despesa', 1, '2027-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO documento_fracoes (documento_id, fraction_id, criado_em) "
        "VALUES (1, ?, '2027-01-01T00:00:00+00:00')",
        (id_a,),
    )
    pagamentos_antes = db.payments()

    with app.app_context():
        modulo_db.init_db()

    assert db.scalar("PRAGMA user_version") == modulo_db.ESQUEMA_VERSAO
    assert db.payments() == pagamentos_antes, "a migração reescreveu pagamentos existentes"

    # A linha antiga sobrevive, com os metadados novos nos valores por defeito.
    linha = db.rows("SELECT * FROM documentos WHERE id = 1")[0]
    assert linha["origem"] == "anexo"
    assert linha["nome_interno"] == "antigo.pdf"
    assert linha["resumo"] == "Fatura antiga"
    assert linha["categoria"] == "Outro"
    assert linha["versao"] == 1
    assert linha["versao_de"] is None
    assert linha["arquivado"] == 0
    assert linha["partilha_todos"] == 0
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes") == 1
    assert db.scalar("SELECT COUNT(*) FROM documento_fracoes") == 1

    # Os travões voltam a existir depois da reconstrução.
    travoes = db.triggers()
    assert "documentos_sem_apagar_os_ligados" in travoes
    assert "documento_ligacoes_sem_apagar" in travoes

    # A v10 aceita o que a v9 recusava: upload e associação a fração.
    db.execute(
        "INSERT INTO documentos (origem, nome_interno, mime, tamanho_bytes, criado_em) "
        "VALUES ('upload', 'novo.pdf', 'application/pdf', 10, '2027-02-01T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO documentos (origem, nome_interno, mime, tamanho_bytes, criado_em) "
            "VALUES ('fax', 'x.pdf', 'application/pdf', 10, '2027-02-01T00:00:00+00:00')"
        )
    db.execute(
        "INSERT INTO documento_ligacoes (documento_id, alvo, alvo_id, criado_em) "
        "VALUES (1, 'fracao', ?, '2027-02-01T00:00:00+00:00')",
        (id_a,),
    )

    # Repetir a migração não duplica nem altera nada.
    with app.app_context():
        modulo_db.init_db()
    assert db.scalar("SELECT COUNT(*) FROM documentos") == 2
    assert db.scalar("SELECT COUNT(*) FROM documento_ligacoes") == 2
