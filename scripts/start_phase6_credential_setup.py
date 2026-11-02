"""Isolated development credential UI only. No pollers, jobs, or tunnel.

Run scripts/start_phase6_credential_setup.sh; keep its terminal open.
The normal backend/frontend and every .env file remain unchanged.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from app.core.config import Settings  # noqa: E402

FRONTEND = ROOT.parent / 'bdr_fe'
SESSION_ROOT = ROOT / 'dist' / 'phase6-sessions'
DEV_REF = 'bpidntbyvoooqvaispup'
DEV_URL = f'https://{DEV_REF}.supabase.co'
BACKEND_PORT = 5071
FRONTEND_PORT = 4510
ACCEPTANCE_WORKER = 'e0846bf6-ea57-4927-9d12-de514532a6e3'
ACCEPTANCE_OWNER = '5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3'
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
        ASSISTANT_TOKEN_SECRET=secrets.token_urlsafe(48),
        LLM_QUEUE_CLAIM_TYPES='assistant_turn,assistant_approval_execute',
        FRONTEND_URL='http://localhost:4510',
        CORS_ORIGINS='http://localhost:4510,http://127.0.0.1:4510')
    # Constructor values take precedence over .env; run the normal full validator,
    # including separate vault keys, without altering the parent environment.
    validation = {key.lower(): value for key, value in overrides.items()}
    validation['rfp_ingest_enabled'] = False
    configured = settings_type(_env_file=ROOT / '.env', **validation)
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


def acceptance_environment(backend: dict) -> dict:
    """Opt-in, process-only execution; the normal application's lifespan stays off."""
    return {**backend, 'PHASE6_ACCEPTANCE': 'true',
            'PHASE6_ACCEPTANCE_OWNER': ACCEPTANCE_OWNER,
            'PHASE6_ACCEPTANCE_WORKER': ACCEPTANCE_WORKER,
            'PHASE6_ACCEPTANCE_RUN': secrets.token_hex(16),
            'LLM_QUEUE_ENABLED': 'true', 'ASSISTANT_CONCURRENCY': '1',
            'LLM_QUEUE_CLAIM_TYPES': 'assistant_turn,assistant_approval_execute,assistant_continue'}


def acceptance_preflight() -> None:
    from app.core.supabase_client import get_supabase
    from app.core.config import get_settings
    require_dev(get_settings().supabase_url)
    sb = get_supabase()
    workers = sb.table('browser_agent_workers').select('id,status,version,allowed_types').eq('principal_kind', 'user').neq('status', 'revoked').execute().data or []
    if (len(workers) != 1 or workers[0]['id'] != ACCEPTANCE_WORKER
            or workers[0]['status'] != 'active' or workers[0]['version'] != '1.0.6'
            or workers[0]['allowed_types'] != ['user_browser_read']):
        raise SetupError('The designated personal worker must be active on hub-RFIs fix release 1.0.6 before acceptance.')
    kinds = sb.table('browser_agent_types').select('key,enabled').execute().data or []
    if not any(row['key'] == 'user_browser_read' for row in kinds) or any(row['enabled'] for row in kinds):
        raise SetupError('All browser types must remain disabled while starting the test server.')
    jobs = sb.table('llm_jobs').select('id').in_('job_type', ['assistant_turn', 'assistant_approval_execute', 'assistant_continue']).in_('status', ['queued', 'running']).limit(1).execute().data or []
    if jobs:
        raise SetupError('Existing assistant jobs need review before starting this isolated test.')


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
    print('Run this block in Windows PowerShell as Administrator to reconnect only the personal worker:', flush=True)
    print(reconnect_commands(api, template), flush=True)


def reconnect_commands(api: str, template: bytes) -> str:
    from prepare_phase6_windows_install import TUNNEL_URL
    if TUNNEL_URL.fullmatch(api) is None:
        raise SetupError('Invalid isolated test tunnel URL.')
    digest = hashlib.sha256(template).hexdigest()
    return ("& {\n$ErrorActionPreference = 'Stop'\n"
            f"$api = '{api}'\n"
            '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n'
            'Invoke-WebRequest -UseBasicParsing "$api/phase6-acceptance/reconnect.ps1" -OutFile "$env:TEMP\\bdr-personal-reconnect.ps1"\n'
            f'if ((Get-FileHash "$env:TEMP\\bdr-personal-reconnect.ps1" -Algorithm SHA256).Hash -ne \'{digest}\') {{ throw "Reconnect script hash mismatch" }}\n'
            'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-personal-reconnect.ps1" -ApiBase $api\n'
            'if ($LASTEXITCODE -ne 0) { throw "Personal worker reconnect failed" }\n}')


def main(*, controlled_read=False) -> int:
    children, logs, workspace = [], [], None
    try:
        backend_env, frontend_env = process_environments(FRONTEND, dict(os.environ))
        if controlled_read:
            acceptance_preflight()
            backend_env = acceptance_environment(backend_env)
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
            print('Controlled test server ready. Browser type remains disabled until the reconnected worker and read request are verified.', flush=True)
            print('Keep this terminal open. Use a normal BDR assistant conversation and review its read approval card when testing is enabled.', flush=True)
        else:
            print('Credential setup only. This helper does not start queue workers, pollers, model calls, or browser jobs.', flush=True)
        print('Open http://localhost:4510/settings/assistant in your normal browser. Sign in normally if needed.', flush=True)
        if not controlled_read:
            print('Save your personal Procore site account in Browser sign-ins. Never enter the password in chat.', flush=True)
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
