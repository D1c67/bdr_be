"""Isolated development browser v2 helpers (Phase 6 helpers moved to v2, WI-20).

Credential setup: scripts/start_phase6_credential_setup.sh (no pollers, jobs
or tunnel). Live acceptance: scripts/start_phase6_acceptance.sh (the custom
lifespan in scripts/phase6_acceptance_app.py, two uvicorn workers, a tunnel
for the personal worker). Both turn on ASSISTANT_BROWSER_V2_ENABLED and
ASSISTANT_HOSTED_SEARCH_ENABLED for these processes only; the hold signing
key must already be in bdr_be/.env (docs/BROWSER_V2_RELEASE_RUNBOOK.md).
Keep the terminal open. The normal backend/frontend and every .env file
remain unchanged.
"""
from __future__ import annotations

import base64
import argparse
import errno
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from app.core.config import Settings  # noqa: E402

FRONTEND = ROOT.parent / 'bdr_fe'
FIREWALL_SCRIPT = ROOT / 'agent_worker' / 'install' / 'apply_user_pool_firewall.ps1'
SESSION_ROOT = ROOT / 'dist' / 'phase6-sessions'
DEV_REF = 'bpidntbyvoooqvaispup'
DEV_URL = f'https://{DEV_REF}.supabase.co'
BACKEND_PORT = 5071
FRONTEND_PORT = 4510
ACCEPTANCE_OWNER = '5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3'
# Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 18.4): the personal worker is
# found, not pinned, because the 1.1.0 rollout re-enrolls it (-Reenroll makes
# a new worker row). Two uvicorn workers, as in production.
V2_TYPE = 'user_browser'
RETIRED_TYPE = 'user_browser_read'
ACCEPTANCE_UVICORN_WORKERS = 2
ACTIVE_JOB_STATUSES = ('queued', 'running', 'waiting_help', 'applying')
PERSONAL_PYTHON = r'C:\Program Files\BDR Python 3.12.10\tools\python.exe'
V2_FLAGS = {'ASSISTANT_BROWSER_V2_ENABLED': 'true', 'ASSISTANT_HOSTED_SEARCH_ENABLED': 'true'}
# Off in OFF_FLAGS for credential setup, on for the acceptance window only:
# acceptance step 3 checks the owner's Teams alert for a held final action,
# and step 4 lets the email code reader read the owner's own mailbox
# (assistant_ms.configured() needs ASSISTANT_OUTLOOK_ENABLED). This custom
# lifespan starts no Outlook or Teams poller.
ACCEPTANCE_ON_FLAGS = ('TEAMS_ALERTS_ENABLED', 'ASSISTANT_OUTLOOK_ENABLED')
OFF_FLAGS = (
    'RFQ_POLLING_ENABLED', 'DUE_REMINDERS_ENABLED', 'DUE_DIGEST_ENABLED',
    'AUTO_SEND_POLLER_ENABLED', 'CALL_IN_ENABLED', 'EMAIL_INGEST_ENABLED',
    'EMAIL_CLASSIFY_ENABLED', 'RFP_INGESTION_ENABLED', 'RFP_TESTING_ENABLED',
    'RFP_NGEM_ENABLED', 'RFP_BC_ENABLED', 'BUILDING_CONNECTED_ENABLED',
    'RFP_HARVEST_ENABLED', 'RFP_SPLIT_ENABLED', 'RFP_CREATE_AUTO_ENABLED',
    'RFP_MATCH_AUTO_MERGE_ENABLED', 'PM_RFI_SYNC_ENABLED', 'PM_RFI_EMAIL_SCAN_ENABLED',
    'LLM_HEALTH_ENABLED', 'NOTIFICATION_EMAILS_ENABLED', 'TEAMS_ALERTS_ENABLED',
    'ASSISTANT_SCHEDULER_ENABLED', 'ASSISTANT_MAINTENANCE_ENABLED',
    'ASSISTANT_CALENDAR_ENABLED', 'ASSISTANT_OUTLOOK_ENABLED', 'ASSISTANT_WEB_ENABLED',
    'LLM_QUEUE_ENABLED', 'BID_FILE_SPLITTER_ENABLED', 'PLANS_READER_ENABLED',
    'AI_PRICING_ENABLED',
)


