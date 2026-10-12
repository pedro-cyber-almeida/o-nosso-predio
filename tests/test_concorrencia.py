"""Concorrência real: dois pedidos simultâneos sobre o mesmo estado guardado.

Estes testes usam duas sessões e duas threads contra a mesma base. O objetivo é o
invariante: dois formulários abertos sobre o mesmo estado não podem ambos ser
aplicados, e o resultado final nunca fica com metade de cada alteração.
"""

from __future__ import annotations

import re
import threading

from conftest import ADMIN_TESTE, entrar

from app.units import format_permilagem

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
REVISAO_RE = re.compile(r'name="(revisao_[a-z]+)" value="([^\"]*)\"')


def _protecao(client, caminho: str) -> dict[str, str]:
    html = client.get(caminho).get_data(as_text=True)
    match = CSRF_RE.search(html)
    assert match, f"sem token CSRF em {caminho}"
    campos = {nome: valor for nome, valor in REVISAO_RE.findall(html)}
    assert campos, f"sem revisão de concorrência em {caminho}"
    return {"csrf_token": match.group(1), **campos}


def _formulario(client, db, protecao: dict[str, str], **mudancas) -> dict:
    form = {
        f"permilagem_{fracao['id']}": format_permilagem(fracao["permilagem"])
        for fracao in db.fractions()
    }
    form.update(protecao)
    form["confirmar_reparticao"] = "on"
    form["motivo"] = "Alteração concorrente"
    for codigo, valor in mudancas.items():
        form[f"permilagem_{db.fraction(codigo)['id']}"] = format_permilagem(valor)
    return form


def test_duas_sessoes_com_a_mesma_revisao_as_duas_recebem_a_mesma(app, client, db):
    """Duas páginas abertas ao mesmo tempo mostram a mesma revisão."""
    segunda = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    primeira = _protecao(client, "/fractions/permilagens")
    outra = _protecao(segunda, "/fractions/permilagens")

    assert primeira["revisao_fracoes"] == outra["revisao_fracoes"]


def test_dois_post_simultaneos_sobre_o_mesmo_estado_apenas_um_e_aplicado(app, client, db):
    """Dois pedidos com a mesma revisão: um é aplicado, o outro é recusado, sem mistura."""
    segunda = entrar(app, ADMIN_TESTE[0], ADMIN_TESTE[1])
    permilagens_antes = db.permilagens()

    protecao_1 = _protecao(client, "/fractions/permilagens")
    protecao_2 = _protecao(segunda, "/fractions/permilagens")
    assert protecao_1["revisao_fracoes"] == protecao_2["revisao_fracoes"]

    # As duas propostas partem do mesmo estado e movem permilagem diferente.
    proposta_a = _formulario(
        client, db, protecao_1, A=permilagens_antes["A"] + 1000, H=permilagens_antes["H"] - 1000
    )
    proposta_b = _formulario(
        client, db, protecao_2, A=permilagens_antes["A"] - 1000, G=permilagens_antes["G"] + 1000
    )

    resultados: list[tuple[object, int]] = []
    barreira = threading.Barrier(2, timeout=15)

    def submeter(client_, formulario: dict) -> None:
        barreira.wait()
        resposta = client_.post("/fractions/permilagens", data=formulario)
        resultados.append((client_, resposta.status_code))

    threads = [
        threading.Thread(target=submeter, args=(client, proposta_a)),
        threading.Thread(target=submeter, args=(segunda, proposta_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(codigo for _c, codigo in resultados) == [302, 302], (
        f"esperava duas recusas/aceitações por redirect, obtive {resultados}"
    )

    guardado = db.permilagens()
    assert sum(guardado.values()) == 100000, "a soma deixou de ser 1000,00"
    assert guardado in (
        {**permilagens_antes, "A": permilagens_antes["A"] + 1000, "H": permilagens_antes["H"] - 1000},
        {**permilagens_antes, "A": permilagens_antes["A"] - 1000, "G": permilagens_antes["G"] + 1000},
    ), "o resultado final mistura as duas alterações concorrentes"

    assert db.scalar("SELECT COUNT(*) FROM permilagem_history") == 2, (
        "o rasto não corresponde a exatamente uma das alterações aplicadas"
    )

    # O código de estado não distingue aceitação de recusa: o que distingue é o resultado.
    paginas = [
        cliente.get("/fractions/permilagens").get_data(as_text=True)
        for cliente, _codigo in resultados
    ]
    aceites = [corpo for corpo in paginas if "Permilagens atualizadas" in corpo]
    recusadas = [corpo for corpo in paginas if "desatualizada" in corpo]
    assert len(aceites) == 1, "mais de um pedido concorrente foi aceite"
    assert len(recusadas) == 1, "o pedido recusado não explicou a recusa"
