"""Monthly report checked against numbers computed by hand (see comments)."""

from conftest import C, client, I, make_cfg, NOW, OWN_CPF, OWN_ITAU, party, S, TODAY, tx

from fin.categorize import approve, run_categorize
from fin.reports import can_i_buy, format_bills, format_summary, monthly_summary
from fin.sync import run_sync


def _scenario(tmp_path, ledger, tax):
    cfg = make_cfg(tmp_path, fixed_costs=[{"nome": "Aluguel", "valor": 1500, "dia": 10, "padrao": "LOCADOR"},
                                          {"nome": "Internet", "valor": 120, "dia": 20, "padrao": "VIVO"}])
    f10 = {"billForecastDate": "2026-10"}
    data = {
        I: [tx("sal", I, "2026-10-05", 8500.0, "SALARIO", op="FOLHA_PAGAMENTO", method="TED",
               payer=party("EMPRESA", "11.111.111/0001-11", "001"), receiver=OWN_ITAU),                      # +8500 Empresa CLT
            tx("pj", I, "2026-10-06", 6000.0, "PIX RECEBIDO CLIENTE PJ", op="PIX", method="PIX",
               payer=party("CLIENTE PJ", "22.222.222/0001-22", "237"), receiver=OWN_ITAU),                   # +6000 Cliente PJ
            tx("rent", I, "2026-10-10", -1500.0, "PIX ENVIADO LOCADOR EXEMPLO", op="PIX", method="PIX", payer=OWN_ITAU,
               receiver=party("LOCADOR", "444.444.444-44", "104")),                                           # Moradia 1500
            tx("tar", I, "2026-10-10", -19.9, "TARIFA PACOTE", op="PACOTE_TARIFA_SERVICOS"),                 # Taxas 19,90
            tx("cdb", I, "2026-10-12", -500.0, "APLICACAO CDB", op="OUTROS"),                                # invest 500
            tx("rend", I, "2026-10-14", 3.12, "REND PAGO APLIC AUT", op="RENDIMENTO_APLIC_FINANCEIRA"),     # invest -3,12
            tx("toS", I, "2026-10-10", -700.0, "PIX ENVIADO", op="PIX", method="PIX", payer=OWN_ITAU,
               receiver=party("TITULAR", OWN_CPF, "033", "99887-1")),                                         # transfer
            tx("bol", I, "2026-10-08", -700.0, "PAGTO FATURA CARTAO", op="BOLETO", method="BOLETO", payer=OWN_ITAU,
               receiver=party("EMISSOR", "33.333.333/0001-33", "999"))],                                      # bill payment
        S: [tx("fromI", S, "2026-10-10", 700.0, "PIX RECEBIDO", op="PIX", method="PIX",
               payer=party("TITULAR", OWN_CPF, "341", "12345-6"))],
        C: [tx("mkt", C, "2026-09-20", 400.0, "SUPERMERCADO EXEMPLO", card=True, ccm=f10),                  # Alim 400
            tx("ifd", C, "2026-09-22", 100.0, "IFD*IFOOD 99", card=True, ccm=f10),                           # Alim 100
            tx("est", C, "2026-09-25", -50.0, "ESTORNO SUPERMERCADO EXEMPLO", op="ESTORNO", card=True, ccm=f10),  # Alim -50
            tx("par", C, "2026-09-10", 250.0, "LOJA ELETRONICOS PARC 2/4", card=True,
               ccm={**f10, "installmentNumber": 2, "totalInstallments": 4}),                                   # Compras 250
            tx("nfx", C, "2026-10-08", 55.9, "NETFLIX.COM", card=True, ccm={"billForecastDate": "2026-11"}),  # November
            tx("pay", C, "2026-10-08", -700.0, "Pagamento recebido", op="PAGAMENTO_FATURA", card=True, ccm={})],
    }
    run_sync(ledger, client(data), cfg, tax, today=TODAY, now=NOW)
    for kind, pattern, cat in (("contains", "SUPERMERCADO", "Alimentação"), ("contains", "IFOOD", "Alimentação"),
                               ("contains", "LOJA ELETRONICOS", "Compras"), ("contains", "NETFLIX", "Assinaturas")):
        ledger.add_dictionary({"kind": kind, "pattern": pattern, "categoria": cat, "origin": "user"})
    run_categorize(ledger, cfg, tax, None)
    rent = ledger.one("SELECT local_id FROM transactions WHERE pluggy_id='rent'")["local_id"]
    approve(ledger, tax, rent, categoria="Moradia", essencial="Essencial", necessidade="fixo")
    return cfg


def test_monthly_summary_matches_hand_calculation(tmp_path, ledger, tax):
    cfg = _scenario(tmp_path, ledger, tax)
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert s.receitas == 1_450_000
    assert s.receitas_por_fonte == {"Empresa CLT": 850_000, "Cliente PJ": 600_000}
    assert s.despesas_por_categoria == {"Moradia": 150_000, "Alimentação": 45_000, "Compras": 25_000, "Impostos/Taxas": 1_990}
    assert s.despesas == 221_990                       # 1500 + 450 + 250 + 19,90
    assert s.investimentos == 49_688                   # 500 - 3,12
    assert s.saldo == 1_228_010                        # 14500 - 2219,90
    assert round(s.taxa_poupanca, 4) == 0.8469         # 12280,10 / 14500
    assert s.parcelas["2026-11"] == 25_000 and s.parcelas["2026-12"] == 25_000 and s.parcelas["2027-01"] == 0
    assert s.saldo_contas == 680_000                   # 5000 + 1000 + 800
    assert s.fixos_pendentes == [("Internet", 12_000, 20)]
    assert s.fixos_ate_salario == 12_000
    assert s.faturas_ate_salario == 0                  # Oct bill already due, Nov bill due after next salary
    assert s.saldo_livre == 668_000                    # 6800 - 120
    assert s.renda_ref == 1_450_000
    assert s.pendentes == 0
    text = format_summary(s)
    assert "R$ 14.500,00" in text and "R$ 2.219,90" in text and "84,7%" in text and "R$ 6.680,00" in text


def test_can_i_buy(tmp_path, ledger, tax):
    cfg = _scenario(tmp_path, ledger, tax)
    s = monthly_summary(ledger, cfg, tax, "2026-10", TODAY)
    assert can_i_buy(s, 700_000).startswith("Não")       # 6680 - 7000 < 0
    assert can_i_buy(s, 600_000).startswith("Apertado")  # 680 left < 10% of income (1450)
    assert can_i_buy(s, 100_000).startswith("Sim")
    assert "10x de R$ 300,00" in can_i_buy(s, 300_000, 10)


def test_bills_text(tmp_path, ledger, tax):
    cfg = _scenario(tmp_path, ledger, tax)
    text = format_bills(ledger, cfg, TODAY)
    assert "2026-10 vence 10/10: R$ 700,00 [paga]" in text            # 400 + 100 - 50 + 250
    assert "2026-11 vence 10/11: R$ 305,90 [aberta]" in text           # Netflix 55,90 + projected 3/4 250
    assert "inclui R$ 250,00 de parcelas projetadas" in text
