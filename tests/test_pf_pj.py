"""PF x PJ (MEI): owner per Pluggy item, PF<->PJ transfers, PJ categories, reports, bot and Sheets.
Numbers checked by hand in the comments. Everything is synthetic."""

import copy
import sqlite3

import pytest
from conftest import accounts, C, I, ITEMS, make_cfg, NOW, OWN_CPF, OWN_ITAU, party, TODAY, tx

from fin.categorize import run_categorize
from fin.config import config_from_dict, ConfigError, load_config, PROJECT_ROOT
from fin.demo import ACC_PJ, CNPJ_MEI, CNPJ_PJ, item, ITEM_PJ, load_seed, pj_account
from fin.ledger import Ledger, MIGRATIONS
from fin.pluggy import MemoryPluggyClient
from fin.reports import can_i_buy, format_summary, month_totals, monthly_summary
from fin.sync import parse_account, run_sync

OWN_PJ = party("MEI EXEMPLO", CNPJ_MEI, "260", "5550001-2", "0001")
TO_PJ = party("MEI EXEMPLO", CNPJ_MEI, "260", "5550001-2")
PJ_ITEM = {"bank": "nubank_pj", "item_id": ITEM_PJ, "owner": "pj"}


def pj_cfg(tmp_path, owner="pj", **overrides):
    items = [{"bank": b, "item_id": iid} for b, iid in ITEMS.items()] + [{**PJ_ITEM, "owner": owner}]
    return make_cfg(tmp_path, pluggy={"items": items}, **overrides)


def pj_client(txs, pj_balance=2750.0, pj_tax=CNPJ_MEI):
    accs = accounts()
    acc = pj_account(pj_balance)
    acc["taxNumber"] = pj_tax
    accs[ITEM_PJ] = [acc]
    items = {iid: item(iid) for iid in [*ITEMS.values(), ITEM_PJ]}
    return MemoryPluggyClient(items, accs, copy.deepcopy({k: [] for k in (I, C, ACC_PJ)} | txs), {})


def row(ledger, pid):
    return ledger.one("SELECT * FROM transactions WHERE pluggy_id=?", (pid,))


def scenario(tmp_path, ledger, tax):
    """October 2026, by hand:
    PF  Itaú: salário +8.500 (Empresa CLT) · retirada da PJ +3.000 · tarifa -19,90 · aporte na PJ -200
        cartão: supermercado 400 (fatura 2026-10)
    PJ  cliente +6.000 (Cliente PJ, categoria "PJ" no config -> Faturamento PJ) · DAS -80,90 · tarifa Pix -2,50
        retirada -3.000 para o Itaú · aporte +200 vindo do Itaú
    """
    cfg = pj_cfg(tmp_path, income={"salary_day": 5, "sources": [
        {"cnpj": "11111111000111", "fonte": "Empresa CLT", "categoria": "Salário CLT"},
        {"cnpj": "22222222000122", "fonte": "Cliente PJ", "categoria": "PJ"}]})
    data = {
        I: [tx("sal", I, "2026-10-05", 8500.0, "SALARIO", op="FOLHA_PAGAMENTO", method="TED",
               payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU),
            tx("ret_in", I, "2026-10-08", 3000.0, "PIX RECEBIDO MEI EXEMPLO", op="PIX", method="PIX",
               payer=TO_PJ, receiver=OWN_ITAU),
            tx("tar", I, "2026-10-10", -19.9, "TARIFA PACOTE", op="PACOTE_TARIFA_SERVICOS"),
            tx("apo_out", I, "2026-10-12", -200.0, "PIX ENVIADO MEI EXEMPLO", op="PIX", method="PIX",
               payer=OWN_ITAU, receiver=TO_PJ)],
        C: [tx("mkt", C, "2026-09-20", 400.0, "SUPERMERCADO EXEMPLO", card=True, ccm={"billForecastDate": "2026-10"})],
        ACC_PJ: [
            tx("cli", ACC_PJ, "2026-10-06", 6000.0, "PIX RECEBIDO CLIENTE PJ", op="PIX", method="PIX",
               payer=party("CLIENTE PJ", CNPJ_PJ, "237"), receiver=OWN_PJ),
            tx("tarpj", ACC_PJ, "2026-10-06", -2.5, "TARIFA PIX", op="TARIFA_SERVICOS_AVULSOS"),
            tx("ret_out", ACC_PJ, "2026-10-08", -3000.0, "PIX ENVIADO TITULAR", op="PIX", method="PIX",
               payer=OWN_PJ, receiver=party("TITULAR", OWN_CPF, "341", "12345-6")),
            tx("apo_in", ACC_PJ, "2026-10-12", 200.0, "PIX RECEBIDO TITULAR", op="PIX", method="PIX",
               payer=OWN_ITAU, receiver=OWN_PJ),
            tx("das", ACC_PJ, "2026-10-20", -80.9, "PAGAMENTO DAS-SIMEI 10/2026", op="CONVENIO_ARRECADACAO")],
    }
    run_sync(ledger, pj_client(data), cfg, tax, today=TODAY, now=NOW)
    load_seed(ledger)
    run_categorize(ledger, cfg, tax, None)
    return cfg


