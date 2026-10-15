import time

from fin.config import VaultUnmounted
from fin.telegram_bot import accept, Allowlist, Handlers, parse_command, split_message

ALLOW = Allowlist(chat_id=1001, user_id=1001)


def msg(chat=1001, user=1001, kind="private", text="/resumo", age=0):
    return {"chat": {"id": chat, "type": kind}, "from": {"id": user}, "text": text, "date": int(time.time()) - age}


def test_allowlist_accepts_only_owner_private_chat():
    now = time.time()
    assert accept(msg(), ALLOW, now)
    assert not accept(msg(chat=2002), ALLOW, now)              # other chat
    assert not accept(msg(user=2002), ALLOW, now)              # other user in same chat id
    assert not accept(msg(kind="group"), ALLOW, now)           # groups
    assert not accept(msg(age=3600), ALLOW, now)               # stale command after downtime
    assert not accept(None, ALLOW, now)
    assert not accept({"chat": {"id": 1001, "type": "private"}}, ALLOW, now)  # no sender


def test_parse_command():
    assert parse_command("/posso_comprar@meubot 1.200,50 10x") == ("/posso_comprar", ["1.200,50", "10x"])
    assert parse_command("oi") == ("", [])


def test_handlers_are_read_only_and_handle_unmounted_vault(cfg, tax, ledger):
    def unmounted():
        raise VaultUnmounted("x")

    h = Handlers(unmounted, cfg, tax)
    assert "Cofre desmontado" in h("/resumo", [])
    assert "Comandos" in h("/ajuda", [])
    assert "Não conheço" in h("/apagar_tudo", [])
    from fin.ledger import Ledger

    h = Handlers(lambda: Ledger(ledger.path), cfg, tax)
    before = ledger.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert "Uso:" in h("/posso_comprar", [])
    assert "inválido" in h("/posso_comprar", ["abc"])
    assert h("/pendentes", []) == "Nada pendente."
    assert h("/resumo", []).startswith("<pre>")
    assert ledger.one("SELECT COUNT(*) n FROM transactions")["n"] == before


def test_split_message_keeps_pre_balanced():
    text = "<pre>" + "\n".join(f"linha {i:04d}" for i in range(2000)) + "</pre>"
    parts = split_message(text)
    assert len(parts) > 1 and all(p.startswith("<pre>") and p.endswith("</pre>") and len(p) <= 4096 for p in parts)
