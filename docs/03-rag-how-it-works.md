# 03 — How the RAG works (learning notes)

## Big picture
RAG = **Retrieve** relevant passages from a trusted corpus, then let the LLM **generate** an answer
grounded in them. It fixes two LLM weaknesses that matter a lot for Argentine tax work: stale
knowledge (RGs change monthly) and unverifiable claims (no citations).

```
document ──► chunker ──► chunks ──► BM25 index ─────────┐
   (md/txt/pdf + metadata)    └──► bi-encoder vectors ─┤
query ─────────────────────────────────────────────────┤──► filters ─► RRF fusion ─► top-k + citas ─► LLM
```

## 1. Chunking (`rag/text.py`)
Split by markdown headings (keeps the "breadcrumb" `Título › Sección` for citations), then by
paragraphs up to ~1200 chars with a small overlap so a sentence cut at a boundary still appears whole
in one chunk. Small chunks → precise retrieval; big chunks → more context. Legal text benefits from
section-aware chunking because articles are natural units.

## 2. Lexical retrieval — BM25 (`rag/retrievers.py`)
`score(q, d) = Σ_t IDF(t) · f(t,d)(k1+1) / (f(t,d) + k1(1 − b + b·|d|/avgdl))`
- **IDF**: rare terms ("SIRCREB", "F931") weigh more than common ones.
- **Saturation (k1)**: the 10th repetition of a word adds little.
- **Length normalization (b)**: long chunks don't win just by being long.
Normalization matters in Spanish: we strip accents, fold plurals and join codes ("F.931" → `f931`).

## 3. Dense retrieval — embeddings (deep-learning part)
A **bi-encoder** (a transformer such as multilingual MiniLM) maps any text to a fixed vector
(e.g. 384 dims). Training with contrastive learning pulls paraphrases together and pushes unrelated
texts apart, so *"cargas sociales"* lands near *"contribuciones patronales"* even with no shared
words. Similarity = cosine. Vectors are cached in SQLite per model so the corpus is embedded once.
Enable with `pip install -e ".[dense]"` (fastembed runs the model locally with ONNX, no GPU).

**Next level:** a **cross-encoder re-ranker** feeds `(query, chunk)` jointly through a transformer
and outputs a relevance score. It's slower (one forward pass per pair) but more accurate — use it
on the top 20 from the hybrid stage.

## 4. Hybrid fusion — Reciprocal Rank Fusion
BM25 and cosine scores live on different scales. RRF uses only ranks:
`RRF(d) = Σ_r 1 / (60 + rank_r(d))`. A chunk ranked high by both wins; each result reports
`recuperado_por: ["bm25", "denso"]` so you can see which retriever found it.

## 5. Metadata filters — the "law changes" problem
Every document carries `impuesto`, `jurisdiccion`, `vigente_desde/hasta`, `tipo` and optionally
`client_cuit`. `vigente_al="2026-03-01"` answers *"what applied then?"*; client notes are isolated so
one client's context never leaks into another's answers.

## 6. Three memories, not one
| Memory | Store | Why |
|---|---|---|
| Facts about clients | SQLite tables | exact, queryable, auditable |
| Rules (dates, rates) | Python + JSON tables | deterministic: an LLM must not "guess" a due date |
| Knowledge (law, criteria, notes) | RAG index | fuzzy questions in natural language, with citations |

## Ideas to experiment with
- Evaluate retrieval with a small gold set: 30 real questions → expected chunk; measure recall@5
  with BM25 vs hybrid.
- Query rewriting: let the LLM generate 2–3 reformulations (the `consulta_normativa` prompt already
  suggests it) and fuse all of them with RRF.
- Fine-tune the bi-encoder on accountant Q&A pairs (contrastive loss) once you have usage data.
