"""painel.json: plan math checked against the hand calculation of 17/10/2026 (synthetic labels)."""

import json
from datetime import date, datetime, UTC

import pytest
import yaml

from fin.ledger import Ledger
from fin.painel import compute_financas, export_painel, _mes, build_painel, compute_detalhe, fora_pct, nome_fixo, rotulo_gasto, load_empresas, load_historico, status_of

TODAY = date(2026, 10, 17)

PLANO = {
    "marco": {"titulo": "Pagar o cartão", "sobra_ate": "2026-11-05", "depende_de": ["FGTS", "Depósito"]},
    "meta": {"titulo": "Wise", "alvo": 6294, "depositos": [
        {"data": "2026-11-05", "valor": 1500}, {"data": "2026-11-15", "valor": 2000}]},
    "dia_a_dia": {"orcamento": 300, "desde": "2026-10-15", "ate": "2026-10-31", "categorias": ["Alimentação"]},
    "plano": [
        {"data": "2026-10-16", "descricao": "Contadora", "sai": 250, "feito": True},
        {"data": "2026-10-19", "descricao": "FGTS", "entra": 906.08},
        {"data": "2026-10-20", "descricao": "Adiantamento", "entra": 2014.89},
        {"data": "2026-10-21", "descricao": "Passagem", "sai": 1660},
        {"data": "2026-10-23", "descricao": "Reembolsos", "entra": 921},
        {"data": "2026-10-23", "descricao": "MEI", "sai": 123.70},
        {"data": "2026-10-24", "descricao": "Cartão", "sai": 3900, "marco": True},
        {"data": "2026-10-28", "descricao": "Vivo", "sai": 59},
        {"data": "2026-11-05", "descricao": "Salário", "entra": 2958.50},
        {"data": "2026-11-05", "descricao": "Faculdade", "sai": 371},  # paid with that day's salary: out of the sobra
        {"data": "2026-11-15", "descricao": "Depois do salário", "sai": 999},
    ],
    "compromissos": [{"descricao": "Passagem", "mensal": 1660, "de": "2026-07", "ate": "2026-12"}],
}
CONTAS = [{"nome": "PJ", "tipo": "PJ", "saldo_cents": 220904}, {"nome": "A", "tipo": "PF", "saldo_cents": 3003},
          {"nome": "B", "tipo": "PF", "saldo_cents": 720}, {"nome": "C", "tipo": "PF", "saldo_cents": 666}]


def test_milestone_matches_hand_calculation():
    f = compute_financas(PLANO, CONTAS, 8150, TODAY)
    # 2252.93 + 906.08 + 2014.89 + 921 - 1660 - 123.70 = 4311.20 (the done item is already in the balances)
    assert f["marco"]["disponivel"] == 4311.20
    assert f["marco"]["pagar"] == 3900
    # 4311.20 - 3900 - Vivo 59 - day-to-day left (300 - 81.50) = 133.70; the salary on 05/11 is not counted
    assert f["marco"]["sobra"] == 133.70
    assert f["diaADia"] == {"orcamento": 300, "gasto": 81.5, "ate": "2026-10-31"}
    assert f["meta"]["acumulado"] == 0 and f["meta"]["proximo"] == {"data": "2026-11-05", "valor": 1500}
    assert [d["feito"] for d in f["meta"]["depositos"]] == [False, False]
    assert f["compromissos"] == [{"descricao": "Passagem", "mensal": 1660, "de": "2026-07", "ate": "2026-12"}]


def test_status_late_item_needs_attention():
    assert status_of({"data": "2026-10-16", "feito": True}, TODAY) == "feito"
    assert status_of({"data": "2026-10-16"}, TODAY) == "atencao"  # planned and already late
    assert status_of({"data": "2026-10-28"}, TODAY) == "previsto"
    assert status_of({"data": "2026-10-28", "marco": True}, TODAY) == "atencao"


def test_plan_without_milestone_is_an_error():
    with pytest.raises(ValueError, match="marco: true"):
        compute_financas({**PLANO, "plano": [i for i in PLANO["plano"] if not i.get("marco")]}, CONTAS, 0, TODAY)


CSV_HEAD = ("semana,rotulo,commits_clt,commits_pj,prs_clt,bugs_resolvidos,entregas_pj,"
            "cartoes_pj,aprov1_clt,aprov1_pj,const_clt,const_pj,ciclo_clt,"
            "ciclo_pj,fora_clt,fora_pj,cycle_pr_clt,lead_pj,regressoes,pendencias_pj,atend_clt\n")


def test_history_keeps_unmeasured_as_null_and_weights_after_hours(tmp_path):
    csv = tmp_path / "h.csv"
    csv.write_text(CSV_HEAD + "2026-10-10,10/10–16/10,14,13,7,3,6,3,,100,75,34.5,,,64,23,0.5,,0,3,20\n", encoding="utf-8")
    [s] = load_historico(csv)
    assert (s["inicio"], s["rotulo"], s["commitsClt"], s["commitsPj"], s["prsClt"], s["entregasPj"]) == (
        "2026-10-10", "10/10", 14, 13, 7, 6)
    assert s["aprov1Clt"] is None and s["aprov1Pj"] == 100 and s["cicloClt"] is None  # empty = not measured
    assert (s["cyclePrClt"], s["leadPj"], s["regressoesClt"], s["pendenciasPj"], s["atendClt"]) == (0.5, None, 0, 3, 20)
    assert fora_pct(s) == 44  # (64*14 + 23*13) / 27


