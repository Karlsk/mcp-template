"""Test MCP client: list tools and call tools over Streamable HTTP.

Examples::

    python scripts/test_client.py list-tools --url http://127.0.0.1:8000/mcp
    python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp --name ping
    python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp \
        --name ping --args-json '{"message": "hi"}'
    python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp --name sdn_health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://127.0.0.1:8000/mcp"


def _print_content(blocks: Iterable[Any]) -> None:
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            print(f"text: {text}")


async def list_tools(url: str) -> int:
    async with (
        streamablehttp_client(url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
    for tool in result.tools:
        print(f"- {tool.name}: {tool.description or '(no description)'}")
        if tool.inputSchema:
            print(f"    schema: {json.dumps(tool.inputSchema)}")
    print(f"\n{len(result.tools)} tool(s) available.")
    return 0


async def call_tool(url: str, name: str, args_json: str | None) -> int:
    arguments: dict[str, Any] = {}
    if args_json:
        try:
            parsed = json.loads(args_json)
        except json.JSONDecodeError as exc:
            print(f"Invalid --args-json: {exc}", file=sys.stderr)
            return 2
        if not isinstance(parsed, dict):
            print("--args-json must be a JSON object.", file=sys.stderr)
            return 2
        arguments = parsed

    async with (
        streamablehttp_client(url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, arguments)

    print(f"isError: {result.isError}")
    if result.structuredContent:
        print(f"structured: {json.dumps(result.structuredContent, indent=2)}")
    _print_content(result.content)
    return 0 if not result.isError else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test client for the SDN MCP server.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-tools", help="List available MCP tools.")
    p_list.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default: {DEFAULT_URL})")

    p_call = sub.add_parser("call-tool", help="Call an MCP tool by name.")
    p_call.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default: {DEFAULT_URL})")
    p_call.add_argument("--name", required=True, help="Tool name to call.")
    p_call.add_argument(
        "--args-json",
        default=None,
        help='Arguments as a JSON object, e.g. \'{"message": "hi"}\'',
    )

    args = parser.parse_args(argv)
    if args.command == "list-tools":
        return asyncio.run(list_tools(args.url))
    if args.command == "call-tool":
        return asyncio.run(call_tool(args.url, args.name, args.args_json))
    return 2  # pragma: no cover (argparse enforces a subcommand)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
