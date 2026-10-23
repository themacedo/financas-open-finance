"""Applies the decisions made on the Revisar screen (site, phone, desktop app) to the ledger.

They arrive through the cloud queue (Pub/Sub, subscription fin-comandos-mac): scripts/aplicar-revisoes.sh pulls a
batch, this module applies it with the same `approve` as `fin review approve` and says which messages may be
acknowledged. A row that is no longer pending (approved here meanwhile) is left alone: the decision made later on
the Mac wins over an older one from the queue. Two decisions for the same row in one batch: the newest one counts.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fin.categorize import ReviewError, approve
from fin.config import Taxonomy
from fin.ledger import Ledger


def ler_lote(pull: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The output of `gcloud pubsub subscriptions pull --format=json` -> [{ackId, revisao | erro}]."""
    out = []
    for m in pull:
        ack = m.get("ackId", "")
        try:
            corpo = json.loads(base64.b64decode(m["message"]["data"]))
            if corpo.get("tipo") != "revisao":
                raise ValueError(f"tipo desconhecido: {corpo.get('tipo')}")
            out.append({"ackId": ack, "revisao": corpo})
        except (KeyError, ValueError, TypeError) as exc:
            out.append({"ackId": ack, "erro": f"mensagem inválida: {exc}"})
    return out


def aplicar(ledger: Ledger, tax: Taxonomy, lote: list[dict[str, Any]]) -> dict[str, Any]:
    """Applies the batch. Every message gets a result; all of them can be acknowledged (an invalid one will not
    become valid by being delivered again). Returns {aplicadas, ignoradas, erros, ackIds, detalhes}."""
    detalhes: list[dict[str, Any]] = []
    ultima: dict[int, dict[str, Any]] = {}
    for item in lote:
        if "erro" in item:
            detalhes.append({"ackId": item["ackId"], "resultado": "erro", "motivo": item["erro"]})
            continue
        r = item["revisao"]
        try:
            lid = int(r["id"])
        except (KeyError, ValueError, TypeError):
            detalhes.append({"ackId": item["ackId"], "resultado": "erro", "motivo": "id inválido"})
            continue
        anterior = ultima.get(lid)
        if anterior is not None and str(anterior["revisao"].get("em", "")) > str(r.get("em", "")):
            detalhes.append({"ackId": item["ackId"], "id": lid, "resultado": "ignorada", "motivo": "há decisão mais nova no lote"})
            continue
        if anterior is not None:
            detalhes.append({"ackId": anterior["ackId"], "id": lid, "resultado": "ignorada", "motivo": "há decisão mais nova no lote"})
        ultima[lid] = item

    for lid, item in ultima.items():
        r = item["revisao"]
        row = ledger.tx_by_local(lid)
        if row is None:
            detalhes.append({"ackId": item["ackId"], "id": lid, "resultado": "erro", "motivo": "lançamento não existe"})
            continue
        if row["review_status"] != "pendente":
            detalhes.append({"ackId": item["ackId"], "id": lid, "resultado": "ignorada", "motivo": "já revisado"})
            continue
        try:
            res = approve(ledger, tax, lid, categoria=r.get("categoria"), essencial=r.get("essencial") or None)
            detalhes.append({"ackId": item["ackId"], "id": lid, "resultado": "aplicada", "categoria": res["categoria"],
                             "aprendido": res["learned"]})
        except ReviewError as exc:
            detalhes.append({"ackId": item["ackId"], "id": lid, "resultado": "erro", "motivo": str(exc)})

    conta = lambda k: sum(1 for d in detalhes if d["resultado"] == k)  # noqa: E731
    return {"aplicadas": conta("aplicada"), "ignoradas": conta("ignorada"), "erros": conta("erro"),
            "ackIds": [d["ackId"] for d in detalhes if d.get("ackId")], "detalhes": detalhes}
