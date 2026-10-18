"""Command line: fin <setup|sync|categorize|review|publish|report|bot|doctor|caju|demo>."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import plistlib
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path

from fin import secrets
from fin.config import ConfigError, load_config, load_taxonomy, PROJECT_ROOT, VaultUnmounted
from fin.ledger import Ledger, local_id_str, parse_local_id
from fin.money import brl, parse_brl

log = logging.getLogger("fin")


class _Redact(logging.Filter):
    """Belt and braces: drop any record that could carry a token/key."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        return not ("api.telegram.org/bot" in msg or "X-API-KEY" in msg or "sk-ant-" in msg)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    log_file = os.environ.get("FIN_LOG_FILE")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.addFilter(_Redact())
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # httpx logs full URLs (Telegram token) at INFO


def _open():
    cfg = load_config()
    tax = load_taxonomy()
    cfg.ensure_data_dir()
    return cfg, tax, Ledger(cfg.db_path)


def _pluggy_client():
    from fin.pluggy import HttpPluggyClient

    return HttpPluggyClient(secrets.require("pluggy_client_id"), secrets.require("pluggy_client_secret"))


def _categorizer(cfg, ledger):
    from fin.llm import AnthropicCategorizer, make_client

    if not cfg.llm_enabled:
        return None
    key = secrets.get("anthropic_api_key")
    if not key:
        log.warning("Anthropic sem chave: desconhecidos ficam como Revisar")
        return None
    return AnthropicCategorizer(make_client(key, cfg.llm_workspace_id), ledger, block_terms=cfg.block_terms)


def _notify(cfg, text: str) -> None:
    if not cfg.telegram_enabled:
        return
    from fin.telegram_bot import notify

    try:
        notify(secrets.get("telegram_bot_token"), secrets.get("telegram_allowed_chat_id"), text)
    except secrets.SecretMissing:
        pass


# ---- commands --------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    what = args.what
    if what == "status":
        for name, ok in secrets.status().items():
            print(f"  {'ok ' if ok else '-- '} {secrets.NAMES[name]}")
        return 0
    if what in secrets.GROUPS:
        secrets.setup_group(what)
        return 0
    if what == "google":
        if not args.key_file:
            print("Use: fin setup google --key-file ~/Downloads/<arquivo>.json")
            return 2
        secrets.setup_google(Path(args.key_file).expanduser())
        return 0
    if what == "telegram-discover":
        return _telegram_discover()
    if what == "dictionary":
        import yaml

        from fin.categorize import load_seed_dictionary

        _cfg, _tax, ledger = _open()
        n = load_seed_dictionary(ledger, yaml.safe_load((PROJECT_ROOT / "dictionary.seed.yaml").read_text(encoding="utf-8")))
        print(f"{n} entradas do dicionário inicial carregadas (as suas não foram alteradas).")
        personal = Path(_cfg.data_dir) / "dicionario_pessoal.yaml"  # inside the vault: may hold names
        if personal.exists():
            entries = yaml.safe_load(personal.read_text(encoding="utf-8")) or []
            for e in entries:
                e.setdefault("origin", "user")
                e.setdefault("priority", 10)
                kind = e.get("kind", "contains")
                pattern = (str(e["pattern"]) if kind == "raw" else str(e["pattern"]).upper() if kind != "cnpj"
                           else "".join(c for c in str(e["pattern"]) if c.isdigit()))
                ledger.add_dictionary({"kind": kind, "pattern": pattern, "natureza": e.get("natureza"),
                                       "categoria": e["categoria"], "subcategoria": e.get("subcategoria"),
                                       "necessidade": e.get("necessidade"), "essencial": e.get("essencial"),
                                       "fonte": e.get("fonte"), "origin": e["origin"], "priority": int(e["priority"])})
            print(f"{len(entries)} entradas do seu dicionário pessoal ({personal}) aplicadas.")
        return 0
    print(f"setup desconhecido: {what}")
    return 2


