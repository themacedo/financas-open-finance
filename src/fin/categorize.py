"""Categorization pipeline: dictionary -> cache -> LLM (batch) -> your approval.

What may reach the LLM: an opaque per-request id, the normalized merchant
description and "debito"/"credito". Never dates, accounts, balances, documents
or people. Pix/TED to people are handled in rules.py and never get here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fin.config import Config, Taxonomy
from fin.ledger import Ledger, now_iso
from fin.llm import Categorizer
from fin.normalize import is_generic
from fin.privacy import block_reason

log = logging.getLogger("fin.categorize")

EDITABLE_SOURCES = (None, "none", "cache", "llm", "dictionary", "blocked")
FIXED_NATUREZAS = ("Receita", "Transferência", "Investimento")


def _matches(entry: Any, row: Any) -> bool:
    kind, pat = entry["kind"], entry["pattern"]
    desc = row["desc_norm"] or ""
    if kind == "cnpj":
        return bool(row["counterpart_cnpj"]) and row["counterpart_cnpj"] == pat
    if kind == "exact":
        return desc == pat
    if kind == "regex":
        return re.search(pat, desc) is not None
    if kind == "raw":  # regex on the bank's original text (keeps digits: "99*", "Dl*99 Ride")
        return re.search(pat, row["description"] or "", re.I) is not None
    return re.search(rf"(?:^|\b){re.escape(pat)}(?:\b|$)", desc) is not None


def _eligible(row: Any) -> bool:
    # a rule that only managed "Revisar" (card credit, debit with a person's name, ...) can be resolved by
    # your dictionary too
    rule_unresolved = row["cat_source"] in ("rule", "none", "blocked") and row["categoria"] == "Revisar"
    return (row["review_status"] != "aprovado" and (row["cat_source"] in EDITABLE_SOURCES or rule_unresolved)
            and row["counterpart_kind"] != "person" and row["source"] == "pluggy")


def _llm_allowed(row: Any) -> bool:
    """Last gate before the LLM: anything that looks like a transfer and is not to a company stays local
    (the rules already route it to the counterparts table; this covers rows a rule did not catch)."""
    from fin.rules import _is_transfer_like

    return row["counterpart_kind"] in ("company", "own") or not _is_transfer_like(row)


def apply_dictionary(ledger: Ledger, tax: Taxonomy) -> int:
    entries = ledger.dictionary()
    n = 0
    with ledger.tx():
        for row in ledger.active_txs():
            if not _eligible(row):
                continue
            entry = next((e for e in entries if _matches(e, row)), None)
            if entry is None:
                if row["cat_source"] == "dictionary":  # entry was removed/edited
                    ledger.update_tx(row["local_id"], {"cat_source": None, "categoria": None, "review_status": "auto"})
                continue
            cat = entry["categoria"]
            natureza = row["natureza"] if row["rule"] == "refund" else (entry["natureza"] or tax.natureza_of(cat))
            fields = {
                "categoria": cat, "subcategoria": entry["subcategoria"], "natureza": natureza,
                "necessidade": entry["necessidade"] or tax.necessidade_of(cat), "essencial": entry["essencial"],
                "fonte": entry["fonte"], "cat_source": "dictionary", "review_status": "auto", "confidence": None,
            }
            if any(row[k] != v for k, v in fields.items()):
                ledger.update_tx(row["local_id"], fields)
                n += 1
    return n


RE_REFUND_PREFIX = re.compile(r"^(?:ESTORNO|REEMBOLSO|CANCELAMENTO|DEVOLUCAO|EST)\s+(?:DE\s+|DA\s+|DO\s+)?")


def refund_base(desc_norm: str) -> str:
    return RE_REFUND_PREFIX.sub("", desc_norm or "").strip()


def _original_purchase(ledger: Ledger, row: Any) -> Any:
    return ledger.one(
        "SELECT * FROM transactions WHERE account_local_key=? AND desc_norm=? AND amount_cents<0 AND removed_upstream=0 "
        "ORDER BY abs(julianday(date)-julianday(?)) LIMIT 1",
        (row["account_local_key"], refund_base(row["desc_norm"]), row["date"]))


def _inherit_refunds(ledger: Ledger) -> int:
    """A refund takes the category of its purchase (and follows it when you approve the purchase)."""
    n = 0
    with ledger.tx():
        for row in ledger.active_txs("rule='refund' AND review_status!='aprovado' AND COALESCE(cat_source,'') NOT IN ('manual','dictionary')"):
            orig = _original_purchase(ledger, row)
            if not orig or not orig["categoria"]:
                continue
            fields = {"categoria": orig["categoria"], "subcategoria": orig["subcategoria"],
                      "necessidade": orig["necessidade"], "essencial": orig["essencial"], "cat_source": "rule",
                      "review_status": "pendente" if orig["review_status"] == "pendente" else "auto"}
            if any(row[k] != v for k, v in fields.items()):
                ledger.update_tx(row["local_id"], fields)
                n += 1
    return n


def _apply_cache(ledger: Ledger, tax: Taxonomy) -> int:
    n = 0
    with ledger.tx():
        for row in ledger.active_txs("cat_source IS NULL OR cat_source='none'"):
            if not _eligible(row):
                continue
            hit = ledger.one("SELECT * FROM desc_cache WHERE desc_norm=?", (row["desc_norm"],))
            if hit:
                _set_suggestion(ledger, tax, row, hit["categoria"], hit["subcategoria"], hit["confidence"], "cache")
                n += 1
    return n


def _set_suggestion(ledger: Ledger, tax: Taxonomy, row: Any, cat: str, sub: str | None, conf: float | None, source: str) -> None:
    natureza = row["natureza"] if row["rule"] == "refund" else tax.natureza_of(cat)
    ledger.update_tx(row["local_id"], {
        "categoria": cat, "subcategoria": sub or None, "natureza": natureza,
        "necessidade": tax.necessidade_of(cat), "cat_source": source, "review_status": "pendente",
        "confidence": conf,
    })


def run_categorize(ledger: Ledger, cfg: Config, tax: Taxonomy, categorizer: Categorizer | None) -> dict[str, int]:
    stats = {"dictionary": apply_dictionary(ledger, tax)}
    stats["refund_inherited"] = _inherit_refunds(ledger)
    stats["cache"] = _apply_cache(ledger, tax)

    pending = [r for r in ledger.active_txs("cat_source IS NULL") if _eligible(r)]
    llm_rows: dict[str, list[Any]] = {}
    blocked: list[dict[str, str]] = []
    with ledger.tx():
        for r in pending:
            is_refund = r["rule"] == "refund"
            if r["amount_cents"] > 0 and not is_refund:
                # Unknown income: the source (Cliente PJ/Empresa CLT/Pessoal) is your decision, not the AI's.
                ledger.update_tx(r["local_id"], {"natureza": "Receita", "categoria": "Revisar", "cat_source": "none",
                                                 "review_status": "pendente"})
                continue
            if r["natureza"] in FIXED_NATUREZAS and not is_refund:
                continue
            if is_refund and _original_purchase(ledger, r) is not None:
                continue  # inherits from the purchase after it is categorized
            # raw description too: normalization drops digits and "@", which the patterns look for
            reason = (block_reason(r["desc_norm"], cfg.block_terms)
                      or (block_reason(r["description"], cfg.block_terms) if r["description"] else None)
                      or (None if _llm_allowed(r) else "transfer"))
            if reason:
                blocked.append({"local_id": r["local_id"], "reason": reason})
                ledger.update_tx(r["local_id"], {"natureza": r["natureza"] or "Despesa", "categoria": "Revisar",
                                                 "cat_source": "blocked", "review_status": "pendente"})
                continue
            llm_rows.setdefault(r["desc_norm"], []).append(r)
    if blocked:
        ledger.audit("blocked", {}, "not_sent", 0, blocked)  # what stayed local and why (no text)
    stats["blocked"] = len(blocked)

    if not llm_rows:
        stats["llm_sent"] = 0
        _finish(ledger, stats)
        return stats
    if not (cfg.llm_enabled and categorizer):
        with ledger.tx():
            for rows in llm_rows.values():
                for r in rows:
                    ledger.update_tx(r["local_id"], {"natureza": r["natureza"] or "Despesa", "categoria": "Revisar",
                                                     "cat_source": "none", "review_status": "pendente"})
        stats["llm_sent"] = 0
        _finish(ledger, stats)
        return stats

    descs = sorted(llm_rows)
    sent = 0
    if not descs:
        stats["llm_sent"] = 0
        _finish(ledger, stats)
        return stats
    llm_cats = tax.llm_categories
    for start in range(0, len(descs), cfg.llm_chunk_size):
        chunk = descs[start:start + cfg.llm_chunk_size]
        items = []
        for i, desc in enumerate(chunk, start=1):
            sample = llm_rows[desc][0]
            items.append({"id": f"d{i}", "desc": desc, "tipo": "debito" if sample["amount_cents"] < 0 else "credito"})
        results = categorizer.categorize(items, llm_cats)
        sent += len(items)
        by_id = {r["id"]: r for r in results}
        with ledger.tx():
            for item in items:
                res = by_id.get(item["id"])
                if res is None:
                    continue
                cat = res["categoria"]
                conf = float(res.get("confianca") or 0.0)
                if conf < cfg.llm_min_confidence:
                    cat = "Revisar"
                if conf > 0:
                    ledger.x("INSERT INTO desc_cache(desc_norm,categoria,subcategoria,merchant_norm,confidence,source,created_at) "
                             "VALUES(?,?,?,?,?,?,?) ON CONFLICT(desc_norm) DO UPDATE SET categoria=excluded.categoria, "
                             "subcategoria=excluded.subcategoria, confidence=excluded.confidence",
                             (item["desc"], cat, res.get("subcategoria"), res.get("estabelecimento_normalizado"), conf, "llm", now_iso()))
                for r in llm_rows[item["desc"]]:
                    _set_suggestion(ledger, tax, r, cat, res.get("subcategoria"), conf, "llm" if conf > 0 else "none")
    stats["llm_sent"] = sent
    _finish(ledger, stats)
    return stats


def _finish(ledger: Ledger, stats: dict[str, int]) -> None:
    from fin.rules import project_installments

    stats["refund_inherited"] += _inherit_refunds(ledger)
    project_installments(ledger)  # projections carry the (new) category


class ReviewError(ValueError):
    pass


def approve(ledger: Ledger, tax: Taxonomy, local_id: int, *, categoria: str | None = None,
            natureza: str | None = None, essencial: str | None = None, necessidade: str | None = None,
            fonte: str | None = None, learn: bool = True) -> dict[str, Any]:
    row = ledger.tx_by_local(local_id)
    if row is None:
        raise ReviewError(f"lançamento t{local_id:06d} não existe")
    cat = tax.canonical(categoria or row["categoria"])
    if cat is None or cat == "Revisar":
        raise ReviewError("escolha uma categoria válida (diferente de Revisar)")
    if natureza and natureza not in tax.naturezas:
        raise ReviewError(f"natureza inválida: {natureza}")
    if essencial and essencial not in tax.essencial:
        raise ReviewError(f"essencial inválido: {essencial} (use {', '.join(tax.essencial)})")
    if necessidade and necessidade not in tax.necessidades:
        raise ReviewError(f"necessidade inválida: {necessidade}")
    if fonte and fonte not in tax.fontes:
        raise ReviewError(f"fonte inválida: {fonte}")
    keep_natureza = row["rule"] in ("refund", "person", "salary", "income_source") and row["natureza"]
    nat = natureza or (row["natureza"] if keep_natureza and tax.natureza_of(cat) != "Transferência" else tax.natureza_of(cat))
    fields = {"categoria": cat, "natureza": nat, "necessidade": necessidade or row["necessidade"] or tax.necessidade_of(cat),
              "essencial": essencial or row["essencial"], "fonte": fonte or row["fonte"],
              "cat_source": "manual", "review_status": "aprovado"}
    learned = None
    with ledger.tx():
        ledger.update_tx(local_id, fields)
        if learn:
            if row["counterpart_alias"]:
                from fin.rules import person_key

                if not person_key(row, ledger.salt)[1]:
                    learned = None  # no document and a generic description: nothing to learn from
                else:
                    ledger.x("UPDATE counterparts SET natureza=?, categoria=?, necessidade=?, essencial=?, fonte=? WHERE alias=?",
                             (nat, cat, fields["necessidade"], fields["essencial"], fields["fonte"], row["counterpart_alias"]))
                    learned = f"contraparte {row['counterpart_alias']}"
            elif row["counterpart_cnpj"] and row["amount_cents"] > 0:
                ledger.add_dictionary({"kind": "cnpj", "pattern": row["counterpart_cnpj"], "natureza": nat, "categoria": cat,
                                       "necessidade": fields["necessidade"], "essencial": fields["essencial"],
                                       "fonte": fields["fonte"], "origin": "approved", "priority": 10})
                learned = "dicionário (CNPJ)"
            elif row["desc_norm"] and row["source"] == "pluggy" and not is_generic(row["desc_norm"]):
                ledger.add_dictionary({"kind": "exact", "pattern": row["desc_norm"], "natureza": None if keep_natureza else nat,
                                       "categoria": cat, "subcategoria": row["subcategoria"],
                                       "necessidade": fields["necessidade"], "essencial": fields["essencial"],
                                       "fonte": fields["fonte"], "origin": "approved", "priority": 10})
                learned = f"dicionário ({row['desc_norm']})"
    if learn:
        apply_dictionary(ledger, tax)
        _finish(ledger, {"refund_inherited": 0})
    return {"local_id": local_id, "categoria": cat, "natureza": nat, "learned": learned}


def load_seed_dictionary(ledger: Ledger, entries: list[dict[str, Any]]) -> int:
    """Seed entries never overwrite your own (user/approved) entries."""
    n = 0
    for e in entries:
        kind = e.get("kind", "contains")
        pattern = str(e["pattern"]).upper() if kind != "cnpj" else "".join(c for c in str(e["pattern"]) if c.isdigit())
        existing = ledger.one("SELECT origin FROM dictionary WHERE kind=? AND pattern=?", (kind, pattern))
        if existing and existing["origin"] != "seed":
            continue
        ledger.add_dictionary({"kind": kind, "pattern": pattern, "natureza": e.get("natureza"), "categoria": e["categoria"],
                               "subcategoria": e.get("subcategoria"), "necessidade": e.get("necessidade"),
                               "essencial": e.get("essencial"), "fonte": e.get("fonte"),
                               "origin": e.get("origin", "seed"), "priority": int(e.get("priority", 100))})
        n += 1
    return n
