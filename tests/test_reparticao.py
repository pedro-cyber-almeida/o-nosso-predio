"""Repartição de montantes: a soma tem de ser exatamente o total, sempre."""

from __future__ import annotations

import pytest

from app.reparticao import repartir, repartir_em_prestacoes, repartir_igual


def test_repartir_igual_conserva_o_total_exato():
    partes = repartir_igual(10000, 3)
    assert sum(partes) == 10000
    assert partes == [3334, 3333, 3333], "a diferença vai às primeiras frações, por ordem"


def test_repartir_igual_nao_perde_nem_acrescenta_centimos_em_muitas_partes():
    for total in (1, 99, 100, 101, 999999, 10000000):
        for quantidade in (1, 2, 3, 7, 8, 12):
            partes = repartir_igual(total, quantidade)
            assert sum(partes) == total, (total, quantidade, partes)
            assert all(parte >= 0 for parte in partes)
            assert max(partes) - min(partes) <= 1, "as partes não podem diferir mais de um cêntimo"


def test_repartir_proporcional_mantem_a_proporcao_e_o_total():
    permilagens = [10000, 12000]
    partes = repartir(100000, permilagens)
    assert sum(partes) == 100000
    # A proporção pedida é respeitada ao cêntimo mais próximo.
    assert partes[0] < partes[1]
    assert abs(partes[0] / 100000 - 10000 / 22000) < 1e-4


def test_repartir_proporcional_em_varios_totais_nunca_desvia_o_total():
    pesos = [10000, 12000, 11000, 13000, 12500, 12500, 9000, 10000]
    for total in (100, 999, 100000, 1234567, 10000000):
        partes = repartir(total, pesos)
        assert sum(partes) == total, (total, partes)


def test_repartir_em_prestacoes_soma_exatamente_a_quota_parte():
    assert repartir_em_prestacoes(10000, 3) == [3333, 3333, 3334]
    assert repartir_em_prestacoes(10000, 1) == [10000]
    assert repartir_em_prestacoes(100, 7) == [14, 14, 14, 14, 14, 14, 16]


def test_prestacoes_conservam_o_total_em_qualquer_divisao():
    for valor in (1, 7, 100, 33333, 10000000):
        for quantidade in (1, 2, 3, 5, 12):
            partes = repartir_em_prestacoes(valor, quantidade)
            assert sum(partes) == valor, (valor, quantidade, partes)
            assert partes[:-1] == [partes[0]] * (quantidade - 1), "as primeiras são todas iguais"


def test_reparticao_recusa_entradas_sem_sentido():
    with pytest.raises(ValueError):
        repartir(0, [1])
    with pytest.raises(ValueError):
        repartir(-100, [1])
    with pytest.raises(ValueError):
        repartir(100, [])
    with pytest.raises(ValueError):
        repartir(100, [1, 0])
    with pytest.raises(ValueError):
        repartir_em_prestacoes(100, 0)