def _telegram_discover() -> int:
    from fin.telegram_bot import TelegramAPI

    api = TelegramAPI(secrets.require("telegram_bot_token"))
    data = api.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    if not data.get("ok"):
        print(f"Telegram respondeu erro {data.get('error_code')}. Confira o token (fin setup telegram).")
        return 1
    found = {}
    last = None
    for u in data.get("result") or []:
        last = u["update_id"]
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") == "private":
            found[chat["id"]] = (m.get("from") or {})
    if not found:
        print("Nenhuma mensagem privada. Mande /start para o seu bot no Telegram e rode de novo.")
        return 1
    for chat_id, frm in found.items():
        print(f"  chat_id={chat_id} tipo=private usuario=@{frm.get('username', '?')}")
    if len(found) > 1:
        print("Mais de um chat encontrado; confira qual é o seu.")
    chat_id, frm = next(iter(found.items()))
    if input(f"Gravar chat_id {chat_id} (@{frm.get('username', '?')}) como único permitido? [s/N] ").strip().lower() != "s":
        return 1
    secrets.put("telegram_allowed_chat_id", str(chat_id))
    secrets.put("telegram_allowed_user_id", str(frm.get("id")))
    if last is not None:
        api.call("getUpdates", {"offset": last + 1, "timeout": 0})  # confirm what was read
    print("Allowlist gravada no Keychain.")
    return 0


SYNC_TIMEOUT_S = int(os.environ.get("FIN_SYNC_TIMEOUT", "1800"))


def _watchdog(seconds: int) -> threading.Timer:
    """Hard deadline for the scheduled sync. A Keychain dialog nobody answers blocks inside C code (a Python
    signal would never fire), and launchd does not start a second instance: the next days would not run."""
    def _kill() -> None:
        log.error("sync passou de %ss (Keychain esperando 'Permitir'? rede travada?): encerrado", seconds)
        logging.shutdown()
        os._exit(124)

    t = threading.Timer(seconds, _kill)
    t.daemon = True
    t.start()
    return t


def cmd_sync(args) -> int:
    cfg = load_config()
    tax = load_taxonomy()
    try:
        cfg.ensure_data_dir()
    except VaultUnmounted as exc:
        print(str(exc))
        log.error("sync não rodou: cofre indisponível")
        if not args.no_notify:
            _notify(cfg, f"fin sync NÃO rodou: {exc}. Monte com scripts/montar-cofre.sh e rode launchctl kickstart.")
        return 3
    watchdog = _watchdog(SYNC_TIMEOUT_S)
    ledger = Ledger(cfg.db_path)
    try:
        if args.se_houver_novidade:
            from fin.sync import ha_novidade

            novo, motivo = ha_novidade(ledger, _pluggy_client(), cfg)
            print(f"novidade no Pluggy: {'sim' if novo else 'não'} ({motivo})")
            if not novo:
                return 0
        return _sync(args, cfg, tax, ledger)
    except Exception as exc:
        log.error("sync falhou: %s", type(exc).__name__)
        log.debug("detalhe", exc_info=True)
        print(f"sync falhou: {type(exc).__name__}")
        if not args.no_notify:
            _notify(cfg, f"fin sync falhou ({type(exc).__name__}). Veja ~/Library/Logs/fin/sync.log")
        return 1
    finally:
        watchdog.cancel()
        ledger.close()


def _sync(args, cfg, tax, ledger) -> int:
    from fin.categorize import run_categorize
    from fin.llm import LlmFatal
    from fin.sync import run_sync

    caju_path = cfg.data_dir / cfg.caju_file
    if caju_path.exists():
        from fin.caju import import_caju

        import_caju(ledger, tax, caju_path)
    result = run_sync(ledger, _pluggy_client(), cfg, tax)
    log.info("sync %s: %s", result.status, {k: v for k, v in result.stats.items()})
    alerts = list(result.alerts)
    if not args.no_categorize:
        try:
            st = run_categorize(ledger, cfg, tax, _categorizer(cfg, ledger))
            log.info("categorize: %s", st)
        except LlmFatal as exc:
            alerts.append(f"IA: {exc}")
    if not args.no_publish and cfg.spreadsheet_id:
        from fin.sheets import make_publisher, publish

        try:
            st = publish(ledger, tax, make_publisher(cfg, secrets.get), date.today())
            log.info("publish: %s", st)
        except Exception as exc:  # noqa: BLE001 - Sheets failing must not lose the sync
            alerts.append(f"Sheets: falha ao publicar ({type(exc).__name__})")
    try:
        from fin.painel import export_painel

        if (out := export_painel(ledger, cfg.data_dir, date.today(), datetime.now().astimezone(), cfg)):
            log.info("painel: %s", out.name)
    except Exception as exc:  # noqa: BLE001 - the dashboard failing must not lose the sync
        alerts.append(f"Painel: falha ao gerar ({type(exc).__name__}: {exc})")
    pend = ledger.one("SELECT COUNT(*) n FROM transactions WHERE review_status='pendente' AND removed_upstream=0")["n"]
    if alerts and not args.no_notify:
        _notify(cfg, "fin sync:\n" + "\n".join(f"- {a}" for a in alerts) + (f"\n{pend} pendente(s) de revisão." if pend else ""))
    for a in alerts:
        print(f"alerta: {a}")
    print(f"sync {result.status}: {result.stats.get('inserted', 0)} novos, {result.stats.get('updated', 0)} atualizados, "
          f"{result.stats.get('relinked', 0)} religados, {pend} pendentes")
    return 0 if result.status != "error" else 1


