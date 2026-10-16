"""Deterministic reports. Every number comes from the ledger; nothing from the LLM.

Conventions (net-worth view, cents):
- Receita: + amounts with natureza Receita; Despesa: -(sum) of natureza Despesa (refunds reduce it);
  Investimento: -(sum) (money moved to investments, net of yields/redemptions).
- Transferência (own transfers, card bill payments) never counts.
- Card purchases count in the month of the bill (competence), bank items by date.

PF x PJ (owner of the account, config pluggy.items[].owner):
- Consolidated (owner=None): every account; PF<->PJ transfers are Transferência and never count.
- PF view: "Retirada PJ→PF" is income (source "Retirada PJ", apart from the CLT salary);
  "Aporte PF→PJ" is an outflow to the company (reduces the balance, not an expense).
- PJ view: Faturamento/DAS/fees as usual; retiradas leave and aportes enter the balance, never as
  revenue or expense. So saldo PF + saldo PJ == saldo consolidado (no double counting).
- /posso_comprar, saldo livre, parcelas and custos fixos use only PF (the card is PF): monthly_summary(owner="pf").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fin.config import Config, Taxonomy
from fin.ledger import Ledger
from fin.money import brl
from fin.rules import add_months, OWNER_TRANSFER_CATS, RETIRADA

HORIZON = 6
PAY_GRACE_DAYS = 5
RETIRADA_FONTE = "Retirada PJ"
OWNER_LABEL = {"pf": "PF", "pj": "PJ", None: "Consolidado"}
# owner of a transaction = owner of its account; rows of accounts not in the table (manual) are PF
OWNER_SQL = "COALESCE((SELECT owner FROM accounts WHERE accounts.id=transactions.account_id),'pf')=?"


def _owner_where(where: str, params: tuple, owner: str | None) -> tuple[str, tuple]:
    if owner is None:
        return where, params
    return f"({where}) AND {OWNER_SQL}", (*params, owner)


def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _due_date(month: str, day: int) -> date:
    import calendar

    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, min(max(day, 1), calendar.monthrange(y, m)[1]))


def next_salary_date(today: date, salary_day: int, received_this_month: bool = False) -> date:
    """Next salary. If this month's salary already came in (early, e.g. day 3 for salary_day 5), it is next month's."""
    if today.day < salary_day and not received_this_month:
        return _due_date(ym(today), salary_day)
    return _due_date(add_months(ym(today), 1), salary_day)


def salary_received(ledger: Ledger, today: date) -> bool:
    row = ledger.one(
        "SELECT 1 FROM transactions WHERE removed_upstream=0 AND amount_cents > 0 AND substr(date,1,7)=? AND date<=? "
        "AND (rule='salary' OR (rule='income_source' AND categoria='Salário CLT')) LIMIT 1",
        (ym(today), today.isoformat()))
    return row is not None


@dataclass
class CardBill:
    account_id: str
    label: str
    month: str
    due: date | None
    amount_cents: int          # purchases - refunds (+ projected installments for future months)
    projected_cents: int
    paid_cents: int
    @property
    def open_cents(self) -> int:
        return max(self.amount_cents - self.paid_cents, 0)


@dataclass
class OwnerView:
    """Month totals of one owner (PF, PJ) or of everything (Consolidado)."""
    titular: str
    receitas: int = 0
    receitas_por_fonte: dict[str, int] = field(default_factory=dict)
    despesas: int = 0
    despesas_por_categoria: dict[str, int] = field(default_factory=dict)
    investimentos: int = 0
    retiradas: int = 0   # PJ -> PF (PF view: already inside receitas)
    aportes: int = 0     # PF -> PJ
    mov_titular: int = 0  # what moves the balance outside receitas/despesas (PF: -aportes; PJ: aportes - retiradas)
    saldo: int = 0
    taxa_poupanca: float | None = None
    saldo_contas: int = 0


@dataclass
class Summary:
    month: str
    today: date
    receitas: int = 0
    receitas_por_fonte: dict[str, int] = field(default_factory=dict)
    despesas: int = 0
    despesas_por_categoria: dict[str, int] = field(default_factory=dict)
    investimentos: int = 0
    saldo: int = 0
    taxa_poupanca: float | None = None
    parcelas: dict[str, int] = field(default_factory=dict)
    renda_ref: int = 0
    fixos_pendentes: list[tuple[str, int, int]] = field(default_factory=list)
    saldo_contas: int = 0
    caju_saldo: int = 0
    faturas_ate_salario: int = 0
    fixos_ate_salario: int = 0
    saldo_livre: int = 0
    proximo_salario: date | None = None
    pendentes: int = 0
    media_variavel: int = 0
    fixos_total: int = 0
    owner: str | None = "pf"
    aportes: int = 0
    mov_titular: int = 0
    secoes: dict[str, OwnerView] = field(default_factory=dict)  # PF / PJ / Consolidado, only when a PJ account exists


FATURAMENTO_PJ = "Faturamento PJ"


def month_totals(ledger: Ledger, month: str, owner: str | None = None) -> dict[str, Any]:
    return _with_pj_billing_in_pf(ledger, _totals(ledger.active_txs(*_owner_where("competence=?", (month,), owner)), owner),
                                  "competence=?", (month,), owner)


def date_range_totals(ledger: Ledger, start: str, end: str, owner: str | None = None) -> dict[str, Any]:
    """Same as month_totals, by transaction date (inclusive ISO dates) instead of competence."""
    return _with_pj_billing_in_pf(ledger, _totals(ledger.active_txs(*_owner_where("date >= ? AND date <= ?", (start, end), owner)), owner),
                                  "date >= ? AND date <= ?", (start, end), owner)


# MEI without separation of accounts: the company only has revenue, DAS and bank fees. Anything else paid
# from the PJ account is your personal spending: an implicit withdrawal from the PJ + your expense in the PF.
PJ_OWN_CATS = {"Faturamento PJ", "Impostos/DAS", "Taxas bancárias PJ"}


def _with_pj_billing_in_pf(ledger: Ledger, out: dict[str, Any], where: str, params: tuple, owner: str | None) -> dict[str, Any]:
    """A client of the company that paid into a PF account is still company revenue: in the PJ view it is
    faturamento followed by an implicit withdrawal to the titular (net zero on the PJ balance).
    Personal spending paid from the PJ account moves to the PF view (see PJ_OWN_CATS)."""
    if owner in ("pf", "pj"):
        cats = tuple(sorted(PJ_OWN_CATS))
        marks = ",".join("?" * len(cats))
        for r in ledger.active_txs(*_owner_where(f"({where}) AND natureza='Despesa' AND COALESCE(categoria,'Revisar') NOT IN ({marks})",
                                                 (*params, *cats), "pj")):
            amt, cat = r["amount_cents"], r["categoria"] or "Revisar"
            if owner == "pj":  # undo the expense, book it as a withdrawal to the titular
                out["despesas"] += amt
                out["categorias"][cat] = out["categorias"].get(cat, 0) + amt
                if not out["categorias"][cat]:
                    del out["categorias"][cat]
                out["retiradas"] -= amt
                out["titular"] += amt
            else:  # your expense, funded by a withdrawal from the PJ
                out["despesas"] -= amt
                out["categorias"][cat] = out["categorias"].get(cat, 0) - amt
                out["receitas"] -= amt
                out["fontes"][RETIRADA_FONTE] = out["fontes"].get(RETIRADA_FONTE, 0) - amt
    if owner != "pj":
        return out
    for r in ledger.active_txs(*_owner_where(f"({where}) AND natureza='Receita' AND categoria=?", (*params, FATURAMENTO_PJ), "pf")):
        out["receitas"] += r["amount_cents"]
        key = r["fonte"] or "sem fonte"
        out["fontes"][key] = out["fontes"].get(key, 0) + r["amount_cents"]
        out["retiradas"] += r["amount_cents"]
        out["titular"] -= r["amount_cents"]
    return out


def _totals(rows: list[Any], owner: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"receitas": 0, "despesas": 0, "investimentos": 0, "fontes": {}, "categorias": {},
                           "retiradas": 0, "aportes": 0, "titular": 0}
    for r in rows:
        nat, amt = r["natureza"], r["amount_cents"]
        if owner and nat == "Transferência" and r["categoria"] in OWNER_TRANSFER_CATS:
            if r["categoria"] == RETIRADA:
                out["retiradas"] += abs(amt)
                if owner == "pf":  # income for you, coming from the company (not salary)
                    out["receitas"] += amt
                    out["fontes"][RETIRADA_FONTE] = out["fontes"].get(RETIRADA_FONTE, 0) + amt
                else:
                    out["titular"] += amt
            else:  # APORTE
                out["aportes"] += abs(amt)
                out["titular"] += amt
            continue
        if nat == "Receita":
            out["receitas"] += amt
            key = r["fonte"] or "sem fonte"
            out["fontes"][key] = out["fontes"].get(key, 0) + amt
        elif nat == "Despesa":
            out["despesas"] -= amt
            key = r["categoria"] or "Revisar"
            out["categorias"][key] = out["categorias"].get(key, 0) - amt
        elif nat == "Investimento":
            out["investimentos"] -= amt
        elif nat is None and r["categoria"] is None:
            # not categorized yet: count by sign so the month never looks better than it is
            if amt < 0:
                out["despesas"] -= amt
                out["categorias"]["Revisar"] = out["categorias"].get("Revisar", 0) - amt
    return out


def owner_view(ledger: Ledger, month: str, owner: str | None) -> OwnerView:
    t = month_totals(ledger, month, owner)
    v = OwnerView(OWNER_LABEL[owner], receitas=t["receitas"], despesas=t["despesas"], investimentos=t["investimentos"],
                  retiradas=t["retiradas"], aportes=t["aportes"], mov_titular=t["titular"])
    v.receitas_por_fonte = dict(sorted(t["fontes"].items(), key=lambda kv: -kv[1]))
    v.despesas_por_categoria = dict(sorted(t["categorias"].items(), key=lambda kv: -kv[1]))
    v.saldo = v.receitas - v.despesas + v.mov_titular
    v.taxa_poupanca = (v.saldo / v.receitas) if v.receitas > 0 else None
    v.saldo_contas = bank_balance(ledger, owner)
    return v


def bank_balance(ledger: Ledger, owner: str | None = None) -> int:
    rows = ledger.q("SELECT balance_cents, owner FROM accounts WHERE type='BANK'")
    return sum(a["balance_cents"] or 0 for a in rows if owner is None or (a["owner"] or "pf") == owner)


def committed_installments(ledger: Ledger, months: list[str], owner: str | None = None) -> dict[str, int]:
    out = {m: 0 for m in months}
    for r in ledger.active_txs(*_owner_where("total_installments > 1 AND COALESCE(natureza,'Despesa')='Despesa'", (), owner)):
        if r["competence"] in out:
            out[r["competence"]] -= r["amount_cents"]
    proj_owner = "" if owner is None else " WHERE COALESCE((SELECT owner FROM accounts WHERE accounts.id=p.account_id),'pf')=?"
    for p in ledger.q("SELECT month, amount_cents FROM installments_projection p" + proj_owner, () if owner is None else (owner,)):
        if p["month"] in out:
            out[p["month"]] -= p["amount_cents"]
    return out


def income_reference(ledger: Ledger, cfg: Config, month: str, owner: str | None = None) -> int:
    if cfg.expected_income_cents:
        return cfg.expected_income_cents
    vals = [month_totals(ledger, add_months(month, -k), owner)["receitas"] for k in (1, 2, 3)]
    vals = [v for v in vals if v > 0]
    if vals:
        return sum(vals) // len(vals)
    return month_totals(ledger, month, owner)["receitas"]


def average_variable(ledger: Ledger, month: str, owner: str | None = None) -> int:
    return _average_spend(ledger, month, "COALESCE(necessidade,'variavel')='variavel'", owner)


def average_fixed(ledger: Ledger, month: str, owner: str | None = None) -> int:
    """Fixed spending actually seen (subscriptions, rent by Pix...), also what is not in config.fixed_costs."""
    return _average_spend(ledger, month, "necessidade='fixo'", owner)


def _average_spend(ledger: Ledger, month: str, cond: str, owner: str | None = None) -> int:
    totals = []
    for k in (1, 2, 3):
        m = add_months(month, -k)
        rows = ledger.active_txs(*_owner_where(f"competence=? AND natureza='Despesa' AND {cond} "
                                               "AND COALESCE(total_installments,1) <= 1", (m,), owner))
        if rows:
            totals.append(-sum(r["amount_cents"] for r in rows))
    return sum(totals) // len(totals) if totals else 0


def fixed_cost_status(ledger: Ledger, cfg: Config, month: str, owner: str | None = None) -> list[tuple[str, int, int, bool]]:
    # The payment is searched in every account, whatever the view: a personal bill paid from the PJ account is
    # still paid (`owner` only scopes the views that call this; it no longer filters where the payment came from).
    del owner
    rows = ledger.active_txs("(competence=? OR substr(date,1,7)=?) AND amount_cents < 0", (month, month))
    out = []
    for fc in cfg.fixed_costs:
        # paid when the matching debits of the month reach ~80% of the expected value (a R$ 20 Pix to the same
        # person does not mark a R$ 1.660 installment as paid; two R$ 99,99 plans cover R$ 199,98)
        hits = sum(-r["amount_cents"] for r in rows
                   if fc.padrao in (r["desc_norm"] or "") or fc.padrao in (r["description"] or "").upper())
        paid = hits > 0 and hits >= 0.8 * fc.valor_cents
        out.append((fc.nome, fc.valor_cents, fc.dia, paid))
    return out


def fixed_until(ledger: Ledger, cfg: Config, today: date, until: date, owner: str | None = None) -> int:
    """Unpaid fixed costs due before `until`, month by month (rent on the 1st of next month counts
    when the salary only comes on the 5th). This month's overdue unpaid ones count too."""
    total = 0
    month = ym(today)
    while _due_date(month, 1) < until:
        for _nome, valor, dia, paid in fixed_cost_status(ledger, cfg, month, owner):
            due = _due_date(month, dia)
            if paid or due >= until or (month != ym(today) and due < today):
                continue
            total += valor
        month = add_months(month, 1)
    return total


