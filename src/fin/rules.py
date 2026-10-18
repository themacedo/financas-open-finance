"""Deterministic rules (code, never the LLM).

Order: single-row rules -> own-transfer pairing (also PF<->PJ) -> bank-side bill payment pairing ->
people (Pix/TED to a CPF) -> installment projection.
Rows you approved manually (review_status='aprovado') are never touched.

PF x PJ: the owner comes from the account (config pluggy.items[].owner). A transfer between your own
PF and PJ accounts stays Transferência (out of the consolidated numbers) but is labeled
"Retirada PJ→PF" (MEI profit distribution) or "Aporte PF→PJ"; reports.py uses the label per owner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fin.config import Config, Taxonomy
from fin.ledger import Ledger
from fin.privacy import doc_hash

TRANSFER_OPS = {"PIX", "TED", "DOC", "TRANSFERENCIA_MESMA_INSTITUICAO"}
TRANSFER_METHODS = {"PIX", "TED", "DOC", "TEV"}
FEE_OPS = {"TARIFA", "OPERACOES_CREDITO_CONTRATADAS_CARTAO", "PACOTE_TARIFA_SERVICOS",
           "TARIFA_SERVICOS_AVULSOS", "ENCARGOS_JUROS_CHEQUE_ESPECIAL"}
INVEST_OPS = {"RENDIMENTO_APLIC_FINANCEIRA", "RESGATE_APLIC_FINANCEIRA"}
SALARY_OPS = {"FOLHA_PAGAMENTO", "PORTABILIDADE_SALARIO"}

RE_CARD_PAYMENT = re.compile(r"pagamento (recebido|de fatura|efetuado)", re.I)
# Bank-side card bill payment. "fatura" alone is not enough (utility bills: "PAGAMENTO FATURA VIVO").
RE_BANK_BILL = re.compile(
    r"\bfatura\b.*\b(cart[aã]o|card|cr[eé]dito|nubank|nu pagamentos|itaucard|santander|mastercard|visa)\b"
    r"|\b(cart[aã]o|card|cr[eé]dito)\b.*\bfatura\b"
    r"|\bpag(to|amento)?\s+(de\s+)?cart[aã]o\b|\bnu pagamentos\b", re.I)
# Card statement line that carries the unpaid balance of the previous bill (partial payment / revolving):
# it is not a new expense (the purchases were already counted).
RE_CARRYOVER = re.compile(r"saldo (restante|anterior|em atraso|em rotativo)|fatura anterior|restante da fatura|saldo devedor anterior", re.I)
# Transfer-looking description (used when Open Finance payment data is missing).
RE_TRANSFER_DESC = re.compile(r"^(PIX|TED|DOC|TEV|TRANSF\w*)\b|\bPIX\b")
# Plain "PAGAMENTO DE FATURA" on the checking account of a bank whose card you also have (Nubank pays its own card).
RE_SAME_BANK_BILL = re.compile(r"^pagamento de fatura\b", re.I)
RE_DEBT_PAYMENT = re.compile(r"renegocia|emprestimo|empr[eé]stimo|d[ií]vida|parcelamento de fatura", re.I)
RE_CARD_LIMIT_TO_ACCOUNT = re.compile(
    r"valor adicionado (na conta )?(por|para) (cart[aã]o|pix no cr[eé]dito)|limite convertido em saldo|saldo adicionado na sua conta",
    re.I)
RE_REFUND = re.compile(r"estorno|reembolso|cancelamento|devolu", re.I)
RE_FEE = re.compile(r"\biof\b|juros|anuidade|multa|encargo|tarifa", re.I)
RE_INVEST = re.compile(r"aplica[cç][aã]o|resgate|rendimento|\bcdb\b|tesouro|poupan[cç]a|caixinha", re.I)

RULE_SOURCES = ("rule", "counterpart")
RETIRADA, APORTE = "Retirada PJ→PF", "Aporte PF→PJ"
OWNER_TRANSFER_CATS = (RETIRADA, APORTE)
FEE_CAT = {"pf": "Impostos/Taxas", "pj": "Taxas bancárias PJ"}


def owner_transfer_label(src_owner: str | None, dst_owner: str | None) -> str:
    """Category of a transfer between your own accounts, by who sends and who receives."""
    if not src_owner or not dst_owner or src_owner == dst_owner:
        return "Transferência"
    return RETIRADA if src_owner == "pj" else APORTE


@dataclass
class Decision:
    natureza: str | None = None
    categoria: str | None = None
    necessidade: str | None = None
    fonte: str | None = None
    rule: str | None = None
    pair_id: str | None = None
    review: str = "auto"
    cat_source: str | None = "rule"
    alias: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


CARD_PAYMENT_DUP = "card_payment_duplicate"


def pair_id(a: int, b: int) -> str:
    lo, hi = sorted((a, b))
    return "P" + hashlib.sha256(f"{lo}-{hi}".encode()).hexdigest()[:10]


def add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def card_competence(day: date, closing_day: int, due_day: int) -> str:
    """Month of the bill a card purchase falls into (used when billForecastDate is absent)."""
    ym = f"{day.year:04d}-{day.month:02d}"
    if not closing_day:
        return add_months(ym, 1)
    close_ym = ym if day.day < closing_day else add_months(ym, 1)
    return close_ym if due_day > closing_day else add_months(close_ym, 1)


def _single_row(row: Any, acc_type: str, cfg: Config, tax: Taxonomy, income_by_cnpj: dict,
                owner: str = "pf") -> Decision | None:
    op = row["operation_type"] or ""
    fee_cat = FEE_CAT.get(owner, "Impostos/Taxas")
    desc = row["description"] or ""
    amt = row["amount_cents"]
    if acc_type == "CREDIT":
        if op == "PAGAMENTO_FATURA" or (not op and amt > 0 and RE_CARD_PAYMENT.search(desc)):
            return Decision("Transferência", "Transferência", "variavel", rule="card_bill_payment")
        if op == "CASHBACK":
            return Decision("Receita", "Outros", "variavel", fonte="Pessoal", rule="cashback")
        if amt > 0 and (op == "ESTORNO" or RE_REFUND.search(desc)):
            # category inherited from the original purchase in categorize.py
            return Decision("Despesa", None, rule="refund", cat_source=None)
        if amt < 0 and RE_CARRYOVER.search(desc):
            return Decision("Transferência", "Transferência", "variavel", rule="card_carryover")
        if op == "OPERACOES_CREDITO_CONTRATADAS_CARTAO" and not RE_FEE.search(desc):
            # Pix/boleto paid with the card ("Pix no crédito"): a purchase, not a fee; often to a person
            return Decision("Despesa", "Revisar", "variavel", rule="card_credit_op", review="pendente")
        if op in FEE_OPS or (amt < 0 and op in ("", "OUTROS") and RE_FEE.search(desc)):
            return Decision("Despesa", fee_cat, "variavel", rule="fee")
        if amt > 0:
            # money in on a card is never income: a bill payment (paired below with the bank side) or an
            # adjustment/chargeback credit (negative expense) you confirm
            return Decision("Despesa", "Revisar", "variavel", rule="card_credit", review="pendente")
        return None
    # bank accounts
    if RE_CARD_LIMIT_TO_ACCOUNT.search(desc):
        # "Pix no crédito": the card limit lands in your account first (your own money moving, the debt is on the card)
        return Decision("Transferência", "Transferência", "variavel", rule="card_limit_to_account")
    if amt < 0 and op == "OPERACAO_CREDITO" and RE_DEBT_PAYMENT.search(desc):
        # paying back a loan/renegotiated card debt: the purchases were already counted on the card
        return Decision("Transferência", "Transferência", "variavel", rule="debt_payment")
    if op in FEE_OPS:
        return Decision("Despesa", fee_cat, "variavel", rule="fee")
    if op in INVEST_OPS or (op in ("", "OUTROS") and RE_INVEST.search(desc)):
        return Decision("Investimento", "Investimento", "variavel", rule="investment")
    if op in SALARY_OPS and amt > 0:
        src = income_by_cnpj.get(row["counterpart_cnpj"] or "")
        fonte = src.get("fonte") if src else None
        return Decision("Receita", (src or {}).get("categoria") or "Salário CLT", "fixo", fonte=fonte,
                        rule="salary", review="auto" if fonte else "pendente")
    if row["counterpart_kind"] == "own":
        return Decision("Transferência", "Transferência", "variavel", rule="own_transfer")
    if amt > 0 and row["counterpart_cnpj"] and row["counterpart_cnpj"] in income_by_cnpj:
        src = income_by_cnpj[row["counterpart_cnpj"]]
        cat = tax.canonical(src.get("categoria")) or "PJ"
        if owner == "pj" and cat == "PJ" and "Faturamento PJ" in tax.categories:
            cat = "Faturamento PJ"  # client paying into the company account
        return Decision("Receita", cat, tax.necessidade_of(cat), fonte=src.get("fonte"), rule="income_source")
    return None


def _is_transfer_like(row: Any) -> bool:
    if (row["operation_type"] in TRANSFER_OPS) or (row["payment_method"] in TRANSFER_METHODS):
        return True
    from fin.normalize import fold

    return RE_TRANSFER_DESC.search(fold(row["description"] or "")) is not None


def person_key(row: Any, salt: str) -> tuple[str, bool]:
    """Counterpart key for a transfer to/from a person, and whether it may be learned.

    Document hash when known; otherwise the name in the description (2+ meaningful words). A generic
    description ("PIX ENVIADO") gets a per-transaction key: nothing is shared or learned from it.
    """
    from fin.normalize import GENERIC_WORDS

    if row["counterpart_hash"]:
        return row["counterpart_hash"], True
    words = [w for w in (row["desc_norm"] or "").split() if w not in GENERIC_WORDS]
    if len(words) >= 2:
        return doc_hash("desc|" + " ".join(words), salt), True
    return doc_hash(f"tx|{row['local_id']}", salt), False


def apply_all(ledger: Ledger, cfg: Config, tax: Taxonomy, today: date | None = None) -> dict[str, int]:
    accounts = {a["id"]: a for a in ledger.accounts()}
    rows = [r for r in ledger.active_txs() if r["review_status"] != "aprovado"]
    approved_pairs = {r["pair_id"] for r in ledger.active_txs("review_status='aprovado' AND pair_id IS NOT NULL")}
    income_by_cnpj = {"".join(c for c in str(s.get("cnpj", "")) if c.isdigit()): s for s in cfg.income_sources}
    decisions: dict[int, Decision] = {}
    acc_type = lambda r: (accounts.get(r["account_id"]) or {"type": "MANUAL"})["type"]
    owners = {aid: a["owner"] or "pf" for aid, a in accounts.items()}
    owner_of = lambda r: owners.get(r["account_id"], "pf")
    partner: dict[int, int] = {}

    # 1) single-row rules
    for r in rows:
        if r["source"] == "manual":
            continue
        d = _single_row(r, acc_type(r), cfg, tax, income_by_cnpj, owner_of(r))
        if d is None and _matches_extra_own(r, cfg, ledger.salt):
            d = Decision("Transferência", "Transferência", "variavel", rule="own_transfer_extra")
        if d:
            decisions[r["local_id"]] = d

    # 2) own transfers between connected bank accounts: same |amount|, dates within 2 days
    cands = [r for r in rows if acc_type(r) == "BANK" and r["source"] == "pluggy"
             and r["counterpart_kind"] in ("own", "unknown", "none")
             and (_is_transfer_like(r) or r["counterpart_kind"] == "own")
             and (r["local_id"] not in decisions or decisions[r["local_id"]].rule == "own_transfer")]
    outs = [r for r in cands if r["amount_cents"] < 0]
    ins = [r for r in cands if r["amount_cents"] > 0]
    used: set[int] = set()
    pairs = []
    for o in outs:
        for i in ins:
            if i["account_local_key"] == o["account_local_key"] or i["amount_cents"] != -o["amount_cents"]:
                continue
            gap = abs((_d(i["date"]) - _d(o["date"])).days)
            if gap <= 2:
                pairs.append((gap, o["local_id"], i["local_id"]))
    by_lid = {r["local_id"]: r for r in cands}
    for _gap, a, b in sorted(pairs):
        if a in used or b in used:
            continue
        used |= {a, b}
        partner[a], partner[b] = b, a
        pid = pair_id(a, b)
        # same value on both sides is not proof: without one side known as yours, it is only a suggestion
        sure = "own" in (by_lid[a]["counterpart_kind"], by_lid[b]["counterpart_kind"])
        for lid in (a, b):
            decisions[lid] = Decision("Transferência", "Transferência", "variavel", rule="own_transfer", pair_id=pid,
                                      review="auto" if sure else "pendente")

    # 2b) PF<->PJ label: paired -> by the other side's owner; unpaired -> by whose document/account it went to
    cp_owner = _counterpart_owners(accounts.values())
    by_any = {r["local_id"]: r for r in rows}
    for lid, d in decisions.items():
        if d.rule != "own_transfer":
            continue
        r = by_any[lid]
        other = owner_of(by_any[partner[lid]]) if lid in partner else (
            cp_owner.get(("t", r["counterpart_transfer_key"])) or cp_owner.get(("d", r["counterpart_hash"])))
        src, dst = (owner_of(r), other) if r["amount_cents"] < 0 else (other, owner_of(r))
        d.categoria = owner_transfer_label(src, dst)

    # 3) bank-side card bill payment, paired with the card-side payment or bills.payments[]
    issuer_cnpjs = {c.issuer_cnpj for c in cfg.cards.values() if c.issuer_cnpj}
    card_banks = {str(a["local_key"]).split(":")[0] for a in ledger.q("SELECT * FROM accounts") if a["type"] == "CREDIT"}
    card_side = [r for r in rows if decisions.get(r["local_id"])
                 and decisions[r["local_id"]].rule in ("card_bill_payment", "card_credit")]
    bill_payments = []
    for b in ledger.q("SELECT * FROM bills"):
        for p in json.loads(b["payments_json"] or "[]"):
            if p.get("amount") is not None and p.get("paymentDate"):
                from fin.money import to_cents

                bill_payments.append((to_cents(p["amount"]), _d(p["paymentDate"]), b["id"], len(bill_payments)))
    used_card: set[int] = set()
    used_bp: set[int] = set()
    for r in rows:
        if acc_type(r) != "BANK" or r["amount_cents"] >= 0 or r["local_id"] in used:
            continue
        desc_r = r["description"] or ""
        same_bank = RE_SAME_BANK_BILL.search(desc_r) and str(r["account_local_key"]).split(":")[0] in card_banks
        if not ((r["counterpart_cnpj"] and r["counterpart_cnpj"] in issuer_cnpjs) or RE_BANK_BILL.search(desc_r) or same_bank):
            continue
        amount = -r["amount_cents"]
        match = None
        for c in card_side:
            if c["local_id"] in used_card:
                continue
            if abs(c["amount_cents"] - amount) <= 1 and abs((_d(c["date"]) - _d(r["date"])).days) <= 3:
                match = c
                break
        if match is not None:
            used_card.add(match["local_id"])
            pid = pair_id(r["local_id"], match["local_id"])
            decisions[r["local_id"]] = Decision("Transferência", "Transferência", "variavel", rule="bank_bill_payment", pair_id=pid)
            decisions[match["local_id"]] = Decision("Transferência", "Transferência", "variavel", rule="card_bill_payment", pair_id=pid)
            continue
        bp = next((p for p in bill_payments if p[3] not in used_bp
                   and abs(p[0] - amount) <= 1 and abs((p[1] - _d(r["date"])).days) <= 3), None)
        if bp is not None:
            used_bp.add(bp[3])
            decisions[r["local_id"]] = Decision("Transferência", "Transferência", "variavel", rule="bank_bill_payment",
                                                pair_id="B" + bp[2][:10])
        else:
            decisions[r["local_id"]] = Decision("Transferência", "Revisar", "variavel", rule="bill_payment_unpaired", review="pendente")

    # 3b) the same card payment listed under two bills: an unpaired card-side copy of a paired payment (same card, date
    # and amount) is the aggregator's duplicate; kept, but out of every balance
    paired = {(c["account_id"], c["date"], c["amount_cents"]) for c in card_side if c["local_id"] in used_card}
    for c in card_side:
        if (c["local_id"] not in used_card and decisions[c["local_id"]].rule == "card_bill_payment"
                and (c["account_id"], c["date"], c["amount_cents"]) in paired):
            decisions[c["local_id"]] = Decision("Transferência", "Transferência", "variavel", rule=CARD_PAYMENT_DUP)

    # 4) people: Pix/TED to/from a CPF that is not yours, or transfer without counterpart data
    for r in rows:
        if r["local_id"] in decisions or r["source"] != "pluggy":
            continue
        kind = r["counterpart_kind"]
        if acc_type(r) == "BANK":
            is_person = kind == "person" or (kind in ("unknown", "none") and _is_transfer_like(r))
        elif acc_type(r) == "CREDIT":  # "Pix no crédito" to a person
            is_person = kind != "company" and r["amount_cents"] < 0 and _is_transfer_like(r)
        else:
            is_person = False
        if not is_person:
            continue
        key, learnable = person_key(r, ledger.salt)
        alias = ledger.counterpart_alias(key)
        cp = ledger.counterpart(alias) if learnable else None
        if cp and cp["categoria"]:
            cat = cp["categoria"]
            decisions[r["local_id"]] = Decision(cp["natureza"] or tax.natureza_of(cat), cat,
                                                cp["necessidade"] or tax.necessidade_of(cat), fonte=cp["fonte"],
                                                rule="person", cat_source="counterpart", alias=alias,
                                                extra={"essencial": cp["essencial"]})
        else:
            natureza = "Despesa" if r["amount_cents"] < 0 else "Receita"
            decisions[r["local_id"]] = Decision(natureza, "Revisar", "variavel", rule="person",
                                                cat_source="counterpart", review="pendente", alias=alias)

    # write
    changed = 0
    with ledger.tx():
        for r in rows:
            d = decisions.get(r["local_id"])
            if d is None:
                if r["cat_source"] in RULE_SOURCES:
                    ledger.update_tx(r["local_id"], {"natureza": None, "categoria": None, "necessidade": None,
                                                     "fonte": None, "rule": None, "pair_id": None,
                                                     "cat_source": None, "review_status": "auto"})
                    changed += 1
                elif r["rule"] or r["pair_id"]:
                    ledger.update_tx(r["local_id"], {"rule": None, "pair_id": None})
                    changed += 1
                continue
            if r["pair_id"] in approved_pairs:
                d.pair_id = r["pair_id"]
            fields: dict[str, Any] = {"rule": d.rule, "pair_id": d.pair_id, "natureza": d.natureza}
            if d.alias:
                fields["counterpart_alias"] = d.alias
            if d.categoria is not None:
                fields.update(categoria=d.categoria, necessidade=d.necessidade, fonte=d.fonte,
                              cat_source=d.cat_source, review_status=d.review, confidence=None)
                if "essencial" in d.extra:
                    fields["essencial"] = d.extra["essencial"]
            elif r["cat_source"] in RULE_SOURCES and r["rule"] != d.rule:
                fields.update(categoria=None, cat_source=None, review_status="auto")
            if any(r[k] != v for k, v in fields.items()):
                ledger.update_tx(r["local_id"], fields)
                changed += 1
    stats = {"rules_changed": changed}
    ledger.set_meta("installment_cutoffs", json.dumps(open_bill_months(ledger, cfg, today or date.today())))
    stats.update(project_installments(ledger))
    return stats


def _counterpart_owners(accounts: Any) -> dict[tuple[str, str], str]:
    """("t", transfer_key) / ("d", owner_doc_hash) -> owner, for your bank accounts. A document shared by PF and PJ
    accounts (MEI account opened with the CPF) says nothing: only the account (transfer key) decides then."""
    out: dict[tuple[str, str], str] = {}
    docs: dict[str, set[str]] = {}
    for a in accounts:
        if a["type"] != "BANK":
            continue
        owner = a["owner"] or "pf"
        if a["transfer_key"]:
            out[("t", a["transfer_key"])] = owner
        if a["owner_doc_hash"]:
            docs.setdefault(a["owner_doc_hash"], set()).add(owner)
    out.update({("d", h): next(iter(o)) for h, o in docs.items() if len(o) == 1})
    return out


def open_bill_months(ledger: Ledger, cfg: Config, today: date) -> dict[str, str]:
    """Per card account: the first bill month still open. Earlier bills are closed: a projected
    installment there that never arrived (cancelled, refunded) is dropped."""
    out = {}
    for acc in ledger.q("SELECT id, bank FROM accounts WHERE type='CREDIT'"):
        card = cfg.card(acc["bank"])
        if card.closing_day:
            out[acc["id"]] = card_competence(today, card.closing_day, card.due_day)
        else:  # closing day unknown: only past months are surely closed
            out[acc["id"]] = f"{today.year:04d}-{today.month:02d}"
    return out


def _matches_extra_own(row: Any, cfg: Config, salt: str) -> bool:
    """Pix/TED to one of your accounts outside Pluggy (config extra_own_accounts)."""
    if not cfg.extra_own_accounts or row["amount_cents"] >= 0 or not row["counterpart_transfer_key"]:
        return False
    keys = {transfer_key(str(a.get("compe", "")), str(a.get("account", "")), salt) for a in cfg.extra_own_accounts}
    return row["counterpart_transfer_key"] in keys


def transfer_key(compe: str, account: str, salt: str) -> str | None:
    acct = "".join(c for c in account if c.isdigit()).lstrip("0")
    compe = "".join(c for c in compe if c.isdigit()).lstrip("0")
    if not acct or not compe:
        return None
    return doc_hash(f"acct|{compe}|{acct}", salt)


def installment_base(competence: str, n: int) -> str:
    return add_months(competence, -(n - 1))


def project_installments(ledger: Ledger, cutoffs: dict[str, str] | None = None) -> dict[str, int]:
    """Rebuild installments_projection (derived table) from the card installments in the ledger.

    Not projected: months whose bill is already closed (see open_bill_months, saved by apply_all) and the
    rest of a purchase refunded at once (refund of the same merchant ~ remaining value)."""
    from fin.categorize import refund_base

    if cutoffs is None:
        cutoffs = json.loads(ledger.meta("installment_cutoffs") or "{}")
    refunds = [r for r in ledger.active_txs("amount_cents > 0 AND rule='refund'")]
    rows = ledger.active_txs(
        "total_installments > 1 AND installment_number IS NOT NULL AND account_id IN (SELECT id FROM accounts WHERE type='CREDIT')"
    )
    groups: dict[tuple, list[list[Any]]] = {}
    for r in rows:
        key = (r["account_local_key"], r["desc_norm"], r["total_installments"],
               installment_base(r["competence"], r["installment_number"]))
        clusters = groups.setdefault(key, [])
        for cl in clusters:
            ref = cl[0]["amount_cents"]
            if abs(r["amount_cents"] - ref) <= max(5, abs(ref) // 100):
                cl.append(r)
                break
        else:
            clusters.append([r])
    projections = []
    for (alk, desc, total, base), clusters in groups.items():
        for idx, cl in enumerate(clusters):
            known = {r["installment_number"]: r for r in cl}
            last_n = max(known)
            last = known[last_n]
            gkey = hashlib.sha256(f"{alk}|{desc}|{total}|{base}|{idx}".encode()).hexdigest()[:16]
            remaining = -last["amount_cents"] * (total - last_n)
            full = -last["amount_cents"] * total
            first_day = min(r["date"] for r in cl)
            if remaining > 0 and any(
                    f["account_local_key"] == alk and refund_base(f["desc_norm"]) == desc and f["date"] >= first_day
                    and remaining * 95 // 100 <= f["amount_cents"] <= full * 105 // 100 for f in refunds):
                continue  # rest of the purchase refunded
            cutoff = cutoffs.get(last["account_id"], "")
            for n in range(last_n + 1, total + 1):
                month = add_months(base, n - 1)
                if month < cutoff:
                    continue  # bill closed and the real installment never came
                projections.append((gkey, last["account_id"], desc, n, total, month,
                                    last["amount_cents"], last["natureza"] or "Despesa", last["categoria"]))
    with ledger.tx():
        ledger.x("DELETE FROM installments_projection")
        ledger.conn.executemany(
            "INSERT INTO installments_projection(group_key,account_id,desc_norm,installment_number,total_installments,"
            "month,amount_cents,natureza,categoria) VALUES(?,?,?,?,?,?,?,?,?)", projections)
    return {"installment_groups": sum(len(c) for c in groups.values()), "installments_projected": len(projections)}

