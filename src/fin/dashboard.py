"""Charts-only dashboard in Google Sheets with two dropdowns: view (Semanal | Mensal | Anual) and titular.

The script precomputes every view x titular into the hidden tab `_dash` (RAW values, rewritten on each
publish). A few formulas in `_dash` pick the block that matches the dropdowns, and the charts on
`Dashboard` read those formula ranges, so changing a dropdown redraws the charts instantly.
All numbers are by transaction date (what happened in the week/month/year), not by card competence.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from fin.ledger import Ledger
from fin.money import from_cents
from fin.reports import _owner_where, date_range_totals

DASH, DATA = "Dashboard", "_dash"
VIEWS = ["Semanal", "Mensal", "Anual"]
OWNERS = [("PF", "pf"), ("PJ", "pj"), ("Consolidado", None)]
BLOCK_H = 40                 # rows per (view, titular) block
N_PERIODS, N_CATS, N_TOP = 13, 12, 10
R_CATS, R_TOP = 15, 29       # header rows of the category and top-merchant tables inside a block
OPTIONS_ROW = len(VIEWS) * len(OWNERS) * BLOCK_H + 1   # 0-based row of the dropdown option lists


def _periods(view: str, today: date, first_year: int) -> tuple[list[tuple[str, str, str]], int]:
    """[(label, start_iso, end_iso)] and the index of the current period."""
    if view == "Semanal":
        last = calendar.monthrange(today.year, today.month)[1]
        out, cur = [], 0
        for i, d0 in enumerate(range(1, last + 1, 7)):
            d1 = min(d0 + 6, last)
            out.append((f"{d0:02d}–{d1:02d}/{today.month:02d}", date(today.year, today.month, d0).isoformat(),
                        date(today.year, today.month, d1).isoformat()))
            if d0 <= today.day <= d1:
                cur = i
        return out, cur
    if view == "Mensal":  # January of the current year to the current month
        out = []
        for mm in range(1, today.month + 1):
            last = calendar.monthrange(today.year, mm)[1]
            out.append((f"{today.year:04d}-{mm:02d}", date(today.year, mm, 1).isoformat(),
                        date(today.year, mm, last).isoformat()))
        return out, len(out) - 1
    years = list(range(min(first_year, today.year), today.year + 1))[-N_PERIODS:]
    return [(str(y), f"{y}-01-01", f"{y}-12-31") for y in years], len(years) - 1


def _top_merchants(ledger: Ledger, start: str, end: str, owner: str | None, apelidos: dict[str, str]) -> list[tuple[str, int]]:
    from fin.sheets import _public_desc

    agg: dict[str, int] = {}
    for r in ledger.active_txs(*_owner_where("date >= ? AND date <= ? AND natureza='Despesa'", (start, end), owner)):
        key = _public_desc(r, apelidos) or "(sem descrição)"
        agg[key] = agg.get(key, 0) - r["amount_cents"]
    return sorted(((k, v) for k, v in agg.items() if v > 0), key=lambda kv: -kv[1])[:N_TOP]


def build_dash_data(ledger: Ledger, today: date) -> list[list[Any]]:
    apelidos = {r["alias"]: r["apelido"] for r in ledger.q("SELECT alias, apelido FROM counterparts WHERE apelido IS NOT NULL")}
    first = ledger.one("SELECT MIN(date) AS d FROM transactions WHERE removed_upstream=0")
    first_year = int(first["d"][:4]) if first and first["d"] else today.year
    rows: list[list[Any]] = [[""] * 5 for _ in range(OPTIONS_ROW + len(VIEWS))]
    for vi, view in enumerate(VIEWS):
        periods, cur = _periods(view, today, first_year)
        for oi, (olabel, owner) in enumerate(OWNERS):
            base = (vi * len(OWNERS) + oi) * BLOCK_H
            rows[base][:4] = ["periodo", "receitas", "despesas", "saldo"]
            for k, (label, start, end) in enumerate(periods[:N_PERIODS]):
                t = date_range_totals(ledger, start, end, owner)
                saldo = t["receitas"] - t["despesas"] + t["titular"]
                rows[base + 1 + k][:4] = [label, from_cents(t["receitas"]), from_cents(t["despesas"]), from_cents(saldo)]
            label, start, end = periods[cur]
            rows[base + R_CATS - 1][0] = f"{view} · {olabel} · período atual: {label}"
            t = date_range_totals(ledger, start, end, owner)
            cats = sorted(((c, v) for c, v in t["categorias"].items() if v > 0), key=lambda kv: -kv[1])
            if len(cats) > N_CATS:
                cats = cats[:N_CATS - 1] + [("Demais", sum(v for _, v in cats[N_CATS - 1:]))]
            rows[base + R_CATS][:2] = ["categoria", "valor"]
            for k, (c, v) in enumerate(cats):
                rows[base + R_CATS + 1 + k][:2] = [c, from_cents(v)]
            rows[base + R_TOP][:2] = ["estabelecimento", "valor"]
            for k, (name, v) in enumerate(_top_merchants(ledger, start, end, owner, apelidos)):
                rows[base + R_TOP + 1 + k][:2] = [name, from_cents(v)]
    for i, v in enumerate(VIEWS):
        rows[OPTIONS_ROW + i][0] = v
    for i, (olabel, _o) in enumerate(OWNERS):
        rows[OPTIONS_ROW + i][1] = olabel
    rows[0][4] = f"atualizado {today:%d/%m/%Y}"
    return rows


# ---- formulas, validation and charts (created once) ---------------------------------------------
def _a1(row0: int, col0: int) -> str:
    return f"{chr(65 + col0)}{row0 + 1}"


def formula_cells() -> dict[str, str]:
    """Cells of `_dash` (outside A:E) holding the selector formulas. Argument separator ";": the sheet is pt-BR
    and USER_ENTERED formulas are parsed with the spreadsheet locale."""
    opt_v = f"$A${OPTIONS_ROW + 1}:$A${OPTIONS_ROW + len(VIEWS)}"
    opt_o = f"$B${OPTIONS_ROW + 1}:$B${OPTIONS_ROW + len(OWNERS)}"
    base = (f"(MATCH({DASH}!$C$3;{opt_v};0)-1)*{len(OWNERS) * BLOCK_H}"
            f"+(MATCH({DASH}!$F$3;{opt_o};0)-1)*{BLOCK_H}")

    def pick(off: int, height: int, width: int) -> str:
        rng = f"OFFSET($A$1;$G$1+{off};0;{height};{width})"
        first = f"OFFSET($A$1;$G$1+{off};0;{height};1)"
        return f'=IFERROR(FILTER({rng};{first}<>"");"")'

    return {
        "G1": f"=IFERROR({base};0)",
        "H1": pick(0, N_PERIODS + 1, 4),
        "H16": pick(R_CATS, N_CATS + 1, 2),
        "H30": pick(R_TOP, N_TOP + 1, 2),
        "H15": f'=OFFSET($A$1;$G$1+{R_CATS - 1};0)',
    }


def _gr(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict[str, int]:
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}


def setup_requests(dash_id: int, data_id: int) -> list[dict[str, Any]]:
    def dropdown(row: int, col: int, values: list[str]) -> dict[str, Any]:
        return {"setDataValidation": {"range": _gr(dash_id, row, row + 1, col, col + 1), "rule": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
            "showCustomUi": True, "strict": True}}}

    def pos(row: int, col: int, w: int = 640, h: int = 340) -> dict[str, Any]:
        return {"overlayPosition": {"anchorCell": {"sheetId": dash_id, "rowIndex": row, "columnIndex": col},
                                    "widthPixels": w, "heightPixels": h}}

    per = (0, N_PERIODS + 1)
    col = {"title": "Receitas x Despesas", "basicChart": {
        "chartType": "COLUMN", "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(data_id, *per, 7, 8)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(data_id, *per, c, c + 1)]}}, "targetAxis": "LEFT_AXIS"}
                   for c in (8, 9)]}}
    saldo = {"title": "Saldo (receitas − despesas)", "basicChart": {
        "chartType": "COLUMN", "legendPosition": "NO_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(data_id, *per, 7, 8)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(data_id, *per, 10, 11)]}}, "targetAxis": "LEFT_AXIS"}]}}
    pie = {"title": "Gastos por categoria (período atual)", "pieChart": {
        "legendPosition": "RIGHT_LEGEND", "pieHole": 0.4,
        "domain": {"sourceRange": {"sources": [_gr(data_id, 16, 16 + N_CATS, 7, 8)]}},
        "series": {"sourceRange": {"sources": [_gr(data_id, 16, 16 + N_CATS, 8, 9)]}}}}
    top = {"title": "Maiores gastos (período atual)", "basicChart": {
        "chartType": "BAR", "legendPosition": "NO_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(data_id, 29, 30 + N_TOP, 7, 8)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(data_id, 29, 30 + N_TOP, 8, 9)]}}, "targetAxis": "BOTTOM_AXIS"}]}}
    charts = [(col, 5, 0), (saldo, 5, 8), (pie, 24, 0), (top, 24, 8)]
    reqs: list[dict[str, Any]] = [
        dropdown(2, 2, VIEWS), dropdown(2, 5, [o for o, _ in OWNERS]),
        {"updateSheetProperties": {"properties": {"sheetId": dash_id, "gridProperties": {"hideGridlines": True}},
                                   "fields": "gridProperties.hideGridlines"}},
        {"updateSheetProperties": {"properties": {"sheetId": data_id, "hidden": True}, "fields": "hidden"}},
        {"repeatCell": {"range": _gr(dash_id, 0, 1, 0, 1), "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 16}}},
                        "fields": "userEnteredFormat.textFormat"}},
    ]
    reqs += [{"addChart": {"chart": {"spec": sp, "position": pos(r, c)}}} for sp, r, c in charts]
    return reqs


def dashboard_cells() -> list[dict[str, Any]]:
    """Static cells of the Dashboard tab (USER_ENTERED): title, the two dropdowns and the current-period label."""
    return [
        {"range": f"{DASH}!A1", "values": [["Dashboard financeiro"]]},
        {"range": f"{DASH}!B3:F3", "values": [["Visão:", "Mensal", "", "Titular:", "PF"]]},
        {"range": f"{DASH}!H3", "values": [[f"={DATA}!H15"]]},
    ]


__all__ = ["DASH", "DATA", "build_dash_data", "dashboard_cells", "formula_cells", "setup_requests", "timedelta"]