def card_bills(ledger: Ledger, cfg: Config, today: date, months: int = 2, owner: str | None = None) -> list[CardBill]:
    out = []
    first = ym(today)
    for acc in ledger.q("SELECT * FROM accounts WHERE type='CREDIT' ORDER BY bank, id"):
        if owner is not None and (acc["owner"] or "pf") != owner:
            continue
        label = f"{acc['bank'].capitalize()} final {acc['last4']}" if acc["last4"] else acc["bank"].capitalize()
        due_day = cfg.card(acc["bank"]).due_day or (int(acc["balance_due_date"][8:10]) if acc["balance_due_date"] else 0)
        for k in range(months):
            m = add_months(first, k)
            rows = ledger.active_txs("account_id=? AND competence=? AND COALESCE(natureza,'Despesa')!='Transferência'", (acc["id"], m))
            amount = -sum(r["amount_cents"] for r in rows)
            proj = -sum(p["amount_cents"] for p in ledger.q(
                "SELECT amount_cents FROM installments_projection WHERE account_id=? AND month=?", (acc["id"], m)))
            due = _due_date(m, due_day) if due_day else None
            paid = 0
            if due:
                # payments between the previous due date (+ grace) and this due date (+ grace): early payments
                # (Nubank allows any day) and late ones (due on Saturday, paid on Monday) both count
                prev_due = _due_date(add_months(m, -1), due_day)
                lo = date.fromordinal(prev_due.toordinal() + PAY_GRACE_DAYS + 1).isoformat()
                hi = date.fromordinal(due.toordinal() + PAY_GRACE_DAYS).isoformat()
                paid = sum(r["amount_cents"] for r in ledger.active_txs(
                    "account_id=? AND rule='card_bill_payment' AND date BETWEEN ? AND ?", (acc["id"], lo, hi)))
            out.append(CardBill(acc["id"], label, m, due, amount + proj, proj, paid))
        # Anchor the open bill to the bank: the card's used limit (balance) is the whole debt still to pay,
        # rotativo, Pix no crédito and fees included. Open bill = balance - what is already scheduled for the
        # following bills. Summing our own rows misses lines the bank adds (rotativo carryover, financing).
        if acc["balance_cents"] and out and out[-months].account_id == acc["id"]:
            later = 0
            for r in ledger.active_txs("account_id=? AND competence > ? AND COALESCE(rule,'') != 'card_bill_payment'",
                                       (acc["id"], first)):
                later -= r["amount_cents"]
            later -= sum(p["amount_cents"] for p in ledger.q(
                "SELECT amount_cents FROM installments_projection WHERE account_id=? AND month > ?", (acc["id"], first)))
            open_now = acc["balance_cents"] - later
            b0 = out[-months]
            if open_now <= b0.open_cents or (b0.paid_cents and b0.paid_cents >= b0.amount_cents):
                continue  # our rows already explain it, or this bill is paid and the balance is for the next ones
            out[-months] = CardBill(b0.account_id, b0.label, b0.month, b0.due, open_now + b0.paid_cents, b0.projected_cents,
                                    b0.paid_cents)
    return out


