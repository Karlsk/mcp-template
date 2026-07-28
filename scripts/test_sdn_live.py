"""Live integration check against a real SDN controller.

Exercises the full basic-mode flow end to end:
  1. login (initialize) -> obtain a bearer access_token,
  2. a business call (POST the alerts endpoint) using that token.
Token acquisition and refresh are handled transparently by SDNClient.

Reads configuration from the project's `.env` (SDN_CONTROLLER_*) and
``config/sdn_controller.yaml``. Run from the project root:

    PYTHONPATH=. uv run python scripts/test_sdn_live.py

(`PYTHONPATH=.` is needed in envs where the editable-install .pth does not
autoload — see README "排错".)
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.sdn.client import SDNClient
from app.sdn.exceptions import SDNError
from app.settings import Settings

# A sample business endpoint + payload (controller-specific). Override freely.
ALERTS_ENDPOINT = "/monitor/v2/alert/page"
ALERTS_BODY: dict[str, object] = {
    "interval": "1h",
    "namespace": "device",
    "category": "PE端口Down",
    "pageNum": 1,
    "pageSize": 10,
}


def build_settings() -> Settings:
    """Load settings from .env + YAML; refuse to run if the controller is unset."""
    settings = Settings()
    if not settings.sdn_is_configured():
        sys.exit(
            "SDN controller not configured. Set SDN_CONTROLLER_BASE_URL, "
            "SDN_CONTROLLER_USERNAME, SDN_CONTROLLER_PASSWORD in .env."
        )
    return settings


async def main() -> int:
    settings = build_settings()
    client = SDNClient(settings)
    try:
        login_url = f"{settings.sdn_base_url}{settings.sdn.endpoints['login']}"
        print(f"[1/2] Logging in: POST {login_url} (ssl_verify={settings.sdn.ssl_verify})")
        await client.initialize()
        token = client._http.bearer_token
        masked = f"{token[:16]}..." if len(token) > 16 else "(short token)"
        print(f"      OK — access_token: {masked} (length={len(token)})")

        print(f"[2/2] Business call: POST {settings.sdn_base_url}{ALERTS_ENDPOINT}")
        resp = await client.request("POST", ALERTS_ENDPOINT, json=ALERTS_BODY)
        print(f"      HTTP {resp.status_code}")
        try:
            body = resp.json()
            text = json.dumps(body, ensure_ascii=False, indent=2)
            print("      body:")
            print(text[:2000] + ("..." if len(text) > 2000 else ""))
        except (ValueError, TypeError):
            print(f"      body (non-JSON): {resp.text[:2000]}")
        return 0 if resp.status_code < 400 else 1
    except SDNError as exc:
        # Sanitized message only — no URL/status/credentials leaked.
        print(f"      FAILED (SDNError): {exc}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
