"""Parser for ARCA 'Mis Comprobantes' exports (CSV, emitidos / recibidos).

ARCA has changed the export layout over time (';' vs ',' delimiters, decimal comma, IVA split by
alícuota columns in newer versions). Headers are matched by normalized keywords, not position.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from ..rag.text import strip_accents


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", strip_accents(h.lower())).strip()


def _num(value: str | None) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace("$", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def _date(value: str) -> date:
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Fecha no reconocida: {value!r}")


# normalized header -> canonical field
_FIELDS: list[tuple[str, str]] = [
    ("fecha", "fecha"),
    ("tipo", "tipo_comprobante"),
    ("punto de venta", "punto_venta"),
    ("numero desde", "numero"),
    ("nro doc emisor", "contraparte_cuit"),
    ("nro doc receptor", "contraparte_cuit"),
    ("nro doc", "contraparte_cuit"),
    ("denominacion", "contraparte_nombre"),
    ("tipo cambio", "tipo_cambio"),
    ("moneda", "moneda"),
    ("imp neto gravado", "neto_gravado"),
    ("neto gravado total", "neto_gravado"),
    ("imp neto no gravado", "neto_no_gravado"),
    ("neto no gravado", "neto_no_gravado"),
    ("imp op exentas", "exento"),
    ("op exentas", "exento"),
    ("otros tributos", "otros_tributos"),
    ("total iva", "iva"),
    ("iva", "iva"),
    ("imp total", "total"),
]


def _map_headers(headers: list[str]) -> tuple[dict[str, int], list[int], list[int]]:
    """Return canonical->column index, plus split IVA columns and split neto columns (newer layout)."""
    mapping: dict[str, int] = {}
    iva_split: list[int] = []
    neto_split: list[int] = []
    for i, raw in enumerate(headers):
        h = _norm(raw)
        if re.match(r"^(imp )?iva \d", h):  # "IVA 21%", "Imp. IVA 10,5%"
            iva_split.append(i)
            continue
        if re.match(r"^(imp )?neto grav(ado)? iva \d", h):
            neto_split.append(i)
            continue
        if h.startswith("tipo doc"):
            continue
        for key, field in _FIELDS:
            if field in mapping:
                continue
            if h == key or h.startswith(key):
                if key == "tipo" and not h.startswith("tipo de comprobante") and h != "tipo":
                    continue
                if key == "iva" and h != "iva":
                    continue
                mapping[field] = i
                break
    return mapping, iva_split, neto_split


def parse_mis_comprobantes(content: str) -> tuple[list[dict], list[str]]:
    content = content.lstrip("﻿")
    lines = content.splitlines()
    # Some exports prepend a title row; find the header row containing "Fecha".
    start = next((i for i, l in enumerate(lines) if "fecha" in _norm(l)), 0)
    body = "\n".join(lines[start:])
    sample = lines[start] if lines else ""
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    headers = next(reader, [])
    mapping, iva_split, neto_split = _map_headers(headers)
    required = {"fecha", "tipo_comprobante", "total"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"No se reconocen columnas {sorted(missing)} en el CSV. Encabezados: {headers}")

    rows, warnings = [], []
    for n, rec in enumerate(reader, start=2):
        if not rec or not any(c.strip() for c in rec):
            continue
        get = lambda f: rec[mapping[f]] if f in mapping and mapping[f] < len(rec) else None  # noqa: E731
        try:
            tipo_raw = get("tipo_comprobante") or ""
            m = re.match(r"\s*(\d+)", tipo_raw)
            if not m:
                raise ValueError(f"tipo de comprobante {tipo_raw!r}")
            iva = sum(_num(rec[i]) for i in iva_split if i < len(rec)) if iva_split else _num(get("iva"))
            neto = (sum(_num(rec[i]) for i in neto_split if i < len(rec))
                    if neto_split and "neto_gravado" not in mapping else _num(get("neto_gravado")))
            cuit_cp = re.sub(r"\D", "", get("contraparte_cuit") or "") or None
            rows.append({
                "fecha": _date(get("fecha") or "").isoformat(),
                "tipo_comprobante": int(m.group(1)),
                "punto_venta": int(_num(get("punto_venta"))),
                "numero": int(_num(get("numero"))),
                "contraparte_cuit": cuit_cp,
                "contraparte_nombre": (get("contraparte_nombre") or "").strip() or None,
                "neto_gravado": neto,
                "neto_no_gravado": _num(get("neto_no_gravado")),
                "exento": _num(get("exento")),
                "iva": iva,
                "otros_tributos": _num(get("otros_tributos")),
                "total": _num(get("total")),
                "moneda": (get("moneda") or "PES").strip() or "PES",
                "tipo_cambio": _num(get("tipo_cambio")) or 1.0,
            })
        except (ValueError, IndexError) as exc:
            warnings.append(f"Fila {n} ignorada: {exc}")
    return rows, warnings
