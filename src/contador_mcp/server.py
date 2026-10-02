"""FastMCP server: exposes the ContadorService as MCP tools, resources and prompts."""

from __future__ import annotations

import argparse
import json
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from .config import Settings, load_settings
from .rag.retrievers import Embedder
from .service import ContadorService

INSTRUCTIONS = """\
Conector para estudios contables de Argentina (cumplimiento mensual de empresas).

Reglas de uso:
1. FECHAS: nunca calcular vencimientos de memoria. Usar `deadlines`, `upcoming_deadlines` o los checklists.
2. NORMATIVA: antes de afirmar algo sobre leyes, RG de ARCA, alícuotas o plazos, llamar a `knowledge_search`
   y citar el campo `cita` de los fragmentos usados. Si no hay resultados, decirlo: no inventar.
3. NÚMEROS: usar las calculadoras (iva_position, payroll_contributions, ganancias_anticipos,
   iibb_convenio_multilateral, sicore_retencion_ganancias) y trasladar sus `advertencias` al usuario.
4. CONTEXTO DEL CLIENTE: consultar `client_get` y guardar hallazgos relevantes con `client_add_note`
   (quedan indexados en la base de conocimiento solo para ese cliente).
5. Flujo mensual: inicio de mes -> `month_start_checklist`; cierre -> `month_end_checklist`;
   vista del estudio -> `portfolio_overview`. Registrar cada presentación con `mark_filed`.
Convenciones: `periodo` = período fiscal AAAA-MM; `month` = mes calendario AAAA-MM en que vence/se trabaja.
Responder en español rioplatense profesional.
"""

Cuit = Annotated[str, Field(description="CUIT del cliente (con o sin guiones), ej. 30-71234567-1")]
Month = Annotated[str, Field(description="Mes calendario AAAA-MM (mes en que se trabaja / vence)", pattern=r"^\d{4}-\d{2}$")]
Period = Annotated[str, Field(description="Período fiscal AAAA-MM", pattern=r"^\d{4}-\d{2}$")]
Direccion = Literal["emitida", "recibida"]

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


