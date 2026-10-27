"""
Tiger Securities API client wrapper.
Handles authentication and provides a unified interface for trading and market data.

Order routing note: the exchange is resolved from the broker's own contract list
instead of being guessed from a hard-coded ticker whitelist. The old guess sent
INTC (NASDAQ) to NYSE and produced rejected orders.
"""

import logging
import os
import time
from typing import Dict, List, Optional

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.common.consts import Language, BarPeriod
from tigeropen.trade.trade_client import TradeClient
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.push.push_client import PushClient

from config.settings import TIGER_CONFIG

logger = logging.getLogger(__name__)

# Used only when the broker contract lookup is unavailable. Unknown symbols are
# NOT guessed — they raise, because a wrong exchange means a rejected order.
_EXCHANGE_FALLBACK = {
    "AAPL": "NASDAQ", "MSFT": "NASDAQ", "GOOGL": "NASDAQ", "META": "NASDAQ",
    "NVDA": "NASDAQ", "AMD": "NASDAQ", "INTC": "NASDAQ", "AVGO": "NASDAQ",
    "ADBE": "NASDAQ", "QCOM": "NASDAQ", "TXN": "NASDAQ", "AMAT": "NASDAQ",
    "LRCX": "NASDAQ", "KLAC": "NASDAQ", "MRVL": "NASDAQ", "FTNT": "NASDAQ",
    "PANW": "NASDAQ", "SNOW": "NYSE", "DDOG": "NASDAQ", "ISRG": "NASDAQ",
    "VRTX": "NASDAQ", "REGN": "NASDAQ", "AMGN": "NASDAQ", "BIIB": "NASDAQ",
    "MRNA": "NASDAQ", "ILMN": "NASDAQ", "IDXX": "NASDAQ", "HOLX": "NASDAQ",
    "COST": "NASDAQ", "SBUX": "NASDAQ", "NFLX": "NASDAQ", "CMCSA": "NASDAQ",
    "PYPL": "NASDAQ", "TSLA": "NASDAQ", "GILD": "NASDAQ", "MDLZ": "NASDAQ",
    "BABA": "NYSE", "PFE": "NYSE", "JPM": "NYSE", "BAC": "NYSE", "WFC": "NYSE",
    "GS": "NYSE", "MS": "NYSE", "BLK": "NYSE", "SPGI": "NYSE", "MCO": "NYSE",
    "ICE": "NYSE", "CME": "NASDAQ", "V": "NYSE", "MA": "NYSE", "AXP": "NYSE",
    "COF": "NYSE", "UNH": "NYSE", "JNJ": "NYSE", "ABBV": "NYSE", "MRK": "NYSE",
    "TMO": "NYSE", "ABT": "NYSE", "DHR": "NYSE", "LLY": "NYSE", "ORCL": "NYSE",
    "CRM": "NYSE", "IBM": "NYSE", "AMZN": "NASDAQ", "HD": "NYSE", "MCD": "NYSE",
    "NKE": "NYSE", "LOW": "NYSE", "TGT": "NYSE", "TJX": "NYSE", "HON": "NASDAQ",
    "GE": "NYSE", "MMM": "NYSE", "CAT": "NYSE", "DE": "NYSE", "UPS": "NYSE",
    "FDX": "NYSE", "LMT": "NYSE", "RTX": "NYSE", "NOC": "NYSE", "XOM": "NYSE",
    "CVX": "NYSE", "COP": "NYSE", "EOG": "NYSE", "SLB": "NYSE", "PSX": "NYSE",
    "VLO": "NYSE", "MPC": "NYSE", "NEE": "NYSE", "DUK": "NYSE", "AMT": "NYSE",
    "PLD": "NYSE", "EQIX": "NASDAQ", "T": "NYSE", "VZ": "NYSE", "DIS": "NYSE",
    "SOFI": "NASDAQ", "AFRM": "NASDAQ", "UPST": "NASDAQ", "RIVN": "NASDAQ",
    "ENPH": "NASDAQ", "FSLR": "NASDAQ", "ETSY": "NASDAQ", "CHWY": "NYSE",
    "TTD": "NASDAQ", "AXON": "NASDAQ", "CELH": "NASDAQ", "TOST": "NYSE",
    "SPY": "NYSEARCA", "QQQ": "NASDAQ",
}

