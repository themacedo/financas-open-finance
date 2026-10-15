"""AnthropicCategorizer against the real SDK (anthropic 1.x) with a mocked HTTP transport. No network."""

import json

import anthropic
import httpx2
import pytest

from fin.llm import AnthropicCategorizer, LlmFatal, MODEL

CATS = ["Alimentação", "Transporte", "Outros", "Revisar"]
ITEMS = [{"id": "d1", "desc": "IFOOD", "tipo": "debito"},
         {"id": "d2", "desc": "UBER TRIP", "tipo": "debito"},
         {"id": "d3", "desc": "LOJA XPTO", "tipo": "debito"}]


def msg(text, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
            "stop_reason": stop, "stop_sequence": None, "stop_details": None,
            "usage": {"input_tokens": 10, "output_tokens": 20}}


def sdk(handler, retries=0):
    return anthropic.Anthropic(api_key="test-not-real", base_url="http://mock.invalid", max_retries=retries,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


GOOD = json.dumps({"items": [
    {"id": "d1", "categoria": "ALIMENTAÇÃO", "subcategoria": "delivery", "estabelecimento_normalizado": "iFood", "confianca": 0.95},
    {"id": "d2", "categoria": "Transporte", "subcategoria": "app", "estabelecimento_normalizado": "Uber", "confianca": 0.9},
    {"id": "d2", "categoria": "Outros", "subcategoria": "", "estabelecimento_normalizado": "dup", "confianca": 0.1},
    {"id": "zz", "categoria": "Outros", "subcategoria": "", "estabelecimento_normalizado": "x", "confianca": 0.5}]}, ensure_ascii=False)


def test_request_shape_retry_and_validation(ledger):
    seen, calls = [], {"n": 0}

    def handler(req):
        calls["n"] += 1
        seen.append(json.loads(req.content))
        if calls["n"] < 3:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
        return httpx2.Response(200, json=msg(GOOD))

    out = AnthropicCategorizer(sdk(handler, retries=2), ledger).categorize(ITEMS, CATS)
    assert calls["n"] == 3  # SDK retried the two 529s by itself
    body = seen[-1]
    assert body["model"] == "claude-haiku-5-5"
    assert "temperature" not in body and "thinking" not in body and "top_p" not in body
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert json.loads(body["messages"][0]["content"]) == ITEMS
    assert [(r["id"], r["categoria"]) for r in out] == [("d1", "Alimentação"), ("d2", "Transporte"), ("d3", "Revisar")]
    audit = ledger.one("SELECT * FROM llm_audit ORDER BY id DESC LIMIT 1")
    assert audit["status"] == "ok" and json.loads(audit["request_json"])["messages"] == body["messages"]


def test_refusal_and_exhausted_retries_become_review(ledger):
    out = AnthropicCategorizer(sdk(lambda r: httpx2.Response(200, json=msg("", "refusal"))), ledger).categorize(ITEMS, CATS)
    assert {r["categoria"] for r in out} == {"Revisar"}
    out = AnthropicCategorizer(sdk(lambda r: httpx2.Response(503, json={"type": "error", "error": {"type": "api_error", "message": "x"}})),
                               ledger).categorize(ITEMS, CATS)
    assert {r["categoria"] for r in out} == {"Revisar"}


def test_auth_error_stops(ledger):
    handler = lambda r: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "x"}})
    with pytest.raises(LlmFatal):
        AnthropicCategorizer(sdk(handler), ledger).categorize(ITEMS, CATS)


def test_max_tokens_splits_chunk(ledger):
    calls = []

    def handler(req):
        items = json.loads(json.loads(req.content)["messages"][0]["content"])
        calls.append(len(items))
        if len(items) > 1:
            return httpx2.Response(200, json=msg("{\"items\": [", "max_tokens"))
        it = items[0]
        return httpx2.Response(200, json=msg(json.dumps({"items": [{"id": it["id"], "categoria": "Outros", "subcategoria": "",
                                                                    "estabelecimento_normalizado": "", "confianca": 0.7}]})))

    out = AnthropicCategorizer(sdk(handler), ledger).categorize(ITEMS, CATS)
    assert [r["categoria"] for r in out] == ["Outros"] * 3 and calls[0] == 3


def test_confidence_out_of_range_is_rejected(ledger):
    bad = json.dumps({"items": [{"id": "d1", "categoria": "Outros", "subcategoria": "", "estabelecimento_normalizado": "", "confianca": 7}]})
    out = AnthropicCategorizer(sdk(lambda r: httpx2.Response(200, json=msg(bad))), ledger).categorize(ITEMS[:1], CATS)
    assert out[0]["categoria"] == "Revisar"


def test_analyst_gets_only_aggregates(ledger):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx2.Response(200, json=msg("Gastos estáveis."))

    agg = {"mes": "2026-10", "receitas": 100.0, "despesas_por_categoria": {"Lazer": 10.0}}
    text = AnthropicCategorizer(sdk(handler), ledger).analyze(agg)
    assert text == "Gastos estáveis."
    assert json.loads(seen[0]["messages"][0]["content"]) == agg
