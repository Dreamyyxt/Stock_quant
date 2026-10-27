"""
Global configuration settings for the quantitative trading system.

Credentials are read from the environment first (optionally via config/.env),
so the repository never has to contain live secrets. There are no literal
credential fallbacks in this file — this repository is public, so tiger id and
account number must be supplied via the environment. core.tiger_client raises
when either is missing instead of dialling out with an empty account.

Environment variables
---------------------
  TRADING_ENV              paper | live            (default: paper)
  TIGER_ID                 developer tiger id
  TIGER_ACCOUNT            account number for the selected environment
  TIGER_PRIVATE_KEY_PATH   path to the RSA private key (default config/private_key.pem)
  TIGER_PRIVATE_KEY        inline RSA private key (alternative to the file)
  TIGER_SANDBOX            true to use Tiger's sandbox endpoint (default false)
  LIVE_TRADING_CONFIRM     required to trade a live account, see LIVE_CONFIRM_TOKEN
  ALERT_WEBHOOK_URL        optional webhook for alerts

Switching to live requires ALL of:
  1. TRADING_ENV=live
  2. TIGER_ACCOUNT set to the live account number
  3. LIVE_TRADING_CONFIRM=<LIVE_CONFIRM_TOKEN>
See docs/LIVE_READINESS.md for the full pre-flight checklist.
"""

