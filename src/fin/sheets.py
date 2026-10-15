"""Publication to Google Sheets (for Data Studio). The ledger stays the source of truth.

Tabs written only by the script (full snapshot, idempotent): lancamentos, resumo_mensal,
parcelas_futuras, dicionario. Tab you edit: revisoes (read first, approvals imported,
your edits on other rows preserved). Headers are stable: Data Studio depends on them.
"""

from __future__ import annotations

import csv
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from fin.categorize import approve, ReviewError
from fin.config import Config, Taxonomy
from fin.ledger import Ledger, local_id_str, parse_local_id
from fin.money import from_cents
from fin.reports import bank_balance, committed_installments, month_totals, OWNER_LABEL
from fin.rules import add_months

log = logging.getLogger("fin.sheets")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]  # no Drive scope on purpose

HEADERS: dict[str, list[str]] = {
    "lancamentos": ["id", "data", "competencia", "conta", "descricao", "valor", "natureza", "categoria",
                    "subcategoria", "necessidade", "essencial", "fonte", "status", "parcela", "revisao",
                    "origem_categoria", "par_id", "titular"],
    "revisoes": ["id", "data", "conta", "descricao", "valor", "sugestao", "origem", "confianca",
                 "categoria_final", "essencial", "necessidade", "fonte", "status"],
    "dicionario": ["tipo", "padrao", "natureza", "categoria", "subcategoria", "necessidade", "essencial", "fonte", "origem"],
    # one row per month and titular: Consolidado always; PF and PJ too when a PJ account exists.
    # Filter titular in Data Studio: summing every row would count the month twice.
    "resumo_mensal": ["mes", "titular", "receitas", "despesas", "investimentos", "saldo", "taxa_poupanca",
                      "parcelas_mes_seguinte", "pct_renda_parcelas", "retiradas_pj", "aportes_pj"],
    "parcelas_futuras": ["mes", "conta", "descricao", "parcela", "total_parcelas", "valor", "origem", "categoria"],
}
SCRIPT_TABS = ["lancamentos", "resumo_mensal", "parcelas_futuras", "dicionario"]
REVIEW_STATUSES = ["Pendente", "Aprovado"]


