# 02 — Design & tool catalog

## Decisions (from the design Q&A)

| Topic | Decision |
|---|---|
| Primary user | **Estudio contable** managing a portfolio of client CUITs |
| Client data | **SQLite + CSV import** (ARCA "Mis Comprobantes"), manual entry via tools |
| RAG | **Hybrid & pluggable**: BM25 always on; dense embeddings (fastembed) fused with RRF when installed |
| v1 scope | **IVA + Libro IVA, Seguridad social/sueldos, Ganancias, IIBB (local & Convenio Multilateral), SICORE** (monotributo out of scope for v1) |
| Transport | **stdio + streamable HTTP** (same code, CLI flag) |
| Language | Outputs & corpus in **Spanish**; code in English |
| KB ingest | Estudio can add **md / txt / pdf** documents with source + vigencia metadata |

## Architecture — three kinds of "memory"

```
           ┌──────────────────────────── FastMCP server (contador-mcp) ───────────────────────────┐
 LLM  ───► │ tools / resources / prompts                                                          │
           │                                                                                      │
           │  1. FACTS about clients  ── SQLite: clients, notes, invoices, employees, filings     │
           │  2. RULES (deterministic) ── calendar engine (CUIT digit → due date, business days,   │
           │                             holidays, overrides) + calculators (IVA, F.931, SAC,     │
           │                             anticipos, CM coefficients, SICORE)                      │
           │  3. KNOWLEDGE (RAG)      ── chunks of laws/RGs/memos/client notes, hybrid retrieval, │
           │                             metadata filters (tax, jurisdicción, vigencia, cliente)   │
           └──────────────────────────────────────────────────────────────────────────────────────┘
```

Principle: **the LLM never invents a date, a rate or a law.** Dates come from the rules engine,
numbers from calculators (with configurable rate tables flagged "verificar vigencia"), and
normative claims from `knowledge_search` with citations.

## Tool catalog

### Clients (portfolio memory)
| Tool | Purpose |
|---|---|
| `client_register` | Create/replace a client profile (CUIT validated with check digit). The profile drives which obligations apply. |
| `client_update` | Partial update of a profile. |
| `client_list` | Portfolio list. |
| `client_get` | Full profile + recent notes + derived obligations. |
| `client_add_note` | Persist client-specific context ("cobra con cheques diferidos", "agente de percepción ARBA desde 03/2026"). Notes are also indexed in RAG scoped to that client. |

### Calendar & status (rules engine)
| Tool | Purpose |
|---|---|
| `obligations_for_client` | Which obligations apply and why (derived from profile). |
| `deadlines` | Due dates for one client or the whole portfolio in a month, with status (pendiente/presentada/vencida). |
| `upcoming_deadlines` | Next N days across the portfolio — the "what do I do today" list. |
| `mark_filed` | Record that an obligation/period was filed/paid (amount, VEP/nro. transacción). |
| `calendar_set_override` | Load the official ARCA/Comarb date when it differs from the nominal rule. |

### Monthly workflows
| Tool | Purpose |
|---|---|
| `month_start_checklist` | Inicio de mes: docs to request, payroll, everything due days 1–15 with dates. |
| `month_end_checklist` | Cierre de mes: everything due 16–EOM + bookkeeping close tasks + IVA preview. |
| `portfolio_overview` | Estudio dashboard: clients × obligations status for a month. |

### Data & calculators
| Tool | Purpose |
|---|---|
| `import_mis_comprobantes` | Import ARCA "Mis Comprobantes" CSV (emitidos / recibidos), tolerant to format variants. |
| `add_invoice` | Manual invoice entry. |
| `list_invoices` | Invoices for a period with totals. |
| `iva_position` | Débito / crédito fiscal, percepciones, saldo técnico, saldo de libre disponibilidad. |
| `employee_upsert`, `employee_list` | Payroll roster. |
| `payroll_contributions` | Estimated aportes y contribuciones for F.931 for a period. |
| `sac_calculation` | Aguinaldo (Ley 23.041) proportional calc. |
| `ganancias_anticipos` | Anticipos schedule (RG 4034: sociedades 25% + 9×8,33%; humanas 5×20%) with due dates. |
| `iibb_convenio_multilateral` | Art. 2 coefficients (50% ingresos / 50% gastos) + tax per jurisdicción. |
| `sicore_retencion_ganancias` | Retención de Ganancias (RG 830 style: acumulado mensual − mínimo no sujeto × alícuota). |
| `rates_get` | Show the rate tables in use (and that they need verification). |

### Knowledge (RAG)
| Tool | Purpose |
|---|---|
| `knowledge_search` | Hybrid search with filters; returns chunks with citations. |
| `knowledge_add_document` | Ingest text or a file (md/txt/pdf) with metadata (fuente, url, impuesto, jurisdicción, vigencia, cliente). |
| `knowledge_list_documents` | Inventory of the corpus. |
| `knowledge_remove_document` | Remove an outdated document. |
| `knowledge_status` | Index stats & which retrievers are active. |

### Resources
- `contador://clients` · `contador://clients/{cuit}` · `contador://calendar/{month}` · `contador://knowledge/documents`

### Prompts
- `inicio_de_mes(cuit, month)` · `cierre_de_mes(cuit, month)` · `consulta_normativa(pregunta, cuit?)` · `informe_mensual_cliente(cuit, month)`

## Conventions
- `period` = **período fiscal** `YYYY-MM` (month the tax accrues). `month` = **calendar month**
  `YYYY-MM` in which the work/deadline happens. E.g. IVA period 2026-09 is due in month 2026-10.
- Due-date rules are **nominal** (published pattern by CUIT digit) rolled to the next business
  day using the holiday table; official dates can be pinned with `calendar_set_override`.
- Every calculator returns `advertencias` when it uses default rates that must be verified.
