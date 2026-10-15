"""Shared fixtures. Everything is synthetic; no network, no Keychain."""

from __future__ import annotations

import copy
from datetime import date, datetime, UTC

import keyring
import keyring.backend
import pytest

from fin.config import config_from_dict, load_taxonomy
from fin.demo import ACC, bank_account, card_account, CNPJ_CARD, CNPJ_CLT, CNPJ_PJ, item, ITEMS, OWN_CPF, party, tx
from fin.ledger import Ledger
from fin.pluggy import MemoryPluggyClient

TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
I, S, NB, C = ACC["itau"], ACC["santander"], ACC["nubank"], ACC["card"]
OWN_ITAU = party("TITULAR TESTE", OWN_CPF, "341", "12345-6", "0001")


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)  # never touch the real macOS Keychain from tests
    yield kr
    keyring.set_keyring(previous)


@pytest.fixture
def tax():
    return load_taxonomy()


def make_cfg(tmp_path, **overrides):
    raw = {
        "data_dir": str(tmp_path / "data"),
        "pluggy": {"items": [{"bank": b, "item_id": iid} for b, iid in ITEMS.items()], "missing_syncs_to_remove": 3},
        "cards": {"nubank": {"issuer_cnpj": CNPJ_CARD, "closing_day": 3, "due_day": 10}},
        "income": {"salary_day": 5, "sources": [
            {"cnpj": "11111111000111", "fonte": "Empresa CLT", "categoria": "Salário CLT"},
            {"cnpj": "22222222000122", "fonte": "Cliente PJ", "categoria": "PJ"}]},
        "privacy": {"block_terms": ["titular teste"]},
    }
    raw.update(overrides)
    return config_from_dict(raw)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(tmp_path / "ledger.db")
    yield lg
    lg.close()


def accounts():
    return {
        ITEMS["itau"]: [bank_account(I, ITEMS["itau"], "0001/12345-6", "341", 5000.00)],
        ITEMS["nubank"]: [bank_account(NB, ITEMS["nubank"], "0001/7654321-0", "260", 1000.00),
                          card_account(C, ITEMS["nubank"], "1234", 10000, 1000, "2026-11-03", "2026-11-10")],
        ITEMS["santander"]: [bank_account(S, ITEMS["santander"], "0002/99887-1", "033", 800.00)],
    }


def client(txs: dict[str, list[dict]], bills: dict | None = None) -> MemoryPluggyClient:
    items = {iid: item(iid) for iid in ITEMS.values()}
    return MemoryPluggyClient(items, accounts(), copy.deepcopy({I: [], S: [], NB: [], C: [], **txs}), bills or {})


__all__ = ["TODAY", "NOW", "I", "S", "NB", "C", "OWN_ITAU", "OWN_CPF", "CNPJ_CARD", "CNPJ_CLT", "CNPJ_PJ",
           "client", "make_cfg", "party", "tx"]
