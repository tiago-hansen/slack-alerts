import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class AlertType(str, Enum):
    TVL_DROP = "tvl_drop"
    TVL_SPIKE = "tvl_spike"
    VOLUME_DROP = "volume_drop"
    VOLUME_SPIKE = "volume_spike"
    LARGE_DEPOSITS = "large_deposits"
    LARGE_WITHDRAWALS = "large_withdrawals"
    POOL_PAUSED = "pool_paused"


@dataclass
class Alert:
    alert_type: AlertType
    pool_id: str
    pool_name: str
    pool_address: str
    chain: str
    current_tvl_usd: float
    previous_tvl_usd: float | None = None
    version: int = 3
    # Volume alerts
    current_volume_usd: float | None = None
    previous_volume_usd: float | None = None
    # Flow (deposit/withdrawal) alerts
    flow_total_usd: float | None = None
    flow_count: int = 0
    flow_largest_usd: float | None = None
    flow_largest_tx: str | None = None

    @property
    def tvl_change_pct(self) -> float | None:
        if self.previous_tvl_usd is None or self.previous_tvl_usd == 0:
            return None
        return (self.current_tvl_usd - self.previous_tvl_usd) / self.previous_tvl_usd

    @property
    def volume_change_pct(self) -> float | None:
        if self.current_volume_usd is None or not self.previous_volume_usd:
            return None
        return (self.current_volume_usd - self.previous_volume_usd) / self.previous_volume_usd

    @property
    def flow_pct_of_tvl(self) -> float | None:
        if self.flow_total_usd is None or self.current_tvl_usd == 0:
            return None
        return self.flow_total_usd / self.current_tvl_usd


def _make_alert(alert_type: AlertType, pool: dict[str, Any], **fields: Any) -> Alert:
    """Build an Alert, filling the pool identity fields shared by every type."""
    return Alert(
        alert_type=alert_type,
        pool_id=pool["id"],
        pool_name=pool["name"],
        pool_address=pool["address"],
        chain=pool["chain"],
        current_tvl_usd=pool["total_liquidity_usd"],
        version=pool.get("version", 3),
        **fields,
    )


def _prior_day_volume(pool: dict[str, Any]) -> tuple[float, float] | None:
    """
    Return (last 24h volume, the 24h before that) for a pool.

    volume48h is cumulative over two days, so the prior day is the
    difference. Returns None when the source carries no volume data.
    """
    volume_24h = pool.get("volume_24h_usd")
    volume_48h = pool.get("volume_48h_usd")
    if volume_24h is None or volume_48h is None:
        return None
    return volume_24h, max(volume_48h - volume_24h, 0.0)


