"""painel.json for the Next.js dashboard: aggregates only, built after each sync.

Inputs: bank balances and day-to-day spending from the ledger, the month plan from `plano.yaml`
(in data_dir, next to the ledger) and the weekly history CSV of the performance review.
Nothing that identifies a transaction, a counterpart or a client leaves here: totals, the plan
items as written in plano.yaml and weekly counts.
"""

from __future__ import annotations

import base64
import calendar
import csv
import json
import mimetypes
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from fin.ledger import Ledger

if TYPE_CHECKING:
    from fin.config import Config

PLANO_FILE = "plano.yaml"
PAINEL_FILE = "painel.json"
SEMANAS_CSV = Path.home() / "dados/historico_semanal.csv"
NOTAS_JSON = Path.home() / "dados/notas_semanais.json"
N_SEMANAS = 8


def _cents(v: Any) -> int:
    return round(float(v or 0) * 100)


def _reais(c: int) -> float:
    return round(c / 100, 2)


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, date) else str(v)


def load_plano(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def status_of(item: dict[str, Any], today: date) -> str:
    if item.get("feito"):
        return "feito"
    if item.get("marco") or item.get("atencao") or _iso(item["data"]) < today.isoformat():
        return "atencao"  # the milestone, a flagged item, or something planned that is already late
    return "previsto"


def compute_financas(plano: dict[str, Any], contas: list[dict[str, Any]], gasto_dia_cents: int,
                     today: date) -> dict[str, Any]:
    """Pure: the plan math. Only items not yet done move money; done items are already in the balances."""
    itens = sorted(plano.get("plano") or [], key=lambda i: _iso(i["data"]))
    marco_item = next((i for i in itens if i.get("marco")), None)
    if marco_item is None:
        raise ValueError("plano.yaml: marque um item do plano com marco: true")
    m = plano.get("marco") or {}
    data_marco, sobra_ate = _iso(marco_item["data"]), _iso(m["sobra_ate"])
    pendentes = [i for i in itens if not i.get("feito") and i is not marco_item]

    saldo = sum(c["saldo_cents"] for c in contas)
    ate_marco = [i for i in pendentes if _iso(i["data"]) <= data_marco]
    disponivel = saldo + sum(_cents(i.get("entra")) - _cents(i.get("sai")) for i in ate_marco)
    pagar = _cents(marco_item.get("sai"))

    # After the milestone and before the next salary. The salary day itself (sobra_ate) is out, inflows and outflows:
    # what is paid that day is paid with that salary.
    janela = [i for i in pendentes if data_marco < _iso(i["data"]) < sobra_ate]
    depois = sum(_cents(i.get("entra")) - _cents(i.get("sai")) for i in janela)
    dia = plano.get("dia_a_dia") or {}
    orcamento = _cents(dia.get("orcamento"))
    restante_dia = max(0, orcamento - gasto_dia_cents) if dia and _iso(dia["ate"]) <= sobra_ate else 0
    sobra = disponivel - pagar + depois - restante_dia

    meta = plano.get("meta") or {}
    depositos = sorted(meta.get("depositos") or [], key=lambda d: _iso(d["data"]))
    proximo = next((d for d in depositos if not d.get("feito")), None)
    return {
        "contas": [{"nome": c["nome"], "tipo": c["tipo"], "saldo": _reais(c["saldo_cents"])} for c in contas],
        "marco": {
            "titulo": m.get("titulo") or marco_item["descricao"],
            "data": data_marco,
            "disponivel": _reais(disponivel),
            "pagar": _reais(pagar),
            "sobra": _reais(sobra),
            "sobraAte": sobra_ate,
            "dependeDe": list(m.get("depende_de") or []),
        },
        "meta": {
            "titulo": meta.get("titulo", "Meta"),
            "alvo": _reais(_cents(meta.get("alvo"))),
            "acumulado": _reais(sum(_cents(d["valor"]) for d in depositos if d.get("feito"))),
            "proximo": {"data": _iso(proximo["data"]), "valor": _reais(_cents(proximo["valor"]))} if proximo else None,
            "depositos": [{"data": _iso(d["data"]), "valor": _reais(_cents(d["valor"])), "feito": bool(d.get("feito"))}
                          for d in depositos],
        },
        "compromissos": [{"descricao": c["descricao"], "mensal": _reais(_cents(c["mensal"])), "de": str(c["de"])[:7],
                          "ate": str(c["ate"])[:7]} for c in plano.get("compromissos") or []],
        "diaADia": {"orcamento": _reais(orcamento), "gasto": _reais(gasto_dia_cents), "ate": _iso(dia.get("ate", ""))},
        "plano": [
            {"data": _iso(i["data"]), "descricao": i["descricao"], "status": status_of(i, today),
             **({"entra": _reais(_cents(i["entra"]))} if i.get("entra") else {}),
             **({"sai": _reais(_cents(i["sai"]))} if i.get("sai") else {})}
            for i in itens
        ],
    }


def bank_accounts(ledger: Ledger, nomes: dict[str, str]) -> list[dict[str, Any]]:
    """Checking accounts only (a card balance is debt, not money you have)."""
    out = []
    for a in ledger.q("SELECT bank, owner, balance_cents FROM accounts WHERE type='BANK' ORDER BY owner DESC, bank"):
        owner = (a["owner"] or "pf").upper()
        out.append({"nome": nomes.get(a["bank"]) or f"{a['bank'].capitalize()}{' PJ' if owner == 'PJ' else ''}",
                    "tipo": owner, "saldo_cents": a["balance_cents"] or 0})
    return out


def day_to_day_spent(ledger: Ledger, dia: dict[str, Any], today: date) -> int:
    if not dia:
        return 0
    cats = list(dia.get("categorias") or [])
    excluir = [int(x) for x in dia.get("excluir") or []]
    fim = min(_iso(dia["ate"]), today.isoformat())
    where = (f"t.date >= ? AND t.date <= ? AND t.amount_cents < 0 AND t.natureza='Despesa' "
             f"AND t.categoria IN ({','.join('?' * len(cats))})")
    params: list[Any] = [_iso(dia["desde"]), fim, *cats]
    if excluir:
        where += f" AND t.local_id NOT IN ({','.join('?' * len(excluir))})"
        params += excluir
    row = ledger.one(
        "SELECT COALESCE(-SUM(t.amount_cents), 0) s FROM transactions t JOIN accounts a ON a.id = t.account_id "
        f"WHERE a.type='BANK' AND COALESCE(t.removed_upstream, 0)=0 AND {where}", params)
    return int(row["s"])


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _int(v: str | None) -> int:
    return int(_num(v) or 0)


def load_historico(path: Path) -> list[dict[str, Any]]:
    """Every week of the performance review. Empty cells stay null (not measured), not zero."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [{
        "inicio": r["semana"], "rotulo": r["rotulo"].split("–")[0],
        "commitsClt": _int(r["commits_clt"]), "commitsPj": _int(r["commits_pj"]),
        "prsClt": _int(r["prs_clt"]), "bugsClt": _int(r["bugs_resolvidos"]),
        "entregasPj": _int(r["entregas_pj"]), "cartoesPj": _num(r.get("cartoes_pj")),
        "foraClt": _num(r.get("fora_clt")), "foraPj": _num(r.get("fora_pj")),
        "aprov1Clt": _num(r.get("aprov1_clt")), "aprov1Pj": _num(r.get("aprov1_pj")),
        "constClt": _num(r.get("const_clt")), "constPj": _num(r.get("const_pj")),
        "cicloClt": _num(r.get("ciclo_clt")), "cicloPj": _num(r.get("ciclo_pj")),
        "cyclePrClt": _num(r.get("cycle_pr_clt")), "leadPj": _num(r.get("lead_pj")),
        "regressoesClt": _num(r.get("regressoes")), "pendenciasPj": _num(r.get("pendencias_pj")),
        "atendClt": _num(r.get("atend_clt")),
    } for r in rows]


def fora_pct(semana: dict[str, Any]) -> int:
    """Share of commits outside working hours, weighted by each job's commits."""
    c, p = semana["commitsClt"], semana["commitsPj"]
    fc, fp = semana.get("foraClt") or 0, semana.get("foraPj") or 0
    return round((fc * c + fp * p) / (c + p)) if c + p else 0


def load_notas(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_empresas(plano: dict[str, Any], base: Path) -> dict[str, Any]:
    """Names, colors and logos (embedded as data URLs, so the page needs no other private file)."""
    out = {}
    for chave, padrao in (("clt", "Empresa CLT"), ("pj", "Cliente PJ")):
        e = (plano.get("empresas") or {}).get(chave) or {}
        item: dict[str, Any] = {"nome": e.get("nome", padrao)}
        if e.get("cor"):
            item["cor"] = e["cor"]
        if e.get("logo"):
            logo = (base / e["logo"]).expanduser()
            mime = mimetypes.guess_type(logo.name)[0] or "image/png"
            item["logo"] = f"data:{mime};base64,{base64.b64encode(logo.read_bytes()).decode()}"
        out[chave] = item
    return out


def _mes(d: date, delta: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + delta, 12)
    return date(y, m + 1, 1)


def nome_fixo(nome: str, apelidos: dict[str, str]) -> str:
    """Fixed-cost label without the notes in parentheses (where names of people tend to be)."""
    return apelidos.get(nome) or re.sub(r"\s*\(.*$", "", nome).strip()


PARCELA = re.compile(r"\s*\d{1,2}/\d{1,2}\s*$")
PIX_PREFIXO = re.compile(r"^(TRANSFERENCIA|TRANSFERÊNCIA|PIX)\b.*", re.I)


def rotulo_gasto(row: Any) -> str:
    """Where the money went, safe to show: the merchant, or "Pessoa física" when it reads like a person's name."""
    from fin.privacy import block_reason

    desc = row["description"] or ""
    base = desc.split("|", 1)[1] if "|" in desc else (row["desc_norm"] or desc)
    base = PARCELA.sub("", base).strip()
    if PIX_PREFIXO.match(base):
        base = ""
    if row["counterpart_kind"] == "person" or not base or block_reason(base):
        return "Pessoa física (nome oculto)" if base or row["counterpart_kind"] == "person" else "Pix ou transferência"
    return " ".join(w.capitalize() if len(w) > 2 else w.lower() for w in base.split())


def _top(rows: list[Any], n: int = 3) -> list[dict[str, Any]]:
    por: dict[str, list[int]] = {}
    for r in rows:
        v = por.setdefault(rotulo_gasto(r), [0, 0])
        v[0] += -r["amount_cents"]
        v[1] += 1
    top = sorted(por.items(), key=lambda kv: -kv[1][0])[:n]
    return [{"nome": k, "valor": _reais(v[0]), "n": v[1]} for k, v in top]


def compute_clt(ledger: Ledger, ini: date, today: date) -> dict[str, Any]:
    """Salary entries of the month (category Salário CLT): what arrived and what is scheduled (pending or future)."""
    fim = _mes(ini, 1)
    itens = []
    for r in ledger.active_txs("categoria='Salário CLT' AND amount_cents > 0 AND date >= ? AND date < ?",
                               (ini.isoformat(), fim.isoformat())):
        d = (r["description"] or "").lower()
        # the description says it most of the time; a scheduled entry ("Entrada PAGTO") is named by the day:
        # salary comes around the 5th, the advance around the 20th
        nome = "Adiantamento" if "adiant" in d else "Salário" if "sal" in d else ("Salário" if int(r["date"][8:]) <= 12 else "Adiantamento")
        recebido = r["status"] != "PENDING" and r["date"] <= today.isoformat()
        itens.append({"data": r["date"], "descricao": nome, "valor": _reais(r["amount_cents"]),
                      "situacao": "recebido" if recebido else "previsto"})
    itens.sort(key=lambda i: i["data"])
    soma = lambda s: _reais(sum(_cents(i["valor"]) for i in itens if i["situacao"] == s))
    return {"itens": itens, "recebido": soma("recebido"), "previsto": soma("previsto")}


def compute_detalhe(ledger: Ledger, cfg: "Config", plano: dict[str, Any], today: date) -> dict[str, Any]:
    """Month by category against the same days of last month, fixed costs, future installments and the PJ."""
    from fin.reports import committed_installments, date_range_totals, fixed_cost_status, month_totals

    ini, ant = _mes(today, 0), _mes(today, -1)
    fim_ant = date(ant.year, ant.month, min(today.day, calendar.monthrange(ant.year, ant.month)[1]))
    atual = date_range_totals(ledger, ini.isoformat(), today.isoformat(), "pf")
    antes = date_range_totals(ledger, ant.isoformat(), fim_ant.isoformat(), "pf")
    cheio = month_totals(ledger, ant.strftime("%Y-%m"), "pf")
    cats = sorted(set(atual["categorias"]) | set(antes["categorias"]), key=lambda c: -atual["categorias"].get(c, 0))
    pj = month_totals(ledger, ini.strftime("%Y-%m"), "pj")
    meses = [_mes(today, i).strftime("%Y-%m") for i in range(1, 13)]
    parcelas = committed_installments(ledger, meses)
    # detail for the hover cards: what is inside each category and each month of installments
    from fin.reports import PJ_OWN_CATS

    gastos = [r for r in ledger.active_txs("date >= ? AND date <= ? AND amount_cents < 0 AND natureza='Despesa'",
                                           (ini.isoformat(), today.isoformat()))
              if (r["categoria"] or "Revisar") not in PJ_OWN_CATS]
    por_cat: dict[str, list[Any]] = {}
    for r in gastos:
        por_cat.setdefault(r["categoria"] or "Revisar", []).append(r)
    itens_mes: dict[str, list[dict[str, Any]]] = {m: [] for m in meses}
    for r in ledger.active_txs("total_installments > 1 AND COALESCE(natureza,'Despesa')='Despesa'"):
        if r["competence"] in itens_mes:
            itens_mes[r["competence"]].append({"nome": rotulo_gasto(r), "parcela": f"{r['installment_number']}/{r['total_installments']}",
                                               "valor": _reais(-r["amount_cents"]), "categoria": r["categoria"] or "Revisar"})
    for pr in ledger.q("SELECT month, desc_norm, installment_number, total_installments, amount_cents, categoria FROM installments_projection"):
        if pr["month"] in itens_mes:
            fake = {"description": pr["desc_norm"], "desc_norm": pr["desc_norm"], "counterpart_kind": None}
            itens_mes[pr["month"]].append({"nome": rotulo_gasto(fake), "parcela": f"{pr['installment_number']}/{pr['total_installments']}",
                                           "valor": _reais(-pr["amount_cents"]), "categoria": pr["categoria"] or "Revisar"})
    apelidos = plano.get("fixos_nomes") or {}
    clt = compute_clt(ledger, ini, today)
    return {
        "mes": ini.strftime("%Y-%m"), "ate": today.isoformat(), "anteriorAte": fim_ant.isoformat(),
        "entradas": {  # what actually came in, by source; a PJ -> PF withdrawal is the same money moving, not income
            "clt": clt["recebido"],
            "pj": _reais(pj["receitas"]),
            "outros": _reais(max(0, atual["receitas"] - atual["fontes"].get("Retirada PJ", 0) - _cents(clt["recebido"]))),
            "custosPj": _reais(sum(pj["categorias"].values())),
        },
        "resumo": {"receitas": _reais(atual["receitas"]), "despesas": _reais(atual["despesas"]),
                   "despesasAnteriorMesmoPeriodo": _reais(antes["despesas"]),
                   "despesasAnteriorMesCheio": _reais(cheio["despesas"])},
        "categorias": [{"nome": c, "atual": _reais(atual["categorias"].get(c, 0)),
                        "anterior": _reais(antes["categorias"].get(c, 0)),
                        "n": len(por_cat.get(c, [])), "onde": _top(por_cat.get(c, [])),
                        "bruto": _reais(-sum(r["amount_cents"] for r in por_cat.get(c, [])))} for c in cats],
        "fixos": [{"nome": nome_fixo(n, apelidos), "valor": _reais(v), "dia": d, "pago": pago}
                  for n, v, d, pago in fixed_cost_status(ledger, cfg, ini.strftime("%Y-%m"))],
        "parcelas": [{"mes": m, "valor": _reais(parcelas[m]),
                      "itens": sorted(itens_mes[m], key=lambda i: -i["valor"])} for m in meses],
        "clt": clt,
        "pj": {"faturamento": _reais(pj["receitas"]), "retiradas": _reais(pj["retiradas"]),
               "custos": {c: _reais(v) for c, v in sorted(pj["categorias"].items(), key=lambda kv: -kv[1])}},
    }


def build_painel(ledger: Ledger, plano: dict[str, Any], today: date, now: datetime,
                 semanas_csv: Path | None = None, base: Path | None = None, cfg: "Config | None" = None) -> dict[str, Any]:
    contas = bank_accounts(ledger, plano.get("contas_nomes") or {})
    gasto = day_to_day_spent(ledger, plano.get("dia_a_dia") or {}, today)
    historico = load_historico(Path(plano.get("semanas_csv") or semanas_csv or SEMANAS_CSV).expanduser())
    notas = load_notas(Path(plano.get("notas_json") or NOTAS_JSON).expanduser())
    ultima_nota = notas[max(notas)] if notas else {}
    semana = plano.get("semana") or {}
    curto = ("rotulo", "commitsClt", "commitsPj", "entregasPj", "prsClt")
    return {
        "atualizadoEm": now.isoformat(timespec="seconds"),
        "dono": plano.get("dono", ""),
        "empresas": load_empresas(plano, base or Path.cwd()),
        "financas": {**compute_financas(plano, contas, gasto, today),
                     **({"detalhe": compute_detalhe(ledger, cfg, plano, today)} if cfg else {})},
        "desempenho": {
            "semanas": [{k: s[k] for k in curto} for s in historico[-N_SEMANAS:]],
            "historico": historico,
            "marcos": [{"data": _iso(m["data"]), "rotulo": m.get("rotulo", "")} for m in plano.get("marcos") or []],
            "notas": [{"inicio": k, **v} for k, v in sorted(notas.items())],
            "atual": {"nota": ultima_nota.get("geral", semana.get("nota")),
                      "destaque": ultima_nota.get("destaque", semana.get("destaque", "")),
                      "foraDoExpedientePct": fora_pct(historico[-1]) if historico else 0},
        },
    }


def export_painel(ledger: Ledger, data_dir: Path, today: date, now: datetime, cfg: "Config | None" = None) -> Path | None:
    """Writes data_dir/painel.json (0600, atomic). No plano.yaml: nothing to do."""
    plano_path = data_dir / PLANO_FILE
    if not plano_path.exists():
        return None
    out = data_dir / PAINEL_FILE
    tmp = out.with_suffix(".tmp")
    data = json.dumps(build_painel(ledger, load_plano(plano_path), today, now, base=data_dir, cfg=cfg), ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, out)
    return out
