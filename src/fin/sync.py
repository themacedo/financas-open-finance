"""Daily sync: Pluggy -> ledger (idempotent upsert with identity resolution).

Pluggy transaction ids are NOT stable (recreated when date/description/amount
change, or when a bank is reconnected). Matching order for an incoming row:
  1. same pluggy id                       -> update
  2. same providerId in the same account  -> relink (keeps category/approval)
  3. same natural_key, old id absent      -> relink
  4. same account+desc+amount (+parcela), date within 4 days, old id absent -> relink
  5. otherwise insert
Rows that disappear from the window are soft-flagged; after N syncs they become
removed_upstream (kept in the ledger, out of the numbers). Nothing is hard-deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, UTC
from typing import Any

import httpx

from fin.config import Config, Taxonomy
from fin.ledger import Ledger, scrub_raw
from fin.money import to_cents
from fin.normalize import digits, normalize_description
from fin.pluggy import PluggyAPI, PluggyError
from fin.privacy import doc_hash
from fin.rules import add_months, apply_all, card_competence, transfer_key

log = logging.getLogger("fin.sync")

DATA_FIELDS = (
    "pluggy_id", "provider_id", "natural_key", "account_id", "account_local_key", "date", "competence",
    "amount_cents", "currency", "orig_amount_cents", "orig_currency", "direction", "status", "description",
    "desc_norm", "operation_type", "payment_method", "counterpart_kind", "counterpart_hash",
    "counterpart_cnpj", "counterpart_transfer_key", "installment_number", "total_installments",
    "purchase_date", "bill_id", "bill_forecast", "sign_mismatch", "raw_json",
)
try:
    from zoneinfo import ZoneInfo

    BRT: Any = ZoneInfo("America/Sao_Paulo")
except Exception:  # noqa: BLE001 - no tz database: Brazil has no DST since 2019
    BRT = timezone(timedelta(hours=-3))
BANK_NAMES = {"itau": "Itaú", "nubank": "Nubank", "santander": "Santander", "caju": "Caju", "nubank_pj": "Nubank PJ"}


@dataclass
class SyncResult:
    status: str = "success"
    alerts: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def account_local_key(bank: str, subtype: str | None, last4: str, owner: str = "pf") -> str:
    """bank:subtype:last4 (PF, unchanged since v1) or bank:pj:subtype:last4: Nubank PF and Nubank PJ never collide."""
    return f"{bank}:{subtype}:{last4}" if owner == "pf" else f"{bank}:{owner}:{subtype}:{last4}"


def parse_account(acc: dict[str, Any], bank: str, salt: str, owner: str = "pf") -> dict[str, Any]:
    last4 = digits(acc.get("number"))[-4:]
    subtype = acc.get("subtype") or acc.get("type")
    tax = digits(acc.get("taxNumber"))
    owner_hash = doc_hash(tax, salt) if len(tax) in (11, 14) else None
    tkey = None
    bank_data = acc.get("bankData") or {}
    if bank_data.get("transferNumber"):
        parts = str(bank_data["transferNumber"]).split("/")
        if len(parts) >= 3:
            tkey = transfer_key(parts[0], parts[-1], salt)
    credit = acc.get("creditData") or {}
    return {
        "id": acc["id"],
        "item_id": acc.get("itemId") or "",
        "bank": bank,
        "type": acc.get("type") or "BANK",
        "subtype": subtype,
        "local_key": account_local_key(bank, subtype, last4, owner),
        "owner": owner,
        "name": acc.get("marketingName") or acc.get("name"),
        "last4": last4,
        "owner_doc_hash": owner_hash,
        "transfer_key": tkey,
        "balance_cents": to_cents(acc["balance"]) if acc.get("balance") is not None else None,
        "credit_limit_cents": to_cents(credit["creditLimit"]) if credit.get("creditLimit") is not None else None,
        "available_credit_cents": to_cents(credit["availableCreditLimit"]) if credit.get("availableCreditLimit") is not None else None,
        "balance_close_date": (credit.get("balanceCloseDate") or None),
        "balance_due_date": (credit.get("balanceDueDate") or None),
        "minimum_payment_cents": to_cents(credit["minimumPayment"]) if credit.get("minimumPayment") is not None else None,
    }


def parse_transaction(t: dict[str, Any], account: Any, salt: str, own_docs: set[str], own_tkeys: set[str],
                      cfg: Config) -> dict[str, Any]:
    is_card = account["type"] == "CREDIT"
    raw_amount = t["amountInAccountCurrency"] if t.get("amountInAccountCurrency") is not None else t["amount"]
    cents = to_cents(raw_amount)
    guessed = -cents if is_card else cents  # sign convention seen in the examples (card: + = purchase)
    ttype = t.get("type")
    if ttype in ("DEBIT", "CREDIT"):
        # `type` is the explicit direction: it wins over the sign convention (net-worth view: debit = money out)
        amount = -abs(cents) if ttype == "DEBIT" else abs(cents)
        direction = ttype
    else:
        amount = guessed
        direction = "DEBIT" if amount < 0 else "CREDIT"
    mismatch = int(cents != 0 and ttype in ("DEBIT", "CREDIT") and guessed != amount)
    day = local_day(t["date"])
    ccm = t.get("creditCardMetadata") or {}
    desc = t.get("description") or t.get("descriptionRaw") or ""
    pd = t.get("paymentData") if isinstance(t.get("paymentData"), dict) else None

    kind, cp_hash, cp_cnpj, cp_tkey, method = "none", None, None, None, None
    if pd:
        method = pd.get("paymentMethod")
        party = (pd.get("receiver") if amount < 0 else pd.get("payer")) or {}
        doc = digits((party.get("documentNumber") or {}).get("value"))
        cp_tkey = transfer_key(str(party.get("routingNumber") or ""), str(party.get("accountNumber") or ""), salt)
        if len(doc) == 11:
            cp_hash = doc_hash(doc, salt)
            kind = "own" if cp_hash in own_docs else "person"
        elif len(doc) == 14:
            cp_cnpj = doc
            cp_hash = doc_hash(doc, salt)
            kind = "own" if cp_hash in own_docs else "company"
        else:
            kind = "unknown"
        if cp_tkey and cp_tkey in own_tkeys:
            kind = "own"

    if is_card:
        forecast = ccm.get("billForecastDate")
        if forecast:
            competence = str(forecast)[:7]
        else:
            card = cfg.card(account["bank"])
            competence = card_competence(date.fromisoformat(day), card.closing_day, card.due_day)
            n = ccm.get("installmentNumber")
            purchase = str(ccm.get("purchaseDate") or "")[:10]
            if n and int(n) > 1 and purchase and day <= purchase:
                # some banks send every installment with the purchase date: installment n is n-1 bills later
                competence = add_months(card_competence(date.fromisoformat(purchase), card.closing_day, card.due_day),
                                        int(n) - 1)
    else:
        competence = day[:7]

    orig_cents = orig_ccy = None
    if t.get("amountInAccountCurrency") is not None and t.get("currencyCode") not in (None, "BRL"):
        orig_cents, orig_ccy = to_cents(t["amount"]), t["currencyCode"]

    return {
        "pluggy_id": t["id"],
        "provider_id": t.get("providerId"),
        "account_id": account["id"],
        "account_local_key": account["local_key"],
        "source": "pluggy",
        "date": day,
        "competence": competence,
        "amount_cents": amount,
        "currency": "BRL",
        "orig_amount_cents": orig_cents,
        "orig_currency": orig_ccy,
        "direction": direction,
        "status": t.get("status") or "POSTED",
        "description": desc,
        "desc_norm": normalize_description(desc),
        "operation_type": t.get("operationType"),
        "payment_method": method,
        "counterpart_kind": kind,
        "counterpart_hash": cp_hash,
        "counterpart_cnpj": cp_cnpj,
        "counterpart_transfer_key": cp_tkey,
        "installment_number": ccm.get("installmentNumber"),
        "total_installments": ccm.get("totalInstallments"),
        "purchase_date": str(ccm["purchaseDate"])[:10] if ccm.get("purchaseDate") else None,
        "bill_id": ccm.get("billId"),
        "bill_forecast": str(ccm["billForecastDate"])[:7] if ccm.get("billForecastDate") else None,
        "sign_mismatch": mismatch,
        "raw_json": json.dumps(scrub_raw(t), ensure_ascii=False, sort_keys=True),
        "_order": t.get("order") or 0,
    }


def local_day(raw: Any) -> str:
    """Calendar day in Brasília. Midnight UTC means "date only" (kept as is); any other time is converted."""
    text = str(raw)
    if len(text) <= 10 or text[11:19] in ("", "00:00:00"):
        return text[:10]
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    if ts.tzinfo is None:
        return text[:10]
    return ts.astimezone(BRT).date().isoformat()


def drop_reissued_card_payments(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The Nubank card (since 16/10/2026) returns each bill payment twice: the original, with the exact time, and a
    copy attached to another bill with the date only (03:00Z = midnight in Brasília) and a new id. Both keep coming,
    so identity resolution never pairs them. The original stays; one date-only copy per original is dropped."""
    def is_payment(t: dict[str, Any]) -> bool:
        return t.get("operationType") == "PAGAMENTO_FATURA"

    def date_only(t: dict[str, Any]) -> bool:
        return str(t.get("date"))[11:23] in ("", "00:00:00.000", "03:00:00.000")

    originals = Counter((local_day(t["date"]), str(t.get("amount"))) for t in raw if is_payment(t) and not date_only(t))
    kept: list[dict[str, Any]] = []
    dropped = 0
    for t in raw:
        key = (local_day(t["date"]), str(t.get("amount")))
        if is_payment(t) and date_only(t) and originals[key] > 0:
            originals[key] -= 1
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


