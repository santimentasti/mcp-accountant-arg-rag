"""SQLite persistence for client facts, invoices, payroll, filings and the knowledge base."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    cuit TEXT PRIMARY KEY,
    razon_social TEXT NOT NULL,
    tipo_persona TEXT NOT NULL CHECK (tipo_persona IN ('humana', 'juridica')),
    condicion_iva TEXT NOT NULL CHECK (condicion_iva IN ('responsable_inscripto', 'exento', 'no_alcanzado')),
    iibb_regimen TEXT NOT NULL CHECK (iibb_regimen IN ('local', 'convenio_multilateral', 'exento', 'no_inscripto')),
    iibb_jurisdicciones TEXT NOT NULL DEFAULT '[]',
    empleador INTEGER NOT NULL DEFAULT 0,
    agente_retencion_sicore INTEGER NOT NULL DEFAULT 0,
    agente_recaudacion_iibb INTEGER NOT NULL DEFAULT 0,
    cierre_ejercicio_mes INTEGER NOT NULL DEFAULT 12 CHECK (cierre_ejercicio_mes BETWEEN 1 AND 12),
    actividad TEXT,
    contacto TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS client_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit TEXT NOT NULL REFERENCES clients(cuit) ON DELETE CASCADE,
    note TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    kb_document_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit TEXT NOT NULL REFERENCES clients(cuit) ON DELETE CASCADE,
    direccion TEXT NOT NULL CHECK (direccion IN ('emitida', 'recibida')),
    fecha TEXT NOT NULL,
    periodo TEXT NOT NULL,
    tipo_comprobante INTEGER NOT NULL,
    punto_venta INTEGER NOT NULL DEFAULT 0,
    numero INTEGER NOT NULL DEFAULT 0,
    contraparte_cuit TEXT,
    contraparte_nombre TEXT,
    neto_gravado REAL NOT NULL DEFAULT 0,
    neto_no_gravado REAL NOT NULL DEFAULT 0,
    exento REAL NOT NULL DEFAULT 0,
    iva REAL NOT NULL DEFAULT 0,
    percepciones_iva REAL NOT NULL DEFAULT 0,
    otros_tributos REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    moneda TEXT NOT NULL DEFAULT 'PES',
    tipo_cambio REAL NOT NULL DEFAULT 1,
    origen TEXT NOT NULL DEFAULT 'manual',
    UNIQUE (cuit, direccion, tipo_comprobante, punto_venta, numero, contraparte_cuit)
);
CREATE INDEX IF NOT EXISTS idx_invoices_period ON invoices(cuit, periodo);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit TEXT NOT NULL REFERENCES clients(cuit) ON DELETE CASCADE,
    cuil TEXT NOT NULL,
    nombre TEXT NOT NULL,
    remuneracion_bruta REAL NOT NULL,
    fecha_ingreso TEXT,
    convenio TEXT,
    activo INTEGER NOT NULL DEFAULT 1,
    UNIQUE (cuit, cuil)
);

CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit TEXT NOT NULL REFERENCES clients(cuit) ON DELETE CASCADE,
    obligacion TEXT NOT NULL,
    periodo TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('presentada', 'pagada', 'presentada_y_pagada')),
    importe REAL,
    referencia TEXT,
    fecha TEXT NOT NULL DEFAULT (date('now')),
    UNIQUE (cuit, obligacion, periodo)
);

CREATE TABLE IF NOT EXISTS calendar_overrides (
    obligacion TEXT NOT NULL,
    periodo TEXT NOT NULL,
    digito INTEGER NOT NULL CHECK (digito BETWEEN 0 AND 9),
    vencimiento TEXT NOT NULL,
    fuente TEXT,
    PRIMARY KEY (obligacion, periodo, digito)
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    fuente TEXT,
    url TEXT,
    impuesto TEXT,
    jurisdiccion TEXT,
    tipo TEXT NOT NULL DEFAULT 'documento',
    vigente_desde TEXT,
    vigente_hasta TEXT,
    client_cuit TEXT,
    content_hash TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    heading TEXT,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    subject TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS kb_embeddings (
    chunk_id INTEGER NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector TEXT NOT NULL,
    PRIMARY KEY (chunk_id, model)
);
"""

CLIENT_COLUMNS = frozenset({
    "cuit", "razon_social", "tipo_persona", "condicion_iva", "iibb_regimen", "iibb_jurisdicciones", "empleador",
    "agente_retencion_sicore", "agente_recaudacion_iibb", "cierre_ejercicio_mes", "actividad", "contacto",
})
CLIENT_JSON_FIELDS = ("iibb_jurisdicciones",)
CLIENT_BOOL_FIELDS = ("empleador", "agente_retencion_sicore", "agente_recaudacion_iibb")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # Tools run in worker threads: one connection guarded by a re-entrant lock.
        self.lock = threading.RLock()
        new = not Path(self.path).exists()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        if new:
            try:
                os.chmod(self.path, 0o600)  # sensitive taxpayer data: owner-only
            except OSError:  # pragma: no cover
                pass
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA secure_delete = ON")  # deleted rows are zeroed on disk
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- generic helpers (all parameterized: never interpolate user input into SQL) ----
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    @contextmanager
    def transaction(self):
        """Atomic multi-statement write: commit on success, rollback on error."""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # -- clients ---------------------------------------------------------
    @staticmethod
    def _client_from_row(row: dict[str, Any]) -> dict[str, Any]:
        for f in CLIENT_JSON_FIELDS:
            row[f] = json.loads(row[f] or "[]")
        for f in CLIENT_BOOL_FIELDS:
            row[f] = bool(row[f])
        return row

    def upsert_client(self, data: dict[str, Any]) -> dict[str, Any]:
        row = dict(data)
        for f in CLIENT_JSON_FIELDS:
            row[f] = json.dumps(row.get(f) or [], ensure_ascii=False)
        for f in CLIENT_BOOL_FIELDS:
            row[f] = int(bool(row.get(f)))
        unknown = set(row) - CLIENT_COLUMNS
        if unknown:  # column names are interpolated below: only whitelisted identifiers may reach SQL
            raise ValueError(f"Campos de cliente desconocidos: {sorted(unknown)}")
        cols = list(row.keys())
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "cuit")
        self.execute(  # identifiers whitelisted above, values parameterized
            f"INSERT INTO clients ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "  # nosec B608
            f"ON CONFLICT(cuit) DO UPDATE SET {updates}, updated_at=datetime('now')",
            [row[c] for c in cols],
        )
        return self.get_client(row["cuit"])  # type: ignore[return-value]

    def get_client(self, cuit: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM clients WHERE cuit = ?", [cuit])
        return self._client_from_row(row) if row else None

    def list_clients(self) -> list[dict[str, Any]]:
        return [self._client_from_row(r) for r in self.query("SELECT * FROM clients ORDER BY razon_social")]
