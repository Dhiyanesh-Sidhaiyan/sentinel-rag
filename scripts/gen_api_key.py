"""Generate an API key for a tenant and write only its SHA-256 hash into .env (API_KEYS_SHA256).
Also generates a random POSTGRES_PASSWORD for docker compose if one is not set.

    python scripts/gen_api_key.py --tenant orbitly

The raw key is printed once to your terminal and stored nowhere else.
"""

import argparse
import hashlib
import json
import re
import secrets
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="orbitly")
    args = ap.parse_args()

    key = "sk-sentinel-" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode()).hexdigest()

    if not ENV.exists():
        ENV.write_text((ENV.parent / ".env.example").read_text())
    content = ENV.read_text()
    m = re.search(r"^API_KEYS_SHA256=(.*)$", content, re.MULTILINE)
    current = json.loads(m.group(1)) if m and m.group(1).strip() else {}
    current[digest] = args.tenant
    line = f"API_KEYS_SHA256={json.dumps(current)}"
    content = re.sub(r"^API_KEYS_SHA256=.*$", line, content, flags=re.MULTILINE) if m else f"{content}\n{line}\n"
    if not re.search(r"^POSTGRES_PASSWORD=\S+", content, re.MULTILINE):  # random local DB password for compose
        content += f"\nPOSTGRES_PASSWORD={secrets.token_urlsafe(24)}\n"
    ENV.write_text(content)
    print(f"tenant={args.tenant}\nAPI key (shown once, store it in your password manager):\n{key}")


if __name__ == "__main__":
    main()