def check_alerts(
    current_pools: list[dict[str, Any]],
    previous_snapshot: dict[str, Any],
    tvl_drop_threshold: float,
    tvl_spike_threshold: float,
    min_tvl_usd: float,
    volume_change_threshold: float,
    min_volume_usd: float,
) -> list[Alert]:
    """
    Compare current pool data against the previous snapshot and return
    a list of triggered alerts.

    Args:
        current_pools: list of pool dicts from the Balancer API.
        previous_snapshot: dict mapping pool_id -> pool data from last run.
        tvl_drop_threshold: fractional drop (e.g. 0.10 = 10%) that triggers a TVL drop alert.
        tvl_spike_threshold: fractional gain (e.g. 0.10 = 10%) that triggers a TVL spike alert.
        min_tvl_usd: pools below this TVL on both days are ignored.
        volume_change_threshold: day-over-day volume move that triggers a volume alert,
            applied as a ratio — 1.00 means volume doubled (spike) or halved (drop).
        min_volume_usd: pools below this volume on both days are ignored.
    """
    triggered: list[Alert] = []

    for pool in current_pools:
        pool_id = pool["id"]
        current_tvl = pool["total_liquidity_usd"]
        is_paused = pool["is_paused"]

        previous = previous_snapshot.get(pool_id)
        previous_tvl = float((previous or {}).get("total_liquidity_usd", 0))

        # Gate on the larger of the two so a pool collapsing below the floor
        # still alerts, while genuinely small pools stay filtered out.
        if max(current_tvl, previous_tvl) < min_tvl_usd:
            continue

        if previous is None:
            logger.debug("Pool %s not in previous snapshot — skipping delta checks", pool_id)
        elif previous_tvl > 0:
            change = (current_tvl - previous_tvl) / previous_tvl

            if change <= -tvl_drop_threshold:
                triggered.append(
                    _make_alert(AlertType.TVL_DROP, pool, previous_tvl_usd=previous_tvl)
                )
                logger.info("TVL_DROP triggered for pool %s: %.1f%%", pool_id, change * 100)

            elif change >= tvl_spike_threshold:
                triggered.append(
                    _make_alert(AlertType.TVL_SPIKE, pool, previous_tvl_usd=previous_tvl)
                )
                logger.info("TVL_SPIKE triggered for pool %s: +%.1f%%", pool_id, change * 100)

        # Volume comes from the API's own 24h/48h counters, so it needs no
        # snapshot and is immune to the daily job running late.
        volumes = _prior_day_volume(pool)
        if volumes is not None:
            volume_24h, prior_volume = volumes
            if prior_volume > 0 and max(volume_24h, prior_volume) >= min_volume_usd:
                change = (volume_24h - prior_volume) / prior_volume

                # Compared as a ratio, not raw percent change: at a threshold of
                # 1.00 the spike arm means "doubled" and the drop arm means
                # "halved". Testing the drop as change <= -1.00 would only ever
                # fire on volume reaching exactly zero.
                factor = 1 + volume_change_threshold

                if volume_24h * factor <= prior_volume:
                    triggered.append(
                        _make_alert(
                            AlertType.VOLUME_DROP,
                            pool,
                            current_volume_usd=volume_24h,
                            previous_volume_usd=prior_volume,
                        )
                    )
                    logger.info("VOLUME_DROP triggered for pool %s: %.1f%%", pool_id, change * 100)

                elif volume_24h >= prior_volume * factor:
                    triggered.append(
                        _make_alert(
                            AlertType.VOLUME_SPIKE,
                            pool,
                            current_volume_usd=volume_24h,
                            previous_volume_usd=prior_volume,
                        )
                    )
                    logger.info("VOLUME_SPIKE triggered for pool %s: +%.1f%%", pool_id, change * 100)

        was_paused = bool((previous or {}).get("is_paused", False))
        if is_paused and not was_paused:
            triggered.append(_make_alert(AlertType.POOL_PAUSED, pool))
            logger.info("POOL_PAUSED triggered for pool %s", pool_id)

    logger.info("%d alert(s) triggered in total", len(triggered))
    return triggered


def check_flow_alerts(
    pool: dict[str, Any],
    add_events: list[dict[str, Any]],
    remove_events: list[dict[str, Any]],
    flow_pct_of_tvl: float,
    flow_abs_usd: float,
) -> list[Alert]:
    """
    Aggregate one pool's add/remove liquidity events over the window and
    alert when either side is large.

    A side fires when its total clears flow_abs_usd, or is worth at least
    flow_pct_of_tvl of the pool's TVL — the absolute arm catches whale moves
    in deep pools, the relative arm catches proportionally big moves in
    small ones. Both sides can fire for the same pool; that is real churn
    and worth seeing as two separate alerts.
    """
    triggered: list[Alert] = []
    current_tvl = pool["total_liquidity_usd"]

    for alert_type, events in (
        (AlertType.LARGE_DEPOSITS, add_events),
        (AlertType.LARGE_WITHDRAWALS, remove_events),
    ):
        if not events:
            continue

        total = sum(e["value_usd"] for e in events)
        if total < flow_abs_usd and total < flow_pct_of_tvl * current_tvl:
            continue

        largest = max(events, key=lambda e: e["value_usd"])
        triggered.append(
            _make_alert(
                alert_type,
                pool,
                flow_total_usd=total,
                flow_count=len(events),
                flow_largest_usd=largest["value_usd"],
                flow_largest_tx=largest["tx"],
            )
        )
        logger.info(
            "%s triggered for pool %s: $%.0f across %d tx",
            alert_type.name,
            pool["id"],
            total,
            len(events),
        )

    return triggered
