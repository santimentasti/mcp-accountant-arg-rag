"""Calculators: payroll contributions (F.931), SAC, Ganancias anticipos, Convenio Multilateral, SICORE.

All rates come from the rates table (data/rates.json, overridable). Every result carries
`advertencias` so the assistant can tell the user what must be double-checked.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable


def _r(x: float) -> float:
    return round(x + 0.0, 2)


# ---------------------------------------------------------------- seguridad social
def payroll_contributions(
    employees: Iterable[dict],
    rates: dict,
    categoria_empleador: str = "general",
    art_porcentaje: float | None = None,
    art_fijo_por_empleado: float = 0.0,
    seguro_vida_por_empleado: float = 0.0,
    tope_base_imponible: float | None = None,
    detraccion_por_empleado: float | None = None,
) -> dict:
    ss = rates["seguridad_social"]
    aportes_rates = ss["aportes_empleado"]
    contrib_rates = ss["contribuciones_empleador"]
    sipa_key = "grandes_servicios_comercio" if categoria_empleador == "grandes_servicios_comercio" else "general"
    sipa_rate = contrib_rates["sipa_inssjp_fne_aaff"][sipa_key]
    os_rate = contrib_rates["obra_social"]
    tope = tope_base_imponible if tope_base_imponible is not None else ss.get("tope_base_imponible_maxima")
    detraccion = detraccion_por_empleado if detraccion_por_empleado is not None else ss.get("detraccion_por_empleado")

    advertencias = [f"Alícuotas por defecto ({ss['fuente']}). Verificar vigencia y convenio colectivo."]
    if tope is None:
        advertencias.append("No se aplicó tope de base imponible máxima de aportes (pasar `tope_base_imponible`).")
    if detraccion is None:
        advertencias.append("No se aplicó detracción de contribuciones (pasar `detraccion_por_empleado` si corresponde).")
    if art_porcentaje is None:
        advertencias.append("ART no incluida en porcentaje: indicar la alícuota del contrato con la ART (`art_porcentaje`).")

    detalle = []
    tot = {"bruto": 0.0, "aportes": 0.0, "contribuciones": 0.0, "art": 0.0, "seguro_vida": 0.0}
    for e in employees:
        bruto = float(e["remuneracion_bruta"])
        base_aportes = min(bruto, tope) if tope else bruto
        aportes = {k: _r(base_aportes * v / 100) for k, v in aportes_rates.items()}
        base_contrib = max(0.0, bruto - (detraccion or 0.0))
        contrib = {
            "sipa_inssjp_fne_aaff": _r(base_contrib * sipa_rate / 100),
            "obra_social": _r(bruto * os_rate / 100),
        }
        art = _r(bruto * (art_porcentaje or 0) / 100 + art_fijo_por_empleado)
        a_total, c_total = sum(aportes.values()), sum(contrib.values())
        detalle.append({
            "cuil": e.get("cuil"), "nombre": e.get("nombre"), "remuneracion_bruta": _r(bruto),
            "aportes": aportes, "aportes_total": _r(a_total), "neto_estimado": _r(bruto - a_total),
            "contribuciones": contrib, "contribuciones_total": _r(c_total), "art": art,
            "seguro_vida": _r(seguro_vida_por_empleado),
        })
        tot["bruto"] += bruto
        tot["aportes"] += a_total
        tot["contribuciones"] += c_total
        tot["art"] += art
        tot["seguro_vida"] += seguro_vida_por_empleado

    total_931 = tot["aportes"] + tot["contribuciones"] + tot["art"] + tot["seguro_vida"]
    return {
        "empleados": len(detalle),
        "categoria_empleador": sipa_key,
        "alicuotas": {"aportes_empleado_%": aportes_rates, "contribucion_sipa_%": sipa_rate, "contribucion_obra_social_%": os_rate},
        "totales": {k: _r(v) for k, v in tot.items()},
        "total_estimado_f931": _r(total_931),
        "costo_laboral_total": _r(tot["bruto"] + tot["contribuciones"] + tot["art"] + tot["seguro_vida"]),
        "detalle": detalle,
        "advertencias": advertencias,
    }


def sac_calculation(mejor_remuneracion_semestre: float, semestre: int, anio: int,
                    fecha_ingreso: str | None = None, fecha_egreso: str | None = None) -> dict:
    """SAC = 50% de la mayor remuneración mensual del semestre × días trabajados / días del semestre."""
    if semestre not in (1, 2):
        raise ValueError("semestre debe ser 1 o 2")
    start = date(anio, 1, 1) if semestre == 1 else date(anio, 7, 1)
    end = date(anio, 6, 30) if semestre == 1 else date(anio, 12, 31)
    worked_from = max(start, date.fromisoformat(fecha_ingreso)) if fecha_ingreso else start
    worked_to = min(end, date.fromisoformat(fecha_egreso)) if fecha_egreso else end
    dias_sem = (end - start).days + 1
    dias = max(0, (worked_to - worked_from).days + 1)
    sac = mejor_remuneracion_semestre / 2 * dias / dias_sem
    return {
        "semestre": f"{anio}-S{semestre}",
        "dias_semestre": dias_sem,
        "dias_trabajados": dias,
        "sac_bruto": _r(sac),
        "vencimiento_pago": (date(anio, 6, 30) if semestre == 1 else date(anio, 12, 18)).isoformat(),
        "fuente": "Ley 23.041 y Dto. 1078/84; LCT arts. 121-123",
        "nota": "El SAC está sujeto a aportes y contribuciones; se declara en el F.931 del período de pago.",
    }


# ---------------------------------------------------------------- ganancias
def ganancias_anticipos(tipo_persona: str, impuesto_determinado: float,
                        retenciones_percepciones: float = 0.0) -> dict:
    base = max(0.0, impuesto_determinado - retenciones_percepciones)
    if tipo_persona == "juridica":
        pcts = [25.0] + [8.33] * 9
    else:
        pcts = [20.0] * 5
    cuotas = [{"anticipo": i + 1, "porcentaje": p, "importe": _r(base * p / 100)} for i, p in enumerate(pcts)]
    if tipo_persona == "juridica":
        # Ajuste de redondeo: el total debe ser exactamente el 100% de la base.
        diff = _r(base - sum(c["importe"] for c in cuotas))
        cuotas[-1]["importe"] = _r(cuotas[-1]["importe"] + diff)
    return {
        "base_anticipos": _r(base),
        "cantidad": len(cuotas),
        "anticipos": cuotas,
        "fuente": "RG 4034-E (AFIP/ARCA): sociedades 1° 25% + 9 de 8,33%; personas humanas 5 de 20%.",
        "advertencias": [
            "Existen importes mínimos por debajo de los cuales no corresponde ingresar anticipos y regímenes de reducción "
            "(opción de reducción por proyección): verificar en la normativa vigente.",
        ],
    }


# ---------------------------------------------------------------- IIBB convenio multilateral
def convenio_multilateral(
    ingresos_por_jurisdiccion: dict[str, float],
    gastos_por_jurisdiccion: dict[str, float],
    base_imponible_mes: float,
    alicuotas: dict[str, float],
    deducciones: dict[str, float] | None = None,
) -> dict:
    """Régimen general (art. 2 CM): coeficiente = 50% ingresos + 50% gastos del último balance cerrado."""
    deducciones = deducciones or {}
    jur = sorted(set(ingresos_por_jurisdiccion) | set(gastos_por_jurisdiccion))
    ti = sum(ingresos_por_jurisdiccion.values())
    tg = sum(gastos_por_jurisdiccion.values())
    if ti <= 0:
        raise ValueError("Los ingresos totales deben ser mayores a cero")
    advertencias = []
    if tg <= 0:
        advertencias.append("Sin gastos informados: el coeficiente se calculó 100% sobre ingresos.")
    filas, total_impuesto, total_a_pagar = [], 0.0, 0.0
    for j in jur:
        ci = ingresos_por_jurisdiccion.get(j, 0.0) / ti
        cg = gastos_por_jurisdiccion.get(j, 0.0) / tg if tg > 0 else ci
        coef = round(0.5 * ci + 0.5 * cg, 4) if tg > 0 else round(ci, 4)
        base_j = base_imponible_mes * coef
        alic = alicuotas.get(j)
        if alic is None:
            advertencias.append(f"Falta alícuota para {j}: no se calculó el impuesto.")
        imp = base_j * (alic or 0) / 100
        ded = deducciones.get(j, 0.0)
        a_pagar = max(0.0, imp - ded)
        total_impuesto += imp
        total_a_pagar += a_pagar
        filas.append({
            "jurisdiccion": j, "coef_ingresos": round(ci, 4), "coef_gastos": round(cg, 4),
            "coeficiente_unificado": coef, "base_imponible": _r(base_j), "alicuota_%": alic,
            "impuesto": _r(imp), "retenciones_percepciones_saldos": _r(ded), "a_pagar": _r(a_pagar),
            "saldo_a_favor": _r(max(0.0, ded - imp)),
        })
    suma = round(sum(f["coeficiente_unificado"] for f in filas), 4)
    if abs(suma - 1) > 0.0005:
        advertencias.append(f"La suma de coeficientes es {suma} (≠ 1) por redondeo: ajustar en la jurisdicción de mayor coeficiente.")
    return {
        "jurisdicciones": filas,
        "suma_coeficientes": suma,
        "impuesto_total": _r(total_impuesto),
        "a_pagar_total": _r(total_a_pagar),
        "fuente": "Convenio Multilateral art. 2 (régimen general). Regímenes especiales arts. 6 a 13 no contemplados.",
        "advertencias": advertencias + [
            "Gastos computables (art. 3 CM): no computar costo de materias primas y mercaderías, obras/servicios "
            "contratados para revender, propaganda y publicidad, tributos, intereses, ni honorarios/sueldos de "
            "directores y socios en la parte que exceda el 1% de la utilidad. Los coeficientes surgen del último "
            "balance cerrado en el año calendario anterior (CM05)."
        ],
    }


# ---------------------------------------------------------------- SICORE
def sicore_retencion_ganancias(
    rates: dict,
    concepto: str,
    importe_pago: float,
    inscripto: bool = True,
    pagos_previos_mes: float = 0.0,
    retenciones_previas_mes: float = 0.0,
    alicuota_override: float | None = None,
    minimo_override: float | None = None,
) -> dict:
    table = rates["sicore_ganancias_rg830"]
    conceptos = table["conceptos"]
    if concepto not in conceptos:
        raise ValueError(f"Concepto desconocido {concepto!r}. Opciones: {', '.join(conceptos)}")
    c = conceptos[concepto]
    advertencias = [f"Tabla por defecto ({table['fuente']})."]
    alic = alicuota_override if alicuota_override is not None else (
        c["alicuota_inscripto"] if inscripto else c["alicuota_no_inscripto"])
    if alic is None:
        raise ValueError(
            f"El concepto {concepto!r} usa escala para inscriptos: indicar `alicuota_override` "
            "(alícuota marginal de la escala vigente) y `minimo_override`."
        )
    if inscripto:
        minimo = minimo_override if minimo_override is not None else c["minimo_no_sujeto"]
        if minimo is None:
            raise ValueError(f"Falta mínimo no sujeto para {concepto!r}: indicar `minimo_override`.")
        acumulado = pagos_previos_mes + importe_pago
        base = max(0.0, acumulado - minimo)
        retencion = max(0.0, base * alic / 100 - retenciones_previas_mes)
        metodo = "(pagos del mes al mismo sujeto y concepto − mínimo no sujeto) × alícuota − retenciones previas del mes"
    else:
        minimo, base = 0.0, importe_pago
        retencion = importe_pago * alic / 100
        metodo = "importe del pago × alícuota de no inscripto (sin mínimo no sujeto)"
    return {
        "concepto": concepto,
        "descripcion": c["descripcion"],
        "inscripto": inscripto,
        "alicuota_%": alic,
        "minimo_no_sujeto": minimo,
        "base_sujeta": _r(base),
        "retencion": _r(retencion),
        "importe_neto_a_pagar": _r(importe_pago - retencion),
        "metodo": metodo,
        "advertencias": advertencias + [
            "El importe del pago debe ser neto de IVA. Existe un importe mínimo de retención por debajo del cual no se retiene: verificar.",
            "Verificar certificados de exclusión del beneficiario (consulta en ARCA) antes de retener.",
        ],
    }