def monthly_summary(ledger: Ledger, cfg: Config, tax: Taxonomy, month: str, today: date,
                    owner: str | None = "pf") -> Summary:
    """Summary of one owner (default PF: what /resumo, /posso_comprar and the saldo livre use) plus, when a PJ
    account exists, the PF / PJ / Consolidado sections in `secoes`. Without PJ accounts, PF == everything."""
    v = owner_view(ledger, month, owner)
    s = Summary(month=month, today=today, owner=owner)
    s.receitas, s.despesas, s.investimentos = v.receitas, v.despesas, v.investimentos
    s.receitas_por_fonte, s.despesas_por_categoria = v.receitas_por_fonte, v.despesas_por_categoria
    s.aportes, s.mov_titular = v.aportes, v.mov_titular
    s.saldo, s.taxa_poupanca = v.saldo, v.taxa_poupanca
    if ledger.has_pj():
        s.secoes = {view.titular: view for view in (v if owner == "pf" else owner_view(ledger, month, "pf"),
                                                     owner_view(ledger, month, "pj"), owner_view(ledger, month, None))}
    future = [add_months(month, k) for k in range(1, HORIZON + 1)]
    s.parcelas = committed_installments(ledger, future, owner)
    s.renda_ref = income_reference(ledger, cfg, month, owner)
    s.media_variavel = average_variable(ledger, month, owner)
    s.fixos_total = max(sum(fc.valor_cents for fc in cfg.fixed_costs), average_fixed(ledger, month, owner))

    s.proximo_salario = next_salary_date(today, cfg.salary_day, salary_received(ledger, today))
    fixed = fixed_cost_status(ledger, cfg, ym(today), owner)
    s.fixos_pendentes = [(n, v, d) for n, v, d, paid in fixed if not paid]
    s.fixos_ate_salario = fixed_until(ledger, cfg, today, s.proximo_salario, owner)
    s.saldo_contas = v.saldo_contas
    if owner == "pf" and ledger.has_pj():
        # MEI without separated accounts: the PJ balance is yours too (only DAS/fees are the company's)
        s.saldo_contas += bank_balance(ledger, "pj")
    caju = ledger.one("SELECT COALESCE(SUM(amount_cents),0) AS v FROM transactions WHERE account_id='caju' AND removed_upstream=0")
    s.caju_saldo = caju["v"] if caju else 0
    s.faturas_ate_salario = sum(b.open_cents for b in card_bills(ledger, cfg, today, owner=owner)
                                if b.due and today <= b.due < s.proximo_salario)
    s.saldo_livre = s.saldo_contas - s.faturas_ate_salario - s.fixos_ate_salario
    s.pendentes = ledger.one("SELECT COUNT(*) AS n FROM transactions WHERE review_status='pendente' AND removed_upstream=0")["n"]
    return s


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%".replace(".", ",")


