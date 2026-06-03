# DiCarlo BX Scanner - Configuration
# Matches your Pine Script v5.2.1 settings

# === BX Trender Parameters ===
SHORT_L1 = 5       # EMA fast
SHORT_L2 = 20      # EMA slow
SHORT_L3 = 15      # RSI length

# === Entry Filters ===
REQUIRE_DEEP_RED = True
DEEP_RED_THRESHOLD = -15.0
DEEP_RED_LOOKBACK = 10
REQUIRE_TWO_GREEN = True
MAX_DAYS_SINCE_FLIP = 3
# Flip freshness ignores zero-line jitter: a dip shallower than this floor
# does NOT reset the "days since flip" clock (prevents a -0.5 wiggle from
# making a stock that's been green for a week look freshly flipped).
FLIP_NOISE_FLOOR = -2.0
REQUIRE_VOLUME = True
VOLUME_MULT = 1.2

# === Earnings Filter ===
BLOCK_EARNINGS = True
EARNINGS_BUFFER_DAYS = 7

# === Backtest Scoring ===
# Backtest is run only on actionable stocks (fast), producing a 0-100 score
# so you can rank which setups are historically worth trading.
RUN_BACKTEST = True
BACKTEST_STATUSES = ["ENTER", "EARNINGS BLOCK", "ALMOST", "TOO LATE"]
MIN_BACKTEST_TRADES = 3        # below this, sample too small to trust
BACKTEST_HISTORY_PERIOD = "10y"  # how far back to backtest. TradingView's chart
# loads years of bars, so 10y matches its trade counts/verdicts far better than
# 5y (e.g. TWLO: 5y=1 trade NEED DATA, 10y=5 trades GOOD like TradingView).
# A setup is "PRIME" (strong buy) only when the live verdict is ENTER AND the
# backtest is strongly green (GOOD/EXCELLENT) with a score at/above this AND
# has at least PRIME_MIN_TRADES historical trades (so a thin 3-trade sample
# can't be promoted to a top "strong buy").
PRIME_MIN_SCORE = 60
PRIME_MIN_TRADES = 5

# === Account & Risk ===
ACCOUNT_SIZE = 2000.0
MAX_RISK_PCT_STRICT = 2.0
MAX_RISK_PCT_OVERRIDE = 5.0
TARGET_POSITION_PCT = 33.0
MIN_POSITION_SIZE = 500.0
MAX_POSITION_PCT = 50.0
COMMISSION_PER_TRADE = 2.5

# === Stop Loss ===
STOP_METHOD = "ATR"     # "ATR", "Recent Low", "Fixed %"
ATR_MULT = 2.0
FIXED_STOP_PCT = 7.0

# === Stock Universe ===
# "sp500"           - S&P 500 only (~500 tickers)
# "nasdaq100"       - NASDAQ 100 only
# "sp500_nasdaq100" - Both combined (~600 tickers)
# "all_us"          - ALL US-listed stocks (NASDAQ + NYSE + AMEX, ~8,000-10,000)
# "custom"          - Only tickers from custom_tickers.txt
UNIVERSE = "all_us"
CUSTOM_TICKERS_FILE = "custom_tickers.txt"

# === Liquidity Filters (only used for "all_us" to skip junk/penny stocks) ===
# These keep the scan tradeable - illiquid stocks have huge spreads and
# can't actually be entered/exited at the prices shown.
MIN_PRICE = 3.0                     # Skip stocks under this price
MIN_AVG_DOLLAR_VOLUME = 1_000_000   # Skip stocks trading under $1M/day average
EXCLUDE_ETFS = True                 # Skip ETFs (strategy is for individual stocks)

# === Download Performance ===
DOWNLOAD_BATCH_SIZE = 400           # Download tickers in batches of this size
# History length for the main scan. MUST be long (~5y) so the WEEKLY and
# MONTHLY BX (EMA20 + RSI15) actually converge. With <2y the monthly BX is
# garbage (only ~13 monthly candles) and produces false signals.
MAIN_HISTORY_PERIOD = "5y"

# === Data Cache ===
CACHE_HOURS = 4     # Re-download data if cache is older than this

# === Dashboard ===
DASHBOARD_PORT = 5555