class SetupError(ValueError):
    pass


def require_dev(url):
    if not isinstance(url, str) or url.rstrip('/') != DEV_URL:
        raise SetupError('This helper only supports the exact BDR development Supabase project.')


def frontend_values(source: Path, environment: dict) -> dict:
    values = {}
    for name in ('.env', '.env.development', '.env.local', '.env.development.local'):
        path = source / name
        if path.is_file():
            values.update(dotenv_values(path))
    for name in ('NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_KEY'):
        if name in environment:
            values[name] = environment[name]
    require_dev(values.get('NEXT_PUBLIC_SUPABASE_URL'))
    key = values.get('NEXT_PUBLIC_SUPABASE_ANON_KEY') or ''
    if key.startswith('sb_publishable_'):
        return {'NEXT_PUBLIC_SUPABASE_URL': DEV_URL, 'NEXT_PUBLIC_SUPABASE_ANON_KEY': key}
    try:
        parts = key.split('.')
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        if len(parts) != 3 or payload.get('ref') != DEV_REF or payload.get('role') != 'anon':
            raise ValueError
    except (IndexError, ValueError, TypeError):
        raise SetupError('The frontend needs a public anon key for the same development project.') from None
    return {'NEXT_PUBLIC_SUPABASE_URL': DEV_URL, 'NEXT_PUBLIC_SUPABASE_ANON_KEY': key}


def process_environments(source: Path, environment: dict, *, settings_type=Settings) -> tuple[dict, dict]:
    overrides = {name: 'false' for name in OFF_FLAGS}
    overrides.update(ASSISTANT_ENABLED='true', ASSISTANT_BROWSER_ENABLED='true',
        BROWSER_AGENTS_ENABLED='true', ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED='true',
        ASSISTANT_TOKEN_SECRET=secrets.token_urlsafe(48), **V2_FLAGS,
        LLM_QUEUE_CLAIM_TYPES='assistant_turn,assistant_approval_execute',
        FRONTEND_URL='http://localhost:4510',
        CORS_ORIGINS='http://localhost:4510,http://127.0.0.1:4510')
    # Constructor values take precedence over .env; run the normal full validator,
    # including separate vault keys, without altering the parent environment.
    validation = {key.lower(): value for key, value in overrides.items()}
    validation['rfp_ingest_enabled'] = False
    try:
        configured = settings_type(_env_file=ROOT / '.env', **validation)
    except ValidationError as exc:
        # Messages only: a model-level error's input would carry every
        # setting, secrets included, so the input is never printed.
        reasons = '; '.join(str(error.get('msg', '')) for error in exc.errors(include_input=False))
        raise SetupError(f'Development configuration refused: {reasons} '
                         '(see docs/BROWSER_V2_RELEASE_RUNBOOK.md).') from None
    require_dev(configured.supabase_url)
    public = frontend_values(source, environment)
    backend = {**environment, **overrides}
    frontend = {**environment, **public, 'NODE_ENV': 'development',
                'NEXT_PUBLIC_API_BASE_URL': 'http://localhost:5071'}
    return backend, frontend


def require_free_ports() -> None:
    for port in (BACKEND_PORT, FRONTEND_PORT):
        # Match the actual IPv4 server listeners. Like uvicorn/Node, permit
        # rebinding after shutdown while old connections remain in TIME_WAIT.
        # Do not use SO_REUSEPORT: an existing listener must still be refused.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
                sock.listen(1)
        except OSError as exc:
            reason = 'already in use' if exc.errno == errno.EADDRINUSE else (
                'not permitted' if exc.errno in {errno.EACCES, errno.EPERM} else 'unavailable')
            code = errno.errorcode.get(exc.errno, 'unknown socket error')
            raise SetupError(f'127.0.0.1:{port} is {reason} ({code}). No existing process was stopped.') from None


