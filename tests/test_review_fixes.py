"""Regression tests for the review findings (rules, privacy, operation). Each one failed before its fix."""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from conftest import C, client, CNPJ_CARD, I, make_cfg, NB, NOW, OWN_ITAU, party, S, TODAY, tx

from fin.categorize import approve, run_categorize
from fin.llm import AnthropicCategorizer, OfflineAnthropicClient, validate
from fin.privacy import block_reason
from fin.reports import card_bills, month_totals, monthly_summary
from fin.sync import run_sync

FORECAST_NOV = {"billForecastDate": "2026-11"}


def _row(lg, pid):
    return lg.one("SELECT * FROM transactions WHERE pluggy_id=?", (pid,))


def _sync(ledger, data, cfg, tax, today=TODAY, bills=None):
    return run_sync(ledger, client(data, bills), cfg, tax, today=today, now=NOW)


class Recording:
    def __init__(self):
        self.items = []

    def categorize(self, items, categories):
        self.items += items
        return [{"id": i["id"], "categoria": "Revisar", "subcategoria": "", "estabelecimento_normalizado": "",
                 "confianca": 0.0} for i in items]


# ---- regras financeiras -----------------------------------------------------------------------
def test_1_card_sign_follows_type_and_alerts(ledger, cfg, tax):
    t = tx("buy", C, "2026-10-05", -100.0, "LOJA X", ttype="DEBIT", card=True, ccm=FORECAST_NOV)
    res = _sync(ledger, {C: [t]}, cfg, tax)
    assert _row(ledger, "buy")["amount_cents"] == -10000
    assert any("sinal" in a for a in res.alerts)
    ok = _sync(ledger, {C: [tx("b2", C, "2026-10-05", 100.0, "LOJA Y", card=True, ccm=FORECAST_NOV)]}, cfg, tax)
    assert _row(ledger, "b2")["amount_cents"] == -10000 and not any("sinal" in a for a in ok.alerts)


def test_2_timestamp_converted_to_brasilia(ledger, cfg, tax):
    t = tx("late", I, "2026-10-31", -50.0, "PADARIA X")
    t["date"] = "2026-11-01T01:30:00.000Z"  # 31/10 22:30 em Brasília
    mid = tx("mid", I, "2026-11-01", -10.0, "PADARIA Y")  # T00:00:00Z = data pura
    _sync(ledger, {I: [t, mid]}, cfg, tax, today=date(2026, 11, 2))
    assert (_row(ledger, "late")["date"], _row(ledger, "late")["competence"]) == ("2026-10-31", "2026-10")
    assert _row(ledger, "mid")["date"] == "2026-11-01"


def test_3_installments_with_purchase_date_and_no_forecast(ledger, cfg, tax):
    rows = [tx(f"p{n}", C, "2026-08-01", 250.0, f"LOJA ELETRO PARC {n:02d}/10", card=True,
               ccm={"installmentNumber": n, "totalInstallments": 10, "purchaseDate": "2026-08-01T00:00:00.000Z"})
            for n in (1, 2, 3)]
    _sync(ledger, {C: rows}, cfg, tax)
    assert [_row(ledger, f"p{n}")["competence"] for n in (1, 2, 3)] == ["2026-08", "2026-09", "2026-10"]
    assert ledger.one("SELECT COUNT(*) n FROM installments_projection")["n"] == 7


def test_4a_projection_of_closed_bill_expires(ledger, cfg, tax):
    t = tx("p3", C, "2026-07-01", 250.0, "LOJA ELETRO PARC 03/10", card=True,
           ccm={"installmentNumber": 3, "totalInstallments": 10, "billForecastDate": "2026-07"})
    _sync(ledger, {C: [t]}, cfg, tax)  # 15/10: open bill is 2026-11 (closing 3, due 10)
    months = [r["month"] for r in ledger.q("SELECT month FROM installments_projection ORDER BY month")]
    assert months and min(months) == "2026-11"
    assert [b for b in card_bills(ledger, cfg, TODAY) if b.month == "2026-10"][0].projected_cents == 0


def test_4b_refund_of_the_rest_cancels_projection(ledger, cfg, tax):
    t = tx("p3", C, "2026-10-01", 250.0, "LOJA ELETRO PARC 03/10", card=True,
           ccm={"installmentNumber": 3, "totalInstallments": 10, "billForecastDate": "2026-11"})
    r = tx("ref", C, "2026-10-10", -1750.0, "ESTORNO LOJA ELETRO", op="ESTORNO", card=True, ccm=FORECAST_NOV)
    _sync(ledger, {C: [t]}, cfg, tax)
    assert ledger.one("SELECT COUNT(*) n FROM installments_projection")["n"] == 7
    _sync(ledger, {C: [t, r]}, cfg, tax)
    assert ledger.one("SELECT COUNT(*) n FROM installments_projection")["n"] == 0


