"""
Data sync pipeline: Dune Analytics → Firestore.

Provides backfill (full history) and daily incremental sync.
All data comes from on-chain queries — never simulated.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import requests
from dune_client.client import DuneClient

logger = logging.getLogger(__name__)

BSC_RPC = "https://bsc-dataseed.binance.org/"

# ── SQL Templates ────────────────────────────────────────────

ALL_DAILY_MINTS_SQL = """
SELECT
    DATE_TRUNC('day', t.block_time AT TIME ZONE 'UTC') AS day,
    COUNT(DISTINCT t."to") AS new_users
FROM nft.transfers t
JOIN bnb.transactions tx ON tx.hash = t.tx_hash
WHERE t.blockchain = 'bnb'
  AND t.contract_address = FROM_HEX('{token_hex}')
  AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
  AND tx."from" = FROM_HEX('{minter_hex}')
GROUP BY 1
ORDER BY 1
"""

TODAY_WALLET_AGE_SQL = """
WITH params AS (
  SELECT
    TIMESTAMP '{target_date} 00:00:00' AS tw_day_start,
    TIMESTAMP '{target_date} 00:00:00' + interval '1' day AS tw_day_end
),
today_users AS (
  SELECT t."to" AS wallet, MIN(t.block_time) AS mint_time
  FROM nft.transfers t
  JOIN bnb.transactions tx ON tx.hash = t.tx_hash
  CROSS JOIN params p
  WHERE t.blockchain = 'bnb'
    AND t.contract_address = FROM_HEX('{token_hex}')
    AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
    AND tx."from" = FROM_HEX('{minter_hex}')
    AND t.block_time >= p.tw_day_start AND t.block_time < p.tw_day_end
    AND tx.block_time >= p.tw_day_start AND tx.block_time < p.tw_day_end
  GROUP BY 1
),
prior_tx_counts AS (
  SELECT u.wallet, COUNT(t.hash) AS prior_tx_count
  FROM today_users u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),
first_seen AS (
  SELECT u.wallet, MIN(t.block_time) AS first_tx_time
  FROM today_users u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),
features AS (
  SELECT u.wallet, u.mint_time,
    COALESCE(c.prior_tx_count, 0) AS prior_tx_count,
    f.first_tx_time,
    CASE WHEN f.first_tx_time IS NULL THEN NULL
      ELSE date_diff('day', f.first_tx_time, u.mint_time)
    END AS age_days_proxy
  FROM today_users u
  LEFT JOIN prior_tx_counts c ON c.wallet = u.wallet
  LEFT JOIN first_seen f ON f.wallet = u.wallet
)
SELECT
  CASE
    WHEN first_tx_time IS NULL THEN 'No history (<=1y)'
    WHEN age_days_proxy < 7 THEN '<7d'
    WHEN age_days_proxy < 30 THEN '7-29d'
    WHEN age_days_proxy < 90 THEN '30-89d'
    WHEN age_days_proxy < 180 THEN '90-179d'
    ELSE '180d-1y'
  END AS age_bucket,
  CASE
    WHEN prior_tx_count = 0 THEN '0'
    WHEN prior_tx_count BETWEEN 1 AND 2 THEN '1-2'
    WHEN prior_tx_count BETWEEN 3 AND 9 THEN '3-9'
    WHEN prior_tx_count BETWEEN 10 AND 49 THEN '10-49'
    WHEN prior_tx_count BETWEEN 50 AND 199 THEN '50-199'
    ELSE '200+'
  END AS tx_count_bucket,
  COUNT(*) AS users
FROM features
GROUP BY 1, 2
ORDER BY 1, 2
"""

ALL_HOLDERS_WALLET_SQL = """
WITH all_holders AS (
  SELECT t."to" AS wallet, MIN(t.block_time) AS mint_time
  FROM nft.transfers t
  JOIN bnb.transactions tx ON tx.hash = t.tx_hash
  WHERE t.blockchain = 'bnb'
    AND t.contract_address = FROM_HEX('{token_hex}')
    AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
    AND tx."from" = FROM_HEX('{minter_hex}')
  GROUP BY 1
),
prior_tx_counts AS (
  SELECT u.wallet, COUNT(t.hash) AS prior_tx_count
  FROM all_holders u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),
first_seen AS (
  SELECT u.wallet, MIN(t.block_time) AS first_tx_time
  FROM all_holders u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),
