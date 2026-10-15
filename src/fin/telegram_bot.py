"""Telegram bot (long polling, read-only). Answers ONLY the allowlisted private chat/user;
everything else is dropped silently (no reply, content not logged). No command changes data.
"""

from __future__ import annotations

import html
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from fin.config import Config, Taxonomy, VaultUnmounted
from fin.ledger import Ledger, local_id_str
from fin.money import brl, parse_brl
from fin.reports import can_i_buy, format_bills, format_summary, monthly_summary, ym

log = logging.getLogger("fin.bot")
MAX_AGE_S = 600
CHUNK = 4000
HELP = ("Comandos:\n/resumo [AAAA-MM] — resumo do mês (PF, PJ e consolidado)\n/faturas — cartões e faturas\n"
        "/posso_comprar <valor> [Nx] — ex.: /posso_comprar 1200 10x (só PF: contas, cartão e renda da pessoa física)\n"
        "/pendentes — lançamentos para revisar")


@dataclass(frozen=True)
class Allowlist:
    chat_id: int
    user_id: int


def accept(message: dict[str, Any] | None, allow: Allowlist, now: float) -> bool:
    if not message:
        return False
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or chat.get("id") != allow.chat_id or sender.get("id") != allow.user_id:
        return False
    if now - float(message.get("date") or 0) > MAX_AGE_S:
        return False  # stale command after downtime
    return True


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    return parts[0].split("@")[0].lower(), parts[1:]


def pre(text: str) -> str:
    return f"<pre>{html.escape(text, quote=False)}</pre>"


class Handlers:
    def __init__(self, open_ledger: Callable[[], Ledger], cfg: Config, tax: Taxonomy,
                 today: Callable[[], date] = date.today):
        self.open_ledger, self.cfg, self.tax, self.today = open_ledger, cfg, tax, today

    def __call__(self, cmd: str, args: list[str]) -> str:
        if cmd in ("/start", "/ajuda", "/help"):
            return html.escape(HELP, quote=False)
        if cmd not in ("/resumo", "/faturas", "/posso_comprar", "/pendentes"):
            return html.escape("Não conheço esse comando.\n" + HELP, quote=False)
        try:
            ledger = self.open_ledger()
        except VaultUnmounted:
            return "Cofre desmontado: monte o volume FinVault no Mac e tente de novo."
        try:
            today = self.today()
            if cmd == "/resumo":
                month = args[0] if args and len(args[0]) == 7 else ym(today)
                return pre(format_summary(monthly_summary(ledger, self.cfg, self.tax, month, today)))
            if cmd == "/faturas":
                return pre(format_bills(ledger, self.cfg, today))
            if cmd == "/posso_comprar":
                if not args:
                    return "Uso: /posso_comprar 1200 [10x]"
                try:
                    value = parse_brl(args[0])
                    n = int(args[1].lower().rstrip("x")) if len(args) > 1 else 1
                except ValueError:
                    return "Valor inválido. Ex.: /posso_comprar 1.200,50 10x"
                if value <= 0 or not 1 <= n <= 48:
                    return "Valor ou parcelas fora do intervalo."
                s = monthly_summary(ledger, self.cfg, self.tax, ym(today), today)
                return pre(can_i_buy(s, value, n))
            rows = ledger.active_txs("review_status='pendente'")
            if not rows:
                return "Nada pendente."
            lines = [f"{len(rows)} pendente(s). Aprove no Sheets (aba revisoes) ou com fin review."]
            for r in rows[:15]:
                # Telegram is not end-to-end encrypted: a row kept from the LLM (may hold a name) is not shown here
                desc = r["counterpart_alias"] or ("(bloqueado: fin review)" if r["cat_source"] == "blocked" else r["desc_norm"])
                desc = desc or "(sem descrição)"
                lines.append(f"{local_id_str(r['local_id'])} {r['date'][5:]} {brl(r['amount_cents']):>12} {desc[:22]} → {r['categoria'] or '?'}")
            return pre("\n".join(lines))
        finally:
            ledger.close()


def split_message(text: str, limit: int = CHUNK) -> list[str]:
    """Split by lines keeping <pre> blocks balanced."""
    if len(text) <= limit:
        return [text]
    is_pre = text.startswith("<pre>") and text.endswith("</pre>")
    body = text[5:-6] if is_pre else text
    chunks, cur = [], ""
    for line in body.split("\n"):
        if len(cur) + len(line) + 1 > limit - 11:
            chunks.append(cur)
            cur = ""
        cur += (line + "\n")
    if cur:
        chunks.append(cur)
    return [f"<pre>{c.rstrip()}</pre>" if is_pre else c.rstrip() for c in chunks]


class TelegramAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._http = httpx.Client(timeout=httpx.Timeout(60, connect=10), transport=transport)

    def __repr__(self) -> str:
        return "TelegramAPI(<redacted>)"

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(self._base + method, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error_code": resp.status_code}
        return data

    def send(self, chat_id: int, text: str) -> None:
        for part in split_message(text):
            data = self.call("sendMessage", {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                                              "link_preview_options": {"is_disabled": True}})
            if not data.get("ok") and data.get("error_code") == 429:
                time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)))
                self.call("sendMessage", {"chat_id": chat_id, "text": part, "parse_mode": "HTML"})
            time.sleep(1.0)  # ~1 msg/s per chat


def notify(token: str | None, chat_id: str | None, text: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        TelegramAPI(token).send(int(chat_id), html.escape(text, quote=False))
        return True
    except (httpx.HTTPError, ValueError):
        log.warning("falha ao enviar alerta pelo Telegram")
        return False


def run_bot(api: TelegramAPI, allow: Allowlist, handlers: Callable[[str, list[str]], str]) -> None:
    stop = {"flag": False}

    def _term(*_: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    backoff = 1
    while not stop["flag"]:  # boot without network: wait instead of crashing (launchd would restart every 30 s)
        try:
            api.call("deleteWebhook", {"drop_pending_updates": False})
            break
        except httpx.HTTPError:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    offset = 0
    backoff = 1
    log.info("bot iniciado")
    while not stop["flag"]:
        try:
            data = api.call("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]})
        except httpx.HTTPError:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if not data.get("ok"):
            code = data.get("error_code")
            if code == 401:
                log.error("telegram 401: token inválido (rode fin setup telegram); bot parado sem relançar")
                return
            if code == 429:
                time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)))
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            continue
        backoff = 1
        for upd in data.get("result") or []:
            offset = max(offset, int(upd["update_id"]) + 1)
            msg = upd.get("message")
            if not accept(msg, allow, time.time()):
                continue  # silent drop
            cmd, args = parse_command(msg.get("text") or "")
            if not cmd:
                continue
            try:
                reply = handlers(cmd, args)
            except Exception as exc:
                log.error("erro no comando %s: %s", cmd, type(exc).__name__)
                log.debug("detalhe", exc_info=True)
                reply = "Erro interno ao gerar a resposta (veja o log no Mac)."
            try:
                api.send(allow.chat_id, reply)
            except httpx.HTTPError as exc:
                log.warning("falha ao responder (%s)", type(exc).__name__)
    log.info("bot encerrado")
