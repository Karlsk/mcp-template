"""Live integration check against a real SDN controller.

Exercises the full basic-mode flow and each v1.5 business method end to end:
  1. login (initialize) -> obtain a bearer access_token,
  2. query_devices, query_links, get_topology,
  3. performance history (port traffic, link performance),
  4. query_alert_page (the v1.5 alerts contract), query_operation_logs.

Token acquisition and refresh are handled transparently by SDNClient. Each step
is tolerant: an SDNError is recorded as a failure and the run continues, so this
script doubles as a schema probe — feed any field-shape surprises back into the
thin-typed models in ``app/sdn/models.py``.

Reads configuration from the project's ``.env`` (SDN_CONTROLLER_*) and
``config/sdn_controller.yaml``. Run from the project root:

    PYTHONPATH=. uv run python scripts/test_sdn_live.py

(`PYTHONPATH=.` is needed in envs where the editable-install .pth does not
autoload — see README "排错".)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.sdn.client import SDNClient
from app.sdn.exceptions import SDNError
from app.settings import Settings


def build_settings() -> Settings:
    """Load settings from .env + YAML; refuse to run if the controller is unset."""
    settings = Settings()
    if not settings.sdn_is_configured():
        sys.exit(
            "SDN controller not configured. Set SDN_CONTROLLER_BASE_URL, "
            "SDN_CONTROLLER_USERNAME, SDN_CONTROLLER_PASSWORD in .env."
        )
    return settings


def _preview(obj: Any, limit: int = 1500) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        return f"(non-JSON) {str(obj)[:limit]}"
    return text[:limit] + ("..." if len(text) > limit else "")


async def main() -> int:
    settings = build_settings()
    client = SDNClient(settings)
    failures: list[str] = []
    first_device_name: str | None = None
    first_port_name: str | None = None
    first_link_id: str | None = None

    async def step(name: str, coro: Any) -> Any:
        try:
            result = await coro
            print("      OK")
            return result
        except SDNError as exc:
            print(f"      FAILED (SDNError): {exc}")
            failures.append(name)
            return None

    try:
        login_url = f"{settings.sdn_base_url}{settings.sdn.endpoints['login']}"
        print(f"[login] POST {login_url} (ssl_verify={settings.sdn.ssl_verify})")
        try:
            await client.initialize()
            token = client._http.bearer_token
            masked = f"{token[:16]}..." if len(token) > 16 else "(short token)"
            print(f"        OK — access_token: {masked} (length={len(token)})")
        except SDNError as exc:
            print(f"        FAILED (SDNError): {exc}", file=sys.stderr)
            return 2

        print("[1] query_devices(page_size=5)")
        devices = await step("query_devices", client.query_devices(page_size=5))
        if devices is not None and devices.content:
            dev = devices.content[0].model_dump(by_alias=True)
            first_device_name = dev.get("name")
            assert "password" not in dev, "device password leaked into the model!"
            ports = (dev.get("peports") or {}).get("peport-info") or []
            if ports:
                first_port_name = ports[0].get("name")
            print(f"        devices={devices.total_elements}; first={first_device_name}")

        print("[2] query_links(page_size=5)")
        links = await step("query_links", client.query_links(page_size=5))
        if links is not None and links.content:
            first_link_id = links.content[0].link_id or None
            print(f"        links={links.total_elements}; first={first_link_id}")

        print("[3] get_topology()")
        topology = await step("get_topology", client.get_topology())
        if topology is not None:
            nodes = sum(len(t.node) for t in topology.topology)
            links_n = sum(len(t.link) for t in topology.topology)
            print(f"        topologies={len(topology.topology)} nodes={nodes} links={links_n}")

        if first_device_name and first_port_name:
            print(
                f"[4] query_switch_history(port) device={first_device_name} "
                f"port={first_port_name}"
            )
            port_perf = await step(
                "query_switch_history(port)",
                client.query_switch_history(
                    "port",
                    metric_names=["in_traffic", "out_traffic"],
                    device_name=first_device_name,
                    port_name=first_port_name,
                ),
            )
            if port_perf is not None:
                print(f"        points={len(port_perf.data)}")
        else:
            print("[4] query_switch_history(port) skipped (no device/port discovered)")

        if first_link_id:
            print(f"[5] query_switch_history(link) link_id={first_link_id}")
            link_perf = await step(
                "query_switch_history(link)",
                client.query_switch_history(
                    "link", metric_names=["jitter", "rtt", "loss"], link_id=first_link_id
                ),
            )
            if link_perf is not None:
                print(f"        points={len(link_perf.data)}")
        else:
            print("[5] query_switch_history(link) skipped (no link discovered)")

        print("[6] query_alert_page() (last hour)")
        alerts = await step("query_alert_page", client.query_alert_page())
        if alerts is not None:
            print(f"        total={alerts.total} returned={len(alerts.data)}")

        print("[7] query_operation_logs() (last hour)")
        logs = await step("query_operation_logs", client.query_operation_logs())
        if logs is not None:
            print(f"        returned={len(logs.data)}")

        print("\n=== summary ===")
        if failures:
            print(f"FAIL: {len(failures)} step(s) failed: {', '.join(failures)}")
            return 1
        print("PASS: all steps succeeded")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