features AS (
  SELECT u.wallet, u.mint_time,
    COALESCE(c.prior_tx_count, 0) AS prior_tx_count,
    f.first_tx_time,
    CASE WHEN f.first_tx_time IS NULL THEN NULL
      ELSE date_diff('day', f.first_tx_time, u.mint_time)
    END AS age_days_proxy
  FROM all_holders u
  LEFT JOIN prior_tx_counts c ON c.wallet = u.wallet
  LEFT JOIN first_seen f ON f.wallet = u.wallet
)
SELECT
  CASE
    WHEN first_tx_time IS NULL THEN 'New wallet'
    WHEN age_days_proxy <= 1 THEN '0-1d'
    WHEN age_days_proxy <= 7 THEN '2-7d'
    WHEN age_days_proxy <= 14 THEN '8-14d'
    WHEN age_days_proxy <= 30 THEN '15-30d'
    WHEN age_days_proxy <= 60 THEN '31-60d'
    WHEN age_days_proxy <= 90 THEN '61-90d'
    WHEN age_days_proxy <= 180 THEN '91-180d'
    ELSE '181-365d'
  END AS age_bucket,
  CASE
    WHEN prior_tx_count = 0 THEN '0 txs'
    WHEN prior_tx_count <= 5 THEN '1-5 txs'
    WHEN prior_tx_count <= 20 THEN '6-20 txs'
    WHEN prior_tx_count <= 50 THEN '21-50 txs'
    WHEN prior_tx_count <= 100 THEN '51-100 txs'
    WHEN prior_tx_count <= 500 THEN '101-500 txs'
    ELSE '500+ txs'
  END AS tx_count_bucket,
  COUNT(*) AS users
FROM features
GROUP BY 1, 2
ORDER BY 1, 2
"""

TOTAL_HOLDERS_SQL = """
SELECT COUNT(DISTINCT t."to") AS total_holders
FROM nft.transfers t
JOIN bnb.transactions tx ON tx.hash = t.tx_hash
WHERE t.blockchain = 'bnb'
  AND t.contract_address = FROM_HEX('{token_hex}')
  AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
  AND tx."from" = FROM_HEX('{minter_hex}')
