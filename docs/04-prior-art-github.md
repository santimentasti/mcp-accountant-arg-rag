# 04 — Prior art on GitHub (what exists, what we reused)

Survey done in October 2026 to avoid rebuilding what already exists and to speed up the knowledge base.

## Big picture
- Existing **ARCA MCP servers** focus on one job: **issuing electronic invoices** (WSAA + WSFE). None
  covers the estudio's monthly cycle (calendar across a portfolio, checklists, IVA/F.931/IIBB/SICORE
  calculators) or a normative RAG with citations. That's our positioning: complementary, not competing.
  A future version can *compose* with an invoicing MCP instead of reimplementing WSFE.
- The highest-value reuse is **data**, not code: holiday calendars and ARCA parameter tables are tedious to
  keep current and easy to get subtly wrong.

## Projects reviewed

| Project | What it is | License | What we took |
|---|---|---|---|
| [enzonotario/esjs-argentina-datos-api](https://github.com/enzonotario/esjs-argentina-datos-api) (ArgentinaDatos) | Public API + versioned JSON of Argentine data, including **feriados** per year (scraped from argentina.gob.ar) | MIT | `data/feriados.json` 2023–2027 regenerated from it. It caught a holiday my hand-made table missed — **09/11/2026 (visita del papa León XIV)** — which shifts real due dates. Future: fetch `api.argentinadatos.com/v1/feriados/{año}` to refresh. |
| [WhyNotHugo/django-afip](https://github.com/WhyNotHugo/django-afip) | Django app for ARCA e-invoicing, with fixtures of WSFE parameter tables | ISC | `data/arca_tablas.json` (36 receipt types, 36 document types, VAT rates, tax types, concepts, **receiver VAT condition codes for RG 5616**, optionals, currencies) → `arca_codes` tool + RAG doc `11-tablas-codigos-arca.md`; IVA logic now knows all class-A documents (e.g. Recibo A) that give tax credit. |
| [reingart/pyafipws](https://github.com/reingart/pyafipws) | The classic Python library for ARCA web services (WSFE, WSFEX, padrón, CPE…) | GPL-3.0 | **No code copied** (GPL would be viral for a closed SaaS). Reference for a future ARCA web-services integration. |
| [ramide1/arca-mcp](https://github.com/ramide1/arca-mcp) | TypeScript MCP wrapping WSAA/WSFE parameter queries | MIT | **Security lessons (anti-patterns)**: an LLM-controlled `currentDir` parameter is concatenated into an `execSync('openssl …')` shell string (command injection + path traversal), and the key password is passed on the command line (visible in `ps`). Our design: no shell, no LLM-controlled paths outside an allow-listed inbox. |
| AMKRS3/keycae-mcp, codespar mcp-afip, crossi-dev Velora | Invoicing / commerce MCPs (listed in MCP directories) | — | Market signal only (repo not accessible / hosted product). Confirms invoicing is crowded; compliance workflow is not. |
| 0xKoller/mcp-argentina-datos | MCP over ArgentinaDatos (FX, inflation, holidays) | — | Idea for later: a "macro" tool (dólar, IPC for ajuste por inflación). |
| centriqs/argentina-fiscal-calendar (ClawHub skill) | Prompt-level knowledge of ARCA due-date rules | — | Validation that "¿qué vence esta semana?" is a top use case; we compute it deterministically instead. |

## License hygiene
Third-party data is attributed in `NOTICE` and inside each data file (`_nota`). Only permissive-licensed
(MIT/ISC) data was imported.
