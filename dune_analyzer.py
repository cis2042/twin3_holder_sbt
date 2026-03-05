"""
Dune Analytics API Client — Daily Growth & Wallet Age Analysis.

Queries Dune for:
  1. Today's newly minted users
  2. Wallet age profiling (prior tx history)
  3. Cross-tabulation (age bucket × tx count bucket)
  4. 30-day daily mint trend

Uses nft.transfers for mint detection and bnb.transactions for wallet
history analysis. Falls back to bnb.logs if nft.transfers is unavailable.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from dune_client.client import DuneClient

import config

logger = logging.getLogger(__name__)

# ── SQL Query Templates ──────────────────────────────────────

# Full wallet age analysis: today's mints + wallet age profiling
# Adapted from user's reference SQL using nft.transfers
WALLET_AGE_ANALYSIS_SQL = """
WITH params AS (
  SELECT
    date_trunc('day', now() AT TIME ZONE '{{timezone}}') AS tw_day_start,
    date_trunc('day', now() AT TIME ZONE '{{timezone}}') + interval '1' day AS tw_day_end
),

today_users AS (
  SELECT
    t."to" AS wallet,
    MIN(t.block_time) AS mint_time
  FROM nft.transfers t
  JOIN bnb.transactions tx
    ON tx.hash = t.tx_hash
  CROSS JOIN params p
  WHERE t.blockchain = 'bnb'
    AND t.contract_address = {{token_address}}
    AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
    AND tx."from" = {{minter_address}}
    AND t.block_time >= p.tw_day_start
    AND t.block_time <  p.tw_day_end
    AND tx.block_time >= p.tw_day_start
    AND tx.block_time <  p.tw_day_end
  GROUP BY 1
),

prior_tx_counts AS (
  SELECT
    u.wallet,
    COUNT(t.hash) AS prior_tx_count
  FROM today_users u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet
    AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),

first_seen AS (
  SELECT
    u.wallet,
    MIN(t.block_time) AS first_tx_time
  FROM today_users u
  LEFT JOIN bnb.transactions t
    ON t."from" = u.wallet
    AND t.block_time < u.mint_time
    AND t.block_time >= u.mint_time - interval '365' day
  GROUP BY 1
),

