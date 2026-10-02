"""Inicio de mes / cierre de mes checklists derived from the client profile and its deadlines."""

from __future__ import annotations

from .calendar import Deadline
from .periods import parse_period, shift_period


def _task(tarea: str, detalle: str, herramienta: str | None = None, fecha: str | None = None) -> dict:
    t = {"tarea": tarea, "detalle": detalle}
    if herramienta:
        t["herramienta_sugerida"] = herramienta
    if fecha:
        t["fecha_objetivo"] = fecha
    return t


def _deadline_tasks(deadlines: list[Deadline]) -> list[dict]:
    out = []
    for d in deadlines:
        out.append({
            "tarea": f"{d.nombre} — período {d.periodo}",
            "detalle": f"{d.formulario}. {d.nota}".strip(" ."),
            "fecha_objetivo": d.vencimiento.isoformat(),
            "estado": d.estado,
            "codigo_obligacion": d.codigo,
            "herramienta_sugerida": "mark_filed (al presentar/pagar)",
        })
    return out


def month_start(client: dict, month: str, deadlines: list[Deadline]) -> dict:
    prev = shift_period(month, -1)
    tasks: list[dict] = [
        _task("Revisar Domicilio Fiscal Electrónico (DFE / e-ventanilla)",
              "Notificaciones, intimaciones o requerimientos de ARCA y fiscos provinciales."),
        _task("Solicitar documentación del período " + prev,
              "Facturas de compra no electrónicas, extractos bancarios, resúmenes de tarjetas, certificados de "
              "retenciones/percepciones sufridas, contratos nuevos, movimientos de caja."),
        _task("Descargar 'Mis Comprobantes' emitidos y recibidos de " + prev,
              "Importarlos para controlar contra la documentación del cliente y preparar el Libro IVA Digital.",
              "import_mis_comprobantes"),
    ]
    if client.get("empleador"):
        tasks += [
            _task("Relevar novedades de sueldos de " + prev,
                  "Altas/bajas (alta temprana en Simplificación Registral), horas extra, ausencias, licencias, "
                  "vacaciones, embargos, cambios de categoría o escala del convenio."),
            _task("Liquidar sueldos de " + prev + " y emitir recibos",
                  "Aplicar escalas vigentes del CCT; pagar hasta el 4° día hábil.", "payroll_contributions"),
            _task("Generar Libro de Sueldos Digital y F.931",
                  "Primero el Libro de Sueldos Digital, luego el F.931 desde la misma liquidación.", "payroll_contributions"),
        ]
    if client.get("agente_retencion_sicore"):
        tasks.append(_task("Compilar retenciones/percepciones practicadas en " + prev,
                           "Base para la DDJJ SICORE y los certificados a entregar a proveedores.", "sicore_retencion_ganancias"))
    if client.get("iibb_regimen") in ("local", "convenio_multilateral"):
        tasks.append(_task("Controlar percepciones y retenciones de IIBB sufridas",
                           "SIRCREB (bancarias), SIRCAR/SIRPEI y padrones provinciales; conciliarlas con la DDJJ del mes.",
                           "iibb_convenio_multilateral" if client.get("iibb_regimen") == "convenio_multilateral" else None))
    early = [d for d in deadlines if d.vencimiento.day <= 15]
    return {
        "fase": "inicio_de_mes",
        "mes": month,
        "periodo_que_se_cierra": prev,
        "cliente": {"cuit": client["cuit"], "razon_social": client["razon_social"]},
        "tareas": tasks,
        "vencimientos_dias_1_a_15": _deadline_tasks(early),
        "consultas_kb_sugeridas": [
            "novedades normativas del mes ARCA resoluciones generales",
            *(["escalas salariales convenio colectivo actualización"] if client.get("empleador") else []),
        ],
    }


def month_end(client: dict, month: str, deadlines: list[Deadline]) -> dict:
    _, mon = parse_period(month)
    prev = shift_period(month, -1)
    tasks: list[dict] = []
    if client.get("condicion_iva") == "responsable_inscripto":
        tasks.append(_task("Cerrar Libro IVA Digital y posición de IVA de " + prev,
                           "Controlar débito/crédito, percepciones y saldo técnico antes del F.2002.", "iva_position"))
    tasks += [
        _task("Conciliación bancaria del mes", "Extractos vs. mayor de bancos; partidas pendientes, impuesto a los débitos y créditos, comisiones."),
        _task("Registrar asientos del mes", "Ventas, compras, cobranzas, pagos, sueldos y cargas sociales, impuestos devengados."),
        _task("Devengamientos y provisiones",
              "SAC y vacaciones proporcionales (≈1/12 mensual) con sus cargas sociales, honorarios, servicios a pagar, "
              "impuestos devengados no vencidos."),
        _task("Amortizaciones de bienes de uso y control de inventario si corresponde", "Según política contable."),
        _task("Control de comprobantes faltantes",
              "Cruzar Mis Comprobantes vs. registraciones; pedir al cliente lo faltante.", "list_invoices"),
        _task("Preparar informe mensual al cliente",
              "Impuestos pagados, posición IVA, saldos a favor, vencimientos del mes próximo, alertas normativas.",
              "prompt: informe_mensual_cliente"),
    ]
    if client.get("empleador") and mon in (6, 12):
        tasks.append(_task("Liquidar y pagar el SAC (aguinaldo)",
                           "1ª cuota hasta 30/06, 2ª hasta 18/12 (con 4 días hábiles de gracia).", "sac_calculation"))
    if int(client.get("cierre_ejercicio_mes") or 12) == mon:
        tasks.append(_task("CIERRE DE EJERCICIO",
                           "Inventario y valuación, ajuste por inflación contable (RT 6), provisión de Ganancias, "
                           "conciliaciones de todas las cuentas, preparación de Estados Contables y DDJJ (5° mes).",
                           "knowledge_search"))
    if client.get("tipo_persona") == "humana" and mon in (4, 5):
        tasks.append(_task("Preparar DDJJ anual Ganancias / Bienes Personales (vence en junio)",
                           "Relevar patrimonio al 31/12 y rentas; evaluar Ganancias simplificada (Ley 27.799).",
                           "knowledge_search"))
    if client.get("iibb_regimen") == "convenio_multilateral" and mon in (5, 6):
        tasks.append(_task("Preparar CM05 (vence 30/06)", "Ingresos y gastos por jurisdicción del último balance → nuevos coeficientes.",
                           "iibb_convenio_multilateral"))
    tasks.append(_task("Planificar el próximo mes", "Revisar vencimientos y documentación a pedir.", "upcoming_deadlines"))
    late = [d for d in deadlines if d.vencimiento.day > 15]
    return {
        "fase": "cierre_de_mes",
        "mes": month,
        "cliente": {"cuit": client["cuit"], "razon_social": client["razon_social"]},
        "tareas": tasks,
        "vencimientos_dias_16_a_fin": _deadline_tasks(late),
        "consultas_kb_sugeridas": ["cierre de mes buenas prácticas devengamiento provisiones"],
    }
