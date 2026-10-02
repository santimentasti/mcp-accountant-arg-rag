"""Knowledge base: documents -> chunks -> hybrid index, with metadata filters and citations."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

from ..db import Database
from .retrievers import BM25, Embedder, cosine_scores, normalize_vec, rank, rrf
from .text import chunk_markdown, parse_front_matter

META_FIELDS = ("fuente", "url", "impuesto", "jurisdiccion", "tipo", "vigente_desde", "vigente_hasta", "client_cuit")
NATIONAL = {None, "", "nacional"}


def read_file_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Para ingerir PDF instalar: pip install 'contador-mcp[pdf]'") from exc
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        return "\n\n".join(f"## Página {i}\n\n{t}" for i, t in enumerate(pages, 1) if t)
    raise ValueError(f"Formato no soportado: {suffix} (usar .md, .txt o .pdf)")


class KnowledgeBase:
    def __init__(self, db: Database, embedder: Embedder | None = None, dense_status: str = "deshabilitado"):
        self.db = db
        self.embedder = embedder
        self.dense_status = dense_status
        self._lock = threading.RLock()
        self._dirty = True
        self._rows: list[dict[str, Any]] = []
        self._bm25: BM25 | None = None
        self._vectors: list[list[float]] = []

    # ------------------------------------------------------------ ingestion
    def add_document(self, title: str, content: str, origin: str = "user", **meta: Any) -> dict:
        if not content.strip():
            raise ValueError("El documento está vacío")
        fm, body = parse_front_matter(content)
        title = fm.pop("title", None) or title
        for k, v in fm.items():
            if k in META_FIELDS and not meta.get(k):
                meta[k] = v
        meta = {k: (v or None) for k, v in meta.items() if k in META_FIELDS}
        meta.setdefault("tipo", "documento")
        meta["tipo"] = meta["tipo"] or "documento"
        content_hash = hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest()
        with self._lock:
            existing = self.db.one("SELECT id FROM kb_documents WHERE content_hash = ?", [content_hash])
            if existing:
                return {"document_id": existing["id"], "duplicado": True, "chunks": self._count_chunks(existing["id"])}
            cols = ["title", "content_hash", "origin", *meta.keys()]
            vals = [title, content_hash, origin, *meta.values()]
            cur = self.db.execute(
                f"INSERT INTO kb_documents ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
            )
            doc_id = cur.lastrowid
            chunks = chunk_markdown(body)
            self.db.conn.executemany(
                "INSERT INTO kb_chunks (document_id, ord, heading, text) VALUES (?, ?, ?, ?)",
                [(doc_id, i, c.heading, c.text) for i, c in enumerate(chunks)],
            )
            self.db.conn.commit()
            self._dirty = True
        return {"document_id": doc_id, "duplicado": False, "chunks": len(chunks), "titulo": title}

    def add_file(self, path: str | Path, title: str | None = None, **meta: Any) -> dict:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"No existe el archivo: {p}")
        return self.add_document(title or p.stem.replace("-", " ").replace("_", " "), read_file_text(p), **meta)

    def remove_document(self, document_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM kb_documents WHERE id = ?", [document_id])
            self._dirty = True
            return cur.rowcount > 0

    def sync_seed(self, directory: Path) -> dict:
        """Ingest packaged seed docs; replace a seed doc whose file content changed (same title)."""
        added = updated = 0
        for path in sorted(directory.glob("*.md")):
            meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
            title = meta.get("title") or path.stem
            content_hash = hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest()
            prev = self.db.one("SELECT id, content_hash FROM kb_documents WHERE origin='seed' AND title=?", [title])
            if prev and prev["content_hash"] == content_hash:
                continue
            if prev:
                self.remove_document(prev["id"])
                updated += 1
            else:
                added += 1
            self.add_document(title, path.read_text(encoding="utf-8"), origin="seed")
        return {"agregados": added, "actualizados": updated}

    def _count_chunks(self, doc_id: int) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM kb_chunks WHERE document_id = ?", [doc_id])["n"]

    # ------------------------------------------------------------ indexing
    def _rebuild(self) -> None:
        rows = self.db.query(
            "SELECT c.id AS chunk_id, c.heading, c.text, d.id AS document_id, d.title, d.fuente, d.url, d.impuesto, "
            "d.jurisdiccion, d.tipo, d.vigente_desde, d.vigente_hasta, d.client_cuit "
            "FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id ORDER BY c.id"
        )
        # Title and heading are part of the indexed text: they carry most of the topical signal.
        texts = [f"{r['title']}\n{r['heading'] or ''}\n{r['text']}" for r in rows]
        self._rows = rows
        self._bm25 = BM25(texts)
        self._vectors = self._load_vectors(rows, texts) if self.embedder else []
        self._dirty = False

    def _load_vectors(self, rows: list[dict], texts: list[str]) -> list[list[float]]:
        assert self.embedder is not None
        model = self.embedder.name
        stored = {
            r["chunk_id"]: json.loads(r["vector"])
            for r in self.db.query("SELECT chunk_id, vector FROM kb_embeddings WHERE model = ?", [model])
        }
        missing = [i for i, r in enumerate(rows) if r["chunk_id"] not in stored]
        if missing:
            new = self.embedder.embed([texts[i] for i in missing])
            self.db.conn.executemany(
                "INSERT OR REPLACE INTO kb_embeddings (chunk_id, model, vector) VALUES (?, ?, ?)",
                [(rows[i]["chunk_id"], model, json.dumps(normalize_vec(v))) for i, v in zip(missing, new)],
            )
            self.db.conn.commit()
            for i, v in zip(missing, new):
                stored[rows[i]["chunk_id"]] = normalize_vec(v)
        return [stored[r["chunk_id"]] for r in rows]

    # ------------------------------------------------------------ search
    @staticmethod
    def _matches(row: dict, impuesto: str | None, jurisdiccion: str | None, vigente_al: str | None,
                 client_cuit: str | None, tipo: str | None) -> bool:
        if row["client_cuit"] and row["client_cuit"] != client_cuit:
            return False  # client notes never leak into other clients' searches
        if impuesto:
            taxes = {t.strip().lower() for t in (row["impuesto"] or "").split(",") if t.strip()}
            if taxes and impuesto.lower() not in taxes and "general" not in taxes:
                return False
        if jurisdiccion:
            j = (row["jurisdiccion"] or "").lower()
            if j not in NATIONAL and j != jurisdiccion.lower():
                return False
        if tipo and (row["tipo"] or "") != tipo:
            return False
        if vigente_al:
            if row["vigente_desde"] and row["vigente_desde"] > vigente_al:
                return False
            if row["vigente_hasta"] and row["vigente_hasta"] < vigente_al:
                return False
        return True

    def search(self, query: str, top_k: int = 5, impuesto: str | None = None, jurisdiccion: str | None = None,
               vigente_al: str | None = None, client_cuit: str | None = None, tipo: str | None = None,
               modo: str = "hibrido") -> list[dict]:
        if vigente_al:
            date.fromisoformat(vigente_al)  # validate
        with self._lock:
            if self._dirty:
                self._rebuild()
            rows = self._rows
            if not rows:
                return []
            allowed = lambda i: self._matches(rows[i], impuesto, jurisdiccion, vigente_al, client_cuit, tipo)  # noqa: E731
            rankings: dict[str, list[int]] = {}
            if modo in ("hibrido", "lexico"):
                rankings["bm25"] = rank(self._bm25.scores(query), allowed)[: top_k * 4]  # type: ignore[union-attr]
            if modo in ("hibrido", "semantico") and self.embedder and self._vectors:
                qv = self.embedder.embed([query])[0]
                rankings["denso"] = rank(cosine_scores(qv, self._vectors), allowed, min_score=0.0)[: top_k * 4]
            fused = rrf(list(rankings.values()))[:top_k]
        results = []
        for idx, score in fused:
            r = rows[idx]
            cita = f"{r['title']}" + (f" › {r['heading']}" if r["heading"] else "")
            if r["fuente"]:
                cita += f" ({r['fuente']}"
                cita += f", vigente desde {r['vigente_desde']})" if r["vigente_desde"] else ")"
            results.append({
                "cita": cita,
                "texto": r["text"],
                "titulo": r["title"],
                "seccion": r["heading"],
                "fuente": r["fuente"],
                "url": r["url"],
                "impuesto": r["impuesto"],
                "jurisdiccion": r["jurisdiccion"],
                "tipo": r["tipo"],
                "vigente_desde": r["vigente_desde"],
                "vigente_hasta": r["vigente_hasta"],
                "document_id": r["document_id"],
                "chunk_id": r["chunk_id"],
                "score_rrf": round(score, 5),
                "recuperado_por": [name for name, rk in rankings.items() if idx in rk],
            })
        return results

    def list_documents(self, client_cuit: str | None = None) -> list[dict]:
        return self.db.query(
            "SELECT d.id, d.title, d.fuente, d.url, d.impuesto, d.jurisdiccion, d.tipo, d.vigente_desde, "
            "d.vigente_hasta, d.client_cuit, d.origin, d.created_at, COUNT(c.id) AS chunks "
            "FROM kb_documents d LEFT JOIN kb_chunks c ON c.document_id = d.id "
            "WHERE d.client_cuit IS NULL OR d.client_cuit = ? GROUP BY d.id ORDER BY d.origin, d.title",
            [client_cuit],
        )

    def status(self) -> dict:
        docs = self.db.one("SELECT COUNT(*) AS n FROM kb_documents")["n"]
        chunks = self.db.one("SELECT COUNT(*) AS n FROM kb_chunks")["n"]
        return {
            "documentos": docs,
            "chunks": chunks,
            "retriever_lexico": "BM25 activo",
            "retriever_denso": self.dense_status,
            "fusion": "Reciprocal Rank Fusion (k=60)" if self.embedder else "solo BM25",
        }