def cell(value: Any) -> Any:
    """Numbers stay numbers; None -> "". Text is sent as is: Sheets gets it with valueInputOption RAW
    (never parsed: no formulas, "3/10" or "2026-10" never turn into dates)."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return str(value)


def csv_cell(value: Any) -> Any:
    """CSV opened in Excel/Numbers *is* parsed: text starting with = + - @ is quoted (formula injection)."""
    value = cell(value)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") and not _is_number(value):
        return "'" + value
    return value


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _account_label(a: Any) -> str:
    bank = a["bank"].removesuffix("_pj").capitalize() + (" PJ" if a["owner"] == "pj" else "")
    return f"{bank} {'cartão' if a['type'] == 'CREDIT' else 'conta'}"


def _account_labels(ledger: Ledger) -> dict[str, str]:
    labels = {a["id"]: _account_label(a) for a in ledger.accounts()}
    labels["caju"] = "Caju"
    return labels


def _public_desc(row: Any, apelidos: dict[str, str]) -> str:
    if row["counterpart_alias"]:
        ap = apelidos.get(row["counterpart_alias"])
        return f"{row['counterpart_alias']} ({ap})" if ap else row["counterpart_alias"]
    return row["desc_norm"] or "(sem descrição)"  # never the raw text (may be only a CPF or a Pix key)


def build_tables(ledger: Ledger, today: date) -> dict[str, list[list[Any]]]:
    labels = _account_labels(ledger)
    owners = ledger.account_owners()
    apelidos = {r["alias"]: r["apelido"] for r in ledger.q("SELECT alias, apelido FROM counterparts WHERE apelido IS NOT NULL")}
    lanc = [HEADERS["lancamentos"]]
    for r in ledger.active_txs():
        parcela = f"{r['installment_number']}/{r['total_installments']}" if (r["total_installments"] or 0) > 1 else ""
        lanc.append([cell(x) for x in (
            local_id_str(r["local_id"]), r["date"], r["competence"], labels.get(r["account_id"], r["account_id"]),
            _public_desc(r, apelidos), from_cents(r["amount_cents"]), r["natureza"], r["categoria"], r["subcategoria"],
            r["necessidade"], r["essencial"], r["fonte"], "Pendente" if r["status"] == "PENDING" else "Lançado",
            parcela, r["review_status"], r["cat_source"], r["pair_id"], OWNER_LABEL[owners.get(r["account_id"], "pf")])])

    months = [m["competence"] for m in ledger.q("SELECT DISTINCT competence FROM transactions WHERE removed_upstream=0 ORDER BY competence")]
    resumo = [HEADERS["resumo_mensal"]]
    views = (None, "pf", "pj") if ledger.has_pj() else (None,)
    for m in months:
        nxt = add_months(m, 1)
        for owner in views:
            t = month_totals(ledger, m, owner)
            parc = committed_installments(ledger, [nxt], owner)[nxt]
            saldo = t["receitas"] - t["despesas"] + t["titular"]
            resumo.append([m, OWNER_LABEL[owner], from_cents(t["receitas"]), from_cents(t["despesas"]),
                           from_cents(t["investimentos"]), from_cents(saldo),
                           round(saldo / t["receitas"], 4) if t["receitas"] > 0 else "",
                           from_cents(parc), round(parc / t["receitas"], 4) if t["receitas"] > 0 else "",
                           from_cents(t["retiradas"]), from_cents(t["aportes"])])

    current = f"{today.year:04d}-{today.month:02d}"
    parcelas = [HEADERS["parcelas_futuras"]]
    for r in ledger.active_txs("total_installments > 1 AND competence > ?", (current,)):
        parcelas.append([r["competence"], labels.get(r["account_id"], ""), cell(r["desc_norm"] or "(sem descrição)"),
                         r["installment_number"], r["total_installments"], from_cents(-r["amount_cents"]), "Real (futura)",
                         cell(r["categoria"])])
    for p in ledger.q("SELECT * FROM installments_projection ORDER BY month, desc_norm, installment_number"):
        parcelas.append([p["month"], labels.get(p["account_id"], ""), cell(p["desc_norm"]), p["installment_number"],
                         p["total_installments"], from_cents(-p["amount_cents"]), "Projetada", cell(p["categoria"])])

    dic = [HEADERS["dicionario"]]
    for e in ledger.dictionary():
        dic.append([cell(x) for x in (e["kind"], e["pattern"], e["natureza"], e["categoria"], e["subcategoria"],
                                      e["necessidade"], e["essencial"], e["fonte"], e["origin"])])
    return {"lancamentos": lanc, "resumo_mensal": resumo, "parcelas_futuras": parcelas, "dicionario": dic}


MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro",
         "Outubro", "Novembro", "Dezembro"]
MONTH_TAB_HEADER = ["data", "conta", "titular", "descricao", "valor", "natureza", "categoria", "subcategoria",
                    "essencial", "parcela", "revisao"]


def month_tab_name(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MESES[int(m) - 1]} {y}"


def build_month_tabs(ledger: Ledger, today: date) -> dict[str, list[list[Any]]]:
    """One tab per calendar month (by transaction date, day 1 to last day), from January of this year to now."""
    labels = _account_labels(ledger)
    owners = ledger.account_owners()
    apelidos = {r["alias"]: r["apelido"] for r in ledger.q("SELECT alias, apelido FROM counterparts WHERE apelido IS NOT NULL")}
    out: dict[str, list[list[Any]]] = {}
    months = [f"{today.year:04d}-{m:02d}" for m in range(1, today.month + 1)]
    for ym in months:
        rows = ledger.active_txs("substr(date, 1, 7) = ?", (ym,))
        body = []
        tot = {"Receita": 0, "Despesa": 0, "Investimento": 0, "Transferência": 0}
        for r in sorted(rows, key=lambda x: (x["date"], x["local_id"])):
            nat = r["natureza"] or ""
            if nat in tot:
                tot[nat] += r["amount_cents"]
            parcela = f"{r['installment_number']}/{r['total_installments']}" if r["total_installments"] else ""
            body.append([cell(x) for x in (
                r["date"], labels.get(r["account_id"], r["account_id"]), OWNER_LABEL[owners.get(r["account_id"], "pf")],
                _public_desc(r, apelidos), from_cents(r["amount_cents"]), nat, r["categoria"], r["subcategoria"],
                r["essencial"], parcela, r["review_status"])])
        resumo = [
            [f"{month_tab_name(ym)} (01 a {_last_day(ym):02d}, pela data da transação)"],
            ["Receitas", from_cents(tot["Receita"]), "Despesas", from_cents(-tot["Despesa"]),
             "Saldo (receitas - despesas)", from_cents(tot["Receita"] + tot["Despesa"])],
            ["Transferências e pagamentos de fatura não contam como gasto. PF e PJ somados; o resumo_mensal separa."],
            [],
        ]
        out[month_tab_name(ym)] = resumo + [MONTH_TAB_HEADER] + body
    return out


PAINEL = "Painel"
PAINEL_ROWS, PAINEL_COLS = 40, 8
# fixed blocks (0-based rows) so the charts can point at stable ranges
P_MONTHS_HDR, P_MONTHS_N = 5, 12          # A6:E18  last 12 months, PF + PJ revenue
P_CATS_HDR, P_CATS_N = 5, 12              # G6:H18  spending by category, current month (PF)
P_INST_HDR, P_INST_N = 20, 6              # A21:B27 installments already committed, next 6 months (PF)
P_PREV_HDR, P_PREV_N = 20, 12             # G21:H33 spending by category, previous month (PF)


def build_painel(ledger: Ledger, today: date) -> list[list[Any]]:
    """Dashboard tab: KPIs + 4 fixed tables that the charts read. Numbers by competence, like resumo_mensal."""
    grid: list[list[Any]] = [["" for _ in range(PAINEL_COLS)] for _ in range(PAINEL_ROWS)]

    def put(r: int, c: int, v: Any) -> None:
        grid[r][c] = v

    cur = f"{today.year:04d}-{today.month:02d}"
    prev = add_months(cur, -1)
    has_pj = ledger.has_pj()
    t_cur, t_prev = month_totals(ledger, cur, "pf"), month_totals(ledger, prev, "pf")
    nxt = add_months(cur, 1)
    put(0, 0, f"Painel financeiro (atualizado em {today:%d/%m/%Y})")
    put(2, 0, "Saldo nas contas PF")
    put(3, 0, from_cents(bank_balance(ledger, "pf")))
    put(2, 1, "Saldo conta PJ")
    put(3, 1, from_cents(bank_balance(ledger, "pj")) if has_pj else "")
    put(2, 2, f"Saldo do mês {cur} (PF)")
    put(3, 2, from_cents(t_cur["receitas"] - t_cur["despesas"] + t_cur["titular"]))
    sp = t_prev["receitas"] - t_prev["despesas"] + t_prev["titular"]
    put(2, 3, f"Poupança {prev} (PF)")
    put(3, 3, round(sp / t_prev["receitas"], 4) if t_prev["receitas"] > 0 else "")
    put(2, 4, f"Parcelas já em {nxt} (PF)")
    put(3, 4, from_cents(committed_installments(ledger, [nxt], "pf")[nxt]))

    for i, h in enumerate(["mes", "receitas_pf", "despesas_pf", "saldo_pf", "faturamento_pj"]):
        put(P_MONTHS_HDR, i, h)
    months = [f"{today.year:04d}-{m:02d}" for m in range(1, today.month + 1)]  # this year only
    for k, m in enumerate(months):
        t = month_totals(ledger, m, "pf")
        pj = month_totals(ledger, m, "pj")["receitas"] if has_pj else 0
        row = P_MONTHS_HDR + 1 + k
        put(row, 0, m)
        put(row, 1, from_cents(t["receitas"]))
        put(row, 2, from_cents(t["despesas"]))
        put(row, 3, from_cents(t["receitas"] - t["despesas"] + t["titular"]))
        put(row, 4, from_cents(pj))

    def cats_block(hdr: int, n: int, totals: dict, title: str) -> None:
        put(hdr, 6, title)
        put(hdr, 7, "valor")
        items = sorted(totals["categorias"].items(), key=lambda kv: -kv[1])
        items = [kv for kv in items if kv[1] > 0]
        if len(items) > n:
            rest = sum(v for _, v in items[n - 1:])
            items = items[:n - 1] + [("Demais", rest)]
        for k, (cat, v) in enumerate(items):
            put(hdr + 1 + k, 6, cat)
            put(hdr + 1 + k, 7, from_cents(v))

    cats_block(P_CATS_HDR, P_CATS_N, t_cur, f"categoria {cur}")
    cats_block(P_PREV_HDR, P_PREV_N, t_prev, f"categoria {prev}")

    put(P_INST_HDR, 0, "mes")
    put(P_INST_HDR, 1, "parcelas_pf")
    inst_months = [add_months(cur, k) for k in range(1, P_INST_N + 1)]
    inst = committed_installments(ledger, inst_months, "pf")
    for k, m in enumerate(inst_months):
        put(P_INST_HDR + 1 + k, 0, m)
        put(P_INST_HDR + 1 + k, 1, from_cents(inst[m]))
    put(P_PREV_HDR + P_PREV_N + 3, 0, "Valores por competência (mês da fatura no cartão). Revisar = ainda não classificado.")
    return grid


def _gr(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict[str, int]:
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}


def painel_format_requests(sid: int) -> list[dict[str, Any]]:
    brl = {"numberFormat": {"type": "CURRENCY", "pattern": "\"R$\" #,##0.00"}}
    pct = {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}

    def fmt(r0: int, r1: int, c0: int, c1: int, f: dict[str, Any]) -> dict[str, Any]:
        return {"repeatCell": {"range": _gr(sid, r0, r1, c0, c1), "cell": {"userEnteredFormat": f},
                               "fields": "userEnteredFormat.numberFormat"}}

    bold = {"repeatCell": {"range": _gr(sid, 0, 1, 0, 1), "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                           "fields": "userEnteredFormat.textFormat"}}
    heads = [{"repeatCell": {"range": _gr(sid, r, r + 1, 0, PAINEL_COLS), "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                             "fields": "userEnteredFormat.textFormat"}} for r in (2, P_MONTHS_HDR, P_INST_HDR)]
    return [bold, *heads,
            fmt(3, 4, 0, 3, brl), fmt(3, 4, 3, 4, pct), fmt(3, 4, 4, 5, brl),
            fmt(P_MONTHS_HDR + 1, P_MONTHS_HDR + 1 + P_MONTHS_N, 1, 5, brl),
            fmt(P_CATS_HDR + 1, P_PREV_HDR + 1 + P_PREV_N, 7, 8, brl),
            fmt(P_INST_HDR + 1, P_INST_HDR + 1 + P_INST_N, 1, 2, brl),
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 8},
                                           "properties": {"pixelSize": 150}, "fields": "pixelSize"}}]


def painel_chart_requests(sid: int) -> list[dict[str, Any]]:
    def pos(row: int) -> dict[str, Any]:
        return {"overlayPosition": {"anchorCell": {"sheetId": sid, "rowIndex": row, "columnIndex": 9},
                                    "widthPixels": 620, "heightPixels": 320}}
    mh, mn = P_MONTHS_HDR, P_MONTHS_N
    col = {"title": "Receitas x Despesas por mês (PF)", "basicChart": {
        "chartType": "COLUMN", "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(sid, mh, mh + 1 + mn, 0, 1)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(sid, mh, mh + 1 + mn, c, c + 1)]}}, "targetAxis": "LEFT_AXIS"}
                   for c in (1, 2)]}}
    saldo = {"title": "Saldo do mês (PF) e faturamento PJ", "basicChart": {
        "chartType": "LINE", "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(sid, mh, mh + 1 + mn, 0, 1)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(sid, mh, mh + 1 + mn, c, c + 1)]}}, "targetAxis": "LEFT_AXIS"}
                   for c in (3, 4)]}}
    pie = {"title": "Gastos por categoria, mês atual (PF)", "pieChart": {
        "legendPosition": "RIGHT_LEGEND",
        "domain": {"sourceRange": {"sources": [_gr(sid, P_CATS_HDR + 1, P_CATS_HDR + 1 + P_CATS_N, 6, 7)]}},
        "series": {"sourceRange": {"sources": [_gr(sid, P_CATS_HDR + 1, P_CATS_HDR + 1 + P_CATS_N, 7, 8)]}}}}
    pie_prev = {"title": "Gastos por categoria, mês anterior (PF)", "pieChart": {
        "legendPosition": "RIGHT_LEGEND",
        "domain": {"sourceRange": {"sources": [_gr(sid, P_PREV_HDR + 1, P_PREV_HDR + 1 + P_PREV_N, 6, 7)]}},
        "series": {"sourceRange": {"sources": [_gr(sid, P_PREV_HDR + 1, P_PREV_HDR + 1 + P_PREV_N, 7, 8)]}}}}
    inst = {"title": "Parcelas já comprometidas (PF)", "basicChart": {
        "chartType": "COLUMN", "legendPosition": "NO_LEGEND", "headerCount": 1,
        "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
        "domains": [{"domain": {"sourceRange": {"sources": [_gr(sid, P_INST_HDR, P_INST_HDR + 1 + P_INST_N, 0, 1)]}}}],
        "series": [{"series": {"sourceRange": {"sources": [_gr(sid, P_INST_HDR, P_INST_HDR + 1 + P_INST_N, 1, 2)]}},
                    "targetAxis": "LEFT_AXIS"}]}}
    specs = [col, saldo, pie, pie_prev, inst]
    return [{"addChart": {"chart": {"spec": sp, "position": pos(17 * i)}}} for i, sp in enumerate(specs)]


def _last_day(ym: str) -> int:
    import calendar
    y, m = (int(x) for x in ym.split("-"))
    return calendar.monthrange(y, m)[1]


def pending_review_rows(ledger: Ledger) -> list[list[Any]]:
    labels = _account_labels(ledger)
    apelidos = {r["alias"]: r["apelido"] for r in ledger.q("SELECT alias, apelido FROM counterparts WHERE apelido IS NOT NULL")}
    out = []
    for r in ledger.active_txs("review_status='pendente'"):
        out.append([cell(x) for x in (
            local_id_str(r["local_id"]), r["date"], labels.get(r["account_id"], ""), _public_desc(r, apelidos),
            from_cents(r["amount_cents"]), r["categoria"], r["cat_source"],
            round(r["confidence"], 2) if r["confidence"] is not None else "", "", "", "", "", "Pendente")])
    return out


def merge_reviews(ledger: Ledger, tax: Taxonomy, sheet_rows: list[list[Any]]) -> tuple[list[list[Any]], dict[str, int]]:
    """Import 'Aprovado' rows, keep the rest (with your edits), add new pending rows."""
    stats = {"imported": 0, "invalid": 0}
    header = HEADERS["revisoes"]
    idx = {h: i for i, h in enumerate(header)}
    kept: list[list[Any]] = []
    listed: set[str] = set()
    body = sheet_rows[1:] if sheet_rows and sheet_rows[0][:1] == ["id"] else sheet_rows
    for raw in body:
        row = list(raw) + [""] * (len(header) - len(raw))
        rid = str(row[idx["id"]]).strip()
        if not rid:
            continue
        status = str(row[idx["status"]]).strip().lower()
        if status == "aprovado":
            try:
                approve(ledger, tax, parse_local_id(rid),
                        categoria=str(row[idx["categoria_final"]]).strip() or None,
                        essencial=str(row[idx["essencial"]]).strip() or None,
                        necessidade=str(row[idx["necessidade"]]).strip() or None,
                        fonte=str(row[idx["fonte"]]).strip() or None)
                stats["imported"] += 1
                continue
            except (ReviewError, ValueError):
                stats["invalid"] += 1
                row[idx["status"]] = "Pendente"
        tx = ledger.tx_by_local(parse_local_id(rid)) if rid[1:].isdigit() else None
        if tx is None or tx["review_status"] != "pendente":
            continue  # already decided elsewhere (CLI) or gone
        kept.append([cell(v) for v in row[: len(header)]])
        listed.add(rid)
    for new in pending_review_rows(ledger):
        if new[0] not in listed:
            kept.append(new)
    return [header] + kept, stats


class Publisher(Protocol):
    def read(self, tab: str) -> list[list[Any]]: ...
    def write(self, tables: dict[str, list[list[Any]]]) -> None: ...
    def ensure_layout(self, tax: Taxonomy) -> None: ...


class CsvPublisher:
    """Writes each tab as CSV (demo / dry-run). Reads revisoes back from disk."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def read(self, tab: str) -> list[list[Any]]:
        p = self.out_dir / f"{tab}.csv"
        if not p.exists():
            return []
        with p.open(encoding="utf-8", newline="") as f:
            return [row for row in csv.reader(f)]

    def write(self, tables: dict[str, list[list[Any]]]) -> None:
        for tab, rows in tables.items():
            with (self.out_dir / f"{tab}.csv").open("w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows([[csv_cell(v) for v in row] for row in rows])

    def ensure_layout(self, tax: Taxonomy) -> None:
        return None


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out or "A"


def _with_backoff(fn: Callable[[], Any], sleep: Callable[[float], None] = time.sleep, attempts: int = 6) -> Any:
    from gspread.exceptions import APIError

    for n in range(attempts):
        try:
            return fn()
        except APIError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", 0)
            if code not in (429, 500, 502, 503, 504) or n == attempts - 1:
                raise
            sleep(min(2 ** n + random.random(), 64))
    return None


class GSheetsPublisher:
    def __init__(self, service_account_json: str, spreadsheet_id: str):
        import gspread

        info = json.loads(service_account_json)
        self.gc = gspread.service_account_from_dict(info, scopes=SCOPES)
        self.sh = _with_backoff(lambda: self.gc.open_by_key(spreadsheet_id))

    def read(self, tab: str) -> list[list[Any]]:
        try:
            data = _with_backoff(lambda: self.sh.values_get(f"{tab}!A:Z", params={"valueRenderOption": "UNFORMATTED_VALUE"}))
        except Exception:  # noqa: BLE001 - missing tab on first run
            return []
        return data.get("values", [])

    def write(self, tables: dict[str, list[list[Any]]]) -> None:
        data = [{"range": f"{tab}!A1", "values": rows} for tab, rows in tables.items()]
        # RAW: nothing typed by the script is interpreted (formulas, dates, "3/10"); numbers stay numbers
        _with_backoff(lambda: self.sh.values_batch_update({"valueInputOption": "RAW", "data": data}))
        # clear leftovers from a longer previous publish, only inside the current grid
        grid = {ws.title: ws for ws in _with_backoff(self.sh.worksheets)}
        clear = []
        for tab, rows in tables.items():
            ws = grid.get(tab)
            if ws is not None and ws.row_count > len(rows):
                clear.append(f"{tab}!A{len(rows) + 1}:{_col_letter(ws.col_count)}{ws.row_count}")
        if clear:
            _with_backoff(lambda: self.sh.values_batch_clear(body={"ranges": clear}))

    def ensure_painel(self) -> None:
        """Create the dashboard tab first in the tab bar and its charts once (they read fixed ranges)."""
        existing = {ws.title: ws for ws in _with_backoff(self.sh.worksheets)}
        ws = existing.get(PAINEL)
        if ws is None:
            ws = _with_backoff(lambda: self.sh.add_worksheet(title=PAINEL, rows=100, cols=20, index=0))
        elif ws.row_count < 100:  # charts are stacked down to row ~90
            _with_backoff(lambda: ws.resize(rows=100))
        meta = _with_backoff(lambda: self.sh.fetch_sheet_metadata({"fields": "sheets(properties.sheetId,charts.chartId)"}))
        charts = next((sh.get("charts", []) for sh in meta.get("sheets", [])
                       if sh["properties"]["sheetId"] == ws.id), [])
        reqs = painel_format_requests(ws.id) + ([] if charts else painel_chart_requests(ws.id))
        _with_backoff(lambda: self.sh.batch_update({"requests": reqs}))

    def ensure_dashboard(self) -> None:
        """Charts-only Dashboard (dropdowns view/titular) + hidden _dash with data and selector formulas."""
        from fin.dashboard import DASH, dashboard_cells, DATA, formula_cells, setup_requests

        existing = {ws.title: ws for ws in _with_backoff(self.sh.worksheets)}
        dash = existing.get(DASH) or _with_backoff(lambda: self.sh.add_worksheet(title=DASH, rows=60, cols=16, index=0))
        data = existing.get(DATA) or _with_backoff(lambda: self.sh.add_worksheet(title=DATA, rows=400, cols=12))
        meta = _with_backoff(lambda: self.sh.fetch_sheet_metadata({"fields": "sheets(properties.sheetId,charts.chartId)"}))
        has_charts = any(sh.get("charts") for sh in meta.get("sheets", []) if sh["properties"]["sheetId"] == dash.id)
        # selector formulas are rewritten every time (self-healing); dropdown values/charts only on creation
        formulas = [{"range": f"{DATA}!{a1}", "values": [[f]]} for a1, f in formula_cells().items()]
        cells = formulas if has_charts else dashboard_cells() + formulas
        _with_backoff(lambda: self.sh.values_batch_update({"valueInputOption": "USER_ENTERED", "data": cells}))
        if not has_charts:
            _with_backoff(lambda: self.sh.batch_update({"requests": setup_requests(dash.id, data.id)}))

    def ensure_tabs(self, names: list[str]) -> None:
        existing = {ws.title for ws in _with_backoff(self.sh.worksheets)}
        for name in names:
            if name not in existing:
                _with_backoff(lambda n=name: self.sh.add_worksheet(title=n, rows=1000, cols=len(MONTH_TAB_HEADER)))

    def ensure_layout(self, tax: Taxonomy) -> None:
        existing = {ws.title: ws for ws in _with_backoff(self.sh.worksheets)}
        requests: list[dict[str, Any]] = []
        for tab, header in HEADERS.items():
            if tab not in existing:
                ws = _with_backoff(lambda t=tab, h=header: self.sh.add_worksheet(title=t, rows=1000, cols=max(len(h), 10)))
                existing[tab] = ws
        for tab in SCRIPT_TABS:
            ws = existing[tab]
            protected = _with_backoff(lambda w=ws: self.sh.list_protected_ranges(w.id))
            if not any(p.get("description") == "fin:managed" for p in protected):
                requests.append({"addProtectedRange": {"protectedRange": {
                    "range": {"sheetId": ws.id}, "description": "fin:managed", "warningOnly": True}}})
        rev = existing["revisoes"]
        cats = [c for c in tax.names if c != "Revisar"]
        for col, values in ((8, cats), (9, tax.essencial), (10, tax.necessidades), (11, tax.fontes), (12, REVIEW_STATUSES)):
            requests.append({"setDataValidation": {
                "range": {"sheetId": rev.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                         "showCustomUi": True, "strict": False}}})
        if requests:
            _with_backoff(lambda: self.sh.batch_update({"requests": requests}))


def publish(ledger: Ledger, tax: Taxonomy, publisher: Publisher, today: date, ensure: bool = True) -> dict[str, int]:
    if ensure:
        publisher.ensure_layout(tax)
    reviews, stats = merge_reviews(ledger, tax, publisher.read("revisoes"))
    tables = build_tables(ledger, today)  # after importing approvals
    tables["revisoes"] = reviews
    month_tabs = build_month_tabs(ledger, today)
    if hasattr(publisher, "ensure_tabs"):
        publisher.ensure_tabs(list(month_tabs))
    tables.update(month_tabs)
    tables[PAINEL] = build_painel(ledger, today)
    if hasattr(publisher, "ensure_painel"):
        publisher.ensure_painel()
    from fin.dashboard import build_dash_data, DATA

    tables[DATA] = build_dash_data(ledger, today)
    if hasattr(publisher, "ensure_dashboard"):
        publisher.ensure_dashboard()
    publisher.write(tables)
    stats.update({tab: len(rows) - 1 for tab, rows in tables.items()})
    return stats


def make_publisher(cfg: Config, get_secret: Callable[[str], str | None]) -> GSheetsPublisher:
    sa = get_secret("google_service_account_json")
    if not sa or not cfg.spreadsheet_id:
        raise RuntimeError("Sheets não configurado: rode fin setup google e preencha sheets.spreadsheet_id")
    return GSheetsPublisher(sa, cfg.spreadsheet_id)