def _fontes(v: dict[str, int]) -> str:
    text = " · ".join(f"{k} {brl(x)}" for k, x in v.items())
    return f"  ({text})" if text else ""


def _section(v: OwnerView) -> list[str]:
    """One owner block of /resumo (PF, PJ or Consolidado)."""
    head = {"PF": "PF (pessoa física)", "PJ": "PJ (MEI)", "Consolidado": "Consolidado (sem PF↔PJ)"}[v.titular]
    lines = [head]
    lines.append(f"{'Faturamento' if v.titular == 'PJ' else 'Receitas':<15}{brl(v.receitas):>14}{_fontes(v.receitas_por_fonte)}")
    lines.append(f"Despesas       {brl(v.despesas):>14}")
    if v.investimentos:
        lines.append(f"Investimentos  {brl(v.investimentos):>14}")
    if v.titular == "PF" and v.aportes:
        lines.append(f"Aportes na PJ  {brl(-v.aportes):>14}")
    if v.titular == "PJ":
        if v.retiradas:
            lines.append(f"Retiradas p/ PF{brl(-v.retiradas):>14}")
        if v.aportes:
            lines.append(f"Aportes da PF  {brl(v.aportes):>14}")
    tail = "" if v.titular == "PJ" else f"   poupança {_pct(v.taxa_poupanca)}"
    lines.append(f"Saldo do mês   {brl(v.saldo):>14}{tail}")
    if v.titular != "Consolidado" and v.despesas_por_categoria:
        for cat, x in v.despesas_por_categoria.items():
            lines.append(f"  {cat:<18}{brl(x):>11}")
    if v.titular == "PJ":
        lines.append(f"Saldo na conta {brl(v.saldo_contas):>14}")
    return lines


