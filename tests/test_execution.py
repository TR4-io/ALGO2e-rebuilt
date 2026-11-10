import itertools
import random
import unittest
from unittest.mock import Mock
import requests
import execution as s


def row(oid=1, ticker='CNR', action='BUY', quantity=1000, filled=0, status='OPEN', price=10):
    return dict(order_id=oid, ticker=ticker, action=action, quantity=quantity,
                quantity_filled=filled, status=status, price=price)


class RiskTests(unittest.TestCase):
    def test_reserves_both_sides_without_netting(self):
        p = dict.fromkeys(s.TICKERS, 0)
        orders = [s.Order(1, 'CNR', 'BUY', 5000, 10, 0), s.Order(2, 'CNR', 'SELL', 5000, 11, 0)]
        low, high, gross, net = s.risk_bounds(p, orders)
        self.assertEqual((low['CNR'], high['CNR'], gross, net), (-5000, 5000, 5000, 5000))
        self.assertEqual(s.capacity(p, orders, 'CNR', 'BUY', 5000, 18000, 18000), 1500)

    def test_gross_is_not_signed_net(self):
        p = {'CNR': 6000, 'RY': -6000, 'AC': 0}
        self.assertEqual(s.capacity(p, [], 'AC', 'BUY', 5000, 13000, 18000), 1000)
        self.assertEqual(s.capacity(p, [], 'RY', 'BUY', 2000, 18000, 18000), 2000)

    def test_random_fill_corners_stay_safe(self):
        rng = random.Random(2026)
        for _ in range(500):
            p = {t: rng.randint(-3000, 3000) for t in s.TICKERS}
            orders = [s.Order(i, t, a, rng.randint(0, 1000), 10, 0)
                      for i, (t, a) in enumerate(itertools.product(s.TICKERS, ('BUY', 'SELL')))]
            t, a = rng.choice(s.TICKERS), rng.choice(('BUY', 'SELL'))
            q = s.capacity(p, orders, t, a, 5000, 15000, 10000)
            _, _, old_gross, old_net = s.risk_bounds(p, orders)
            if old_gross > 15000 or old_net > 10000:
                self.assertEqual(q, 0)
                continue
            orders.append(s.Order(9, t, a, q, 10, 0))
            for fills in itertools.product((0, 1), repeat=len(orders)):
                result = dict(p)
                for o, filled in zip(orders, fills):
                    result[o.ticker] += filled*o.remaining*(1 if o.action == 'BUY' else -1)
                self.assertLessEqual(sum(map(abs, result.values())), 15000)
                self.assertLessEqual(abs(sum(result.values())), 10000)
                self.assertLessEqual(max(map(abs, result.values())), s.STOCK_BUDGET)