# ---- 1. config, ledger, local_key -----------------------------------------------------------
def test_owner_in_config_defaults_to_pf_and_validates(tmp_path):
    cfg = pj_cfg(tmp_path)
    assert [i["owner"] for i in cfg.items] == ["pf", "pf", "pf", "pj"] and cfg.has_pj
    assert cfg.owner_of_item(ITEM_PJ) == "pj" and cfg.owner_of_item(ITEMS["itau"]) == "pf"
    with pytest.raises(ConfigError):
        config_from_dict({"data_dir": str(tmp_path), "pluggy": {"items": [{"bank": "x", "item_id": "1", "owner": "cnpj"}]}})
    example = load_config(PROJECT_ROOT / "config.example.yaml")
    assert {i["bank"]: i["owner"] for i in example.items}["nubank_pj"] == "pj"


def test_migration_adds_owner_to_existing_ledger(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0] + "; PRAGMA user_version=1;")
    conn.execute("INSERT INTO accounts(id,item_id,bank,type,local_key,updated_at) VALUES('a','i','itau','BANK','itau:X:1','t')")
    conn.commit()
    conn.close()
    lg = Ledger(path)
    assert lg.one("PRAGMA user_version")[0] == len(MIGRATIONS)
    assert lg.account("a")["owner"] == "pf" and lg.account_owners() == {"a": "pf"} and not lg.has_pj()
    lg.close()


def test_local_key_separates_nubank_pf_from_nubank_pj():
    acc = pj_account()
    pf, pj = parse_account(acc, "nubank", "salt", "pf"), parse_account(acc, "nubank", "salt", "pj")
    assert pf["local_key"] == "nubank:CHECKING_ACCOUNT:0012"      # PF keeps the v1 format
    assert pj["local_key"] == "nubank:pj:CHECKING_ACCOUNT:0012" and pj["owner"] == "pj"


def test_switching_item_to_pj_keeps_history_without_duplicates(tmp_path, ledger, tax):
    data = {ACC_PJ: [tx("das", ACC_PJ, "2026-10-20", -80.9, "PAGAMENTO DAS-SIMEI", op="CONVENIO_ARRECADACAO")]}
    run_sync(ledger, pj_client(data), pj_cfg(tmp_path, owner="pf"), tax, today=TODAY, now=NOW)
    assert row(ledger, "das")["account_local_key"] == "nubank_pj:CHECKING_ACCOUNT:0012"
    r2 = run_sync(ledger, pj_client(data), pj_cfg(tmp_path), tax, today=TODAY, now=NOW)
    assert r2.stats["inserted"] == 0
    assert ledger.one("SELECT COUNT(*) n FROM transactions")["n"] == 1
    assert row(ledger, "das")["account_local_key"] == "nubank_pj:pj:CHECKING_ACCOUNT:0012"
    assert ledger.account(ACC_PJ)["owner"] == "pj"


# ---- 2/3. rules and taxonomy ----------------------------------------------------------------
def test_pf_pj_transfers_are_paired_and_labeled(tmp_path, ledger, tax):
    scenario(tmp_path, ledger, tax)
    ret_in, ret_out = row(ledger, "ret_in"), row(ledger, "ret_out")
    apo_in, apo_out = row(ledger, "apo_in"), row(ledger, "apo_out")
    assert ret_in["pair_id"] and ret_in["pair_id"] == ret_out["pair_id"]
    assert apo_in["pair_id"] and apo_in["pair_id"] == apo_out["pair_id"]
    for r in (ret_in, ret_out):
        assert (r["natureza"], r["categoria"], r["review_status"]) == ("Transferência", "Retirada PJ→PF", "auto")
    for r in (apo_in, apo_out):
        assert (r["natureza"], r["categoria"], r["review_status"]) == ("Transferência", "Aporte PF→PJ", "auto")


