"""Caju (meal voucher) entered by hand: CSV or YAML in the data dir.

CSV (semicolon, BRL format), header required:
    data;descricao;valor;categoria;fonte
    2026-10-01;Crédito Caju;1000,00;Salário CLT;Empresa CLT
    2026-10-31;Gastos do mês;-812,40;;
YAML: a list with the same keys. Positive = credit, negative = spend.
Re-importing is idempotent; lines removed from the file are soft-removed in the ledger.
"""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from fin.config import Taxonomy
from fin.ledger import Ledger
from fin.money import parse_brl, to_cents
from fin.normalize import normalize_description

ACCOUNT_ID = "caju"


def read_entries(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in (".yaml", ".yml"):
        return list(yaml.safe_load(path.read_text(encoding="utf-8")) or [])
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _value(raw: Any) -> int:
    if isinstance(raw, (int, float)):
        return to_cents(raw)
    return parse_brl(str(raw))


def import_caju(ledger: Ledger, tax: Taxonomy, path: Path) -> dict[str, int]:
    entries = read_entries(path)
    ledger.upsert_account({"id": ACCOUNT_ID, "item_id": "manual", "bank": "caju", "type": "MANUAL",
                           "subtype": "MEAL_VOUCHER", "local_key": "caju:MANUAL:", "name": "Caju"})
    occ: dict[tuple, int] = defaultdict(int)
    keys: set[str] = set()
    stats = {"inserted": 0, "updated": 0, "removed": 0}
    with ledger.tx():
        for e in entries:
            day = str(e.get("data") or e.get("date") or "").strip()[:10]
            desc = str(e.get("descricao") or "Caju").strip()
            amount = _value(e.get("valor"))
            k = (day, desc, amount)
            key = "caju:" + hashlib.sha256(f"{day}|{desc}|{amount}|{occ[k]}".encode()).hexdigest()[:24]
            occ[k] += 1
            keys.add(key)
            cat = tax.canonical(str(e.get("categoria") or "")) or ("Salário CLT" if amount > 0 else "Alimentação")
            row = {
                "pluggy_id": key, "natural_key": key, "account_id": ACCOUNT_ID, "account_local_key": "caju:MANUAL:",
                "source": "manual", "date": day, "competence": day[:7], "amount_cents": amount, "currency": "BRL",
                "direction": "CREDIT" if amount > 0 else "DEBIT", "status": "POSTED", "description": desc,
                "desc_norm": normalize_description(desc), "counterpart_kind": "none",
                "natureza": tax.natureza_of(cat), "categoria": cat, "necessidade": tax.necessidade_of(cat),
                "fonte": (str(e.get("fonte") or "").strip() or None), "cat_source": "manual", "review_status": "aprovado",
            }
            current = ledger.one("SELECT local_id FROM transactions WHERE pluggy_id=?", (key,))
            if current:
                ledger.update_tx(current["local_id"], {**{k2: v for k2, v in row.items() if k2 != "pluggy_id"},
                                                       "removed_upstream": 0})
                stats["updated"] += 1
            else:
                ledger.insert_tx(row)
                stats["inserted"] += 1
        for r in ledger.q("SELECT local_id, pluggy_id FROM transactions WHERE account_id=? AND removed_upstream=0", (ACCOUNT_ID,)):
            if r["pluggy_id"] not in keys:
                ledger.update_tx(r["local_id"], {"removed_upstream": 1})
                stats["removed"] += 1
    return stats
