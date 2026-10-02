from pathlib import Path

from conftest import ACME, TODAY, HashEmbedder

from contador_mcp.service import ContadorService

FIX = Path(__file__).parent / "fixtures"


def test_import_mis_comprobantes_both_formats(acme):
    r = acme.import_mis_comprobantes(ACME, "recibida", file_path=str(FIX / "mis_comprobantes_recibidos.csv"))
    assert (r["leidos"], r["insertados"]) == (3, 3)
    assert r["periodos"] == ["2026-09"]
    again = acme.import_mis_comprobantes(ACME, "recibida", file_path=str(FIX / "mis_comprobantes_recibidos.csv"))
    assert again["duplicados_omitidos"] == 3
    e = acme.import_mis_comprobantes(ACME, "emitida", csv_text=(FIX / "mis_comprobantes_emitidos_nuevo.csv").read_text())
    assert e["insertados"] == 2 and not e["advertencias"]

    pos = acme.iva_position(ACME, "2026-09", percepciones_iva_adicionales=3000)
    assert pos["debito_fiscal"] == 52_500  # 42.000 + 10.500
    assert pos["credito_fiscal"] == 18_900  # 21.000 - 2.100 (NC)
    assert pos["saldo_a_pagar"] == 52_500 - 18_900 - 3000
    assert pos["vencimiento_f2002"] == "2026-10-19"
    lst = acme.list_invoices(ACME, "2026-09", "recibida")
    assert lst["totales"]["recibida"]["iva"] == 18_900


def test_add_invoice_and_payroll(acme):
    r = acme.add_invoice(ACME, "emitida", "2026-09-30", 1, total=121, neto_gravado=100, iva=21)
    assert r["ok"] and not r["alertas"]
    acme.employee_upsert(ACME, "20-12345678-6", "Juan Pérez", 1_500_000)
    p = acme.payroll_contributions(ACME, "2026-09", art_porcentaje=3)
    assert p["empleados"] == 1 and p["vencimiento_f931"] == "2026-10-09"


def test_ganancias_anticipos_with_dates(acme):
    r = acme.ganancias_anticipos(ACME, 1_000_000)
    assert r["ejercicio_base"] == "2025-12"
    assert r["anticipos"][0]["vencimiento"] == "2026-06-16"  # 13/06 sábado, 15/06 feriado (Güemes)
    assert r["anticipos"][4]["periodo_obligacion"] == "2025-12/A05"


def test_knowledge_search_seed_and_citations(svc):
    res = svc.knowledge_search("¿las facturas B dan crédito fiscal?", top_k=3)["resultados"]
    assert any("IVA" in r["titulo"] and "Crédito fiscal" in (r["seccion"] or "") for r in res)
    assert all(r["cita"] and r["recuperado_por"] == ["bm25"] for r in res)
    f931 = svc.knowledge_search("vencimiento F.931 aportes", impuesto="seguridad_social")["resultados"]
    assert all(r["impuesto"] in ("seguridad_social", "general") for r in f931)


def test_client_notes_are_private_and_retrievable(acme):
    acme.client_register("20-11111111-2", "Otro", "humana", "responsable_inscripto", "local")
    acme.client_add_note(ACME, "El cliente cobra con cheques diferidos a 60 días y tiene percepción de ARBA del 3%.",
                         ["iibb"])
    hit = acme.knowledge_search("cheques diferidos", cuit=ACME)["resultados"]
    assert hit and hit[0]["tipo"] == "nota_cliente"
    assert not any(r["tipo"] == "nota_cliente" for r in acme.knowledge_search("cheques diferidos")["resultados"])
    assert not any(r["tipo"] == "nota_cliente"
                   for r in acme.knowledge_search("cheques diferidos", cuit="20111111112")["resultados"])
    assert acme.client_get(ACME)["notas_recientes"][0]["tags"] == ["iibb"]


def test_add_remove_document_and_vigencia(svc, tmp_path):
    p = tmp_path / "rg-ficticia.txt"
    p.write_text("La RG 9999 establece el régimen ficticio de percepción de pruebas unitarias.", encoding="utf-8")
    r = svc.knowledge_add_document("RG 9999", file_path=str(p), fuente="RG 9999", impuesto="iva",
                                   vigente_desde="2027-01-01")
    assert r["chunks"] == 1
    assert svc.knowledge_add_document("RG 9999", file_path=str(p), fuente="RG 9999")["duplicado"]
    assert svc.knowledge_search("RG 9999 percepción pruebas", vigente_al="2027-02-01")["resultados"][0]["titulo"] == "RG 9999"
    assert not any(x["titulo"] == "RG 9999"
                   for x in svc.knowledge_search("RG 9999 percepción pruebas", vigente_al="2026-10-01")["resultados"])
    assert svc.knowledge_remove_document(r["document_id"])["eliminado"]
    assert not any(x["titulo"] == "RG 9999" for x in svc.knowledge_search("RG 9999")["resultados"])


def test_hybrid_retrieval_with_dense_embedder(settings):
    svc = ContadorService(settings, embedder=HashEmbedder(), today=TODAY)
    assert "test-hash-embedder" in svc.knowledge_status()["retriever_denso"]
    res = svc.knowledge_search("crédito fiscal facturas", top_k=5)["resultados"]
    assert any("denso" in r["recuperado_por"] for r in res)
    assert svc.knowledge_search("crédito fiscal", modo="semantico")["resultados"]
    # embeddings are cached in SQLite and reused
    n = svc.db.one("SELECT COUNT(*) AS n FROM kb_embeddings")["n"]
    assert n == svc.db.one("SELECT COUNT(*) AS n FROM kb_chunks")["n"]


def test_seed_sync_is_idempotent(settings):
    ContadorService(settings, today=TODAY)
    again = ContadorService(settings, today=TODAY)
    assert again.seed_result == {"agregados": 0, "actualizados": 0}


def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_pdf_ingestion(svc, tmp_path):
    p = tmp_path / "memo.pdf"
    p.write_bytes(_minimal_pdf("Memo interno: regimen de percepcion SIRTAC aplicable desde marzo"))
    r = svc.knowledge_add_document("Memo SIRTAC", file_path=str(p), tipo="documento")
    assert r["chunks"] >= 1
    hit = svc.knowledge_search("SIRTAC percepcion")["resultados"][0]
    assert hit["titulo"] == "Memo SIRTAC" and "Página 1" in hit["seccion"]
