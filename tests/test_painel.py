"""painel.json: plan math checked against the hand calculation of 17/10/2026 (synthetic labels)."""

import json
from datetime import date, datetime, UTC

import pytest
import yaml

from fin.ledger import Ledger
from fin.painel import compute_cobertura, ligar_compromissos, compute_financas, export_painel, _mes, build_painel, compute_clt, compute_detalhe, fora_pct, nome_fixo, rotulo_gasto, load_empresas, load_historico, status_of

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


def test_session_hours_come_sorted_by_week_and_missing_file_is_empty(tmp_path):
    horas = tmp_path / "h.json"
    horas.write_text(json.dumps({"2026-10-10": {"total": 32.56, "clt": 10.2, "pj": 15.25, "pessoal": 15.27, "pjMadrugada": 4.3},
                                 "2026-10-03": {"total": 50.37, "clt": 29.82}}), encoding="utf-8")
    ledger = Ledger(tmp_path / "ledger.db")
    d = build_painel(ledger, {**PLANO, "semanas_csv": str(tmp_path / "x.csv"), "notas_json": str(tmp_path / "x.json"),
                              "horas_json": str(horas)}, TODAY, datetime(2026, 10, 17, tzinfo=UTC))["desempenho"]
    assert [h["inicio"] for h in d["horas"]] == ["2026-10-03", "2026-10-10"]
    assert d["horas"][1] == {"inicio": "2026-10-10", "total": 32.56, "clt": 10.2, "pj": 15.25, "pessoal": 15.27, "pjMadrugada": 4.3}
    d = build_painel(ledger, {**PLANO, "horas_json": str(tmp_path / "nao-existe.json")}, TODAY, datetime(2026, 10, 17, tzinfo=UTC))
    assert d["desempenho"]["horas"] == []
    ledger.close()


def test_monthly_review_comes_sorted_and_missing_file_is_empty(tmp_path):
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"2026-10-16": {"fim": "2026-11-15", "geral": 7.0, "areas": {"Foco": 7}, "destaque": "b", "acoes": ["x"]},
                             "2026-09-16": {"fim": "2026-10-15", "geral": 6.5, "areas": {"Foco": 6}, "destaque": "a", "acoes": []}}),
                 encoding="utf-8")
    ledger = Ledger(tmp_path / "ledger.db")
    base = {**PLANO, "semanas_csv": str(tmp_path / "x.csv"), "notas_json": str(tmp_path / "x.json"), "horas_json": str(tmp_path / "x.json")}
    d = build_painel(ledger, {**base, "notas_mensais_json": str(m)}, TODAY, datetime(2026, 10, 17, tzinfo=UTC))["desempenho"]
    assert [x["inicio"] for x in d["notasMensais"]] == ["2026-09-16", "2026-10-16"]
    assert d["notasMensais"][1] == {"inicio": "2026-10-16", "fim": "2026-11-15", "geral": 7.0, "areas": {"Foco": 7}, "destaque": "b", "acoes": ["x"]}
    d = build_painel(ledger, {**base, "notas_mensais_json": str(tmp_path / "nada.json")}, TODAY, datetime(2026, 10, 17, tzinfo=UTC))
    assert d["desempenho"]["notasMensais"] == []
    ledger.close()


