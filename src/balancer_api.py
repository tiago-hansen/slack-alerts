import logging
from collections.abc import Iterator
from typing import Any

import requests

logger = logging.getLogger(__name__)

V3_POOLS_QUERY = """
query GetV3Pools($chains: [GqlChain!]!) {
  poolGetPools(
    where: { protocolVersionIn: [3], chainIn: $chains }
    first: 1000
  ) {
    id
    name
    address
    chain
    dynamicData {
      totalLiquidity
      isPaused
    }
  }
}
"""

POOLS_BY_ID_QUERY = """
query GetPoolsById($ids: [String!]!, $chains: [GqlChain!]!) {
  poolGetPools(
    where: { idIn: $ids, chainIn: $chains }
    first: 1000
  ) {
    id
    name
    address
    chain
    dynamicData {
      totalLiquidity
      volume24h
      volume48h
      isPaused
    }
  }
}
"""

# Fallback scan for v2 pools whose id we don't know yet. A v2 pool id is
# address + nonce, and Notion only gives us the address, so a brand new v2
# pool can't be resolved via idIn until it lands in the snapshot once.
V2_SCAN_QUERY = """
query ScanV2Pools($chains: [GqlChain!]!, $first: Int!, $skip: Int!) {
  poolGetPools(
    where: { protocolVersionIn: [2], chainIn: $chains }
    first: $first
    skip: $skip
  ) {
    id
    name
    address
    chain
    dynamicData {
      totalLiquidity
      volume24h
      volume48h
      isPaused
    }
  }
}
"""

POOL_EVENTS_QUERY = """
query GetPoolEvents($poolId: String!, $chain: GqlChain!, $type: GqlPoolEventType!, $first: Int!, $skip: Int!) {
  poolEvents(
    where: { poolId: $poolId, chainIn: [$chain], type: $type }
    first: $first
    skip: $skip
  ) {
    id
    type
    timestamp
    valueUSD
    userAddress
    tx
  }
}
"""

V2_SUBGRAPH_QUERY = """
query GetV2Pools($addresses: [Bytes!]!) {
  pools(where: { address_in: $addresses }) {
    id
    address
    name
    totalLiquidity
    swapEnabled
  }
}
"""


