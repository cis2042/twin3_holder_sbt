"""
Main entry point — BSC Token Daily Growth Report.

Pipeline: Dune API → Analysis → Scrollytelling Report
"""

import argparse
import logging
import sys
from datetime import datetime

import config
from dune_analyzer import DuneTokenAnalyzer
from analyzer import Analyzer
from report_generator import ReportGenerator

# ── Logging Setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)-18s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            config.PROJECT_ROOT / "logs" / f"run_{datetime.now():%Y%m%d}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main")


def run_analysis(lookback_days: int | None = None) -> str:
    """Execute the full analysis pipeline."""

    if lookback_days:
        config.LOOKBACK_DAYS = lookback_days

    logger.info("=" * 60)
    logger.info("BSC Token Daily Growth Report")
    logger.info(f"  Contract: {config.TOKEN_CONTRACT_ADDRESS}")
    logger.info(f"  Minter:   {config.MINTER_ADDRESS}")
    logger.info(f"  Lookback: {config.LOOKBACK_DAYS} days")
    logger.info(f"  Timezone: {config.TIMEZONE}")
    logger.info("=" * 60)

    # ── Step 1: Fetch on-chain data ───────────────────────────
    logger.info("\n[1/3] Fetching on-chain data from Dune Analytics...")
    try:
        dune = DuneTokenAnalyzer()
        dune_data = dune.fetch_all()
    except ValueError as e:
        logger.error(f"Dune initialization failed: {e}")
        logger.error("Cannot proceed without Dune API key. Exiting.")
        sys.exit(1)

    # ── Step 2: Run analysis ──────────────────────────────────
    logger.info("\n[2/3] Running growth analysis...")
    sql_queries = dune.get_resolved_sql()
    analyzer = Analyzer(dune_data, sql_queries=sql_queries)
    report_data = analyzer.run()

    # ── Step 3: Generate report ───────────────────────────────
    logger.info("\n[3/3] Generating scrollytelling report...")
    generator = ReportGenerator()
    report_path = generator.generate(report_data)

    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ Report generated: {report_path}")
    logger.info("=" * 60)

    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="BSC Token Daily Growth Report"
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Override lookback period (default: from .env or 30)",
    )
    args = parser.parse_args()

    report_path = run_analysis(lookback_days=args.lookback_days)

    # Print the report URL for easy access
    print(f"\n📊 Report: file://{config.PROJECT_ROOT / report_path}")


if __name__ == "__main__":
    main()
