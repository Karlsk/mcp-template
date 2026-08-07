"""Live Neo4j driver connectivity test (manual only).

Tests the driver layer (app/common/neo4j.py) directly against a real Neo4j,
isolating network/auth issues from the graph business layer.

Usage:
    uv run python scripts/test_neo4j_driver.py
    # or with explicit URI:
    NEO4J_URI=bolt://localhost:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD='dms123!!' \
    uv run python scripts/test_neo4j_driver.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from neo4j import AsyncGraphDatabase


async def test_raw_driver(uri: str, user: str, password: str) -> bool:
    """Test 1: Raw neo4j official driver — no app code involved."""
    print(f"\n[1] Raw driver: {uri}")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        await driver.verify_connectivity()
        print("    verify_connectivity: OK")
        async with driver.session() as session:
            result = await session.run("RETURN 1 AS n")
            records = [r async for r in result]
            print(f"    query 'RETURN 1': {records[0]['n']}")
        return True
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return False
    finally:
        await driver.close()


async def test_neo4j_client(uri: str, user: str, password: str) -> bool:
    """Test 2: app.common.neo4j.Neo4jClient (the generic driver wrapper)."""
    from app.common.neo4j import Neo4jClient, Neo4jClientConfig

    print("\n[2] app.common.neo4j.Neo4jClient")
    client = Neo4jClient(
        Neo4jClientConfig(
            uri=uri,
            username=user,
            password=password,
            database="neo4j",
            query_timeout=10.0,
            max_transaction_retry_time=5.0,
        )
    )
    try:
        await client.verify_connectivity()
        print("    verify_connectivity: OK")
        rows = await client.run_read(
            "RETURN 1 AS n, $database AS db",
            {"database": "neo4j"},
            db_tag="neo4j",
            query_name="smoke_test",
        )
        print(f"    run_read 'RETURN 1': {rows}")
        print(f"    configured={client.configured}")
        return True
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return False
    finally:
        await client.close()


async def test_graph_client(uri: str, user: str, password: str) -> bool:
    """Test 3: app.graph.GraphClient (the business layer + probe)."""
    from app.graph import GraphClient
    from app.settings import Neo4jSettings, Settings

    print("\n[3] app.graph.GraphClient (probe only)")
    settings = Settings(
        neo4j_uri=uri,
        neo4j_username=user,
        neo4j_password=password,
        neo4j=Neo4jSettings(uri=uri, query_timeout=10.0, max_transaction_retry_time=5.0),
    )
    graph = GraphClient(settings)
    try:
        ok = await graph.probe()
        print(f"    probe: {'OK' if ok else 'FAILED'}")
        print(f"    configured={graph.configured}")
        return ok
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return False
    finally:
        await graph.aclose()


async def test_latency(uri: str, user: str, password: str) -> None:
    """Test 4: Quick latency check (3 consecutive pings)."""
    print("\n[4] Latency (3x connectivity checks)")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        for i in range(3):
            t0 = time.monotonic()
            await driver.verify_connectivity()
            ms = (time.monotonic() - t0) * 1000
            print(f"    ping #{i + 1}: {ms:.1f} ms")
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")
    finally:
        await driver.close()


async def main() -> int:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "dms123!!")

    print(f"=== Neo4j Driver Live Test ===")
    print(f"  uri={uri}  user={user}")

    ok1 = await test_raw_driver(uri, user, password)
    ok2 = await test_neo4j_client(uri, user, password)
    ok3 = await test_graph_client(uri, user, password)
    await test_latency(uri, user, password)

    print(f"\n=== Summary ===")
    print(f"  raw driver:     {'PASS' if ok1 else 'FAIL'}")
    print(f"  Neo4jClient:    {'PASS' if ok2 else 'FAIL'}")
    print(f"  GraphClient:    {'PASS' if ok3 else 'FAIL'}")
    return 0 if ok1 and ok2 and ok3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
