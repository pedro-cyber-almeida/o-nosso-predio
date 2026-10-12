"""Portal do condómino: só as próprias frações, sempre em leitura (entrega 1)."""

from __future__ import annotations

import pytest

from conftest import (
    afetar_pagamento,
    anular_pagamento,
    criar_condomino,
    csrf_de,
    entrar_condomino,
    id_da_cobranca,
    registar_pagamento,
    regras_e_mes,
    ultimo_pagamento,
)


@pytest.fixture(autouse=True)
def _dia_fixo(hoje):
    """Estes testes correm como se hoje fosse 01/02/2027."""


def _pagamento_de_a(client, db):
    registar_pagamento(client, db, "A", "43,00")
    pagamento = ultimo_pagamento(db)
    afetar_pagamento(
        client,
        pagamento["id"],
        {
            id_da_cobranca(db, "A", "2027-01", "quota_ordinaria"): "40,00",
            id_da_cobranca(db, "A", "2027-01", "reforco_fundo"): "3,00",
        },
    )
    return pagamento


def test_condomino_ve_as_suas_fracoes_e_nao_ve_as_outras(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    html = condomino.get("/minhas-fracoes").get_data(as_text=True)
    assert "Fração A" in html
    assert "Fração B" not in html
    assert "Fração H" not in html


def test_fracao_de_outra_pessoa_devolve_404_mesmo_com_endereco_escrito_a_mao(app, client, db):
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")
    resposta = condomino.get(f"/minhas-fracoes/{db.fraction('B')['id']}")
    assert resposta.status_code == 404


def test_varias_fracoes_por_utilizador_e_utilizadores_diferentes_na_mesma_fracao(app, client, db):
    criar_condomino(app, db, "condomino.ac", ("A", "C"))
    criar_condomino(app, db, "condomino.a2", ("A",))

    primeiro = entrar_condomino(app, "condomino.ac").get("/minhas-fracoes").get_data(as_text=True)
    assert "Fração A" in primeiro and "Fração C" in primeiro and "Fração B" not in primeiro

    segundo = entrar_condomino(app, "condomino.a2").get("/minhas-fracoes").get_data(as_text=True)
    assert "Fração A" in segundo and "Fração C" not in segundo


def test_portal_mostra_cobrancas_e_pagamentos_como_o_administrador_calcula(app, client, db):
    regras_e_mes(client, db)
    _pagamento_de_a(client, db)
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    html = condomino.get(f"/minhas-fracoes/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "43,00 EUR" in html  # recebido
    assert "40,00 EUR" in html  # quota ordinária liquidada
    assert "0,00 EUR" in html  # dívida liquidada deste mês
    assert "Transferência bancária" in html


def test_portal_e_so_leitura_sem_ligacoes_de_gestao(app, client, db):
    regras_e_mes(client, db)
    pagamento = _pagamento_de_a(client, db)
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    html = condomino.get(f"/minhas-fracoes/{db.fraction('A')['id']}").get_data(as_text=True)
    for proibido in ["/edit", "/cancel", "/allocate", "/payments/add", "/utilizadores", "/config"]:
        assert proibido not in html

    # As rotas de gestão também não valem escritas: o POST não cria nada.
    csrf = csrf_de(condomino, "/")
    resposta = condomino.post(
        "/payments/add",
        data={
            "csrf_token": csrf,
            "fracao": db.fraction("A")["id"],
            "valor": "10,00",
            "data": "15/01/2027",
            "meio": "dinheiro",
            "confirmar_pagamento": "on",
        },
    )
    assert resposta.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM payments") == 1  # só o do administrador


def test_pagamento_anulado_aparece_anulado_no_portal(app, client, db):
    regras_e_mes(client, db)
    pagamento = _pagamento_de_a(client, db)
    anular_pagamento(client, pagamento["id"], motivo="Engano de registo")
    criar_condomino(app, db, "condomino.a", ("A",))
    condomino = entrar_condomino(app, "condomino.a")

    html = condomino.get(f"/minhas-fracoes/{db.fraction('A')['id']}").get_data(as_text=True)
    assert "anulado" in html
    assert "Engano de registo" in html