def assign_natural_keys(rows: list[dict[str, Any]]) -> None:
    rows.sort(key=lambda r: (r["account_local_key"], r["date"], r["amount_cents"], r["desc_norm"], r["_order"], r["pluggy_id"]))
    seen: dict[tuple, int] = defaultdict(int)
    for r in rows:
        k = (r["account_local_key"], r["date"], r["amount_cents"], r["desc_norm"])
        occ = seen[k]
        seen[k] += 1
        r["natural_key"] = hashlib.sha256("|".join(map(str, (*k, occ))).encode()).hexdigest()[:32]


def upsert_account_transactions(ledger: Ledger, account: Any, incoming: list[dict[str, Any]], *,
                                window_start: str | None, today: str, missing_limit: int) -> dict[str, int]:
    stats = {"inserted": 0, "updated": 0, "relinked": 0, "missing": 0, "removed": 0}
    assign_natural_keys(incoming)
    alk = account["local_key"]
    existing = ledger.q("SELECT * FROM transactions WHERE account_local_key=? AND source='pluggy'", (alk,))
    by_pid = {r["pluggy_id"]: r for r in existing}
    incoming_ids = {r["pluggy_id"] for r in incoming}
    orphans = [r for r in existing if r["pluggy_id"] not in incoming_ids]
    claimed: set[int] = set()
    seen: set[int] = set()

    def find_orphan(pred) -> Any:
        for o in orphans:
            if o["local_id"] not in claimed and pred(o):
                return o
        return None

    with ledger.tx():
        for row in incoming:
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            current = by_pid.get(row["pluggy_id"])
            relinked = False
            if current is None and row["provider_id"]:
                current = find_orphan(lambda o: o["provider_id"] == row["provider_id"])
                relinked = current is not None
            if current is None:
                current = find_orphan(lambda o: o["natural_key"] == row["natural_key"])
                relinked = current is not None
            if current is None:
                current = find_orphan(lambda o: o["desc_norm"] == row["desc_norm"]
                                      and o["amount_cents"] == row["amount_cents"]
                                      and o["installment_number"] == row["installment_number"]
                                      and abs((date.fromisoformat(o["date"]) - date.fromisoformat(row["date"])).days) <= 4)
                relinked = current is not None
            if current is None and row["status"] == "POSTED":
                # pre-authorization (PENDING) replaced by the posted row with a new id and a slightly different amount
                current = find_orphan(lambda o: o["status"] == "PENDING" and o["desc_norm"] == row["desc_norm"]
                                      and (o["amount_cents"] < 0) == (row["amount_cents"] < 0)
                                      and abs(o["amount_cents"] - row["amount_cents"]) <= abs(o["amount_cents"]) // 5
                                      and abs((date.fromisoformat(o["date"]) - date.fromisoformat(row["date"])).days) <= 4)
                relinked = current is not None
            if current is None:
                lid = ledger.insert_tx(row)
                stats["inserted"] += 1
                seen.add(lid)
                continue
            claimed.add(current["local_id"])
            seen.add(current["local_id"])
            changes = {k: row[k] for k in DATA_FIELDS if current[k] != row[k]}
            if current["missing_count"] or current["missing_since"] or current["removed_upstream"]:
                changes.update(missing_count=0, missing_since=None, removed_upstream=0)
            material = {"desc_norm", "amount_cents"} & set(changes)
            if material and current["review_status"] != "aprovado" and current["cat_source"] in ("cache", "llm", "dictionary", "blocked", "none"):
                changes.update(cat_source=None, categoria=None, review_status="auto", confidence=None)
            if changes:
                ledger.update_tx(current["local_id"], changes)
                stats["relinked" if relinked else "updated"] += 1

        if window_start:
            for r in existing:
                if r["local_id"] in seen or r["date"] < window_start or r["removed_upstream"]:
                    continue
                count = r["missing_count"] + 1
                fields: dict[str, Any] = {"missing_count": count, "missing_since": r["missing_since"] or today}
                if count >= missing_limit:
                    fields["removed_upstream"] = 1
                    stats["removed"] += 1
                else:
                    stats["missing"] += 1
                ledger.update_tx(r["local_id"], fields)
    return stats


