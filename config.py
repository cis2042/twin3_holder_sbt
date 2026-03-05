"""
Centralized configuration for BSC Token Daily Growth Report.
Loads settings from .env file and provides defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ── Project Paths ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
REPORT_OUTPUT_DIR = Path(os.getenv("REPORT_OUTPUT_DIR", "./reports"))
REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Dune Analytics ────────────────────────────────────────────
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")

# ── Token Contract ────────────────────────────────────────────
TOKEN_CONTRACT_ADDRESS = os.getenv(
    "TOKEN_CONTRACT_ADDRESS",
    "0xe3ec133e29addfbba26a412c38ed5de37195156f",
)

# Minter address — the address that mints tokens to new users
MINTER_ADDRESS = os.getenv(
    "MINTER_ADDRESS",
    "0x344659F3Ef3c2D2A0cdF071ea13Fa87867777777",
)

# ── Analysis Settings ─────────────────────────────────────────
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Taipei")

# ── Wallet Age Buckets ────────────────────────────────────────
AGE_BUCKETS = [
    ("No history (<=1y)", None),
    ("<7d", 7),
    ("7-29d", 30),
    ("30-89d", 90),
    ("90-179d", 180),
    ("180d-1y", 365),
]

TX_COUNT_BUCKETS = [
    ("0", 0, 0),
    ("1-2", 1, 2),
    ("3-9", 3, 9),
    ("10-49", 10, 49),
    ("50-199", 50, 199),
    ("200+", 200, None),
]

# ── Schedule ──────────────────────────────────────────────────
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")  # Daily run time (HH:MM)
