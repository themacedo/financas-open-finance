from datetime import date

from conftest import C, client, I, NB, NOW, OWN_CPF, OWN_ITAU, party, S, TODAY, tx

from fin.reports import month_totals
from fin.sync import run_sync


def _row(ledger, pid):
    return ledger.one("SELECT * FROM transactions WHERE pluggy_id=?", (pid,))


def test_internal_transfer_itau_to_santander(ledger, cfg, tax):
    data = {
        I: [tx("out", I, "2026-10-10", -700.0, "PIX ENVIADO TITULAR TESTE", op="PIX", method="PIX", payer=OWN_ITAU,
               receiver=party("TITULAR TESTE", OWN_CPF, "033", "99887-1"))],
        S: [tx("in", S, "2026-10-11", 700.0, "PIX RECEBIDO TITULAR TESTE", op="PIX", method="PIX",
               payer=party("TITULAR TESTE", OWN_CPF, "341", "12345-6"))],
    }
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    a, b = _row(ledger, "out"), _row(ledger, "in")
    assert a["natureza"] == b["natureza"] == "Transferência"
    assert a["pair_id"] and a["pair_id"] == b["pair_id"]
    assert a["counterpart_kind"] == "own"
    t = month_totals(ledger, "2026-10")
    assert t["receitas"] == 0 and t["despesas"] == 0


def test_transfer_without_counterpart_pairs_by_value_and_date(ledger, cfg, tax):
    # Nubank never sends counterpart on same-institution transfers: only value + date within 2 days
    data = {
        I: [tx("out", I, "2026-10-07", -1500.0, "PIX ENVIADO", op="PIX")],
        NB: [tx("in", NB, "2026-10-09", 1500.0, "TRANSFERENCIA RECEBIDA PELO PIX", op="PIX")],
    }
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    assert _row(ledger, "out")["pair_id"] == _row(ledger, "in")["pair_id"] is not None
    # 3 days apart: no pair -> both treated as people (never sent to the LLM)
    data[NB][0]["date"] = "2026-10-11T00:00:00.000Z"
    data[NB][0]["id"] = "in2"
    run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 16), now=NOW)
    assert _row(ledger, "out")["rule"] == "person"


def test_bill_payment_is_transfer_not_expense(ledger, cfg, tax):
    data = {
        I: [tx("boleto", I, "2026-10-08", -158.4, "PAGTO FATURA CARTAO EXEMPLO", op="BOLETO", method="BOLETO",
               payer=OWN_ITAU, receiver=party("EMISSOR", "33.333.333/0001-33", "999"))],
        C: [tx("p1", C, "2026-09-20", 100.0, "SUPERMERCADO EXEMPLO", op="PAGAMENTO", card=True, ccm={"billForecastDate": "2026-10"}),
            tx("p2", C, "2026-09-25", 58.4, "NETFLIX.COM", op="PAGAMENTO", card=True, ccm={"billForecastDate": "2026-10"}),
            tx("pay", C, "2026-10-08", -158.4, "Pagamento recebido", op="PAGAMENTO_FATURA", card=True, ccm={"billForecastDate": "2026-11"})],
    }
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    bank, card = _row(ledger, "boleto"), _row(ledger, "pay")
    assert bank["natureza"] == card["natureza"] == "Transferência"
    assert bank["pair_id"] == card["pair_id"] is not None
    t = month_totals(ledger, "2026-10")
    assert t["despesas"] == 15840  # only the purchases, once
    # without the card-side payment the bank row goes to review (not silently an expense)
    data[C] = data[C][:2]
    data[I][0]["id"] = "boleto-x"
    data[I][0]["date"] = "2026-10-20T00:00:00.000Z"
    run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 21), now=NOW)
    row = _row(ledger, "boleto-x")
    assert row["rule"] == "bill_payment_unpaired" and row["review_status"] == "pendente"