_OPEN_STATUSES = {"submitted", "pending", "partial_fill", "held", "new"}


def _bare_base64_key(text: str) -> str:
    """Strip PEM armour down to the raw base64 body.

    Tiger's SDK base64-decodes `private_key` verbatim
    (tigeropen/common/util/signature_utils.load_private_key), so passing it a
    complete PEM file fails with "Invalid base64-encoded string": the
    "-----BEGIN RSA PRIVATE KEY-----" lines are not base64. Hand it the body
    only. Accepts either a PEM file's contents or an already-bare key string.
    """
    lines = [line.strip() for line in (text or "").strip().splitlines()]
    return "".join(line for line in lines if line and not line.startswith("-----"))


def _build_client_config() -> TigerOpenClientConfig:
    """Build Tiger API client config from settings."""
    missing = [
        name
        for name, value in (
            ("TIGER_ID", TIGER_CONFIG.get("tiger_id")),
            ("TIGER_ACCOUNT", TIGER_CONFIG.get("account")),
        )
        if not (value or "").strip()
    ]
    if missing:
        # fail-closed: no literal fallbacks live in the repo (it is public)
        raise ValueError(
            "Missing Tiger credentials: "
            + ", ".join(missing)
            + ". Set them in the environment or config/.env."
        )

    cfg = TigerOpenClientConfig(sandbox_debug=TIGER_CONFIG.get("sandbox", False))
    cfg.tiger_id = TIGER_CONFIG["tiger_id"]
    cfg.account = TIGER_CONFIG["account"]
    cfg.language = Language.zh_CN

    key_path = TIGER_CONFIG.get("private_key_path", "")
    key_str = TIGER_CONFIG.get("private_key", "")

    if key_str:
        cfg.private_key = _bare_base64_key(key_str)
    elif key_path and os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            cfg.private_key = _bare_base64_key(f.read())
    else:
        raise ValueError(
            "No RSA private key configured. Set TIGER_PRIVATE_KEY_PATH (or "
            "TIGER_PRIVATE_KEY) in the environment, or config/.env."
        )

    return cfg


