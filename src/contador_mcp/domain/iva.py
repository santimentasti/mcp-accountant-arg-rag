"""IVA monthly position (posición mensual de IVA) from invoices."""

from __future__ import annotations

from typing import Iterable

# Códigos de comprobante ARCA (tabla de tipos de comprobante, WSFE).
TIPOS = {
    1: "Factura A", 2: "Nota de Débito A", 3: "Nota de Crédito A",
    6: "Factura B", 7: "Nota de Débito B", 8: "Nota de Crédito B",
    11: "Factura C", 12: "Nota de Débito C", 13: "Nota de Crédito C",
    51: "Factura M", 52: "Nota de Débito M", 53: "Nota de Crédito M",
    201: "Factura de Crédito Electrónica MiPyME A", 202: "Nota de Débito Electrónica MiPyME A",
    203: "Nota de Crédito Electrónica MiPyME A",
    206: "Factura de Crédito Electrónica MiPyME B", 207: "Nota de Débito Electrónica MiPyME B",
    208: "Nota de Crédito Electrónica MiPyME B",
    211: "Factura de Crédito Electrónica MiPyME C", 212: "Nota de Débito Electrónica MiPyME C",
    213: "Nota de Crédito Electrónica MiPyME C",
}
NOTAS_CREDITO = {3, 8, 13, 53, 203, 208, 213}
CLASE_A_M = {1, 2, 3, 51, 52, 53, 201, 202, 203}  # discriminan IVA -> crédito fiscal computable
CLASE_C = {11, 12, 13, 211, 212, 213}


def tipo_nombre(code: int) -> str:
    return TIPOS.get(int(code), f"Tipo {code}")


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def iva_position(
    invoices: Iterable[dict],
    saldo_tecnico_anterior: float = 0.0,
    saldo_libre_disponibilidad_anterior: float = 0.0,
    retenciones_iva_sufridas: float = 0.0,
    percepciones_iva_adicionales: float = 0.0,
) -> dict:
    debito = credito = percepciones = 0.0
    neto_ventas = neto_compras = 0.0
    alertas: list[str] = []
    n_emit = n_rec = 0
    sin_credito = 0.0

    for inv in invoices:
        tipo = int(inv["tipo_comprobante"])
        sign = -1.0 if tipo in NOTAS_CREDITO else 1.0
        fx = float(inv.get("tipo_cambio") or 1.0)
        iva = float(inv.get("iva") or 0) * fx * sign
        neto = float(inv.get("neto_gravado") or 0) * fx * sign
        if inv["direccion"] == "emitida":
            n_emit += 1
            if tipo in CLASE_C:
                alertas.append(
                    f"Comprobante emitido clase C ({tipo_nombre(tipo)} {inv.get('punto_venta')}-{inv.get('numero')}): "
                    "un Responsable Inscripto no debería emitir clase C."
                )
                continue
            debito += iva
            neto_ventas += neto
        else:
            n_rec += 1
            percepciones += float(inv.get("percepciones_iva") or 0) * fx * sign
            if tipo in CLASE_A_M:
                credito += iva
                neto_compras += neto
            elif iva:
                sin_credito += iva

    if sin_credito:
        alertas.append(
            f"Hay ${_r(sin_credito):,.2f} de IVA en comprobantes recibidos B/C que NO es crédito fiscal computable "
            "(solo computan comprobantes que discriminan IVA: A, M, FCE A)."
        )

    percepciones += percepciones_iva_adicionales
    saldo_tecnico = debito - credito - saldo_tecnico_anterior
    impuesto_determinado = max(0.0, saldo_tecnico)
    saldo_tecnico_a_favor = max(0.0, -saldo_tecnico)
    pagos_a_cuenta = percepciones + retenciones_iva_sufridas + saldo_libre_disponibilidad_anterior
    a_pagar = max(0.0, impuesto_determinado - pagos_a_cuenta)
    sld_nuevo = max(0.0, pagos_a_cuenta - impuesto_determinado)

    if n_emit == 0:
        alertas.append("No hay comprobantes emitidos cargados para el período: ¿falta importar Mis Comprobantes emitidos?")
    if n_rec == 0:
        alertas.append("No hay comprobantes recibidos cargados para el período: ¿falta importar Mis Comprobantes recibidos?")

    return {
        "comprobantes_emitidos": n_emit,
        "comprobantes_recibidos": n_rec,
        "neto_gravado_ventas": _r(neto_ventas),
        "neto_gravado_compras": _r(neto_compras),
        "debito_fiscal": _r(debito),
        "credito_fiscal": _r(credito),
        "saldo_tecnico_anterior_aplicado": _r(saldo_tecnico_anterior),
        "impuesto_determinado": _r(impuesto_determinado),
        "saldo_tecnico_a_favor_proximo_periodo": _r(saldo_tecnico_a_favor),
        "percepciones_iva": _r(percepciones),
        "retenciones_iva": _r(retenciones_iva_sufridas),
        "saldo_libre_disponibilidad_anterior_aplicado": _r(saldo_libre_disponibilidad_anterior),
        "saldo_a_pagar": _r(a_pagar),
        "saldo_libre_disponibilidad_nuevo": _r(sld_nuevo),
        "alertas": alertas,
        "explicacion": (
            "Saldo técnico = débito − crédito − saldo técnico a favor anterior. Si es positivo es el impuesto "
            "determinado, al que se le restan percepciones, retenciones y saldo de libre disponibilidad. "
            "El saldo técnico a favor solo compensa débitos futuros; el de libre disponibilidad puede usarse "
            "para otros impuestos (previa presentación/solicitud)."
        ),
    }