def cmd_categorize(args) -> int:
    from fin.categorize import run_categorize

    cfg, tax, ledger = _open()
    print(run_categorize(ledger, cfg, tax, _categorizer(cfg, ledger)))
    return 0


def cmd_review(args) -> int:
    from fin.categorize import approve, ReviewError

    _cfg, tax, ledger = _open()
    if args.action == "list":
        rows = ledger.active_txs("review_status='pendente'")
        for r in rows:
            desc = r["counterpart_alias"] or r["desc_norm"] or r["description"]
            print(f"{local_id_str(r['local_id'])} {r['date']} {brl(r['amount_cents']):>13}  {desc[:38]:<38} "
                  f"sugestão={r['categoria'] or '-'} ({r['cat_source'] or '-'})")
        print(f"{len(rows)} pendente(s). Aprovar: fin review approve t000123 --categoria Alimentação [--essencial Essencial]")
        return 0
    if args.action == "approve":
        try:
            for raw in args.ids:
                res = approve(ledger, tax, parse_local_id(raw), categoria=args.categoria, natureza=args.natureza,
                              essencial=args.essencial, necessidade=args.necessidade, fonte=args.fonte)
                print(f"{local_id_str(res['local_id'])}: {res['categoria']} / {res['natureza']}"
                      + (f" (aprendido: {res['learned']})" if res["learned"] else ""))
        except (ReviewError, ValueError) as exc:
            print(f"erro: {exc}")
            return 2
        return 0
    if args.action == "apelido":
        alias, nickname = args.ids[0].upper(), " ".join(args.ids[1:])
        ledger.x("UPDATE counterparts SET apelido=? WHERE alias=?", (nickname or None, alias))
        print(f"{alias} = {nickname or '(sem apelido)'}")
        return 0
    return 2


def cmd_publish(args) -> int:
    from fin.sheets import CsvPublisher, make_publisher, publish

    cfg, tax, ledger = _open()
    pub = CsvPublisher(Path(args.csv)) if args.csv else make_publisher(cfg, secrets.get)
    print(publish(ledger, tax, pub, date.today()))
    return 0


def cmd_report(args) -> int:
    from fin.reports import aggregates_for_analyst, can_i_buy, format_bills, format_summary, monthly_summary, ym

    cfg, tax, ledger = _open()
    today = date.today()
    month = args.mes or ym(today)
    s = monthly_summary(ledger, cfg, tax, month, today)
    if args.faturas:
        print(format_bills(ledger, cfg, today))
    elif args.posso_comprar:
        print(can_i_buy(s, parse_brl(args.posso_comprar), args.parcelas))
    else:
        print(format_summary(s))
    if args.analista:
        cat = _categorizer(cfg, ledger)
        if cat is None:
            print("\n(analista: Anthropic não configurada)")
        else:
            print("\nAnalista (recebeu só agregados):\n" + (cat.analyze(aggregates_for_analyst(s)) or "(sem resposta)"))
    return 0


def cmd_painel(args) -> int:
    from fin.painel import export_painel

    cfg, _tax, ledger = _open()
    try:
        out = export_painel(ledger, cfg.data_dir, date.today(), datetime.now().astimezone(), cfg)
    finally:
        ledger.close()
    print(f"painel: {out}" if out else f"painel: sem {cfg.data_dir / 'plano.yaml'}, nada gerado")
    return 0


def cmd_bot(args) -> int:
    from fin.telegram_bot import Allowlist, Handlers, run_bot, TelegramAPI

    cfg = load_config()
    tax = load_taxonomy()
    try:
        token = secrets.require("telegram_bot_token")
        allow = Allowlist(int(secrets.require("telegram_allowed_chat_id")), int(secrets.require("telegram_allowed_user_id")))
    except secrets.SecretMissing as exc:
        # permanent: exit 0 so launchd (KeepAlive SuccessfulExit=false) does not relaunch every 30 s forever
        log.error("bot não iniciou: %s", exc)
        print(f"erro: {exc}")
        return 0

    def open_ledger() -> Ledger:
        cfg.ensure_data_dir()  # raises VaultUnmounted -> bot answers "cofre desmontado" and keeps running
        return Ledger(cfg.db_path)

    run_bot(TelegramAPI(token), allow, Handlers(open_ledger, cfg, tax))
    return 0


