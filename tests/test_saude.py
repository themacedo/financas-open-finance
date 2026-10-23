"""Saúde dos dados no painel: o sync guarda a frescura de cada banco no Pluggy; o painel mostra sync, bancos,
pendentes de revisão e a última comparação da nuvem."""
import json

from conftest import ITEMS, NOW, TODAY, client, tx, I
from fin.painel import SOMBRA_JSON, compute_saude
from fin.sync import run_sync


def test_sync_saves_each_bank_freshness_and_dashboard_shows_it(ledger, cfg, tax, tmp_path):
    run_sync(ledger, client({I: [tx("a", I, "2026-10-10", -10.0, "PADARIA EXEMPLO")]}), cfg, tax, today=TODAY, now=NOW)
    salvo = json.loads(ledger.meta("pluggy_conexoes"))
    assert set(salvo) == set(ITEMS) and salvo["itau"]["status"] == "UPDATED" and salvo["itau"]["ultima"]
    s = compute_saude(ledger, tmp_path)
    assert s["sync"]["status"] == "success" and s["sync"]["novos"] == 1 and s["sync"]["ultimoSucesso"] == s["sync"]["em"]
    assert len(s["conexoes"]) == len(ITEMS) and all("consentimento" in c for c in s["conexoes"])
    assert {c["banco"] for c in s["conexoes"]} >= {"Itaú", "Nubank", "Santander"}
    assert s["pendentes"] == ledger.one("SELECT COUNT(*) n FROM transactions WHERE review_status='pendente'")["n"]
    assert s["sombra"] is None  # sem o arquivo da nuvem


def test_last_cloud_comparison_comes_from_the_file_next_to_the_ledger(ledger, tmp_path):
    (tmp_path / SOMBRA_JSON).write_text(json.dumps({"hoje": "2026-10-19", "sync": "success", "campos": 6039, "diferencas": 0,
                                                    "amostra": ["x"], "stats": {}}), encoding="utf-8")
    s = compute_saude(ledger, tmp_path)
    assert s["sombra"] == {"hoje": "2026-10-19", "sync": "success", "campos": 6039, "diferencas": 0}
    assert s["sync"] is None and s["conexoes"] == [] and s["pendentes"] == 0