class TigerClient:
    """Unified client for Tiger Securities trade + quote APIs."""

    def __init__(self):
        self._config: Optional[TigerOpenClientConfig] = None
        self._trade: Optional[TradeClient] = None
        self._quote: Optional[QuoteClient] = None
        self._connected = False
        self._contract_cache: Dict[str, object] = {}

    # ------------------------------------------------------------------ #
    #  Connection
    # ------------------------------------------------------------------ #

    def connect(self):
        try:
            self._config = _build_client_config()
            self._trade = TradeClient(self._config)
            self._quote = QuoteClient(self._config)
            self._trade.get_assets(account=TIGER_CONFIG["account"])
            self._connected = True
            logger.info(
                "Tiger API connected (env=%s, sandbox=%s, account=%s)",
                TIGER_CONFIG.get("env"), TIGER_CONFIG.get("sandbox"),
                TIGER_CONFIG.get("account"),
            )
        except Exception as exc:
            self._connected = False
            logger.error("Tiger API connection failed: %s", exc)
            raise

    @property
    def connected(self) -> bool:
        return self._connected

    def get_trade_client(self) -> TradeClient:
        if not self._trade:
            raise RuntimeError("Tiger trade client is not connected")
        return self._trade

    def get_quote_client(self) -> QuoteClient:
        if not self._quote:
            raise RuntimeError("Tiger quote client is not connected")
        return self._quote

    # ------------------------------------------------------------------ #
    #  Contracts
    # ------------------------------------------------------------------ #

    def resolve_contract(self, symbol: str):
        """
        Return a broker-sourced Contract for `symbol`.

        Falls back to a curated exchange map when the contract endpoint is
        unavailable. Never guesses an exchange for an unknown symbol.
        """
        symbol = symbol.upper()
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        from tigeropen.trade.domain.contract import Contract
        from tigeropen.common.consts import SecurityType

        exchange = None
        try:
            contracts = self._quote.get_contract(symbols=[symbol])
            for c in contracts or []:
                if getattr(c, "symbol", "").upper() == symbol:
                    sec_type = getattr(c, "sec_type", None)
                    if sec_type in (None, SecurityType.STK, "STK"):
                        exchange = getattr(c, "exchange", None)
                        self._contract_cache[symbol] = c
                        return c
        except Exception as exc:
            logger.warning("Contract lookup failed for %s: %s", symbol, exc)

        exchange = exchange or _EXCHANGE_FALLBACK.get(symbol)
        if not exchange:
            raise RuntimeError(
                f"No exchange known for {symbol}. Refusing to guess — add it to "
                f"_EXCHANGE_FALLBACK or restore contract lookup."
            )

        contract = Contract(
            symbol=symbol,
            exchange=exchange,
            currency="USD",
            sec_type=SecurityType.STK,
        )
        self._contract_cache[symbol] = contract
        return contract

    # ------------------------------------------------------------------ #
    #  Account / Portfolio
    # ------------------------------------------------------------------ #

    def get_account_assets(self):
        return self._trade.get_assets(account=TIGER_CONFIG["account"])

    def get_account_summary(self) -> dict:
        """Normalised account summary — the single source of truth for sizing."""
        try:
            assets = self.get_account_assets()
            if isinstance(assets, list):
                assets = assets[0] if assets else None
            if assets is None:
                return {}
            summary = getattr(assets, "summary", assets) or assets

            def pick(*names, default=0.0):
                for name in names:
                    value = getattr(summary, name, None)
                    if value is not None:
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            continue
                return default

            return {
                "net_liquidation": pick("net_liquidation", "netLiquidation"),
                "cash": pick("cash"),
                "buying_power": pick("buying_power", "buyingPower"),
                "gross_position_value": pick("gross_position_value"),
                "unrealized_pnl": pick("unrealized_pnl"),
                "realized_pnl": pick("realized_pnl"),
                "currency": getattr(summary, "currency", "USD"),
            }
        except Exception as exc:
            logger.error("Failed to fetch account summary: %s", exc)
            return {}

    def get_positions(self) -> dict:
        """Current positions as {symbol: {...}}. Empty dict on failure."""
        try:
            positions = self._trade.get_positions(account=TIGER_CONFIG["account"])
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            return {}

        result = {}
        for pos in positions or []:
            contract = getattr(pos, "contract", None)
            symbol = getattr(contract, "symbol", "") if contract else ""
            symbol = (symbol or getattr(pos, "symbol", "") or "").upper()
            if not symbol:
                continue
            qty = getattr(pos, "quantity", 0) or 0
            avg_cost = getattr(pos, "average_cost", 0) or 0
            market_value = getattr(pos, "market_value", None)
            unrealized = getattr(pos, "unrealized_pnl", None)
            pnl_ratio = getattr(pos, "unrealized_pnl_ratio", None)
            result[symbol] = {
                "symbol": symbol,
                "quantity": int(qty),
                "avg_cost": float(avg_cost),
                "market_value": float(market_value) if market_value is not None else 0.0,
                "unrealized_pnl": float(unrealized) if unrealized is not None else 0.0,
                "unrealized_pnl_pct": float(pnl_ratio) if pnl_ratio is not None else None,
            }
        return result

    def get_orders(self, status=None):
        """Return order list, optionally filtered by status."""
        return self._trade.get_orders(account=TIGER_CONFIG["account"])

    def get_order_by_id(self, order_id: int):
        """Look up a single order; returns None when not found."""
        try:
            return self._trade.get_order(account=TIGER_CONFIG["account"], id=order_id)
        except Exception:
            pass
        try:
            for order in self.get_orders() or []:
                oid = getattr(order, "id", None) or getattr(order, "order_id", None)
                if oid is not None and int(oid) == int(order_id):
                    return order
        except Exception as exc:
            logger.debug("Order lookup failed for %s: %s", order_id, exc)
        return None

    def has_open_order(self, symbol: str, action: str) -> bool:
        """True when an open order already exists for this symbol/side."""
        try:
            orders = self.get_orders() or []
        except Exception as exc:
            logger.warning("Could not list open orders: %s", exc)
            return False

        action = action.upper()
        for order in orders:
            contract = getattr(order, "contract", None)
            sym = (getattr(contract, "symbol", "") or "").upper()
            if sym != symbol.upper():
                continue
            side = str(getattr(order, "action", "") or "").upper()
            status = str(getattr(order, "status", "") or "").lower()
            if (side == action or action in side) and any(s in status for s in _OPEN_STATUSES):
                logger.warning("Open %s order already exists for %s (%s)", action, symbol, status)
                return True
        return False

    # ------------------------------------------------------------------ #
    #  Order Management
    # ------------------------------------------------------------------ #

    def place_order(self, symbol: str, action: str, quantity: int,
                    order_type: str = "LMT", limit_price: float = None,
                    time_in_force: str = "DAY", outside_rth: bool = False):
        """
        Place an order and return the broker's Order object.

        order_type: 'LMT' (limit) or 'MKT' (market). Passed as a string because
        the SDK's enum is not JSON-serialisable in this version.
        """
        from tigeropen.trade.domain.order import Order

        normalized = (order_type or "LMT").upper()
        normalized = {"LIMIT": "LMT", "MARKET": "MKT"}.get(normalized, normalized)
        if normalized not in ("LMT", "MKT"):
            raise ValueError(f"Unsupported order type: {order_type}")

        contract = self.resolve_contract(symbol)
        order = Order(
            account=TIGER_CONFIG["account"],
            contract=contract,
            action=action.upper(),
            order_type=normalized,
            quantity=int(quantity),
            limit_price=limit_price,
            time_in_force=time_in_force,
            outside_rth=outside_rth,
        )
        result = self._trade.place_order(order)
        order_id = getattr(result, "id", None) or result
        logger.info(
            "Placed %s %s x%d @ %s (%s) → id=%s",
            action.upper(), symbol, quantity, limit_price, normalized, order_id,
        )
        return result

    def cancel_order(self, order_id: int):
        result = self._trade.cancel_order(account=TIGER_CONFIG["account"], id=order_id)
        logger.info("Cancelled order %s", order_id)
        return result

    def modify_order(self, order_id: int, quantity: int = None, limit_price: float = None):
        return self._trade.modify_order(
            account=TIGER_CONFIG["account"],
            id=order_id,
            quantity=quantity,
            limit_price=limit_price,
        )

    # ------------------------------------------------------------------ #
    #  Market Data
    # ------------------------------------------------------------------ #

    def get_quote(self, symbols: list):
        """Delayed quote snapshots (no real-time subscription required)."""
        return self._quote.get_stock_delay_briefs(symbols=symbols)

    def get_bars(self, symbol: str, period: str = "day", limit: int = 252,
                 begin_time: int = None, end_time: int = None):
        period_map = {
            "day": BarPeriod.DAY,
            "week": BarPeriod.WEEK,
            "month": BarPeriod.MONTH,
            "1min": BarPeriod.ONE_MINUTE,
            "5min": BarPeriod.FIVE_MINUTES,
            "15min": BarPeriod.FIFTEEN_MINUTES,
            "30min": BarPeriod.HALF_HOUR,
            "60min": BarPeriod.ONE_HOUR,
        }
        bar_period = period_map.get(period, BarPeriod.DAY)
        df = self._quote.get_bars(
            symbols=[symbol], period=bar_period, limit=limit,
            begin_time=begin_time, end_time=end_time,
        )
        if df is None or (hasattr(df, "empty") and df.empty):
            return None
        if "symbol" in df.columns:
            return df[df["symbol"] == symbol].copy()
        return df

    def get_financials(self, symbol: str, fields: list = None):
        return self._quote.get_stock_details(symbols=[symbol])

    def get_timeline(self, symbols: list):
        return self._quote.get_timeline(symbols=symbols)

    def search_stocks(self, query: str):
        return self._quote.search_stocks(query=query)

    def get_market_status(self) -> list:
        """Raw market status list from the quote API."""
        return self._quote.get_market_status(market="US", lang="en_US")

    # ------------------------------------------------------------------ #
    #  Push / Streaming (optional)
    # ------------------------------------------------------------------ #

    def get_push_client(self) -> PushClient:
        return PushClient(self._config)