def create_workspace() -> Path:
    # Long-running acceptance sessions must survive OS temporary-file cleanup.
    if SESSION_ROOT.is_symlink():
        raise SetupError('The private acceptance session directory must not be a symlink.')
    SESSION_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    SESSION_ROOT.chmod(0o700)
    workspace = Path(tempfile.mkdtemp(prefix='bdr-phase6-credential-', dir=SESSION_ROOT))
    workspace.chmod(0o700)
    return workspace


def scratch_frontend(source: Path, workspace: Path) -> Path:
    if not (source / 'node_modules/.bin/next').exists():
        raise SetupError('Install the existing frontend dependencies before starting credential setup.')
    destination = workspace / 'frontend'
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy,
        ignore=shutil.ignore_patterns('.git', '.next', 'node_modules', '.env*', '.turbo', 'coverage', '*.tsbuildinfo'))
    (destination / 'node_modules').symlink_to((source / 'node_modules').resolve(), target_is_directory=True)
    for path in source.glob('.env*'):
        if path.is_file():
            (destination / path.name).symlink_to(path.resolve())
    return destination


def stop_owned(process, *, grace=10) -> None:
    # Next may leave descendants after its parent exits. Every child was
    # launched in its own session, so target that owned group, not a port.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def wait_ready(client, processes, url: str, *, health: bool, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise SetupError('A setup server stopped early. Check the protected local logs.')
        try:
            response = client.get(url)
            if response.status_code == 200 and (not health or response.json() == {'status': 'ok'}):
                return
        except (httpx.TransportError, ValueError):
            pass
        time.sleep(0.5)
    raise SetupError('A setup server did not become ready. Check the protected local logs.')


def acceptance_environment(backend: dict, worker_id: str | None = None) -> dict:
    """Opt-in, process-only execution; the normal application's lifespan stays off."""
    env = {**backend, 'PHASE6_ACCEPTANCE': 'true',
           'PHASE6_ACCEPTANCE_OWNER': ACCEPTANCE_OWNER,
           'PHASE6_ACCEPTANCE_RUN': secrets.token_hex(16),
           'LLM_QUEUE_ENABLED': 'true', 'ASSISTANT_CONCURRENCY': '1',
           'LLM_QUEUE_CLAIM_TYPES': 'assistant_turn,assistant_approval_execute,assistant_continue',
           **V2_FLAGS, **{name: 'true' for name in ACCEPTANCE_ON_FLAGS}}
    env.pop('PHASE6_ACCEPTANCE_WORKER', None)
    if worker_id is not None:
        env['PHASE6_ACCEPTANCE_WORKER'] = valid_worker_id(worker_id)
    return env


