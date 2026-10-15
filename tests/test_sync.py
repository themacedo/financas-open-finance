from datetime import date
from pathlib import Path

from conftest import C, client, I, NOW, OWN_ITAU, party, TODAY, tx

from fin.pluggy import FixturePluggyClient
from fin.sync import run_sync

FIXTURES = Path(__file__).parent / "fixtures" / "pluggy"


def _count(ledger, where="1=1"):
    return ledger.one(f"SELECT COUNT(*) n FROM transactions WHERE {where}")["n"]


def _base():
    return {
        I: [tx("f1", I, "2026-10-05", 8500.0, "SALARIO EMPRESA CLT EXEMPLO", op="FOLHA_PAGAMENTO",
               payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU, method="TED", provider_id="OF1"),
            tx("f2", I, "2026-10-09", -45.0, "PADARIA EXEMPLO", op="CARTAO", provider_id="OF2"),
            tx("f3", I, "2026-10-09", -45.0, "PADARIA EXEMPLO", op="CARTAO", provider_id="OF3")],  # same day, same value: 2 real rows
        C: [tx("c1", C, "2026-10-12", 64.9, "IFD*IFOOD 1234 SAO PAULO", op="PAGAMENTO", card=True,
               ccm={"billForecastDate": "2026-11"})],
    }


def test_sync_twice_does_not_duplicate(ledger, cfg, tax):
    data = _base()
    r1 = run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    assert r1.stats["inserted"] == 4
    r2 = run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    assert r2.stats["inserted"] == 0 and r2.stats["relinked"] == 0
    assert _count(ledger) == 4
    assert _count(ledger, "desc_norm='PADARIA EXEMPLO'") == 2  # duplicates by content are kept (natural_key occurrence)


def test_new_pluggy_id_relinks_and_keeps_category(ledger, cfg, tax):
    data = _base()
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    lid = ledger.one("SELECT local_id FROM transactions WHERE pluggy_id='c1'")["local_id"]
    ledger.update_tx(lid, {"categoria": "Alimentação", "natureza": "Despesa", "cat_source": "manual", "review_status": "aprovado"})
    # Pluggy recreated the card row with a new id (and no providerId): natural_key relinks it
    data[C][0]["id"] = "c1-new"
    data[C][0]["providerId"] = None
    r = run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 16), now=NOW)
    assert r.stats["relinked"] == 1 and r.stats["inserted"] == 0
    row = ledger.tx_by_local(lid)
    assert row["pluggy_id"] == "c1-new" and row["categoria"] == "Alimentação" and row["review_status"] == "aprovado"
    # date shift near closing (content changed, new id): loose match relinks too
    data[C][0]["id"] = "c1-newer"
    data[C][0]["date"] = "2026-10-13T00:00:00.000Z"
    r = run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 17), now=NOW)
    assert r.stats["relinked"] == 1 and _count(ledger, "account_id=?".replace("?", f"'{C}'")) == 1
    assert ledger.tx_by_local(lid)["date"] == "2026-10-13"


def test_missing_rows_are_soft_removed_after_n_syncs(ledger, cfg, tax):
    data = _base()
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    data[I] = data[I][:2]  # f3 vanished upstream
    for k in range(3):
        run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 16 + k), now=NOW)
    row = ledger.one("SELECT * FROM transactions WHERE pluggy_id='f3'")
    assert row is not None, "never hard-deleted"
    assert row["removed_upstream"] == 1 and row["missing_count"] == 3
    assert all(r["pluggy_id"] != "f3" for r in ledger.active_txs())


def test_card_sign_and_foreign_currency(ledger, cfg, tax):
    data = {C: [tx("u1", C, "2026-10-11", 10.0, "STEAM GAMES", card=True, currency="USD", amount_brl=55.2,
                   ccm={"billForecastDate": "2026-11"}),
                tx("bad", C, "2026-10-11", 159.2, "PARCELA ESTRANHA", ttype="CREDIT", card=True, ccm={})]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    usd = ledger.one("SELECT * FROM transactions WHERE pluggy_id='u1'")
    assert usd["amount_cents"] == -5520 and usd["orig_amount_cents"] == 1000 and usd["orig_currency"] == "USD"
    assert usd["competence"] == "2026-11"
    bad = ledger.one("SELECT * FROM transactions WHERE pluggy_id='bad'")
    assert bad["sign_mismatch"] == 1
    assert bad["competence"] == "2026-11"  # no billForecastDate: closing day 3 / due 10 from config


def test_second_pass_uses_created_at(ledger, cfg, tax):
    data = _base()
    c = client(data)
    run_sync(ledger, c, cfg, tax, today=TODAY, now=NOW)
    c2 = client(data)
    run_sync(ledger, c2, cfg, tax, today=date(2026, 10, 16), now=NOW)
    passes = [call for call in c2.calls if call[0] == I]
    assert ("2026-09-01", None) == passes[0][1:]  # dateFrom = today - 45d
    assert passes[1][2] == "2026-10-13T00:00:00.000Z"  # createdAtFrom = last success - 2d


def test_json_fixtures_end_to_end(ledger, tmp_path, tax):
    from conftest import make_cfg

    cfg = make_cfg(tmp_path, cards={"nubank": {"issuer_cnpj": "33.333.333/0001-33", "closing_day": 3, "due_day": 10}})
    result = run_sync(ledger, FixturePluggyClient(FIXTURES), cfg, tax, today=TODAY, now=NOW)
    assert result.stats["inserted"] == 20
    assert any("423" in a or "limite mensal" in a for a in result.alerts)
    assert any("LOGIN_ERROR" in a for a in result.alerts)
    rules = {r["pluggy_id"][-3:]: (r["natureza"], r["rule"], r["pair_id"]) for r in ledger.active_txs()}
    assert rules["003"][1] == "own_transfer" and rules["003"][2] == rules["030"][2]   # Itaú -> Nubank
    assert rules["007"][2] == rules["020"][2] and rules["007"][2]                     # Itaú -> Santander
    assert rules["004"][1] == "bank_bill_payment" and rules["004"][2] == rules["044"][2]
    assert rules["006"][1] == "fee" and rules["008"][1] == "investment" and rules["001"][1] == "salary"
    assert rules["005"][1] == "person" and rules["023"][1] == "person"
    assert rules["045"][1] == "refund"
    # raw JSON in the ledger is scrubbed: no names, no CPF
    raw = " ".join(r["raw_json"] for r in ledger.active_txs())
    assert "111.111.111-11" not in raw and '"name"' not in raw
    assert "444.444.444-44" not in raw
