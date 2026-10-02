from datetime import date

import pytest

from contador_mcp.domain import taxes
from contador_mcp.domain.cuit import InvalidCuit, format_cuit, normalize_cuit
from contador_mcp.domain.iva import iva_position
from contador_mcp.rag.text import chunk_markdown, parse_front_matter, tokenize


def test_cuit_validation():
    assert normalize_cuit("30-71234567-1") == "30712345671"
    assert format_cuit("30712345671") == "30-71234567-1"
    with pytest.raises(InvalidCuit):
        normalize_cuit("30-71234567-2")  # wrong check digit
    with pytest.raises(InvalidCuit):
        normalize_cuit("123")


def test_iva_position_credit_rules_and_balances():
    invs = [
        {"direccion": "emitida", "tipo_comprobante": 1, "neto_gravado": 1000, "iva": 210},
        {"direccion": "emitida", "tipo_comprobante": 3, "neto_gravado": 100, "iva": 21},  # NC A resta
        {"direccion": "recibida", "tipo_comprobante": 1, "neto_gravado": 500, "iva": 105, "percepciones_iva": 15},
        {"direccion": "recibida", "tipo_comprobante": 6, "neto_gravado": 0, "iva": 50},  # B: no computa
    ]
    r = iva_position(invs, saldo_tecnico_anterior=20, retenciones_iva_sufridas=10)
    assert r["debito_fiscal"] == 189
    assert r["credito_fiscal"] == 105
    assert r["impuesto_determinado"] == 64  # 189 - 105 - 20
    assert r["saldo_a_pagar"] == 39  # 64 - 15 - 10
    assert any("NO es crédito fiscal" in a for a in r["alertas"])


def test_iva_position_saldo_a_favor():
    r = iva_position([{"direccion": "recibida", "tipo_comprobante": 1, "iva": 300, "percepciones_iva": 50},
                      {"direccion": "emitida", "tipo_comprobante": 1, "iva": 100}])
    assert r["saldo_tecnico_a_favor_proximo_periodo"] == 200
    assert r["saldo_a_pagar"] == 0
    assert r["saldo_libre_disponibilidad_nuevo"] == 50


def test_ganancias_anticipos_sum_to_base():
    r = taxes.ganancias_anticipos("juridica", 1_000_000, 100_000)
    assert r["cantidad"] == 10 and r["anticipos"][0]["importe"] == 225_000
    assert round(sum(a["importe"] for a in r["anticipos"]), 2) == 900_000
    assert taxes.ganancias_anticipos("humana", 500)["cantidad"] == 5


def test_convenio_multilateral_coefficients():
    r = taxes.convenio_multilateral({"CABA": 600, "Buenos Aires": 400}, {"CABA": 200, "Buenos Aires": 800},
                                    1_000_000, {"CABA": 3.0, "Buenos Aires": 3.5}, {"CABA": 1000})
    by = {f["jurisdiccion"]: f for f in r["jurisdicciones"]}
    assert by["CABA"]["coeficiente_unificado"] == 0.4  # 0.5*0.6 + 0.5*0.2
    assert by["Buenos Aires"]["coeficiente_unificado"] == 0.6
    assert by["CABA"]["impuesto"] == 12_000 and by["CABA"]["a_pagar"] == 11_000
    assert r["suma_coeficientes"] == 1


def test_sicore_inscripto_accumulates_monthly(svc):
    r = taxes.sicore_retencion_ganancias(svc.rates, "locaciones_obra_servicios", 100_000, True,
                                         pagos_previos_mes=50_000, retenciones_previas_mes=500)
    assert r["base_sujeta"] == 150_000 - 67_170
    assert r["retencion"] == round((150_000 - 67_170) * 0.02 - 500, 2)
    no = taxes.sicore_retencion_ganancias(svc.rates, "alquileres_inmuebles", 100_000, False)
    assert no["retencion"] == 28_000
    with pytest.raises(ValueError, match="escala"):
        taxes.sicore_retencion_ganancias(svc.rates, "honorarios_profesionales", 100_000, True)


def test_payroll_and_sac(svc):
    r = taxes.payroll_contributions([{"remuneracion_bruta": 1_000_000, "cuil": "x", "nombre": "A"}], svc.rates,
                                    art_porcentaje=2.5)
    assert r["totales"]["aportes"] == 170_000
    assert r["totales"]["contribuciones"] == 240_000  # 18% + 6%
    assert r["totales"]["art"] == 25_000
    s = taxes.sac_calculation(1_000_000, 1, 2026)
    assert s["sac_bruto"] == 500_000
    half = taxes.sac_calculation(1_000_000, 2, 2026, fecha_ingreso="2026-10-01")
    assert half["dias_trabajados"] == 92 and half["sac_bruto"] == round(500_000 * 92 / 184, 2)


def test_text_utils():
    assert "f931" in tokenize("Vence el F.931")
    assert tokenize("facturas") == tokenize("factura")
    meta, body = parse_front_matter("---\ntitle: X\nimpuesto: iva\n---\n# H\n\ntexto")
    assert meta == {"title": "X", "impuesto": "iva"} and body.startswith("# H")
    chunks = chunk_markdown("# A\n\n" + "\n\n".join(["párrafo " * 40] * 10) + "\n\n## B\n\nfin")
    assert len(chunks) > 2 and chunks[-1].heading == "A > B"
