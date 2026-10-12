"""Isolamento: os testes nunca tocam na base nem nos segredos do projeto."""

from __future__ import annotations

from app import create_app
from app.db import init_db
from conftest import CHAVE_DE_TESTE, PROJECT_ROOT


def test_aplicacao_de_teste_usa_so_ficheiros_em_tmp_path(app, db_path, tmp_path):
    assert app.config["DATABASE"] == str(db_path)
    assert db_path.exists()
    assert db_path.parent == tmp_path
    assert str(PROJECT_ROOT / "instance") not in str(db_path)


def test_segredo_da_aplicacao_de_teste_nao_vem_da_pasta_do_projeto(
    app, db_path, instance_snapshot, snapshot_instance
):
    assert app.config["SECRET_KEY"] == CHAVE_DE_TESTE
    assert str(PROJECT_ROOT / "instance") not in str(db_path)
    assert snapshot_instance() == instance_snapshot


def test_sem_variavel_de_ambiente_o_segredo_e_escrito_no_instance_path_temporario(
    tmp_path, monkeypatch, snapshot_instance, instance_snapshot
):
    monkeypatch.delenv("CONDOMINIO_SECRET_KEY", raising=False)

    aplicacao = create_app(
        instance_path=tmp_path / "instancia",
        database=tmp_path / "outra.db",
        testing=True,
    )

    assert aplicacao.config["SECRET_KEY"] != CHAVE_DE_TESTE
    assert (tmp_path / "instancia" / "secret_key.txt").exists()
    assert snapshot_instance() == instance_snapshot


def test_nenhum_teste_cria_ficheiros_em_instance_do_projeto(
    instance_snapshot, snapshot_instance, client, db
):
    # o fixture autouse compara o estado antes e depois de cada teste
    client.get("/fractions")
    client.get("/fractions/permilagens")
    client.get("/config")
    client.get(f"/fractions/{db.fraction('A')['id']}")
    assert snapshot_instance() == instance_snapshot


def test_create_app_explicito_com_tmp_path_nao_toca_no_projeto(
    tmp_path, snapshot_instance, instance_snapshot
):
    aplicacao = create_app(
        instance_path=tmp_path / "instancia",
        database=tmp_path / "outra.db",
        testing=True,
    )
    with aplicacao.app_context():
        init_db()

    assert snapshot_instance() == instance_snapshot
    assert (tmp_path / "outra.db").exists()


def test_a_base_real_do_projeto_nao_e_criada_nem_alterada(
    client, db_path, instance_snapshot, snapshot_instance
):
    base_real = PROJECT_ROOT / "instance" / "condominio.db"
    assert db_path != base_real

    client.get("/fractions")
    client.get("/config")

    assert snapshot_instance() == instance_snapshot
