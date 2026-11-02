"""Replace only the tunnel to an already running development acceptance API."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import httpx  # noqa: E402
import prepare_phase6_windows_install as preparation  # noqa: E402
import start_phase6_credential_setup as setup  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def existing_run(client) -> str:
    data = preparation.fetch(client, 'http://127.0.0.1:5071/phase6-acceptance/ready',
                             maximum=10000, timeout=15)
    try:
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {'run', 'ready'}
                or value['ready'] is not True or not isinstance(value['run'], str)
                or not re.fullmatch(r'[a-f0-9]{32}', value['run'])):
            raise ValueError
        return value['run']
    except (ValueError, TypeError):
        raise setup.SetupError('The existing port 5071 server is not a ready Phase 6 acceptance API.') from None


def main() -> int:
    children = []
    try:
        setup.require_dev(get_settings().supabase_url)
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            print('Checking the existing isolated test API on port 5071...', flush=True)
            run = existing_run(client)
            print('Starting a replacement tunnel to that same test API...', flush=True)
            setup.connect_acceptance_tunnel(client, children, {'PHASE6_ACCEPTANCE_RUN': run})
        print('Keep this terminal open. Ctrl+C stops only this replacement tunnel.', flush=True)
        print('Run the printed Windows block, then report its result so the worker update can be verified.', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise setup.SetupError('The replacement tunnel stopped. Existing test servers were left running.')
    except KeyboardInterrupt:
        return 0
    except (setup.SetupError, preparation.PreparationError) as exc:
        print(f'Reconnection stopped: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Reconnection stopped ({type(exc).__name__}); existing test servers were left running.', file=sys.stderr)
        return 1
    finally:
        for child in reversed(children):
            preparation.stop_tunnel(child)


if __name__ == '__main__':
    raise SystemExit(main())
