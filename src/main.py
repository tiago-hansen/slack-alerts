import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from alerts import check_alerts, check_flow_alerts
from balancer_api import fetch_pool_events, fetch_pools_by_ids, fetch_v2_pools_subgraph
from notion_pools import query_pool_list
from slack_notifier import send_alerts

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# Reserved snapshot key holding run metadata rather than pool data.
META_KEY = "_meta"
SECONDS_PER_HOUR = 3600


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_snapshot(snapshot_path: Path) -> tuple[dict, dict]:
    """Return (pools keyed by pool id, run metadata) from the snapshot file."""
    if not snapshot_path.exists() or snapshot_path.stat().st_size == 0:
        logger.info("No previous snapshot found at %s — first run.", snapshot_path)
        return {}, {}
    with open(snapshot_path) as f:
        data = json.load(f)
    # Snapshots written before _meta existed simply have no metadata.
    meta = data.pop(META_KEY, {})
    return data, meta


def save_snapshot(snapshot_path: Path, pools: list[dict], run_at: int) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    data = {META_KEY: {"last_run_at": run_at}}
    data.update({pool["id"]: pool for pool in pools})
    with open(snapshot_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Snapshot saved to %s (%d pools).", snapshot_path, len(pools))


def event_window_start(meta: dict, now: int, max_lookback_hours: float) -> int:
    """
    Start of the add/remove event window.

    Anchored to the previous run so a late or retried job neither double-reports
    flows nor skips them, defaulting to 24h and clamped so a long outage cannot
    trigger a huge backfill.
    """
    earliest = now - int(max_lookback_hours * SECONDS_PER_HOUR)
    last_run = meta.get("last_run_at")
    if last_run is None:
        return now - 24 * SECONDS_PER_HOUR
    return max(int(last_run), earliest)


def collect_flow_alerts(
    api_url: str,
    pools: list[dict],
    since_ts: int,
    flow_pct_of_tvl: float,
    flow_abs_usd: float,
) -> list:
    """Fetch add/remove events per pool and return the large-flow alerts."""
    alerts = []
    for pool in pools:
        # Subgraph-sourced pools are absent from the API, so poolEvents has
        # nothing for them; volume_24h_usd is the marker for that path.
        if pool.get("volume_24h_usd") is None:
            logger.debug("Skipping flow check for %s (no API event data)", pool["id"])
            continue

        adds = fetch_pool_events(api_url, pool["id"], pool["chain"], "ADD", since_ts)
        removes = fetch_pool_events(api_url, pool["id"], pool["chain"], "REMOVE", since_ts)
        alerts.extend(
            check_flow_alerts(pool, adds, removes, flow_pct_of_tvl, flow_abs_usd)
        )
    return alerts


def main() -> None:
    config = load_config()

    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook_url:
        logger.error("SLACK_WEBHOOK_URL environment variable is not set.")
        sys.exit(1)

    notion_api_key = os.environ.get("NOTION_API_KEY")
    notion_pools_db_id = os.environ.get("NOTION_POOLS_DB_ID")
    if not notion_api_key or not notion_pools_db_id:
        logger.error("NOTION_API_KEY and NOTION_POOLS_DB_ID environment variables must be set.")
        sys.exit(1)

    api_url: str = config["api_url"]
    chains: list[str] = config["chains"]
    snapshot_path = Path(__file__).parent.parent / config["snapshot_path"]
    v2_subgraph_url = (
        os.environ.get("BALANCER_V2_SUBGRAPH")
        or "https://api.studio.thegraph.com/query/24660/balancer-ethereum-v2/version/latest"
    )

    alert_config = config["alerts"]
    tvl_drop_threshold: float = alert_config["tvl_drop_threshold"]
    tvl_spike_threshold: float = alert_config["tvl_spike_threshold"]
    min_tvl_usd: float = alert_config["min_tvl_usd"]
    volume_change_threshold: float = alert_config["volume_change_threshold"]
    min_volume_usd: float = alert_config["min_volume_usd"]
    flow_pct_of_tvl: float = alert_config["flow_pct_of_tvl"]
    flow_abs_usd: float = alert_config["flow_abs_usd"]
    max_lookback_hours: float = alert_config["max_lookback_hours"]

    logger.info("Fetching pool list from Notion")
    pool_descriptors = query_pool_list(notion_api_key, notion_pools_db_id)
    if not pool_descriptors:
        logger.warning("No pools found in Notion database. Exiting.")
        sys.exit(0)

    previous_snapshot, meta = load_snapshot(snapshot_path)

    # v2 pool ids are address + nonce and can't be derived from the Notion URL,
    # so reuse the ids the last run recorded.
    known_ids = {
        (pool["address"].lower(), pool["chain"]): pool_id
        for pool_id, pool in previous_snapshot.items()
    }

    logger.info("Fetching pool data from Balancer API (v2+v3)")
    current_pools = fetch_pools_by_ids(api_url, pool_descriptors, chains, known_ids)

    # Fallback: v2 pools not found in API — fetch from v2 subgraph (Ethereum-only)
    found_keys = {(p["address"].lower(), p["chain"]) for p in current_pools}
    v2_mainnet_missing = [
        d for d in pool_descriptors
        if d["version"] == 2
        and d["chain"] == "MAINNET"
        and (d["address"].lower(), d["chain"]) not in found_keys
    ]
    if v2_mainnet_missing:
        mainnet_addresses = [d["address"] for d in v2_mainnet_missing]
        logger.info("Fetching %d v2 pool(s) from subgraph (API fallback)", len(mainnet_addresses))
        try:
            v2_pools = fetch_v2_pools_subgraph(v2_subgraph_url, mainnet_addresses, chain="MAINNET")
            for p in v2_pools:
                p["name"] = next(
                    (d["name"] for d in v2_mainnet_missing if d["address"].lower() == p["address"].lower()),
                    p["name"],
                )
            current_pools.extend(v2_pools)
        except Exception as exc:
            logger.warning("V2 subgraph fallback failed (skipping): %s", exc)

    run_at = int(time.time())

    triggered_alerts = check_alerts(
        current_pools=current_pools,
        previous_snapshot=previous_snapshot,
        tvl_drop_threshold=tvl_drop_threshold,
        tvl_spike_threshold=tvl_spike_threshold,
        min_tvl_usd=min_tvl_usd,
        volume_change_threshold=volume_change_threshold,
        min_volume_usd=min_volume_usd,
    )

    since_ts = event_window_start(meta, run_at, max_lookback_hours)
    logger.info("Checking liquidity flows since %d (%.1fh window)", since_ts, (run_at - since_ts) / 3600)
    triggered_alerts.extend(
        collect_flow_alerts(api_url, current_pools, since_ts, flow_pct_of_tvl, flow_abs_usd)
    )

    send_alerts(slack_webhook_url, triggered_alerts)

    save_snapshot(snapshot_path, current_pools, run_at)

    logger.info("Daily check complete. %d alert(s) sent.", len(triggered_alerts))


if __name__ == "__main__":
    main()