def format_summary(s: Summary) -> str:
    lines = [f"Resumo {s.month} (dados até {s.today.strftime('%d/%m')})", ""]
    if s.secoes:
        for key in ("PF", "PJ", "Consolidado"):
            lines += _section(s.secoes[key]) + [""]
        lines.pop()
    else:
        fontes = " · ".join(f"{k} {brl(v)}" for k, v in s.receitas_por_fonte.items())
        lines.append(f"Receitas       {brl(s.receitas):>14}" + (f"  ({fontes})" if fontes else ""))
        lines.append(f"Despesas       {brl(s.despesas):>14}")
        lines.append(f"Investimentos  {brl(s.investimentos):>14}")
        lines.append(f"Saldo do mês   {brl(s.saldo):>14}   poupança {_pct(s.taxa_poupanca)}")
        if s.despesas_por_categoria:
            lines += ["", "Despesas por categoria"]
            for cat, v in s.despesas_por_categoria.items():
                lines.append(f"  {cat:<15}{brl(v):>14}")
    pf = " (PF)" if s.secoes else ""
    committed = {m: v for m, v in s.parcelas.items() if v}
    if committed:
        lines += ["", f"Parcelas já comprometidas{pf}"]
        for m, v in committed.items():
            pct = f" ({_pct(v / s.renda_ref)} da renda)" if s.renda_ref else ""
            lines.append(f"  {m}  {brl(v):>12}{pct}")
    if s.fixos_pendentes:
        lines += ["", f"Custos fixos pendentes no mês{pf}"]
        for nome, v, dia in s.fixos_pendentes:
            lines.append(f"  {nome:<15}{brl(v):>14}  dia {dia}")
    lines += [
        "",
        f"Saldo nas contas{pf:<10}{brl(s.saldo_contas):>14}",
        f"Faturas até o salário    -{brl(s.faturas_ate_salario):>14}",
        f"Fixos até o salário      -{brl(s.fixos_ate_salario):>14}",
        f"Saldo livre até {s.proximo_salario.strftime('%d/%m') if s.proximo_salario else '?'}{pf}  {brl(s.saldo_livre):>14}",
    ]
    if s.caju_saldo:
        lines.append(f"Caju (só alimentação)     {brl(s.caju_saldo):>14}")
    if s.pendentes:
        lines += ["", f"Pendentes de revisão: {s.pendentes} (/pendentes)"]
    return "\n".join(lines)


