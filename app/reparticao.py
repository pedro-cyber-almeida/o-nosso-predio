"""Repartição de um montante por frações e por prestações, com soma exata.

Dinheiro são cêntimos inteiros, por isso uma divisão nunca pode ser exata:
100,00 por três frações são 33,33 + 33,33 + 33,34, não 33,33 repetido três vezes.
Este módulo é o único sítio onde essa decisão é tomada, para que a pré-visualização
mostre exatamente o que a emissão grava.

Regras:
- A soma das partes é exatamente o montante pedido: nem um cêntimo a mais, nem a menos.
- Cada parte é o inteiro mais próximo por arredondamento para baixo; os cêntimos que
  faltam para fechar o total são distribuídos um a um pelas partes com resto maior
  (método do resto maior), o que mantém a proporção pedida.
- Em empate, vale a ordem da lista recebida: a mesma entrada dá sempre a mesma saída,
  e a pré-visualização pode mostrar qual fração recebe o cêntimo a mais.
- Nas prestações, a diferença vai toda para a última: as primeiras são todas iguais.
"""

from __future__ import annotations

MAXIMO_PARTES = 12


def repartir(total_cents: int, pesos: list[int]) -> list[int]:
    """Dividir `total_cents` proporcionalmente a `pesos`, em cêntimos inteiros.

    `pesos` são números relativos (permilagens, ou 1 para partes iguais). A soma do
    resultado é exatamente `total_cents`.
    """
    total = int(total_cents)
    if total <= 0:
        raise ValueError("o montante a repartir tem de ser positivo")
    pesos = [int(peso) for peso in pesos]
    if not pesos:
        raise ValueError("tem de haver pelo menos uma parte")
    if len(pesos) > MAXIMO_PARTES * 8:
        raise ValueError("demasiadas partes para repartir")
    if any(peso <= 0 for peso in pesos):
        raise ValueError("todas as partes têm de ter peso positivo")

    soma_dos_pesos = sum(pesos)
    partes: list[int] = []
    restos: list[tuple[int, int]] = []
    for indice, peso in enumerate(pesos):
        produto = total * peso
        partes.append(produto // soma_dos_pesos)
        restos.append((produto % soma_dos_pesos, indice))

    faltam = total - sum(partes)
    # Maior resto primeiro; em empate, a ordem original da lista.
    for _, indice in sorted(restos, key=lambda item: (-item[0], item[1]))[:faltam]:
        partes[indice] += 1

    assert sum(partes) == total, "a repartição tem de conservar o total"
    return partes


def repartir_igual(total_cents: int, quantidade: int) -> list[int]:
    """Dividir pelo mesmo número de partes, todas iguais salvo os cêntimos sobrantes."""
    if quantidade < 1:
        raise ValueError("tem de haver pelo menos uma parte")
    return repartir(total_cents, [1] * quantidade)


def repartir_em_prestacoes(valor_cents: int, quantidade: int) -> list[int]:
    """Dividir a quota-parte de uma fração pelas prestações, com soma exata.

    As primeiras prestações são todas iguais; a última fecha a diferença, para que
    a soma das prestações seja exatamente a quota-parte.
    """
    valor = int(valor_cents)
    if valor <= 0:
        raise ValueError("o montante a repartir tem de ser positivo")
    if quantidade < 1:
        raise ValueError("tem de haver pelo menos uma prestação")
    base, sobra = divmod(valor, quantidade)
    partes = [base] * (quantidade - 1) + [base + sobra]
    assert sum(partes) == valor, "as prestações têm de conservar a quota-parte"
    return partes
