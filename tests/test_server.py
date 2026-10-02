import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from conftest import ACME, TODAY

from contador_mcp.server import create_server
from contador_mcp.service import ContadorService

EXPECTED_TOOLS = {
    "client_register", "client_update", "client_list", "client_get", "client_add_note",
    "obligations_for_client", "deadlines", "upcoming_deadlines", "mark_filed", "calendar_set_override",
    "month_start_checklist", "month_end_checklist", "portfolio_overview",
    "import_mis_comprobantes", "add_invoice", "list_invoices", "iva_position",
    "employee_upsert", "employee_list", "payroll_contributions", "sac_calculation",
    "ganancias_anticipos", "iibb_convenio_multilateral", "sicore_retencion_ganancias", "rates_get",
    "knowledge_search", "knowledge_add_document", "knowledge_list_documents", "knowledge_remove_document",
    "knowledge_status",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mcp(settings):
    return create_server(service=ContadorService(settings, today=TODAY))


def test_end_to_end_over_mcp(mcp):
    async def scenario():
        async with Client(mcp) as c:
            tools = {t.name for t in await c.list_tools()}
            assert EXPECTED_TOOLS <= tools

            r = await c.call_tool("client_register", {
                "cuit": ACME, "razon_social": "ACME SA", "tipo_persona": "juridica",
                "condicion_iva": "responsable_inscripto", "iibb_regimen": "convenio_multilateral",
                "empleador": True, "agente_retencion_sicore": True})
            assert r.data["cliente"]["terminacion_cuit"] == 1

            r = await c.call_tool("month_start_checklist", {"cuit": ACME, "month": "2026-10"})
            assert r.data["fase"] == "inicio_de_mes"

            r = await c.call_tool("knowledge_search", {"query": "saldo de libre disponibilidad IVA"})
            assert r.data["resultados"][0]["cita"]

            with pytest.raises(ToolError, match="Dígito verificador"):
                await c.call_tool("client_get", {"cuit": "30-71234567-9"})

            res = await c.read_resource("contador://calendar/2026-10")
            data = json.loads(res[0].text)
            assert data["mes"] == "2026-10" and data["cantidad"] > 0

            prompts = {p.name for p in await c.list_prompts()}
            assert {"inicio_de_mes", "cierre_de_mes", "consulta_normativa", "informe_mensual_cliente"} <= prompts
            p = await c.get_prompt("cierre_de_mes", {"cuit": ACME, "month": "2026-10"})
            assert "month_end_checklist" in p.messages[0].content.text

    run(scenario())


def test_input_validation_rejects_bad_month(mcp):
    async def scenario():
        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool("deadlines", {"month": "octubre"})

    run(scenario())