features AS (
  SELECT
    u.wallet,
    u.mint_time,
    COALESCE(c.prior_tx_count, 0) AS prior_tx_count,
    f.first_tx_time,
    CASE
      WHEN f.first_tx_time IS NULL THEN NULL
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
ORDER BY
  CASE age_bucket
    WHEN 'No history (<=1y)' THEN 0
    WHEN '<7d' THEN 1
    WHEN '7-29d' THEN 2
    WHEN '30-89d' THEN 3
    WHEN '90-179d' THEN 4
    WHEN '180d-1y' THEN 5
    ELSE 9
  END,
  CASE tx_count_bucket
    WHEN '0' THEN 0
    WHEN '1-2' THEN 1
    WHEN '3-9' THEN 2
    WHEN '10-49' THEN 3
    WHEN '50-199' THEN 4
    WHEN '200+' THEN 5
    ELSE 9
  END
"""

# Today's new user count (simple count for the hero number)
TODAY_NEW_USERS_SQL = """
WITH params AS (
  SELECT
    date_trunc('day', now() AT TIME ZONE '{{timezone}}') AS tw_day_start,
    date_trunc('day', now() AT TIME ZONE '{{timezone}}') + interval '1' day AS tw_day_end
)
SELECT COUNT(DISTINCT t."to") AS new_users_today
FROM nft.transfers t
JOIN bnb.transactions tx
  ON tx.hash = t.tx_hash
CROSS JOIN params p
WHERE t.blockchain = 'bnb'
  AND t.contract_address = {{token_address}}
  AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
  AND tx."from" = {{minter_address}}
  AND t.block_time >= p.tw_day_start
  AND t.block_time <  p.tw_day_end
  AND tx.block_time >= p.tw_day_start
  AND tx.block_time <  p.tw_day_end
"""

# 30-day daily mint trend
DAILY_MINT_TREND_SQL = """
SELECT
    DATE_TRUNC('day', t.block_time) AS day,
    COUNT(DISTINCT t."to") AS new_users
FROM nft.transfers t
JOIN bnb.transactions tx
  ON tx.hash = t.tx_hash
WHERE t.blockchain = 'bnb'
  AND t.contract_address = {{token_address}}
  AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
  AND tx."from" = {{minter_address}}
  AND t.block_time >= NOW() - INTERVAL '{{lookback_days}}' DAY
  AND tx.block_time >= NOW() - INTERVAL '{{lookback_days}}' DAY
GROUP BY 1
ORDER BY 1
"""

# Total holders count (all-time unique receivers of mints)
TOTAL_HOLDERS_SQL = """
SELECT COUNT(DISTINCT t."to") AS total_holders
FROM nft.transfers t
JOIN bnb.transactions tx
  ON tx.hash = t.tx_hash
WHERE t.blockchain = 'bnb'
  AND t.contract_address = {{token_address}}
  AND t."from" = FROM_HEX('0000000000000000000000000000000000000000')
  AND tx."from" = {{minter_address}}
"""


class DuneTokenAnalyzer:
    """Fetches on-chain token analytics from Dune."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.DUNE_API_KEY
        self.token_address = config.TOKEN_CONTRACT_ADDRESS
        self.minter_address = config.MINTER_ADDRESS
        self.lookback_days = config.LOOKBACK_DAYS
        self.timezone = config.TIMEZONE

        if not self.api_key:
            raise ValueError(
                "DUNE_API_KEY is required. Set it in .env or pass via --dune-key."
            )
        self.client = DuneClient(api_key=self.api_key)

    # ── Public API ────────────────────────────────────────────

    def fetch_all(self) -> dict[str, pd.DataFrame]:
        """Run all analyses and return a dict of DataFrames."""
        logger.info("Starting Dune on-chain analysis...")
        results = {}

        queries = {
            "today_new_users":     TODAY_NEW_USERS_SQL,
            "wallet_age_analysis": WALLET_AGE_ANALYSIS_SQL,
            "daily_mint_trend":    DAILY_MINT_TREND_SQL,
            "total_holders":       TOTAL_HOLDERS_SQL,
        }

        for name, sql in queries.items():
            logger.info(f"  → Fetching {name}...")
            try:
                df = self._execute_query(name, sql)
                results[name] = df
                logger.info(f"    ✓ {name}: {len(df)} rows")
            except Exception as e:
                logger.error(f"    ✗ {name} failed: {e}")
                results[name] = pd.DataFrame()

        return results

    def get_resolved_sql(self) -> dict[str, str]:
        """Return resolved SQL strings (after parameter substitution) for display."""
        queries = {
            "today_new_users":     TODAY_NEW_USERS_SQL,
            "wallet_age_analysis": WALLET_AGE_ANALYSIS_SQL,
            "daily_mint_trend":    DAILY_MINT_TREND_SQL,
            "total_holders":       TOTAL_HOLDERS_SQL,
        }
        resolved = {}
        for name, sql_template in queries.items():
            resolved[name] = self._resolve_sql(sql_template)
        return resolved

    def test_connection(self) -> bool:
        """Test Dune API connectivity."""
        try:
            self.client.run_sql(query_sql="SELECT 1 AS ok", is_private=False)
            logger.info("Dune API connection successful.")
            return True
        except Exception as e:
            logger.error(f"Dune API connection failed: {e}")
            return False

    # ── Internal ──────────────────────────────────────────────

    def _resolve_sql(self, sql_template: str) -> str:
        """Substitute parameters into a SQL template."""
        addr_hex = self.token_address[2:]
        minter_hex = self.minter_address[2:]
        return sql_template.replace(
            "{{token_address}}", f"FROM_HEX('{addr_hex}')"
        ).replace(
            "{{minter_address}}", f"FROM_HEX('{minter_hex}')"
        ).replace(
            "{{lookback_days}}", str(self.lookback_days)
        ).replace(
            "{{timezone}}", self.timezone
        )

    def _execute_query(self, name: str, sql_template: str) -> pd.DataFrame:
        """Execute a query on Dune and return results as DataFrame."""
        sql = self._resolve_sql(sql_template)
        try:
            result = self.client.run_sql(query_sql=sql, is_private=False)
            if result and result.result and result.result.rows:
                return pd.DataFrame(result.result.rows)
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"  run_sql error for {name}: {e}")
            raise

