"""Secrets in the macOS Keychain (via keyring). Never printed, never logged, never in files.

Service "fin"; accounts listed in NAMES. `fin setup` asks with getpass (no echo).
"""

from __future__ import annotations

import getpass
import json
from collections.abc import Callable
from pathlib import Path

import keyring
import keyring.errors

SERVICE = "fin"
NAMES = {
    "pluggy_client_id": "Pluggy Client ID",
    "pluggy_client_secret": "Pluggy Client Secret",
    "anthropic_api_key": "Anthropic API key",
    "google_service_account_json": "JSON da service account Google",
    "telegram_bot_token": "Token do bot Telegram",
    "telegram_allowed_chat_id": "chat_id permitido",
    "telegram_allowed_user_id": "user_id permitido",
}
GROUPS = {
    "pluggy": ["pluggy_client_id", "pluggy_client_secret"],
    "anthropic": ["anthropic_api_key"],
    "telegram": ["telegram_bot_token"],
}


class SecretMissing(RuntimeError):
    pass


def get(name: str) -> str | None:
    if name not in NAMES:
        raise KeyError(name)
    try:
        return keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError as exc:  # locked, access denied, PasswordGetError...
        raise SecretMissing(f"Keychain trancado ou acesso negado ({type(exc).__name__}): desbloqueie a sessão "
                            "ou clique em 'Permitir sempre'") from exc


def require(name: str) -> str:
    value = get(name)
    if not value:
        group = next((g for g, names in GROUPS.items() if name in names), "google" if "google" in name else "telegram-discover")
        raise SecretMissing(f"segredo '{NAMES[name]}' ausente: rode `fin setup {group}`")
    return value


def put(name: str, value: str) -> None:
    if name not in NAMES:
        raise KeyError(name)
    keyring.set_password(SERVICE, name, value)


def status() -> dict[str, bool]:
    """Presence only. Never returns values."""
    out = {}
    for name in NAMES:
        try:
            out[name] = bool(get(name))
        except SecretMissing:
            out[name] = False
    return out


def setup_group(group: str, ask: Callable[[str], str] = getpass.getpass, say: Callable[[str], None] = print) -> None:
    for name in GROUPS[group]:
        value = ask(f"{NAMES[name]} (não aparece na tela): ").strip()
        if not value:
            say(f"{NAMES[name]}: vazio, mantido o valor anterior.")
            continue
        put(name, value)
        say(f"{NAMES[name]}: gravado no Keychain.")


def setup_google(key_file: Path, say: Callable[[str], None] = print) -> str:
    """Reads the downloaded JSON (too long for getpass: 1024-byte line limit), stores it minified."""
    data = json.loads(Path(key_file).read_text(encoding="utf-8"))
    if data.get("type") != "service_account" or "private_key" not in data or "client_email" not in data:
        raise ValueError("arquivo não parece uma chave de service account")
    put("google_service_account_json", json.dumps(data, separators=(",", ":")))
    if json.loads(get("google_service_account_json") or "{}").get("client_email") != data["client_email"]:
        raise RuntimeError("o Keychain não devolveu o JSON inteiro (limite de tamanho?)")
    say(f"Service account gravada. Compartilhe a planilha (Editor) com: {data['client_email']}")
    say(f"Agora apague o arquivo baixado: {key_file}")
    return data["client_email"]
