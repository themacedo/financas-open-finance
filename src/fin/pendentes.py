"""pendentes.json: the rows waiting for review, for the Revisar screen (site, phone and desktop app).

Unlike painel.json this one has transaction-level data (date, value, the bank's description), so it never goes to
GitHub nor into painel.json: only to the private read-model bucket, behind the Google login of the API. The screen
sends back {id, categoria, essencial}; scripts/aplicar-revisoes.sh applies them with `fin review approve`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fin.config import Taxonomy
from fin.ledger import Ledger

PENDENTES_FILE = "pendentes.json"
MOTIVO = {"llm": "A IA não teve certeza", "blocked": "Tem nome de pessoa (não vai para a IA)",
          "counterpart": "Contraparte nova", "dictionary": "Regra do dicionário pediu revisão", "rule": "Regra pediu revisão"}


def build_pendentes(ledger: Ledger, tax: Taxonomy, now: datetime) -> dict[str, Any]:
    from fin.sync import BANK_NAMES

    contas = {a["id"]: a for a in ledger.accounts()}
    itens = []
    for r in ledger.active_txs("review_status='pendente'"):
        a = contas.get(r["account_id"])
        conta = f"{BANK_NAMES.get(a['bank'], a['bank'])} {'cartão' if a['type'] == 'CREDIT' else 'conta'}" if a else "manual"
        itens.append({
            "id": r["local_id"], "data": r["date"], "valor": round(r["amount_cents"] / 100, 2), "conta": conta,
            "descricao": r["description"] or r["desc_norm"] or "", "natureza": r["natureza"],
            "sugestao": r["categoria"] if r["categoria"] and r["categoria"] != "Revisar" else None,
            "motivo": MOTIVO.get(r["cat_source"] or "", "Para revisar"),
            "parcela": f"{r['installment_number']}/{r['total_installments']}" if (r["total_installments"] or 0) > 1 else None,
        })
    itens.sort(key=lambda i: (i["data"], i["id"]), reverse=True)  # mais recentes primeiro
    categorias = [{"nome": n, "natureza": tax.natureza_of(n)} for n in tax.names if n != "Revisar"]
    return {"geradoEm": now.isoformat(timespec="seconds"), "itens": itens, "categorias": categorias,
            "essencial": ["Essencial", "Supérfluo"]}


def export_pendentes(ledger: Ledger, tax: Taxonomy, data_dir: Path, now: datetime) -> Path:
    out, tmp = data_dir / PENDENTES_FILE, data_dir / (PENDENTES_FILE + ".tmp")
    data = json.dumps(build_pendentes(ledger, tax, now), ensure_ascii=False, indent=1)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, out)
    return out
