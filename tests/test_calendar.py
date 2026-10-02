from datetime import date

from conftest import ACME, JUAN


def _by(code, items, periodo=None):
    return [d for d in items if d["codigo"] == code and (periodo is None or d["periodo"] == periodo)]


def test_iva_and_f931_roll_over_weekends_and_holidays(acme):
    v = acme.deadlines("2026-10", ACME)["vencimientos"]  # CUIT terminación 1
    iva = _by("IVA_F2002", v)[0]
    assert iva["periodo"] == "2026-09"
    assert iva["vencimiento_nominal"] == "2026-10-18"  # domingo
    assert iva["vencimiento"] == "2026-10-19"
    assert _by("SEG_SOCIAL_F931", v)[0]["vencimiento"] == "2026-10-09"
    assert _by("SICORE_Q1", v, "2026-10")[0]["vencimiento"] == "2026-10-21"
    assert _by("IIBB_CM03", v)[0]["vencimiento"] == "2026-10-15"


def test_holiday_roll(svc):
    # Terminación 9: F.931 nominal 11/10/2026 (domingo) -> lunes 12/10 feriado -> martes 13/10.
    svc.client_register("20-20000004-9", "Pepe", "humana", "responsable_inscripto", "local", empleador=True)
    v = svc.deadlines("2026-10", "20200000049")["vencimientos"]
    f931 = _by("SEG_SOCIAL_F931", v)[0]
    assert (f931["vencimiento_nominal"], f931["vencimiento"]) == ("2026-10-11", "2026-10-13")


def test_ganancias_sociedades_cycle(acme):
    may = acme.deadlines("2026-05", ACME)["vencimientos"]
    assert _by("GAN_SOC_DDJJ", may)[0]["periodo"] == "2025-12"
    oct_ = acme.deadlines("2026-10", ACME)["vencimientos"]
    assert _by("GAN_SOC_ANTICIPO", oct_)[0]["periodo"] == "2025-12/A05"
    mar27 = acme.deadlines("2027-03", ACME)["vencimientos"]
    assert _by("GAN_SOC_ANTICIPO", mar27)[0]["periodo"] == "2025-12/A10"
    assert not _by("GAN_SOC_ANTICIPO", acme.deadlines("2027-04", ACME)["vencimientos"])


def test_persona_humana_obligations(svc):
    svc.client_register(JUAN, "Juan Pérez", "humana", "responsable_inscripto", "local")
    codes = {o["codigo"] for o in svc.obligations_for_client(JUAN)}
    assert {"IVA_F2002", "AUTONOMOS", "GAN_PH_DDJJ", "BP_PH_DDJJ", "IIBB_LOCAL"} <= codes
    assert "SEG_SOCIAL_F931" not in codes and "GAN_SOC_DDJJ" not in codes
    jun = svc.deadlines("2026-06", JUAN)["vencimientos"]
    assert _by("GAN_PH_DDJJ", jun)[0]["periodo"] == "2025"
    assert _by("GAN_PH_ANTICIPO", svc.deadlines("2026-08", JUAN)["vencimientos"])[0]["periodo"] == "2025/A1"


def test_status_override_and_mark_filed(acme):
    acme.calendar_set_override("IVA_F2002", "2026-09", "2026-10-20", [1], "Calendario oficial (test)")
    iva = _by("IVA_F2002", acme.deadlines("2026-10", ACME)["vencimientos"])[0]
    assert iva["vencimiento"] == "2026-10-20" and iva["origen_fecha"] == "override_oficial"
    # 2026-10-02: F.931 de 2026-09 vence 09/10 -> pendiente; IVA 2026-08 (septiembre) vencido sin registro
    up = acme.upcoming_deadlines(days=10)
    assert any(d["codigo"] == "SEG_SOCIAL_F931" for d in up["proximos"])
    assert any(d["codigo"] == "IVA_F2002" and d["periodo"] == "2026-08" for d in up["vencidos_sin_registro"])
    acme.mark_filed(ACME, "IVA_F2002", "2026-08", importe=1234.5, referencia="VEP 99")
    up = acme.upcoming_deadlines(days=10)
    assert not any(d["codigo"] == "IVA_F2002" and d["periodo"] == "2026-08" for d in up["vencidos_sin_registro"])


def test_checklists_and_portfolio(acme):
    start = acme.month_start_checklist(ACME, "2026-10")
    assert start["periodo_que_se_cierra"] == "2026-09"
    assert any("sueldos" in t["tarea"].lower() for t in start["tareas"])
    assert all(date.fromisoformat(d["fecha_objetivo"]).day <= 15 for d in start["vencimientos_dias_1_a_15"])
    end = acme.month_end_checklist(ACME, "2026-12")
    assert any("CIERRE DE EJERCICIO" in t["tarea"] for t in end["tareas"])
    assert any("SAC" in t["tarea"] for t in end["tareas"])
    assert "preview_iva_periodo_anterior" in end
    ov = acme.portfolio_overview("2026-10")
    assert ov["clientes"] == 1 and ov["obligaciones_totales"] >= 5


def test_pago_sueldos_fourth_business_day(acme):
    v = acme.deadlines("2026-10", ACME)["vencimientos"]
    assert _by("PAGO_SUELDOS", v)[0]["vencimiento"] == "2026-10-06"  # 1,2,5,6 de octubre