def test_5_next_month_fixed_cost_before_salary(tmp_path, ledger, tax):
    cfg = make_cfg(tmp_path, fixed_costs=[{"nome": "Aluguel", "valor": 1500, "dia": 1, "padrao": "LOCADOR"}])
    _sync(ledger, {I: [tx("rent", I, "2026-10-01", -1500.0, "PAGTO LOCADOR", op="BOLETO")]}, cfg, tax, today=date(2026, 10, 28))
    s = monthly_summary(ledger, cfg, tax, "2026-10", date(2026, 10, 28))
    assert s.proximo_salario == date(2026, 11, 5) and s.fixos_ate_salario == 150000


def test_6_salary_received_early_moves_window(ledger, cfg, tax):
    sal = tx("sal", I, "2026-10-03", 8500.0, "SALARIO", op="FOLHA_PAGAMENTO", method="TED",
             payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU)
    buy = tx("buy", C, "2026-09-20", 2000.0, "LOJA X", card=True, ccm={"billForecastDate": "2026-10"})
    _sync(ledger, {I: [sal], C: [buy]}, cfg, tax, today=date(2026, 10, 4))
    s = monthly_summary(ledger, cfg, tax, "2026-10", date(2026, 10, 4))
    assert s.proximo_salario == date(2026, 11, 5) and s.faturas_ate_salario == 200000


def test_7_utility_fatura_and_cartorio_stay_expenses(ledger, cfg, tax):
    data = {I: [tx("vivo", I, "2026-10-08", -120.0, "PAGAMENTO FATURA VIVO", op="BOLETO", method="BOLETO",
                   receiver=party("VIVO", "02.558.157/0001-62", "999")),
                tx("cart", I, "2026-10-09", -80.0, "PAG CARTORIO 2 OFICIO", op="BOLETO"),
                tx("nu", I, "2026-10-09", -300.0, "PAGTO FATURA CARTAO NUBANK", op="BOLETO")]}
    _sync(ledger, data, cfg, tax)
    assert month_totals(ledger, "2026-10")["despesas"] == 20000
    assert _row(ledger, "nu")["natureza"] == "Transferência"


def test_8_card_credit_is_never_income(ledger, cfg, tax):
    data = {C: [tx("pay", C, "2026-10-08", -700.0, "Pagamento em 08 OUT", card=True, ccm=FORECAST_NOV),
                tx("adj", C, "2026-10-09", -20.0, "Credito de ajuste", card=True, ccm=FORECAST_NOV)],
            I: [tx("bol", I, "2026-10-08", -700.0, "PAGTO FATURA CARTAO", op="BOLETO", method="BOLETO",
                   receiver=party("EMISSOR", CNPJ_CARD, "999"))]}
    _sync(ledger, data, cfg, tax)
    run_categorize(ledger, cfg, tax, None)
    assert month_totals(ledger, "2026-10")["receitas"] == 0 and month_totals(ledger, "2026-11")["receitas"] == 0
    assert _row(ledger, "pay")["rule"] == "card_bill_payment" and _row(ledger, "pay")["pair_id"] == _row(ledger, "bol")["pair_id"]
    adj = _row(ledger, "adj")
    assert (adj["natureza"], adj["review_status"]) == ("Despesa", "pendente")
    assert month_totals(ledger, "2026-11")["despesas"] == -2000  # credit reduces expenses, never adds income


def test_9_anonymous_pix_do_not_share_counterpart_but_named_ones_do(ledger, cfg, tax):
    data = {NB: [tx("a", NB, "2026-10-02", -1500.0, "PIX ENVIADO", op="PIX"),
                 tx("b", NB, "2026-10-12", -45.0, "PIX ENVIADO", op="PIX"),
                 tx("w1", NB, "2026-09-10", -900.0, "PIX ENVIADO WESLEY SOUZA", op="PIX"),
                 tx("w2", NB, "2026-10-10", -900.0, "PIX ENVIADO WESLEY SOUZA", op="PIX")]}
    _sync(ledger, data, cfg, tax)
    assert _row(ledger, "a")["counterpart_alias"] != _row(ledger, "b")["counterpart_alias"]
    res = approve(ledger, tax, _row(ledger, "a")["local_id"], categoria="Moradia", necessidade="fixo")
    assert res["learned"] is None
    approve(ledger, tax, _row(ledger, "w1")["local_id"], categoria="Moradia")
    _sync(ledger, data, cfg, tax, today=date(2026, 10, 16))
    assert _row(ledger, "b")["review_status"] == "pendente"
    assert (_row(ledger, "w2")["categoria"], _row(ledger, "w2")["review_status"]) == ("Moradia", "auto")


def test_10_coincident_third_party_pix_only_suggested(ledger, cfg, tax):
    data = {NB: [tx("out", NB, "2026-10-10", -100.0, "PIX ENVIADO", op="PIX")],
            S: [tx("in", S, "2026-10-11", 100.0, "PIX RECEBIDO", op="PIX")]}
    _sync(ledger, data, cfg, tax)
    out = _row(ledger, "out")
    assert (out["rule"], out["review_status"]) == ("own_transfer", "pendente")


