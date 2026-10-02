"""Helpers for 'YYYY-MM' periods and Argentine business days."""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_period(value: str) -> tuple[int, int]:
    m = _PERIOD_RE.match(str(value).strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"Período inválido {value!r}; usar formato AAAA-MM (ej. 2026-09)")
    return int(m.group(1)), int(m.group(2))


def fmt_period(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def shift_period(period: str, delta: int) -> str:
    return fmt_period(*add_months(*parse_period(period), delta))


def period_of(d: date) -> str:
    return fmt_period(d.year, d.month)


def month_bounds(period: str) -> tuple[date, date]:
    y, m = parse_period(period)
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


class BusinessCalendar:
    """Weekends + national holidays (feriados nacionales y días no laborables con fines turísticos)."""

    def __init__(self, holidays: set[date]):
        self.holidays = holidays

    @classmethod
    def from_file(cls, path: Path) -> "BusinessCalendar":
        return cls(_load_holidays(str(path)))

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def roll_forward(self, d: date) -> date:
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d

    def known_years(self) -> list[int]:
        return sorted({d.year for d in self.holidays})


@lru_cache(maxsize=8)
def _load_holidays(path: str) -> set[date]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: set[date] = set()
    for year_items in data["feriados"].values():
        for item in year_items:
            out.add(date.fromisoformat(item["fecha"]))
    return out