def test_pj_categories_income_das_and_fees(tmp_path, ledger, tax):
    scenario(tmp_path, ledger, tax)
    cli = row(ledger, "cli")
    assert (cli["natureza"], cli["categoria"], cli["fonte"], cli["rule"]) == ("Receita", "Faturamento PJ", "Cliente PJ", "income_source")
    assert (row(ledger, "das")["categoria"], row(ledger, "das")["cat_source"]) == ("Impostos/DAS", "dictionary")
    assert row(ledger, "tarpj")["categoria"] == "Taxas bancárias PJ"
    assert row(ledger, "tar")["categoria"] == "Impostos/Taxas"          # PF fee unchanged


def test_client_cnpj_in_pf_account_keeps_configured_category(tmp_path, ledger, tax):
    cfg = pj_cfg(tmp_path)
    data = {I: [tx("cli", I, "2026-10-06", 6000.0, "PIX RECEBIDO CLIENTE PJ", op="PIX", method="PIX",
                   payer=party("CLIENTE PJ", CNPJ_PJ, "237"), receiver=OWN_ITAU)]}
    run_sync(ledger, pj_client(data), cfg, tax, today=TODAY, now=NOW)
    assert row(ledger, "cli")["categoria"] == "PJ"


@pytest.mark.parametrize("desc,cat", [("PAGAMENTO DAS-SIMEI", "Impostos/DAS"), ("PGMEI 2026", "Impostos/DAS"),
                                      ("DAS 202610", "Impostos/DAS"), ("SIMPLES NACIONAL DAS", "Impostos/DAS"),
                                      ("LOJA DAS FLORES", "Revisar")])
def test_das_seed_dictionary(tmp_path, ledger, tax, desc, cat):
    cfg = pj_cfg(tmp_path, llm={"enabled": False})
    run_sync(ledger, pj_client({ACC_PJ: [tx("x", ACC_PJ, "2026-10-20", -80.9, desc, op="BOLETO")]}), cfg, tax,
             today=TODAY, now=NOW)
    load_seed(ledger)
    run_categorize(ledger, cfg, tax, None)
    assert row(ledger, "x")["categoria"] == cat


def test_unpaired_transfer_labeled_by_counterpart_owner(tmp_path, ledger, tax):
    # only the PJ side arrived (the PF side is outside the 2-day window): still a Retirada, by the CPF/account
    data = {ACC_PJ: [tx("ret", ACC_PJ, "2026-10-08", -3000.0, "PIX ENVIADO TITULAR", op="PIX", method="PIX",
                        payer=OWN_PJ, receiver=party("TITULAR", OWN_CPF, "341", "12345-6"))]}
    run_sync(ledger, pj_client(data), pj_cfg(tmp_path), tax, today=TODAY, now=NOW)
    r = row(ledger, "ret")
    assert (r["natureza"], r["categoria"], r["pair_id"]) == ("Transferência", "Retirada PJ→PF", None)


def test_mei_account_with_cpf_needs_the_account_to_tell_owner(tmp_path, ledger, tax):
    # PJ account opened under the CPF: the CPF alone does not say PF or PJ; the account number does
    data = {I: [tx("a", I, "2026-10-12", -200.0, "PIX ENVIADO", op="PIX", method="PIX", payer=OWN_ITAU,
                   receiver=party("TITULAR", OWN_CPF, "260")),
                tx("b", I, "2026-10-13", -300.0, "PIX ENVIADO", op="PIX", method="PIX", payer=OWN_ITAU,
                   receiver=party("TITULAR", OWN_CPF, "260", "5550001-2"))]}
    run_sync(ledger, pj_client(data, pj_tax=OWN_CPF), pj_cfg(tmp_path), tax, today=TODAY, now=NOW)
    assert row(ledger, "a")["categoria"] == "Transferência"
    assert row(ledger, "b")["categoria"] == "Aporte PF→PJ"


