# 01 — Research: market fit & the Argentine accountant's month

> Status: research snapshot, October 2026. Dates and thresholds change by RG (Resolución
> General) of ARCA — the server's knowledge base is designed to be updated, not hardcoded.

## 1. Big picture (key ideas)

1. **Compliance in Argentina is calendar-driven.** Almost every obligation is monthly and its due
   date depends on the **last digit of the CUIT**. The accountant's job is, to a large degree,
   *a scheduler + data pipeline + rules engine* running over ~20 clients at once.
2. **The month has two halves:**
   - **Start of month (días 1–12): "cerrar el mes anterior".** Collect invoices, bank
     statements, payroll novelties; liquidate sueldos; pay F.931 (seguridad social, ~day 9–11);
     autónomos (~day 5–7); SICORE 1st installment.
   - **Middle/end of month (días 13–31): "declarar y pagar".** IVA (day 18–22), IIBB / SIFERE
     (~day 15–20), monotributo (day 20), anticipos de Ganancias, SICORE 2nd fortnight;
     then pre-close: reconcile banks, accruals, prepare next month's checklist.
3. **The knowledge problem is as big as the data problem.** ARCA (ex-AFIP, renamed Oct-2024)
   publishes dozens of RGs per year; 2026 brought **Ley 27.799 de Inocencia Fiscal**
   (simplified Ganancias regime, pre-filled DDJJ, "tapón fiscal") and ongoing labor/tax reform.
   An LLM's training data goes stale → **RAG over a curated, dated, citable corpus** is the
   only safe way to answer "what applies to this client *this month*".
4. **The opportunity for an MCP:** not "another accounting system" (Xubio, Colppy, Tango,
   Contabilium, Aconpy already own the ledger), but an **AI-native co-pilot layer** that any
   assistant (Claude, Cursor, etc.) can plug into: knows the calendar, knows each client's
   profile, knows the law with citations, and produces checklists/drafts.

## 2. Product–market fit hypotheses

| Signal | Evidence | Implication |
|---|---|---|
| Large professional base | ~140k contadores matriculados; ~30k new CPN students/year | Big ICP of small estudios (1–10 people) with 20–150 clients each |
| Pain = deadline risk + regulatory churn | Every month: IVA, F.931, IIBB, SICORE, monotributo, each by CUIT digit; frequent RG changes | "Never miss a vencimiento" + "what changed this month" is a must-have, not a nice-to-have |
| Incumbents add AI inside their silo | Colppy, Aconpy market AI features inside their own ledger | Gap: a **cross-tool, assistant-native** layer (MCP) that works with whatever system the estudio uses |
| Cheap SaaS anchors | Cloud accounting USD 15–80/month | Price an MCP co-pilot per estudio (e.g. USD 20–60/mo) or per client CUIT |
| Monotributistas & PyMEs self-serve | Millions of monotributistas; recategorization twice/year; new simplified Ganancias | Second ICP: small companies' internal admin who want "what do I owe and when" without a full estudio |

**Riskiest assumptions to validate (talk to 10 estudios):**
1. Will accountants trust an AI answer if it cites the RG/ley article? (RAG with citations is the trust mechanism.)
2. Where does client data live today? (Excel + "Mis Comprobantes" downloads + Tango/Xubio). Import must be trivial.
3. Is the buyer the estudio owner (B2B2C) or the company's CFO/admin (B2B)?

## 3. A day/month in the life of an Argentine accountant (estudio contable)

### Monthly rhythm (for each client)