def test_11_pending_replaced_by_posted_with_other_amount(ledger, cfg, tax):
    data = {C: [tx("pend", C, "2026-10-10", 200.0, "POSTO SHELL", card=True, status="PENDING", ccm=FORECAST_NOV)]}
    _sync(ledger, data, cfg, tax)
    data[C] = [tx("post", C, "2026-10-10", 187.35, "POSTO SHELL", card=True, ccm=FORECAST_NOV)]
    res = _sync(ledger, data, cfg, tax, today=date(2026, 10, 16))
    assert res.stats["relinked"] == 1
    nov = [b for b in card_bills(ledger, cfg, date(2026, 10, 16)) if b.month == "2026-11"][0]
    assert nov.amount_cents == 18735


def test_12_sheets_written_raw_and_parcela_is_text(ledger, cfg, tax):
    from fin.sheets import build_tables, GSheetsPublisher

    t = tx("p3", C, "2026-10-01", 250.0, "LOJA PARC 03/10", card=True,
           ccm={"installmentNumber": 3, "totalInstallments": 10, "billForecastDate": "2026-11"})
    _sync(ledger, {C: [t]}, cfg, tax)
    lanc = build_tables(ledger, TODAY)["lancamentos"]
    assert lanc[1][lanc[0].index("parcela")] == "3/10"
    calls = []
    pub = GSheetsPublisher.__new__(GSheetsPublisher)
    pub.sh = SimpleNamespace(values_batch_update=lambda body: calls.append(body), values_batch_clear=lambda body: None,
                             worksheets=lambda: [])
    pub.write({"lancamentos": lanc})
    assert calls[0]["valueInputOption"] == "RAW"


def test_13_fixed_category_not_in_config_counts(ledger, cfg, tax):
    data = {C: [tx(f"n{m}", C, f"2026-0{m}-08", 55.9, "NETFLIX.COM", card=True,
                   ccm={"billForecastDate": f"2026-{m + 1:02d}"}) for m in (7, 8, 9)]}
    _sync(ledger, data, cfg, tax)
    ledger.add_dictionary({"kind": "contains", "pattern": "NETFLIX", "categoria": "Assinaturas", "origin": "user"})
    run_categorize(ledger, cfg, tax, None)
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert s.fixos_total == 5590


def test_14_late_bill_payment_still_pays_the_bill(ledger, cfg, tax):
    buy = tx("buy", C, "2026-09-20", 500.0, "LOJA X", card=True, ccm={"billForecastDate": "2026-10"})
    pay = tx("pay", C, "2026-10-12", -500.0, "Pagamento recebido", op="PAGAMENTO_FATURA", card=True, ccm=FORECAST_NOV)
    _sync(ledger, {C: [buy, pay]}, cfg, tax)
    oct_bill = [b for b in card_bills(ledger, cfg, date(2026, 10, 1)) if b.month == "2026-10"][0]
    assert oct_bill.open_cents == 0  # due 10/10 (Saturday), paid 12/10


def test_15_previous_bill_carryover_is_not_new_expense(ledger, cfg, tax):
    data = {C: [tx("sr", C, "2026-10-04", 800.0, "Saldo restante da fatura anterior", card=True, ccm=FORECAST_NOV),
                tx("j", C, "2026-10-04", 35.0, "Juros de rotativo", card=True, ccm=FORECAST_NOV)]}
    _sync(ledger, data, cfg, tax)
    run_categorize(ledger, cfg, tax, None)
    assert _row(ledger, "sr")["natureza"] == "Transferência"
    assert month_totals(ledger, "2026-11")["despesas"] == 3500


def test_16_bill_payment_entry_used_once(ledger, cfg, tax):
    bills = {C: [{"id": "b1", "dueDate": "2026-10-10T00:00:00.000Z", "totalAmount": 300.0, "payments": [
        {"paymentDate": "2026-10-08T00:00:00.000Z", "amount": 300.0}]}]}
    data = {I: [tx("x1", I, "2026-10-08", -300.0, "PAGTO FATURA CARTAO", op="BOLETO"),
                tx("x2", I, "2026-10-09", -300.0, "PAGTO FATURA CARTAO", op="BOLETO")]}
    _sync(ledger, data, cfg, tax, bills=bills)
    rules = sorted(_row(ledger, p)["rule"] for p in ("x1", "x2"))
    assert rules == ["bank_bill_payment", "bill_payment_unpaired"]


def test_17_generic_description_is_not_learned(ledger, cfg, tax):
    data = {I: [tx("b1", I, "2026-10-08", -90.0, "PAGAMENTO DE BOLETO", op="BOLETO"),
                tx("b2", I, "2026-10-09", -400.0, "PAGAMENTO DE BOLETO", op="BOLETO")]}
    _sync(ledger, data, cfg, tax)
    run_categorize(ledger, cfg, tax, None)
    res = approve(ledger, tax, _row(ledger, "b1")["local_id"], categoria="Moradia")
    assert res["learned"] is None and _row(ledger, "b2")["review_status"] == "pendente"


