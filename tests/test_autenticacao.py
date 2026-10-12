"""Autenticação, perfis e proteção das rotas no servidor (entrega 1)."""

from __future__ import annotations

import pytest

from conftest import (
    ADMIN_TESTE,
    PALAVRA_CONDOMINO_TESTE as PALAVRA_CONDOMINO,
    criar_condomino,
    csrf_de,
    editar_pagamento,
    entrar,
    protecao_de,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)

import pytest


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Pagamentos de teste com data em janeiro de 2027 correm como se hoje fosse 01/02/2027."""


# ---------------------------------------------------------------- setup ----


def test_enquanto_nao_ha_utilizadores_o_setup_e_apresentado_e_cria_o_primeiro_administrador(
    app, cliente_anonimo
):
    resposta = cliente_anonimo.get("/setup")
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert "Criar o primeiro administrador" in html
    assert "não há palavra-passe predefinida" in html

    resposta = cliente_anonimo.post(
        "/setup",
        data={
            "csrf_token": csrf_de(cliente_anonimo, "/setup"),
            "utilizador": "primeiro.admin",
            "nome": "Administrador Inicial",
            "palavra_passe": "medida-da-casa-2026",
            "confirmar_palavra_passe": "medida-da-casa-2026",
        },
    )
    assert resposta.status_code == 302
    painel = cliente_anonimo.get("/").get_data(as_text=True)
    assert "Painel do administrador" in painel
    assert "primeiro.admin" in painel


def test_setup_fecha_depois_do_primeiro_administrador_e_nao_cria_segundo_acesso(app, db):
    from app.auth import PAPEL_ADMIN, criar_utilizador

    with app.app_context():
        criar_utilizador("unico.admin", "palavra-segura-1234", PAPEL_ADMIN)

    anonimo = app.test_client()
    resposta = anonimo.get("/setup")
    assert resposta.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM users") == 1


def test_setup_recusa_palavra_passe_curta_e_confirmacao_diferente_sem_criar_nada(app, db):
    anonimo = app.test_client()
    resposta = anonimo.post(
        "/setup",
        data={
            "csrf_token": csrf_de(anonimo, "/setup"),
            "utilizador": "curto",
            "palavra_passe": "12345",
            "confirmar_palavra_passe": "12345",
        },
    )
    html = resposta.get_data(as_text=True)
    assert "mínimo 10 caracteres" in html
    assert db.scalar("SELECT COUNT(*) FROM users") == 0


def test_login_e_saida_pelo_percurso_real(app, admin_teste):
    cliente = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    assert "Painel do administrador" in cliente.get("/").get_data(as_text=True)

    resposta = cliente.post(
        "/logout",
        data={"csrf_token": csrf_de(cliente, "/")},
    )
    assert resposta.status_code == 302
    assert "Entrar" in cliente.get("/", follow_redirects=True).get_data(as_text=True)


def test_palavra_passe_errada_nao_entrar_e_a_mensagem_nao_revela_se_o_nome_existe(app, admin_teste):
    anonimo = app.test_client()
    csrf = csrf_de(anonimo, "/login")

    resposta = anonimo.post(
        "/login", data={"csrf_token": csrf, "utilizador": ADMIN_TESTE[0], "palavra_passe": "errada"}
    ).get_data(as_text=True)
    assert "Utilizador ou palavra-passe inválidos" in resposta

    resposta = anonimo.post(
        "/login", data={"csrf_token": csrf, "utilizador": "nao.existe", "palavra_passe": "errada"}
    ).get_data(as_text=True)
    assert "Utilizador ou palavra-passe inválidos" in resposta
    # A mesma frase para nome inexistente e palavra-passe errada.
    assert "não existe" not in resposta


# ------------------------------------------------- proteção no servidor ----

ROTAS_ADMIN = [
    "/fractions",
    "/fractions/new",
    "/fractions/permilagens",
    "/quotas",
    "/quotas/generate?period=2027-01",
    "/charges",
    "/payments",
    "/payments/add",
    "/config",
    "/utilizadores",
    "/utilizadores/novo",
]


@pytest.mark.parametrize("caminho", ROTAS_ADMIN)
def test_sem_sessao_nenhuma_rota_de_gestao_devolve_conteudo(app, admin_teste, caminho):
    anonimo = app.test_client()
    resposta = anonimo.get(caminho)
    assert resposta.status_code == 302
    assert resposta.headers["Location"].startswith("/login")
    pagina = anonimo.get(caminho, follow_redirects=True).get_data(as_text=True)
    assert "Entrar" in pagina  # só a página de entrada, nunca a de gestão


def test_post_anonimo_nao_cria_pagamento_apesar_de_ter_csrf_valido(app, client, db):
    anonimo = app.test_client()
    csrf = csrf_de(anonimo, "/login")  # CSRF válido da própria sessão anónima
    resposta = anonimo.post(
        "/payments/add",
        data={
            "csrf_token": csrf,
            "fracao": db.fraction("A")["id"],
            "valor": "43,00",
            "data": "15/01/2027",
            "meio": "transferencia",
            "confirmar_pagamento": "on",
            "form_token": "forjado",
        },
    )
    assert resposta.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM payments") == 0


def test_condomino_nao_acede_a_rotas_de_administracao(app, client, db):
    criar_condomino(app, db, "condomino.um", ("A",))
    condomino = entrar(app, "condomino.um", PALAVRA_CONDOMINO)

    for caminho in ROTAS_ADMIN:
        resposta = condomino.get(caminho)
        assert resposta.status_code == 302, caminho
        assert resposta.headers["Location"].startswith("/")

    html = condomino.get("/").get_data(as_text=True)
    assert "Painel" in html
    assert "Gmail" not in html


def test_utilizador_desativado_nao_entrar_e_sessao_antiga_deixa_de_valer(app, db):
    from app.auth import PAPEL_CONDOMINO, criar_utilizador

    with app.app_context():
        user_id, erros = criar_utilizador("ia.sair", "palavra-valida-1234", PAPEL_CONDOMINO)
    assert not erros

    cliente = entrar(app, "ia.sair", "palavra-valida-1234")
    assert "Painel" in cliente.get("/").get_data(as_text=True)

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    resposta = cliente.get("/")
    assert resposta.status_code == 302
    assert resposta.headers["Location"].startswith("/login")


# ------------------------------------------------------ contas próprias ----


def test_condomino_altera_a_propria_palavra_passe_com_a_atual(app, client, db):
    user_id = criar_condomino(app, db, "condomino.passe", ("A",))
    condomino = entrar(app, "condomino.passe", PALAVRA_CONDOMINO)
    hash_antes = db.scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,))

    resposta = condomino.post(
        "/conta/palavra-passe",
        data={
            "csrf_token": csrf_de(condomino, "/conta/palavra-passe"),
            "palavra_passe_atual": PALAVRA_CONDOMINO,
            "palavra_passe_nova": "nova-passe-do-condomino",
            "confirmar_palavra_passe": "nova-passe-do-condomino",
        },
    )
    assert resposta.status_code == 302
    hash_depois = db.scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    assert hash_depois != hash_antes
    assert "nova-passe-do-condomino" not in hash_depois

    # A antiga deixa de entrar; a nova entra.
    anonimo = app.test_client()
    resposta = anonimo.post(
        "/login",
        data={
            "csrf_token": csrf_de(anonimo, "/login"),
            "utilizador": "condomino.passe",
            "palavra_passe": PALAVRA_CONDOMINO,
        },
    ).get_data(as_text=True)
    assert "inválidos" in resposta
    assert "Painel" in entrar(app, "condomino.passe", "nova-passe-do-condomino").get("/").get_data(
        as_text=True
    )


def test_alterar_palavra_passe_sem_a_atual_nao_muda_nada(app, client, db):
    user_id = criar_condomino(app, db, "condomino.cauteloso", ("A",))
    condomino = entrar(app, "condomino.cauteloso", PALAVRA_CONDOMINO)
    hash_antes = db.scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,))

    resposta = condomino.post(
        "/conta/palavra-passe",
        data={
            "csrf_token": csrf_de(condomino, "/conta/palavra-passe"),
            "palavra_passe_atual": "quase-certa-mas-nao",
            "palavra_passe_nova": "qualquer-coisa-1234",
            "confirmar_palavra_passe": "qualquer-coisa-1234",
        },
    ).get_data(as_text=True)
    assert "palavra-passe atual não está certa" in resposta
    assert db.scalar("SELECT password_hash FROM users WHERE id = ?", (user_id,)) == hash_antes


# ------------------------------------------------- gestão de utilizadores ----


def test_admin_cria_condomino_com_varias_fracoes_e_fracao_pode_ter_varios_utilizadores(
    app, client, db
):
    resposta = client.post(
        "/utilizadores/novo",
        data={
            "csrf_token": csrf_de(client, "/utilizadores/novo"),
            "utilizador": "joao.silva",
            "nome": "João Silva",
            "papel": "condomino",
            "palavra_passe": "uma-passe-segura-10",
            "confirmar_palavra_passe": "uma-passe-segura-10",
            f"fracao_{db.fraction('A')['id']}": "on",
            f"fracao_{db.fraction('C')['id']}": "on",
        },
    )
    assert resposta.status_code == 302

    client.post(
        "/utilizadores/novo",
        data={
            "csrf_token": csrf_de(client, "/utilizadores/novo"),
            "utilizador": "maria.silva",
            "nome": "Maria Silva",
            "papel": "condomino",
            "palavra_passe": "outra-passe-segura-10",
            "confirmar_palavra_passe": "outra-passe-segura-10",
            f"fracao_{db.fraction('A')['id']}": "on",
        },
    )

    linhas = db.rows(
        "SELECT u.username, f.code FROM user_fractions uf "
        "JOIN users u ON u.id = uf.user_id JOIN fractions f ON f.id = uf.fraction_id "
        "ORDER BY u.username, f.code"
    )
    assert linhas == [
        {"username": "joao.silva", "code": "A"},
        {"username": "joao.silva", "code": "C"},
        {"username": "maria.silva", "code": "A"},
    ]


def test_nome_de_utilizador_repetido_e_recusado_sem_criar_meia_conta(app, client, db):
    html = client.post(
        "/utilizadores/novo",
        data={
            "csrf_token": csrf_de(client, "/utilizadores/novo"),
            "utilizador": ADMIN_TESTE[0],
            "papel": "condomino",
            "palavra_passe": "uma-passe-segura-10",
            "confirmar_palavra_passe": "uma-passe-segura-10",
        },
    ).get_data(as_text=True)
    assert "já existe" in html
    assert db.scalar("SELECT COUNT(*) FROM users") == 1
    assert db.scalar("SELECT COUNT(*) FROM user_fractions") == 0


def test_utilizador_nao_e_apagado_e_o_nome_nao_muda(app, client, db):
    user_id = criar_condomino(app, db, "imutavel", ("A",))
    with pytest.raises(Exception, match="não é apagado"):
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    with pytest.raises(Exception, match="não muda"):
        db.execute("UPDATE users SET username = 'novo.nome' WHERE id = ?", (user_id,))


def test_ultimo_administrador_nao_pode_ser_desativado_nem_mudar_de_papel(app, client, db):
    admin_id = db.scalar("SELECT id FROM users WHERE username = ?", (ADMIN_TESTE[0],))

    html = client.post(
        f"/utilizadores/{admin_id}",
        data={
            "csrf_token": csrf_de(client, f"/utilizadores/{admin_id}"),
            "nome": "Administrador",
            "papel": "condomino",
            "ativo": "on",
        },
    ).get_data(as_text=True)
    assert "único administrador ativo" in html
    assert db.scalar("SELECT role FROM users WHERE id = ?", (admin_id,)) == "administrador"

    html = client.post(
        f"/utilizadores/{admin_id}",
        data={
            "csrf_token": csrf_de(client, f"/utilizadores/{admin_id}"),
            "nome": "Administrador",
            "papel": "administrador",
        },
    ).get_data(as_text=True)
    assert "único administrador ativo" in html
    assert db.scalar("SELECT active FROM users WHERE id = ?", (admin_id,)) == 1


# ------------------------------------------------- identidade do corretor ----


def test_correcao_de_pagamento_regista_quem_corrigiu(app, client, db):
    regras_e_mes(client, db)
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    editar_pagamento(client, db, pagamento["id"], valor="53,00", motivo="Valor corrigido")

    correcoes = db.rows("SELECT changed_by, reason FROM payment_revisions")
    assert len(correcoes) == 1
    assert correcoes[0]["changed_by"] == ADMIN_TESTE[0]
    assert correcoes[0]["reason"] == "Valor corrigido"

    historico = client.get(f"/payments/{pagamento['id']}/history").get_data(as_text=True)
    assert ADMIN_TESTE[0] in historico