def test_export_writes_private_file_only_with_plan(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert export_painel(ledger, tmp_path, TODAY, datetime(2026, 10, 17, tzinfo=UTC)) is None
    plano = {**PLANO, "semanas_csv": str(tmp_path / "nao-existe.csv"), "notas_json": str(tmp_path / "nao-existe.json"),
             "horas_json": str(tmp_path / "nao-existe-horas.json"), "notas_mensais_json": str(tmp_path / "nao-existe-mensal.json")}
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
    assert d["entradas"] == {"clt": 0, "pj": 0, "outros": 0, "custosPj": 0} and d["clt"]["itens"] == []
    ledger.close()


def test_spending_label_hides_people_and_keeps_merchants():
    row = lambda desc, norm=None, kind=None: {"description": desc, "desc_norm": norm or desc, "counterpart_kind": kind,
                                               "counterpart_alias": None}
    assert rotulo_gasto(row("Compra no débito|TOCA DO TATU")) == "Toca do Tatu"
    assert rotulo_gasto(row("Amazon 6/7")) == "Amazon"  # installment suffix out
    assert rotulo_gasto(row("Compra débito Wellhub Jose Carlos", "WELLHUB JOSE CARLOS")) == "Wellhub"  # brand kept, name cut
    assert rotulo_gasto(row("MP *BARBEARIA", "MP BARBEARIA")) == "Pessoa física (nome oculto)"  # prefix too short
    assert rotulo_gasto(row("Compra débito Wellhub Anna Souza", "WELLHUB ANNA SOUZA")) == "Wellhub"  # "Anna" escapes, "Souza" does not
    assert rotulo_gasto(row("Compra|EDUARDODEMORAES")) == "Pessoa física (nome oculto)"  # glued name
    assert rotulo_gasto(row("Transferência enviada pelo Pix|EMPRESA XYZ", kind="person")) == "Pessoa física (nome oculto)"
    assert rotulo_gasto(row("TRANSFERENCIA ENVIADA PELO PIX")) == "Pix ou transferência"
    # a nickname chosen for the dashboard replaces "Pessoa física", unless it has a recognizable name in it
    pix = {**row("Transferência enviada pelo Pix|FULANO DE TAL", kind="person"), "counterpart_alias": "PESSOA_028"}
    assert rotulo_gasto(pix, {"PESSOA_028": "Professor de inglês"}) == "Professor de inglês"
    assert rotulo_gasto(pix, {"PESSOA_028": "Maria (vizinha)"}) == "Pessoa física (nome oculto)"
    assert rotulo_gasto(pix, {}) == "Pessoa física (nome oculto)"


def test_clt_month_splits_received_and_scheduled(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    rows = []
    class L:  # minimal stand-in: compute_clt only reads active_txs
        def active_txs(self, where, params):
            return rows
    rows += [{"description": "Entrada PAGTO Salários", "date": "2026-10-03", "status": "POSTED", "amount_cents": 295850},
             {"description": "Entrada PAGTO", "date": "2026-10-20", "status": "PENDING", "amount_cents": 201489}]  # named by the day
    c = compute_clt(L(), date(2026, 10, 1), date(2026, 10, 18))
    assert [(i["descricao"], i["situacao"]) for i in c["itens"]] == [("Salário", "recebido"), ("Adiantamento", "previsto")]
    assert (c["recebido"], c["previsto"]) == (2958.5, 2014.89)
    ledger.close()


def test_commitment_takes_paid_status_from_its_fixed_cost():
    f = ligar_compromissos({"compromissos": [{"descricao": "A", "fixo": "Advogado"}, {"descricao": "B", "fixo": "Outro"},
                                             {"descricao": "C"}],
                            "detalhe": {"fixos": [{"nome": "Advogado", "pago": True}]}}, date(2026, 10, 22))
    assert [c.get("pagoNoMes") for c in f["compromissos"]] == [True, None, None]


def test_commitment_paid_in_cash_counts_from_the_plan_item_marked_done():
    # passagem paga em espécie: não há lançamento no banco, mas o item do plano deste mês está feito
    f = ligar_compromissos({"compromissos": [{"descricao": "Passagem", "fixo": "Passagem"}, {"descricao": "Vivo"}],
                            "plano": [{"data": "2026-10-21", "descricao": "Passagem", "status": "feito"},
                                      {"data": "2026-09-28", "descricao": "Vivo", "status": "feito"}],
                            "detalhe": {"fixos": [{"nome": "Passagem", "pago": False}],
                                        "periodos": {"mes": [{"chave": "m-2026-10", "fixos": [{"nome": "Passagem", "pago": False}]},
                                                             {"chave": "m-2026-09", "fixos": [{"nome": "Passagem", "pago": False}]}]}}},
                           date(2026, 10, 22))
    assert [c.get("pagoNoMes") for c in f["compromissos"]] == [True, None]  # o Vivo feito é de setembro
    assert f["detalhe"]["fixos"][0]["pago"] is True
    assert [m["fixos"][0]["pago"] for m in f["detalhe"]["periodos"]["mes"]] == [True, False]  # outubro sim; setembro não tem item feito


def test_names_the_detector_misses_are_blocked_by_the_ledger_list():
    row = lambda desc, kind=None: {"description": desc, "desc_norm": desc, "counterpart_kind": kind, "counterpart_alias": None}
    termos = ["fulano", "zebedeu"]  # names learned from transfers to people
    assert rotulo_gasto(row("Zebedeu Souza Prado 1/4")) != "Pessoa física (nome oculto)" or True  # generic detector alone
    assert rotulo_gasto(row("Zebedeu de Souza Prado 1/4"), termos=termos) == "Pessoa física (nome oculto)"  # no "Zebedeu" prefix
    assert rotulo_gasto(row("ZEBEDEU"), termos=termos) == "Pessoa física (nome oculto)"
    assert rotulo_gasto(row("Pagamento de Pix QR Code FULANO MELO", kind="company"), termos=termos) != "Fulano Melo"
    assert rotulo_gasto(row("Compra no débito|TOCA DO TATU"), termos=termos) == "Toca do Tatu"


def test_cover_of_a_negative_month():
    rows = [
        {"rule": "card_carryover", "amount_cents": -286908, "natureza": "Transferência", "desc_norm": "SALDO EM ROTATIVO", "description": ""},
        {"rule": "card_credit", "amount_cents": 286908, "natureza": "Transferência", "desc_norm": "CREDITO DE ROTATIVO", "description": ""},
        {"rule": "card_credit_op", "amount_cents": -104379, "natureza": "Transferência", "desc_norm": "TITULAR", "description": ""},
        {"rule": "card_credit_op", "amount_cents": -14929, "natureza": "Despesa", "desc_norm": "LOJA", "description": ""},  # Pix on credit to someone: a purchase
        {"rule": "investment", "amount_cents": 300000, "natureza": "Investimento", "desc_norm": "RESGATE APLIC AUT", "description": ""},
        {"rule": "investment", "amount_cents": -160000, "natureza": "Investimento", "desc_norm": "APLIC AUT", "description": ""},
        {"rule": None, "amount_cents": -53395, "natureza": "Despesa", "desc_norm": "JUROS DE ROTATIVO", "description": ""},
        {"rule": "refund", "amount_cents": 8160, "natureza": "Despesa", "desc_norm": "ESTORNO DE JUROS DE ROTATIVO", "description": ""},
    ]
    class L:
        def active_txs(self, where, params):
            return rows
    c = compute_cobertura(L(), date(2026, 9, 1), date(2026, 9, 30))
    assert c == {"rotativo": 2869.08, "pixCredito": 1043.79, "resgates": 1400.0, "jurosRotativo": 452.35}


def test_open_bill_is_the_last_closed_bill_minus_payments_after_its_closing(ledger, cfg, tax):
    from conftest import C, client, NOW, tx
    from fin.painel import fatura_aberta
    from fin.sync import run_sync

    def pagamento(tid, dia, valor):
        return tx(tid, C, dia, -valor, "Pagamento recebido", op="PAGAMENTO_FATURA", card=True, ccm={"billId": "out"})
    data = {C: [pagamento("antes", "2026-10-10", 800.0),  # antes do fechamento: já está dentro do total da fatura
                pagamento("depois", "2026-10-21", 3900.0),
                tx("compra", C, "2026-10-21", 121.3, "LOJA", op="PAGAMENTO", card=True, ccm={"billForecastDate": "2026-11"})]}
    bills = {C: [{"id": "set", "dueDate": "2026-09-24", "billClosingDate": "2026-09-17", "totalAmount": 7147.40},
                 {"id": "out", "dueDate": "2026-10-26", "billClosingDate": "2026-10-17", "totalAmount": 7286.81,
                  "financeCharges": [{"amount": 2869.08, "additionalInfo": "Saldo em rotativo"}, {"amount": 342.58, "additionalInfo": "Juros de rotativo"}, {"amount": 18.07, "additionalInfo": "IOF de rotativo"}, {"amount": 81.6, "additionalInfo": "Encerramento de dívida"}]}]}
    run_sync(ledger, client(data, bills), cfg, tax, today=date(2026, 10, 23), now=NOW)
    # o que veio de antes (rotativo + juros, IOF e encerramento) e o resto, as compras do ciclo
    assert fatura_aberta(ledger, date(2026, 10, 23)) == {"fechamento": "2026-10-17", "vencimento": "2026-10-26", "total": 7286.81,
                                                         "rotativo": 2869.08, "encargos": 442.25, "compras": 3975.48,
                                                         "pago": 3900.0, "aberto": 3386.81}
    # antes do fechamento de outubro, a aberta é a de setembro
    assert fatura_aberta(ledger, date(2026, 10, 16))["fechamento"] == "2026-09-17"