# ---- privacidade -----------------------------------------------------------------------------
def test_p1_person_transfers_without_payment_data_never_reach_llm(ledger, cfg, tax):
    data = {NB: [tx("p", NB, "2026-10-02", -50.0, "PIX ENVIADO WESLEY SOUZA"),
                 tx("t", NB, "2026-10-03", -60.0, "TED KAUA OLIVEIRA", op="OUTROS"),
                 tx("e", NB, "2026-10-04", -70.0, "TRANSF wsouza@gmail.com")],
            C: [tx("c", C, "2026-10-05", 80.0, "PIX NO CREDITO WESLEY SOUZA", card=True, ccm=FORECAST_NOV),
                tx("ok", C, "2026-10-05", 30.0, "PADARIA EXEMPLO", card=True, ccm=FORECAST_NOV)]}
    _sync(ledger, data, cfg, tax)
    fake = OfflineAnthropicClient()
    run_categorize(ledger, cfg, tax, AnthropicCategorizer(fake, ledger, block_terms=cfg.block_terms))
    sent = json.dumps([r["messages"] for r in fake.requests])
    for name in ("WESLEY", "SOUZA", "KAUA", "OLIVEIRA", "GMAIL"):
        assert name not in sent
    assert "PADARIA EXEMPLO" in sent
    assert {_row(ledger, p)["rule"] for p in ("p", "t", "e", "c")} == {"person"}


def test_p2_raw_description_is_checked_before_sending(ledger, cfg, tax):
    data = {C: [tx("m", C, "2026-10-05", 30.0, "LOJA x@gmail.com", card=True, ccm=FORECAST_NOV),
                tx("o", C, "2026-10-05", 30.0, "LOJA CPF 1l1.111.1l1-11", card=True, ccm=FORECAST_NOV)]}
    _sync(ledger, data, cfg, tax)
    rec = Recording()
    run_categorize(ledger, cfg, tax, rec)
    assert rec.items == []
    assert {_row(ledger, p)["cat_source"] for p in ("m", "o")} == {"blocked"}


@pytest.mark.parametrize("desc", ["MACEDO MATERIAIS", "SILVA SOUZA COMERCIO", "DONA ZEZE QUITANDA", "CONCEICAO DOCES"])
def test_p3_surnames_terms_and_address_words(desc):
    assert block_reason(desc, ["joao souza", "conceição"]) is not None


def test_p3_ordinary_merchant_still_allowed():
    assert block_reason("SUPERMERCADO EXEMPLO", ["joao souza"]) is None


def test_p5_sheets_never_get_raw_description(ledger, cfg, tax):
    from fin.sheets import build_tables

    _sync(ledger, {C: [tx("d", C, "2026-10-05", 30.0, "12345678901", card=True, ccm=FORECAST_NOV)]}, cfg, tax)
    lanc = build_tables(ledger, TODAY)["lancamentos"]
    assert "12345678901" not in json.dumps(lanc)


def test_p7_llm_free_text_is_a_short_label_only():
    items = [{"id": "d1", "desc": "X", "tipo": "debito"}, {"id": "d2", "desc": "Y", "tipo": "debito"}]
    out = validate([{"id": "d1", "categoria": "Lazer", "subcategoria": "=HYPERLINK(\"http://x\")",
                     "estabelecimento_normalizado": "X", "confianca": 0.9},
                    {"id": "d2", "categoria": "Lazer", "subcategoria": "Cinema", "estabelecimento_normalizado": "Y",
                     "confianca": 0.9}], items, ["Lazer", "Revisar"])
    assert out[0]["subcategoria"] == "" and out[1]["subcategoria"] == "Cinema"


def test_p11_telegram_hides_blocked_descriptions(ledger, cfg, tax):
    from fin.ledger import Ledger
    from fin.telegram_bot import Handlers

    _sync(ledger, {C: [tx("m", C, "2026-10-05", 30.0, "MARIA DOCES", card=True, ccm=FORECAST_NOV)]}, cfg, tax)
    run_categorize(ledger, cfg, tax, Recording())
    out = Handlers(lambda: Ledger(ledger.path), cfg, tax)("/pendentes", [])
    assert "MARIA" not in out and "bloqueado" in out


# ---- operação --------------------------------------------------------------------------------
def test_o1_unmounted_vault_alerts_on_telegram(tmp_path, monkeypatch):
    import fin.cli as cli

    cfg = make_cfg(tmp_path, require_mount=str(tmp_path / "no-volume"))
    sent = []
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "_notify", lambda c, text: sent.append(text))
    args = SimpleNamespace(no_notify=False, no_categorize=True, no_publish=True)
    assert cli.cmd_sync(args) == 3
    assert sent and "cofre" in sent[0] and not (tmp_path / "data").exists()