def ha_novidade(ledger: Ledger, client: PluggyAPI, cfg: Config) -> tuple[bool, str]:
    """Whether any bank refreshed in Pluggy after the last successful sync started (cheap: one get_item per bank).
    No successful sync yet, or a bank that cannot be read: yes, so the sync runs and reports it."""
    last = ledger.one("SELECT started_at FROM sync_runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1")
    if not last:
        return True, "nenhum sync anterior"
    desde = datetime.fromisoformat(str(last["started_at"]).replace("Z", "+00:00"))
    for it in cfg.items:
        try:
            item = client.get_item(it["item_id"])
        except (PluggyError, httpx.TransportError):
            return True, f"{it['bank']}: não consegui ler o item"
        quando = item.get("lastUpdatedAt")
        if quando and datetime.fromisoformat(str(quando).replace("Z", "+00:00")) > desde:
            return True, f"{it['bank']} atualizou em {quando}"
    return False, f"nenhum banco atualizou desde {last['started_at']}"


def check_item(item: dict[str, Any], label: str, now: datetime, stale_hours: int) -> list[str]:
    alerts = []
    status = item.get("status")
    err = (item.get("error") or {}).get("code") or item.get("executionStatus")
    if status != "UPDATED":
        alerts.append(f"{label}: status {status} ({err}). Reconecte em meu.pluggy.ai se persistir.")
    for product, detail in (item.get("statusDetail") or {}).items():
        for w in (detail or {}).get("warnings") or []:
            code = str(w.get("code"))
            if code == "423":
                alerts.append(f"{label}: limite mensal do Open Finance atingido em {product}; volta no mês que vem.")
            else:
                alerts.append(f"{label}: aviso {code} em {product}.")
    last = item.get("lastUpdatedAt")
    if last:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        hours = (now - ts).total_seconds() / 3600
        if hours > stale_hours:
            alerts.append(f"{label}: sem atualização há {int(hours)} h.")
    return alerts


