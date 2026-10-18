"""SQLite ledger: the source of truth. Lives inside the encrypted vault.

Privacy by design: CPFs are never stored in clear (only keyed hashes); the raw
Pluggy JSON is stored scrubbed (no names, documents, account numbers or `reason`).
"""

from __future__ import annotations

import json
import secrets as _stdlib_secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE accounts (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        bank TEXT NOT NULL,
        type TEXT NOT NULL,                 -- BANK | CREDIT | MANUAL
        subtype TEXT,
        local_key TEXT NOT NULL,            -- bank:subtype:last4 (survives itemId changes)
        name TEXT,
        last4 TEXT,
        owner_doc_hash TEXT,
        transfer_key TEXT,                  -- hash(compe|account digits)
        balance_cents INTEGER,
        credit_limit_cents INTEGER,
        available_credit_cents INTEGER,
        balance_close_date TEXT,
        balance_due_date TEXT,
        minimum_payment_cents INTEGER,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE transactions (
        local_id INTEGER PRIMARY KEY AUTOINCREMENT,
        pluggy_id TEXT UNIQUE,
        provider_id TEXT,
        natural_key TEXT NOT NULL,
        account_id TEXT NOT NULL,
        account_local_key TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'pluggy',   -- pluggy | manual
        date TEXT NOT NULL,
        competence TEXT NOT NULL,                -- YYYY-MM
        amount_cents INTEGER NOT NULL,           -- signed, net-worth view (+ in, - out)
        currency TEXT,
        orig_amount_cents INTEGER,
        orig_currency TEXT,
        direction TEXT NOT NULL,                 -- DEBIT | CREDIT (as sent by Pluggy)
        status TEXT NOT NULL,                    -- PENDING | POSTED
        description TEXT NOT NULL,
        desc_norm TEXT NOT NULL,
        operation_type TEXT,
        payment_method TEXT,
        counterpart_kind TEXT,                   -- own | person | company | unknown | none
        counterpart_hash TEXT,
        counterpart_cnpj TEXT,
        counterpart_alias TEXT,
        counterpart_transfer_key TEXT,
        installment_number INTEGER,
        total_installments INTEGER,
        purchase_date TEXT,
        bill_id TEXT,
        bill_forecast TEXT,
        sign_mismatch INTEGER NOT NULL DEFAULT 0,
        natureza TEXT,
        categoria TEXT,
        subcategoria TEXT,
        necessidade TEXT,
        essencial TEXT,
        fonte TEXT,
        cat_source TEXT,          -- rule | dictionary | counterpart | cache | llm | manual | blocked | none
        rule TEXT,
        confidence REAL,
        review_status TEXT NOT NULL DEFAULT 'auto',  -- auto | pendente | aprovado
        pair_id TEXT,
        missing_since TEXT,
        missing_count INTEGER NOT NULL DEFAULT 0,
        removed_upstream INTEGER NOT NULL DEFAULT 0,
        raw_json TEXT,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX ix_tx_account_date ON transactions(account_id, date);
    CREATE INDEX ix_tx_natural ON transactions(natural_key);
    CREATE INDEX ix_tx_provider ON transactions(account_local_key, provider_id);
    CREATE INDEX ix_tx_competence ON transactions(competence);

    CREATE TABLE bills (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        due_date TEXT,
        closing_date TEXT,
        total_cents INTEGER,
        minimum_cents INTEGER,
        finance_charges_json TEXT,
        payments_json TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE installments_projection (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_key TEXT NOT NULL,
        account_id TEXT NOT NULL,
        desc_norm TEXT NOT NULL,
        installment_number INTEGER NOT NULL,
        total_installments INTEGER NOT NULL,
        month TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        natureza TEXT,
        categoria TEXT,
        UNIQUE(group_key, installment_number)
    );

    CREATE TABLE dictionary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,          -- contains | exact | regex | cnpj
        pattern TEXT NOT NULL,
        natureza TEXT,
        categoria TEXT NOT NULL,
        subcategoria TEXT,
        necessidade TEXT,
        essencial TEXT,
        fonte TEXT,
        origin TEXT NOT NULL,        -- seed | user | approved
        priority INTEGER NOT NULL DEFAULT 100,
        created_at TEXT NOT NULL,
        UNIQUE(kind, pattern)
    );

    CREATE TABLE counterparts (
        alias TEXT PRIMARY KEY,      -- PESSOA_017
        key_hash TEXT NOT NULL UNIQUE,
        apelido TEXT,                -- local nickname you choose (never sent to the LLM)
        natureza TEXT,
        categoria TEXT,
        necessidade TEXT,
        essencial TEXT,
        fonte TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE desc_cache (
        desc_norm TEXT PRIMARY KEY,
        categoria TEXT NOT NULL,
        subcategoria TEXT,
        merchant_norm TEXT,
        confidence REAL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE llm_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL,            -- categorize | analyst
        request_json TEXT NOT NULL,    -- exact body sent
        response_json TEXT,
        request_id TEXT,
        status TEXT NOT NULL,
        n_items INTEGER,
        blocked_json TEXT              -- items kept local and why
    );

    CREATE TABLE sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        run_date TEXT NOT NULL,
        status TEXT NOT NULL,          -- running | success | partial | error
        stats_json TEXT,
        alerts_json TEXT
    );
    """,
    # v2: account owner (pf | pj). Existing ledgers: every account becomes pf, the only case before.
    """
    ALTER TABLE accounts ADD COLUMN owner TEXT NOT NULL DEFAULT 'pf';
    """,
    # v3: loans (personal credit, renegotiations). The debt enters the net position; amounts in cents.
    """
    CREATE TABLE loans (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        product TEXT,
        contract_date TEXT,
        contract_cents INTEGER,
        cet REAL,                    -- annual effective cost, as a fraction (2.16 = 216% a.a.)
        installments_total INTEGER,
        installments_paid INTEGER,
        first_due_date TEXT,
        settlement_date TEXT,
        outstanding_cents INTEGER,   -- what is owed today, interest to date included
        next_installment_cents INTEGER,
        updated_at TEXT NOT NULL
    );
    """,
    # v4: investments (balance today) and, for loans and investments, the mirror item whose accounts move them
    """
    CREATE TABLE investments (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        mirror_item_id TEXT,
        type TEXT,
        subtype TEXT,
        name TEXT,
        balance_cents INTEGER NOT NULL DEFAULT 0,
        status TEXT,
        updated_at TEXT NOT NULL
    );
    ALTER TABLE loans ADD COLUMN mirror_item_id TEXT;
    """,
]


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_id_str(local_id: int) -> str:
    return f"t{local_id:06d}"


def parse_local_id(text: str) -> int:
    text = text.strip().lower()
    return int(text[1:] if text.startswith("t") else text)


class Ledger:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA temp_store=MEMORY")  # temp files would land outside the vault
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    # ---- infra -----------------------------------------------------------
    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.tx():
                for stmt in _split_sql(script):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version={i}")
        if self.meta("salt") is None:
            self.set_meta("salt", _stdlib_secrets.token_hex(16))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def salt(self) -> str:
        return self.meta("salt") or ""

    # ---- accounts --------------------------------------------------------
    def upsert_account(self, acc: dict[str, Any]) -> None:
        acc = {**acc, "updated_at": now_iso()}
        cols = list(acc)
        old = self.account(acc["id"])
        if old is not None and acc.get("local_key") and old["local_key"] != acc["local_key"]:
            # local_key changed (e.g. the account became owner pj): keep its history linked, never duplicate it
            self.x("UPDATE transactions SET account_local_key=? WHERE account_local_key=? AND account_id=?",
                   (acc["local_key"], old["local_key"], acc["id"]))
        self.x(
            f"INSERT INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'id')}",
            [acc[c] for c in cols],
        )

    def accounts(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM accounts ORDER BY bank, type, id")

    def account_owners(self) -> dict[str, str]:
        """account id -> pf | pj (accounts not in the table, e.g. manual rows, are pf)."""
        return {r["id"]: r["owner"] or "pf" for r in self.q("SELECT id, owner FROM accounts")}

    def has_pj(self) -> bool:
        return self.one("SELECT 1 FROM accounts WHERE owner='pj' LIMIT 1") is not None

    def account(self, account_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM accounts WHERE id=?", (account_id,))

    def own_doc_hashes(self) -> set[str]:
        return {r["owner_doc_hash"] for r in self.q("SELECT owner_doc_hash FROM accounts WHERE type='BANK' AND owner_doc_hash IS NOT NULL")}

    def own_transfer_keys(self) -> set[str]:
        return {r["transfer_key"] for r in self.q("SELECT transfer_key FROM accounts WHERE transfer_key IS NOT NULL")}

    # ---- transactions ----------------------------------------------------
    def insert_tx(self, row: dict[str, Any]) -> int:
        ts = now_iso()
        row = {**row, "first_seen": ts, "updated_at": ts}
        cols = list(row)
        cur = self.x(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [row[c] for c in cols])
        return int(cur.lastrowid)

    def update_tx(self, local_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        fields = {**fields, "updated_at": now_iso()}
        self.x(f"UPDATE transactions SET {','.join(f'{c}=?' for c in fields)} WHERE local_id=?", [*fields.values(), local_id])

    def tx_by_local(self, local_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM transactions WHERE local_id=?", (local_id,))

    def active_txs(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.q(f"SELECT * FROM transactions WHERE removed_upstream=0 AND ({where}) ORDER BY date, local_id", params)

    # ---- dictionary ------------------------------------------------------
    def add_dictionary(self, entry: dict[str, Any]) -> None:
        entry = {"priority": 100, **entry, "created_at": now_iso()}
        cols = list(entry)
        self.x(
            f"INSERT INTO dictionary({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            f"ON CONFLICT(kind, pattern) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c not in ('kind', 'pattern', 'created_at'))}",
            [entry[c] for c in cols],
        )

    def dictionary(self) -> list[sqlite3.Row]:
        # exact/approved first, then longer patterns, so specific beats generic
        return self.q("SELECT * FROM dictionary ORDER BY priority, CASE kind WHEN 'cnpj' THEN 0 WHEN 'exact' THEN 1 ELSE 2 END, length(pattern) DESC")

    # ---- counterparts (people) --------------------------------------------
    def counterpart_alias(self, key_hash: str) -> str:
        row = self.one("SELECT alias FROM counterparts WHERE key_hash=?", (key_hash,))
        if row:
            return row["alias"]
        n = self.one("SELECT COUNT(*) AS n FROM counterparts")["n"] + 1
        alias = f"PESSOA_{n:03d}"
        self.x("INSERT INTO counterparts(alias,key_hash,created_at) VALUES(?,?,?)", (alias, key_hash, now_iso()))
        return alias

    def counterpart(self, alias: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM counterparts WHERE alias=?", (alias,))

    # ---- audit -----------------------------------------------------------
    def audit(self, kind: str, request: dict[str, Any], status: str, n_items: int,
              blocked: list[dict] | None = None) -> int:
        cur = self.x(
            "INSERT INTO llm_audit(created_at,kind,request_json,status,n_items,blocked_json) VALUES(?,?,?,?,?,?)",
            (now_iso(), kind, json.dumps(request, ensure_ascii=False, sort_keys=True), status, n_items,
             json.dumps(blocked or [], ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def audit_result(self, audit_id: int, status: str, response: Any = None, request_id: str | None = None) -> None:
        self.x("UPDATE llm_audit SET status=?, response_json=?, request_id=? WHERE id=?",
               (status, json.dumps(response, ensure_ascii=False) if response is not None else None, request_id, audit_id))

    # ---- sync runs -------------------------------------------------------
    def start_run(self, run_date: str) -> int:
        cur = self.x("INSERT INTO sync_runs(started_at,run_date,status) VALUES(?,?,'running')", (now_iso(), run_date))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict, alerts: list[str]) -> None:
        self.x("UPDATE sync_runs SET finished_at=?, status=?, stats_json=?, alerts_json=? WHERE id=?",
               (now_iso(), status, json.dumps(stats, ensure_ascii=False), json.dumps(alerts, ensure_ascii=False), run_id))

    def last_success_date(self) -> str | None:
        row = self.one("SELECT run_date FROM sync_runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1")
        return row["run_date"] if row else None

    def last_run(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def scrub_raw(tx: dict[str, Any]) -> dict[str, Any]:
    """Drop personal fields from a Pluggy transaction before persisting it."""
    clean = json.loads(json.dumps(tx))
    pd = clean.get("paymentData")
    if isinstance(pd, dict):
        for side in ("payer", "receiver"):
            party = pd.get(side)
            if isinstance(party, dict):
                doc = (party.get("documentNumber") or {}).get("type")
                pd[side] = {"documentType": doc}
        for k in ("reason", "receiverReferenceId", "referenceNumber", "authenticationCode", "boletoMetadata"):
            pd.pop(k, None)
    for k in ("descriptionRaw",):
        clean.pop(k, None)
    return clean