def test_o2_network_down_on_auth_is_retried():
    from fin.pluggy import HttpPluggyClient

    calls = {"auth": 0}

    def handler(request):
        if request.url.path == "/auth":
            calls["auth"] += 1
            if calls["auth"] == 1:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"apiKey": "k"})
        return httpx.Response(200, json={"id": "it"})

    c = HttpPluggyClient("id", "secret", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.get_item("it") == {"id": "it"} and calls["auth"] == 2


def test_o2_network_error_marks_partial_and_closes_run(ledger, cfg, tax):
    class Down:
        def get_item(self, item_id):
            raise httpx.ConnectError("down")

    res = run_sync(ledger, Down(), cfg, tax, today=TODAY, now=NOW)
    assert res.status == "error" and any("conexão" in a for a in res.alerts)
    assert ledger.last_run()["status"] == "error"


def test_o2_unexpected_error_still_finishes_run(ledger, cfg, tax):
    class Boom:
        def get_item(self, item_id):
            raise KeyError("x")

    with pytest.raises(KeyError):
        run_sync(ledger, Boom(), cfg, tax, today=TODAY, now=NOW)
    assert ledger.last_run()["status"] == "error"


def test_o3_data_dir_outside_vault_refused(tmp_path):
    from fin.config import VaultUnmounted

    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_cfg(tmp_path, data_dir=str(tmp_path / "elsewhere"), require_mount=str(vault))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("os.path.ismount", lambda p: True)  # pretend the vault is mounted
        with pytest.raises(VaultUnmounted):
            cfg.ensure_data_dir()
    assert not (tmp_path / "elsewhere").exists()


def test_o8_bot_survives_network_errors(monkeypatch):
    import fin.telegram_bot as bot

    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    monkeypatch.setattr(bot.signal, "signal", lambda *a: None)
    state = {"calls": 0}

    class Api:
        def call(self, method, payload):
            state["calls"] += 1
            if method == "deleteWebhook" and state["calls"] == 1:
                raise httpx.ConnectError("boot sem rede")
            if method == "getUpdates" and state["calls"] == 3:
                return {"ok": True, "result": [{"update_id": 1, "message": {
                    "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/ajuda", "date": int(bot.time.time())}}]}
            return {"ok": False, "error_code": 401}

        def send(self, chat_id, text):
            raise httpx.ConnectError("caiu ao responder")

    bot.run_bot(Api(), bot.Allowlist(1, 1), lambda cmd, args: "ok")  # returns on 401, never raises
    assert state["calls"] == 4


def test_o9_bot_without_secrets_exits_zero(tmp_path, monkeypatch):
    import fin.cli as cli

    monkeypatch.setattr(cli, "load_config", lambda: make_cfg(tmp_path))
    assert cli.cmd_bot(SimpleNamespace()) == 0  # KeepAlive SuccessfulExit=false: no restart loop


def test_o14_any_keychain_error_is_a_clear_message(monkeypatch):
    import keyring
    import keyring.errors

    from fin import secrets

    def boom(*a):
        raise keyring.errors.InitError("denied")

    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(secrets.SecretMissing):
        secrets.get("telegram_bot_token")


def test_privacy_glued_and_acquirer_names():
    from fin.privacy import block_reason
    for txt in ["ROBERTOLUIZV", "JULIANASILVADE", "MP CARLADEALMEIDAPAU", "JIM BARROS TEL L",
                "RICARDO CESAR DA SILV", "RAFAEL S MIRANDA", "PATRICIAKELLY", "BRUNAALONSODA"]:
        assert block_reason(txt), txt
    for txt in ["AMAZON SERVICOS DE VAR", "IFOOD CLUB", "KINOPLEX TOP SHOPPING", "RAIA DROGASIL S A",
                "UBER UBER TRIP HELP U", "SUPERMERCADO VIANENSE"]:
        assert block_reason(txt) is None, txt


def test_real_rotativo_carryover_and_pix_on_credit(ledger, cfg, tax):
    data = {C: [tx("rot", C, "2026-10-04", 3900.22, "SALDO EM ROTATIVO", card=True, ccm=FORECAST_NOV),
                tx("pxc", C, "2026-10-05", 149.29, "FULANA SILVA COSTA", card=True, ccm=FORECAST_NOV,
                   op="OPERACOES_CREDITO_CONTRATADAS_CARTAO")]}
    _sync(ledger, data, cfg, tax)
    run_categorize(ledger, cfg, tax, None)
    assert _row(ledger, "rot")["natureza"] == "Transferência"
    assert _row(ledger, "pxc")["rule"] == "card_credit_op"
    assert _row(ledger, "pxc")["categoria"] == "Revisar"


def test_real_same_bank_bill_and_debt_payment(ledger, cfg, tax):
    data = {C: [tx("cp", C, "2026-10-08", -500.0, "Pagamento recebido", card=True, ccm=FORECAST_NOV)],
            NB: [tx("bp", NB, "2026-10-08", -500.0, "PAGAMENTO DE FATURA", op="OUTROS"),
                tx("rn", NB, "2026-10-09", -232.86, "PAGA RENEGOCIACAO", op="OPERACAO_CREDITO")]}
    _sync(ledger, data, cfg, tax)
    run_categorize(ledger, cfg, tax, None)
    assert _row(ledger, "bp")["natureza"] == "Transferência"
    assert _row(ledger, "rn")["rule"] == "debt_payment"


def test_month_tabs_by_calendar_date(ledger, cfg, tax):
    from fin.sheets import build_month_tabs
    data = {I: [tx("a", I, "2026-09-01", -10.0, "PADARIA X"), tx("b", I, "2026-09-30", -20.0, "PADARIA X"),
                tx("c", I, "2026-10-01", -5.0, "PADARIA X")]}
    _sync(ledger, data, cfg, tax)
    tabs = build_month_tabs(ledger, date(2026, 10, 15))
    assert list(tabs)[0] == "Janeiro 2026" and list(tabs)[-2:] == ["Setembro 2026", "Outubro 2026"]
    set_rows = [r for r in tabs["Setembro 2026"][5:]]
    assert [r[0] for r in set_rows] == ["2026-09-01", "2026-09-30"]
    assert len(tabs["Outubro 2026"][5:]) == 1


def test_painel_fixed_layout_and_charts(ledger, cfg, tax):
    from fin.sheets import build_painel, painel_chart_requests, PAINEL_COLS, PAINEL_ROWS
    data = {I: [tx("a", I, "2026-10-02", -100.0, "PADARIA X"), tx("b", I, "2026-09-05", -50.0, "PADARIA X")]}
    _sync(ledger, data, cfg, tax)
    g = build_painel(ledger, date(2026, 10, 15))
    assert len(g) == PAINEL_ROWS and all(len(r) == PAINEL_COLS for r in g)
    assert g[5][:3] == ["mes", "receitas_pf", "despesas_pf"]
    assert g[6][0] == "2026-01" and g[6 + 9][0] == "2026-10" and g[6 + 9][2] == 100.0
    reqs = painel_chart_requests(123)
    assert len(reqs) == 5 and all("addChart" in r for r in reqs)


def test_dashboard_blocks_and_formulas(ledger, cfg, tax):
    from fin.dashboard import BLOCK_H, build_dash_data, formula_cells, OPTIONS_ROW, setup_requests
    data = {I: [tx("a", I, "2026-10-02", -100.0, "PADARIA X"), tx("b", I, "2026-10-09", 1000.0, "SALARIO")]}
    _sync(ledger, data, cfg, tax)
    rows = build_dash_data(ledger, date(2026, 10, 15))
    assert rows[0][:4] == ["periodo", "receitas", "despesas", "saldo"]
    assert rows[1][0] == "01–07/10" and rows[1][2] == 100.0          # Semanal · PF, week 1
    mensal_pf = 3 * BLOCK_H
    assert rows[mensal_pf + 1][0] == "2026-01" and rows[mensal_pf + 10][0] == "2026-10"
    assert rows[OPTIONS_ROW][0] == "Semanal" and rows[OPTIONS_ROW][1] == "PF"
    assert all(len(r) == 5 for r in rows)
    f = formula_cells()
    assert f["G1"].startswith("=IFERROR((MATCH(Dashboard!$C$3;")
    assert sum("addChart" in r for r in setup_requests(1, 2)) == 4


def test_open_bill_anchored_to_bank_balance_when_higher(ledger, cfg, tax):
    data = {C: [tx("p1", C, "2026-10-02", 100.0, "LOJA X", card=True, ccm=FORECAST_NOV)]}
    _sync(ledger, data, cfg, tax)
    acc = ledger.one("SELECT id FROM accounts WHERE type='CREDIT'")["id"]
    ledger.x("UPDATE accounts SET balance_cents=900000 WHERE id=?", (acc,))  # bank: R$ 9.000 still owed
    bills = card_bills(ledger, cfg, date(2026, 10, 15))
    assert max(b.open_cents for b in bills) >= 800000


def test_card_payment_listed_under_two_bills_counts_once(ledger, cfg, tax):
    from fin.painel import posicao_em

    data = {C: [tx("pay", C, "2026-10-08", -700.0, "Pagamento recebido", card=True, ccm=FORECAST_NOV),
                tx("dup", C, "2026-10-08", -700.0, "Pagamento recebido", card=True, ccm=FORECAST_NOV),
                tx("buy", C, "2026-10-09", 200.0, "LOJA X", card=True, ccm=FORECAST_NOV)],
            I: [tx("bol", I, "2026-10-08", -700.0, "PAGTO FATURA CARTAO", op="BOLETO", method="BOLETO",
                   receiver=party("EMISSOR", CNPJ_CARD, "999"))]}
    _sync(ledger, data, cfg, tax)
    rules = {_row(ledger, p)["rule"] for p in ("pay", "dup")}
    assert rules == {"card_bill_payment", "card_payment_duplicate"}
    # contas 6800 and card 1000 today; on 07/10 the bank had the 700 still and the card owed 1000 - 200 + 700
    sem_extras = lambda d: {k: d[k] for k in ("contas", "cartao", "posicao")}
    assert sem_extras(posicao_em(ledger, TODAY, TODAY)) == {"contas": 6800.0, "cartao": 1000.0, "posicao": 5800.0}
    assert sem_extras(posicao_em(ledger, date(2026, 10, 7), TODAY)) == {"contas": 7500.0, "cartao": 1500.0, "posicao": 6000.0}


def test_two_real_payments_of_the_same_value_are_not_duplicates(ledger, cfg, tax):
    pays = [tx(f"pay{k}", C, "2026-09-04", -150.0, "Pagamento recebido", card=True, ccm=FORECAST_NOV) for k in (1, 2)]
    bols = [tx(f"bol{k}", I, "2026-09-04", -150.0, "PAGTO FATURA CARTAO", op="BOLETO", method="BOLETO",
               receiver=party("EMISSOR", CNPJ_CARD, "999")) for k in (1, 2)]
    _sync(ledger, {C: pays, I: bols}, cfg, tax)
    assert {_row(ledger, p)["rule"] for p in ("pay1", "pay2")} == {"card_bill_payment"}


def test_month_close_reconciles_with_the_net_position_to_the_cent(ledger, cfg, tax):
    from fin.painel import compute_periodo

    data = {C: [tx("sal", C, "2026-10-03", 1545.51, "Saldo em rotativo", card=True, ccm=FORECAST_NOV),
                tx("cred", C, "2026-10-03", -1545.51, "Crédito de rotativo", card=True, ccm=FORECAST_NOV),
                tx("buy", C, "2026-10-04", 90.0, "LOJA X", card=True, ccm=FORECAST_NOV)],
            NB: [tx("loan", NB, "2026-10-02", 1000.0, "Depósito de empréstimo", op="OPERACAO_CREDITO"),
                 tx("parc", NB, "2026-10-06", -233.89, "Parcela Paga | Renegociação", op="OPERACAO_CREDITO"),
                 tx("fora", NB, "2026-10-07", 303.47, "Transferência Recebida", receiver=OWN_ITAU, payer=OWN_ITAU),
                 tx("pao", NB, "2026-10-08", -12.0, "PADARIA EXEMPLO")]}
    _sync(ledger, data, cfg, tax)
    assert {_row(ledger, p)["rule"] for p in ("sal", "cred")} == {"card_carryover"}  # the pair is neutral
    assert (_row(ledger, "loan")["rule"], _row(ledger, "loan")["natureza"]) == ("loan_received", "Transferência")
    p = compute_periodo(ledger, {}, date(2026, 10, 1), date(2026, 11, 1), date(2026, 9, 1), date(2026, 10, 1), TODAY)
    e, a = p["entradas"], p["ajustes"]
    resultado = e["clt"] + e["pj"] + e["outros"] - p["resumo"]["despesas"] - e["custosPj"]
    variacao = p["posicao"]["fim"]["posicao"] - p["posicao"]["inicio"]["posicao"]
    assert a["emprestimos"] == round(1000 - 233.89, 2) and a["naoExplicado"] == 0
    assert round(resultado + sum(v for k, v in a.items() if k != "naoExplicado"), 2) == round(variacao, 2)


def test_loan_investment_and_installments_enter_the_position(ledger, cfg, tax):
    import conftest
    from fin.painel import compute_periodo, posicao_em
    from fin.pluggy import MemoryPluggyClient

    for it in cfg.items:
        if it["bank"] == "nubank":
            it["bank_item_id"] = "BANCO_NU"
    parc = lambda n, day: tx(f"p{n}", C, day, 100.0, f"LOJA X {n}/3", card=True,
                             ccm={"installmentNumber": n, "totalInstallments": 3, "purchaseDate": "2026-09-05",
                                  "billForecastDate": day[:7]})
    data = {I: [], S: [], C: [parc(1, "2026-09-05"), parc(2, "2026-10-05"), parc(3, "2026-11-05")],
            NB: [tx("dep", NB, "2026-08-01", 1000.0, "Depósito de empréstimo", op="OPERACAO_CREDITO"),
                 tx("q1", NB, "2026-09-01", -250.0, "Parcela Paga | Renegociação", op="OPERACAO_CREDITO"),
                 tx("q2", NB, "2026-10-01", -250.0, "Parcela Paga | Renegociação", op="OPERACAO_CREDITO"),
                 tx("apl", NB, "2026-09-10", -300.0, "Aplicação RDB")]}
    loan = {"id": "L1", "productName": "Credito Pessoal", "contractDate": "2026-08-01", "contractAmount": 1000, "CET": 0,
            "firstInstallmentDueDate": "2026-09-01", "settlementDate": None, "nextInstallmentAmount": 250,
            "installments": {"totalNumberOfInstallments": 4, "paidInstallments": 2},
            "payments": {"contractOutstandingBalance": 500}}
    inv = {"id": "V1", "type": "FIXED_INCOME", "subtype": "CDB", "name": "CDB", "balance": 300, "status": "ACTIVE"}
    client = MemoryPluggyClient({iid: conftest.item(iid) for iid in conftest.ITEMS.values()}, conftest.accounts(), data,
                                loans={"BANCO_NU": [loan]}, investments={"BANCO_NU": [inv]})
    run_sync(ledger, client, cfg, tax, today=TODAY, now=NOW)
    assert _row(ledger, "apl")["natureza"] == "Investimento"

    # end of August: no installment purchase yet, nothing invested, whole loan owed
    ago = posicao_em(ledger, date(2026, 8, 31), TODAY)
    assert (ago["cartao"], ago["investimentos"], ago["emprestimos"]) == (700.0, 0.0, 1000.0)
    # end of September: the 3x purchase weighs whole on the card, 300 invested, one installment paid
    st = posicao_em(ledger, date(2026, 9, 30), TODAY)
    assert (st["cartao"], st["investimentos"], st["emprestimos"]) == (1000.0, 300.0, 750.0)
    assert posicao_em(ledger, TODAY, TODAY)["emprestimos"] == 500.0  # today: what the bank reports

    p = compute_periodo(ledger, {}, date(2026, 9, 1), date(2026, 10, 1), date(2026, 8, 1), date(2026, 9, 1), TODAY)
    e, a = p["entradas"], p["ajustes"]
    resultado = e["clt"] + e["pj"] + e["outros"] - p["resumo"]["despesas"] - e["custosPj"]
    assert resultado == -100.0  # only the first installment is September spending
    assert (a["parcelado"], a["emprestimos"], a["aplicacoes"], a["naoExplicado"]) == (-200.0, 0.0, 0.0, 0.0)
    assert round(p["posicao"]["fim"]["posicao"] - p["posicao"]["inicio"]["posicao"], 2) == -300.0


def test_automatic_sweep_balance_comes_from_the_rows(ledger, cfg, tax):
    from fin.painel import posicao_em

    for it in cfg.items:
        if it["bank"] == "itau":
            it["aplicacao_automatica"] = "true"
    data = {I: [tx("a1", I, "2026-10-02", -2500.0, "Aplicação APL APLIC AUT MAIS"),
                tx("r1", I, "2026-10-08", 2400.0, "Resgate RES APLIC AUT MAIS")]}
    _sync(ledger, data, cfg, tax)
    assert posicao_em(ledger, date(2026, 10, 1), TODAY)["investimentos"] == 0.0
    assert posicao_em(ledger, date(2026, 10, 5), TODAY)["investimentos"] == 2500.0  # applied, still yours
    assert posicao_em(ledger, TODAY, TODAY)["investimentos"] == 100.0
    # the money going in and out of the sweep does not move the position
    assert posicao_em(ledger, date(2026, 10, 5), TODAY)["posicao"] == posicao_em(ledger, date(2026, 10, 1), TODAY)["posicao"]


def test_card_cycle_warns_when_the_pace_does_not_fit_the_plan(ledger, cfg, tax):
    from fin.config import CardConfig
    from fin.painel import compute_ciclo_cartao

    cfg.cards["nubank"] = CardConfig(closing_day=17, due_day=24)
    nov = {"billForecastDate": "2026-11"}
    data = {C: [tx("old", C, "2026-10-23", 100.0, "LOJA A 2/3", card=True,
                   ccm={**nov, "installmentNumber": 2, "totalInstallments": 3, "purchaseDate": "2026-09-23"}),
                tx("n1", C, "2026-10-18", 150.0, "MERCADO", card=True, ccm=nov),
                tx("n2", C, "2026-10-20", 150.0, "POSTO", card=True, ccm=nov)]}
    _sync(ledger, data, cfg, tax, today=date(2026, 10, 22))
    plano = {"plano": [{"data": "2026-10-24", "descricao": "Cartão (outubro)", "sai": 3900},
                       {"data": "2026-11-24", "descricao": "Fatura do cartão", "sai": 1000}]}
    c = compute_ciclo_cartao(ledger, cfg, plano, date(2026, 10, 22))
    assert (c["inicio"], c["fechamento"], c["teto"]) == ("2026-10-18", "2026-11-17", 1000.0)  # November's item, not October's
    assert (c["comprometido"], c["novo"], c["atual"]) == (100.0, 300.0, 400.0)  # the later installment was already there
    # 300 in 5 of 31 days -> 1860 by the closing day, plus the 100 already committed: over the 1000 the plan has
    assert (c["projecao"], c["status"]) == (1960.0, "atencao")
    assert c["porDia"] == 23.07  # 600 over 26 days, rounded down: never more than fits


def test_card_cycle_reserves_the_monthly_charges_still_to_come(ledger, cfg, tax):
    from fin.config import CardConfig
    from fin.painel import compute_ciclo_cartao

    cfg.cards["nubank"] = CardConfig(closing_day=17, due_day=24)
    sub = lambda tid, day, comp, v, desc: tx(tid, C, day, v, desc, card=True, ccm={"billForecastDate": comp})
    data = {C: [sub("s8", "2026-08-07", "2026-08", 39.9, "STREAMING X"), sub("s9", "2026-09-07", "2026-09", 39.9, "STREAMING X"),
                sub("c9", "2026-08-30", "2026-09", 149.9, "CAFE CLUBE"), sub("c10", "2026-09-30", "2026-10", 149.9, "CAFE CLUBE"),
                sub("u9", "2026-09-10", "2026-09", 30.0, "APP CORRIDA"), sub("u10", "2026-10-02", "2026-10", 12.0, "APP CORRIDA"),
                sub("x10", "2026-10-03", "2026-10", 500.0, "LOJA UNICA"),
                sub("s11", "2026-10-19", "2026-11", 39.9, "STREAMING X")]}  # already in the open bill: not reserved again
    _sync(ledger, data, cfg, tax, today=date(2026, 10, 20))
    plano = {"plano": [{"data": "2026-11-24", "descricao": "Fatura do cartão", "sai": 300}]}
    c = compute_ciclo_cartao(ledger, cfg, plano, date(2026, 10, 20))
    # steady once a month -> reserved; varying rides and a one-off purchase -> not
    assert [(x["valor"], x["dia"]) for x in c["previstas"]] == [(149.9, 30)]
    assert (c["atual"], c["reservado"]) == (39.9, 149.9)
    assert c["porDia"] == 3.93  # (300 - 39.90 - 149.90) / 28 days, rounded down
    assert c["status"] == "ok"