def upsert_bills(ledger: Ledger, account_id: str, bills: list[dict[str, Any]]) -> int:
    from fin.ledger import now_iso

    with ledger.tx():
        for b in bills:
            ledger.x(
                "INSERT INTO bills(id,account_id,due_date,closing_date,total_cents,minimum_cents,finance_charges_json,payments_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET due_date=excluded.due_date, closing_date=excluded.closing_date, "
                "total_cents=excluded.total_cents, minimum_cents=excluded.minimum_cents, finance_charges_json=excluded.finance_charges_json, "
                "payments_json=excluded.payments_json, updated_at=excluded.updated_at",
                (b["id"], account_id, str(b.get("dueDate") or "")[:10] or None,
                 str(b.get("billClosingDate") or "")[:10] or None,
                 to_cents(b["totalAmount"]) if b.get("totalAmount") is not None else None,
                 to_cents(b["minimumPaymentAmount"]) if b.get("minimumPaymentAmount") is not None else None,
                 json.dumps(b.get("financeCharges") or []), json.dumps(b.get("payments") or []), now_iso()),
            )
    return len(bills)


def upsert_investments(ledger: Ledger, item_id: str, invs: list[dict[str, Any]], mirror_item_id: str | None = None) -> int:
    from fin.ledger import now_iso

    with ledger.tx():
        for v in invs:
            ledger.x(
                "INSERT INTO investments(id,item_id,mirror_item_id,type,subtype,name,balance_cents,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mirror_item_id=excluded.mirror_item_id, "
                "type=excluded.type, subtype=excluded.subtype, name=excluded.name, balance_cents=excluded.balance_cents, "
                "status=excluded.status, updated_at=excluded.updated_at",
                (v["id"], item_id, mirror_item_id, v.get("type"), v.get("subtype"), v.get("name"),
                 to_cents(v.get("balance") or 0), v.get("status"), now_iso()))
    return len(invs)