def _graphql(api_url: str, query: str, variables: dict[str, Any], timeout: int = 60) -> dict:
    """POST a GraphQL query and return the `data` payload, raising on errors."""
    try:
        response = requests.post(
            api_url,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to query Balancer API: %s", exc)
        raise

    body = response.json()
    if "errors" in body:
        logger.error("Balancer API returned errors: %s", body["errors"])
        raise ValueError(f"GraphQL errors: {body['errors']}")

    return body.get("data", {})


def _optional_float(value: Any) -> float | None:
    """Cast to float, preserving None so 'no data' stays distinct from zero."""
    return None if value is None else float(value)


def _normalize_pool(pool: dict) -> dict[str, Any]:
    """Convert raw API/subgraph pool to unified format."""
    dynamic = pool.get("dynamicData") or {}
    return {
        "id": pool.get("id") or pool.get("address", ""),
        "name": pool.get("name", "Unknown"),
        "address": pool.get("address", ""),
        "chain": pool.get("chain", ""),
        "total_liquidity_usd": float(dynamic.get("totalLiquidity") or pool.get("totalLiquidity") or 0),
        # None (not 0) when the source has no volume data — subgraph-sourced
        # pools have none, and that must stay distinct from a quiet pool.
        "volume_24h_usd": _optional_float(dynamic.get("volume24h")),
        "volume_48h_usd": _optional_float(dynamic.get("volume48h")),
        "is_paused": bool(
            dynamic.get("isPaused", False) if "dynamicData" in pool else not pool.get("swapEnabled", True)
        ),
    }


def fetch_pools(api_url: str, chains: list[str]) -> list[dict[str, Any]]:
    """
    Fetch all Balancer V3 pools for the given chains from the API.
    Returns a flat list of pool dicts with fields:
      id, name, address, chain, total_liquidity_usd, is_paused
    """
    data = _graphql(api_url, V3_POOLS_QUERY, {"chains": chains})
    raw_pools = data.get("poolGetPools", [])
    logger.info("Fetched %d pools from Balancer V3 API", len(raw_pools))

    return [_normalize_pool(p) for p in raw_pools]


def fetch_pool_events(
    api_url: str,
    pool_id: str,
    chain: str,
    event_type: str,
    since_ts: int,
    page_size: int = 100,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """
    Fetch ADD or REMOVE events for one pool that are newer than since_ts.

    poolEvents has no server-side time filter, so page through the
    descending-timestamp results and stop at the first event older than
    the window.

    Returns dicts with keys: timestamp, value_usd, user_address, tx.
    A failed request logs and yields [] — one flaky pool should not sink
    the whole daily run.
    """
    events: list[dict[str, Any]] = []

    for page in range(max_pages):
        try:
            data = _graphql(
                api_url,
                POOL_EVENTS_QUERY,
                {
                    "poolId": pool_id,
                    "chain": chain,
                    "type": event_type,
                    "first": page_size,
                    "skip": page * page_size,
                },
                timeout=30,
            )
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Could not fetch %s events for %s: %s", event_type, pool_id, exc)
            return events

        batch = data.get("poolEvents", [])

        for event in batch:
            if int(event["timestamp"]) < since_ts:
                return events
            events.append(
                {
                    "timestamp": int(event["timestamp"]),
                    "value_usd": float(event["valueUSD"]),
                    "user_address": event.get("userAddress", ""),
                    "tx": event.get("tx", ""),
                }
            )

        if len(batch) < page_size:
            return events

    logger.warning(
        "%s event scan for %s hit the %d page cap — window may be truncated",
        event_type,
        pool_id,
        max_pages,
    )
    return events


def fetch_pools_by_ids(
    api_url: str,
    pool_descriptors: list[dict[str, Any]],
    chains: list[str],
    known_ids: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Balancer v2 and v3 pools from the API, filtered to only include
    pools matching the given descriptors (address, chain, version).

    pool_descriptors: list of dicts with keys address, chain, version
    known_ids: (address, chain) -> pool id learned from a previous snapshot.
        Needed for v2, whose id is address + nonce and so cannot be derived
        from the Notion URL alone.

    Returns pool dicts with id, name, address, chain, total_liquidity_usd,
    volume_24h_usd, volume_48h_usd, is_paused, version.
    """
    if not pool_descriptors:
        return []

    known_ids = known_ids or {}
    wanted = {(d["address"].lower(), d["chain"]) for d in pool_descriptors}
    version_by_key = {(d["address"].lower(), d["chain"]): d["version"] for d in pool_descriptors}
    name_by_key = {(d["address"].lower(), d["chain"]): d.get("name", "Unknown") for d in pool_descriptors}

    # For v3 the pool id is the address; for v2 we can only ask by id if a
    # previous run already recorded it.
    candidate_ids = set()
    for d in pool_descriptors:
        key = (d["address"].lower(), d["chain"])
        if d["version"] == 3:
            candidate_ids.add(d["address"].lower())
        if key in known_ids:
            candidate_ids.add(known_ids[key].lower())

    found: dict[tuple[str, str], dict[str, Any]] = {}

    if candidate_ids:
        data = _graphql(api_url, POOLS_BY_ID_QUERY, {"ids": sorted(candidate_ids), "chains": chains})
        for pool in data.get("poolGetPools", []):
            key = ((pool.get("address") or "").lower(), pool.get("chain", ""))
            if key in wanted:
                found[key] = pool

    # Anything still missing is a v2 pool we've never seen. Scan the v2 set in
    # pages rather than a single capped query, which silently drops pools.
    unresolved = wanted - found.keys()
    if unresolved:
        unresolved_chains = sorted({chain for _, chain in unresolved})
        logger.info(
            "Scanning v2 pools on %s to resolve %d unknown pool id(s)",
            ", ".join(unresolved_chains),
            len(unresolved),
        )
        for pool in _scan_v2_pools(api_url, unresolved_chains):
            key = ((pool.get("address") or "").lower(), pool.get("chain", ""))
            if key in unresolved:
                found[key] = pool
                unresolved.discard(key)
                if not unresolved:
                    break

    pools: list[dict[str, Any]] = []
    for key, pool in found.items():
        normalized = _normalize_pool(pool)
        normalized["version"] = version_by_key.get(key, 3)
        if name_by_key.get(key) and name_by_key[key] != "Unknown":
            normalized["name"] = name_by_key[key]
        pools.append(normalized)

    if unresolved:
        logger.warning(
            "%d pool(s) not found in the Balancer API: %s",
            len(unresolved),
            ", ".join(f"{a} on {c}" for a, c in sorted(unresolved)),
        )

    logger.info(
        "Fetched %d of %d monitored pool(s) from Balancer API (v2+v3)", len(pools), len(wanted)
    )
    return pools


def _scan_v2_pools(
    api_url: str,
    chains: list[str],
    page_size: int = 1000,
    max_pages: int = 20,
) -> Iterator[dict[str, Any]]:
    """Yield every v2 pool on the given chains, one page at a time."""
    for page in range(max_pages):
        data = _graphql(
            api_url,
            V2_SCAN_QUERY,
            {"chains": chains, "first": page_size, "skip": page * page_size},
        )
        batch = data.get("poolGetPools", [])
        yield from batch
        if len(batch) < page_size:
            return
    logger.warning("v2 pool scan hit the %d page cap — some pools may be unresolved", max_pages)


def fetch_v2_pools_subgraph(
    subgraph_url: str,
    addresses: list[str],
    chain: str = "MAINNET",
) -> list[dict[str, Any]]:
    """
    Fetch v2 pool data from the Balancer v2 subgraph.
    Used as fallback when pools are not found in the V3 API.

    Note: The default subgraph URL is Ethereum-only. For other chains,
    use the appropriate chain-specific subgraph URL.
    """
    if not addresses:
        return []

    # Subgraph expects checksummed or lowercase addresses
    addrs = [a.lower() if a.startswith("0x") else a for a in addresses]

    payload = {
        "query": V2_SUBGRAPH_QUERY,
        "variables": {"addresses": addrs},
    }

    try:
        response = requests.post(
            subgraph_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to fetch v2 pools from subgraph: %s", exc)
        raise

    body = response.json()
    if "errors" in body:
        logger.error("V2 subgraph returned errors: %s", body["errors"])
        raise ValueError(f"GraphQL errors: {body['errors']}")

    raw_pools = body.get("data", {}).get("pools", [])
    pools: list[dict[str, Any]] = []

    for pool in raw_pools:
        addr = pool.get("address", "")
        pools.append({
            "id": pool.get("id", addr),
            "name": pool.get("name", "Unknown"),
            "address": addr,
            "chain": chain,
            "total_liquidity_usd": float(pool.get("totalLiquidity") or 0),
            # The subgraph exposes no volume data, so volume and flow checks
            # are skipped for pools resolved through this fallback.
            "volume_24h_usd": None,
            "volume_48h_usd": None,
            "is_paused": not bool(pool.get("swapEnabled", True)),
            "version": 2,
        })

    logger.info("Fetched %d v2 pool(s) from subgraph", len(pools))
    return pools
