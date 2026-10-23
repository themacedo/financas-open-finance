"""Revisões que voltam da tela Revisar pela fila: aplicadas com o mesmo approve, sem passar por cima do que já foi revisado."""
import base64
import json

from conftest import I, NOW, TODAY, client, tx
from fin.revisoes import aplicar, ler_lote
from fin.sync import run_sync


def msg(ack, **corpo):
    return {"ackId": ack, "message": {"data": base64.b64encode(json.dumps({"tipo": "revisao", **corpo}).encode()).decode()}}


def pendentes(ledger, cfg, tax):
    run_sync(ledger, client({I: [tx("a", I, "2026-10-10", -10.0, "LOJA UM XYZ"), tx("b", I, "2026-10-12", -55.0, "LOJA DOIS XYZ")]}),
             cfg, tax, today=TODAY, now=NOW)
    ledger.x("UPDATE transactions SET review_status='pendente', categoria='Revisar', cat_source='llm'")
    return {r["pluggy_id"]: r["local_id"] for r in ledger.active_txs("1=1")}


def test_applies_pending_rows_and_every_message_can_be_acked(ledger, cfg, tax):
    ids = pendentes(ledger, cfg, tax)
    lote = ler_lote([msg("k1", id=ids["a"], categoria="Alimentação", essencial="Essencial", em="2026-10-20T10:00:00Z"),
                     {"ackId": "k2", "message": {"data": "não é base64 de json"}}])
    res = aplicar(ledger, tax, lote)
    assert (res["aplicadas"], res["ignoradas"], res["erros"]) == (1, 0, 1)
    assert sorted(res["ackIds"]) == ["k1", "k2"]
    a = ledger.tx_by_local(ids["a"])
    assert (a["categoria"], a["essencial"], a["review_status"], a["cat_source"]) == ("Alimentação", "Essencial", "aprovado", "manual")
    assert ledger.tx_by_local(ids["b"])["review_status"] == "pendente"


def test_already_reviewed_on_the_mac_wins_and_newest_in_batch_wins(ledger, cfg, tax):
    ids = pendentes(ledger, cfg, tax)
    ledger.x("UPDATE transactions SET review_status='aprovado', categoria='Lazer' WHERE local_id=?", (ids["a"],))
    res = aplicar(ledger, tax, ler_lote([
        msg("k1", id=ids["a"], categoria="Alimentação", essencial=None, em="2026-10-20T10:00:00Z"),
        msg("k2", id=ids["b"], categoria="Lazer", essencial=None, em="2026-10-20T11:00:00Z"),
        msg("k3", id=ids["b"], categoria="Alimentação", essencial="Supérfluo", em="2026-10-20T10:30:00Z"),
    ]))
    assert ledger.tx_by_local(ids["a"])["categoria"] == "Lazer"            # não sobrescreve o que o Mac já decidiu
    assert ledger.tx_by_local(ids["b"])["categoria"] == "Lazer"            # a decisão mais nova do lote
    assert (res["aplicadas"], res["ignoradas"], res["erros"]) == (1, 2, 0)
    assert sorted(res["ackIds"]) == ["k1", "k2", "k3"]


def test_invalid_category_or_missing_row_is_an_error_not_a_crash(ledger, cfg, tax):
    ids = pendentes(ledger, cfg, tax)
    res = aplicar(ledger, tax, ler_lote([msg("k1", id=ids["a"], categoria="Revisar", essencial=None),
                                         msg("k2", id=999999, categoria="Lazer", essencial=None),
                                         msg("k3", id="x", categoria="Lazer")]))
    assert (res["aplicadas"], res["erros"]) == (0, 3) and len(res["ackIds"]) == 3
    assert ledger.tx_by_local(ids["a"])["review_status"] == "pendente"
