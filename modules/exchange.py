"""
Binance Futures Exchange interaction module.
Handles REST communication, symbol precision, account balances, position queries,
and signed conditional Algo Orders (/fapi/v1/algoOrder).
"""

import time
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, List
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from binance.client import Client

log = logging.getLogger("ProBot")


class BinanceExchange:
    """
    Unified client for Binance USDT-M Futures.
    Provides cached symbol filters, account queries, and signed algo orders.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.binance.com",
        testnet: bool = False,
        proxy: Optional[str] = None
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.testnet = testnet
        self.proxy = proxy

        # Initialize python-binance Client
        client_kwargs = {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "testnet": self.testnet
        }
        if self.proxy:
            client_kwargs["requests_params"] = {
                "proxies": {
                    "http": self.proxy,
                    "https": self.proxy
                }
            }

        self.client = Client(**client_kwargs)
        if self.testnet:
            self.client.FUTURES_URL = f"{self.base_url}/fapi"

        # Shared HTTP session for custom REST requests (algoOrder)
        self.session = requests.Session()
        if self.proxy:
            self.session.proxies = {
                "http": self.proxy,
                "https": self.proxy
            }
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Cached symbol filters: {symbol: {"step": float, "tick": float, "min_notional": float}}
        self._symbol_filters: Dict[str, Dict[str, float]] = {}

    def get_symbol_filters(self, symbol: str) -> Dict[str, float]:
        """
        Retrieves stepSize, tickSize, and minNotional for a symbol.
        Caches exchange info to prevent redundant network calls.
        """
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]

        log.info(f"Loading exchange filters for {symbol}...")
        info = self.client.futures_exchange_info()
        for s in info.get("symbols", []):
            sym = s["symbol"]
            step = 0.001
            tick = 0.01
            min_notional = 5.0

            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional", 5.0))

            self._symbol_filters[sym] = {
                "step": step,
                "tick": tick,
                "min_notional": min_notional,
            }

        if symbol not in self._symbol_filters:
            log.warning(f"Symbol {symbol} not found in exchange info, using default precision")
            self._symbol_filters[symbol] = {"step": 0.001, "tick": 0.01, "min_notional": 5.0}

        return self._symbol_filters[symbol]

    def round_qty(self, symbol: str, qty: float) -> float:
        """Rounds quantity down to the symbol's step size."""
        step = self.get_symbol_filters(symbol)["step"]
        return float(Decimal(str(qty)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

    def round_price(self, symbol: str, price: float) -> float:
        """Rounds price down to the symbol's tick size."""
        tick = self.get_symbol_filters(symbol)["tick"]
        return float(Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_DOWN))

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Sets the account leverage for the given symbol."""
        try:
            return self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except Exception as e:
            log.error(f"[{symbol}] Failed to set leverage to {leverage}x: {e}")
            raise

    def get_balance(self) -> float:
        """Returns available USDT balance from futures account."""
        try:
            acc = self.client.futures_account()
            return float(acc.get("availableBalance", 0.0))
        except Exception as e:
            log.error(f"Failed to fetch account balance: {e}")
            return 0.0

    def get_position(self, symbol: str) -> Optional[float]:
        """
        Returns active position amount for the symbol.
        Positive = Long, Negative = Short, 0.0 = Flat, None = API/Network Error.
        """
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for p in positions:
                amt = float(p.get("positionAmt", 0.0))
                if amt != 0:
                    return amt
            return 0.0
        except Exception as e:
            log.warning(f"[{symbol}] get_position error: {e}")
            return None

    def get_position_entry(self, symbol: str) -> Optional[float]:
        """Returns average entry price for the active position, or None if flat."""
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for p in positions:
                amt = float(p.get("positionAmt", 0.0))
                if amt != 0:
                    return float(p.get("entryPrice", 0.0))
        except Exception as e:
            log.warning(f"[{symbol}] get_position_entry error: {e}")
        return None

    def fetch_klines(self, symbol: str, interval: str, limit: int = 288) -> List[List]:
        """Fetches historical klines for indicator preloading."""
        return self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)

    def create_market_order(self, symbol: str, side: str, qty: float) -> Dict[str, Any]:
        """Executes a market entry order."""
        return self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

    # ==========================================
    # Binance Futures Algo Order API (/fapi/v1/algoOrder)
    # ==========================================
    def _sign_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Appends timestamp and HMAC-SHA256 signature to query parameters."""
        signed_params = dict(params)
        signed_params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(signed_params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        signed_params["signature"] = signature
        return signed_params

    def place_algo_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        trigger_price: float,
        qty: float
    ) -> str:
        """
        Places a conditional algo order (e.g., STOP_MARKET, TAKE_PROFIT_MARKET).
        Returns the algoId as a string.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "algoType": "CONDITIONAL",
            "quantity": str(qty),
            "triggerPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "timeInForce": "GTC",
        }
        signed_params = self._sign_request(params)
        url = f"{self.base_url}/fapi/v1/algoOrder"
        headers = {"X-MBX-APIKEY": self.api_key}

        resp = self.session.post(url, headers=headers, params=signed_params, timeout=10)
        data = resp.json()

        if resp.status_code != 200 or "algoId" not in data:
            raise RuntimeError(f"Algo order failed [{resp.status_code}]: {data}")

        return str(data["algoId"])

    def cancel_algo_order(self, symbol: str, algo_id: str) -> Optional[Dict[str, Any]]:
        """Cancels a specific conditional algo order by ID."""
        try:
            params = {"symbol": symbol, "algoId": algo_id}
            signed_params = self._sign_request(params)
            url = f"{self.base_url}/fapi/v1/algoOrder"
            headers = {"X-MBX-APIKEY": self.api_key}

            resp = self.session.delete(url, headers=headers, params=signed_params, timeout=10)
            data = resp.json()
            log.info(f"[{symbol}] Cancelled algo order {algo_id}: {data.get('algoStatus', data)}")
            return data
        except Exception as e:
            log.warning(f"[{symbol}] Cancel algo order {algo_id} error: {e}")
            return None

    def cancel_all_algo_orders(self, symbol: str, algo_ids: Optional[List[Optional[str]]] = None) -> None:
        """
        Cancels all open algo and regular orders for the symbol.
        Wipes out any remaining TP or SL orders when a trade closes.
        """
        # 1. Cancel known specific algo order IDs
        if algo_ids:
            for algo_id in algo_ids:
                if algo_id:
                    self.cancel_algo_order(symbol, algo_id)

        # 2. Bulk cancel all open conditional algo orders (/fapi/v1/algoOpenOrders)
        try:
            params = {"symbol": symbol}
            signed_params = self._sign_request(params)
            url = f"{self.base_url}/fapi/v1/algoOpenOrders"
            headers = {"X-MBX-APIKEY": self.api_key}
            resp = self.session.delete(url, headers=headers, params=signed_params, timeout=10)
            log.info(f"[{symbol}] Bulk cancelled open algo orders: {resp.status_code}")
        except Exception as e:
            log.debug(f"[{symbol}] Bulk cancel algo orders notice: {e}")

        # 3. Bulk cancel any standard open orders on this symbol
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            log.info(f"[{symbol}] Bulk cancelled standard open orders")
        except Exception as e:
            log.debug(f"[{symbol}] Bulk cancel standard orders notice: {e}")

    cancel_all_open_orders = cancel_all_algo_orders
