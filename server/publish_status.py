"""Upload only the allowlisted public JSON; no SMTP or browser state leaves Japan."""
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from dotenv import dotenv_values
from export_status import OUTPUT

CONFIG = Path('/etc/vps-status-publisher.env')
STAMP = Path('/var/lib/vmiss-public-status/published.sha256')

def main():
    config = dotenv_values(CONFIG)
    endpoint = config.get('STATUS_PUBLISH_URL', '')
    token = config.get('STATUS_PUBLISH_TOKEN', '')
    if not endpoint.startswith('https://') or not token:
        raise ValueError('Publisher configuration missing')
    raw = OUTPUT.read_bytes()
    data = json.loads(raw)
    # Export timestamps alone don't cause writes; publish each new observation.
    digest = hashlib.sha256(json.dumps(data['products'], sort_keys=True).encode()).hexdigest()
    if STAMP.exists() and STAMP.read_text() == digest:
        return
    request = Request(endpoint, data=raw, method='PUT', headers={
        'Authorization': 'Bearer '+token, 'Content-Type': 'application/json',
        'User-Agent': 'VPS-Stock-Publisher/1.0', 'Accept': 'application/json'})
    with urlopen(request, timeout=20) as response:
        if response.status != 204:
            raise ValueError('Publisher did not accept snapshot')
    temporary = STAMP.with_suffix('.tmp')
    temporary.write_text(digest)
    os.chmod(temporary, 0o600)
    temporary.replace(STAMP)
    print('Japan public snapshot published')

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('Status publishing failed: '+type(exc).__name__)
        raise SystemExit(1)