def test_refund_reduces_expense_and_inherits_category(ledger, cfg, tax):
    from fin.categorize import run_categorize

    data = {C: [tx("buy", C, "2026-09-05", 120.0, "LOJA ROUPAS EXEMPLO", op="PAGAMENTO", card=True, ccm={"billForecastDate": "2026-10"}),
                tx("ref", C, "2026-09-20", -30.0, "ESTORNO LOJA ROUPAS EXEMPLO", op="ESTORNO", card=True, ccm={"billForecastDate": "2026-10"})]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    ledger.add_dictionary({"kind": "exact", "pattern": "LOJA ROUPAS EXEMPLO", "categoria": "Compras", "origin": "user"})
    run_categorize(ledger, cfg, tax, None)
    ref = _row(ledger, "ref")
    assert ref["rule"] == "refund" and ref["natureza"] == "Despesa" and ref["categoria"] == "Compras"
    assert ref["amount_cents"] == 3000
    assert month_totals(ledger, "2026-10")["categorias"]["Compras"] == 9000


def test_fee_investment_salary(ledger, cfg, tax):
    data = {I: [tx("tar", I, "2026-10-10", -19.9, "TARIFA PACOTE", op="PACOTE_TARIFA_SERVICOS"),
                tx("rend", I, "2026-10-14", 3.12, "REND PAGO APLIC AUT", op="RENDIMENTO_APLIC_FINANCEIRA"),
                tx("apl", I, "2026-10-21", -2014.89, "Saída APL APLIC AUT MAIS AP", op="OUTROS"),
                tx("sal", I, "2026-10-05", 8500.0, "SALARIO", op="FOLHA_PAGAMENTO", method="TED",
                   payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU)],
            C: [tx("iof", C, "2026-10-11", 1.92, "IOF COMPRA INTERNACIONAL", card=True, ccm={"billForecastDate": "2026-11"})]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    assert _row(ledger, "tar")["categoria"] == "Impostos/Taxas"
    assert _row(ledger, "iof")["categoria"] == "Impostos/Taxas"
    assert _row(ledger, "rend")["natureza"] == "Investimento"
    assert _row(ledger, "apl")["natureza"] == "Investimento"
    sal = _row(ledger, "sal")
    assert (sal["natureza"], sal["categoria"], sal["fonte"]) == ("Receita", "Salário CLT", "Empresa CLT")


def _parcela(n, day, forecast, status="POSTED", tid=None):
    return tx(tid or f"parc{n}", C, day, 250.0, f"LOJA ELETRONICOS EXEMPLO PARC {n:02d}/10", op="PAGAMENTO", card=True,
              status=status, created_at=f"{day}T09:00:00.000Z",
              ccm={"installmentNumber": n, "totalInstallments": 10, "billForecastDate": forecast,
                   "purchaseDate": "2026-08-01T00:00:00.000Z", "paymentType": "INSTALLMENT"})


def test_installment_3_of_10_projects_4_to_10_then_matches_real_4(ledger, cfg, tax):
    data = {C: [_parcela(3, "2026-10-01", "2026-10")]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    proj = ledger.q("SELECT installment_number, month, amount_cents FROM installments_projection ORDER BY installment_number")
    assert [p["installment_number"] for p in proj] == [4, 5, 6, 7, 8, 9, 10]
    assert [p["month"] for p in proj] == ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05"]
    assert all(p["amount_cents"] == -25000 for p in proj)
    # the real 4/10 arrives (as a future PENDING row): it replaces the projection
    data[C].append(_parcela(4, "2026-11-01", "2026-11", status="PENDING"))
    run_sync(ledger, client(data), cfg, tax, today=date(2026, 10, 16), now=NOW)
    proj = ledger.q("SELECT installment_number FROM installments_projection ORDER BY installment_number")
    assert [p["installment_number"] for p in proj] == [5, 6, 7, 8, 9, 10]
    assert ledger.one("SELECT COUNT(DISTINCT group_key) n FROM installments_projection")["n"] == 1
    from fin.reports import committed_installments

    committed = committed_installments(ledger, ["2026-11", "2026-12"])
    assert committed == {"2026-11": 25000, "2026-12": 25000}  # real 4/10 + projected 5/10, no double count


def test_two_identical_installment_purchases_stay_separate(ledger, cfg, tax):
    a = _parcela(2, "2026-10-01", "2026-10", tid="a2")
    b = _parcela(2, "2026-10-01", "2026-10", tid="b2")
    b["amount"] = 99.0
    run_sync(ledger, client({C: [a, b]}), cfg, tax, today=TODAY, now=NOW)
    assert ledger.one("SELECT COUNT(DISTINCT group_key) n FROM installments_projection")["n"] == 2


def test_person_pix_gets_alias(ledger, cfg, tax):
    data = {I: [tx("p", I, "2026-10-09", -80.0, "PIX ENVIADO AMIGO EXEMPLO", op="PIX", method="PIX", payer=OWN_ITAU,
                   receiver=party("AMIGO EXEMPLO", "555.555.555-55", "104"))]}
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    row = _row(ledger, "p")
    assert row["counterpart_alias"] == "PESSOA_001" and row["rule"] == "person"
    assert row["review_status"] == "pendente" and row["cat_source"] == "counterpart"
    assert row["counterpart_hash"] and "555" not in row["counterpart_hash"]
