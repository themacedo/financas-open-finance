import json

import pytest
from conftest import C, client, I, NOW, OWN_ITAU, party, S, TODAY, tx

from fin.categorize import approve, run_categorize
from fin.llm import AnthropicCategorizer, build_schema, OfflineAnthropicClient
from fin.privacy import block_reason, PrivacyViolation
from fin.sync import run_sync


class RecordingCategorizer:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or {}

    def categorize(self, items, categories):
        self.calls.append((items, categories))
        return [{"id": i["id"], "categoria": self.answer.get(i["desc"], "Revisar"), "subcategoria": "",
                 "estabelecimento_normalizado": i["desc"], "confianca": 0.9} for i in items]


def _row(ledger, pid):
    return ledger.one("SELECT * FROM transactions WHERE pluggy_id=?", (pid,))


def test_dictionary_hit_never_calls_llm(ledger, cfg, tax):
    run_sync(ledger, client({C: [tx("i", C, "2026-10-12", 64.9, "IFD*IFOOD 1234 SAO PAULO", card=True,
                                     ccm={"billForecastDate": "2026-11"})]}), cfg, tax, today=TODAY, now=NOW)
    ledger.add_dictionary({"kind": "contains", "pattern": "IFOOD", "categoria": "Alimentação", "essencial": "Supérfluo", "origin": "user"})
    llm = RecordingCategorizer()
    stats = run_categorize(ledger, cfg, tax, llm)
    row = _row(ledger, "i")
    assert row["desc_norm"] == "IFOOD"
    assert (row["categoria"], row["cat_source"], row["essencial"], row["review_status"]) == ("Alimentação", "dictionary", "Supérfluo", "auto")
    assert stats["dictionary"] == 1 and llm.calls == []