def test_pf_to_pf_transfer_is_still_plain(tmp_path, ledger, tax):
    from conftest import S

    data = {I: [tx("o", I, "2026-10-10", -700.0, "PIX ENVIADO", op="PIX", method="PIX", payer=OWN_ITAU,
                   receiver=party("TITULAR", OWN_CPF, "033", "99887-1"))],
            S: [tx("i", S, "2026-10-10", 700.0, "PIX RECEBIDO", op="PIX", method="PIX",
                   payer=party("TITULAR", OWN_CPF, "341", "12345-6"))]}
    run_sync(ledger, pj_client(data), pj_cfg(tmp_path), tax, today=TODAY, now=NOW)
    assert row(ledger, "o")["categoria"] == row(ledger, "i")["categoria"] == "Transferência"


# ---- 4. reports and bot ---------------------------------------------------------------------
def test_month_totals_per_owner_match_hand_calculation(tmp_path, ledger, tax):
    scenario(tmp_path, ledger, tax)
    pf, pj, all_ = (month_totals(ledger, "2026-10", o) for o in ("pf", "pj", None))
    # PF: 8.500 + 3.000 de retirada; despesas 19,90 + 400; aporte 200 sai do saldo sem ser despesa
    assert pf["receitas"] == 1_150_000 and pf["fontes"] == {"Empresa CLT": 850_000, "Retirada PJ": 300_000}
    assert pf["despesas"] == 41_990 and pf["aportes"] == 20_000 and pf["titular"] == -20_000
    # PJ: faturamento 6.000; DAS 80,90 + tarifa 2,50; retirada sai (-3.000) e aporte entra (+200)
    assert pj["receitas"] == 600_000 and pj["fontes"] == {"Cliente PJ": 600_000}
    assert pj["categorias"] == {"Impostos/DAS": 8_090, "Taxas bancárias PJ": 250}
    assert pj["retiradas"] == 300_000 and pj["aportes"] == 20_000 and pj["titular"] == -280_000
    # consolidado: retirada/aporte não contam; 8.500 + 6.000 e 419,90 + 83,40
    assert all_["receitas"] == 1_450_000 and all_["despesas"] == 50_330 and all_["titular"] == 0
    saldo = lambda t: t["receitas"] - t["despesas"] + t["titular"]
    assert saldo(pf) == 1_088_010 and saldo(pj) == 311_660 and saldo(all_) == 1_399_670
    assert saldo(pf) + saldo(pj) == saldo(all_)                           # no double counting


def test_summary_sections_and_posso_comprar_only_pf(tmp_path, ledger, tax):
    cfg = scenario(tmp_path, ledger, tax)
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert set(s.secoes) == {"PF", "PJ", "Consolidado"}
    assert (s.receitas, s.saldo) == (1_150_000, 1_088_010)                # main numbers are PF
    assert s.secoes["PJ"].saldo == 311_660 and s.secoes["PJ"].saldo_contas == 275_000
    assert s.secoes["Consolidado"].saldo == 1_399_670
    assert s.saldo_contas == 955_000                                      # PF 6.800 + PJ 2.750: MEI cash is yours
    assert s.saldo_livre == 955_000 and s.renda_ref == 1_150_000
    assert can_i_buy(s, 700_000).startswith("Sim") and can_i_buy(s, 1_000_000).startswith("Não")
    text = format_summary(s)
    for piece in ("PF (pessoa física)", "PJ (MEI)", "Consolidado (sem PF↔PJ)", "Retirada PJ R$ 3.000,00",
                  "Faturamento       R$ 6.000,00", "Retiradas p/ PF  -R$ 3.000,00", "Aportes na PJ      -R$ 200,00",
                  "R$ 10.880,10", "R$ 3.116,60", "R$ 13.996,70", "Impostos/DAS", "Saldo livre até 05/11 (PF)"):
        assert piece in text, piece
    assert text.index("PF (pessoa física)") < text.index("PJ (MEI)") < text.index("Consolidado")


def test_summary_without_pj_is_unchanged(tmp_path, ledger, tax):
    cfg = make_cfg(tmp_path)
    from conftest import client

    run_sync(ledger, client({I: [tx("sal", I, "2026-10-05", 8500.0, "SALARIO", op="FOLHA_PAGAMENTO", method="TED",
                                     payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU)]}),
             cfg, tax, today=TODAY, now=NOW)
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert s.secoes == {} and "PJ (MEI)" not in format_summary(s) and "Receitas" in format_summary(s)