import os
import pathlib

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_dotenv(path: pathlib.Path) -> None:
    """Minimal .env loader (KEY=VALUE), no external dependency, never overrides."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(_PROJECT_ROOT / "config" / ".env")

# ============================================================
# TRADING ENVIRONMENT
# ============================================================
# "paper" = Tiger paper account, "live" = real money.
TRADING_ENV = os.getenv("TRADING_ENV", "paper").strip().lower()
LIVE_CONFIRM_TOKEN = "I_UNDERSTAND_LIVE_CAPITAL_IS_AT_RISK"
LIVE_CONFIRM_ENV = os.getenv("LIVE_TRADING_CONFIRM", "").strip()


def is_live() -> bool:
    return TRADING_ENV == "live"


def live_trading_allowed() -> bool:
    """True only when the environment AND the explicit confirmation agree."""
    return is_live() and LIVE_CONFIRM_ENV == LIVE_CONFIRM_TOKEN


# ============================================================
# TIGER SECURITIES API CREDENTIALS
# ============================================================
TIGER_CONFIG = {
    # No literal fallbacks on purpose: this repository is public, so a real
    # developer id / account number must never live in source. They come from
    # the environment (or config/.env), and core.tiger_client fails fast when
    # either is missing rather than dialling out with an empty account.
    "tiger_id": os.getenv("TIGER_ID", ""),
    "account": os.getenv("TIGER_ACCOUNT", ""),
    "private_key_path": os.getenv(
        "TIGER_PRIVATE_KEY_PATH", str(_PROJECT_ROOT / "config" / "private_key.pem")
    ),
    "private_key": os.getenv("TIGER_PRIVATE_KEY", ""),
    "sandbox": os.getenv("TIGER_SANDBOX", "false").lower() in ("1", "true", "yes"),
    "env": TRADING_ENV,
}

# ============================================================
# TRADING UNIVERSE - US Stocks
# ============================================================
# Core liquid large/mid-cap universe for momentum/quality screening
CORE_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "GOOGL", "META", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD",
    "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "FTNT", "PANW", "SNOW", "DDOG",
    # Healthcare
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "ISRG", "VRTX",
    "REGN", "AMGN", "BIIB", "MRNA", "ILMN", "IDXX", "MTD", "BIO", "HOLX",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "BLK", "SPGI", "MCO", "ICE", "CME",
    "V", "MA", "PYPL", "AXP", "COF",
    # Consumer
    "AMZN", "TSLA", "HD", "MCD", "SBUX", "NKE", "LOW", "TGT", "COST", "TJX",
    # Industrials
    "HON", "GE", "MMM", "CAT", "DE", "UPS", "FDX", "LMT", "RTX", "NOC",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "PSX", "VLO", "MPC",
    # Utilities/REITs
    "NEE", "DUK", "AMT", "PLD", "EQIX",
    # Communications
    "T", "VZ", "NFLX", "DIS", "CMCSA",
]

# Undervalued growth stock candidates - less crowded, strong fundamentals
# These are periodically screened; this list is a starting seed
GROWTH_UNIVERSE = [
    # Software/Cloud
    "GTLB", "ESTC", "CFLT", "MDB", "MNDY", "HUBS", "BILL", "ZI", "VEEV",
    "PCTY", "PAYC", "TOST", "ASAN", "APPF", "DOCN", "SMAR",
    # Healthcare/Biotech
    "CRVL", "ACAD", "INSP", "OMCL", "PRVA", "GDRX", "PHR", "SDGR",
    "RXRX", "PACB", "EXAS", "NTRA",
    # Financials/Fintech
    "SOFI", "AFRM", "UPST", "LPLA", "VOYA", "RKT", "UWMC",
    # Industrials/Cleantech
    "ENPH", "SEDG", "FSLR", "ARRY", "RUN", "NOVA", "SHLS",
    "GNRC", "GTLS",
    # Consumer/E-commerce
    "ETSY", "CHWY", "SFIX", "PRPL", "RVLV", "CPNG",
    # Semiconductors (smaller)
    "ONTO", "ACLS", "UCTT", "AMBA", "FORM", "MTSI",
    # Other
    "AXON", "CELH", "MGNI", "TTD", "CIEN", "VIAV",
]

# ============================================================
# SECTOR MAP
# Used by the sector-concentration limit. Without this the validator
# buckets every symbol into "Unknown" and the 35% sector cap degrades
# into a 35% whole-portfolio cap, which silently liquidates the book.
# ============================================================
SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "META": "Technology", "NVDA": "Technology", "AVGO": "Technology",
    "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology",
    "AMD": "Technology", "QCOM": "Technology", "TXN": "Technology",
    "AMAT": "Technology", "LRCX": "Technology", "KLAC": "Technology",
    "MRVL": "Technology", "FTNT": "Technology", "PANW": "Technology",
    "SNOW": "Technology", "DDOG": "Technology", "INTC": "Technology",
    "GTLB": "Technology", "ESTC": "Technology", "CFLT": "Technology",
    "MDB": "Technology", "MNDY": "Technology", "HUBS": "Technology",
    "BILL": "Technology", "ZI": "Technology", "VEEV": "Technology",
    "PCTY": "Technology", "PAYC": "Technology", "TOST": "Technology",
    "ASAN": "Technology", "APPF": "Technology", "DOCN": "Technology",
    "SMAR": "Technology", "ONTO": "Technology", "ACLS": "Technology",
    "UCTT": "Technology", "AMBA": "Technology", "FORM": "Technology",
    "MTSI": "Technology", "CIEN": "Technology", "VIAV": "Technology",
    "MGNI": "Technology", "TTD": "Technology",

    "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "DHR": "Healthcare", "ISRG": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare", "AMGN": "Healthcare",
    "BIIB": "Healthcare", "MRNA": "Healthcare", "ILMN": "Healthcare",
    "IDXX": "Healthcare", "MTD": "Healthcare", "BIO": "Healthcare",
    "HOLX": "Healthcare", "PFE": "Healthcare", "CRVL": "Healthcare",
    "ACAD": "Healthcare", "INSP": "Healthcare", "OMCL": "Healthcare",
    "PRVA": "Healthcare", "GDRX": "Healthcare", "PHR": "Healthcare",
    "SDGR": "Healthcare", "RXRX": "Healthcare", "PACB": "Healthcare",
    "EXAS": "Healthcare", "NTRA": "Healthcare",

    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "BLK": "Financials",
    "SPGI": "Financials", "MCO": "Financials", "ICE": "Financials",
    "CME": "Financials", "V": "Financials", "MA": "Financials",
    "PYPL": "Financials", "AXP": "Financials", "COF": "Financials",
    "SOFI": "Financials", "AFRM": "Financials", "UPST": "Financials",
    "LPLA": "Financials", "VOYA": "Financials", "RKT": "Financials",
    "UWMC": "Financials",

    "AMZN": "Consumer", "TSLA": "Consumer", "HD": "Consumer",
    "MCD": "Consumer", "SBUX": "Consumer", "NKE": "Consumer",
    "LOW": "Consumer", "TGT": "Consumer", "COST": "Consumer",
    "TJX": "Consumer", "ETSY": "Consumer", "CHWY": "Consumer",
    "SFIX": "Consumer", "PRPL": "Consumer", "RVLV": "Consumer",
    "CPNG": "Consumer", "CELH": "Consumer",

    "HON": "Industrials", "GE": "Industrials", "MMM": "Industrials",
    "CAT": "Industrials", "DE": "Industrials", "UPS": "Industrials",
    "FDX": "Industrials", "LMT": "Industrials", "RTX": "Industrials",
    "NOC": "Industrials", "ENPH": "Industrials", "SEDG": "Industrials",
    "FSLR": "Industrials", "ARRY": "Industrials", "RUN": "Industrials",
    "NOVA": "Industrials", "SHLS": "Industrials", "GNRC": "Industrials",
    "GTLS": "Industrials", "AXON": "Industrials",

    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "EOG": "Energy",
    "SLB": "Energy", "PSX": "Energy", "VLO": "Energy", "MPC": "Energy",

    "NEE": "Utilities", "DUK": "Utilities",
    "AMT": "RealEstate", "PLD": "RealEstate", "EQIX": "RealEstate",

    "T": "Communication", "VZ": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication",

    "BABA": "ChinaADR",
}

# ============================================================
# FIXED PORTFOLIO ALLOCATION
# Set use_fixed_portfolio=True to use this instead of dynamic signals.
# Weights must sum to 1.0.
# ============================================================
FIXED_PORTFOLIO = {
    "use_fixed_portfolio": True,

    # Core bucket — high-conviction large caps (60%)
    "core": {
        "MSFT":  0.20,   # Microsoft — cloud + AI dominance
        "GOOGL": 0.20,   # Alphabet — search + cloud + AI undervalued vs MSFT
        "AAPL":  0.20,   # Apple — ecosystem moat, services growth
    },

    # Satellite bucket — tactical positions (40%)
    "satellite": {
        "NVDA":  0.10,   # AI chip cycle leader
        "AMD":   0.10,   # CPU/GPU — gaining vs Intel, AI exposure
        "BABA":  0.08,   # Alibaba — deep value, China recovery optionality
        "INTC":  0.06,   # Intel — turnaround/recovery play, ~0.7x book
        "PFE":   0.06,   # Pfizer — dividend yield ~6%, pipeline re-rating
    },

    # Rebalancing trigger: only trade if drift > this threshold
    "rebalance_threshold": 0.05,   # 5% drift triggers rebalance
}

# ============================================================
# STRATEGY PARAMETERS (used when use_fixed_portfolio=False)
# ============================================================
STRATEGY_CONFIG = {
    # Portfolio composition
    "max_positions": 20,
    "core_positions": 14,
    "growth_positions": 6,
    "min_position_size": 0.02,
    "max_position_size": 0.12,
    "max_sector_weight": 0.35,

    # Rebalancing
    "rebalance_frequency": "daily",     # "daily", "weekly", "monthly"
    "rebalance_day": 1,
    # 09:40 ET — deliberately NOT 09:30. The quote feed is 15-minute delayed,
    # so pricing an order off the previous close at the opening print produced
    # limit orders that could not fill. Give the open 10 minutes to settle.
    "rebalance_time": "09:40",
    "min_rebalance_threshold": 0.05,

    # Momentum parameters (12-1 month lookback, skip last month)
    "momentum_lookback_months": 12,
    "momentum_skip_months": 1,
    "momentum_weight": 0.40,

    # Quality parameters
    "quality_weight": 0.30,
    "min_roe": 0.10,
    "max_debt_to_equity": 2.0,

    # Value parameters
    "value_weight": 0.30,
    "max_peg_ratio": 2.5,
    "max_pe_ratio": 60,

    # Growth stock screening
    "min_revenue_growth": 0.12,
    "min_gross_margin": 0.30,
    "max_peg_growth": 1.5,
    "max_institutional_ownership": 0.85,
}

# ============================================================
# RISK MANAGEMENT PARAMETERS
# ============================================================
RISK_CONFIG = {
    # Stop losses
    "position_stop_loss": -0.15,        # Exit if position falls 15% from entry
    "trailing_stop_pct": 0.12,          # 12% trailing stop from peak

    # After a stop-out, block re-entry for N calendar days. Without this the
    # same-cycle rebalance immediately buys the name back and the stop-out is
    # a pure cost (see 2026-06-29 BABA in logs/trade_events.jsonl).
    "stop_cooldown_days": 10,

    # Portfolio-level risk
    "max_portfolio_drawdown": -0.20,    # Reduce exposure if portfolio drawdown > 20%
    "drawdown_reduce_to": 0.50,         # Reduce to 50% invested when triggered

    # Volatility targeting
    "target_volatility": 0.18,
    "vol_lookback_days": 60,

    # Position sizing method: "equal", "vol_parity", "factor_weighted"
    "sizing_method": "factor_weighted",

    # Kelly fraction (fraction of Kelly criterion to use; < 1 for safety)
    "kelly_fraction": 0.25,
}

# ============================================================
# EXECUTION PARAMETERS
# ============================================================
EXECUTION_CONFIG = {
    "order_type": "LMT",                # "LMT"/"LIMIT" or "MKT"/"MARKET"
    "limit_slippage_pct": 0.002,        # 0.2% through the reference price
    "order_timeout_minutes": 30,        # Cancel unfilled orders after 30 min
    "max_daily_trades": 30,

    # Execution window (US Eastern). Orders outside this window are refused
    # rather than fired into an illiquid pre-market.
    "trade_at_open": False,
    "trade_start_time": "09:40",
    "trade_end_time": "15:45",
    "enforce_trading_window": True,

    # Market-status check. Fail-closed: if the broker status endpoint errors we
    # skip trading instead of assuming the market is open.
    "market_check_fail_open": os.getenv("MARKET_CHECK_FAIL_OPEN", "0") == "1",

    # Fill confirmation: never assume a submitted order is a filled order.
    "require_fill_before_buy": True,    # wait for sell fills before spending cash
    "fill_timeout_seconds": 120,
    "fill_poll_interval_seconds": 3,
    "max_order_retries": 1,             # cancel + re-place once at a fresh price

    # Cash / sizing guards
    "cash_buffer_pct": 0.02,            # keep 2% of buying power untouched
    "min_order_notional": 200.0,        # skip dust orders
    "max_order_notional_pct": 0.35,     # single order ≤ 35% of buying power
    "allow_sell_proceeds_reuse": True,  # US margin: proceeds are tradable at once

    # Duplicate protection
    "block_repeat_symbol_action_days": 1,   # 0 disables
    "max_price_deviation_pct": 0.10,        # abort if ref price vs broker last > 10%
}

# ============================================================
# INTRADAY TRADING
# Long-only, day-trade only (flat before the close). Signals are the existing
# factor family compressed to minute bars: short-window momentum, minute RSI,
# VWAP deviation, relative volume.
# ============================================================
INTRADAY_CONFIG = {
    # Liquid, tight-spread names only. Intraday P&L is dominated by spread and
    # slippage, so illiquid small caps are excluded by default.
    "universe": ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL"],

    # Data
    "provider": os.getenv("INTRADAY_PROVIDER", "auto"),   # auto|tiger|yfinance|replay|synthetic
    "bar_interval": "1min",
    "lookback_bars": 90,
    "poll_seconds": 20,
    "min_bars_for_signal": 35,

    # Position sizing
    "max_positions": 3,
    "position_pct": 0.12,            # of net liquidation per position
    "max_gross_exposure": 0.45,      # never more than 45% invested intraday
    "min_order_notional": 200.0,

    # Which signal engine to run (see intraday/signals.py::make_signals)
    #   orb_breakout   opening-range breakout + volume/momentum confirmation
    #   momentum_rank  the original ranked-momentum score (kept for comparison)
    "signal_mode": "orb_breakout",

    # Opening-range breakout parameters
    "opening_range_minutes": 30,     # range = first 30 bars, fixed for the day
    "orb_min_volume": 1.5,           # relative volume vs its own 60-bar norm
    "orb_min_momentum": 0.005,       # 15-minute momentum floor
    "orb_rsi_max": 80.0,
    # Exits. Measured on 112 sessions: exiting on a small momentum reversal cut
    # the positive tail and turned +1.16% into -1.29%. The edge is the 2-hour
    # hold, so the exit is time-based and the momentum rule is only a disaster
    # brake (a real breakdown, not noise).
    "orb_exit_momentum": -0.020,
    "orb_exit_vwap": -0.020,

    # Signal thresholds (see intraday/signals.py)
    "entry_score": 0.72,
    "exit_score": 0.35,
    "rsi_entry_max": 76.0,
    "rsi_exit": 80.0,
    # A 15-minute momentum of +0.02% is noise, not a trend. Requiring a real
    # move, and exiting only on a clearly negative one, is what stops the engine
    # from paying the spread twice a minute.
    "min_momentum_entry": 0.0012,    # +0.12% over 15 minutes
    "momentum_exit": -0.0008,        # exit below -0.08%, not at exactly 0

    # Exits
    "min_hold_minutes": 120,         # the edge IS the 2-hour hold; no early signal exits
    # 1% is ~1 standard deviation of a 2-hour move on these names, so it was
    # getting hit by noise alone (30 of 173 trades). 2% keeps the tail protected
    # without competing with normal fluctuation.
    "stop_loss_pct": 0.020,
    "take_profit_pct": 0.025,        # 2.5% target (breakouts need room)
    "max_hold_minutes": 120,         # forced time exit — this is the profit taker
    "symbol_cooldown_minutes": 30,   # after an exit, wait before re-entering

    # Session windows (US Eastern)
    # Entries start once the opening range exists (10:00) and stop early enough
    # that a 2-hour hold still fits before the 15:50 flatten.
    "entry_window": ["10:05", "13:40"],
    "flatten_time": "15:50",         # force flat
    "no_new_entry_after": "13:40",  # must leave room for the exit before flatten

    # Intraday risk limits
    "max_daily_loss_pct": 0.015,     # halt + flatten for the day
    "max_trades_per_day": 12,
    "max_round_trips_per_symbol": 3,

    # Execution
    # Fallback slippage only. Real per-symbol values come from
    # state/cost_model.json (scripts/measure_spread.py) and are what the replay
    # actually charges. These defaults are deliberately *above* the measured
    # universe half-spread (~0.012% median / 0.025% p95) so a symbol missing
    # from the model is costed pessimistically, never optimistically.
    "limit_slippage_pct": 0.0003,
    "flatten_slippage_pct": 0.0006,
    "cost_model_percentile": "p95",   # median | p95 — which spread to charge
    "commission_pct": 0.0,            # Alpaca equities: commission free
    "fill_timeout_seconds": 45,
}

# ============================================================
# TSLA OPENING-RANGE BREAKOUT — SINGLE NAME
# ============================================================
# The only rule that survived both halves of a five-year minute-bar sample on
# TSLA (see tsla_breakout_backtest.py). Every value below is the value that was
# validated; changing one means the backtest is no longer evidence for it.
#
# Why 100% of equity in one name: the edge is ~18 bp per trade and the spread
# costs ~4 bp of it. Size is the only way that combination compounds, and it is
# also why the drawdown is -27% rather than -3%. This is a deliberate trade.
ORB_TSLA_CONFIG = {
    **INTRADAY_CONFIG,

    "strategy_name": "TSLA ORB",
    "universe": ["TSLA"],
    "signal_mode": "orb_tsla",
    "lookback_bars": 120,            # must cover the 30-bar range plus the window

    # Direction and sizing
    "allow_short": True,             # half the sample's trades were shorts
    "max_positions": 1,
    "position_pct": 0.995,           # leaves room for the entry half-spread
    "max_gross_exposure": 1.0,

    # The rule itself
    "session_open": "09:30",
    "orb_tsla_range_minutes": 30,    # 09:30–09:59, fixed for the day
    "orb_tsla_stop_mult": 2.0,       # stop = range height × 2
    "orb_tsla_last_entry": "10:30",  # last signal *bar*; the fill is at 10:31

    # Exits: hold to the close. A target or an early time exit was tested and
    # cut the return roughly in half, so both are disabled on purpose.
    "min_hold_minutes": 0,
    "stop_loss_pct": 0.030,          # fallback only; the range stop wins
    "take_profit_pct": 0.0,          # 0 = no target
    "max_hold_minutes": 500,         # longer than any session, so it never binds
    "symbol_cooldown_minutes": 1,

    # Session windows (US Eastern). The entry window ends one minute after the
    # last signal bar because the engine acts on *completed* bars.
    "entry_window": ["10:00", "10:31"],
    "no_new_entry_after": "10:31",
    "flatten_time": "16:00",         # the 15:59 close is the exit

    # Risk limits
    "max_daily_loss_pct": 0.10,      # circuit breaker the backtest does NOT have
    "max_trades_per_day": 1,
    "max_round_trips_per_symbol": 1,
    "alert_on_trade_cap": False,     # one-trade-per-day is normal, not an alert

    # Cost, pinned to the figure the backtest was validated with:
    # half spread 1.51 bp (state/tsla_cost.json) + 0.5 bp of impact per side.
    "cost_model_percentile": "median",
    "cost_override_half_spread_bp": {"TSLA": 1.5077},
    "slippage_extra_bp": 0.5,
}

INTRADAY_CONFIGS = {
    "orb_breakout": INTRADAY_CONFIG,
    "orb_tsla": ORB_TSLA_CONFIG,
}


def intraday_config(name: str = None) -> dict:
    """Config by strategy key, falling back to the multi-name default."""
    key = str(name or os.getenv("INTRADAY_STRATEGY", "orb_breakout")).lower()
    return dict(INTRADAY_CONFIGS.get(key, INTRADAY_CONFIG))

# ============================================================
# LOGGING & MONITORING
# ============================================================
LOG_CONFIG = {
    "log_dir": "logs",
    "log_level": "INFO",
    "log_to_file": True,
    "log_to_console": True,
    "log_rotate_when": "midnight",
    "log_backup_count": 30,
    "performance_report_frequency": "weekly",
}

# ============================================================
# RUNTIME STATE
# ============================================================
STATE_DIR = os.getenv("STATE_DIR", "state")
