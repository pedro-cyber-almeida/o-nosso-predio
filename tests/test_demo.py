"""Demonstração: base separada, dados fictícios e recusa de sobrescrita."""

from __future__ import annotations

from app.demo import DEMO_FRACTIONS, create_demo_database
from conftest import PERMILAGENS_DE_TESTE

DEMO_PERMILAGENS = dict(DEMO_FRACTIONS)


def test_demo_cria_base_separada_com_fracoes_e_responsaveis_ficticios(
    tmp_path, base_direta
):
    caminho = tmp_path / "demo.db"

    sucesso, mensagem = create_demo_database(caminho)

    assert sucesso, mensagem
    base = base_direta(caminho)
    assert base.permilagens() == DEMO_PERMILAGENS
    responsaveis = base.owners()
    assert len(responsaveis) == 8
    assert all("Fictício" in registo["name"] for registo in responsaveis)
    assert all(registo["source"] == "demonstração fictícia" for registo in responsaveis)
    assert "demonstração" in base.settings()["condominio_nome"]


def test_demo_popula_com_dados_claramente_ficticios(tmp_path, base_direta):
    """A demonstração é explicitamente acionada e tudo o que cria está marcado."""
    caminho = tmp_path / "demo.db"
    create_demo_database(caminho)
    base = base_direta(caminho)

    assert base.scalar("SELECT COUNT(*) FROM charge_rules") == 16
    assert base.scalar("SELECT COUNT(*) FROM charges") > 0
    assert base.scalar("SELECT COUNT(*) FROM payments") > 0
    assert base.scalar("SELECT COUNT(*) FROM expenses") > 0
    assert base.scalar("SELECT COUNT(*) FROM documentos") == 3

    # Nada na demonstração fica sem marca de origem fictícia.
    for tabela, coluna in (("charge_rules", "source"), ("payments", "source"),
                           ("owners", "source")):
        total = base.scalar(f"SELECT COUNT(*) FROM {tabela}")
        marcados = base.scalar(
            f"SELECT COUNT(*) FROM {tabela} WHERE {coluna} = 'demonstração fictícia'"
        )
        assert marcados == total, f"{tabela} tem registos sem marca de demonstração"

    # Contas de acesso da demonstração, documentadas no README.
    utilizadores = {
        linha["username"]
        for linha in base.rows("SELECT username FROM users")
    }
    assert {"demo.admin", "demo.condomino"} <= utilizadores


def test_segunda_chamada_recusa_sobrescrever_e_preserva_os_dados(tmp_path, base_direta):
    caminho = tmp_path / "demo.db"
    create_demo_database(caminho)
    base = base_direta(caminho)
    base.execute("UPDATE owners SET name = 'Nome Editado À Mão' WHERE fraction_id = 1")
    base.execute(
        "UPDATE settings SET value = 'Nome Guardado' WHERE key = 'condominio_nome'"
    )

    sucesso, mensagem = create_demo_database(caminho)

    assert not sucesso, mensagem
    assert base.owners()[0]["name"] == "Nome Editado À Mão"
    assert base.settings()["condominio_nome"] == "Nome Guardado"
    assert len(base.fractions()) == 8


def test_demo_recusa_escrever_numa_base_real_com_dados(app, db, db_path):
    sucesso, mensagem = create_demo_database(db_path)

    assert not sucesso, mensagem
    assert db.permilagens() == PERMILAGENS_DE_TESTE


def test_demo_nao_escreve_na_base_real(app, db, db_path, tmp_path):
    permilagens_antes = db.permilagens()
    settings_antes = db.settings()
    responsaveis_antes = db.owners()

    create_demo_database(tmp_path / "demo.db")

    assert db.permilagens() == permilagens_antes
    assert db.settings() == settings_antes
    assert db.owners() == responsaveis_antes
    assert db_path != tmp_path / "demo.db"


def test_demo_aceita_base_existente_mas_sem_dados(tmp_path, base_direta):
    caminho = tmp_path / "demo.db"
    base = base_direta(caminho)
    base.execute("CREATE TABLE IF NOT EXISTS marcas (id INTEGER PRIMARY KEY)")

    sucesso, mensagem = create_demo_database(caminho)

    assert sucesso, mensagem
    assert base.permilagens() == DEMO_PERMILAGENS


def test_demo_nao_escreve_na_pasta_instance_do_projeto(
    tmp_path, instance_snapshot, snapshot_instance
):
    create_demo_database(tmp_path / "demo.db")

    assert snapshot_instance() == instance_snapshot


def test_demo_escreve_documentos_ao_lado_da_base(tmp_path):
    caminho = tmp_path / "demo.db"
    sucesso, _ = create_demo_database(caminho)
    assert sucesso

    pasta = tmp_path / "demo_instance" / "documentos"
    assert pasta.exists(), "os PDFs de exemplo devem ficar junto da base de demonstração"
    assert len(list(pasta.glob("*.pdf"))) == 3