def upsert_loans(ledger: Ledger, item_id: str, loans: list[dict[str, Any]], mirror_item_id: str | None = None) -> int:
    from fin.ledger import now_iso

    day = lambda v: str(v or "")[:10] or None
    with ledger.tx():
        for ln in loans:
            inst, pay = ln.get("installments") or {}, ln.get("payments") or {}
            ledger.x(
                "INSERT INTO loans(id,item_id,product,contract_date,contract_cents,cet,installments_total,installments_paid,"
                "first_due_date,settlement_date,outstanding_cents,next_installment_cents,updated_at,mirror_item_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET product=excluded.product, contract_date=excluded.contract_date, "
                "contract_cents=excluded.contract_cents, cet=excluded.cet, installments_total=excluded.installments_total, "
                "installments_paid=excluded.installments_paid, first_due_date=excluded.first_due_date, "
                "settlement_date=excluded.settlement_date, outstanding_cents=excluded.outstanding_cents, "
                "next_installment_cents=excluded.next_installment_cents, updated_at=excluded.updated_at, "
                "mirror_item_id=excluded.mirror_item_id",
                (ln["id"], item_id, ln.get("productName"), day(ln.get("contractDate")), to_cents(ln.get("contractAmount") or 0),
                 ln.get("CET"), inst.get("totalNumberOfInstallments"), inst.get("paidInstallments"),
                 day(ln.get("firstInstallmentDueDate")), day(ln.get("settlementDate")),
                 to_cents(pay.get("contractOutstandingBalance") if pay.get("contractOutstandingBalance") is not None
                          else ln.get("totalRemainingAmount") or 0),
                 to_cents(ln.get("nextInstallmentAmount") or 0), now_iso(), mirror_item_id),
            )
    return len(loans)


def run_sync(ledger: Ledger, client: PluggyAPI, cfg: Config, tax: Taxonomy, *, today: date | None = None,
             now: datetime | None = None) -> SyncResult:
    today = today or date.today()
    now = now or datetime.now(UTC)
    result = SyncResult()
    run_id = ledger.start_run(today.isoformat())
    finished = False
    try:
        _sync_body(ledger, client, cfg, tax, today, now, result)
        ledger.finish_run(run_id, result.status, dict(result.stats), result.alerts)
        finished = True
    finally:
        if not finished:  # unexpected error: never leave the run as "running"
            ledger.finish_run(run_id, "error", dict(result.stats), result.alerts)
    return result


