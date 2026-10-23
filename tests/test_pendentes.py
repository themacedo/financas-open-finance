"""pendentes.json: o que a tela Revisar recebe; a descrição real só aqui, nunca no painel.json."""
import json
from datetime import UTC, datetime

from conftest import I, NOW, TODAY, client, tx
from fin.pendentes import build_pendentes, export_pendentes
from fin.sync import run_sync


def test_lists_only_pending_rows_newest_first_with_categories_to_choose(ledger, cfg, tax, tmp_path):
    run_sync(ledger, client({I: [tx("a", I, "2026-10-10", -10.0, "PADARIA EXEMPLO"), tx("b", I, "2026-10-12", -55.0, "LOJA QUALQUER XYZ")]}),
             cfg, tax, today=TODAY, now=NOW)
    ledger.x("UPDATE transactions SET review_status='aprovado' WHERE pluggy_id='a'")
    ledger.x("UPDATE transactions SET review_status='pendente', categoria='Revisar', cat_source='llm' WHERE pluggy_id='b'")
    d = build_pendentes(ledger, tax, datetime(2026, 10, 15, tzinfo=UTC))
    assert [i["descricao"] for i in d["itens"]] == ["LOJA QUALQUER XYZ"]
    i = d["itens"][0]
    assert (i["data"], i["valor"], i["conta"], i["motivo"], i["sugestao"]) == ("2026-10-12", -55.0, "Itaú conta", "A IA não teve certeza", None)
    assert "Revisar" not in [c["nome"] for c in d["categorias"]] and {"nome": "Alimentação", "natureza": "Despesa"} in d["categorias"]
    assert d["essencial"] == ["Essencial", "Supérfluo"]
    out = export_pendentes(ledger, tax, tmp_path, datetime(2026, 10, 15, tzinfo=UTC))
    assert out.stat().st_mode & 0o777 == 0o600 and json.loads(out.read_text(encoding="utf-8"))["itens"][0]["id"] == i["id"]
