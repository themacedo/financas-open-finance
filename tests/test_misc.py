import json

import httpx
import pytest
from conftest import C, client, NOW, TODAY, tx

from fin import secrets
from fin.caju import import_caju
from fin.money import brl, parse_brl, to_cents
from fin.normalize import normalize_description
from fin.pluggy import HttpPluggyClient, PluggyError
from fin.sheets import build_tables, cell, csv_cell, CsvPublisher, merge_reviews, publish
from fin.sync import run_sync


@pytest.mark.parametrize("raw,expected", [
    ("IFD*IFOOD 1234 SAO PAULO", "IFOOD"),
    ("UBER *TRIP HELP.UBER.COM", "UBER TRIP HELP UBER"),
    ("NETFLIX.COM", "NETFLIX"),
    ("PAG BOLETO LIGHT SERVICOS", "LIGHT SERVICOS"),
    ("Padaria São José Ltda", "PADARIA SAO JOSE"),
    ("LOJA EXEMPLO PARC 03/10", "LOJA EXEMPLO"),
    ("MERCADO EXEMPLO RJ", "MERCADO EXEMPLO"),
    ("MP*LOJINHA NITEROI", "LOJINHA"),
])
def test_normalize(raw, expected):
    assert normalize_description(raw) == expected


def test_money():
    assert to_cents(0.1 + 0.2) == 30 and to_cents("2350.40") == 235040 and to_cents(159.2) == 15920
    assert brl(123456) == "R$ 1.234,56" and brl(-5) == "-R$ 0,05"
    assert parse_brl("1.200,50") == 120050 and parse_brl("1200") == 120000 and parse_brl("R$ 99,9") == 9990
    assert parse_brl("1200.50") == 120050


def test_cells_block_formula_injection_and_keep_numbers():
    # CSV is parsed by spreadsheet apps: quote formulas; Sheets gets RAW (see test_gsheets_write_is_raw)
    assert csv_cell("=IMPORTXML(1)") == "'=IMPORTXML(1)" and csv_cell("+SUM(A1)") == "'+SUM(A1)" and csv_cell("@x") == "'@x"
    assert csv_cell(-12.5) == -12.5 and csv_cell(None) == "" and csv_cell("IFOOD") == "IFOOD" and csv_cell("-45.0") == "-45.0"
    assert cell("=IMPORTXML(1)") == "=IMPORTXML(1)" and cell(None) == ""


def test_publish_snapshot_and_review_roundtrip(tmp_path, ledger, cfg, tax):
    from fin.categorize import run_categorize

    run_sync(ledger, client({C: [tx("a", C, "2026-10-12", 89.0, "AQUARIO REEF CENTER", card=True, ccm={"billForecastDate": "2026-11"}),
                                 tx("b", C, "2026-10-12", 12.0, "=HYPERLINK(1)", card=True, ccm={"billForecastDate": "2026-11"})]}),
             cfg, tax, today=TODAY, now=NOW)
    run_categorize(ledger, cfg, tax, None)  # no LLM: both pending as Revisar
    pub = CsvPublisher(tmp_path / "sheets")
    st = publish(ledger, tax, pub, TODAY)
    assert st["revisoes"] == 2 and st["lancamentos"] == 2
    st2 = publish(ledger, tax, pub, TODAY)                  # idempotent snapshot
    assert pub.read("lancamentos") == pub.read("lancamentos") and st2["revisoes"] == 2
    rows = pub.read("revisoes")
    header = rows[0]
    rows[1][header.index("categoria_final")] = "Hobby"
    rows[1][header.index("essencial")] = "Supérfluo"
    rows[1][header.index("status")] = "Aprovado"
    rows[2][header.index("categoria_final")] = "Lazer"       # edited but not approved: must be preserved
    pub.write({"revisoes": rows})
    st3 = publish(ledger, tax, pub, TODAY)
    assert st3["imported"] == 1 and st3["revisoes"] == 1
    kept = pub.read("revisoes")
    assert kept[1][header.index("categoria_final")] == "Lazer"
    a = ledger.one("SELECT * FROM transactions WHERE pluggy_id='a'")
    assert (a["categoria"], a["essencial"], a["review_status"]) == ("Hobby", "Supérfluo", "aprovado")
    lanc = build_tables(ledger, TODAY)["lancamentos"]
    assert all(not (isinstance(c, str) and c[:1] and c[:1] in "=+@") for r in lanc[1:] for c in r)
    assert lanc[0][:6] == ["id", "data", "competencia", "conta", "descricao", "valor"]


