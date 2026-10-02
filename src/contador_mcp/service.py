"""Application service: everything the MCP tools do, independent of the transport."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import SEED_KNOWLEDGE_DIR, Settings
from .db import Database
from .domain import checklists, iva as iva_mod, taxes
from .domain.calendar import ComplianceCalendar, Deadline
from .domain.cuit import format_cuit, normalize_cuit
from .domain.periods import BusinessCalendar, month_bounds, parse_period, period_of, shift_period
from .importers.mis_comprobantes import parse_mis_comprobantes
from .rag.knowledge import KnowledgeBase
from .rag.retrievers import Embedder, load_embedder

TIPOS_PERSONA = ("humana", "juridica")
CONDICIONES_IVA = ("responsable_inscripto", "exento", "no_alcanzado")
REGIMENES_IIBB = ("local", "convenio_multilateral", "exento", "no_inscripto")


class NotFound(ValueError):
    pass


class ContadorService:
    def __init__(self, settings: Settings, embedder: Embedder | None = None, today: date | None = None,
                 seed: bool = True):
        self.settings = settings
        self.db = Database(settings.db_path)
        self._today = today
        self.business = BusinessCalendar.from_file(settings.data_file("feriados.json"))
        self.calendar = ComplianceCalendar(self.business, self._override_lookup)
        self.rates = json.loads(settings.data_file("rates.json").read_text(encoding="utf-8"))
        if embedder is None and settings.dense_model:
            embedder, status = load_embedder(settings.dense_model)
        else:
            status = f"activo ({embedder.name})" if embedder else "deshabilitado"
        self.kb = KnowledgeBase(self.db, embedder, status)
        self.seed_result = self.kb.sync_seed(SEED_KNOWLEDGE_DIR) if seed else None

    # ------------------------------------------------------------ helpers
    def today(self) -> date:
        return self._today or date.today()

    def _override_lookup(self, codigo: str, periodo: str, digito: int) -> tuple[date, str] | None:
        row = self.db.one(
            "SELECT vencimiento, fuente FROM calendar_overrides WHERE obligacion=? AND periodo=? AND digito=?",
            [codigo, periodo, digito],
        )
        return (date.fromisoformat(row["vencimiento"]), row["fuente"] or "") if row else None

    def _client(self, cuit: str) -> dict:
        c = self.db.get_client(normalize_cuit(cuit))
        if not c:
            raise NotFound(f"Cliente {cuit} no registrado. Usar client_register primero.")
        return c

    def _with_status(self, deadlines: list[Deadline]) -> list[Deadline]:
        today = self.today()
        for d in deadlines:
            f = self.db.one("SELECT estado, importe, referencia, fecha FROM filings WHERE cuit=? AND obligacion=? AND periodo=?",
                            [d.cuit, d.codigo, d.periodo])
            if f:
                d.estado = f["estado"]
                d.extra = {"registro": f}
            elif d.vencimiento < today:
                d.estado = "VENCIDA_SIN_REGISTRO"
            elif d.vencimiento <= today + timedelta(days=3):
                d.estado = "proxima"
            else:
                d.estado = "pendiente"
        return deadlines

    # ------------------------------------------------------------ clients
    def client_register(self, cuit: str, razon_social: str, tipo_persona: str, condicion_iva: str,
                        iibb_regimen: str, iibb_jurisdicciones: list[str] | None = None, empleador: bool = False,
                        agente_retencion_sicore: bool = False, agente_recaudacion_iibb: bool = False,
                        cierre_ejercicio_mes: int = 12, actividad: str | None = None, contacto: str | None = None) -> dict:
        if tipo_persona not in TIPOS_PERSONA:
            raise ValueError(f"tipo_persona debe ser uno de {TIPOS_PERSONA}")
        if condicion_iva not in CONDICIONES_IVA:
            raise ValueError(f"condicion_iva debe ser uno de {CONDICIONES_IVA}")
        if iibb_regimen not in REGIMENES_IIBB:
            raise ValueError(f"iibb_regimen debe ser uno de {REGIMENES_IIBB}")
        if not 1 <= int(cierre_ejercicio_mes) <= 12:
            raise ValueError("cierre_ejercicio_mes debe estar entre 1 y 12")
        client = self.db.upsert_client({
            "cuit": normalize_cuit(cuit), "razon_social": razon_social.strip(), "tipo_persona": tipo_persona,
            "condicion_iva": condicion_iva, "iibb_regimen": iibb_regimen,
            "iibb_jurisdicciones": iibb_jurisdicciones or [], "empleador": empleador,
            "agente_retencion_sicore": agente_retencion_sicore, "agente_recaudacion_iibb": agente_recaudacion_iibb,
            "cierre_ejercicio_mes": int(cierre_ejercicio_mes), "actividad": actividad, "contacto": contacto,
        })
        return {"cliente": self._present_client(client), "obligaciones": self.obligations_for_client(client["cuit"])}

    def client_update(self, cuit: str, **changes: Any) -> dict:
        current = self._client(cuit)
        payload = {k: v for k, v in current.items() if k not in ("created_at", "updated_at")}
        payload.update({k: v for k, v in changes.items() if v is not None})
        return self.client_register(**payload)

    def _present_client(self, c: dict) -> dict:
        return {**c, "cuit_formateado": format_cuit(c["cuit"]), "terminacion_cuit": int(c["cuit"][-1])}

    def client_list(self) -> list[dict]:
        return [
            {"cuit": c["cuit"], "cuit_formateado": format_cuit(c["cuit"]), "razon_social": c["razon_social"],
             "tipo_persona": c["tipo_persona"], "condicion_iva": c["condicion_iva"], "iibb_regimen": c["iibb_regimen"],
             "empleador": c["empleador"]}
            for c in self.db.list_clients()
        ]

    def client_get(self, cuit: str) -> dict:
        c = self._client(cuit)
        notes = self.db.query("SELECT id, note, tags, created_at FROM client_notes WHERE cuit=? ORDER BY id DESC LIMIT 20",
                              [c["cuit"]])
        for n in notes:
            n["tags"] = json.loads(n["tags"])
        employees = self.db.one("SELECT COUNT(*) AS n FROM employees WHERE cuit=? AND activo=1", [c["cuit"]])["n"]
        return {"cliente": self._present_client(c), "notas_recientes": notes, "empleados_activos": employees,
                "obligaciones": self.obligations_for_client(c["cuit"])}

    def client_add_note(self, cuit: str, note: str, tags: list[str] | None = None) -> dict:
        c = self._client(cuit)
        doc = self.kb.add_document(
            f"Nota de cliente {c['razon_social']} ({self.today().isoformat()})", note,
            tipo="nota_cliente", client_cuit=c["cuit"], fuente="Nota interna del estudio",
            vigente_desde=self.today().isoformat(), impuesto=",".join(tags) if tags else None,
        )
        cur = self.db.execute("INSERT INTO client_notes (cuit, note, tags, kb_document_id) VALUES (?, ?, ?, ?)",
                              [c["cuit"], note, json.dumps(tags or [], ensure_ascii=False), doc["document_id"]])
        return {"nota_id": cur.lastrowid, "indexada_en_kb": True, "document_id": doc["document_id"]}

    # ------------------------------------------------------------ calendar
    def obligations_for_client(self, cuit: str) -> list[dict]:
        c = self._client(cuit)
        return [o.info() for o in self.calendar.applicable(c)]

    def deadlines(self, month: str, cuit: str | None = None) -> dict:
        parse_period(month)
        clients = [self._client(cuit)] if cuit else self.db.list_clients()
        items: list[Deadline] = []
        for c in clients:
            items += self.calendar.deadlines_in_month(c, month)
        items = self._with_status(items)
        items.sort(key=lambda d: (d.vencimiento, d.razon_social, d.codigo))
        return {"mes": month, "cantidad": len(items), "vencimientos": [d.to_dict() for d in items],
                "aviso": self._calendar_notice()}

    def upcoming_deadlines(self, days: int = 7, cuit: str | None = None, desde: str | None = None) -> dict:
        start = date.fromisoformat(desde) if desde else self.today()
        end = start + timedelta(days=days)
        months, p = [], period_of(start)
        while p <= period_of(end):
            months.append(p)
            p = shift_period(p, 1)
        clients = [self._client(cuit)] if cuit else self.db.list_clients()
        items: list[Deadline] = []
        for c in clients:
            for m in months:
                items += [d for d in self.calendar.deadlines_in_month(c, m) if start <= d.vencimiento <= end]
        # Also surface anything overdue (last 31 days) without a filing record.
        for c in clients:
            for m in {period_of(start - timedelta(days=31)), period_of(start)}:
                items += [d for d in self.calendar.deadlines_in_month(c, m)
                          if start - timedelta(days=31) <= d.vencimiento < start]
        items = self._with_status(items)
        upcoming = [d for d in items if d.vencimiento >= start]
        overdue = [d for d in items if d.vencimiento < start and d.estado == "VENCIDA_SIN_REGISTRO"]
        upcoming.sort(key=lambda d: (d.vencimiento, d.razon_social))
        overdue.sort(key=lambda d: d.vencimiento)
        return {"desde": start.isoformat(), "hasta": end.isoformat(),
                "proximos": [d.to_dict() for d in upcoming],
                "vencidos_sin_registro": [d.to_dict() for d in overdue],
                "aviso": self._calendar_notice()}

    def _calendar_notice(self) -> str:
        years = self.business.known_years()
        return (f"Fechas calculadas por regla nominal según terminación de CUIT y corridas al día hábil siguiente "
                f"(feriados cargados: {years[0]}-{years[-1]}). Contrastar con el calendario oficial de ARCA/Comarb "
                f"y fijar diferencias con calendar_set_override.")

    def mark_filed(self, cuit: str, obligacion: str, periodo: str, estado: str = "presentada_y_pagada",
                   importe: float | None = None, referencia: str | None = None, fecha: str | None = None) -> dict:
        c = self._client(cuit)
        if obligacion not in self.calendar.by_code:
            raise ValueError(f"Obligación desconocida {obligacion!r}. Opciones: {sorted(self.calendar.by_code)}")
        self.db.execute(
            "INSERT INTO filings (cuit, obligacion, periodo, estado, importe, referencia, fecha) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cuit, obligacion, periodo) DO UPDATE SET estado=excluded.estado, importe=excluded.importe, "
            "referencia=excluded.referencia, fecha=excluded.fecha",
            [c["cuit"], obligacion, periodo, estado, importe, referencia, fecha or self.today().isoformat()],
        )
        return {"ok": True, "cuit": c["cuit"], "obligacion": obligacion, "periodo": periodo, "estado": estado}

    def calendar_set_override(self, obligacion: str, periodo: str, vencimiento: str, digitos: list[int] | None = None,
                              fuente: str | None = None) -> dict:
        if obligacion not in self.calendar.by_code:
            raise ValueError(f"Obligación desconocida {obligacion!r}. Opciones: {sorted(self.calendar.by_code)}")
        date.fromisoformat(vencimiento)
        digits = digitos if digitos else list(range(10))
        for d in digits:
            if not 0 <= int(d) <= 9:
                raise ValueError("digitos debe contener valores 0..9")
            self.db.execute(
                "INSERT OR REPLACE INTO calendar_overrides (obligacion, periodo, digito, vencimiento, fuente) VALUES (?, ?, ?, ?, ?)",
                [obligacion, periodo, int(d), vencimiento, fuente],
            )
        return {"ok": True, "obligacion": obligacion, "periodo": periodo, "digitos": digits, "vencimiento": vencimiento}

    # ------------------------------------------------------------ workflows
    def month_start_checklist(self, cuit: str, month: str) -> dict:
        c = self._client(cuit)
        return checklists.month_start(c, month, self._with_status(self.calendar.deadlines_in_month(c, month)))

    def month_end_checklist(self, cuit: str, month: str) -> dict:
        c = self._client(cuit)
        result = checklists.month_end(c, month, self._with_status(self.calendar.deadlines_in_month(c, month)))
        if c["condicion_iva"] == "responsable_inscripto":
            prev = shift_period(month, -1)
            pos = self.iva_position(c["cuit"], prev)
            result["preview_iva_periodo_anterior"] = {
                k: pos[k] for k in ("periodo", "debito_fiscal", "credito_fiscal", "saldo_a_pagar", "alertas")
            }
        return result

    def portfolio_overview(self, month: str) -> dict:
        data = self.deadlines(month)["vencimientos"]
        by_client: dict[str, dict] = {}
        for d in data:
            entry = by_client.setdefault(d["cuit"], {"cuit": d["cuit"], "razon_social": d["razon_social"],
                                                       "total": 0, "cumplidas": 0, "vencidas_sin_registro": 0,
                                                       "pendientes": [], })
            entry["total"] += 1
            if d["estado"] in ("presentada", "pagada", "presentada_y_pagada"):
                entry["cumplidas"] += 1
            else:
                if d["estado"] == "VENCIDA_SIN_REGISTRO":
                    entry["vencidas_sin_registro"] += 1
                entry["pendientes"].append(f"{d['vencimiento']} {d['codigo']} ({d['periodo']}) [{d['estado']}]")
        rows = sorted(by_client.values(), key=lambda r: (-r["vencidas_sin_registro"], r["razon_social"]))
        return {"mes": month, "clientes": len(rows),
                "obligaciones_totales": sum(r["total"] for r in rows),
                "cumplidas": sum(r["cumplidas"] for r in rows),
                "vencidas_sin_registro": sum(r["vencidas_sin_registro"] for r in rows),
                "por_cliente": rows}

    # ------------------------------------------------------------ invoices
    def _insert_invoices(self, cuit: str, direccion: str, rows: list[dict], origen: str) -> tuple[int, int]:
        inserted = dup = 0
        for r in rows:
            cur = self.db.conn.execute(
                "INSERT OR IGNORE INTO invoices (cuit, direccion, fecha, periodo, tipo_comprobante, punto_venta, numero, "
                "contraparte_cuit, contraparte_nombre, neto_gravado, neto_no_gravado, exento, iva, percepciones_iva, "
                "otros_tributos, total, moneda, tipo_cambio, origen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [cuit, direccion, r["fecha"], r["fecha"][:7], r["tipo_comprobante"], r.get("punto_venta", 0),
                 r.get("numero", 0), r.get("contraparte_cuit"), r.get("contraparte_nombre"), r.get("neto_gravado", 0),
                 r.get("neto_no_gravado", 0), r.get("exento", 0), r.get("iva", 0), r.get("percepciones_iva", 0),
                 r.get("otros_tributos", 0), r.get("total", 0), r.get("moneda", "PES"), r.get("tipo_cambio", 1), origen],
            )
            if cur.rowcount:
                inserted += 1
            else:
                dup += 1
        self.db.conn.commit()
        return inserted, dup

    def import_mis_comprobantes(self, cuit: str, direccion: str, csv_text: str | None = None,
                                file_path: str | None = None) -> dict:
        c = self._client(cuit)
        if direccion not in ("emitida", "recibida"):
            raise ValueError("direccion debe ser 'emitida' o 'recibida'")
        if not csv_text and not file_path:
            raise ValueError("Indicar csv_text o file_path")
        if file_path:
            raw = Path(file_path).expanduser().read_bytes()
            try:
                csv_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                csv_text = raw.decode("latin-1")
        rows, warnings = parse_mis_comprobantes(csv_text or "")
        inserted, dup = self._insert_invoices(c["cuit"], direccion, rows, "mis_comprobantes")
        periods = sorted({r["fecha"][:7] for r in rows})
        if any(r["otros_tributos"] for r in rows) and direccion == "recibida":
            warnings.append("'Otros tributos' incluye percepciones (IVA/IIBB) sin discriminar: informar las percepciones "
                            "de IVA en iva_position(percepciones_iva_adicionales=...).")
        return {"leidos": len(rows), "insertados": inserted, "duplicados_omitidos": dup, "periodos": periods,
                "advertencias": warnings}

    def add_invoice(self, cuit: str, direccion: str, fecha: str, tipo_comprobante: int, total: float,
                    neto_gravado: float = 0.0, iva: float = 0.0, punto_venta: int = 0, numero: int = 0,
                    contraparte_cuit: str | None = None, contraparte_nombre: str | None = None,
                    neto_no_gravado: float = 0.0, exento: float = 0.0, percepciones_iva: float = 0.0,
                    otros_tributos: float = 0.0, moneda: str = "PES", tipo_cambio: float = 1.0) -> dict:
        c = self._client(cuit)
        if direccion not in ("emitida", "recibida"):
            raise ValueError("direccion debe ser 'emitida' o 'recibida'")
        date.fromisoformat(fecha)
        row = dict(fecha=fecha, tipo_comprobante=int(tipo_comprobante), punto_venta=punto_venta, numero=numero,
                   contraparte_cuit=contraparte_cuit, contraparte_nombre=contraparte_nombre, neto_gravado=neto_gravado,
                   neto_no_gravado=neto_no_gravado, exento=exento, iva=iva, percepciones_iva=percepciones_iva,
                   otros_tributos=otros_tributos, total=total, moneda=moneda, tipo_cambio=tipo_cambio)
        alerts = []
        expected = neto_gravado + neto_no_gravado + exento + iva + percepciones_iva + otros_tributos
        if expected and abs(expected - total) > 1:
            alerts.append(f"El total ({total}) no coincide con la suma de componentes ({round(expected, 2)}).")
        inserted, _ = self._insert_invoices(c["cuit"], direccion, [row], "manual")
        return {"ok": bool(inserted), "duplicado": not inserted, "tipo": iva_mod.tipo_nombre(tipo_comprobante),
                "periodo": fecha[:7], "alertas": alerts}

    def list_invoices(self, cuit: str, periodo: str, direccion: str | None = None, limit: int = 200) -> dict:
        c = self._client(cuit)
        parse_period(periodo)
        sql = "SELECT * FROM invoices WHERE cuit=? AND periodo=?"
        params: list[Any] = [c["cuit"], periodo]
        if direccion:
            sql += " AND direccion=?"
            params.append(direccion)
        rows = self.db.query(sql + " ORDER BY fecha, id", params)
        totals: dict[str, dict[str, float]] = {}
        for r in rows:
            sign = -1 if r["tipo_comprobante"] in iva_mod.NOTAS_CREDITO else 1
            t = totals.setdefault(r["direccion"], {"cantidad": 0, "neto_gravado": 0.0, "iva": 0.0, "total": 0.0})
            t["cantidad"] += 1
            for k in ("neto_gravado", "iva", "total"):
                t[k] = round(t[k] + sign * r[k] * r["tipo_cambio"], 2)
            r["tipo_nombre"] = iva_mod.tipo_nombre(r["tipo_comprobante"])
        return {"periodo": periodo, "totales": totals, "comprobantes": rows[:limit], "truncado": len(rows) > limit}

    def iva_position(self, cuit: str, periodo: str, saldo_tecnico_anterior: float = 0.0,
                     saldo_libre_disponibilidad_anterior: float = 0.0, retenciones_iva_sufridas: float = 0.0,
                     percepciones_iva_adicionales: float = 0.0) -> dict:
        c = self._client(cuit)
        parse_period(periodo)
        invoices = self.db.query("SELECT * FROM invoices WHERE cuit=? AND periodo=?", [c["cuit"], periodo])
        result = iva_mod.iva_position(invoices, saldo_tecnico_anterior, saldo_libre_disponibilidad_anterior,
                                      retenciones_iva_sufridas, percepciones_iva_adicionales)
        due = [d for d in self.calendar.deadlines_in_month(c, shift_period(periodo, 1), ["IVA_F2002"]) if d.periodo == periodo]
        return {"periodo": periodo, "cuit": c["cuit"], "razon_social": c["razon_social"], **result,
                "vencimiento_f2002": due[0].vencimiento.isoformat() if due else None}

    # ------------------------------------------------------------ payroll
    def employee_upsert(self, cuit: str, cuil: str, nombre: str, remuneracion_bruta: float,
                        fecha_ingreso: str | None = None, convenio: str | None = None, activo: bool = True) -> dict:
        c = self._client(cuit)
        cuil_n = normalize_cuit(cuil)
        self.db.execute(
            "INSERT INTO employees (cuit, cuil, nombre, remuneracion_bruta, fecha_ingreso, convenio, activo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(cuit, cuil) DO UPDATE SET nombre=excluded.nombre, "
            "remuneracion_bruta=excluded.remuneracion_bruta, fecha_ingreso=excluded.fecha_ingreso, "
            "convenio=excluded.convenio, activo=excluded.activo",
            [c["cuit"], cuil_n, nombre, remuneracion_bruta, fecha_ingreso, convenio, int(activo)],
        )
        if not c["empleador"]:
            self.db.upsert_client({**{k: v for k, v in c.items() if k not in ("created_at", "updated_at")}, "empleador": True})
        return {"ok": True, "cuil": cuil_n, "nombre": nombre}

    def employee_list(self, cuit: str, solo_activos: bool = True) -> list[dict]:
        c = self._client(cuit)
        sql = "SELECT cuil, nombre, remuneracion_bruta, fecha_ingreso, convenio, activo FROM employees WHERE cuit=?"
        return self.db.query(sql + (" AND activo=1" if solo_activos else "") + " ORDER BY nombre", [c["cuit"]])

    def payroll_contributions(self, cuit: str, periodo: str, categoria_empleador: str = "general",
                              art_porcentaje: float | None = None, art_fijo_por_empleado: float = 0.0,
                              seguro_vida_por_empleado: float = 0.0, tope_base_imponible: float | None = None,
                              detraccion_por_empleado: float | None = None) -> dict:
        c = self._client(cuit)
        parse_period(periodo)
        employees = self.employee_list(c["cuit"])
        if not employees:
            raise ValueError("El cliente no tiene empleados activos cargados (usar employee_upsert).")
        result = taxes.payroll_contributions(employees, self.rates, categoria_empleador, art_porcentaje,
                                             art_fijo_por_empleado, seguro_vida_por_empleado, tope_base_imponible,
                                             detraccion_por_empleado)
        due = [d for d in self.calendar.deadlines_in_month(c, shift_period(periodo, 1), ["SEG_SOCIAL_F931"])
               if d.periodo == periodo]
        return {"periodo": periodo, "cuit": c["cuit"], **result,
                "vencimiento_f931": due[0].vencimiento.isoformat() if due else None}

    # ------------------------------------------------------------ ganancias / iibb / sicore
    def ganancias_anticipos(self, cuit: str, impuesto_determinado: float, retenciones_percepciones: float = 0.0,
                            ejercicio: str | None = None) -> dict:
        c = self._client(cuit)
        result = taxes.ganancias_anticipos(c["tipo_persona"], impuesto_determinado, retenciones_percepciones)
        if c["tipo_persona"] == "juridica":
            close_m = c["cierre_ejercicio_mes"]
            if ejercicio is None:  # last closed fiscal year
                t = self.today()
                y = t.year if t.month > close_m else t.year - 1
                ejercicio = f"{y:04d}-{close_m:02d}"
            for cuota in result["anticipos"]:
                month = shift_period(ejercicio, 5 + cuota["anticipo"])
                due = [d for d in self.calendar.deadlines_in_month(c, month, ["GAN_SOC_ANTICIPO"])
                       if d.periodo == f"{ejercicio}/A{cuota['anticipo']:02d}"]
                cuota["vencimiento"] = due[0].vencimiento.isoformat() if due else None
                cuota["periodo_obligacion"] = f"{ejercicio}/A{cuota['anticipo']:02d}"
            result["ejercicio_base"] = ejercicio
        else:
            year = int(ejercicio) if ejercicio else self.today().year - (1 if self.today().month >= 6 else 2)
            months = [f"{year + 1}-08", f"{year + 1}-10", f"{year + 1}-12", f"{year + 2}-02", f"{year + 2}-04"]
            for cuota, month in zip(result["anticipos"], months):
                due = [d for d in self.calendar.deadlines_in_month(c, month, ["GAN_PH_ANTICIPO"])]
                cuota["vencimiento"] = due[0].vencimiento.isoformat() if due else None
                cuota["periodo_obligacion"] = due[0].periodo if due else None
            result["periodo_fiscal_base"] = str(year)
        return result

    def iibb_convenio_multilateral(self, cuit: str, ingresos_por_jurisdiccion: dict[str, float],
                                   gastos_por_jurisdiccion: dict[str, float], base_imponible_mes: float,
                                   alicuotas: dict[str, float], deducciones: dict[str, float] | None = None) -> dict:
        c = self._client(cuit)
        result = taxes.convenio_multilateral(ingresos_por_jurisdiccion, gastos_por_jurisdiccion, base_imponible_mes,
                                             alicuotas, deducciones)
        if c["iibb_regimen"] != "convenio_multilateral":
            result["advertencias"].insert(0, f"El perfil del cliente indica régimen IIBB '{c['iibb_regimen']}', no Convenio Multilateral.")
        return result

    def sicore_retencion_ganancias(self, concepto: str, importe_pago: float, inscripto: bool = True,
                                   pagos_previos_mes: float = 0.0, retenciones_previas_mes: float = 0.0,
                                   alicuota_override: float | None = None, minimo_override: float | None = None) -> dict:
        return taxes.sicore_retencion_ganancias(self.rates, concepto, importe_pago, inscripto, pagos_previos_mes,
                                                retenciones_previas_mes, alicuota_override, minimo_override)

    def sac_calculation(self, mejor_remuneracion_semestre: float, semestre: int, anio: int,
                        fecha_ingreso: str | None = None, fecha_egreso: str | None = None) -> dict:
        return taxes.sac_calculation(mejor_remuneracion_semestre, semestre, anio, fecha_ingreso, fecha_egreso)

    # ------------------------------------------------------------ knowledge
    def knowledge_search(self, query: str, top_k: int = 5, impuesto: str | None = None,
                         jurisdiccion: str | None = None, vigente_al: str | None = None, cuit: str | None = None,
                         tipo: str | None = None, modo: str = "hibrido") -> dict:
        client_cuit = normalize_cuit(cuit) if cuit else None
        results = self.kb.search(query, max(1, min(top_k, 20)), impuesto, jurisdiccion, vigente_al, client_cuit, tipo, modo)
        return {
            "consulta": query,
            "resultados": results,
            "instrucciones_de_uso": (
                "Responder solo con lo que surge de estos fragmentos y citar la 'cita' de cada uno. Si no alcanzan, "
                "decirlo y recomendar verificar en la norma oficial (Boletín Oficial / ARCA / Comarb)."
            ) if results else "Sin resultados: no responder de memoria; sugerir cargar la norma con knowledge_add_document.",
        }

    def knowledge_add_document(self, title: str, content: str | None = None, file_path: str | None = None,
                               fuente: str | None = None, url: str | None = None, impuesto: str | None = None,
                               jurisdiccion: str | None = None, tipo: str = "documento",
                               vigente_desde: str | None = None, vigente_hasta: str | None = None,
                               cuit: str | None = None) -> dict:
        for d in (vigente_desde, vigente_hasta):
            if d:
                date.fromisoformat(d)
        meta = dict(fuente=fuente, url=url, impuesto=impuesto, jurisdiccion=jurisdiccion, tipo=tipo,
                    vigente_desde=vigente_desde, vigente_hasta=vigente_hasta,
                    client_cuit=normalize_cuit(cuit) if cuit else None)
        if file_path:
            return self.kb.add_file(file_path, title=title or None, **meta)
        if not content:
            raise ValueError("Indicar content o file_path")
        return self.kb.add_document(title, content, **meta)

    def knowledge_list_documents(self, cuit: str | None = None) -> list[dict]:
        return self.kb.list_documents(normalize_cuit(cuit) if cuit else None)

    def knowledge_remove_document(self, document_id: int) -> dict:
        return {"eliminado": self.kb.remove_document(document_id), "document_id": document_id}

    def knowledge_status(self) -> dict:
        return {**self.kb.status(), "seed": self.seed_result, "data_dir": str(self.settings.data_dir)}
