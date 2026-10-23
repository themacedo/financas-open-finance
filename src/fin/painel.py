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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from fin.ledger import Ledger
from fin.normalize import GENERIC_WORDS

if TYPE_CHECKING:
    from fin.config import Config

PLANO_FILE = "plano.yaml"
PAINEL_FILE = "painel.json"
SEMANAS_CSV = Path.home() / "dados/historico_semanal.csv"
NOTAS_JSON = Path.home() / "dados/notas_semanais.json"
HORAS_JSON = Path.home() / "dados/horas_semanais.json"
NOTAS_MENSAIS_JSON = Path.home() / "dados/notas_mensais.json"
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
                          "ate": str(c["ate"])[:7], **({"fixo": str(c["fixo"])} if c.get("fixo") else {})}
                         for c in plano.get("compromissos") or []],
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


def load_horas(path: Path) -> list[dict[str, Any]]:
    """Hours per week (Saturday to Friday) from the Claude sessions, by company code (clt, pj, pessoal, outros):
    the week, its 09-18 weekday share and its 00-06 share. Generated by coletar_horas.py --serie; a floor (calls and
    work outside Claude are not in it)."""
    if not path.exists():
        return []
    dados = json.loads(path.read_text(encoding="utf-8"))
    return [{"inicio": k, **v} for k, v in sorted(dados.items())]


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


PARTICULAS = {"de", "da", "do", "das", "dos", "e", "ltda", "me", "eireli", "pix", "ted", "doc"}