def _sync_body(ledger: Ledger, client: PluggyAPI, cfg: Config, tax: Taxonomy, today: date, now: datetime,
               result: SyncResult) -> None:
    last_ok = ledger.last_success_date()
    window_start = (today - timedelta(days=cfg.window_days)).isoformat() if last_ok else None
    created_from = None
    if last_ok:
        created_from = (date.fromisoformat(last_ok) - timedelta(days=2)).isoformat() + "T00:00:00.000Z"

    # 1) items + accounts first: own documents must be known before parsing transactions
    accounts: list[Any] = []
    conexoes: dict[str, Any] = {}  # how fresh each bank is in Pluggy, for the dashboard's data-health strip
    for it in cfg.items:
        bank, item_id = it["bank"], it["item_id"]
        label = BANK_NAMES.get(bank, bank)
        try:
            item = client.get_item(item_id)
            conexoes[bank] = {"status": item.get("status"), "ultima": item.get("lastUpdatedAt"), "proxima": item.get("nextAutoSyncAt"),
                              "consentimento": item.get("consentExpiresAt")}  # Meu Pluggy: the authorization lasts 1 year
            result.alerts += check_item(item, label, now, cfg.stale_hours)
            for acc in client.list_accounts(item_id):
                ledger.upsert_account(parse_account(acc, bank, ledger.salt, it.get("owner", "pf")))
                accounts.append(ledger.account(acc["id"]))
        except PluggyError as exc:
            result.status = "partial"
            result.alerts.append(f"{label}: erro Pluggy {exc.status} {exc.code or ''}".strip())
            log.warning("item %s falhou: %s", bank, exc.status)
        except httpx.TransportError as exc:
            result.status = "partial"
            result.alerts.append(f"{label}: sem conexão com a Pluggy ({type(exc).__name__})")
            log.warning("item %s: rede (%s)", bank, type(exc).__name__)
    if conexoes:
        ledger.set_meta("pluggy_conexoes", json.dumps(conexoes, sort_keys=True, ensure_ascii=False))
    own_docs, own_tkeys = ledger.own_doc_hashes(), ledger.own_transfer_keys()

    # 2) transactions (two passes) + bills
    for acc in accounts:
        label = f"{BANK_NAMES.get(acc['bank'], acc['bank'])} {'cartão' if acc['type'] == 'CREDIT' else 'conta'}"
        try:
            raw = client.list_transactions(acc["id"], date_from=window_start)
            complete_window = True
            if created_from:
                ids = {t["id"] for t in raw}
                raw += [t for t in client.list_transactions(acc["id"], created_at_from=created_from) if t["id"] not in ids]
        except PluggyError as exc:
            result.status = "partial"
            result.alerts.append(f"{label}: erro ao ler transações ({exc.status})")
            continue
        except httpx.TransportError as exc:
            result.status = "partial"
            result.alerts.append(f"{label}: sem conexão ao ler transações ({type(exc).__name__})")
            continue
        if acc["type"] == "CREDIT":
            raw, dup = drop_reissued_card_payments(raw)
            result.stats["pagamentos_duplicados"] += dup
        rows = [parse_transaction(t, acc, ledger.salt, own_docs, own_tkeys, cfg) for t in raw]
        st = upsert_account_transactions(ledger, acc, rows, window_start=window_start if complete_window else None,
                                         today=today.isoformat(), missing_limit=cfg.missing_syncs_to_remove)
        for k, v in st.items():
            result.stats[k] += v
        result.stats["fetched"] += len(rows)
        mism = sum(r["sign_mismatch"] for r in rows)
        result.stats["sign_mismatch"] += mism
        if mism:
            result.alerts.append(f"{label}: {mism} lançamento(s) com sinal do valor diferente do tipo DEBIT/CREDIT "
                                 "(usado o tipo); confira o extrato")
        if acc["type"] == "CREDIT":
            try:
                result.stats["bills"] += upsert_bills(ledger, acc["id"], client.list_bills(acc["id"]))
            except (PluggyError, httpx.TransportError) as exc:
                result.stats["bills_unavailable"] += 1
                log.info("faturas indisponíveis (%s); usando creditData", getattr(exc, "status", type(exc).__name__))

    # 2b) loans and investments: the Meu Pluggy mirror item does not relay loans; `bank_item_id` is the bank's own item
    for it in cfg.items:
        bid = it.get("bank_item_id")
        if not bid:
            continue
        for what, fetch, upsert in (("empréstimos", client.list_loans, upsert_loans),
                                    ("investimentos", client.list_investments, upsert_investments)):
            try:
                result.stats[what] += upsert(ledger, bid, fetch(bid), it.get("item_id"))
            except (PluggyError, httpx.TransportError) as exc:
                result.alerts.append(f"{it.get('bank', 'item')}: {what} indisponíveis ({getattr(exc, 'status', type(exc).__name__)})")

    # 3) deterministic rules + installment projection
    for k, v in apply_all(ledger, cfg, tax, today=today).items():
        result.stats[k] += v

    # 3b) automatic sweep investments the bank does not report (Itaú "APLIC AUT"): the balance is what went in minus
    # what came back, from the account's first row (`aplicacao_automatica: true` only for an account whose whole
    # history is in the ledger)
    for it in cfg.items:
        if str(it.get("aplicacao_automatica", "")).lower() in ("true", "1", "sim", "yes"):
            saldo = -ledger.one("SELECT COALESCE(SUM(t.amount_cents),0) s FROM transactions t JOIN accounts a ON a.id=t.account_id "
                                "WHERE a.item_id=? AND t.natureza='Investimento' AND t.removed_upstream=0 AND t.date<=?",
                                (it["item_id"], today.isoformat()))["s"]
            upsert_investments(ledger, it["item_id"], [{"id": f"auto:{it['item_id']}", "type": "AUTOMATICA",
                                                        "name": "Aplicação automática", "balance": max(0, saldo) / 100,
                                                        "status": "ACTIVE"}], it["item_id"])
    if not accounts and cfg.items:
        result.status = "error"