def valid_worker_id(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise SetupError('The personal worker id is not a UUID.') from None


def personal_workers(sb) -> list[dict]:
    """Every personal (user pool) worker row that is not revoked."""
    return sb.table('browser_agent_workers').select('id,status,version,allowed_types').eq(
        'principal_kind', 'user').neq('status', 'revoked').execute().data or []


def personal_worker_id(sb=None) -> str:
    """The one approved personal worker, for the reconnect block. Read only."""
    if sb is None:
        from app.core.supabase_client import get_supabase
        sb = get_supabase()
    workers = personal_workers(sb)
    if len(workers) != 1 or workers[0].get('status') != 'active':
        raise SetupError('Exactly one approved personal worker is needed to reconnect it. Revoke extra rows and approve the re-enrolled one first.')
    return valid_worker_id(workers[0]['id'])


def acceptance_preflight() -> str | None:
    """Read-only checks before the browser v2 acceptance server starts.

    Returns the current personal worker id (None before the 1.1.0
    re-enrollment has an approved row). The worker version is not required
    here: the worker reaches BDR only through this server's tunnel, so it
    upgrades to 1.1.0 after the server is up (runbook part D). Every browser
    type must stay disabled until the owner enables user_browser for the
    window; no assistant queue job and no personal browser job may be open.
    """
    from app.core.supabase_client import get_supabase
    from app.core.config import get_settings
    require_dev(get_settings().supabase_url)
    sb = get_supabase()
    workers = personal_workers(sb)
    if len(workers) > 1:
        raise SetupError('More than one personal worker row is not revoked. Revoke the old row before acceptance.')
    if workers and any(t not in (V2_TYPE, RETIRED_TYPE) for t in workers[0].get('allowed_types') or []):
        raise SetupError('The personal worker allows an unexpected agent type.')
    kinds = sb.table('browser_agent_types').select('key,enabled').execute().data or []
    if not any(row['key'] == V2_TYPE for row in kinds):
        raise SetupError('The user_browser type row is missing. Apply 0192 on development first.')
    if any(row['enabled'] for row in kinds):
        raise SetupError('All browser types must remain disabled while starting the test server.')
    jobs = sb.table('llm_jobs').select('id').in_('job_type', ['assistant_turn', 'assistant_approval_execute', 'assistant_continue']).in_('status', ['queued', 'running']).limit(1).execute().data or []
    if jobs:
        raise SetupError('Existing assistant jobs need review before starting this isolated test.')
    browser_jobs = sb.table('browser_agent_jobs').select('id').in_('agent_type', [V2_TYPE, RETIRED_TYPE]).in_(
        'status', list(ACTIVE_JOB_STATUSES)).limit(1).execute().data or []
    if browser_jobs:
        raise SetupError('A personal browser job is still open. Cancel it before selecting a new worker release.')
    if workers and workers[0].get('status') == 'active':
        return valid_worker_id(workers[0]['id'])
    return None


def connect_acceptance_tunnel(client, children: list, backend_env: dict) -> None:
    import prepare_phase6_windows_install as preparation
    process, api = preparation.start_tunnel(origin_port=BACKEND_PORT)
    children.append(process)
    print(f'Isolated test tunnel: {api}', flush=True)
    deadline = time.monotonic() + 180
    last_report = 0
    template = (ROOT / 'scripts/phase6_reconnect_personal_worker.ps1').read_bytes()
    while True:
        if any(child.poll() is not None for child in children):
            raise SetupError('A test server or its tunnel stopped during startup.')
        try:
            data = preparation.fetch(client, api + '/phase6-acceptance/ready', maximum=10000, timeout=20)
            if json.loads(data) != {'run': backend_env['PHASE6_ACCEPTANCE_RUN'], 'ready': True}:
                raise SetupError('The tunnel did not reach this isolated test server.')
            served = preparation.fetch(client, api + '/phase6-acceptance/reconnect.ps1', maximum=100000, timeout=20)
            if served != template:
                raise SetupError('The reconnect endpoint does not serve the checked personal-worker script.')
            if backend_env.get('PHASE6_ACCEPTANCE') == 'true':
                firewall = FIREWALL_SCRIPT.read_bytes()
                served = preparation.fetch(client, api + '/phase6-acceptance/firewall.ps1', maximum=1_000_000, timeout=20)
                if served != firewall:
                    raise SetupError('The firewall endpoint does not serve the checked user pool firewall script.')
            break
        except preparation.DownloadUnavailable as exc:
            now = time.monotonic()
            # Cloudflare can return 530 while the freshly registered tunnel's
            # origin routing is unavailable. Retry only in this bounded tunnel
            # startup check; never treat a failed verification as ready.
            if not (exc.retryable or exc.status == 530) or now >= deadline:
                raise SetupError(f'Test tunnel verification failed: {exc}') from None
            if now - last_report >= 15:
                print(f'Waiting for test tunnel: {exc}', flush=True)
                last_report = now
            time.sleep(1)
    worker = backend_env.get('PHASE6_ACCEPTANCE_WORKER')
    if worker:
        print('Run this block in Windows PowerShell as Administrator to reconnect only the personal worker:', flush=True)
        print(reconnect_commands(api, template, worker), flush=True)
    else:
        print('No approved personal worker yet: reconnecting is not needed before the re-enrollment below.', flush=True)
    if backend_env.get('PHASE6_ACCEPTANCE') == 'true':
        print('Release 1.1.0 rollout only (runbook part D): after selecting 1.1.0, revoking the old personal worker '
              'row and adding a Personal tasks worker for a new code, run this block in Windows PowerShell as '
              'Administrator. It asks for the fingerprint and the code; never paste the code into chat.', flush=True)
        print(reenroll_commands(api), flush=True)
        print('Then, once worker.log shows release 1.1.0 running (runbook part E), apply the user pool firewall '
              'rules in Windows PowerShell as Administrator:', flush=True)
        print(firewall_commands(api, FIREWALL_SCRIPT.read_bytes()), flush=True)


def reconnect_commands(api: str, template: bytes, worker_id: str) -> str:
    from prepare_phase6_windows_install import TUNNEL_URL
    if TUNNEL_URL.fullmatch(api) is None:
        raise SetupError('Invalid isolated test tunnel URL.')
    worker = valid_worker_id(worker_id)
    digest = hashlib.sha256(template).hexdigest()
    return ("& {\n$ErrorActionPreference = 'Stop'\n"
            f"$api = '{api}'\n"
            '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n'
            'Invoke-WebRequest -UseBasicParsing "$api/phase6-acceptance/reconnect.ps1" -OutFile "$env:TEMP\\bdr-personal-reconnect.ps1"\n'
            f'if ((Get-FileHash "$env:TEMP\\bdr-personal-reconnect.ps1" -Algorithm SHA256).Hash -ne \'{digest}\') {{ throw "Reconnect script hash mismatch" }}\n'
            f'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-personal-reconnect.ps1" -ApiBase $api -ExpectedWorkerId \'{worker}\'\n'
            'if ($LASTEXITCODE -ne 0) { throw "Personal worker reconnect failed" }\n}')


def firewall_commands(api: str, script: bytes) -> str:
    """Download agent_worker/install/apply_user_pool_firewall.ps1 through the
    tunnel, check its SHA-256 against the Mac copy, run it elevated."""
    from prepare_phase6_windows_install import TUNNEL_URL
    if TUNNEL_URL.fullmatch(api) is None:
        raise SetupError('Invalid isolated test tunnel URL.')
    digest = hashlib.sha256(script).hexdigest()
    return ("& {\n$ErrorActionPreference = 'Stop'\n"
            f"$api = '{api}'\n"
            '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n'
            'Invoke-WebRequest -UseBasicParsing "$api/phase6-acceptance/firewall.ps1" -OutFile "$env:TEMP\\bdr-user-firewall.ps1"\n'
            f'if ((Get-FileHash "$env:TEMP\\bdr-user-firewall.ps1" -Algorithm SHA256).Hash -ne \'{digest}\') {{ throw "Firewall script hash mismatch" }}\n'
            'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-user-firewall.ps1"\n'
            'if ($LASTEXITCODE -ne 0) { throw "User pool firewall script failed" }\n}')


def reenroll_commands(api: str) -> str:
    """The 1.1.0 rollout re-enrollment of the personal service: the installer
    replaces the launcher (which now syncs the stable runtime before each
    start), the release key and the token. It asks for the fingerprint and
    the enrollment code itself; the code never appears on a command line."""
    from prepare_phase6_windows_install import TUNNEL_URL
    if TUNNEL_URL.fullmatch(api) is None:
        raise SetupError('Invalid isolated test tunnel URL.')
    return ("& {\n$ErrorActionPreference = 'Stop'\n"
            f"$api = '{api}'\n"
            '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n'
            'Invoke-WebRequest -UseBasicParsing "$api/agent-worker/v1/installer.ps1" -OutFile "$env:TEMP\\bdr-personal-install.ps1"\n'
            'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-personal-install.ps1" -Pool user -Reenroll '
            f"-PythonPath '{PERSONAL_PYTHON}' -ApiBase $api\n"
            'if ($LASTEXITCODE -ne 0) { throw "BDR personal re-enrollment failed" }\n}')


def main(*, controlled_read=False) -> int:
    children, logs, workspace = [], [], None
    try:
        backend_env, frontend_env = process_environments(FRONTEND, dict(os.environ))
        if controlled_read:
            backend_env = acceptance_environment(backend_env, acceptance_preflight())
        require_free_ports()
        workspace = create_workspace()
        scratch = scratch_frontend(FRONTEND, workspace)
        for name in ('backend.log', 'frontend.log'):
            descriptor = os.open(workspace / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            logs.append(os.fdopen(descriptor, 'w'))
        print(f'Protected setup logs: {workspace}', flush=True)
        backend_command = [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                           '--port', str(BACKEND_PORT), '--lifespan', 'off']
        if controlled_read:
            backend_command[3] = 'scripts.phase6_acceptance_app:app'
            backend_command[-1] = 'on'
            backend_command[-2:-2] = ['--workers', str(ACCEPTANCE_UVICORN_WORKERS)]
        print('Starting the isolated credential API on localhost:5071; waiting for health...', flush=True)
        children.append(subprocess.Popen(backend_command, cwd=ROOT, env=backend_env,
            stdout=logs[0], stderr=subprocess.STDOUT, start_new_session=True))
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            wait_ready(client, children, 'http://127.0.0.1:5071/health', health=True)
            frontend_command = [str(scratch / 'node_modules/.bin/next'), 'dev', '--webpack',
                                '--hostname', '127.0.0.1', '--port', str(FRONTEND_PORT)]
            print('Starting the scratch frontend on localhost:4510; the first page may take up to two minutes to compile...', flush=True)
            children.append(subprocess.Popen(frontend_command, cwd=scratch, env=frontend_env,
                stdout=logs[1], stderr=subprocess.STDOUT, start_new_session=True))
            wait_ready(client, children, 'http://127.0.0.1:4510/login', health=False)
            if controlled_read:
                connect_acceptance_tunnel(client, children, backend_env)
        if controlled_read:
            print('Browser v2 acceptance server ready. The Personal browser type stays disabled until worker 1.1.0, '
                  'the firewall self-test and the model probe are verified (docs/BROWSER_V2_RELEASE_RUNBOOK.md).', flush=True)
            print('Keep this terminal open. Use a normal BDR assistant conversation; site, sign-in, code and '
                  'final-action cards appear in the chat.', flush=True)
        else:
            print('Credential setup only. This helper does not start queue workers, pollers, model calls, or browser jobs.', flush=True)
        print('Open http://localhost:4510/settings/assistant in your normal browser. Sign in normally if needed.', flush=True)
        if not controlled_read:
            print('Save personal site logins under Browser sites and sign-ins. Never enter a password or code in chat.', flush=True)
        print('Keep this terminal open. Ctrl+C stops only these setup servers; existing services and .env files are unchanged.', flush=True)
        while all(process.poll() is None for process in children):
            time.sleep(1)
        raise SetupError('A setup server stopped. Check the protected local logs.')
    except KeyboardInterrupt:
        return 0
    except SetupError as exc:
        print(f'Credential setup stopped: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Credential setup failed ({type(exc).__name__}). Check development configuration and protected local logs; no .env flags were changed.', file=sys.stderr)
        return 1
    finally:
        for process in reversed(children):
            if controlled_read:
                stop_owned(process, grace=40)
            else:
                stop_owned(process)
        for file in logs:
            file.close()
        if workspace:
            # Keep protected logs for diagnosis; remove only this process's
            # generated frontend and its symlinks, never the symlink targets.
            shutil.rmtree(workspace / 'frontend', ignore_errors=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controlled-read', action='store_true')
    raise SystemExit(main(controlled_read=parser.parse_args().controlled_read))
