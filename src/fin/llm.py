"""Anthropic (Claude Haiku 5.5) categorizer with strict structured output.

Contract (anthropic>=1.14,<2): output_config.format json_schema with enum of
categories; effort "low"; no temperature (only 1 is accepted); thinking blocks may
come first, so the text block is found by type. Refusal / exhausted retries -> "Revisar".
Value, date, installments and nature NEVER come from here.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, Protocol

import anthropic

from fin.ledger import Ledger
from fin.privacy import assert_safe

log = logging.getLogger("fin.llm")
MODEL = "claude-haiku-5-5"
REVIEW = "Revisar"


class LlmFatal(RuntimeError):
    """Bad/revoked key, no credit or a schema bug: stop and alert, do not retry."""


class Categorizer(Protocol):
    def categorize(self, items: list[dict[str, str]], categories: list[str]) -> list[dict[str, Any]]: ...


def make_client(api_key: str, workspace_id: str = "") -> anthropic.Anthropic:
    # Keys created at org level (not inside a workspace) need the workspace header.
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    return anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=60.0, default_headers=headers)


def build_schema(categories: list[str]) -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "categoria": {"type": "string", "enum": categories},
            "subcategoria": {"type": "string"},
            "estabelecimento_normalizado": {"type": "string"},
            "confianca": {"type": "number"},  # 0..1 checked locally (min/max not allowed in schema)
        },
        "required": ["id", "categoria", "subcategoria", "estabelecimento_normalizado", "confianca"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }


def build_system(categories: list[str]) -> str:
    # Stable text (no dates/ids) so it could be cached byte-for-byte if ever enabled.
    return (
        "Você classifica lançamentos financeiros pessoais pelo nome do estabelecimento.\n"
        "Para cada item, escolha exatamente uma categoria da lista. Se não tiver certeza, use "
        '"Revisar". Não invente dados que não estão na descrição.\n'
        "estabelecimento_normalizado: nome curto do comércio, sem cidade, números ou códigos.\n"
        "subcategoria: 1 a 3 palavras, ou string vazia.\n"
        "confianca: número entre 0 e 1.\n"
        "Responda um objeto em items para cada id recebido, sem pular nenhum.\n\n"
        "Categorias:\n" + "\n".join(f"- {c}" for c in categories)
    )


def review_item(item_id: str) -> dict[str, Any]:
    return {"id": item_id, "categoria": REVIEW, "subcategoria": "", "estabelecimento_normalizado": "", "confianca": 0.0}


def validate(out: list[dict[str, Any]], items: list[dict[str, str]], categories: list[str]) -> list[dict[str, Any]]:
    by_lower = {c.lower(): c for c in categories}  # enum casing may drift
    sent = {i["id"] for i in items}
    seen: dict[str, dict[str, Any]] = {}
    for row in out:
        if not isinstance(row, dict):
            continue
        cat = by_lower.get(str(row.get("categoria", "")).lower())
        conf = row.get("confianca")
        rid = row.get("id")
        if rid not in sent or rid in seen or cat is None or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            continue
        seen[rid] = {
            "id": rid,
            "categoria": cat,
            "subcategoria": _clean_label(row.get("subcategoria"), 30),
            "estabelecimento_normalizado": _clean_label(row.get("estabelecimento_normalizado"), 60),
            "confianca": float(conf),
        }
    return [seen.get(i["id"], review_item(i["id"])) for i in items]


RE_LABEL = re.compile(r"[A-Za-zÀ-ÿ0-9/&' .\-]*")


def _clean_label(value: Any, limit: int) -> str:
    """Free text from the model is only a short label: letters, digits, spaces and / & ' . -; anything else
    (or longer than `limit`) is dropped, so it can never carry markup, links or instructions to the Sheets."""
    text = " ".join(str(value or "").split())
    if len(text) > limit or not RE_LABEL.fullmatch(text) or text[:1] in ("=", "+", "-", "@"):
        return ""
    return text


class AnthropicCategorizer:
    def __init__(self, client: Any, ledger: Ledger, *, block_terms: Iterable[str] = (),
                 cache_system: bool = False, model: str = MODEL):
        self.client = client
        self.ledger = ledger
        self.block_terms = list(block_terms)
        self.cache_system = cache_system
        self.model = model

    def build_request(self, items: list[dict[str, str]], categories: list[str]) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": build_system(categories)}
        if self.cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        return {
            "model": self.model,
            "max_tokens": 8000,  # thinking counts toward this cap
            "system": [system_block],
            "messages": [{"role": "user", "content": json.dumps(
                [{"id": i["id"], "desc": i["desc"], "tipo": i["tipo"]} for i in items], ensure_ascii=False)}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": build_schema(categories)}},
        }

    def categorize(self, items: list[dict[str, str]], categories: list[str]) -> list[dict[str, Any]]:
        if not items:
            return []
        assert_safe(items, self.block_terms)  # defense in depth: categorize.py already filtered
        request = self.build_request(items, categories)
        audit_id = self.ledger.audit("categorize", request, "sent", len(items))  # exact payload, kept locally
        fallback = [review_item(i["id"]) for i in items]
        try:
            resp = self.client.messages.create(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            self.ledger.audit_result(audit_id, f"auth_error_{exc.status_code}")
            raise LlmFatal(f"Anthropic recusou a chave ({exc.status_code}); rode fin setup anthropic") from exc
        except anthropic.BadRequestError as exc:
            self.ledger.audit_result(audit_id, "bad_request")
            raise LlmFatal("Anthropic 400: schema/parâmetros inválidos (bug no código)") from exc
        except anthropic.NotFoundError as exc:
            self.ledger.audit_result(audit_id, "not_found")
            raise LlmFatal(f"modelo {self.model} não encontrado") from exc
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError):
            self.ledger.audit_result(audit_id, "retries_exhausted")
            return fallback  # SDK already retried; stays Revisar, next sync tries again
        except anthropic.APIStatusError as exc:
            self.ledger.audit_result(audit_id, f"status_{exc.status_code}")
            if getattr(exc, "type", None) == "billing_error" or exc.status_code == 402:
                raise LlmFatal("Anthropic sem crédito (billing_error)") from exc
            return fallback
        request_id = getattr(resp, "_request_id", None)
        if resp.stop_reason == "refusal":
            self.ledger.audit_result(audit_id, "refusal", request_id=request_id)
            return fallback
        if resp.stop_reason == "max_tokens":
            self.ledger.audit_result(audit_id, "max_tokens", request_id=request_id)
            if len(items) == 1:
                return fallback
            mid = len(items) // 2
            return self.categorize(items[:mid], categories) + self.categorize(items[mid:], categories)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        try:
            parsed = json.loads(text or "")["items"]
        except (ValueError, KeyError, TypeError):
            self.ledger.audit_result(audit_id, "invalid_json", response=text, request_id=request_id)
            return fallback
        self.ledger.audit_result(audit_id, "ok", response=parsed, request_id=request_id)
        return validate(parsed, items, categories)

    def analyze(self, aggregates: dict[str, Any]) -> str:
        """Optional 'analista': receives ONLY aggregates (category totals, month), never rows."""
        payload = json.dumps(aggregates, ensure_ascii=False, sort_keys=True)
        request = {
            "model": self.model,
            "max_tokens": 4000,
            "system": [{"type": "text", "text": (
                "Você é um analista de finanças pessoais. Recebe apenas totais agregados do mês em reais. "
                "Escreva em português, no máximo 8 linhas: o que mudou, onde está o maior gasto variável, "
                "e uma sugestão prática. Não invente números além dos recebidos.")}],
            "messages": [{"role": "user", "content": payload}],
            "output_config": {"effort": "low"},
        }
        audit_id = self.ledger.audit("analyst", request, "sent", 0)
        try:
            resp = self.client.messages.create(**request)
        except anthropic.APIError as exc:
            self.ledger.audit_result(audit_id, f"error_{type(exc).__name__}")
            return ""
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        self.ledger.audit_result(audit_id, resp.stop_reason or "ok", response=text, request_id=getattr(resp, "_request_id", None))
        return text if resp.stop_reason != "refusal" else ""


# ---- offline fake (demo/tests): same response shape as the SDK, no network ----------------------
OFFLINE_KEYWORDS = {
    "Alimentação": ["IFOOD", "RESTAURANTE", "PADARIA", "MERCADO", "SUPERMERC", "LANCHONETE", "PIZZA", "BURGER"],
    "Transporte": ["UBER", "APP TRANSPORTE", "POSTO", "COMBUSTIVEL", "ESTACIONAMENTO", "PEDAGIO", "METRO"],
    "Assinaturas": ["NETFLIX", "SPOTIFY", "STREAMING", "ASSINATURA", "CLOUD", "YOUTUBE"],
    "Saúde": ["FARMACIA", "DROGARIA", "CLINICA", "LABORATORIO"],
    "Moradia": ["ENERGIA", "LUZ", "AGUA", "CONDOMINIO", "ALUGUEL", "INTERNET"],
    "Compras": ["LOJA", "MAGAZINE", "MARKETPLACE", "ELETRONICOS"],
    "Estudo/Cert": ["CURSO", "FACULDADE", "CERTIFICACAO", "LIVRARIA"],
    "Lazer": ["CINEMA", "SHOW", "BAR ", "GAMES", "VIAGEM"],
    "Hobby": ["AQUARIO", "AQUARISMO", "CORAIS", "REEF"],
}


class OfflineAnthropicClient:
    """Mimics anthropic.Anthropic().messages for demo mode. Records every request it gets."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if "temperature" in request:
            raise TypeError("temperature não é aceito no SDK 1.x")
        fmt = (request.get("output_config") or {}).get("format")
        if not fmt:
            text = "Análise offline: os números acima são a fonte; nada foi enviado à API."
            return SimpleNamespace(stop_reason="end_turn", _request_id="offline",
                                   content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])
        enum = fmt["schema"]["properties"]["items"]["items"]["properties"]["categoria"]["enum"]
        out = []
        for it in json.loads(request["messages"][0]["content"]):
            cat, conf = REVIEW, 0.3
            for c, words in OFFLINE_KEYWORDS.items():
                if c in enum and any(w in it["desc"] for w in words):
                    cat, conf = c, 0.85
                    break
            out.append({"id": it["id"], "categoria": cat, "subcategoria": "",
                        "estabelecimento_normalizado": it["desc"].title(), "confianca": conf})
        return SimpleNamespace(stop_reason="end_turn", _request_id="offline",
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=json.dumps({"items": out}, ensure_ascii=False))])