def format_bills(ledger: Ledger, cfg: Config, today: date) -> str:
    bills = card_bills(ledger, cfg, today, months=3)
    if not bills:
        return "Nenhum cartão no ledger."
    lines = []
    accounts = {a["id"]: a for a in ledger.q("SELECT * FROM accounts WHERE type='CREDIT'")}
    for acc_id in dict.fromkeys(b.account_id for b in bills):
        acc = accounts[acc_id]
        mine = [b for b in bills if b.account_id == acc_id]
        lines.append(mine[0].label)
        if acc["credit_limit_cents"]:
            lines.append(f"  limite {brl(acc['credit_limit_cents'])}  disponível {brl(acc['available_credit_cents'] or 0)}")
        for b in mine:
            due = b.due.strftime("%d/%m") if b.due else "?"
            status = "paga" if b.amount_cents and b.open_cents == 0 else "aberta"
            proj = f" (inclui {brl(b.projected_cents)} de parcelas projetadas)" if b.projected_cents else ""
            lines.append(f"  {b.month} vence {due}: {brl(b.amount_cents)} [{status}]{proj}")
        closed = ledger.q("SELECT * FROM bills WHERE account_id=? ORDER BY due_date DESC LIMIT 2", (acc_id,))
        for c in closed:
            lines.append(f"  fechada {c['due_date']}: {brl(c['total_cents'] or 0)} (mínimo {brl(c['minimum_cents'] or 0)})")
        if not closed and acc["balance_due_date"]:
            lines.append(f"  banco informa vencimento {acc['balance_due_date']}")
        lines.append("")
    return "\n".join(lines).strip()


