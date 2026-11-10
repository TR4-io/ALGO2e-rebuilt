"""REST transport, fill ledger and worst-case independent-fill risk reservation."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os

import requests

BASE_URL = os.getenv('RIT_BASE_URL', 'http://localhost:9999/v1')
API_KEY = os.getenv('RIT_API_KEY', 'Rotman')
TICKERS = ('CNR', 'RY', 'AC')
# Positive taker numbers are costs; positive maker numbers are receipts.
TAKER_FEE = {'CNR': 0.0027, 'RY': -0.0014, 'AC': 0.0015}
MAKER_REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}
MAX_ORDER = 5000
STOCK_BUDGET = 6500  # helper default; Bot passes its stricter configured cap


class ApiError(RuntimeError):
    pass


class UnknownWrite(ApiError):
    """Do not replay a POST whose outcome is unknown."""


class HttpError(ApiError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f'HTTP {status} on {path}')


class RateLimited(ApiError):
    def __init__(self, wait):
        self.wait = wait
        super().__init__(f'Rate limited: wait {wait:.2f}s, then make a fresh decision')


def integer(value):
    n = float(value)
    if not math.isfinite(n) or n != int(n):
        raise ValueError('Invalid share quantity')
    return int(n)


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'X-API-Key': API_KEY})

    def call(self, method, path, **params):
        try:
            r = self.session.request(method, BASE_URL.rstrip('/') + '/' + path,
                                     params=params, timeout=2, allow_redirects=False)
        except requests.RequestException as exc:
            kind = UnknownWrite if method != 'GET' else ApiError
            raise kind(f'{method} {path}: connection failed ({type(exc).__name__})') from exc
        if r.status_code == 429:
            try:
                wait = float(r.headers.get('Retry-After') or r.json().get('wait', 0.5))
                if not math.isfinite(wait):
                    wait = 0.5
            except (ValueError, TypeError, AttributeError):
                wait = 0.5
            raise RateLimited(max(0.05, wait))
        if method != 'GET' and r.status_code >= 500:
            raise UnknownWrite(f'{method} {path}: HTTP {r.status_code}; outcome unknown')
        if not 200 <= r.status_code < 300:
            raise HttpError(r.status_code, path)
        try:
            return r.json()
        except ValueError as exc:
            kind = UnknownWrite if method != 'GET' else ApiError
            raise kind(f'{path}: unreadable response') from exc


@dataclass
class Order:
    id: int
    ticker: str
    action: str
    quantity: int
    price: float
    born: float
    filled: int = 0
    status: str = 'OPEN'

    @property
    def remaining(self):
        return self.quantity - self.filled if self.status == 'OPEN' else 0


class Ledger:
    """Inventory comes only from confirmed fills, never cancellation assumptions."""
    def __init__(self):
        self.orders = {}
        self.positions = dict.fromkeys(TICKERS, 0)
        self.volume = 0

    def update(self, row):
        order = self.orders[integer(row['order_id'])]
        filled = integer(row['quantity_filled'])
        if (row['ticker'] != order.ticker or row['action'] != order.action
                or integer(row['quantity']) != order.quantity
                or not order.filled <= filled <= order.quantity
                or row['status'] not in ('OPEN', 'CANCELLED', 'TRANSACTED')):
            raise ValueError('Order identity or cumulative fills changed unexpectedly')
        delta = filled - order.filled
        self.positions[order.ticker] += delta if order.action == 'BUY' else -delta
        self.volume += delta
        order.filled, order.status = filled, row['status']

    def active(self, ticker=None):
        return [o for o in self.orders.values() if o.status == 'OPEN'
                and (ticker is None or o.ticker == ticker)]


def risk_bounds(positions, orders):
    """Worst case over all independent combinations of outstanding fills."""
    low, high = dict(positions), dict(positions)
    for o in orders:
        if o.action == 'BUY':
            high[o.ticker] += o.remaining
        else:
            low[o.ticker] -= o.remaining
    gross = sum(max(abs(low[t]), abs(high[t])) for t in TICKERS)
    net = max(abs(sum(low.values())), abs(sum(high.values())))
    return low, high, gross, net


def capacity(positions, orders, ticker, action, wanted, gross_limit, net_limit, stock_limit=STOCK_BUDGET):
    """Largest safe clip, reserving every remaining open-order share."""
    def safe(q):
        candidate = Order(-1, ticker, action, q, 1, 0)
        low, high, gross, net = risk_bounds(positions, [*orders, candidate])
        return (gross <= gross_limit and net <= net_limit
                and all(max(abs(low[t]), abs(high[t])) <= stock_limit for t in TICKERS))
    if not safe(0):
        return 0
    lo, hi = 0, min(MAX_ORDER, wanted)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if safe(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def clean_book(raw, own_ids):
    result = {}
    for side in ('bids', 'asks'):
        levels = {}
        for row in raw[side]:
            if str(row.get('order_id')) in own_ids or row.get('status', 'OPEN') != 'OPEN':
                continue
            qty = integer(row['quantity']) - integer(row.get('quantity_filled', 0))
            price = float(row['price'])
            if qty < 0 or not math.isfinite(price) or price <= 0:
                raise ValueError('Invalid order book')
            if qty:
                levels[price] = levels.get(price, 0) + qty
        result[side] = sorted(levels.items(), reverse=side == 'bids')
    return result


