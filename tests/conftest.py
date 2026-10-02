import hashlib
import math
from datetime import date

import pytest

from contador_mcp.config import Settings
from contador_mcp.service import ContadorService

TODAY = date(2026, 10, 2)
ACME = "30-71234567-1"  # sociedad, RI, convenio multilateral, empleador, agente SICORE
JUAN = "20-12345678-6"  # persona humana


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder: stands in for a neural model in tests."""

    name = "test-hash-embedder"

    def embed(self, texts):
        from contador_mcp.rag.text import tokenize

        out = []
        for t in texts:
            v = [0.0] * 64
            for tok in tokenize(t):
                v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, dense_model=None)


@pytest.fixture
def svc(settings):
    return ContadorService(settings, today=TODAY)


@pytest.fixture
def acme(svc):
    svc.client_register(ACME, "ACME SA", "juridica", "responsable_inscripto", "convenio_multilateral",
                        ["CABA", "Buenos Aires"], empleador=True, agente_retencion_sicore=True,
                        cierre_ejercicio_mes=12)
    return svc
