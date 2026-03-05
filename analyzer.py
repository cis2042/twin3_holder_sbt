"""
Analysis Engine — Daily Growth & Wallet Age Insights.

Processes Dune query results and generates structured data
for the scrollytelling report, including auto-generated
English narratives for each section.
"""

import logging
from datetime import datetime

import pandas as pd

import config

logger = logging.getLogger(__name__)


class Analyzer:
    """Analyzes on-chain data and generates growth insights."""

    # Plain-English formula descriptions for each metric
    FORMULA_DESCRIPTIONS = {
        "today_new_users": (
            "Count of distinct wallet addresses that received a mint (NFT transfer from 0x0) "
            "via the minter address, within today's date window (midnight-to-midnight in the configured timezone)."
        ),
        "wallet_age_analysis": (
            "For each of today's new users, look back up to 1 year for prior transactions from that wallet. "
            "Classify by: (a) wallet age = days since first prior tx, bucketed into 6 ranges; "
            "(b) prior tx count, bucketed into 6 ranges. Cross-tabulate to produce a user-count matrix."
        ),
        "daily_mint_trend": (
            "For each day in the lookback window, count distinct wallets that received a mint. "
            "7-day moving average change = (avg of last 7 days − avg of prior 7 days) / avg of prior 7 days × 100%."
        ),
        "total_holders": (
            "Count of all-time distinct wallet addresses that have ever received a mint from the minter."
        ),
    }

    def __init__(self, dune_data: dict[str, pd.DataFrame], sql_queries: dict[str, str] | None = None):
        self.dune = dune_data
        self.sql_queries = sql_queries or {}
        self.report_date = datetime.now().strftime("%Y-%m-%d")

    def run(self) -> dict:
        """Run full analysis and return structured report data."""
        logger.info("Running growth analysis...")

        report = {
            "report_date": self.report_date,
            "generated_at": datetime.now().isoformat(),
            "contract_address": config.TOKEN_CONTRACT_ADDRESS,
            "minter_address": config.MINTER_ADDRESS,
            "lookback_days": config.LOOKBACK_DAYS,
            "today": self._analyze_today(),
            "wallet_age": self._analyze_wallet_age(),
            "trend": self._analyze_trend(),
            "insights": [],
            "formulas": {
                name: {
                    "sql": self.sql_queries.get(name, ""),
                    "description": desc,
                }
                for name, desc in self.FORMULA_DESCRIPTIONS.items()
            },
        }

        report["insights"] = self._generate_insights(report)
        logger.info(f"Analysis complete. Generated {len(report['insights'])} insights.")
        return report

    # ── Today's Snapshot ──────────────────────────────────────

    def _analyze_today(self) -> dict:
        """Analyze today's new user metrics."""
        result = {"new_users": 0, "total_holders": 0}

        # Today's new users
        df = self.dune.get("today_new_users", pd.DataFrame())
        if not df.empty and "new_users_today" in df.columns:
            result["new_users"] = int(df["new_users_today"].iloc[0])

        # Total holders
        df_total = self.dune.get("total_holders", pd.DataFrame())
        if not df_total.empty and "total_holders" in df_total.columns:
            result["total_holders"] = int(df_total["total_holders"].iloc[0])

        # Narrative
        n = result["new_users"]
        t = result["total_holders"]
        if n > 0:
            result["narrative"] = (
                f"Today we onboarded {n:,} new wallets, "
                f"bringing our total holder base to {t:,}. "
            )
        else:
            result["narrative"] = (
                f"No new mints were recorded today (as of this report's generation time). "
                f"Total holder base stands at {t:,}."
            )

        return result

    # ── Wallet Age Analysis ───────────────────────────────────

    def _analyze_wallet_age(self) -> dict:
        """Analyze wallet age distribution of today's new users."""
        result = {
            "age_distribution": [],
            "tx_distribution": [],
            "cross_tab": [],
            "narrative": "",
        }

        df = self.dune.get("wallet_age_analysis", pd.DataFrame())
        if df.empty:
            result["narrative"] = "No wallet age data available for today's mints."
            return result

        total_users = int(df["users"].sum())

        # Age bucket aggregation
        age_agg = df.groupby("age_bucket")["users"].sum().reset_index()
        age_agg["pct"] = (age_agg["users"] / max(total_users, 1) * 100).round(1)
        result["age_distribution"] = age_agg.to_dict("records")

        # Tx count bucket aggregation
        tx_agg = df.groupby("tx_count_bucket")["users"].sum().reset_index()
        tx_agg["pct"] = (tx_agg["users"] / max(total_users, 1) * 100).round(1)
        result["tx_distribution"] = tx_agg.to_dict("records")

        # Full cross-tab
        result["cross_tab"] = df.to_dict("records")

        # Find dominant age bucket
        if not age_agg.empty:
            top_age = age_agg.loc[age_agg["users"].idxmax()]
            no_history = age_agg[age_agg["age_bucket"] == "No history (<=1y)"]
            no_history_pct = float(no_history["pct"].iloc[0]) if not no_history.empty else 0

            # Find dominant tx bucket
            top_tx = tx_agg.loc[tx_agg["users"].idxmax()] if not tx_agg.empty else None

            narrative_parts = []
            narrative_parts.append(
                f"Of today's {total_users:,} new users, "
                f"{no_history_pct:.0f}% are brand-new wallets with no prior transaction history "
                f"in the past year."
            )

            if no_history_pct < 50:
                narrative_parts.append(
                    f"The majority ({100 - no_history_pct:.0f}%) are existing wallet users, "
                    f"suggesting organic adoption from the broader crypto community."
                )
            else:
                narrative_parts.append(
                    "This high proportion of fresh wallets may indicate "
                    "new-to-crypto users or purpose-created wallets for this token."
                )

            if top_tx is not None:
                narrative_parts.append(
                    f"The most common prior activity level is '{top_tx['tx_count_bucket']}' "
                    f"transactions ({top_tx['pct']:.0f}% of new users)."
                )

            result["narrative"] = " ".join(narrative_parts)

        return result

    # ── 30-Day Trend ──────────────────────────────────────────

    def _analyze_trend(self) -> dict:
        """Analyze 30-day daily mint trend."""
        result = {"timeline": [], "narrative": "", "stats": {}}

        df = self.dune.get("daily_mint_trend", pd.DataFrame())
        if df.empty:
            result["narrative"] = "No trend data available."
            return result

        df_sorted = df.sort_values("day")
        result["timeline"] = df_sorted.to_dict("records")

        # Stats
        avg_daily = float(df_sorted["new_users"].mean())
        total_period = int(df_sorted["new_users"].sum())
        max_day = df_sorted.loc[df_sorted["new_users"].idxmax()]
        min_day = df_sorted.loc[df_sorted["new_users"].idxmin()]

        result["stats"] = {
            "avg_daily": round(avg_daily, 1),
            "total_period": total_period,
            "max_day": str(max_day["day"]),
            "max_value": int(max_day["new_users"]),
            "min_day": str(min_day["day"]),
            "min_value": int(min_day["new_users"]),
        }

        # 7-day trend comparison
        if len(df_sorted) >= 14:
            recent_7 = df_sorted["new_users"].iloc[-7:].mean()
            prev_7 = df_sorted["new_users"].iloc[-14:-7].mean()
            if prev_7 > 0:
                change_pct = (recent_7 - prev_7) / prev_7 * 100
                result["stats"]["7d_change_pct"] = round(change_pct, 1)

                direction = "up" if change_pct > 0 else "down"
                result["narrative"] = (
                    f"Over the past {config.LOOKBACK_DAYS} days, we averaged "
                    f"{avg_daily:,.0f} new users per day ({total_period:,} total). "
                    f"The 7-day moving average is {direction} {abs(change_pct):.1f}% "
                    f"compared to the prior week. "
                    f"Peak day was {str(max_day['day'])[:10]} with {int(max_day['new_users']):,} mints."
                )
            else:
                result["narrative"] = (
                    f"Over the past {config.LOOKBACK_DAYS} days, we averaged "
                    f"{avg_daily:,.0f} new users per day ({total_period:,} total)."
                )
        else:
            result["narrative"] = (
                f"Over the available period, we averaged "
                f"{avg_daily:,.0f} new users per day ({total_period:,} total)."
            )

        return result

    # ── Insight Generation ────────────────────────────────────

    def _generate_insights(self, report: dict) -> list[dict]:
        """Generate actionable English insights."""
        insights = []
        today = report.get("today", {})
        wallet_age = report.get("wallet_age", {})
        trend = report.get("trend", {})

        # 1. Today's performance
        new_users = today.get("new_users", 0)
        avg_daily = trend.get("stats", {}).get("avg_daily", 0)
        if new_users > 0 and avg_daily > 0:
            ratio = new_users / avg_daily
            if ratio > 1.5:
                insights.append({
                    "type": "positive",
                    "title": "Above-Average Day",
                    "detail": (
                        f"Today's {new_users:,} new users is "
                        f"{ratio:.1f}x the 30-day average ({avg_daily:,.0f}). "
                        f"Consider investigating what's driving the spike."
                    ),
                })
            elif ratio < 0.5:
                insights.append({
                    "type": "warning",
                    "title": "Below-Average Day",
                    "detail": (
                        f"Today's {new_users:,} new users is only "
                        f"{ratio:.1f}x the 30-day average ({avg_daily:,.0f}). "
                        f"May warrant a check on minting pipeline or marketing efforts."
                    ),
                })

        # 2. Wallet age quality
        age_dist = wallet_age.get("age_distribution", [])
        for bucket in age_dist:
            if bucket.get("age_bucket") == "No history (<=1y)" and bucket.get("pct", 0) > 70:
                insights.append({
                    "type": "info",
                    "title": "High Proportion of New Wallets",
                    "detail": (
                        f"{bucket['pct']:.0f}% of today's new users have no prior "
                        f"transaction history. This could indicate bot activity "
                        f"or genuinely new crypto users."
                    ),
                })
                break
            elif bucket.get("age_bucket") in ("90-179d", "180d-1y") and bucket.get("pct", 0) > 30:
                insights.append({
                    "type": "positive",
                    "title": "Attracting Experienced Users",
                    "detail": (
                        f"{bucket['pct']:.0f}% of today's new users have wallets "
                        f"older than 90 days, suggesting genuine crypto-native adoption."
                    ),
                })
                break

        # 3. Growth trajectory
        change_pct = trend.get("stats", {}).get("7d_change_pct")
        if change_pct is not None:
            if change_pct > 20:
                insights.append({
                    "type": "positive",
                    "title": "Strong Growth Momentum",
                    "detail": (
                        f"7-day average is up {change_pct:.1f}% vs prior week. "
                        f"Growth is accelerating."
                    ),
                })
            elif change_pct < -20:
                insights.append({
                    "type": "warning",
                    "title": "Growth Deceleration",
                    "detail": (
                        f"7-day average is down {abs(change_pct):.1f}% vs prior week. "
                        f"May need to review acquisition channels."
                    ),
                })

        return insights
