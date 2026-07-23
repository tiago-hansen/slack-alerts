import logging
from collections import Counter
from datetime import UTC, datetime

import requests

from alerts import Alert, AlertType

logger = logging.getLogger(__name__)

BALANCER_POOL_URL = "https://balancer.fi/pools/{chain_slug}/v{version}/{address}"

# Map API chain enum to Balancer URL slug (MAINNET -> ethereum, etc.)
CHAIN_TO_SLUG: dict[str, str] = {
    "MAINNET": "ethereum",
    "ARBITRUM": "arbitrum",
    "BASE": "base",
    "POLYGON": "polygon",
    "GNOSIS": "gnosis",
    "AVALANCHE": "avalanche",
    "OPTIMISM": "optimism",
}

# Block explorer tx URLs, used to link the largest add/remove event.
CHAIN_TO_EXPLORER_TX: dict[str, str] = {
    "MAINNET": "https://etherscan.io/tx/{tx}",
    "ARBITRUM": "https://arbiscan.io/tx/{tx}",
    "BASE": "https://basescan.org/tx/{tx}",
    "POLYGON": "https://polygonscan.com/tx/{tx}",
    "GNOSIS": "https://gnosisscan.io/tx/{tx}",
    "AVALANCHE": "https://snowtrace.io/tx/{tx}",
    "OPTIMISM": "https://optimistic.etherscan.io/tx/{tx}",
}

ALERT_EMOJI = {
    AlertType.TVL_DROP: ":red_circle:",
    AlertType.TVL_SPIKE: ":large_green_circle:",
    AlertType.VOLUME_DROP: ":chart_with_downwards_trend:",
    AlertType.VOLUME_SPIKE: ":chart_with_upwards_trend:",
    AlertType.LARGE_DEPOSITS: ":inbox_tray:",
    AlertType.LARGE_WITHDRAWALS: ":outbox_tray:",
    AlertType.POOL_PAUSED: ":warning:",
}

ALERT_TITLE = {
    AlertType.TVL_DROP: "TVL Drop Alert",
    AlertType.TVL_SPIKE: "TVL Spike Alert",
    AlertType.VOLUME_DROP: "Volume Drop Alert",
    AlertType.VOLUME_SPIKE: "Volume Spike Alert",
    AlertType.LARGE_DEPOSITS: "Large Deposits",
    AlertType.LARGE_WITHDRAWALS: "Large Withdrawals",
    AlertType.POOL_PAUSED: "Pool Paused",
}

TVL_ALERTS = (AlertType.TVL_DROP, AlertType.TVL_SPIKE)
VOLUME_ALERTS = (AlertType.VOLUME_DROP, AlertType.VOLUME_SPIKE)
FLOW_ALERTS = (AlertType.LARGE_DEPOSITS, AlertType.LARGE_WITHDRAWALS)


def _format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _format_pct(fraction: float, signed: bool = True) -> str:
    sign = "+" if signed and fraction > 0 else ""
    return f"{sign}{fraction * 100:.1f}%"


def _build_alert_block(alert: Alert) -> list[dict]:
    emoji = ALERT_EMOJI[alert.alert_type]
    title = ALERT_TITLE[alert.alert_type]
    chain = alert.chain.capitalize()
    version = getattr(alert, "version", 3)
    chain_slug = CHAIN_TO_SLUG.get(alert.chain, alert.chain.lower())
    pool_url = BALANCER_POOL_URL.format(
        chain_slug=chain_slug, version=version, address=alert.pool_address
    )

    header = f"{emoji} *{title}* — Balancer V{version}"
    pool_line = f"Pool: *<{pool_url}|{alert.pool_name}>* ({chain})"

    lines = [header, pool_line]

    if alert.alert_type in TVL_ALERTS:
        lines.append(f"TVL yesterday:  {_format_usd(alert.previous_tvl_usd or 0)}")
        lines.append(f"TVL today:      {_format_usd(alert.current_tvl_usd)}")
        if alert.tvl_change_pct is not None:
            lines.append(f"Change:         *{_format_pct(alert.tvl_change_pct)}*")

    if alert.alert_type in VOLUME_ALERTS:
        lines.append(f"Volume yesterday:  {_format_usd(alert.previous_volume_usd or 0)}")
        lines.append(f"Volume today:      {_format_usd(alert.current_volume_usd or 0)}")
        if alert.volume_change_pct is not None:
            lines.append(f"Change:            *{_format_pct(alert.volume_change_pct)}*")
        lines.append(f"Current TVL:       {_format_usd(alert.current_tvl_usd)}")

    if alert.alert_type in FLOW_ALERTS:
        verb = "Deposited" if alert.alert_type == AlertType.LARGE_DEPOSITS else "Withdrawn"
        share = ""
        if alert.flow_pct_of_tvl is not None:
            share = f" ({_format_pct(alert.flow_pct_of_tvl, signed=False)} of TVL)"
        tx_count = "1 tx" if alert.flow_count == 1 else f"{alert.flow_count} txs"
        lines.append(f"{verb}:      {_format_usd(alert.flow_total_usd or 0)} across {tx_count}{share}")

        if alert.flow_largest_usd is not None:
            largest = _format_usd(alert.flow_largest_usd)
            explorer = CHAIN_TO_EXPLORER_TX.get(alert.chain)
            if explorer and alert.flow_largest_tx:
                largest = f"<{explorer.format(tx=alert.flow_largest_tx)}|{largest}>"
            lines.append(f"Largest:        {largest}")

        lines.append(f"Current TVL:    {_format_usd(alert.current_tvl_usd)}")

    if alert.alert_type == AlertType.POOL_PAUSED:
        lines.append(f"Current TVL:    {_format_usd(alert.current_tvl_usd)}")
        lines.append("The pool has been paused since the last daily check.")

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        },
        {"type": "divider"},
    ]


def _build_summary_header(alerts: list[Alert], run_date: str) -> list[dict]:
    counts = Counter(alert.alert_type for alert in alerts)
    # Follow ALERT_TITLE's declaration order so the breakdown reads the same
    # way every day rather than shifting with whatever fired.
    breakdown = " · ".join(
        f"{ALERT_EMOJI[alert_type]} {count} {ALERT_TITLE[alert_type]}"
        for alert_type, count in ((t, counts[t]) for t in ALERT_TITLE)
        if count
    )

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Balancer Daily Report — {run_date}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{len(alerts)} alert(s)* triggered in the last 24 hours.\n{breakdown}",
            },
        },
        {"type": "divider"},
    ]


def send_alerts(webhook_url: str, alerts: list[Alert]) -> None:
    """
    Send all triggered alerts as a single formatted Slack message.
    Does nothing if the alerts list is empty.
    """
    if not alerts:
        logger.info("No alerts to send.")
        return

    run_date = datetime.now(UTC).strftime("%Y-%m-%d")
    blocks: list[dict] = _build_summary_header(alerts, run_date)

    # Flow alerts are collected after the snapshot checks, so group by pool to
    # keep every signal about one pool together in the message.
    type_order = list(ALERT_TITLE)
    ordered = sorted(alerts, key=lambda a: (a.pool_name, type_order.index(a.alert_type)))

    for alert in ordered:
        blocks.extend(_build_alert_block(alert))

    payload = {"blocks": blocks}

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Slack notification sent successfully (%d alerts).", len(alerts))
    except requests.RequestException as exc:
        logger.error("Failed to send Slack notification: %s", exc)
        raise
