"""CUIT/CUIL validation (módulo 11) and normalization."""

from __future__ import annotations

import re

_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_VALID_PREFIXES = {"20", "23", "24", "25", "26", "27", "30", "33", "34"}


class InvalidCuit(ValueError):
    pass


def normalize_cuit(value: str) -> str:
    """Return the 11 digits of a CUIT ('30-71234567-1' -> '30712345671') or raise InvalidCuit."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11:
        raise InvalidCuit(f"El CUIT debe tener 11 dígitos: {value!r}")
    if digits[:2] not in _VALID_PREFIXES:
        raise InvalidCuit(f"Prefijo de CUIT inválido ({digits[:2]}): {value!r}")
    if check_digit(digits[:10]) != int(digits[10]):
        raise InvalidCuit(f"Dígito verificador inválido: {value!r}")
    return digits


def check_digit(first_ten: str) -> int:
    total = sum(int(d) * w for d, w in zip(first_ten, _WEIGHTS))
    dv = 11 - total % 11
    if dv == 11:
        return 0
    if dv == 10:
        # ARCA re-assigns the prefix (23/24/33) in this case, so the 10-digit base is never valid.
        return -1
    return dv


def format_cuit(digits: str) -> str:
    return f"{digits[:2]}-{digits[2:10]}-{digits[10]}"


def last_digit(cuit: str) -> int:
    """Terminación del CUIT: the digit ARCA uses to stagger due dates."""
    return int(normalize_cuit(cuit)[-1])
