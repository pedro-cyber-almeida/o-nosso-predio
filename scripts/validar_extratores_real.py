"""Valida os extratores com os documentos reais descarregados pelo diagnóstico.

Imprime apenas contagens e estados: nº de movimentos, campos válidos,
extractor usado. Nunca imprime valores, datas, referências ou descrições.

Uso: python scripts/validar_extratores_real.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.extratores import extrair_documento  # noqa: E402


def main() -> int:
    pasta = Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "bancario"
    ficheiros = sorted(pasta.glob("*.bin"))
    if not ficheiros:
        print("sem ficheiros de diagnóstico")
        return 1

    for caminho in ficheiros:
        dados = caminho.read_bytes()
        mime = "application/pdf" if dados[:5] == b"%PDF-" else "(outro)"
        r = extrair_documento(caminho, mime)
        movs = r["movimentos"]
        validos = sum(
            1 for m in movs
            if m["data"] and m["valor_cents"] is not None and m["sentido"]
        )
        print(
            f"{caminho.name[-6:]}: estado={r['estado']} extrator={r['extrator'] or '-'}"
            f" v{r['versao']} paginas={r['paginas']} chars={r['caracteres']}"
            f" movimentos={len(movs)} completos={validos}"
            f" sem_sentido={sum(1 for m in movs if not m['sentido'])}"
            f" nao_executados={sum(1 for m in movs if not m['executado_flag'])}"
            f" nota={r['nota'][:60] or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
