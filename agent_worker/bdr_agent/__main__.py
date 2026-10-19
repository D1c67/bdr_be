"""Command line entry point.

    python -m bdr_agent enroll --api https://<bdr-api> --code <20 chars> [--fingerprint "<hex>"]
    python -m bdr_agent run
    python -m bdr_agent fingerprint

enroll: shows the release public key fingerprint and requires it to match the
copy saved at keygen (`--fingerprint`, or typing `yes` at the prompt), then
calls /enroll with the one-time code, stores the token (DPAPI on Windows) and
writes worker.json. Run it as the service user so the service can decrypt the
token. The worker then waits for IT Admin approval in Settings.

run: the service process. Exit codes: 0 stopped, 2 local setup problem,
3 stale_boot (restart), 4 revoked/unauthorized (re-enroll), 5 a signed update
was staged (the launcher starts it). Under the launcher it writes the local
health marker once worker.json and the token are loaded and Chromium is up.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import platform
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .client import BdrApiError, BdrClient, BdrUnreachable, RetryPolicy, SyncBdrClient
from .local_config import (
    LocalConfig,
    LocalConfigError,
    Paths,
    default_home,
    load_local_config,
    normalize_base_url,
    save_local_config,
)
from .protocol import ENROLL_CODE_RE, EnrollRequest
from .redact import LiveSecrets
from .release_key import ReleaseKeyError, fingerprint, fingerprint_matches, load_public_key
from .tokenstore import TokenStore, TokenStoreError
from .updater import ENV_BAD_VERSIONS, Updater, parse_bad_versions, write_health_marker

EXIT_SETUP = 2
log = logging.getLogger("bdr_agent")


class _ScrubFilter(logging.Filter):
    """Last line of defence: scrub live secrets from every log record."""

    def __init__(self, secrets: LiveSecrets):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if len(self.secrets):
            record.msg = self.secrets.scrub_text(record.getMessage())
            record.args = ()
        return True


def setup_logging(paths: Paths | None, secrets: LiveSecrets, *, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if paths is not None:
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            paths.logs_dir / "worker.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    scrub = _ScrubFilter(secrets)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(scrub)
        root.addHandler(h)
    # httpx logs full request URLs at INFO (signed release URLs carry a token
    # query): keep the HTTP stack quiet whatever the root level is.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.home) if args.home else default_home())


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def os_info() -> dict[str, Any]:
    return {"system": platform.system(), "release": platform.release(), "build": platform.version()[:60],
            "machine": platform.machine(), "python": platform.python_version()}


def _confirm_fingerprint(args: argparse.Namespace, paths: Paths) -> bool:
    key_path = args.release_key or paths.release_pubkey
    try:
        key = load_public_key(key_path)
    except ReleaseKeyError as exc:
        print(f"Release key problem: {exc}", file=sys.stderr)
        return False
    fp = fingerprint(key)
    print(f"Release public key fingerprint:\n  {fp}")
    if args.fingerprint:
        if not fingerprint_matches(key, args.fingerprint):
            print("The fingerprint does NOT match the expected value. Stop and check the installer.",
                  file=sys.stderr)
            return False
        print("Fingerprint matches.")
        return True
    if args.yes:
        print("Refusing --yes without --fingerprint: the fingerprint must be checked.", file=sys.stderr)
        return False
    answer = input("Compare it with the fingerprint saved at keygen. Type yes if it matches: ")
    return answer.strip().lower() == "yes"


def cmd_fingerprint(args: argparse.Namespace) -> int:
    paths = _paths(args)
    try:
        key = load_public_key(args.release_key or paths.release_pubkey)
    except ReleaseKeyError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP
    print(fingerprint(key))
    return 0


def cmd_enroll(args: argparse.Namespace, *, transport: Any = None) -> int:
    paths = _paths(args)
    paths.ensure()
    try:
        base = normalize_base_url(args.api)
    except LocalConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP
    code = normalize_code(args.code)
    if not ENROLL_CODE_RE.match(code):
        print("The enrollment code must be 20 characters (A-Z, 2-7).", file=sys.stderr)
        return EXIT_SETUP
    store = TokenStore(paths.token_file)
    try:
        existing = store.load()
    except TokenStoreError:
        existing = None
    if existing and not args.force:
        print("This PC is already enrolled. Use --force to replace the token.", file=sys.stderr)
        return EXIT_SETUP
    if not _confirm_fingerprint(args, paths):
        return EXIT_SETUP

    client = SyncBdrClient(base, version=__version__, transport=transport,
                           retry=RetryPolicy(max_attempts=3, cap_s=5.0))
    try:
        resp = client.enroll(EnrollRequest(code=code, hostname=(args.hostname or socket.gethostname())[:255],
                                           os_info=os_info(), version=__version__))
    except BdrApiError as exc:
        print(f"Enrollment refused ({exc.code or exc.status}). Ask IT Admin for a new code.", file=sys.stderr)
        return EXIT_SETUP
    except BdrUnreachable as exc:
        print(f"Could not reach BDR: {exc.last}", file=sys.stderr)
        return EXIT_SETUP
    finally:
        client.close()
    # Token first: if power drops before worker.json is written, `enroll --force`
    # with a fresh code repairs it; the reverse order would leave a worker id
    # with no way to authenticate.
    store.save(resp.token)
    save_local_config(paths, LocalConfig(api_base_url=base, worker_id=resp.worker_id))
    print(f"Enrolled as worker {resp.worker_id}. Status: {resp.status}. "
          "Ask IT Admin to approve it in Settings.")
    return 0


def load_runners() -> tuple[dict[str, Any], Any]:
    """Agent types come from bdr_agent.agents.REGISTRY (W3/W4). The module may
    also expose `browser_provider(paths)` returning the shared Chromium (W2)."""
    try:
        from . import agents  # type: ignore[attr-defined]
    except ImportError:
        log.warning("no agent types are installed (bdr_agent.agents missing); the worker will not claim")
        return {}, None
    registry = dict(getattr(agents, "REGISTRY", {}) or {})
    factory = getattr(agents, "browser_provider", None)
    return registry, factory


async def _run_async(paths: Paths, cfg: LocalConfig, token: str, secrets: LiveSecrets) -> int:
    from .supervisor import Supervisor

    runners, provider_factory = load_runners()
    browser = provider_factory(paths) if provider_factory else None
    if browser is not None and hasattr(browser, "start"):
        await browser.start()
    # Local health for the launcher (spec 8): config and token loaded, Chromium
    # up. Deliberately before any BDR contact, so an outage never looks unhealthy.
    write_health_marker()
    client = BdrClient(cfg.api_base_url, token=token, version=__version__)
    hb_client = SyncBdrClient(cfg.api_base_url, token=token, version=__version__)
    sup = Supervisor(client=client, hb_client=hb_client, paths=paths, runners=runners, version=__version__,
                     secrets=secrets, browser=browser, api_host=cfg.api_host,
                     chromium_version=getattr(browser, "chromium_version", None),
                     bad_versions=parse_bad_versions(os.environ.get(ENV_BAD_VERSIONS)),
                     updater=Updater.from_env(paths, __version__))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, sup.request_shutdown)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows: no loop signal handlers; plain handlers run on the main
            # thread and hand off thread-safely.
            signal.signal(sig, lambda *_: sup.request_shutdown())
    try:
        return await sup.run()
    finally:
        if browser is not None and hasattr(browser, "stop"):
            try:
                await browser.stop()
            except Exception:
                log.exception("browser stop failed")
        await client.aclose()
        hb_client.close()


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    secrets = LiveSecrets()
    setup_logging(paths, secrets, verbose=args.verbose)
    try:
        cfg = load_local_config(paths)
    except (LocalConfigError, ValueError) as exc:
        log.error("worker.json is invalid: %s", exc)
        return EXIT_SETUP
    if cfg is None:
        log.error("not enrolled: run `python -m bdr_agent enroll` first")
        return EXIT_SETUP
    try:
        token = TokenStore(paths.token_file).load()
    except TokenStoreError as exc:
        log.error("worker token unreadable: %s", exc)
        return EXIT_SETUP
    if not token:
        log.error("no worker token: re-enroll")
        return EXIT_SETUP
    secrets.add(token)
    return asyncio.run(_run_async(paths, cfg, token, secrets))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bdr_agent", description="BDR browser agent worker")
    p.add_argument("--home", help="data directory (default: C:\\ProgramData\\BDRAgent on Windows)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="enroll this PC with a one-time code from IT Admin")
    e.add_argument("--api", required=True, help="BDR API base URL, https://...")
    e.add_argument("--code", required=True, help="20-character enrollment code")
    e.add_argument("--fingerprint", help="expected release key fingerprint (from the password manager)")
    e.add_argument("--release-key", help="path to the release public key (default: <home>/release_key.pub)")
    e.add_argument("--hostname", help="override the reported hostname")
    e.add_argument("--force", action="store_true", help="replace an existing token")
    e.add_argument("--yes", action="store_true", help="non-interactive (requires --fingerprint)")

    r = sub.add_parser("run", help="run the worker service")
    r.add_argument("--verbose", action="store_true")

    f = sub.add_parser("fingerprint", help="print the release public key fingerprint")
    f.add_argument("--release-key")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "enroll":
        return cmd_enroll(args)
    if args.cmd == "fingerprint":
        return cmd_fingerprint(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
