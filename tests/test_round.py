"""Stylized adversarial exchange checks. These are NOT performance backtests."""
import contextlib
import io
import random
import unittest
from dataclasses import replace
from config import Config
from engine import Bot
from execution import TICKERS, TAKER_FEE, MAKER_REBATE, risk_bounds


class Exchange:
    def __init__(self, seed=1, regime='balanced'):
        self.rng = random.Random(seed)
        self.regime = regime
        self.now = 0.
        self.orders = {}
        self.positions = dict.fromkeys(TICKERS, 0)
        self.cash = dict.fromkeys(TICKERS, 0.)
        self.mid = dict.fromkeys(TICKERS, 10.)
        self.volume = 0
        self.calls = 0
        self.cancels = 0
        self.takes = 0

    def prices(self, ticker):
        mid = self.mid[ticker]
        return round(mid-.01, 2), round(mid+.01, 2)

    def fill(self, o, qty, price, maker):
        qty = min(qty, o['quantity']-o['quantity_filled'])
        if qty <= 0:
            return
        sign = 1 if o['action'] == 'BUY' else -1
        t = o['ticker']
        old = o['quantity_filled']
        o['vwap'] = (o.get('vwap', 0)*old+qty*price)/(old+qty)
        o['quantity_filled'] += qty
        self.positions[t] += sign*qty
        self.cash[t] += -sign*qty*price + qty*(MAKER_REBATE[t] if maker else -TAKER_FEE[t])
        self.volume += qty
        if o['quantity_filled'] == o['quantity']:
            o['status'] = 'TRANSACTED'
        assert sum(abs(x) for x in self.positions.values()) <= 25000
        assert abs(sum(self.positions.values())) <= 25000

    def advance(self):
        self.now = round(self.now+.25, 2)
        for t in TICKERS:
            if self.regime == 'trend' and int(self.now*4) % 8 == 0:
                self.mid[t] = round(self.mid[t]+(.01 if t != 'RY' else -.01), 2)
            elif self.regime == 'churn' and self.rng.random() < .1:
                self.mid[t] = round(self.mid[t]+self.rng.choice([-.01, .01]), 2)
            bid, ask = self.prices(t)
            action = self.rng.choice(['BUY', 'SELL'])
            demand = self.rng.randint(100, 900)
            for o in self.orders.values():
                if o['ticker'] != t or o['status'] != 'OPEN':
                    continue
                crosses = o['action'] == 'BUY' and o['price'] >= ask or o['action'] == 'SELL' and o['price'] <= bid
                if crosses:
                    # Resting limit picked off after a price move, at its limit.
                    self.fill(o, min(1000, o['quantity']), o['price'], True)
                elif o['action'] == action and (o['price'] >= bid if action == 'BUY' else o['price'] <= ask):
                    ahead = min(o['ahead'], demand)
                    o['ahead'] -= ahead
                    self.fill(o, demand-ahead, o['price'], True)

    def call(self, method, path, **p):
        self.calls += 1
        if path == 'case':
            return dict(period=1, tick=self.now, status='ACTIVE' if self.now < 300 else 'STOPPED')
        if path == 'limits':
            return [dict(gross_limit=25000, net_limit=25000)]
        if path == 'securities':
            return [dict(ticker=t, position=self.positions[t], realized=self.cash[t]+self.positions[t]*self.mid[t],
                         unrealized=0) for t in TICKERS]
        if path == 'securities/book':
            t = p['ticker']
            bid, ask = self.prices(t)
            depth = 12000 if self.regime == 'crowded' else 3000
            book = {'bids': [dict(price=round(bid-i*.01, 2), quantity=depth, order_id=-10-i) for i in range(5)],
                    'asks': [dict(price=round(ask+i*.01, 2), quantity=depth, order_id=-20-i) for i in range(5)]}
            if self.regime == 'one_sided' and 280 <= self.now < 295:
                # Keep only the side needed to liquidate each stock.
                book['asks' if self.positions[t] >= 0 else 'bids'] = []
            for o in self.orders.values():
                if o['status'] == 'OPEN' and o['ticker'] == t:
                    book['bids' if o['action'] == 'BUY' else 'asks'].append(dict(o))
            return book
        if path == 'orders' and method == 'GET':
            return [dict(o) for o in self.orders.values() if o['status'] == p['status']]
        if path == 'orders' and method == 'POST':
            assert 0 < p['quantity'] <= 5000
            oid = len(self.orders)+1
            bid, ask = self.prices(p['ticker'])
            improved = p['price'] > bid if p['action'] == 'BUY' else p['price'] < ask
            o = dict(p, order_id=oid, quantity_filled=0, status='OPEN',
                     ahead=0 if improved else (12000 if self.regime == 'crowded' else 3000))
            self.orders[oid] = o
            if p['action'] == 'BUY' and p['price'] >= ask or p['action'] == 'SELL' and p['price'] <= bid:
                self.takes += 1
                self.fill(o, min(3000, p['quantity']), ask if p['action'] == 'BUY' else bid, False)
            return dict(o)
        if path.startswith('orders/'):
            o = self.orders[int(path.split('/')[1])]
            if method == 'DELETE' and o['status'] == 'OPEN':
                self.cancels += 1
                bid, ask = self.prices(o['ticker'])
                if (o['price'] >= bid if o['action'] == 'BUY' else o['price'] <= ask):
                    self.fill(o, 37, o['price'], True)
                if o['status'] == 'OPEN':
                    o['status'] = 'CANCELLED'
            return dict(o)
        if path == 'commands/cancel':
            for o in self.orders.values():
                if o['ticker'] == p['ticker'] and o['status'] == 'OPEN':
                    o['status'] = 'CANCELLED'
            return {}
        raise AssertionError((method, path, p))


def exercise(seed, regime):
    x = Exchange(seed, regime)
    b = Bot(x, clock=lambda: x.now)
    with contextlib.redirect_stdout(io.StringIO()):
        b.start()
        while b.step():
            _, _, gross, net = risk_bounds(b.ledger.positions, b.ledger.active())
            assert gross <= b.gross_limit and net <= b.net_limit
            assert b.ledger.volume == x.volume
            assert b.ledger.positions == x.positions
            x.advance()
            assert x.now <= 300.25
        b.cleanup()
    assert not b.ledger.active()
    assert not any(x.positions.values())
    assert b.ledger.volume == x.volume
    return dict(regime=regime, seed=seed, volume=x.volume, qualified=x.volume >= b.cfg.minimum_volume,
                end_tick=x.now, hard_stop=b.stopping,
                residual=sum(map(abs, x.positions.values())), cancels=x.cancels, taker_orders=x.takes)


class RoundTests(unittest.TestCase):
    def test_seeded_rounds_with_competition_and_partial_fills(self):
        for regime in ('balanced', 'crowded', 'trend', 'churn', 'one_sided'):
            for seed in (11, 29):
                with self.subTest(regime=regime, seed=seed):
                    result = exercise(seed, regime)
                    self.assertGreater(result['volume'], 0)


if __name__ == '__main__':
    unittest.main()
