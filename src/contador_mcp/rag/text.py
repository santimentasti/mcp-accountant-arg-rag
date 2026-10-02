"""Spanish-aware normalization, tokenization and markdown-aware chunking."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

STOPWORDS = set(
    """a al algo algunas algunos ante antes como con contra cual cuando de del desde donde durante e el ella
    ellas ellos en entre era es esa esas ese eso esos esta estas este esto estos fue fueron ha han hasta hay
    la las le les lo los mas me mi mientras muy no nos o otra otras otro otros para pero por porque que quien
    se sea segun ser si sin sobre son su sus tambien tiene tienen todo todos tu un una uno unos y ya cada
    debe deben puede pueden cual cuales sido esta estan""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    t = strip_accents(text.lower())
    # Join codes like "F.931", "art.28", "RG 5.616" -> "f931", "art28"? keep words separated, join dotted numbers.
    t = re.sub(r"(?<=[a-z0-9])\.(?=[0-9])", "", t)
    return t


def stem(token: str) -> str:
    """Very light Spanish stemmer: plural folding only (keeps legal terms recognisable)."""
    if token.isdigit() or len(token) <= 4:
        return token
    if token.endswith("ces"):
        return token[:-3] + "z"
    if token.endswith("es") and len(token) > 5 and token[-3] not in "aeiou":
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN_RE.findall(normalize(text)) if t not in STOPWORDS]


@dataclass
class Chunk:
    heading: str
    text: str


def chunk_markdown(text: str, max_chars: int = 1200, overlap: int = 150) -> list[Chunk]:
    """Split by markdown headings, then by paragraphs up to max_chars with a small overlap."""
    sections: list[tuple[str, list[str]]] = []
    path: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if any(line.strip() for line in current):
            sections.append((" > ".join(path), current.copy()))
        current.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2).strip()]
            continue
        current.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        buf = ""
        for p in paragraphs:
            while len(p) > max_chars:  # very long paragraph: hard split
                head, p = p[:max_chars], p[max_chars - overlap:]
                if buf:
                    chunks.append(Chunk(heading, buf))
                    buf = ""
                chunks.append(Chunk(heading, head))
            if buf and len(buf) + len(p) + 2 > max_chars:
                chunks.append(Chunk(heading, buf))
                tail = buf[-overlap:]
                buf = tail[tail.find(" ") + 1:] + "\n\n" + p if overlap else p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf:
            chunks.append(Chunk(heading, buf))
    return chunks


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Minimal `---\\nkey: value\\n---` front matter parser (no YAML dependency)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[end + 4:].lstrip("\n")
