# contador-mcp — AI connector for Argentine accounting firms

An MCP server built with **FastMCP** that turns any MCP-capable assistant (Claude Desktop, Claude Code,
Cursor…) into a co-pilot for an **estudio contable**: it knows each client's profile, computes the
monthly compliance calendar (by CUIT terminación, holidays included), runs the core calculators
(IVA, F.931, SAC, Ganancias anticipos, Convenio Multilateral, SICORE) and answers regulatory questions
from a **hybrid RAG knowledge base with citations**.

- Research & product-market fit, accountant's day/month: [`docs/01-research-pmf-and-accountant-day.md`](docs/01-research-pmf-and-accountant-day.md)
- Design decisions & full tool catalog: [`docs/02-design-and-tools.md`](docs/02-design-and-tools.md)
- How the RAG works (learning notes): [`docs/03-rag-how-it-works.md`](docs/03-rag-how-it-works.md)

## Quick start

```bash
pip install -e ".[pdf]"          # add ,dense for local neural embeddings (fastembed)
contador-mcp                     # stdio (Claude Desktop / Claude Code)
contador-mcp --transport http --port 8000   # streamable HTTP at http://127.0.0.1:8000/mcp
```

Data lives in `~/.contador-mcp/` (override with `--data-dir` or `CONTADOR_DATA_DIR`): a SQLite DB with
clients, invoices, employees, filings, calendar overrides and the knowledge index.

### Connect to Claude Code
```bash
claude mcp add contador -- contador-mcp
```

### Connect to Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "contador": {
      "command": "contador-mcp",
      "env": { "CONTADOR_DATA_DIR": "/Users/me/estudio-data" }
    }
  }
}
```

### Environment variables
| Variable | Default | Meaning |
|---|---|---|
| `CONTADOR_DATA_DIR` | `~/.contador-mcp` | DB + user overrides (`rates.json`, `feriados.json`) |
| `CONTADOR_DENSE_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | fastembed model for dense retrieval; `none` disables it. Used only if `fastembed` is installed. |

## What the assistant can do (30 tools)

| Area | Tools |
|---|---|
| Clients | `client_register`, `client_update`, `client_list`, `client_get`, `client_add_note` |
| Calendar | `obligations_for_client`, `deadlines`, `upcoming_deadlines`, `mark_filed`, `calendar_set_override` |
| Monthly flow | `month_start_checklist`, `month_end_checklist`, `portfolio_overview` |
| IVA & invoices | `import_mis_comprobantes`, `add_invoice`, `list_invoices`, `iva_position` |
| Payroll | `employee_upsert`, `employee_list`, `payroll_contributions`, `sac_calculation` |
| Ganancias / IIBB / SICORE | `ganancias_anticipos`, `iibb_convenio_multilateral`, `sicore_retencion_ganancias`, `rates_get` |
| Knowledge (RAG) | `knowledge_search`, `knowledge_add_document`, `knowledge_list_documents`, `knowledge_remove_document`, `knowledge_status` |

Resources: `contador://clients`, `contador://clients/{cuit}`, `contador://calendar/{month}`,
`contador://knowledge/documents`. Prompts: `inicio_de_mes`, `cierre_de_mes`, `consulta_normativa`,
`informe_mensual_cliente`.

### Example conversation
> "Registrá a ACME SA, CUIT 30-71234567-1, sociedad RI, Convenio Multilateral CABA/PBA, con empleados
> y agente SICORE. ¿Qué tengo que hacer esta semana con toda la cartera?"

The assistant calls `client_register`, then `upcoming_deadlines(days=7)`, and answers with the dated
list (e.g. *06/10 pago de sueldos, 09/10 F.931 período 2026-09, 15/10 CM03…*), flagging anything
`VENCIDA_SIN_REGISTRO`.

## Design principles
1. **Dates are computed, never guessed** — nominal rule by CUIT digit → next business day (holiday
   table) → official override if loaded.
2. **Law comes from RAG with citations** — every chunk carries source, jurisdiction and vigencia;
   client notes are only retrievable for that client.
3. **Numbers come from calculators with editable rate tables** that always return `advertencias`.

> ⚠️ Rate tables, holiday lists and day-patterns are defaults to be verified against official ARCA /
> Comisión Arbitral / provincial publications. The knowledge corpus is an orientative summary, not
> legal advice.

## Development
```bash
pip install -e ".[dev]"
pytest -q
```

## Roadmap
- Monotributo module (categories, recategorización, exclusion risk).
- ARCA web services (WSAA/WSFE, padrón) and SIRADIG / Libro IVA Digital exports.
- Provincial calendars (ARBA, AGIP, etc.) loaded as overrides automatically.
- Cross-encoder re-ranker and query rewriting for the RAG; scheduled ingestion of Boletín Oficial.
- Multi-user auth for the HTTP deployment (FastMCP auth providers).
