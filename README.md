# Balancer Slack Warnings

This repository runs two automated checks and posts alerts to Slack:

- **Pool alerts**: monitors selected Balancer pools for TVL moves, volume moves, large liquidity flows, and newly paused pools.
- **Touchpoint alerts**: posts daily touchpoint reminders grouped by attendee.

Both checks are designed to run in GitHub Actions on a schedule, but you can also run them locally.

## Pool Alert Rules

The pool list comes from the Notion pools database; each row's Balancer URL supplies the
address, chain, and protocol version. Every rule below is evaluated per pool, per run, and
all triggered alerts are batched into a single Slack message. Nothing is sent when none fire.

| Rule | Trigger | Source |
| --- | --- | --- |
| TVL Drop | day-over-day TVL ≤ −10% | snapshot diff |
| TVL Spike | day-over-day TVL ≥ +10% | snapshot diff |
| Volume Drop | 24h volume at most half the prior 24h | `volume24h` / `volume48h` |
| Volume Spike | 24h volume at least double the prior 24h | `volume24h` / `volume48h` |
| Large Deposits | window `ADD` total ≥ 10% of TVL **or** ≥ $250K | `poolEvents` |
| Large Withdrawals | window `REMOVE` total ≥ 10% of TVL **or** ≥ $250K | `poolEvents` |
| Pool Paused | `isPaused` flips false → true | snapshot diff |

Filters:

- TVL rules need `max(today, yesterday) ≥ min_tvl_usd`, so a pool collapsing below the floor
  still alerts while genuinely small pools stay quiet.
- Volume rules need `max(today, prior day) ≥ min_volume_usd` and a non-zero prior day.
  Volume is far noisier than TVL day to day, which is why its threshold is much wider.
- Delta rules need the pool to be present in the previous snapshot; a pool's first run only
  establishes a baseline.
- The deposit/withdrawal window runs from the previous run's timestamp (recorded in the
  snapshot under `_meta.last_run_at`) to now, so a late or retried job neither double-reports
  flows nor skips them. It defaults to 24h and is capped at `max_lookback_hours`.

## Installation and Local Configuration

### Prerequisites

- Python `3.12` (same as GitHub Actions)
- `pip`
- Access to:
  - Slack incoming webhook
  - Notion integration token
  - Relevant Notion database IDs

### 1) Clone and enter the repository

```bash
git clone <your-repo-url>
cd slack-warnings
```

### 2) Create and activate a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Create a local `.env`

Create a `.env` file in the repository root:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
NOTION_API_KEY=secret_xxx
NOTION_POOLS_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NOTION_TOUCHPOINT_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BALANCER_V2_SUBGRAPH=https://api.studio.thegraph.com/query/24660/balancer-ethereum-v2/version/latest
```

Note:
- `BALANCER_V2_SUBGRAPH` is optional locally (the app has a default fallback URL).

### 5) Verify static config

Review `config.yaml`:
- `alerts`: alert thresholds (see below).
- `chains`: Balancer chains queried in API.
- `api_url`: Balancer GraphQL endpoint.
- `snapshot_path`: path of persistent comparison snapshot.

Alert threshold keys:

| Key | Meaning |
| --- | --- |
| `tvl_drop_threshold` | fractional TVL drop that alerts (`0.10` = 10%) |
| `tvl_spike_threshold` | fractional TVL gain that alerts |
| `min_tvl_usd` | pools under this TVL on both days are ignored |
| `volume_change_threshold` | day-over-day volume move that alerts, as a ratio (`1.00` = doubled or halved) |
| `min_volume_usd` | pools under this volume on both days are ignored |
| `flow_pct_of_tvl` | deposit/withdrawal total worth this share of TVL alerts |
| `flow_abs_usd` | deposit/withdrawal total at or above this USD value alerts |
| `max_lookback_hours` | ceiling on the event window after a missed run |

## Running Locally

### Pool alerts job

```bash
python src/main.py
```

### Touchpoint alerts job

```bash
python src/touchpoint_check.py
```

Optional Monday simulation:

```bash
python src/touchpoint_check.py --monday
```

## Environment Variables and GitHub Actions

This repo uses GitHub Actions workflows:

- `.github/workflows/daily-check.yml`
- `.github/workflows/touchpoint-check.yml`

Both workflows pass runtime configuration using `env` from GitHub **Secrets**.

### Required GitHub Secrets

Set these in **Repository Settings -> Secrets and variables -> Actions -> Secrets**:

- `SLACK_WEBHOOK_URL`
- `NOTION_API_KEY`
- `NOTION_POOLS_DB_ID` (used by daily pool check)
- `NOTION_TOUCHPOINT_DB_ID` (used by touchpoint check)

Optional:
- `BALANCER_V2_SUBGRAPH` (used by daily pool check fallback for missing v2 pools)

## Repository Structure

```text
.
├── .github/workflows/
│   ├── daily-check.yml          # Scheduled pool monitoring workflow
│   └── touchpoint-check.yml     # Scheduled touchpoint workflow
├── config.yaml                  # Alert thresholds + chain/API config
├── data/
│   └── snapshot.json            # Last pool snapshot used for diffing
├── src/
│   ├── main.py                  # Entry point: pool monitoring flow
│   ├── alerts.py                # Alert detection rules
│   ├── balancer_api.py          # Balancer API + v2 subgraph fetchers
│   ├── notion_pools.py          # Notion pools DB parser/query
│   ├── slack_notifier.py        # Pool alert Slack formatter/sender
│   ├── touchpoint_check.py      # Entry point: touchpoint flow
│   ├── touchpoint_alerts.py     # Touchpoint filtering rules
│   ├── notion_client.py         # Notion touchpoint DB client
│   └── touchpoint_notifier.py   # Touchpoint Slack formatter/sender
└── requirements.txt             # Python dependencies
```

## Testing and Validation

There is currently **no automated test suite** (no `pytest` tests yet). For now, use this validation workflow:

### 1) Run both flows manually

```bash
python src/main.py
python src/touchpoint_check.py --monday
```

## Common First-Time Issues

- **`SLACK_WEBHOOK_URL environment variable is not set`**  
  Missing `.env` locally or missing GitHub secret in Actions.

- **Notion API 401/403**  
  Invalid `NOTION_API_KEY` or integration is not shared with the target database.

- **No pools/touchpoints returned**  
  Wrong DB ID, unexpected Notion property schema, or filters exclude everything.

- **No alerts sent**  
  Expected when nothing matches thresholds/criteria. Check logs and thresholds in `config.yaml`.