def test_latest_grade_comes_from_weekly_notes(tmp_path):
    csv, notas = tmp_path / "h.csv", tmp_path / "n.json"
    csv.write_text(CSV_HEAD + "2026-10-03,03/10–09/10,38,27,30,10,3,,,,,,,,39,41\n"
                   "2026-10-10,10/10–16/10,14,13,7,3,6,,,,,,,,64,23\n", encoding="utf-8")
    notas.write_text(json.dumps({"2026-10-10": {"geral": 7.0, "areas": {"Foco": 7}, "destaque": "x"},
                                 "2026-10-03": {"geral": 6.0, "areas": {}, "destaque": "antiga"}}), encoding="utf-8")
    ledger = Ledger(tmp_path / "ledger.db")
    d = build_painel(ledger, {**PLANO, "semanas_csv": str(csv), "notas_json": str(notas), "semana": {"nota": 1}},
                     TODAY, datetime(2026, 10, 17, tzinfo=UTC))["desempenho"]
    assert d["atual"]["nota"] == 7.0 and d["atual"]["destaque"] == "x"
    assert [n["inicio"] for n in d["notas"]] == ["2026-10-03", "2026-10-10"]
    assert len(d["historico"]) == 2 and set(d["semanas"][0]) == {"rotulo", "commitsClt", "commitsPj", "entregasPj", "prsClt"}
    ledger.close()


def test_export_writes_private_file_only_with_plan(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert export_painel(ledger, tmp_path, TODAY, datetime(2026, 10, 17, tzinfo=UTC)) is None
    plano = {**PLANO, "semanas_csv": str(tmp_path / "nao-existe.csv"), "notas_json": str(tmp_path / "nao-existe.json")}
    (tmp_path / "plano.yaml").write_text(yaml.safe_dump(plano, allow_unicode=True) +
                                         "marcos:\n  - {data: 2026-09-15, rotulo: virada}\n", encoding="utf-8")
    out = export_painel(ledger, tmp_path, TODAY, datetime(2026, 10, 17, tzinfo=UTC))
    assert out.stat().st_mode & 0o777 == 0o600
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["financas"]["contas"] == [] and data["desempenho"]["semanas"] == []
    assert data["desempenho"]["marcos"] == [{"data": "2026-09-15", "rotulo": "virada"}]  # YAML date -> ISO string
    ledger.close()


def test_companies_default_to_generic_names_and_embed_logo(tmp_path):
    assert load_empresas({}, tmp_path) == {"clt": {"nome": "Empresa CLT"}, "pj": {"nome": "Cliente PJ"}}
    (tmp_path / "l.png").write_bytes(b"\x89PNG")
    e = load_empresas({"empresas": {"pj": {"nome": "X", "cor": "#f60", "logo": "l.png"}}}, tmp_path)
    assert e["pj"] == {"nome": "X", "cor": "#f60", "logo": "data:image/png;base64,iVBORw=="}


def test_fixed_cost_label_drops_notes_and_uses_alias():
    assert nome_fixo("Faculdade (UNIG)", {}) == "Faculdade"
    assert nome_fixo("Passagem (Pix p/ Fulana", {}) == "Passagem"  # truncated note from the config, too
    assert nome_fixo("Cartão da Fulana (6x", {"Cartão da Fulana (6x": "Cartão parcelado"}) == "Cartão parcelado"


def test_month_shift_and_same_period_of_last_month(tmp_path):
    assert _mes(date(2026, 1, 31), -1) == date(2025, 12, 1) and _mes(date(2026, 12, 5), 1) == date(2027, 1, 1)
    from fin.config import config_from_dict
    cfg = config_from_dict({"data_dir": str(tmp_path)})
    ledger = Ledger(tmp_path / "ledger.db")
    d = compute_detalhe(ledger, cfg, {}, date(2026, 3, 31))
    assert (d["mes"], d["ate"], d["anteriorAte"]) == ("2026-03", "2026-03-31", "2026-02-28")  # Feb has no 31
    assert [p["mes"] for p in d["parcelas"]][:2] == ["2026-04", "2026-05"] and len(d["parcelas"]) == 12
    ledger.close()


def test_spending_label_hides_people_and_keeps_merchants():
    row = lambda desc, norm=None, kind=None: {"description": desc, "desc_norm": norm or desc, "counterpart_kind": kind}
    assert rotulo_gasto(row("Compra no débito|TOCA DO TATU")) == "Toca do Tatu"
    assert rotulo_gasto(row("Amazon 6/7")) == "Amazon"  # installment suffix out
    assert rotulo_gasto(row("WELLHUB FULANO DA SILVA", "WELLHUB JOSE CARLOS")) == "Pessoa física (nome oculto)"
    assert rotulo_gasto(row("Compra|EDUARDODEMORAES")) == "Pessoa física (nome oculto)"  # glued name
    assert rotulo_gasto(row("Transferência enviada pelo Pix|EMPRESA XYZ", kind="person")) == "Pessoa física (nome oculto)"
    assert rotulo_gasto(row("TRANSFERENCIA ENVIADA PELO PIX")) == "Pix ou transferência"