def test_bot_resumo_shows_pf_and_pj_and_posso_comprar_uses_pf(tmp_path, ledger, tax):
    from fin.telegram_bot import Handlers

    cfg = scenario(tmp_path, ledger, tax)
    h = Handlers(lambda: Ledger(ledger.path), cfg, tax, today=lambda: TODAY)
    text = h("/resumo", [])
    assert "PF (pessoa física)" in text and "PJ (MEI)" in text and "Consolidado" in text
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert can_i_buy(s, 700_000) in h("/posso_comprar", ["7000"]).replace("&gt;", ">")
    assert "só PF" in h("/ajuda", [])


# ---- 5. Sheets ------------------------------------------------------------------------------
def test_sheets_titular_column_and_resumo_per_titular(tmp_path, ledger, tax):
    from fin.sheets import build_tables

    scenario(tmp_path, ledger, tax)
    t = build_tables(ledger, TODAY)
    lanc = t["lancamentos"]
    h = lanc[0]
    assert h[-1] == "titular"
    by_id = {r[h.index("id")]: r for r in lanc[1:]}
    lid = lambda pid: f"t{row(ledger, pid)['local_id']:06d}"
    assert by_id[lid("das")][h.index("titular")] == "PJ" and by_id[lid("das")][h.index("conta")] == "Nubank PJ conta"
    assert by_id[lid("sal")][h.index("titular")] == "PF" and by_id[lid("mkt")][h.index("titular")] == "PF"
    res = t["resumo_mensal"]
    rh = res[0]
    oct_ = {r[rh.index("titular")]: r for r in res[1:] if r[0] == "2026-10"}
    assert set(oct_) == {"Consolidado", "PF", "PJ"}
    get = lambda who, col: oct_[who][rh.index(col)]
    assert (get("PF", "receitas"), get("PF", "despesas"), get("PF", "saldo")) == (11500.0, 419.9, 10880.1)
    assert (get("PJ", "receitas"), get("PJ", "despesas"), get("PJ", "saldo")) == (6000.0, 83.4, 3116.6)
    assert (get("PJ", "retiradas_pj"), get("PJ", "aportes_pj")) == (3000.0, 200.0)
    assert (get("Consolidado", "receitas"), get("Consolidado", "saldo")) == (14500.0, 13996.7)


def test_demo_has_pj_account_and_sections(tmp_path):
    from fin.demo import run_demo

    res = run_demo(tmp_path / "demo", echo=lambda *_: None)
    text = (tmp_path / "demo" / "relatorio.txt").read_text(encoding="utf-8")
    assert "PJ (MEI)" in text and "Retirada PJ" in text and "Impostos/DAS" in text
    # PJ October: 6.000 - (80,90 DAS + 2,50 tarifa) - 5.000 retirada = 916,60
    assert res["pj_saldo"] == 91_660
    assert res["pf_saldo"] + res["pj_saldo"] == res["consolidado_saldo"]


def test_pj_billing_received_in_pf_counts_as_pj_revenue(ledger):
    from fin.reports import month_totals
    acc = ledger.q("SELECT id, owner FROM accounts")
    if not acc:
        return
    pf = next((a["id"] for a in acc if (a["owner"] or "pf") == "pf"), None)
    if pf is None:
        return
    ledger.insert_tx({"pluggy_id": "fx1", "natural_key": "fx1", "account_id": pf, "account_local_key": "itau:x",
                      "date": "2026-09-05", "competence": "2026-09", "amount_cents": 483000, "direction": "CREDIT",
                      "status": "POSTED", "description": "PIX CLIENTE PJ", "desc_norm": "PIX CLIENTE PJ",
                      "natureza": "Receita", "categoria": "Faturamento PJ", "fonte": "Cliente PJ"})
    pj = month_totals(ledger, "2026-09", "pj")
    pfv = month_totals(ledger, "2026-09", "pf")
    cons = month_totals(ledger, "2026-09", None)
    assert pj["receitas"] >= 483000 and pj["fontes"].get("Cliente PJ", 0) >= 483000
    assert pfv["fontes"].get("Cliente PJ", 0) >= 483000
    assert cons["fontes"].get("Cliente PJ", 0) == pfv["fontes"].get("Cliente PJ", 0)  # consolidated counts it once