def test_merge_reviews_rejects_invalid_category(ledger, tax):
    from fin.sheets import HEADERS

    row = [""] * len(HEADERS["revisoes"])
    row[0], row[8], row[-1] = "t000999", "Moradia", "Aprovado"
    rows, stats = merge_reviews(ledger, tax, [HEADERS["revisoes"], row])
    assert stats["invalid"] == 1


def test_caju_import_idempotent_and_soft_removal(tmp_path, ledger, tax):
    f = tmp_path / "caju.csv"
    f.write_text("data;descricao;valor;categoria;fonte\n2026-10-01;Crédito;1000,00;Salário CLT;Empresa CLT\n"
                 "2026-10-14;Gastos;-401,10;;\n", encoding="utf-8")
    assert import_caju(ledger, tax, f) == {"inserted": 2, "updated": 0, "removed": 0}
    assert import_caju(ledger, tax, f)["inserted"] == 0
    f.write_text("data;descricao;valor;categoria;fonte\n2026-10-01;Crédito;1000,00;Salário CLT;Empresa CLT\n", encoding="utf-8")
    assert import_caju(ledger, tax, f)["removed"] == 1
    rows = ledger.active_txs("account_id='caju'")
    assert len(rows) == 1 and rows[0]["fonte"] == "Empresa CLT" and rows[0]["natureza"] == "Receita"


def test_setup_never_prints_secret(capsys, memory_keyring):
    secrets.setup_group("pluggy", ask=lambda prompt: "SUPER-SECRET-VALUE")
    out = capsys.readouterr().out
    assert "SUPER-SECRET-VALUE" not in out
    assert secrets.get("pluggy_client_secret") == "SUPER-SECRET-VALUE"
    assert secrets.status()["pluggy_client_secret"] is True
    assert "SUPER-SECRET-VALUE" not in json.dumps(secrets.status())


def test_setup_google_key_file(tmp_path, capsys):
    key = tmp_path / "k.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "sa@proj.iam.gserviceaccount.com",
                               "private_key": "-----BEGIN PRIVATE KEY-----FAKE"}), encoding="utf-8")
    secrets.setup_google(key)
    out = capsys.readouterr().out
    assert "FAKE" not in out and "sa@proj.iam.gserviceaccount.com" in out


def test_pluggy_http_pagination_429_and_reauth():
    calls = []
    state = {"rate": True, "expired": True}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, str(req.url.query, "ascii") if isinstance(req.url.query, bytes) else req.url.query))
        if req.url.path == "/auth":
            assert json.loads(req.content) == {"clientId": "id", "clientSecret": "sec"}
            return httpx.Response(200, json={"apiKey": "k"})
        assert req.headers["X-API-KEY"] == "k"
        if state["expired"]:
            state["expired"] = False
            return httpx.Response(401, json={"code": 401})
        if state["rate"]:
            state["rate"] = False
            return httpx.Response(429, headers={"RateLimit-Reset": "7"}, json={"code": 429})
        if req.url.path == "/v2/transactions":
            if "after=CUR" in str(req.url):
                return httpx.Response(200, json={"results": [{"id": "t2"}], "next": None})
            return httpx.Response(200, json={"results": [{"id": "t1"}], "next": "?accountId=a&after=CUR"})
        return httpx.Response(404, json={"codeDescription": "ITEM_NOT_FOUND"})

    slept = []
    c = HttpPluggyClient("id", "sec", base_url="https://api.test", transport=httpx.MockTransport(handler), sleep=slept.append)
    assert [t["id"] for t in c.list_transactions("a", date_from="2026-09-01")] == ["t1", "t2"]
    assert slept == [7]
    assert sum(1 for m, p, _ in calls if p == "/auth") == 2
    assert "sec" not in repr(c)
    with pytest.raises(PluggyError) as exc:
        c.get_item("x")
    assert exc.value.code == "ITEM_NOT_FOUND"
    with pytest.raises(ValueError):
        c.list_transactions("a", date_from="2026-09-01", created_at_from="2026-09-01T00:00:00.000Z")


def test_demo_runs_offline(tmp_path):
    from fin.demo import run_demo

    res = run_demo(tmp_path / "demo", echo=lambda *_: None)
    assert res["count2"] == res["count1"] + 1   # second sync: only the new 4/10 installment
    assert all(set(i) == {"id", "desc", "tipo"} for i in res["llm_payload"])
    assert not any("TITULAR" in i["desc"] or "LOCADOR" in i["desc"] or "AMIGO" in i["desc"] for i in res["llm_payload"])