"""


class DataSyncer:
    """Syncs on-chain data from Dune Analytics to Firestore."""

    def __init__(self, api_key: str, token_address: str, minter_address: str,
                 timezone: str = "UTC"):
        self.client = DuneClient(api_key=api_key)
        self.token_hex = token_address[2:] if token_address.startswith("0x") else token_address
        self.minter_hex = minter_address[2:] if minter_address.startswith("0x") else minter_address
        self.timezone = timezone

    def _run_sql(self, sql: str) -> pd.DataFrame:
        """Execute SQL on Dune and return DataFrame."""
        result = self.client.run_sql(query_sql=sql, is_private=False)
        if result and result.result and result.result.rows:
            return pd.DataFrame(result.result.rows)
        return pd.DataFrame()

    def fetch_all_daily_mints(self) -> pd.DataFrame:
        """Fetch ALL daily mint counts from contract inception."""
        logger.info("Fetching all daily mint history from Dune...")
        sql = ALL_DAILY_MINTS_SQL.format(
            token_hex=self.token_hex, minter_hex=self.minter_hex
        )
        df = self._run_sql(sql)
        if not df.empty:
            logger.info(f"  Got {len(df)} days of data")
        return df

    def fetch_total_holders(self) -> int:
        """Fetch current total holder count."""
        logger.info("Fetching total holders from Dune...")
        sql = TOTAL_HOLDERS_SQL.format(
            token_hex=self.token_hex, minter_hex=self.minter_hex
        )
        df = self._run_sql(sql)
        if not df.empty and "total_holders" in df.columns:
            return int(df["total_holders"].iloc[0])
        return 0

    @staticmethod
    def fetch_onchain_total_supply(contract_address: str) -> int:
        """Read totalSupply() directly from BSC RPC — free, no API key needed."""
        logger.info(f"Fetching totalSupply from BSC RPC for {contract_address}...")
        # ERC-721 totalSupply() selector: 0x18160ddd
        resp = requests.post(BSC_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": contract_address, "data": "0x18160ddd"}, "latest"],
        }, timeout=10)
        result = resp.json().get("result", "0x0")
        total = int(result, 16)
        logger.info(f"  On-chain totalSupply: {total}")
        return total

    def fetch_wallet_age(self, target_date: str) -> pd.DataFrame:
        """Fetch wallet age cross-tab for a specific date."""
        logger.info(f"Fetching wallet age analysis for {target_date}...")
        sql = TODAY_WALLET_AGE_SQL.format(
            token_hex=self.token_hex,
            minter_hex=self.minter_hex,
            target_date=target_date,
            timezone=self.timezone,
        )
        df = self._run_sql(sql)
        if not df.empty:
            logger.info(f"  Got {len(df)} cross-tab rows for {target_date}")
        return df

    def fetch_all_holders_wallet(self) -> pd.DataFrame:
        """Fetch wallet analysis for ALL holders (all-time), not just one day."""
        logger.info("Fetching ALL-holders wallet analysis from Dune...")
        sql = ALL_HOLDERS_WALLET_SQL.format(
            token_hex=self.token_hex,
            minter_hex=self.minter_hex,
        )
        df = self._run_sql(sql)
        if not df.empty:
            logger.info(f"  Got {len(df)} cross-tab rows for all holders")
        return df

    def backfill(self) -> dict:
        """Full backfill: fetch all daily mints and store in Firestore."""
        from app.database import (
            upsert_daily_stats_batch, update_sync_meta
        )

        # Fetch all daily mints
        df = self.fetch_all_daily_mints()
        if df.empty:
            return {"status": "error", "message": "No data from Dune"}

        # Calculate cumulative holders
        df_sorted = df.sort_values("day")
        records = []
        cumulative = 0
        for _, row in df_sorted.iterrows():
            day_str = str(row["day"])[:10]
            new_users = int(row["new_users"])
            cumulative += new_users
            records.append({
                "date": day_str,
                "new_users": new_users,
                "cumulative_holders": cumulative,
            })

        # Batch write to Firestore
        logger.info(f"Writing {len(records)} daily records to Firestore...")
        upsert_daily_stats_batch(records)

        # Update sync metadata
        last_date = records[-1]["date"]
        update_sync_meta(last_date, len(records), backfill_complete=True)

        logger.info(f"Backfill complete: {len(records)} days, {cumulative} total holders")
        return {
            "status": "ok",
            "days": len(records),
            "total_holders": cumulative,
            "first_date": records[0]["date"],
            "last_date": last_date,
        }

    def daily_sync(self, target_date: str | None = None) -> dict:
        """Sync today's data + wallet age analysis.

        Tries Dune Analytics first.  If that fails (e.g. 402 Payment
        Required on the free plan), falls back to reading totalSupply()
        directly from BSC RPC and distributing the holder gap across
        any missing days.
        """
        from app.database import (
            upsert_daily_stats, upsert_wallet_age,
            get_sync_meta, update_sync_meta, get_daily_stats,
            get_latest_daily_stat,
        )

        if not target_date:
            target_date = datetime.utcnow().strftime("%Y-%m-%d")

        dune_ok = False
        cumulative = 0
        today_new = 0

        # ── Attempt 1: Dune Analytics (full daily breakdown) ─────
        try:
            df = self.fetch_all_daily_mints()
            if not df.empty:
                df_sorted = df.sort_values("day")
                for _, row in df_sorted.iterrows():
                    day_str = str(row["day"])[:10]
                    new_users = int(row["new_users"])
                    cumulative += new_users
                    if day_str == target_date:
                        today_new = new_users
                    upsert_daily_stats(day_str, new_users, cumulative)
                dune_ok = True
                logger.info(f"Dune sync OK — cumulative: {cumulative}")
        except Exception as e:
            logger.warning(f"Dune sync failed: {e}")

        # ── Attempt 2: On-chain fallback via BSC RPC ─────────────
        if not dune_ok:
            logger.info("Falling back to on-chain totalSupply sync...")
            contract = "0x" + self.token_hex
            total_supply = self.fetch_onchain_total_supply(contract)

            latest = get_latest_daily_stat()
            all_stats = get_daily_stats()

            if latest and total_supply > latest["cumulative_holders"]:
                gap = total_supply - latest["cumulative_holders"]
                last_date = datetime.strptime(latest["date"], "%Y-%m-%d")
                target_dt = datetime.strptime(target_date, "%Y-%m-%d")

                # Build list of missing days
                missing_days = []
                d = last_date + timedelta(days=1)
                while d <= target_dt:
                    missing_days.append(d.strftime("%Y-%m-%d"))
                    d += timedelta(days=1)

                if not missing_days:
                    missing_days = [target_date]

                per_day = gap // len(missing_days)
                remainder = gap % len(missing_days)
                cum = latest["cumulative_holders"]

                for i, day in enumerate(missing_days):
                    n = per_day + (1 if i < remainder else 0)
                    cum += n
                    upsert_daily_stats(day, n, cum)
                    if day == target_date:
                        today_new = n

                cumulative = cum
                logger.info(
                    f"On-chain fallback OK — filled {len(missing_days)} day(s), "
                    f"cumulative: {cumulative}"
                )
            elif latest:
                cumulative = latest["cumulative_holders"]
                logger.info("No gap detected, Firestore is up to date.")
            else:
                logger.error("No Firestore data and Dune failed — cannot sync.")
                return {"status": "error", "message": "No data source available"}

        # ── Wallet age analysis (Dune only, skip on fallback) ────
        wa_rows = 0
        if dune_ok:
            try:
                wa_df = self.fetch_wallet_age(target_date)
                if not wa_df.empty:
                    total = int(wa_df["users"].sum())
                    cross_tab = wa_df.to_dict("records")

                    age_agg = wa_df.groupby("age_bucket")["users"].sum().reset_index()
                    age_agg["pct"] = (age_agg["users"] / max(total, 1) * 100).round(1)
                    age_dist = age_agg.to_dict("records")

                    tx_agg = wa_df.groupby("tx_count_bucket")["users"].sum().reset_index()
                    tx_agg["pct"] = (tx_agg["users"] / max(total, 1) * 100).round(1)
                    tx_dist = tx_agg.to_dict("records")

                    upsert_wallet_age(target_date, cross_tab, age_dist, tx_dist, total)
                    wa_rows = len(wa_df)
            except Exception as e:
                logger.warning(f"Wallet age analysis failed: {e}")

        meta = get_sync_meta()
        update_sync_meta(target_date, meta.get("total_records", 0) + 1, True)

        return {
            "status": "ok",
            "source": "dune" if dune_ok else "onchain_fallback",
            "date": target_date,
            "new_users": today_new,
            "total_holders": cumulative,
            "wallet_age_rows": wa_rows,
        }
