"""Offline demo with SYNTHETIC data (fictitious names/documents; real bank COMPE codes).

Runs the whole flow without credentials or network: sync -> rules -> categorize
(offline fake of the Anthropic SDK) -> approval -> Caju -> 2nd sync (id changes and a
new installment) -> publish (CSV instead of Sheets) -> reports as the bot would answer.
Includes a fictitious MEI account (owner pj): client payment, DAS, a bank fee and a monthly
withdrawal to the PF account ("Retirada PJ→PF").
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import date, datetime, UTC
from pathlib import Path
from typing import Any

from fin.caju import import_caju
from fin.categorize import approve, load_seed_dictionary, run_categorize
from fin.config import config_from_dict, load_taxonomy, PROJECT_ROOT
from fin.ledger import Ledger, local_id_str
from fin.llm import AnthropicCategorizer, OfflineAnthropicClient
from fin.pluggy import MemoryPluggyClient
from fin.reports import monthly_summary
from fin.rules import add_months, card_competence
from fin.sheets import CsvPublisher, publish
from fin.sync import run_sync
from fin.telegram_bot import Handlers

OWN_CPF = "111.111.111-11"
TODAY = date(2026, 10, 15)
ITEMS = {"itau": "10000000-0000-4000-8000-000000000001", "nubank": "10000000-0000-4000-8000-000000000002",
         "santander": "10000000-0000-4000-8000-000000000003"}
ACC = {"itau": "a0000000-0000-4000-8000-000000000001", "nubank": "a0000000-0000-4000-8000-000000000002",
       "card": "c0000000-0000-4000-8000-000000000002", "santander": "a0000000-0000-4000-8000-000000000003"}
CNPJ_CLT, CNPJ_PJ, CNPJ_CARD = "11.111.111/0001-11", "22.222.222/0001-22", "33.333.333/0001-33"
CPF_LOCADOR, CPF_AMIGO = "444.444.444-44", "555.555.555-55"
# the MEI (owner pj): its own CNPJ and its account at Nubank Empresas (all fictitious)
CNPJ_MEI = "77.777.777/0001-77"
ITEM_PJ = "10000000-0000-4000-8000-000000000004"
ACC_PJ = "a0000000-0000-4000-8000-000000000004"
PJ_NUMBER = "0001/5550001-2"


# ---- Pluggy-shaped builders (also used by tests) ---------------------------------------------

def _id_factory():
    counter = iter(range(1, 10_000))
    return lambda prefix: f"{prefix}{next(counter):07d}-0000-4000-8000-000000000000"

def party(name: str, doc: str | None, compe: str | None = None, account: str | None = None, branch: str | None = None) -> dict:
    p: dict[str, Any] = {"name": name, "routingNumber": compe}
    if doc:
        p["documentNumber"] = {"type": "CPF" if len(re.sub(r"\D", "", doc)) == 11 else "CNPJ", "value": doc}
    if account:
        p["accountNumber"] = account
    if branch:
        p["branchNumber"] = branch
    return p


def bank_account(acc_id: str, item_id: str, number: str, compe: str, balance: float, tax_number: str = OWN_CPF,
                 owner: str = "TITULAR TESTE") -> dict:
    return {"id": acc_id, "itemId": item_id, "type": "BANK", "subtype": "CHECKING_ACCOUNT", "number": number,
            "name": "Conta Corrente", "marketingName": None, "balance": balance, "taxNumber": tax_number,
            "owner": owner, "currencyCode": "BRL",
            "bankData": {"transferNumber": f"{compe}/{number}", "closingBalance": balance}}


def card_account(acc_id: str, item_id: str, last4: str, limit: float, used: float, close: str, due: str) -> dict:
    return {"id": acc_id, "itemId": item_id, "type": "CREDIT", "subtype": "CREDIT_CARD", "number": last4,
            "name": "Cartao", "marketingName": "CARTAO EXEMPLO", "balance": used, "taxNumber": "***.***.111-11",
            "owner": "TITULAR TESTE", "currencyCode": "BRL",
            "creditData": {"creditLimit": limit, "availableCreditLimit": round(limit - used, 2), "balanceCloseDate": close,
                           "balanceDueDate": due, "minimumPayment": None, "brand": "MASTERCARD", "status": "ACTIVE"}}


def tx(tid: str, account_id: str, day: str, amount: float, desc: str, *, ttype: str | None = None, op: str | None = None,
       status: str = "POSTED", payer: dict | None = None, receiver: dict | None = None, method: str | None = None,
       ccm: dict | None = None, provider_id: str | None = None, created_at: str | None = None,
       currency: str = "BRL", amount_brl: float | None = None, card: bool = False) -> dict:
    if ttype is None:
        ttype = ("DEBIT" if amount > 0 else "CREDIT") if card else ("DEBIT" if amount < 0 else "CREDIT")
    pd = None
    if payer or receiver or method:
        pd = {"payer": payer, "receiver": receiver, "paymentMethod": method}
    t = {"id": tid, "description": desc, "descriptionRaw": desc, "currencyCode": currency, "amount": amount,
         "date": f"{day}T00:00:00.000Z", "accountId": account_id, "type": ttype, "status": status,
         "operationType": op, "paymentData": pd, "merchant": None, "category": None, "categoryId": None,
         "providerId": provider_id, "createdAt": created_at or f"{day}T09:00:00.000Z",
         "updatedAt": created_at or f"{day}T09:00:00.000Z", "balance": None, "order": 0}
    if amount_brl is not None:
        t["amountInAccountCurrency"] = amount_brl
    if ccm is not None:
        t["creditCardMetadata"] = ccm
    return t


def item(item_id: str, status: str = "UPDATED", warnings: list | None = None, last: str = "2026-10-15T06:05:00.000Z") -> dict:
    detail = {"transactions": {"isUpdated": not warnings, "warnings": warnings or []}} if warnings else None
    return {"id": item_id, "connector": {"id": 200, "name": "MeuPluggy"}, "status": status,
            "executionStatus": "SUCCESS" if status == "UPDATED" else status, "error": None,
            "lastUpdatedAt": last, "statusDetail": detail}


# ---- dataset ----------------------------------------------------------------------------------
def _card_purchases() -> list[tuple[str, float, str, dict]]:
    """(day, amount, desc, extra ccm) for the Nubank card, Aug-Oct 2026."""
    out: list[tuple[str, float, str, dict]] = []
    for m in ("2026-08", "2026-09", "2026-10"):
        out += [
            (f"{m}-04", 64.90, "IFD*IFOOD 1234 SAO PAULO", {}),
            (f"{m}-12", 38.50, "IFD*IFOOD 5678 SAO PAULO", {}),
            (f"{m}-09", 27.80, "UBER *TRIP HELP.UBER.COM", {}),
            (f"{m}-15", 55.90, "NETFLIX.COM", {}),
            (f"{m}-18", 412.37, "SUPERMERCADO EXEMPLO RJ", {}),
            (f"{m}-21", 89.00, "AQUARIO REEF CENTER EXEMPLO", {}),
        ]
    out += [
        ("2026-08-25", 74.30, "DROGARIA EXEMPLO 0042", {}),
        ("2026-09-02", 120.00, "LOJA ROUPAS EXEMPLO", {}),
        ("2026-10-11", 10.00, "STEAM GAMES", {"_usd": 55.20}),
        ("2026-10-13", 42.50, "UBER *TRIP HELP.UBER.COM", {}),
    ]
    # 10x purchase on 2026-08-03 (bill 2026-09 = 1/10)
    for n, day in ((1, "2026-08-03"), (2, "2026-09-03"), (3, "2026-10-03")):
        out.append((day, 300.00, f"LOJA ELETRONICOS EXEMPLO PARC {n:02d}/10",
                    {"installmentNumber": n, "totalInstallments": 10, "purchaseDate": "2026-08-03T00:00:00.000Z",
                     "paymentType": "INSTALLMENT"}))
    return out


def pj_transactions(months: tuple[str, ...] = ("2026-08", "2026-09", "2026-10"), nid=None) -> list[dict]:
    """MEI account (owner pj): client payment, DAS, Pix fee (October) and the withdrawal to the PF Itaú account.
    The Itaú side of each withdrawal is in pf_side_of_withdrawals()."""
    nid = nid or _id_factory()
    own_pj = party("MEI EXEMPLO", CNPJ_MEI, "260", "5550001-2", "0001")
    out = []
    for m in months:
        out += [
            tx(nid("j"), ACC_PJ, f"{m}-06", 6000.00, "PIX RECEBIDO CLIENTE PJ EXEMPLO", op="PIX", method="PIX",
               payer=party("CLIENTE PJ EXEMPLO LTDA", CNPJ_PJ, "237"), receiver=own_pj),
            tx(nid("j"), ACC_PJ, f"{m}-08", -5000.00, "PIX ENVIADO TITULAR TESTE", op="PIX", method="PIX",
               payer=own_pj, receiver=party("TITULAR TESTE", OWN_CPF, "341", "12345-6")),
            tx(nid("j"), ACC_PJ, f"{m}-20", -80.90, "PAGAMENTO DAS-SIMEI", op="CONVENIO_ARRECADACAO"),
        ]
    if "2026-10" in months:
        out.append(tx(nid("j"), ACC_PJ, "2026-10-06", -2.50, "TARIFA PIX RECEBIDO", op="TARIFA_SERVICOS_AVULSOS"))
    return out


def pf_side_of_withdrawals(months: tuple[str, ...] = ("2026-08", "2026-09", "2026-10"), nid=None) -> list[dict]:
    nid = nid or _id_factory()
    own_itau = party("TITULAR TESTE", OWN_CPF, "341", "12345-6", "0001")
    return [tx(nid("w"), ACC["itau"], f"{m}-08", 5000.00, "PIX RECEBIDO MEI EXEMPLO", op="PIX", method="PIX",
               payer=party("MEI EXEMPLO", CNPJ_MEI, "260", "5550001-2"), receiver=own_itau) for m in months]


def pj_account(balance: float = 2750.00) -> dict:
    return bank_account(ACC_PJ, ITEM_PJ, PJ_NUMBER, "260", balance, tax_number=CNPJ_MEI, owner="MEI EXEMPLO")


def dataset(day2: bool = False) -> MemoryPluggyClient:
    i, s, nb, c = ACC["itau"], ACC["santander"], ACC["nubank"], ACC["card"]
    own_itau = party("TITULAR TESTE", OWN_CPF, "341", "12345-6", "0001")
    txs: dict[str, list[dict]] = {i: [], s: [], nb: [], c: [], ACC_PJ: []}
    seq = {"n": 0}

    def nid(prefix: str) -> str:
        seq["n"] += 1
        return f"{prefix}{seq['n']:07d}-0000-4000-8000-000000000000"

    # card purchases + bills
    bill_totals: dict[str, int] = {}
    for d, amount, desc, extra in _card_purchases():
        forecast = card_competence(date.fromisoformat(d), 3, 10)
        status = "POSTED" if forecast <= "2026-10" else "PENDING"
        ccm = {"cardNumber": "1234", "billForecastDate": forecast, "paymentType": "SINGLE"}
        ccm.update({k: v for k, v in extra.items() if not k.startswith("_")})
        if status == "POSTED":
            ccm["billId"] = f"b{forecast.replace('-', '')}-0000-4000-8000-000000000000"
        usd = extra.get("_usd")
        t = tx(nid("c"), c, d, amount, desc, status=status, op="PAGAMENTO", ccm=ccm, card=True,
               currency="USD" if usd else "BRL", amount_brl=usd)
        if usd:
            t["amount"], t["amountInAccountCurrency"] = 10.00, usd
        txs[c].append(t)
        bill_totals[forecast] = bill_totals.get(forecast, 0) + round((usd or amount) * 100)
    # refund on the clothes store (bill 2026-09 already closed -> lands on 2026-10)
    txs[c].append(tx(nid("c"), c, "2026-09-20", -30.00, "ESTORNO LOJA ROUPAS EXEMPLO", op="ESTORNO", card=True,
                     ccm={"cardNumber": "1234", "billForecastDate": "2026-10", "billId": "b202610-0000-4000-8000-000000000000"}))
    bill_totals["2026-10"] -= 3000

    bills = []
    for m in ("2026-08", "2026-09", "2026-10"):
        total = bill_totals.get(m, 0) / 100
        if not total:
            continue
        pay_day = f"{m}-08"
        bills.append({"id": f"b{m.replace('-', '')}-0000-4000-8000-000000000000", "dueDate": f"{m}-10T00:00:00.000Z",
                      "billClosingDate": f"{m}-03T00:00:00.000Z", "totalAmount": total, "minimumPaymentAmount": round(total * 0.15, 2),
                      "financeCharges": [], "payments": [{"valueType": "FULL_PAYMENT", "paymentDate": f"{pay_day}T00:00:00.000Z",
                                                          "paymentMode": "BANK_SLIP", "amount": total}]})
        # card-side payment + bank-side boleto
        txs[c].append(tx(nid("c"), c, pay_day, -total, "Pagamento recebido", op="PAGAMENTO_FATURA", card=True,
                         ccm={"cardNumber": "1234", "billForecastDate": add_months(m, 1)}))
        txs[i].append(tx(nid("f"), i, pay_day, -total, "PAGTO FATURA CARTAO EXEMPLO", op="BOLETO", method="BOLETO",
                         payer=own_itau, receiver=party("EMISSOR CARTAO EXEMPLO SA", CNPJ_CARD, "999")))

    for m in ("2026-08", "2026-09", "2026-10"):
        txs[i] += [
            tx(nid("f"), i, f"{m}-05", 8500.00, "SALARIO EMPRESA CLT EXEMPLO", op="FOLHA_PAGAMENTO", method="TED",
               payer=party("EMPRESA CLT EXEMPLO LTDA", CNPJ_CLT, "001"), receiver=own_itau),
            tx(nid("f"), i, f"{m}-07", -1500.00, "PIX ENVIADO TITULAR TESTE", op="PIX", method="PIX",
               payer=own_itau, receiver=party("TITULAR TESTE", OWN_CPF, "260", "7654321-0")),
            tx(nid("f"), i, f"{m}-10", -700.00, "PIX ENVIADO TITULAR TESTE", op="PIX", method="PIX",
               payer=own_itau, receiver=party("TITULAR TESTE", OWN_CPF, "033", "99887-1")),
            tx(nid("f"), i, f"{m}-10", -1500.00, "PIX ENVIADO LOCADOR EXEMPLO", op="PIX", method="PIX",
               payer=own_itau, receiver=party("LOCADOR EXEMPLO", CPF_LOCADOR, "104")),
            tx(nid("f"), i, f"{m}-10", -19.90, "TARIFA PACOTE SERVICOS", op="PACOTE_TARIFA_SERVICOS"),
            tx(nid("f"), i, f"{m}-14", 3.12, "REND PAGO APLIC AUT", op="RENDIMENTO_APLIC_FINANCEIRA"),
            tx(nid("f"), i, f"{m}-20", -500.00, "APLICACAO CDB EXEMPLO", op="OUTROS"),
        ]
        # Nubank: same-institution style credit without counterpart data
        txs[nb].append(tx(nid("n"), nb, f"{m}-07", 1500.00, "TRANSFERENCIA RECEBIDA PELO PIX", op="PIX"))
        txs[s] += [
            tx(nid("s"), s, f"{m}-10", 700.00, "PIX RECEBIDO TITULAR TESTE", op="PIX", method="PIX",
               payer=party("TITULAR TESTE", OWN_CPF, "341", "12345-6"), receiver=party("TITULAR TESTE", OWN_CPF, "033")),
            tx(nid("s"), s, f"{m}-11", -45.90, "PIX PADARIA EXEMPLO", op="PIX", method="PIX",
               receiver=party("PADARIA EXEMPLO LTDA", "55.555.555/0001-55", "077")),
            tx(nid("s"), s, f"{m}-12", -210.33, "PAGTO CONTA ENERGIA LIGHT", op="CONVENIO_ARRECADACAO"),
        ]
        if m != "2026-10":
            txs[s].append(tx(nid("s"), s, f"{m}-15", -119.90, "VIVO FIBRA INTERNET", op="BOLETO", method="BOLETO",
                             receiver=party("TELEFONICA EXEMPLO SA", "66.666.666/0001-66", "001")))
    # MEI: the client pays the PJ account; the PJ withdraws to the PF Itaú account every month
    txs[ACC_PJ] += pj_transactions(nid=nid)
    txs[i] += pf_side_of_withdrawals(nid=nid)
    txs[i].append(tx(nid("f"), i, "2026-10-09", -80.00, "PIX ENVIADO AMIGO EXEMPLO", op="PIX", method="PIX",
                     payer=own_itau, receiver=party("AMIGO EXEMPLO", CPF_AMIGO, "104")))
    txs[s].append(tx(nid("s"), s, "2026-10-13", -35.00, "PIX ENVIADO", op="PIX"))
    txs[i].append(tx(nid("f"), i, "2026-10-02", -129.00, "CURSO ONLINE EXEMPLO EAD", op="CARTAO"))

    if day2:
        # 1) Pluggy recreated an IFOOD transaction with a new id (same content) -> must relink, not duplicate
        for t in txs[c]:
            if t["description"].startswith("IFD*IFOOD 5678") and t["date"].startswith("2026-10"):
                t["id"] = "c9999999-0000-4000-8000-000000000000"
                t["providerId"] = None
        # 2) the 4/10 installment arrives (future, PENDING) -> replaces the projection
        txs[c].append(tx("c8888888-0000-4000-8000-000000000000", c, "2026-11-03", 300.00, "LOJA ELETRONICOS EXEMPLO PARC 04/10",
                         status="PENDING", op="PAGAMENTO", card=True, created_at="2026-10-16T06:00:00.000Z",
                         ccm={"cardNumber": "1234", "billForecastDate": "2026-12", "installmentNumber": 4, "totalInstallments": 10,
                              "purchaseDate": "2026-08-03T00:00:00.000Z", "paymentType": "INSTALLMENT"}))

    used = sum(t["amount"] if not t.get("amountInAccountCurrency") else t["amountInAccountCurrency"]
               for t in txs[c] if t["status"] == "PENDING" or (t["creditCardMetadata"] or {}).get("billForecastDate", "") >= "2026-11")
    accounts = {
        ITEMS["itau"]: [bank_account(i, ITEMS["itau"], "0001/12345-6", "341", 4210.55)],
        ITEMS["nubank"]: [bank_account(nb, ITEMS["nubank"], "0001/7654321-0", "260", 1650.00),
                          card_account(c, ITEMS["nubank"], "1234", 10000, round(used + 2700, 2), "2026-11-03", "2026-11-10")],
        ITEMS["santander"]: [bank_account(s, ITEMS["santander"], "0002/99887-1", "033", 812.30)],
        ITEM_PJ: [pj_account()],
    }
    items = {ITEM_PJ: item(ITEM_PJ), ITEMS["itau"]: item(ITEMS["itau"]),
             ITEMS["nubank"]: item(ITEMS["nubank"], warnings=[{"code": "423", "message": "limit"}]),
             ITEMS["santander"]: item(ITEMS["santander"])}
    return MemoryPluggyClient(items, accounts, copy.deepcopy(txs), {c: bills})


def demo_config(data_dir: Path) -> dict[str, Any]:
    return {
        "data_dir": str(data_dir),
        "pluggy": {"items": [{"bank": b, "item_id": iid} for b, iid in ITEMS.items()]
                   + [{"bank": "nubank_pj", "item_id": ITEM_PJ, "owner": "pj"}]},
        "cards": {"nubank": {"issuer_cnpj": CNPJ_CARD, "closing_day": 3, "due_day": 10}},
        "income": {"salary_day": 5, "sources": [
            {"cnpj": re.sub(r"\D", "", CNPJ_CLT), "fonte": "Empresa CLT", "categoria": "Salário CLT"},
            {"cnpj": re.sub(r"\D", "", CNPJ_PJ), "fonte": "Cliente PJ", "categoria": "Faturamento PJ"}]},
        "fixed_costs": [
            {"nome": "Aluguel", "valor": 1500.00, "dia": 10, "padrao": "LOCADOR"},
            {"nome": "Energia", "valor": 210.00, "dia": 12, "padrao": "ENERGIA"},
            {"nome": "Internet", "valor": 119.90, "dia": 15, "padrao": "VIVO"},
            {"nome": "Netflix", "valor": 55.90, "dia": 15, "padrao": "NETFLIX"},
        ],
        "privacy": {"block_terms": ["titular teste"]},
        "llm": {"enabled": True, "chunk_size": 40, "min_confidence": 0.6},
    }


CAJU_CSV = """data;descricao;valor;categoria;fonte
2026-08-01;Crédito Caju;1000,00;Salário CLT;Empresa CLT
2026-08-31;Gastos Caju agosto;-934,20;;
2026-09-01;Crédito Caju;1000,00;Salário CLT;Empresa CLT
2026-09-30;Gastos Caju setembro;-1.012,75;;
2026-10-01;Crédito Caju;1000,00;Salário CLT;Empresa CLT
2026-10-14;Gastos Caju outubro (parcial);-401,10;;
"""


def _strip_html(text: str) -> str:
    return re.sub(r"</?pre>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def run_demo(out: Path, echo=print) -> dict[str, Any]:
    out = Path(out).resolve()
    marker = out / ".fin-demo"
    if out.exists() and any(out.iterdir()) and not marker.exists():
        raise RuntimeError(f"{out} não está vazio e não é uma pasta de demo; escolha outra com --out")
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    marker.write_text("pasta gerada por fin demo (dados sintéticos)\n", encoding="utf-8")
    (out / "data" / "caju.csv").write_text(CAJU_CSV, encoding="utf-8")

    cfg = config_from_dict(demo_config(out / "data"))
    tax = load_taxonomy()
    ledger = Ledger(cfg.db_path)
    seed = load_seed(ledger)
    now = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    summary: dict[str, Any] = {"out": str(out), "seed_entries": seed}

    echo("== 1. Sync (dados sintéticos, sem rede)")
    r1 = run_sync(ledger, dataset(), cfg, tax, today=TODAY, now=now)
    echo(f"   status={r1.status} inseridos={r1.stats['inserted']} faturas={r1.stats['bills']} "
         f"parcelas projetadas={r1.stats['installments_projected']}")
    for a in r1.alerts:
        echo(f"   alerta: {a}")
    count1 = ledger.one("SELECT COUNT(*) n FROM transactions")["n"]

    echo("== 2. Categorização: dicionário -> cache -> LLM (cliente offline, mesmo formato do SDK)")
    fake = OfflineAnthropicClient()
    cat = run_categorize(ledger, cfg, tax, AnthropicCategorizer(fake, ledger, block_terms=cfg.block_terms))
    echo(f"   dicionário={cat['dictionary']} estorno herdado={cat['refund_inherited']} cache={cat['cache']} "
         f"enviados ao LLM={cat['llm_sent']} bloqueados={cat['blocked']}")
    if fake.requests:
        sent = json.loads(fake.requests[0]["messages"][0]["content"])
        echo("   payload exato enviado (também gravado em llm_audit no cofre):")
        for it in sent:
            echo(f"     {it}")
        summary["llm_payload"] = sent

    echo("== 3. Aprovações (você): aluguel para pessoa física e uma sugestão da IA")
    locador = ledger.one("SELECT local_id, counterpart_alias FROM transactions WHERE description LIKE '%LOCADOR%' ORDER BY date LIMIT 1")
    res = approve(ledger, tax, locador["local_id"], categoria="Moradia", essencial="Essencial", necessidade="fixo")
    ledger.x("UPDATE counterparts SET apelido='Aluguel' WHERE alias=?", (locador["counterpart_alias"],))
    echo(f"   {local_id_str(res['local_id'])} -> {res['categoria']} (aprendido: {res['learned']})")
    reef = ledger.one("SELECT local_id FROM transactions WHERE desc_norm LIKE 'AQUARIO REEF%' ORDER BY date LIMIT 1")
    res = approve(ledger, tax, reef["local_id"], categoria="Hobby", essencial="Supérfluo")
    echo(f"   {local_id_str(res['local_id'])} -> {res['categoria']} (aprendido: {res['learned']})")

    echo("== 4. Caju (CSV manual)")
    cj = import_caju(ledger, tax, out / "data" / cfg.caju_file)
    echo(f"   {cj}")

    echo("== 5. Segundo sync: id recriado pela Pluggy + parcela 4/10 chegando")
    r2 = run_sync(ledger, dataset(day2=True), cfg, tax, today=date(2026, 10, 16), now=now)
    run_categorize(ledger, cfg, tax, AnthropicCategorizer(fake, ledger, block_terms=cfg.block_terms))
    count2 = ledger.one("SELECT COUNT(*) n FROM transactions WHERE source='pluggy'")["n"]
    echo(f"   inseridos={r2.stats['inserted']} religados={r2.stats['relinked']} atualizados={r2.stats['updated']} "
         f"(antes {count1} lançamentos Pluggy, agora {count2}: só a 4/10 é nova)")
    proj = ledger.q("SELECT installment_number FROM installments_projection ORDER BY installment_number")
    echo(f"   projeções restantes: {[p['installment_number'] for p in proj]}")

    echo("== 6. Publicação (CSV no lugar do Google Sheets)")
    pub = publish(ledger, tax, CsvPublisher(out / "sheets"), TODAY)
    echo(f"   {pub} -> {out / 'sheets'}")

    echo("== 7. Respostas do bot (Telegram)")
    handlers = Handlers(lambda: Ledger(cfg.db_path), cfg, tax, today=lambda: TODAY)
    report = {}
    for cmd, args in (("/resumo", []), ("/faturas", []), ("/posso_comprar", ["1200"]),
                      ("/posso_comprar", ["3.000", "10x"]), ("/pendentes", [])):
        text = _strip_html(handlers(cmd, args))
        report[" ".join([cmd, *args])] = text
        echo(f"\n>>> {cmd} {' '.join(args)}\n{text}")
    (out / "relatorio.txt").write_text("\n\n".join(f">>> {k}\n{v}" for k, v in report.items()), encoding="utf-8")
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    summary.update(receitas=s.receitas, despesas=s.despesas, saldo_livre=s.saldo_livre, pendentes=s.pendentes,
                   count1=count1, count2=count2, report=str(out / "relatorio.txt"))
    summary.update({f"{k.lower()}_saldo": v.saldo for k, v in s.secoes.items()})
    ledger.close()
    return summary


def load_seed(ledger: Ledger) -> int:
    import yaml

    path = PROJECT_ROOT / "dictionary.seed.yaml"
    if not path.exists():
        return 0
    return load_seed_dictionary(ledger, yaml.safe_load(path.read_text(encoding="utf-8")) or [])