def create_server(settings: Settings | None = None, embedder: Embedder | None = None,
                  service: ContadorService | None = None) -> FastMCP:
    svc = service or ContadorService(settings or load_settings(), embedder=embedder)
    mcp = FastMCP("contador-mcp", instructions=INSTRUCTIONS, version="0.1.0")
    mcp.service = svc  # type: ignore[attr-defined]  # handy for tests / embedding

    # ================================================================ clients
    @mcp.tool(annotations=WRITE, tags={"clientes"})
    def client_register(
        cuit: Cuit,
        razon_social: str,
        tipo_persona: Literal["humana", "juridica"],
        condicion_iva: Literal["responsable_inscripto", "exento", "no_alcanzado"],
        iibb_regimen: Literal["local", "convenio_multilateral", "exento", "no_inscripto"],
        iibb_jurisdicciones: Annotated[list[str] | None, Field(description="Ej. ['CABA', 'Buenos Aires']")] = None,
        empleador: bool = False,
        agente_retencion_sicore: bool = False,
        agente_recaudacion_iibb: bool = False,
        cierre_ejercicio_mes: Annotated[int, Field(ge=1, le=12, description="Mes de cierre del ejercicio comercial")] = 12,
        actividad: str | None = None,
        contacto: str | None = None,
    ) -> dict:
        """Registra (o reemplaza) el perfil de un cliente del estudio. El perfil determina qué obligaciones
        mensuales y anuales le aplican. Valida el dígito verificador del CUIT."""
        return svc.client_register(cuit, razon_social, tipo_persona, condicion_iva, iibb_regimen, iibb_jurisdicciones,
                                   empleador, agente_retencion_sicore, agente_recaudacion_iibb, cierre_ejercicio_mes,
                                   actividad, contacto)

    @mcp.tool(annotations=WRITE, tags={"clientes"})
    def client_update(
        cuit: Cuit,
        razon_social: str | None = None,
        tipo_persona: Literal["humana", "juridica"] | None = None,
        condicion_iva: Literal["responsable_inscripto", "exento", "no_alcanzado"] | None = None,
        iibb_regimen: Literal["local", "convenio_multilateral", "exento", "no_inscripto"] | None = None,
        iibb_jurisdicciones: list[str] | None = None,
        empleador: bool | None = None,
        agente_retencion_sicore: bool | None = None,
        agente_recaudacion_iibb: bool | None = None,
        cierre_ejercicio_mes: Annotated[int | None, Field(ge=1, le=12)] = None,
        actividad: str | None = None,
        contacto: str | None = None,
    ) -> dict:
        """Actualiza parcialmente el perfil de un cliente (solo los campos indicados)."""
        return svc.client_update(cuit, razon_social=razon_social, tipo_persona=tipo_persona, condicion_iva=condicion_iva,
                                 iibb_regimen=iibb_regimen, iibb_jurisdicciones=iibb_jurisdicciones, empleador=empleador,
                                 agente_retencion_sicore=agente_retencion_sicore,
                                 agente_recaudacion_iibb=agente_recaudacion_iibb,
                                 cierre_ejercicio_mes=cierre_ejercicio_mes, actividad=actividad, contacto=contacto)

    @mcp.tool(annotations=READ_ONLY, tags={"clientes"})
    def client_list() -> list[dict]:
        """Lista la cartera de clientes del estudio."""
        return svc.client_list()

    @mcp.tool(annotations=READ_ONLY, tags={"clientes"})
    def client_get(cuit: Cuit) -> dict:
        """Perfil completo del cliente, notas recientes del estudio y obligaciones que le aplican."""
        return svc.client_get(cuit)

    @mcp.tool(annotations=WRITE, tags={"clientes", "conocimiento"})
    def client_add_note(
        cuit: Cuit,
        note: Annotated[str, Field(description="Hecho o criterio a recordar sobre el cliente")],
        tags: Annotated[list[str] | None, Field(description="Impuestos/temas relacionados, ej. ['iva', 'iibb']")] = None,
    ) -> dict:
        """Guarda contexto del cliente (comportamiento, acuerdos, criterios). Se indexa en la base de conocimiento
        y solo se recupera en búsquedas de ese mismo cliente."""
        return svc.client_add_note(cuit, note, tags)

    # ================================================================ calendar
    @mcp.tool(annotations=READ_ONLY, tags={"calendario"})
    def obligations_for_client(cuit: Cuit) -> list[dict]:
        """Obligaciones que aplican al cliente según su perfil (IVA, F.931, SICORE, IIBB, Ganancias...)."""
        return svc.obligations_for_client(cuit)

    @mcp.tool(annotations=READ_ONLY, tags={"calendario"})
    def deadlines(month: Month, cuit: Annotated[str | None, Field(description="Omitir para toda la cartera")] = None) -> dict:
        """Vencimientos de un mes calendario para un cliente o toda la cartera, con estado
        (pendiente / proxima / presentada / VENCIDA_SIN_REGISTRO)."""
        return svc.deadlines(month, cuit)

    @mcp.tool(annotations=READ_ONLY, tags={"calendario"})
    def upcoming_deadlines(
        days: Annotated[int, Field(ge=1, le=90)] = 7,
        cuit: str | None = None,
        desde: Annotated[str | None, Field(description="Fecha ISO AAAA-MM-DD; por defecto hoy")] = None,
    ) -> dict:
        """'¿Qué vence?': próximos vencimientos de la cartera en los siguientes N días y vencidos sin registro."""
        return svc.upcoming_deadlines(days, cuit, desde)

    @mcp.tool(annotations=WRITE, tags={"calendario"})
    def mark_filed(
        cuit: Cuit,
        obligacion: Annotated[str, Field(description="Código, ej. IVA_F2002, SEG_SOCIAL_F931, GAN_SOC_ANTICIPO")],
        periodo: Annotated[str, Field(description="Período tal como aparece en deadlines (ej. 2026-09 o 2025-12/A03)")],
        estado: Literal["presentada", "pagada", "presentada_y_pagada"] = "presentada_y_pagada",
        importe: float | None = None,
        referencia: Annotated[str | None, Field(description="Nro. de VEP, transacción o acuse")] = None,
        fecha: str | None = None,
    ) -> dict:
        """Registra que una obligación de un período fue presentada y/o pagada."""
        return svc.mark_filed(cuit, obligacion, periodo, estado, importe, referencia, fecha)

    @mcp.tool(annotations=WRITE, tags={"calendario"})
    def calendar_set_override(
        obligacion: str,
        periodo: str,
        vencimiento: Annotated[str, Field(description="Fecha oficial AAAA-MM-DD")],
        digitos: Annotated[list[int] | None, Field(description="Terminaciones de CUIT afectadas (todas si se omite)")] = None,
        fuente: Annotated[str | None, Field(description="Ej. 'RG ARCA xxxx - calendario 2026'")] = None,
    ) -> dict:
        """Fija la fecha oficial de un vencimiento cuando difiere de la regla nominal (calendario ARCA/Comarb/provincia)."""
        return svc.calendar_set_override(obligacion, periodo, vencimiento, digitos, fuente)

    # ================================================================ workflows
    @mcp.tool(annotations=READ_ONLY, tags={"flujo_mensual"})
    def month_start_checklist(cuit: Cuit, month: Month) -> dict:
        """Checklist de INICIO DE MES: documentación a pedir, liquidación de sueldos, descargas de ARCA y
        vencimientos de los días 1 a 15 con su estado."""
        return svc.month_start_checklist(cuit, month)

    @mcp.tool(annotations=READ_ONLY, tags={"flujo_mensual"})
    def month_end_checklist(cuit: Cuit, month: Month) -> dict:
        """Checklist de CIERRE DE MES: vencimientos días 16 a fin, conciliaciones, devengamientos, provisiones,
        cierre de ejercicio si corresponde y preview de IVA del período anterior."""
        return svc.month_end_checklist(cuit, month)

    @mcp.tool(annotations=READ_ONLY, tags={"flujo_mensual"})
    def portfolio_overview(month: Month) -> dict:
        """Tablero del estudio: por cliente, obligaciones del mes cumplidas, pendientes y vencidas sin registro."""
        return svc.portfolio_overview(month)

    # ================================================================ invoices / IVA
    @mcp.tool(annotations=WRITE, tags={"iva", "datos"})
    def import_mis_comprobantes(
        cuit: Cuit,
        direccion: Direccion,
        csv_text: Annotated[str | None, Field(description="Contenido del CSV exportado de 'Mis Comprobantes'")] = None,
        file_path: Annotated[str | None, Field(description="Ruta local al CSV (alternativa a csv_text)")] = None,
    ) -> dict:
        """Importa comprobantes emitidos o recibidos desde el CSV de 'Mis Comprobantes' de ARCA (ignora duplicados)."""
        return svc.import_mis_comprobantes(cuit, direccion, csv_text, file_path)

    @mcp.tool(annotations=WRITE, tags={"iva", "datos"})
    def add_invoice(
        cuit: Cuit,
        direccion: Direccion,
        fecha: Annotated[str, Field(description="AAAA-MM-DD")],
        tipo_comprobante: Annotated[int, Field(description="Código ARCA: 1 FA A, 3 NC A, 6 FA B, 11 FA C, 51 FA M, 201 FCE A...")],
        total: float,
        neto_gravado: float = 0.0,
        iva: float = 0.0,
        punto_venta: int = 0,
        numero: int = 0,
        contraparte_cuit: str | None = None,
        contraparte_nombre: str | None = None,
        neto_no_gravado: float = 0.0,
        exento: float = 0.0,
        percepciones_iva: float = 0.0,
        otros_tributos: float = 0.0,
        moneda: str = "PES",
        tipo_cambio: float = 1.0,
    ) -> dict:
        """Carga manual de un comprobante (ej. factura no electrónica o con percepciones discriminadas)."""
        return svc.add_invoice(cuit, direccion, fecha, tipo_comprobante, total, neto_gravado, iva, punto_venta, numero,
                               contraparte_cuit, contraparte_nombre, neto_no_gravado, exento, percepciones_iva,
                               otros_tributos, moneda, tipo_cambio)

    @mcp.tool(annotations=READ_ONLY, tags={"iva", "datos"})
    def list_invoices(cuit: Cuit, periodo: Period, direccion: Direccion | None = None,
                      limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> dict:
        """Comprobantes de un período con totales por dirección (notas de crédito restan)."""
        return svc.list_invoices(cuit, periodo, direccion, limit)

    @mcp.tool(annotations=READ_ONLY, tags={"iva"})
    def iva_position(
        cuit: Cuit,
        periodo: Period,
        saldo_tecnico_anterior: Annotated[float, Field(ge=0, description="Saldo técnico a favor del período anterior")] = 0.0,
        saldo_libre_disponibilidad_anterior: Annotated[float, Field(ge=0)] = 0.0,
        retenciones_iva_sufridas: Annotated[float, Field(ge=0)] = 0.0,
        percepciones_iva_adicionales: Annotated[float, Field(ge=0, description="Percepciones no cargadas en comprobantes")] = 0.0,
    ) -> dict:
        """Posición mensual de IVA: débito y crédito fiscal, saldo técnico, pagos a cuenta y saldo a pagar
        o de libre disponibilidad, con alertas y fecha de vencimiento del F.2002."""
        return svc.iva_position(cuit, periodo, saldo_tecnico_anterior, saldo_libre_disponibilidad_anterior,
                                retenciones_iva_sufridas, percepciones_iva_adicionales)

    # ================================================================ payroll
    @mcp.tool(annotations=WRITE, tags={"sueldos"})
    def employee_upsert(cuit: Cuit, cuil: str, nombre: str,
                        remuneracion_bruta: Annotated[float, Field(gt=0, description="Remuneración bruta mensual")],
                        fecha_ingreso: str | None = None, convenio: str | None = None, activo: bool = True) -> dict:
        """Alta o modificación de un empleado del cliente (marca al cliente como empleador)."""
        return svc.employee_upsert(cuit, cuil, nombre, remuneracion_bruta, fecha_ingreso, convenio, activo)

    @mcp.tool(annotations=READ_ONLY, tags={"sueldos"})
    def employee_list(cuit: Cuit, solo_activos: bool = True) -> list[dict]:
        """Nómina del cliente."""
        return svc.employee_list(cuit, solo_activos)

    @mcp.tool(annotations=READ_ONLY, tags={"sueldos"})
    def payroll_contributions(
        cuit: Cuit,
        periodo: Period,
        categoria_empleador: Literal["general", "grandes_servicios_comercio"] = "general",
        art_porcentaje: Annotated[float | None, Field(ge=0, description="% de la ART según contrato")] = None,
        art_fijo_por_empleado: float = 0.0,
        seguro_vida_por_empleado: float = 0.0,
        tope_base_imponible: Annotated[float | None, Field(description="Base imponible máxima vigente")] = None,
        detraccion_por_empleado: float | None = None,
    ) -> dict:
        """Estimación de aportes y contribuciones (F.931) del período por empleado y totales, con vencimiento."""
        return svc.payroll_contributions(cuit, periodo, categoria_empleador, art_porcentaje, art_fijo_por_empleado,
                                         seguro_vida_por_empleado, tope_base_imponible, detraccion_por_empleado)

    @mcp.tool(annotations=READ_ONLY, tags={"sueldos"})
    def sac_calculation(
        mejor_remuneracion_semestre: Annotated[float, Field(gt=0)],
        semestre: Literal[1, 2],
        anio: int,
        fecha_ingreso: str | None = None,
        fecha_egreso: str | None = None,
    ) -> dict:
        """Aguinaldo (SAC): 50% de la mejor remuneración mensual del semestre, proporcional a los días trabajados."""
        return svc.sac_calculation(mejor_remuneracion_semestre, semestre, anio, fecha_ingreso, fecha_egreso)

    # ================================================================ ganancias / iibb / sicore
    @mcp.tool(annotations=READ_ONLY, tags={"ganancias"})
    def ganancias_anticipos(
        cuit: Cuit,
        impuesto_determinado: Annotated[float, Field(ge=0, description="Impuesto determinado de la última DDJJ")],
        retenciones_percepciones: Annotated[float, Field(ge=0)] = 0.0,
        ejercicio: Annotated[str | None, Field(description="Sociedades: cierre AAAA-MM; humanas: año AAAA")] = None,
    ) -> dict:
        """Plan de anticipos de Ganancias (importes y vencimientos) según RG 4034."""
        return svc.ganancias_anticipos(cuit, impuesto_determinado, retenciones_percepciones, ejercicio)

    @mcp.tool(annotations=READ_ONLY, tags={"iibb"})
    def iibb_convenio_multilateral(
        cuit: Cuit,
        ingresos_por_jurisdiccion: Annotated[dict[str, float], Field(description="Ingresos del último balance por jurisdicción")],
        gastos_por_jurisdiccion: Annotated[dict[str, float], Field(description="Gastos computables por jurisdicción")],
        base_imponible_mes: Annotated[float, Field(ge=0)],
        alicuotas: Annotated[dict[str, float], Field(description="Alícuota % por jurisdicción")],
        deducciones: Annotated[dict[str, float] | None, Field(description="Retenciones/percepciones/saldos por jurisdicción")] = None,
    ) -> dict:
        """Coeficientes unificados (art. 2 CM) e impuesto del anticipo mensual por jurisdicción."""
        return svc.iibb_convenio_multilateral(cuit, ingresos_por_jurisdiccion, gastos_por_jurisdiccion,
                                              base_imponible_mes, alicuotas, deducciones)

    @mcp.tool(annotations=READ_ONLY, tags={"sicore"})
    def sicore_retencion_ganancias(
        concepto: Literal["bienes_muebles", "locaciones_obra_servicios", "alquileres_inmuebles", "honorarios_profesionales"],
        importe_pago: Annotated[float, Field(gt=0, description="Importe del pago neto de IVA")],
        inscripto: bool = True,
        pagos_previos_mes: Annotated[float, Field(ge=0)] = 0.0,
        retenciones_previas_mes: Annotated[float, Field(ge=0)] = 0.0,
        alicuota_override: float | None = None,
        minimo_override: float | None = None,
    ) -> dict:
        """Retención de Ganancias a practicar a un proveedor (esquema RG 830, acumulado mensual)."""
        return svc.sicore_retencion_ganancias(concepto, importe_pago, inscripto, pagos_previos_mes,
                                              retenciones_previas_mes, alicuota_override, minimo_override)

    @mcp.tool(annotations=READ_ONLY, tags={"configuracion"})
    def rates_get() -> dict:
        """Tablas de alícuotas en uso (por defecto o personalizadas). Recordar al usuario verificar su vigencia."""
        return svc.rates

    # ================================================================ knowledge (RAG)
    @mcp.tool(annotations=READ_ONLY, tags={"conocimiento"})
    def knowledge_search(
        query: Annotated[str, Field(description="Pregunta o términos, ej. 'cómputo crédito fiscal facturas B'")],
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
        impuesto: Annotated[str | None, Field(description="iva | ganancias | seguridad_social | iibb | sicore | bienes_personales | general")] = None,
        jurisdiccion: Annotated[str | None, Field(description="Ej. CABA, Buenos Aires (incluye siempre normas nacionales)")] = None,
        vigente_al: Annotated[str | None, Field(description="AAAA-MM-DD: solo normas vigentes a esa fecha")] = None,
        cuit: Annotated[str | None, Field(description="Incluye notas privadas de ese cliente")] = None,
        tipo: Annotated[str | None, Field(description="resumen_normativo | norma | procedimiento | nota_cliente | documento")] = None,
        modo: Literal["hibrido", "lexico", "semantico"] = "hibrido",
    ) -> dict:
        """Búsqueda en la base de conocimiento normativa (RAG híbrido BM25 + embeddings) con filtros y citas."""
        return svc.knowledge_search(query, top_k, impuesto, jurisdiccion, vigente_al, cuit, tipo, modo)

    @mcp.tool(annotations=WRITE, tags={"conocimiento"})
    def knowledge_add_document(
        title: str,
        content: Annotated[str | None, Field(description="Texto/markdown del documento")] = None,
        file_path: Annotated[str | None, Field(description="Ruta local a .md, .txt o .pdf")] = None,
        fuente: Annotated[str | None, Field(description="Ej. 'RG ARCA 5616', 'Ley 27.799 art. 17'")] = None,
        url: str | None = None,
        impuesto: Annotated[str | None, Field(description="Uno o varios separados por coma")] = None,
        jurisdiccion: str | None = None,
        tipo: str = "documento",
        vigente_desde: str | None = None,
        vigente_hasta: str | None = None,
        cuit: Annotated[str | None, Field(description="Si es un documento privado de un cliente")] = None,
    ) -> dict:
        """Agrega una norma, memo interno o documento a la base de conocimiento (deduplica por contenido)."""
        return svc.knowledge_add_document(title, content, file_path, fuente, url, impuesto, jurisdiccion, tipo,
                                          vigente_desde, vigente_hasta, cuit)

    @mcp.tool(annotations=READ_ONLY, tags={"conocimiento"})
    def knowledge_list_documents(cuit: str | None = None) -> list[dict]:
        """Inventario de documentos de la base de conocimiento (generales + los del cliente indicado)."""
        return svc.knowledge_list_documents(cuit)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}, tags={"conocimiento"})
    def knowledge_remove_document(document_id: int) -> dict:
        """Elimina un documento desactualizado de la base de conocimiento."""
        return svc.knowledge_remove_document(document_id)

    @mcp.tool(annotations=READ_ONLY, tags={"conocimiento"})
    def knowledge_status() -> dict:
        """Estado del índice: documentos, chunks y retrievers activos."""
        return svc.knowledge_status()

    # ================================================================ resources
    @mcp.resource("contador://clients", mime_type="application/json")
    def res_clients() -> str:
        """Cartera de clientes."""
        return json.dumps(svc.client_list(), ensure_ascii=False, indent=2)

    @mcp.resource("contador://clients/{cuit}", mime_type="application/json")
    def res_client(cuit: str) -> str:
        """Perfil y obligaciones de un cliente."""
        return json.dumps(svc.client_get(cuit), ensure_ascii=False, indent=2, default=str)

    @mcp.resource("contador://calendar/{month}", mime_type="application/json")
    def res_calendar(month: str) -> str:
        """Vencimientos de toda la cartera en un mes (AAAA-MM)."""
        return json.dumps(svc.deadlines(month), ensure_ascii=False, indent=2)

    @mcp.resource("contador://knowledge/documents", mime_type="application/json")
    def res_kb_docs() -> str:
        """Inventario de la base de conocimiento."""
        return json.dumps(svc.knowledge_list_documents(), ensure_ascii=False, indent=2)

    # ================================================================ prompts
    @mcp.prompt
    def inicio_de_mes(cuit: str, month: str) -> str:
        """Guía el arranque del mes para un cliente."""
        return (
            f"Estamos en el inicio del mes {month} para el cliente {cuit}.\n"
            "1) Llamá a client_get para conocer su perfil y notas.\n"
            f"2) Llamá a month_start_checklist(cuit='{cuit}', month='{month}').\n"
            "3) Redactá un mensaje breve para el cliente pidiendo la documentación faltante.\n"
            "4) Listá los vencimientos de los días 1-15 en una tabla (fecha, obligación, período, estado).\n"
            "5) Si hay dudas normativas, usá knowledge_search y citá las fuentes."
        )

    @mcp.prompt
    def cierre_de_mes(cuit: str, month: str) -> str:
        """Guía el cierre del mes para un cliente."""
        return (
            f"Cerramos el mes {month} del cliente {cuit}.\n"
            f"1) Llamá a month_end_checklist(cuit='{cuit}', month='{month}').\n"
            "2) Si es Responsable Inscripto, revisá iva_position del período anterior y explicá el saldo.\n"
            "3) Señalá vencimientos pendientes o vencidos sin registro y proponé acciones.\n"
            "4) Registrá con mark_filed lo que el usuario confirme como presentado.\n"
            "5) Guardá con client_add_note cualquier hallazgo que convenga recordar el mes próximo."
        )

    @mcp.prompt
    def consulta_normativa(pregunta: str, cuit: str | None = None) -> str:
        """Responde una consulta normativa con RAG y citas."""
        scope = f" para el cliente {cuit} (incluí sus notas con cuit='{cuit}')" if cuit else ""
        return (
            f"Consulta{scope}: {pregunta}\n"
            "Buscá con knowledge_search (probá 2-3 reformulaciones con términos técnicos). Respondé SOLO con lo "
            "que surge de los fragmentos, citando cada 'cita'. Indicá vigencia y qué verificar en la norma oficial. "
            "Si no hay información suficiente, decilo explícitamente."
        )

    @mcp.prompt
    def informe_mensual_cliente(cuit: str, month: str) -> str:
        """Arma el informe mensual para enviar al cliente."""
        return (
            f"Prepará el informe mensual {month} para el cliente {cuit}:\n"
            "- client_get (perfil y notas)\n- deadlines del mes y estado de cada obligación\n"
            "- iva_position del período anterior si es RI\n- upcoming_deadlines de los próximos 30 días\n"
            "Formato: resumen ejecutivo (3 líneas), impuestos del mes (tabla), saldos a favor, alertas, "
            "documentación pendiente y próximos vencimientos. Tono profesional y claro."
        )

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="contador-mcp", description="MCP para estudios contables (Argentina)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=None, help="Directorio de datos (default: $CONTADOR_DATA_DIR o ~/.contador-mcp)")
    args = parser.parse_args(argv)
    server = create_server(load_settings(args.data_dir))
    if args.transport == "http":
        server.run(transport="http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