| When | Task | Output / form |
|---|---|---|
| Day 1–3 | Request docs from clients: facturas emitidas/recibidas, extractos bancarios, novedades de sueldos (altas/bajas, horas extra, licencias) | Checklist by client, WhatsApp/email chasing |
| Day 1–5 | Download **"Mis Comprobantes"** (ARCA) emitidos/recibidos; cross-check with client's files | CSV/Excel |
| Day 1–5 | **Liquidación de sueldos** (convenio colectivo, escalas vigentes), recibos | Recibos, Libro de Sueldos Digital |
| Day 5–7 | Autónomos payment (by CUIT digit) | VEP |
| Day 9–11 | **F.931 / SICOSS**: aportes y contribuciones de seguridad social | F.931 + VEP |
| Day ~9–13 | **SICORE** retenciones/percepciones Ganancias & IVA (1st fortnight / monthly DDJJ) | SICORE DDJJ |
| Day 10–15 | Load **Libro IVA Digital**: ventas & compras, check débito/crédito fiscal, percepciones sufridas | Libro IVA Digital |
| Day ~13–20 | **IIBB**: local (e.g. ARBA, AGIP) or **Convenio Multilateral (SIFERE)** coefficient distribution | DDJJ IIBB |
| Day 18–22 | **IVA F.2002** DDJJ + pago (CUIT 0-1: 18 · 2-3: 19 · 4-5: 20 · 6-7: 21 · 8-9: 22) | F.2002 + VEP |
| Day 20 | **Monotributo** cuota (clients in régimen simplificado) | VEP/débito |
| Mid-month | **Anticipos Ganancias / Bienes Personales** (per calendar) | VEP |
| Day 20–31 | Bookkeeping: asientos, conciliación bancaria, devengamientos, provisiones (aguinaldo, vacaciones), amortizaciones | Mayor, balance de sumas y saldos |
| Last week | Management report to client: impuestos pagados, posición IVA (saldo técnico / saldo de libre disponibilidad), cash projection, alerts for next month | Report / email |

### Annual & semi-annual peaks
- **Jan / Jul:** Monotributo recategorization (due ~Feb 5 / Aug 5).
- **Jun & Jul:** Aguinaldo (SAC) payable Jun 30 / Dec 18.
- **May–Jun:** Ganancias & Bienes Personales personas humanas (annual DDJJ, ~mid-June).
- **5 months after fiscal year-end:** Ganancias sociedades DDJJ + Estados Contables (balance) with
  auditor report, legalization at the Consejo Profesional.
- **Every month:** new RGs, CCT salary scales, IIBB alícuotas updates.

### Typical day of a staff accountant (mid-month)
1. 08:30 — Check ARCA "Domicilio Fiscal Electrónico" (e-ventanilla) notices for every client.
2. 09:00 — Today's vencimientos list (CUIT digits due today) → prepare & pay VEPs.
3. 11:00 — Answer client questions ("¿puedo facturar A a este cliente?", "¿me conviene pasar a RI?").
4. 14:00 — Load Libro IVA, reconcile with Mis Comprobantes, chase missing invoices.
5. 16:00 — Read Boletín Oficial / newsletter of the Consejo for new RGs; update internal notes.
6. 17:30 — Plan tomorrow: which clients still owe documentation.

**Where AI helps most:** (1) per-client calendar & checklist generation, (2) answering regulatory
questions **with citations**, (3) data checks (IVA position, missing invoices, monotributo
category drift), (4) drafting client communications, (5) remembering client-specific context
("this client is Convenio Multilateral with coefficients X; agente de percepción ARBA").

## 4. Approaches to "not lose context" (RAG and beyond)

| Approach | What it solves | Notes |
|---|---|---|
| **Lexical retrieval (BM25)** | Exact legal terms: "RG 5616", "art. 28", "F.931" | Zero dependencies, great for legal text where exact tokens matter |
| **Dense embeddings** | Paraphrases: "cuándo pago cargas sociales" ↔ "F.931 vencimiento" | Needs a model (local fastembed or API) |
| **Hybrid (BM25 + dense, fused with RRF)** | Best of both | Reciprocal Rank Fusion: `score = Σ 1/(k + rank_i)` — no score calibration needed |
| **Metadata filters** | Validity dates, jurisdiction, taxpayer type | Law changes → every chunk carries `vigente_desde`, `fuente`, `jurisdiccion` |
| **Structured memory per client** | Client profile, history, notes | Not RAG: a DB. RAG for *law*, DB for *facts about the client* |
| **Rules engine** | Deterministic calendar & thresholds | Never let an LLM "guess" a due date — compute it |

Deep-learning angle: a dense retriever is a **bi-encoder** — a transformer maps query and
passage independently to vectors; similarity = cosine. A **cross-encoder re-ranker** reads
(query, passage) jointly — more accurate, slower — typically applied to the top-k only.
