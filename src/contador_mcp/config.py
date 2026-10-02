"""Runtime configuration: where the SQLite DB, rate tables and knowledge seed live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SEED_KNOWLEDGE_DIR = PACKAGE_DIR / "knowledge"
PACKAGE_DATA_DIR = PACKAGE_DIR / "data"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    dense_model: str | None  # fastembed model name; None disables dense retrieval

    @property
    def db_path(self) -> Path:
        return self.data_dir / "contador.sqlite3"

    def data_file(self, name: str) -> Path:
        """User override in data_dir wins over the packaged default."""
        override = self.data_dir / name
        return override if override.exists() else PACKAGE_DATA_DIR / name


def load_settings(data_dir: str | Path | None = None) -> Settings:
    base = Path(
        data_dir
        or os.environ.get("CONTADOR_DATA_DIR")
        or Path.home() / ".contador-mcp"
    ).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    dense = os.environ.get(
        "CONTADOR_DENSE_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    if dense.strip().lower() in {"", "none", "off", "0"}:
        dense = None
    return Settings(data_dir=base, dense_model=dense)