def nomes_de_pessoas(ledger: Ledger) -> list[str]:
    """Every word of the names that appear in transfers to or from people (the ledger knows they are people).
    The generic name detector misses many first names (and spellings like "Anna"); these words block them."""
    nomes: set[str] = set()
    for r in ledger.q("SELECT DISTINCT description FROM transactions WHERE counterpart_kind = 'person'"):
        desc = r["description"] or ""
        nome = desc.split("|", 1)[1] if "|" in desc else re.sub(r"^(PIX|TRANSFER\S*|TED|DOC)\s+(ENVIAD\S*|RECEBID\S*)?\s*", "", desc, flags=re.I)
        nomes |= {w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", nome) if w.lower() not in PARTICULAS}
    # words that are part of the transfer text, not of the name ("Pagamento de Pix QR Code", "Valor adicionado")
    genericas = {_sem_acento(w).lower() for w in GENERIC_WORDS} | {"valor", "adicionado", "adicionada", "recebida", "enviada",
                                                                   "qrcode", "conta", "saldo", "limite", "convertido"}
    return sorted(n for n in nomes if _sem_acento(n) not in genericas)


def _sem_acento(w: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", w) if unicodedata.category(c) != "Mn")


def termos_privados(ledger: Ledger, plano: dict[str, Any]) -> list[str]:
    """Names that must never reach the dashboard: people from the ledger plus the owner's own name."""
    dono = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", str(plano.get("dono", "")))]
    return sorted(set(nomes_de_pessoas(ledger)) | set(dono))


def rotulo_gasto(row: Any, apelidos: dict[str, str] | None = None, termos: list[str] | None = None) -> str:
    """Where the money went, safe to show: the merchant, the nickname you gave the person (only when the nickname
    itself has no name in it), or "Pessoa física" when it reads like a person's name."""
    from functools import partial

    from fin.privacy import block_reason as _br

    block_reason = partial(_br, extra_terms=termos or ())
    apelido = (apelidos or {}).get(row.get("counterpart_alias") if isinstance(row, dict) else row["counterpart_alias"])
    if apelido and not block_reason(apelido):
        return apelido

    desc = row["description"] or ""
    base = desc.split("|", 1)[1] if "|" in desc else (row["desc_norm"] or desc)
    base = PARCELA.sub("", base).strip()
    if PIX_PREFIXO.match(base):
        base = ""
    if row["counterpart_kind"] != "person" and base and block_reason(base):
        # "WELLHUB FULANA DE TAL": keep only the first word (the brand) when it alone is safe. Only the first word:
        # the name detector misses spellings like "Anna", so anything after the brand may still be a name
        prefixo = base.split()[0]
        base = prefixo if len(prefixo) >= 4 and not block_reason(prefixo) else base
    if row["counterpart_kind"] == "person" or not base or block_reason(base):
        return "Pessoa física (nome oculto)" if base or row["counterpart_kind"] == "person" else "Pix ou transferência"
    return " ".join(w.capitalize() if len(w) > 2 else w.lower() for w in base.split())


def _top(rows: list[Any], n: int = 3, apelidos: dict[str, str] | None = None,
         termos: list[str] | None = None) -> list[dict[str, Any]]:
    por: dict[str, list[int]] = {}
    for r in rows:
        v = por.setdefault(rotulo_gasto(r, apelidos, termos), [0, 0])
        v[0] += -r["amount_cents"]
        v[1] += 1
    top = sorted(por.items(), key=lambda kv: -kv[1][0])[:n]
    return [{"nome": k, "valor": _reais(v[0]), "n": v[1]} for k, v in top]


def compute_clt(ledger: Ledger, ini: date, today: date, fim: date | None = None) -> dict[str, Any]:
    """Salary entries of the period (category Salário CLT, end exclusive; default: the month of `ini`):
    what arrived and what is scheduled (pending or future)."""
    fim = fim or _mes(ini, 1)
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


RE_FATURA_PLANO = re.compile(r"fatura|cart[aã]o", re.I)


def _chave_recorrente(r: Any) -> str:
    return " ".join((r["desc_norm"] or "").split()[:2])


def cobrancas_recorrentes(ledger: Ledger, account_id: str, comp: str, apelidos: dict[str, str] | None = None,
                          termos: list[str] | None = None, hoje: date | None = None) -> list[dict[str, Any]]:
    """Monthly charges on the card (subscriptions, a coffee club): one-off purchases in at least 2 of the last 3
    bills and seen in the last 40 days, either filed as Assinaturas or once per bill with a steady amount (a delivery
    order of about the same value now and then is not one). Expected value: the latest one."""
    from fin.rules import add_months

    ultimas = [add_months(comp, -k) for k in (1, 2, 3)]
    por: dict[str, dict[str, list[Any]]] = {}
    recente: dict[str, str] = {}  # latest date, the open bill included (a charge there proves it is still running)
    for r in ledger.active_txs(f"account_id=? AND competence IN ({','.join('?' * (len(ultimas) + 1))}) AND amount_cents < 0 "
                               "AND COALESCE(natureza,'Despesa')='Despesa' AND COALESCE(total_installments,0) <= 1",
                               (account_id, *ultimas, comp)):
        k = _chave_recorrente(r)
        recente[k] = max(recente.get(k, ""), r["date"])
        if r["competence"] != comp:
            por.setdefault(k, {}).setdefault(r["competence"], []).append(r)
    out = []
    for chave, meses in por.items():
        if not chave or len(meses) < 2:
            continue
        todas = [r for rs in meses.values() for r in rs]
        valores = [-r["amount_cents"] for r in todas]
        assinatura = any(r["categoria"] == "Assinaturas" for r in todas)
        estavel = all(len(rs) == 1 for rs in meses.values()) and max(valores) <= min(valores) * 1.15
        if not (assinatura or estavel) or any(r["categoria"] in ("Impostos/Taxas", "Outros") and "JUROS" in chave for r in todas):
            continue
        ultima = max(todas, key=lambda r: r["date"])
        if hoje and recente[chave] < (hoje - timedelta(days=40)).isoformat():  # stopped: nothing in the last 40 days
            continue
        out.append({"chave": chave, "nome": rotulo_gasto(ultima, apelidos, termos), "valor": _reais(-ultima["amount_cents"]),
                    "dia": int(ultima["date"][8:10])})
    return sorted(out, key=lambda x: -x["valor"])


def compute_ciclo_cartao(ledger: Ledger, cfg: "Config", plano: dict[str, Any], today: date) -> dict[str, Any] | None:
    """The open card bill against what the plan sets aside to pay it: what is already in it (old installments,
    interest), what was bought this cycle, the pace projected to the closing day and what still fits per day."""
    from fin.rules import CARD_PAYMENT_DUP, card_competence

    acc = next((a for a in ledger.accounts() if a["type"] == "CREDIT"), None)
    if acc is None:
        return None
    card = cfg.card(acc["bank"])
    fecha, vence = card.closing_day, card.due_day
    if not fecha or not vence:
        return None
    comp = card_competence(today, fecha, vence)
    inicio = (date(today.year, today.month, 1) if today.day >= fecha else _mes(today, -1)).replace(day=fecha) + timedelta(days=1)
    fim = _mes(inicio, 1).replace(day=fecha)
    vencimento = date(int(comp[:4]), int(comp[5:]), min(vence, 28))
    rows = [r for r in ledger.active_txs("account_id=? AND competence=?", (acc["id"], comp))
            if r["rule"] not in ("card_bill_payment", CARD_PAYMENT_DUP) and not (r["rule"] == "card_carryover" and r["amount_cents"] > 0)]
    ja = lambda r: r["date"] < inicio.isoformat() or r["date"] > today.isoformat() or (r["installment_number"] or 1) > 1
    comprometido = -sum(r["amount_cents"] for r in rows if ja(r))
    novo = -sum(r["amount_cents"] for r in rows if not ja(r))
    recorrentes = cobrancas_recorrentes(ledger, acc["id"], comp, {str(k): str(v) for k, v in (plano.get("apelidos_painel") or {}).items()},
                                        termos_privados(ledger, plano), today)
    abertas = {_chave_recorrente(r) for r in rows}
    previstas = [x for x in recorrentes if x["chave"] not in abertas]
    reservado = sum(_cents(x["valor"]) for x in previstas)
    # the plan item that pays this bill: a card/bill outflow due in the bill's month
    teto = next((_cents(i.get("sai")) for i in plano.get("plano") or []
                 if i.get("sai") and RE_FATURA_PLANO.search(str(i.get("descricao", ""))) and _iso(i.get("data"))[:7] == comp), None)
    dias = (fim - inicio).days + 1
    passados = max(1, (min(today, fim) - inicio).days + 1)
    restantes = max(0, (fim - today).days)
    # a subscription that already fell this cycle is not a spending pace: only the rest is projected
    chaves = {x["chave"] for x in recorrentes}
    fixo_no_ciclo = -sum(r["amount_cents"] for r in rows if not ja(r) and _chave_recorrente(r) in chaves)
    projecao = comprometido + fixo_no_ciclo + round((novo - fixo_no_ciclo) * dias / passados)
    atual = comprometido + novo
    projecao += reservado
    status = None if teto is None else ("estourou" if atual > teto else "atencao" if projecao > teto or atual + reservado > teto else "ok")
    return {"inicio": inicio.isoformat(), "fechamento": fim.isoformat(), "vencimento": vencimento.isoformat(),
            "teto": _reais(teto) if teto is not None else None, "atual": _reais(atual), "comprometido": _reais(comprometido),
            "novo": _reais(novo), "projecao": _reais(projecao), "dias": dias, "diasPassados": passados,
            "diasRestantes": restantes,
            "porDia": _reais(max(0, (teto - atual - reservado)) // max(1, restantes)) if teto is not None else None,
            "status": status, "reservado": _reais(reservado),
            "previstas": [{k: v for k, v in x.items() if k != "chave"} for x in previstas]}


def compute_detalhe(ledger: Ledger, cfg: "Config", plano: dict[str, Any], today: date) -> dict[str, Any]:
    """Month by category against the same days of last month, fixed costs, future installments and the PJ."""
    from fin.reports import committed_installments, date_range_totals, fixed_cost_status, month_totals

    ini, ant = _mes(today, 0), _mes(today, -1)
    fim_ant = date(ant.year, ant.month, min(today.day, calendar.monthrange(ant.year, ant.month)[1]))
    atual = date_range_totals(ledger, ini.isoformat(), today.isoformat(), "pf")
    antes = date_range_totals(ledger, ant.isoformat(), fim_ant.isoformat(), "pf")
    cheio = month_totals(ledger, ant.strftime("%Y-%m"), "pf")
    cats = sorted(set(atual["categorias"]) | set(antes["categorias"]), key=lambda c: (-atual["categorias"].get(c, 0), c))
    pj = month_totals(ledger, ini.strftime("%Y-%m"), "pj")
    meses = [_mes(today, i).strftime("%Y-%m") for i in range(1, 13)]
    parcelas = committed_installments(ledger, meses)
    # detail for the hover cards: what is inside each category and each month of installments
    from fin.reports import PJ_OWN_CATS

    gastos = [r for r in ledger.active_txs("date >= ? AND date <= ? AND amount_cents < 0 AND natureza='Despesa'",
                                           (ini.isoformat(), today.isoformat()))
              if (r["categoria"] or "Revisar") not in PJ_OWN_CATS]
    # only the nicknames chosen for the dashboard (plano.yaml); the ledger has others with people's names in them
    pessoas = {str(k): str(v) for k, v in (plano.get("apelidos_painel") or {}).items()}
    termos = termos_privados(ledger, plano)
    por_cat: dict[str, list[Any]] = {}
    for r in gastos:
        por_cat.setdefault(r["categoria"] or "Revisar", []).append(r)
    itens_mes: dict[str, list[dict[str, Any]]] = {m: [] for m in meses}
    for r in ledger.active_txs("total_installments > 1 AND COALESCE(natureza,'Despesa')='Despesa'"):
        if r["competence"] in itens_mes:
            itens_mes[r["competence"]].append({"nome": rotulo_gasto(r, pessoas, termos), "parcela": f"{r['installment_number']}/{r['total_installments']}",
                                               "valor": _reais(-r["amount_cents"]), "categoria": r["categoria"] or "Revisar"})
    for pr in ledger.q("SELECT month, desc_norm, installment_number, total_installments, amount_cents, categoria FROM installments_projection"):
        if pr["month"] in itens_mes:
            fake = {"description": pr["desc_norm"], "desc_norm": pr["desc_norm"], "counterpart_kind": None}
            itens_mes[pr["month"]].append({"nome": rotulo_gasto(fake, pessoas, termos), "parcela": f"{pr['installment_number']}/{pr['total_installments']}",
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
                        "n": len(por_cat.get(c, [])), "onde": _top(por_cat.get(c, []), apelidos=pessoas, termos=termos),
                        "bruto": _reais(-sum(r["amount_cents"] for r in por_cat.get(c, [])))} for c in cats],
        "fixos": [{"nome": nome_fixo(n, apelidos), "valor": _reais(v), "dia": d, "pago": pago}
                  for n, v, d, pago in fixed_cost_status(ledger, cfg, ini.strftime("%Y-%m"))],
        "parcelas": [{"mes": m, "valor": _reais(parcelas[m]),
                      "itens": sorted(itens_mes[m], key=lambda i: -i["valor"])} for m in meses],
        "clt": clt,
        "pj": {"faturamento": _reais(pj["receitas"]), "retiradas": _reais(pj["retiradas"]),
               "custos": {c: _reais(v) for c, v in sorted(pj["categorias"].items(), key=lambda kv: -kv[1])}},
        "periodos": compute_periodos(ledger, cfg, plano, today),
        "posicaoSerie": posicao_serie(ledger, today),
        "cicloCartao": compute_ciclo_cartao(ledger, cfg, plano, today),
        "faturaAberta": fatura_aberta(ledger, today),
        "inicioDados": _inicio_dados(ledger),
    }


def fatura_aberta(ledger: Ledger, today: date) -> dict[str, Any] | None:
    """The last closed card bill and what is still open on it today: its total minus the bill payments made after
    the closing (a payment listed twice counts once). The month's closing on the Finanças screen uses it: the next
    bills (later installments, purchases after the closing) are not this month's."""
    from fin.money import to_cents
    from fin.rules import CARD_PAYMENT_DUP

    acc = next((a for a in ledger.accounts() if a["type"] == "CREDIT"), None)
    if acc is None:
        return None
    b = ledger.one("SELECT closing_date, due_date, total_cents, finance_charges_json FROM bills WHERE account_id=? AND closing_date<=? "
                   "AND total_cents IS NOT NULL ORDER BY closing_date DESC LIMIT 1", (acc["id"], today.isoformat()))
    if not b:
        return None
    # what the bill carries from before (the bank lists it as charges): the rotativo balance of the previous bill,
    # and its interest, IOF and debt-closing costs; the rest of the total is the cycle's purchases
    cobrancas = json.loads(b["finance_charges_json"] or "[]")
    rotativo = sum(to_cents(c.get("amount") or 0) for c in cobrancas if str(c.get("additionalInfo") or "").startswith("Saldo em rotativo"))
    encargos = sum(to_cents(c.get("amount") or 0) for c in cobrancas) - rotativo
    pago = ledger.one("SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE account_id=? AND removed_upstream=0 "
                      "AND operation_type='PAGAMENTO_FATURA' AND amount_cents>0 AND COALESCE(rule,'')!=? AND date>? AND date<=?",
                      (acc["id"], CARD_PAYMENT_DUP, b["closing_date"], today.isoformat()))["s"]
    return {"fechamento": b["closing_date"], "vencimento": b["due_date"], "total": _reais(b["total_cents"]),
            "rotativo": _reais(rotativo), "encargos": _reais(encargos), "compras": _reais(b["total_cents"] - rotativo - encargos),
            "pago": _reais(pago), "aberto": _reais(max(0, b["total_cents"] - pago))}


MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def compute_cobertura(ledger: Ledger, ini: date, ate: date) -> dict[str, float]:
    """How a month that spent more than it earned was paid for: card balance carried to the next bill (rotativo),
    Pix on credit to yourself (cash taken from the card limit), savings withdrawn, and what the rotativo cost."""
    rows = ledger.active_txs("date >= ? AND date <= ?", (ini.isoformat(), ate.isoformat()))
    rot = sum(-r["amount_cents"] for r in rows if r["rule"] == "card_carryover" and r["amount_cents"] < 0)
    pix = sum(-r["amount_cents"] for r in rows
              if r["rule"] == "card_credit_op" and r["natureza"] == "Transferência" and r["amount_cents"] < 0)
    # net: the bank's automatic investment sweeps money in and out every day; only what left savings counts
    resg = max(0, sum(r["amount_cents"] for r in rows if r["natureza"] == "Investimento"))
    juros = sum(-r["amount_cents"] for r in rows if "ROTATIVO" in (r["desc_norm"] or "").upper() + (r["description"] or "").upper()
                and r["natureza"] == "Despesa")
    return {"rotativo": _reais(rot), "pixCredito": _reais(pix), "resgates": _reais(resg), "jurosRotativo": _reais(juros)}


def _inicio_dados(ledger: Ledger) -> str | None:
    return ledger.one("SELECT MIN(date) d FROM transactions")["d"]


def divida_emprestimo(ledger: Ledger, loan: Any, dia: date, today: date) -> int:
    """Outstanding balance of one loan at the end of `dia`, in cents. Today: what the bank reports. Before: the
    PRICE schedule from the contract (monthly rate from the CET), counting the installments actually paid by
    then (the loan payments seen in the accounts; they are rarely paid on the due day)."""
    contrato = loan["contract_date"]
    if not contrato or dia.isoformat() < contrato or (loan["settlement_date"] and dia.isoformat() >= loan["settlement_date"]):
        return 0
    if dia >= today:
        return loan["outstanding_cents"] or 0
    n, pv = loan["installments_total"] or 0, loan["contract_cents"] or 0
    if not n:
        return pv
    fim = loan["settlement_date"] or today.isoformat()
    datas = [r["date"] for r in ledger.active_txs("rule='debt_payment' AND date >= ? AND date <= ?", (contrato, fim))]
    inicio = _inicio_dados(ledger) or contrato
    if contrato >= inicio:  # the whole loan is in the data
        pagas = sum(1 for d in datas if d <= dia.isoformat())
    else:  # installments before the data started: from the count the bank reports
        pagas = (loan["installments_paid"] or 0) - sum(1 for d in datas if d > dia.isoformat())
    resta = n - max(0, pagas)
    if resta <= 0:
        return 0
    i = (1 + (loan["cet"] or 0)) ** (1 / 12) - 1
    if i <= 0:
        return round(pv * resta / n)
    parcela = pv * i / (1 - (1 + i) ** -n)
    return round(parcela * (1 - (1 + i) ** -resta) / i)


def _antes_dos_dados(ledger: Ledger, dia: date) -> date:
    # before the first transaction there is nothing to compare with: use the day the data starts
    primeiro = _inicio_dados(ledger)
    return date.fromisoformat(primeiro) - timedelta(days=1) if primeiro and dia.isoformat() < primeiro else dia


def dividas_emprestimo(ledger: Ledger, dia: date, today: date) -> int:
    dia = _antes_dos_dados(ledger, dia)
    return sum(divida_emprestimo(ledger, ln, dia, today) for ln in ledger.q("SELECT * FROM loans"))


def investimentos_em(ledger: Ledger, dia: date, today: date) -> int:
    """Invested balance at the end of `dia`, for the banks that report it: today's balance minus what was applied
    after `dia` plus what was redeemed (the Investimento rows of that bank's accounts)."""
    total = 0
    for m in ledger.q("SELECT mirror_item_id m, SUM(balance_cents) s FROM investments WHERE mirror_item_id IS NOT NULL "
                      "GROUP BY mirror_item_id"):
        depois = 0
        if dia < today:
            depois = ledger.one("SELECT COALESCE(SUM(t.amount_cents),0) s FROM transactions t JOIN accounts a ON a.id=t.account_id "
                                "WHERE a.item_id=? AND t.natureza='Investimento' AND t.date>? AND t.date<=? AND t.removed_upstream=0",
                                (m["m"], dia.isoformat(), today.isoformat()))["s"]
        total += max(0, (m["s"] or 0) + depois)
    return total


EFETIVA_CARTAO = ("CASE WHEN COALESCE(total_installments,0)>1 AND purchase_date IS NOT NULL THEN purchase_date "
                  "ELSE date END")


def _cartao_depois(ledger: Ledger, account_id: str, dia: date, today: date) -> int:
    """Card rows that hit the limit after `dia`: an installment purchase weighs whole on the purchase day (its
    later installments, even future-dated ones, were already in the used limit), everything else on its date."""
    from fin.rules import CARD_PAYMENT_DUP

    return ledger.one(f"SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE account_id=? AND removed_upstream=0 "
                      f"AND COALESCE(rule,'')!=? AND {EFETIVA_CARTAO}>? AND {EFETIVA_CARTAO}<=?",
                      (account_id, CARD_PAYMENT_DUP, dia.isoformat(), today.isoformat()))["s"]


def posicao_em(ledger: Ledger, dia: date, today: date) -> dict[str, float]:
    """Money in the bank accounts and investments minus everything owed on the cards and loans at the end of `dia`, rebuilt backwards from the
    balances Pluggy reports today. Scheduled rows (a pending salary, future installments the card limit already
    holds) stay out. A card payment the aggregator listed twice does not lower the debt twice."""
    from fin.rules import CARD_PAYMENT_DUP

    dia = _antes_dos_dados(ledger, dia)
    contas = cartao = 0
    for a in ledger.accounts():
        if a["type"] not in ("BANK", "CREDIT") or a["balance_cents"] is None:
            continue
        if a["type"] == "BANK":
            depois = ledger.one("SELECT COALESCE(SUM(amount_cents),0) s FROM transactions WHERE account_id=? AND date>? "
                                "AND date<=? AND removed_upstream=0", (a["id"], dia.isoformat(), today.isoformat()))["s"]
            contas += a["balance_cents"] - (depois if dia < today else 0)
        else:  # balance = used limit, every future installment included; purchases are negative rows, payments positive
            cartao += a["balance_cents"] + (_cartao_depois(ledger, a["id"], dia, today) if dia < today else 0)
    emprestimos = dividas_emprestimo(ledger, dia, today)
    invest = investimentos_em(ledger, dia, today)
    return {"contas": _reais(contas), "investimentos": _reais(invest), "cartao": _reais(cartao),
            "emprestimos": _reais(emprestimos), "posicao": _reais(contas + invest - cartao - emprestimos)}


def posicao_serie(ledger: Ledger, today: date, n_semanas: int = 12, n_meses: int = 12) -> dict[str, list[dict[str, Any]]]:
    """Net position at the end of each week (Sunday), month and year with data, oldest first; the last point is today."""
    primeiro = ledger.one("SELECT MIN(date) d FROM transactions")["d"]
    inicio = date.fromisoformat(primeiro) if primeiro else today
    hoje = {"data": today.isoformat(), "rotulo": "hoje", **posicao_em(ledger, today, today)}
    dom = today - timedelta(days=today.weekday() + 1)  # last Sunday before this week
    semanas = [dom - timedelta(weeks=k) for k in range(n_semanas - 1, -1, -1)]
    meses = [_mes(today, -k + 1) - timedelta(days=1) for k in range(n_meses, 0, -1)]
    anos = [date(a, 12, 31) for a in range(inicio.year, today.year)]
    ponto = lambda d, rot: {"data": d.isoformat(), "rotulo": rot, **posicao_em(ledger, d, today)}
    return {"semana": [ponto(d, f"{d:%d/%m}") for d in semanas if d >= inicio] + [hoje],
            "mes": [ponto(d, MESES_ABREV[d.month - 1]) for d in meses if d >= inicio] + [hoje],
            "ano": [ponto(d, str(d.year)) for d in anos if d >= inicio] + [hoje]}


DIVIDA_RULES = ("debt_payment", "loan_received")
PIX_CREDITO_RULES = ("card_credit_op", "card_limit_to_account")


def compute_ajustes(ledger: Ledger, ini: date, ate: date, resultado_cents: int, variacao_cents: int,
                    divida_delta_cents: int = 0, invest_delta_cents: int = 0) -> dict[str, float]:
    """What moves the accounts and the card debt without being income or spending, so that
    resultado + ajustes = variação da posição. `naoExplicado` is the leftover: zero when every row is accounted for."""
    from fin.rules import CARD_PAYMENT_DUP

    cartoes = {a["id"] for a in ledger.accounts() if a["type"] == "CREDIT"}
    parcela = lambda r: r["account_id"] in cartoes and (r["total_installments"] or 0) > 1 and r["purchase_date"]
    todas = [r for r in ledger.active_txs("date >= ? AND date <= ?", (ini.isoformat(), ate.isoformat()))
             if r["rule"] != CARD_PAYMENT_DUP]
    rows = [r for r in todas if r["natureza"] in ("Transferência", "Investimento") and not parcela(r)]
    # installment purchases made in the period (every installment, future ones included) against the installments
    # that fell in it and were counted as spending
    desde = max(ini, _antes_dos_dados(ledger, ini) + timedelta(days=1)) if ini.isoformat() < (_inicio_dados(ledger) or "") else ini
    assumido = sum(r["amount_cents"] for r in ledger.active_txs("purchase_date >= ? AND purchase_date <= ? AND COALESCE(total_installments,0) > 1",
                                                                 (desde.isoformat(), ate.isoformat())) if parcela(r))
    lancado = sum(r["amount_cents"] for r in todas if parcela(r) and (r["natureza"] or "Despesa") in ("Despesa", "Receita"))
    parcelado = assumido - lancado
    soma = lambda f: sum(r["amount_cents"] for r in rows if f(r))
    contas_fora = soma(lambda r: r["rule"] in ("own_transfer", "own_transfer_extra") and not r["pair_id"])
    pix = soma(lambda r: r["rule"] in PIX_CREDITO_RULES)
    dividas = soma(lambda r: r["rule"] in DIVIDA_RULES)
    aplic = soma(lambda r: r["natureza"] == "Investimento")
    # own transfers between connected accounts, bill payments and the rotativo pair cancel out; what is left is
    # a transfer to someone outside (a person marked as transfer, a service fee)
    outras = soma(lambda r: r["natureza"] == "Transferência") - contas_fora - pix - dividas
    # the loan money in and the installments out, against what the debt moved: what is left is interest and fees
    # (and the cost of a renegotiation, that raises the debt with no money moving)
    custo_emp = dividas - divida_delta_cents
    # money sent to an investment the bank reports is still yours: only the yield (and the sweeps of banks that
    # do not report the balance) stays here
    aplic = aplic + invest_delta_cents
    nao = variacao_cents - resultado_cents - contas_fora - pix - custo_emp - aplic - outras - parcelado
    return {"parcelado": _reais(parcelado), "contasFora": _reais(contas_fora), "custoPixCredito": _reais(pix), "emprestimos": _reais(custo_emp),
            "aplicacoes": _reais(aplic), "outrasTransferencias": _reais(outras), "naoExplicado": _reais(nao)}


def compute_periodo(ledger: Ledger, pessoas: dict[str, str], ini: date, fim: date, ant_ini: date, ant_fim: date,
                    today: date, termos: list[str] | None = None) -> dict[str, Any]:
    """Spending by category, income by source, CLT and PJ for [ini, fim) against [ant_ini, ant_fim).
    A period still running is cut at today, and so is the comparison (same number of days)."""
    from fin.reports import PJ_OWN_CATS, date_range_totals

    ate = min(fim - timedelta(days=1), today)
    ant_ate = min(ant_fim - timedelta(days=1), ant_ini + (ate - ini))
    atual = date_range_totals(ledger, ini.isoformat(), ate.isoformat(), "pf")
    antes = date_range_totals(ledger, ant_ini.isoformat(), ant_ate.isoformat(), "pf")
    pj = date_range_totals(ledger, ini.isoformat(), ate.isoformat(), "pj")
    por_cat: dict[str, list[Any]] = {}
    for r in ledger.active_txs("date >= ? AND date <= ? AND amount_cents < 0 AND natureza='Despesa'", (ini.isoformat(), ate.isoformat())):
        if (r["categoria"] or "Revisar") not in PJ_OWN_CATS:
            por_cat.setdefault(r["categoria"] or "Revisar", []).append(r)
    cats = sorted(set(atual["categorias"]) | set(antes["categorias"]), key=lambda c: (-atual["categorias"].get(c, 0), c))
    clt = compute_clt(ledger, ini, today, fim)
    # other income only: without the withdrawal (same money), the salary and the PJ billing that a
    # client paid straight into a PF account (already counted as PJ revenue)
    outros = max(0, atual["receitas"] - atual["fontes"].get("Retirada PJ", 0) - _cents(clt["recebido"])
                 - sum(v for f, v in atual["fontes"].items() if f in pj["fontes"]))
    custos_pj = sum(pj["categorias"].values())
    entrou_cents = _cents(clt["recebido"]) + pj["receitas"] + outros
    posicao = {"inicio": posicao_em(ledger, ini - timedelta(days=1), today), "fim": posicao_em(ledger, ate, today)}
    return {
        "inicio": ini.isoformat(), "fim": (fim - timedelta(days=1)).isoformat(), "ate": ate.isoformat(),
        "anteriorInicio": ant_ini.isoformat(), "anteriorAte": ant_ate.isoformat(), "emCurso": ate < fim - timedelta(days=1),
        # the data starts mid-way (Open Finance history): a comparison with a period only partly covered is not one
        "anteriorCompleto": bool(_inicio_dados(ledger)) and ant_ini.isoformat() >= (_inicio_dados(ledger) or ""),
        "entradas": {"clt": clt["recebido"], "pj": _reais(pj["receitas"]),
                     "outros": _reais(outros), "custosPj": _reais(custos_pj)},
        "cobertura": compute_cobertura(ledger, ini, ate),
        "posicao": posicao,
        "ajustes": compute_ajustes(ledger, ini, ate, entrou_cents - atual["despesas"] - custos_pj,
                                   _cents(posicao["fim"]["posicao"]) - _cents(posicao["inicio"]["posicao"]),
                                   _cents(posicao["fim"]["emprestimos"]) - _cents(posicao["inicio"]["emprestimos"]),
                                   _cents(posicao["fim"]["investimentos"]) - _cents(posicao["inicio"]["investimentos"])),
        "resumo": {"receitas": _reais(atual["receitas"]), "despesas": _reais(atual["despesas"]),
                   "despesasAnterior": _reais(antes["despesas"])},
        "categorias": [{"nome": c, "atual": _reais(atual["categorias"].get(c, 0)), "anterior": _reais(antes["categorias"].get(c, 0)),
                        "n": len(por_cat.get(c, [])), "onde": _top(por_cat.get(c, []), apelidos=pessoas, termos=termos),
                        "bruto": _reais(-sum(r["amount_cents"] for r in por_cat.get(c, [])))} for c in cats],
        "clt": clt,
        "pj": {"faturamento": _reais(pj["receitas"]), "retiradas": _reais(pj["retiradas"]),
               "custos": {c: _reais(v) for c, v in sorted(pj["categorias"].items(), key=lambda kv: -kv[1])}},
    }


def compute_periodos(ledger: Ledger, cfg: "Config", plano: dict[str, Any], today: date,
                     n_semanas: int = 12, n_meses: int = 12) -> dict[str, list[dict[str, Any]]]:
    """Every period the selector offers, newest first: weeks (Monday to Sunday), months and years with data."""
    from fin.reports import fixed_cost_status

    pessoas = {str(k): str(v) for k, v in (plano.get("apelidos_painel") or {}).items()}
    termos = termos_privados(ledger, plano)
    nomes_fixos = plano.get("fixos_nomes") or {}
    primeiro = ledger.one("SELECT MIN(date) d FROM transactions")["d"]
    inicio_dados = date.fromisoformat(primeiro) if primeiro else today
    semanas, meses, anos = [], [], []
    seg = today - timedelta(days=today.weekday())
    for k in range(n_semanas):
        ini = seg - timedelta(weeks=k)
        if ini + timedelta(days=6) < inicio_dados:
            break
        p = compute_periodo(ledger, pessoas, ini, ini + timedelta(days=7), ini - timedelta(days=7), ini, today, termos)
        fimv = ini + timedelta(days=6)
        semanas.append({"chave": f"s-{ini.isoformat()}", "rotulo": f"{ini:%d/%m} a {fimv:%d/%m}", **p})
    for k in range(n_meses):
        ini = _mes(today, -k)
        if _mes(ini, 1) <= inicio_dados:
            break
        p = compute_periodo(ledger, pessoas, ini, _mes(ini, 1), _mes(ini, -1), ini, today, termos)
        fixos = [{"nome": nome_fixo(n, nomes_fixos), "valor": _reais(v), "dia": d, "pago": pago}
                 for n, v, d, pago in fixed_cost_status(ledger, cfg, ini.strftime("%Y-%m"))]
        meses.append({"chave": f"m-{ini:%Y-%m}", "rotulo": f"{MESES_ABREV[ini.month - 1]}/{ini:%y}", "fixos": fixos, **p})
    for ano in range(today.year, inicio_dados.year - 1, -1):
        ini = date(ano, 1, 1)
        p = compute_periodo(ledger, pessoas, ini, date(ano + 1, 1, 1), date(ano - 1, 1, 1), ini, today, termos)
        anos.append({"chave": f"a-{ano}", "rotulo": str(ano), **p})
    return {"semana": semanas, "mes": meses, "ano": anos}


SOMBRA_JSON = "sombra_ultima.json"


def compute_saude(ledger: Ledger, base: Path | None) -> dict[str, Any]:
    """Data health for the dashboard: the last sync, how fresh each bank is in Pluggy (saved by the sync), the rows
    waiting for review and the last cloud comparison (sombra_ultima.json next to the ledger, fetched before the sync)."""
    from fin.sync import BANK_NAMES

    run = ledger.one("SELECT finished_at, status, stats_json FROM sync_runs ORDER BY id DESC LIMIT 1")
    ok = ledger.one("SELECT finished_at FROM sync_runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1")
    stats = json.loads(run["stats_json"] or "{}") if run else {}
    conexoes = json.loads(ledger.meta("pluggy_conexoes") or "{}")
    sombra = None
    if base and (base / SOMBRA_JSON).exists():
        s = json.loads((base / SOMBRA_JSON).read_text(encoding="utf-8"))
        sombra = {"hoje": s.get("hoje"), "sync": s.get("sync"), "campos": s.get("campos"), "diferencas": s.get("diferencas")}
    return {
        "sync": {"em": run["finished_at"], "status": run["status"], "novos": stats.get("inserted", 0),
                 "ultimoSucesso": ok["finished_at"] if ok else None} if run else None,
        "conexoes": [{"banco": BANK_NAMES.get(b, b), "status": c.get("status"), "ultima": c.get("ultima"), "proxima": c.get("proxima"),
                      "consentimento": c.get("consentimento")}
                     for b, c in sorted(conexoes.items())],
        "pendentes": ledger.one("SELECT COUNT(*) n FROM transactions WHERE review_status='pendente' AND removed_upstream=0")["n"],
        "sombra": sombra,
    }


def ligar_compromissos(f: dict[str, Any], today: date) -> dict[str, Any]:
    """A commitment linked to a fixed cost (`fixo`) takes this month's paid status from it, instead of guessing.
    A plan item of this month marked done with the commitment's name also counts as paid (e.g. paid in cash: no
    bank transaction to find); the linked fixed cost follows."""
    def feitos_em(mes: str) -> set[str]:
        return {i["descricao"] for i in f.get("plano", []) if i.get("status") == "feito" and str(i.get("data", ""))[:7] == mes}

    def marcar(fixos: list[dict[str, Any]], feitos: set[str]) -> None:
        for c in f.get("compromissos", []):
            if c.get("descricao") in feitos:
                for x in fixos:
                    if x["nome"] == c.get("fixo"):
                        x["pago"] = True

    feitos = feitos_em(today.isoformat()[:7])
    detalhe = f.get("detalhe") or {}
    fixos = detalhe.get("fixos", [])
    marcar(fixos, feitos)
    for m in (detalhe.get("periodos") or {}).get("mes", []):  # a lista de fixos de cada mês na tela Finanças
        marcar(m.get("fixos", []), feitos_em(str(m.get("chave", ""))[2:]))
    pagos = {x["nome"]: x["pago"] for x in fixos}
    for c in f.get("compromissos", []):
        if c.get("fixo") in pagos:
            c["pagoNoMes"] = pagos[c["fixo"]]
        elif c.get("descricao") in feitos:
            c["pagoNoMes"] = True
    return f


def build_painel(ledger: Ledger, plano: dict[str, Any], today: date, now: datetime,
                 semanas_csv: Path | None = None, base: Path | None = None, cfg: "Config | None" = None) -> dict[str, Any]:
    contas = bank_accounts(ledger, plano.get("contas_nomes") or {})
    gasto = day_to_day_spent(ledger, plano.get("dia_a_dia") or {}, today)
    # a copy next to the ledger wins (the runner gets them from the Mac); else the Mac's own files
    local = lambda nome, padrao: base / nome if base and (base / nome).exists() else Path(padrao).expanduser()
    historico = load_historico(local(SEMANAS_CSV.name, plano.get("semanas_csv") or semanas_csv or SEMANAS_CSV))
    notas = load_notas(local(NOTAS_JSON.name, plano.get("notas_json") or NOTAS_JSON))
    horas = load_horas(local(HORAS_JSON.name, plano.get("horas_json") or HORAS_JSON))
    mensais = load_notas(local(NOTAS_MENSAIS_JSON.name, plano.get("notas_mensais_json") or NOTAS_MENSAIS_JSON))
    ultima_nota = notas[max(notas)] if notas else {}
    semana = plano.get("semana") or {}
    curto = ("rotulo", "commitsClt", "commitsPj", "entregasPj", "prsClt")
    return {
        "atualizadoEm": now.isoformat(timespec="seconds"),
        "dono": plano.get("dono", ""),
        "empresas": load_empresas(plano, base or Path.cwd()),
        "saude": compute_saude(ledger, base),
        "financas": ligar_compromissos({**compute_financas(plano, contas, gasto, today),
                                        **({"detalhe": compute_detalhe(ledger, cfg, plano, today)} if cfg else {})}, today),
        "desempenho": {
            "semanas": [{k: s[k] for k in curto} for s in historico[-N_SEMANAS:]],
            "historico": historico,
            "marcos": [{"data": _iso(m["data"]), "rotulo": m.get("rotulo", "")} for m in plano.get("marcos") or []],
            "notas": [{"inicio": k, **v} for k, v in sorted(notas.items())],
            "horas": horas,
            # monthly coach review (cycle from the 16th to the 15th): same shape as the weekly one, plus `fim` and `acoes`
            "notasMensais": [{"inicio": k, **v} for k, v in sorted(mensais.items())],
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
