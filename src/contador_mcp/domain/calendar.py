"""Deterministic compliance calendar.

Each obligation knows (a) whether it applies to a client profile and (b) its *nominal* due date
for a given CUIT terminación. Nominal dates are rolled forward to the next business day and can
be pinned to the official ARCA / Comisión Arbitral date through overrides.

All published day-patterns here are nominal and must be checked against the official annual
calendar (ARCA publishes it by RG every year). That is why overrides exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Iterable

from .periods import BusinessCalendar, add_months, fmt_period, parse_period, safe_date


def _by_digit(groups: dict[tuple[int, ...], int]) -> dict[int, int]:
    return {d: day for digits, day in groups.items() for d in digits}


IVA_DAYS = _by_digit({(0, 1): 18, (2, 3): 19, (4, 5): 20, (6, 7): 21, (8, 9): 22})
F931_DAYS = _by_digit({(0, 1, 2, 3): 9, (4, 5, 6): 10, (7, 8, 9): 11})
AUTONOMOS_DAYS = _by_digit({(0, 1, 2, 3): 5, (4, 5, 6): 6, (7, 8, 9): 7})
SICORE_Q1_DAYS = _by_digit({(0, 1, 2, 3): 21, (4, 5, 6): 22, (7, 8, 9): 23})
SICORE_DDJJ_DAYS = _by_digit({(0, 1, 2, 3): 9, (4, 5, 6): 10, (7, 8, 9): 11})
CM03_DAYS = _by_digit({(0, 1, 2): 15, (3, 4, 5): 16, (6, 7): 17, (8, 9): 18})
GANANCIAS_DAYS = _by_digit({(0, 1, 2, 3): 13, (4, 5, 6): 14, (7, 8, 9): 15})
PH_ANNUAL_DAYS = _by_digit({(0, 1, 2, 3): 11, (4, 5, 6): 12, (7, 8, 9): 13})

PH_ANTICIPO_MONTHS = (8, 10, 12, 2, 4)  # anticipos personas humanas: ago, oct, dic, feb, abr


@dataclass
class Deadline:
    codigo: str
    nombre: str
    impuesto: str
    formulario: str
    periodo: str
    vencimiento: date
    vencimiento_nominal: date
    origen_fecha: str  # 'regla_nominal' | 'override_oficial' | 'fecha_fija'
    nota: str = ""
    cuit: str = ""
    razon_social: str = ""
    estado: str = "pendiente"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vencimiento"] = self.vencimiento.isoformat()
        d["vencimiento_nominal"] = self.vencimiento_nominal.isoformat()
        if not d["extra"]:
            d.pop("extra")
        return d


# (periodo, nominal_date, nota, fixed_date?)
Candidate = tuple[str, date, str, bool]


@dataclass(frozen=True)
class Obligation:
    codigo: str
    nombre: str
    impuesto: str
    formulario: str
    descripcion: str
    consulta_kb: str
    applies: Callable[[dict], bool]
    candidates: Callable[[dict, int, int, int], list[Candidate]]  # (client, digit, year, month)

    def info(self) -> dict:
        return {
            "codigo": self.codigo,
            "nombre": self.nombre,
            "impuesto": self.impuesto,
            "formulario": self.formulario,
            "descripcion": self.descripcion,
        }


def _monthly(days: dict[int, int], lag: int, nota: str = "") -> Callable[[dict, int, int, int], list[Candidate]]:
    """Obligation for período P due in month P+lag on days[digit]."""

    def gen(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
        py, pm = add_months(year, month, -lag)
        return [(fmt_period(py, pm), safe_date(year, month, days[digit]), nota, False)]

    return gen


def _fixed(month_day: dict[int, int], nota: str) -> Callable[[dict, int, int, int], list[Candidate]]:
    def gen(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
        if month in month_day:
            return [(fmt_period(year, month), date(year, month, month_day[month]), nota, True)]
        return []

    return gen


def _ganancias_sociedades(kind: str) -> Callable[[dict, int, int, int], list[Candidate]]:
    """DDJJ: 5th month after year-end. Anticipos 1..10: months 6..15 after year-end (RG 4034)."""

    def gen(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
        close_m = int(client.get("cierre_ejercicio_mes") or 12)
        out: list[Candidate] = []
        for close_y in (year - 2, year - 1, year):
            diff = (year * 12 + month) - (close_y * 12 + close_m)
            ejercicio = fmt_period(close_y, close_m)
            due = safe_date(year, month, GANANCIAS_DAYS[digit])
            if kind == "ddjj" and diff == 5:
                out.append((ejercicio, due, f"Ejercicio cerrado en {ejercicio}", False))
            if kind == "anticipo" and 6 <= diff <= 15:
                n = diff - 5
                pct = "25%" if n == 1 else "8,33%"
                out.append(
                    (f"{ejercicio}/A{n:02d}", due,
                     f"Anticipo {n}/10 ({pct} del impuesto determinado del ejercicio {ejercicio}, neto de retenciones/percepciones)",
                     False)
                )
        return out

    return gen


def _ph_anticipos(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
    if month not in PH_ANTICIPO_MONTHS:
        return []
    n = PH_ANTICIPO_MONTHS.index(month) + 1
    fiscal_year = year - 1 if month >= 8 else year - 2
    return [(f"{fiscal_year}/A{n}", safe_date(year, month, GANANCIAS_DAYS[digit]),
             f"Anticipo {n}/5 (20%) a cuenta del período {fiscal_year + 1}, base DDJJ {fiscal_year}", False)]


def _ph_annual(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
    if month != 6:
        return []
    return [(str(year - 1), safe_date(year, 6, PH_ANNUAL_DAYS[digit]), f"Período fiscal {year - 1}", False)]


def _pago_sueldos(cal: BusinessCalendar) -> Callable[[dict, int, int, int], list[Candidate]]:
    def gen(client: dict, digit: int, year: int, month: int) -> list[Candidate]:
        d, count = date(year, month, 1), 0
        while True:
            if cal.is_business_day(d):
                count += 1
                if count == 4:
                    break
            d = date.fromordinal(d.toordinal() + 1)
        py, pm = add_months(year, month, -1)
        return [(fmt_period(py, pm), d, "LCT art. 128: hasta el 4° día hábil del mes siguiente (remuneración mensual)", True)]

    return gen


def _is_ri(c: dict) -> bool:
    return c.get("condicion_iva") == "responsable_inscripto"


def build_obligations(cal: BusinessCalendar) -> list[Obligation]:
    return [
        Obligation(
            "IVA_F2002", "IVA - DDJJ mensual y pago", "iva", "F.2002 + Libro IVA Digital",
            "Declaración jurada mensual de IVA (débito − crédito fiscal). Antes se presenta el Libro IVA Digital del período.",
            "liquidación mensual IVA débito crédito fiscal saldo técnico libre disponibilidad",
            _is_ri, _monthly(IVA_DAYS, 1),
        ),
        Obligation(
            "SEG_SOCIAL_F931", "Seguridad social - F.931 (SICOSS) y pago", "seguridad_social", "F.931 / Libro de Sueldos Digital",
            "Aportes y contribuciones de los empleados del período (incluye obra social, ART, seguro de vida).",
            "F.931 aportes contribuciones seguridad social empleador vencimiento",
            lambda c: bool(c.get("empleador")), _monthly(F931_DAYS, 1),
        ),
        Obligation(
            "PAGO_SUELDOS", "Pago de remuneraciones", "seguridad_social", "Recibos de sueldo",
            "Fecha límite de pago de sueldos mensuales al personal.",
            "plazo pago remuneraciones LCT 128",
            lambda c: bool(c.get("empleador")), _pago_sueldos(cal),
        ),
        Obligation(
            "SAC", "Aguinaldo (SAC)", "seguridad_social", "Recibo SAC",
            "Sueldo anual complementario: 1ª cuota 30/06, 2ª cuota 18/12 (Ley 23.041). Plazo de gracia de 4 días hábiles.",
            "sueldo anual complementario aguinaldo cálculo",
            lambda c: bool(c.get("empleador")), _fixed({6: 30, 12: 18}, "Ley 23.041 / Dto. 1078/84"),
        ),
        Obligation(
            "AUTONOMOS", "Autónomos - aporte mensual", "seguridad_social", "VEP autónomos",
            "Aporte previsional mensual del titular persona humana inscripta en autónomos (vence en el mismo mes).",
            "autónomos aportes categorías vencimiento",
            lambda c: c.get("tipo_persona") == "humana" and _is_ri(c), _monthly(AUTONOMOS_DAYS, 0),
        ),
        Obligation(
            "SICORE_Q1", "SICORE - pago a cuenta 1ª quincena", "sicore", "VEP SICORE",
            "Ingreso de retenciones/percepciones practicadas en la 1ª quincena del mes (Ganancias e IVA).",
            "SICORE retenciones percepciones primera quincena ingreso",
            lambda c: bool(c.get("agente_retencion_sicore")), _monthly(SICORE_Q1_DAYS, 0),
        ),
        Obligation(
            "SICORE_DDJJ", "SICORE - DDJJ mensual y saldo", "sicore", "F.744 SICORE",
            "Declaración jurada mensual de retenciones/percepciones y pago del saldo (2ª quincena).",
            "SICORE declaración jurada mensual retenciones",
            lambda c: bool(c.get("agente_retencion_sicore")), _monthly(SICORE_DDJJ_DAYS, 1),
        ),
        Obligation(
            "IIBB_CM03", "IIBB Convenio Multilateral - CM03 (SIFERE)", "iibb", "CM03 / CM04",
            "Anticipo mensual de Ingresos Brutos distribuido entre jurisdicciones por coeficiente unificado.",
            "convenio multilateral coeficiente unificado anticipo mensual SIFERE",
            lambda c: c.get("iibb_regimen") == "convenio_multilateral", _monthly(CM03_DAYS, 1),
        ),
        Obligation(
            "IIBB_CM05", "IIBB Convenio Multilateral - CM05 anual", "iibb", "CM05",
            "Declaración jurada anual: determina coeficientes del año siguiente.",
            "CM05 declaración jurada anual convenio multilateral coeficientes",
            lambda c: c.get("iibb_regimen") == "convenio_multilateral", _fixed({6: 30}, "Comarb: CM05 vence el 30/06"),
        ),
        Obligation(
            "IIBB_LOCAL", "IIBB contribuyente local - anticipo mensual", "iibb", "DDJJ jurisdicción (ARBA/AGIP/etc.)",
            "Anticipo mensual de Ingresos Brutos en la jurisdicción local. Cada fisco fija su calendario: verificar.",
            "ingresos brutos contribuyente local anticipo mensual",
            lambda c: c.get("iibb_regimen") == "local",
            _monthly(CM03_DAYS, 1, "Fecha nominal genérica: cada jurisdicción publica su calendario (cargar override)."),
        ),
        Obligation(
            "GAN_SOC_DDJJ", "Ganancias sociedades - DDJJ anual", "ganancias", "F.713 + Estados Contables",
            "DDJJ anual de Ganancias de sociedades: vence el 5° mes posterior al cierre de ejercicio.",
            "ganancias sociedades declaración jurada anual alícuotas",
            lambda c: c.get("tipo_persona") == "juridica", _ganancias_sociedades("ddjj"),
        ),
        Obligation(
            "GAN_SOC_ANTICIPO", "Ganancias sociedades - anticipo", "ganancias", "VEP anticipo",
            "10 anticipos: el 1° del 25% y 9 del 8,33% (RG 4034), desde el mes siguiente al vencimiento de la DDJJ.",
            "anticipos ganancias sociedades RG 4034",
            lambda c: c.get("tipo_persona") == "juridica", _ganancias_sociedades("anticipo"),
        ),
        Obligation(
            "GAN_PH_DDJJ", "Ganancias personas humanas - DDJJ anual", "ganancias", "F.715 / Ganancias simplificada",
            "DDJJ anual de personas humanas (junio). Evaluar régimen simplificado Ley 27.799.",
            "ganancias personas humanas declaración jurada anual inocencia fiscal simplificado",
            lambda c: c.get("tipo_persona") == "humana", _ph_annual,
        ),
        Obligation(
            "BP_PH_DDJJ", "Bienes Personales - DDJJ anual", "bienes_personales", "F.762",
            "DDJJ anual de Bienes Personales (si supera el mínimo no imponible).",
            "bienes personales mínimo no imponible alícuotas",
            lambda c: c.get("tipo_persona") == "humana", _ph_annual,
        ),
        Obligation(
            "GAN_PH_ANTICIPO", "Ganancias personas humanas - anticipo", "ganancias", "VEP anticipo",
            "5 anticipos del 20% (ago, oct, dic, feb, abr) según RG 4034.",
            "anticipos ganancias personas humanas",
            lambda c: c.get("tipo_persona") == "humana", _ph_anticipos,
        ),
    ]


class ComplianceCalendar:
    def __init__(self, cal: BusinessCalendar, overrides: Callable[[str, str, int], tuple[date, str] | None]):
        self.cal = cal
        self.obligations = build_obligations(cal)
        self.by_code = {o.codigo: o for o in self.obligations}
        self._overrides = overrides

    def applicable(self, client: dict) -> list[Obligation]:
        return [o for o in self.obligations if o.applies(client)]

    def deadlines_in_month(self, client: dict, month: str, codes: Iterable[str] | None = None) -> list[Deadline]:
        """All deadlines whose *final* date (after business-day roll / override) falls in `month`."""
        year, mon = parse_period(month)
        digit = int(client["cuit"][-1])
        wanted = set(codes) if codes else None
        out: list[Deadline] = []
        seen: set[tuple[str, str]] = set()
        for ob in self.applicable(client):
            if wanted and ob.codigo not in wanted:
                continue
            # Look at neighbouring nominal months: rolling may push a date across a month boundary.
            for delta in (-1, 0):
                gy, gm = add_months(year, mon, delta)
                for periodo, nominal, nota, fixed in ob.candidates(client, digit, gy, gm):
                    if (ob.codigo, periodo) in seen:
                        continue
                    override = self._overrides(ob.codigo, periodo, digit)
                    if override:
                        final, origen = override[0], "override_oficial"
                        if override[1]:
                            nota = (nota + " | " if nota else "") + f"Fuente: {override[1]}"
                    else:
                        final = self.cal.roll_forward(nominal)
                        origen = "fecha_fija" if fixed else "regla_nominal"
                    if (final.year, final.month) != (year, mon):
                        continue
                    seen.add((ob.codigo, periodo))
                    out.append(Deadline(
                        codigo=ob.codigo, nombre=ob.nombre, impuesto=ob.impuesto, formulario=ob.formulario,
                        periodo=periodo, vencimiento=final, vencimiento_nominal=nominal, origen_fecha=origen,
                        nota=nota, cuit=client["cuit"], razon_social=client.get("razon_social", ""),
                    ))
        out.sort(key=lambda d: (d.vencimiento, d.codigo))
        return out