def can_i_buy(s: Summary, value_cents: int, installments: int = 1) -> str:
    buffer = s.renda_ref // 10
    if installments <= 1:
        after = s.saldo_livre - value_cents
        verdict = "Sim" if after >= buffer else ("Apertado" if after >= 0 else "Não")
        return (f"{verdict}. Saldo livre até {s.proximo_salario.strftime('%d/%m')}: {brl(s.saldo_livre)}; "
                f"depois da compra sobra {brl(after)} (folga mínima sugerida {brl(buffer)}).")
    monthly = -(-value_cents // installments)
    worst_month, worst = None, None
    lines = []
    months = sorted(s.parcelas)[:installments] if s.parcelas else []
    for m in months:
        free = s.renda_ref - s.fixos_total - s.media_variavel - s.parcelas[m]
        lines.append(f"  {m}: livre {brl(free)} → {brl(free - monthly)}")
        if worst is None or free < worst:
            worst, worst_month = free, m
    if worst is None:
        return "Sem dados suficientes de renda/gastos para simular parcelado."
    after = worst - monthly
    verdict = "Sim" if after >= buffer else ("Apertado" if after >= 0 else "Não")
    head = (f"{verdict}. {installments}x de {brl(monthly)}. Pior mês {worst_month}: sobra {brl(after)} "
            f"(renda ref {brl(s.renda_ref)}, fixos {brl(s.fixos_total)}, variável médio {brl(s.media_variavel)}).")
    return head + "\n" + "\n".join(lines)


def aggregates_for_analyst(s: Summary) -> dict[str, Any]:
    """Only aggregates: what the optional LLM analyst may see (PF; PJ and consolidated totals when present)."""
    out = {
        "mes": s.month,
        "receitas": s.receitas / 100,
        "despesas": s.despesas / 100,
        "investimentos": s.investimentos / 100,
        "taxa_poupanca": round(s.taxa_poupanca, 3) if s.taxa_poupanca is not None else None,
        "despesas_por_categoria": {k: v / 100 for k, v in s.despesas_por_categoria.items()},
        "parcelas_proximos_meses": {k: v / 100 for k, v in s.parcelas.items()},
        "media_variavel_3m": s.media_variavel / 100,
    }
    for key, v in s.secoes.items():
        if key != "PF":
            out[key.lower()] = {"receitas": v.receitas / 100, "despesas": v.despesas / 100, "saldo": v.saldo / 100,
                                "despesas_por_categoria": {k: x / 100 for k, x in v.despesas_por_categoria.items()}}
    return out