def test_llm_fallback_with_schema_cache_and_approval(ledger, cfg, tax):
    data = {C: [tx("a", C, "2026-10-12", 89.0, "AQUARIO REEF CENTER EXEMPLO", card=True, ccm={"billForecastDate": "2026-11"}),
                tx("b", C, "2026-10-13", 45.0, "AQUARIO REEF CENTER EXEMPLO", card=True, ccm={"billForecastDate": "2026-11"})]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    fake = OfflineAnthropicClient()
    cat = AnthropicCategorizer(fake, ledger, block_terms=cfg.block_terms)
    stats = run_categorize(ledger, cfg, tax, cat)
    assert stats["llm_sent"] == 1  # deduplicated by description
    req = fake.requests[0]
    assert req["model"] == "claude-haiku-5-5" and "temperature" not in req
    assert req["output_config"]["effort"] == "low"
    schema = req["output_config"]["format"]["schema"]
    assert schema == build_schema(tax.llm_categories)
    item_schema = schema["properties"]["items"]["items"]
    assert item_schema["additionalProperties"] is False and set(item_schema["required"]) == set(item_schema["properties"])
    assert "Revisar" in item_schema["properties"]["categoria"]["enum"]
    assert "Salário CLT" not in item_schema["properties"]["categoria"]["enum"]  # income never comes from the AI
    a = _row(ledger, "a")
    assert (a["categoria"], a["cat_source"], a["review_status"]) == ("Hobby", "llm", "pendente")
    # cache: new row with same merchant does not call the LLM again
    data[C].append(tx("c", C, "2026-10-14", 10.0, "AQUARIO REEF CENTER EXEMPLO", card=True, ccm={"billForecastDate": "2026-11"}))
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    run_categorize(ledger, cfg, tax, cat)
    assert len(fake.requests) == 1 and _row(ledger, "c")["cat_source"] == "cache"
    # approval feeds the dictionary and resolves the siblings
    approve(ledger, tax, a["local_id"], essencial="Supérfluo")
    for pid in ("b", "c"):
        r = _row(ledger, pid)
        assert (r["categoria"], r["cat_source"], r["review_status"], r["essencial"]) == ("Hobby", "dictionary", "auto", "Supérfluo")
    # audit holds the exact payload that was sent
    audit = ledger.one("SELECT * FROM llm_audit WHERE kind='categorize'")
    assert json.loads(audit["request_json"]) == json.loads(json.dumps(req, sort_keys=True))


def test_payload_contains_only_id_desc_tipo(ledger, cfg, tax):
    data = {S: [tx("x", S, "2026-10-11", -45.9, "PIX PADARIA EXEMPLO", op="PIX", method="PIX",
                   receiver=party("PADARIA EXEMPLO LTDA", "55.555.555/0001-55", "077"))]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    llm = RecordingCategorizer({"PADARIA EXEMPLO": "Alimentação"})
    run_categorize(ledger, cfg, tax, llm)
    items, _ = llm.calls[0]
    assert items == [{"id": "d1", "desc": "PADARIA EXEMPLO", "tipo": "debito"}]
    blob = json.dumps(items)
    for forbidden in ("2026", "55.555", "-45", "santander", "a0000000"):
        assert forbidden not in blob.lower()


@pytest.mark.parametrize("desc,reason", [
    ("LOJA 12345678901", "digits"),
    ("PIX CHAVE FULANO@EMAIL.COM", "at"),
    ("PAG 123.456.789", "digits_sep"),
    ("REF 0f8fad5b-d9cb-469f-a165-7a8b6c7d9e0f", "uuid"),
    ("TITULAR TESTE CONSULTORIA", "term"),
    ("MARIA DOCES", "first_name"),
])
def test_privacy_guard_blocks(desc, reason):
    assert block_reason(desc, ["titular teste"]) == reason


def test_blocked_items_never_reach_llm(ledger, cfg, tax):
    data = {C: [tx("n", C, "2026-10-12", 30.0, "MARIA DOCES", card=True, ccm={"billForecastDate": "2026-11"}),
                tx("t", C, "2026-10-12", 30.0, "TITULAR TESTE SERVICOS", card=True, ccm={"billForecastDate": "2026-11"}),
                tx("ok", C, "2026-10-12", 30.0, "STEAM GAMES", card=True, ccm={"billForecastDate": "2026-11"})]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    fake = OfflineAnthropicClient()
    stats = run_categorize(ledger, cfg, tax, AnthropicCategorizer(fake, ledger, block_terms=cfg.block_terms))
    assert stats["blocked"] == 2
    sent = json.loads(fake.requests[0]["messages"][0]["content"])
    assert [i["desc"] for i in sent] == ["STEAM GAMES"]
    for pid in ("n", "t"):
        r = _row(ledger, pid)
        assert (r["categoria"], r["cat_source"], r["review_status"]) == ("Revisar", "blocked", "pendente")
    blocked = json.loads(ledger.one("SELECT blocked_json FROM llm_audit WHERE kind='blocked'")["blocked_json"])
    assert {b["reason"] for b in blocked} == {"first_name", "term"}


def test_categorizer_refuses_unsafe_payload_even_if_called_directly(ledger):
    cat = AnthropicCategorizer(OfflineAnthropicClient(), ledger)
    with pytest.raises(PrivacyViolation):
        cat.categorize([{"id": "d1", "desc": "CPF 11111111111", "tipo": "debito"}], ["Outros", "Revisar"])
    with pytest.raises(PrivacyViolation):
        cat.categorize([{"id": "d1", "desc": "LOJA", "tipo": "debito", "data": "2026-10-01"}], ["Outros", "Revisar"])


def test_pix_to_person_never_goes_to_llm(ledger, cfg, tax):
    data = {I: [tx("p1", I, "2026-10-09", -80.0, "PIX ENVIADO LOJINHA DO BAIRRO", op="PIX", method="PIX", payer=OWN_ITAU,
                   receiver=party("FULANO", "555.555.555-55", "104")),
                tx("p2", I, "2026-10-09", -35.0, "PIX ENVIADO", op="PIX"),                     # no paymentData
                tx("m", I, "2026-10-09", -20.0, "STEAM GAMES", op="CARTAO")]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    llm = RecordingCategorizer()
    run_categorize(ledger, cfg, tax, llm)
    sent = [i["desc"] for items, _ in llm.calls for i in items]
    assert sent == ["STEAM GAMES"]
    assert _row(ledger, "p1")["counterpart_alias"].startswith("PESSOA_")
    assert _row(ledger, "p2")["counterpart_alias"].startswith("PESSOA_")
    # approving a person teaches the counterpart table (not the LLM, not the dictionary)
    lid = _row(ledger, "p1")["local_id"]
    approve(ledger, tax, lid, categoria="Moradia", essencial="Essencial")
    cp = ledger.counterpart(_row(ledger, "p1")["counterpart_alias"])
    assert cp["categoria"] == "Moradia"
    assert ledger.one("SELECT COUNT(*) n FROM dictionary")["n"] == 0


def test_unknown_income_is_not_guessed(ledger, cfg, tax):
    data = {I: [tx("in", I, "2026-10-06", 900.0, "PIX RECEBIDO NOVO CLIENTE", op="PIX", method="PIX",
                   payer=party("NOVO CLIENTE LTDA", "77.777.777/0001-77", "237"), receiver=OWN_ITAU)]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    llm = RecordingCategorizer()
    run_categorize(ledger, cfg, tax, llm)
    row = _row(ledger, "in")
    assert llm.calls == [] and (row["natureza"], row["categoria"], row["review_status"]) == ("Receita", "Revisar", "pendente")
    approve(ledger, tax, row["local_id"], categoria="PJ", fonte="Cliente PJ")
    entry = ledger.one("SELECT * FROM dictionary WHERE kind='cnpj'")
    assert entry["pattern"] == "77777777000177" and entry["fonte"] == "Cliente PJ"


def test_llm_disabled_marks_review(ledger, tmp_path, tax):
    from conftest import make_cfg

    cfg = make_cfg(tmp_path, llm={"enabled": False})
    run_sync(ledger, client({C: [tx("z", C, "2026-10-12", 30.0, "STEAM GAMES", card=True, ccm={"billForecastDate": "2026-11"})]}),
             cfg, tax, today=TODAY, now=NOW)
    run_categorize(ledger, cfg, tax, RecordingCategorizer())
    assert _row(ledger, "z")["categoria"] == "Revisar"
