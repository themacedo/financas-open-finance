"""Pluggy API client (Meu Pluggy, personal use) behind a small interface.

Contract (docs.pluggy.ai, checked 2026-10-15):
- POST /auth {clientId, clientSecret} -> {apiKey} (valid 2h; one per run, never logged)
- GET /items/{id}, GET /accounts?itemId=, GET /v2/transactions?accountId= (cursor `next`),
  GET /bills?accountId= (page/pageSize). 429 -> wait RateLimit-Reset seconds.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger("fin.pluggy")
BASE_URL = "https://api.pluggy.ai"


class PluggyError(RuntimeError):
    def __init__(self, status: int, code: str | None = None, message: str = ""):
        super().__init__(f"pluggy {status} {code or ''}".strip())
        self.status = status
        self.code = code
        self.message = message


class PluggyAPI(Protocol):
    def get_item(self, item_id: str) -> dict[str, Any]: ...
    def list_accounts(self, item_id: str) -> list[dict[str, Any]]: ...
    def list_transactions(self, account_id: str, *, date_from: str | None = None,
                          date_to: str | None = None, created_at_from: str | None = None) -> list[dict[str, Any]]: ...
    def list_bills(self, account_id: str) -> list[dict[str, Any]]: ...


class HttpPluggyClient:
    def __init__(self, client_id: str, client_secret: str, *, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_key: str | None = None
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._http = httpx.Client(base_url=base_url, transport=transport,
                                  timeout=httpx.Timeout(60, connect=10),
                                  headers={"Accept": "application/json"})

    def __repr__(self) -> str:  # never show credentials
        return "HttpPluggyClient(<redacted>)"

    def close(self) -> None:
        self._http.close()

    # ---- low level -------------------------------------------------------
    def _auth(self) -> str:
        resp = self._http.post("/auth", json={"clientId": self._client_id, "clientSecret": self._client_secret})
        if resp.status_code != 200:
            raise _error(resp)
        self._api_key = resp.json()["apiKey"]
        return self._api_key

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        key = self._api_key
        reauthed = False
        for attempt in range(1, self._max_attempts + 1):
            try:
                if key is None:  # inside the retry loop: a network blip on /auth is retried too
                    key = self._auth()
                resp = self._http.get(path, params=params, headers={"X-API-KEY": key})
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise
                self._sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and not reauthed:  # apiKey expired (2h)
                key, reauthed = self._auth(), True
                continue
            if resp.status_code == 429 and attempt < self._max_attempts:
                wait = _int(resp.headers.get("RateLimit-Reset")) or _int(resp.headers.get("Retry-After")) or 60
                log.warning("pluggy 429, aguardando %ss", wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < self._max_attempts:
                self._sleep(min(2 ** attempt, 30))
                continue
            raise _error(resp)
        raise PluggyError(0, "RETRIES_EXHAUSTED")

    # ---- API -------------------------------------------------------------
    def get_item(self, item_id: str) -> dict[str, Any]:
        return self._get(f"/items/{item_id}")

    def list_accounts(self, item_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get("/accounts", {"itemId": item_id, "page": page})
            out.extend(data.get("results") or [])
            if page >= int(data.get("totalPages") or 1):
                return out
            page += 1

    def list_transactions(self, account_id: str, *, date_from: str | None = None,
                          date_to: str | None = None, created_at_from: str | None = None) -> list[dict[str, Any]]:
        if created_at_from and date_from:
            raise ValueError("createdAtFrom não combina com dateFrom")
        params: dict[str, Any] = {"accountId": account_id}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if created_at_from:
            params["createdAtFrom"] = created_at_from
        data = self._get("/v2/transactions", params)
        out = list(data.get("results") or [])
        while data.get("next"):
            # `next` is an opaque query string: use it exactly as received.
            data = self._get("/v2/transactions" + data["next"])
            out.extend(data.get("results") or [])
        return out

    def list_bills(self, account_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get("/bills", {"accountId": account_id, "page": page, "pageSize": 500})
            out.extend(data.get("results") or [])
            if page >= int(data.get("totalPages") or 1):
                return out
            page += 1


class FixturePluggyClient:
    """Serves Pluggy-shaped JSON from a directory (demo/tests). No network."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))

    def _load(self, name: str | None) -> Any:
        if not name:
            return None
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = self._load(self.index["items"].get(item_id))
        if item is None:
            raise PluggyError(404, "ITEM_NOT_FOUND")
        return item

    def list_accounts(self, item_id: str) -> list[dict[str, Any]]:
        return (self._load(self.index["accounts"].get(item_id)) or {}).get("results", [])

    def list_transactions(self, account_id: str, *, date_from: str | None = None,
                          date_to: str | None = None, created_at_from: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in self.index["transactions"].get(account_id, []):
            out.extend(self._load(name).get("results", []))
        if date_from:
            out = [t for t in out if t["date"][:10] >= date_from]
        if date_to:
            out = [t for t in out if t["date"][:10] <= date_to]
        if created_at_from:
            out = [t for t in out if t["createdAt"] >= created_at_from]
        return out

    def list_bills(self, account_id: str) -> list[dict[str, Any]]:
        name = self.index.get("bills", {}).get(account_id)
        if not name:
            raise PluggyError(400, "BILLS_NOT_AVAILABLE")
        return self._load(name).get("results", [])


class MemoryPluggyClient:
    """In-memory fake used by tests: mutate `.transactions[account_id]` between syncs."""

    def __init__(self, items: dict[str, dict], accounts: dict[str, list[dict]],
                 transactions: dict[str, list[dict]], bills: dict[str, list[dict]] | None = None):
        self.items, self.accounts, self.transactions = items, accounts, transactions
        self.bills = bills or {}
        self.calls: list[tuple] = []

    def get_item(self, item_id: str) -> dict[str, Any]:
        return self.items[item_id]

    def list_accounts(self, item_id: str) -> list[dict[str, Any]]:
        return self.accounts.get(item_id, [])

    def list_transactions(self, account_id: str, *, date_from: str | None = None,
                          date_to: str | None = None, created_at_from: str | None = None) -> list[dict[str, Any]]:
        self.calls.append((account_id, date_from, created_at_from))
        out = list(self.transactions.get(account_id, []))
        if date_from:
            out = [t for t in out if t["date"][:10] >= date_from]
        if created_at_from:
            out = [t for t in out if t["createdAt"] >= created_at_from]
        return out

    def list_bills(self, account_id: str) -> list[dict[str, Any]]:
        if account_id not in self.bills:
            raise PluggyError(400, "BILLS_NOT_AVAILABLE")
        return self.bills[account_id]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _error(resp: httpx.Response) -> PluggyError:
    code = None
    message = ""
    try:
        body = resp.json()
        code = body.get("codeDescription") or body.get("code")
        message = str(body.get("message") or "")[:200]
    except ValueError:
        pass
    return PluggyError(resp.status_code, str(code) if code is not None else None, message)