def cmd_caju(args) -> int:
    from fin.caju import import_caju

    cfg, tax, ledger = _open()
    path = Path(args.file).expanduser() if args.file else cfg.data_dir / cfg.caju_file
    print(import_caju(ledger, tax, path))
    return 0


def cmd_demo(args) -> int:
    from fin.demo import run_demo

    res = run_demo(Path(args.out))
    print(f"\nDemo ok. Pasta: {res['out']} (relatório em {res['report']}, abas em sheets/*.csv)")
    return 0


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def cmd_doctor(args) -> int:
    problems = 0

    def line(ok: bool | None, text: str) -> None:
        nonlocal problems
        mark = {True: "ok ", False: "!! ", None: "?? "}[ok]
        problems += ok is False
        print(f"  {mark} {text}")

    print("Ambiente")
    line(sys.version_info >= (3, 12), f"Python {platform.python_version()} em {sys.executable}")
    home = Path.home()
    protected = [home / d for d in ("Documents", "Desktop", "Downloads")]
    in_tcc = any(PROJECT_ROOT.is_relative_to(p) for p in protected)
    line(None if in_tcc else True, f"projeto em {PROJECT_ROOT}" + (
        " (pasta protegida pelo TCC: o launchd pode pedir permissão ou ser bloqueado; veja README)" if in_tcc else ""))
    icloud_docs = (home / "Library/Mobile Documents/com~apple~CloudDocs/Documents").exists()
    in_synced = any(PROJECT_ROOT.is_relative_to(home / d) for d in ("Documents", "Desktop"))
    if in_synced:
        line(False if icloud_docs else None,
             "iCloud 'Mesa e Documentos' " + ("LIGADO: config.yaml (seu nome, CNPJs, custos, itemIds) sobe para a Apple; "
                                              "mova o projeto (README passo 0)" if icloud_docs else
                                              "não detectado; confira em Ajustes > Apple ID > iCloud > iCloud Drive"))
    code, out = _run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
    line(code != 0, "login automático " + ("DESLIGADO" if code != 0
                                           else "LIGADO (Keychain abre sozinho no boot; desligue em Ajustes > Usuários e Grupos)"))

    print("Configuração")
    try:
        cfg = load_config()
        tax = load_taxonomy()
        line(True, f"config {cfg.source_path}; {len(cfg.items)} item(ns) Pluggy; {len(tax.categories)} categorias")
        placeholder = [i["bank"] for i in cfg.items if not i.get("item_id") or "COLE" in i.get("item_id", "")]
        line(not placeholder, "itemIds preenchidos" if not placeholder else f"itemIds faltando: {placeholder}")
        line(bool(cfg.spreadsheet_id) or None, "sheets.spreadsheet_id " + ("definido" if cfg.spreadsheet_id else "vazio (publicação desligada)"))
    except ConfigError as exc:
        line(False, str(exc))
        cfg = None

    print("Cofre")
    if cfg:
        mount = cfg.require_mount
        mounted = bool(mount) and os.path.ismount(mount)
        line(mounted if mount else False, f"volume {mount} " + ("montado" if mounted else "NÃO montado") if mount
             else "require_mount não definido: o ledger pode ser gravado fora do cofre")
        if mount:
            inside = cfg.data_dir.resolve().is_relative_to(Path(mount).resolve())
            line(inside, f"data_dir {cfg.data_dir} " + ("dentro do cofre" if inside else "FORA do cofre (confira FIN_DATA_DIR)"))
        if mounted:
            code, out = _run(["diskutil", "info", "-plist", str(mount)])
            enc = None
            if code == 0:
                info = plistlib.loads(out.encode())
                enc = info.get("Encryption") if "Encryption" in info else info.get("FileVault")
            line(bool(enc) if enc is not None else None, "volume criptografado" if enc else "criptografia não confirmada pelo diskutil")
            code, out = _run(["mdutil", "-s", str(mount)])
            line("disabled" in out.lower() or None, "Spotlight: " + (out.strip().splitlines()[-1].strip() if out.strip() else "?"))
            try:
                ledger = Ledger(cfg.db_path)
                last = ledger.last_run()
                n = ledger.one("SELECT COUNT(*) n FROM transactions")["n"]
                line(True, f"ledger {cfg.db_path}: {n} lançamentos; último sync: "
                     + (f"{last['run_date']} {last['status']}" if last else "nunca"))
                ledger.close()
            except Exception as exc:  # noqa: BLE001
                line(False, f"ledger: {type(exc).__name__}")

    print("Segredos (Keychain, só presença)")
    try:
        for name, ok in secrets.status().items():
            line(ok or None, secrets.NAMES[name])
    except Exception as exc:  # noqa: BLE001
        line(False, f"Keychain: {type(exc).__name__}")

    print("launchd")
    uid = os.getuid()
    for label in ("local.fin.sync", "local.fin.bot"):
        plist = home / "Library/LaunchAgents" / f"{label}.plist"
        code, out = _run(["launchctl", "print", f"gui/{uid}/{label}"])
        if code == 0:
            info = {k.strip(): v.strip() for k, _, v in (ln.partition("=") for ln in out.splitlines()) if _}
            last = info.get("last exit code", "?")
            pid = info.get("pid")
            ok = last in ("0", "(never exited)", "?") or (label == "local.fin.bot" and pid)
            line(bool(ok) or None if last != "124" else False,
                 f"{label}: carregado; último código de saída {last}" + (f"; rodando (pid {pid})" if pid else "")
                 + ("; 124 = sync passou do prazo (Keychain esperando 'Permitir'?)" if last == "124" else ""))
        else:
            line(None, f"{label}: " + ("plist existe, não carregado" if plist.exists() else "não instalado"))
    print("Se o launchd foi bloqueado: Ajustes > Privacidade e Segurança > Arquivos e Pastas (e Volumes Removíveis).")
    print("Nenhum problema bloqueante." if not problems else f"{problems} problema(s).")
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fin", description="Controle financeiro pessoal")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="grava segredos no Keychain / carrega dicionário")
    s.add_argument("what", choices=["pluggy", "anthropic", "google", "telegram", "telegram-discover", "dictionary", "status"])
    s.add_argument("--key-file")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("sync", help="Pluggy -> ledger -> regras -> categorização -> Sheets")
    s.add_argument("--no-categorize", action="store_true")
    s.add_argument("--no-publish", action="store_true")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--se-houver-novidade", action="store_true",
                   help="só sincroniza se algum banco atualizou no Pluggy depois do último sync")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("categorize", help="dicionário -> cache -> LLM para o que faltar")
    s.set_defaults(fn=cmd_categorize)

    s = sub.add_parser("review", help="lista/aprova pendentes")
    s.add_argument("action", choices=["list", "approve", "apelido"], nargs="?", default="list")
    s.add_argument("ids", nargs="*")
    s.add_argument("--categoria")
    s.add_argument("--natureza")
    s.add_argument("--essencial")
    s.add_argument("--necessidade")
    s.add_argument("--fonte")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("publish", help="publica no Google Sheets (ou --csv DIR)")
    s.add_argument("--csv")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("report", help="resumo do mês / faturas / posso comprar")
    s.add_argument("--mes")
    s.add_argument("--faturas", action="store_true")
    s.add_argument("--posso-comprar")
    s.add_argument("--parcelas", type=int, default=1)
    s.add_argument("--analista", action="store_true", help="texto opcional da IA (só agregados)")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("bot", help="bot do Telegram (long polling)")
    s.set_defaults(fn=cmd_bot)

    s = sub.add_parser("caju", help="importa o CSV/YAML do Caju")
    s.add_argument("--file")
    s.set_defaults(fn=cmd_caju)

    s = sub.add_parser("painel", help="gera painel.json (só agregados) a partir do plano.yaml")
    s.set_defaults(fn=cmd_painel)

    s = sub.add_parser("doctor", help="checa ambiente, cofre, segredos e launchd (sem rede)")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("demo", help="fluxo completo offline com dados sintéticos")
    s.add_argument("--out", default=str(PROJECT_ROOT / "demo-out"))
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.fn(args) or 0)
    except (ConfigError, VaultUnmounted, secrets.SecretMissing) as exc:
        print(f"erro: {exc}")
        return 2
    except Exception as exc:
        if args.verbose:
            raise
        # no traceback in the logs (outside the vault): the message may carry a description or a value
        log.error("erro inesperado em %s: %s", args.cmd, type(exc).__name__)
        print(f"erro inesperado: {type(exc).__name__} (rode com -v para detalhes)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
