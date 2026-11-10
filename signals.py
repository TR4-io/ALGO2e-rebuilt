"""Snapshot signals, not trader identification or a proven predictive model."""
from dataclasses import dataclass
import math


def clamp(x, low, high):
    return max(low, min(high, x))


@dataclass
class Snapshot:
    bid: float
    ask: float
    mid: float
    fair: float
    sigma: float
    imbalance: float
    flow: float
    churn: float
    stable_for: float
    samples: int

    @property
    def spread(self):
        return self.ask - self.bid


class Market:
    def __init__(self):
        self.last = None
        self.when = None
        self.variance = self.trend = self.imbalance = self.flow = self.churn = 0.0
        self.direction = 0
        self.since = 0.0
        self.samples = 0

    def observe(self, book, now):
        if not book['bids'] or not book['asks']:
            # Do not resume a taker signal across a data gap.
            self.last = self.when = None
            self.samples = 0
            return None
        bid, bq = book['bids'][0]
        ask, aq = book['asks'][0]
        if bid >= ask:
            self.last = self.when = None
            self.samples = 0
            return None
        mid = (bid + ask) / 2
        def depth(side, best):
            return sum(q/(i+1) for i, (p, q) in enumerate(book[side][:5]) if abs(p-best) <= .050001)
        bd, ad = depth('bids', bid), depth('asks', ask)
        imbalance = (bd-ad)/(bd+ad)
        micro = (ask*bq + bid*aq)/(bq+aq)
        if self.when is None or now-self.when > 2:
            self.imbalance, self.flow, self.churn = imbalance, 0, 0
            self.trend = self.variance = 0
            self.samples = 1
            self.since = now
        elif now > self.when:
            dt = max(.01, now-self.when)
            w = 1-math.exp(-dt/.8)
            slow = 1-math.exp(-dt/3)
            ob, oq, oa, oaq, om = self.last
            # Best-level order-flow imbalance includes additions/cancellations;
            # it MUST NOT be described as observed aggressive trade flow.
            event = (bq if bid >= ob else 0)-(oq if bid <= ob else 0)
            event -= (aq if ask <= oa else 0)-(oaq if ask >= oa else 0)
            ofi = clamp(event/max(1, (bq+aq+oq+oaq)/2), -1, 1)
            churn = min(1, (abs(bq-oq)+abs(aq-oaq))/max(1, bq+aq+oq+oaq))
            self.flow += w*(ofi-self.flow)
            self.imbalance += w*(imbalance-self.imbalance)
            self.churn += w*(churn-self.churn)
            self.trend += slow*((mid-om)/dt-self.trend)
            self.variance += slow*((mid-om)**2/dt-self.variance)
            self.samples += 1
        alpha = (.25*(micro-mid)+.006*self.imbalance+.004*self.flow+.5*self.trend)
        alpha = clamp(alpha*(1-.6*self.churn), -.04, .04)
        direction = 1 if alpha > .002 else -1 if alpha < -.002 else 0
        if direction != self.direction:
            self.since = now
        self.direction = direction
        self.when, self.last = now, (bid, bq, ask, aq, mid)
        return Snapshot(bid, ask, mid, mid+alpha, math.sqrt(max(0, self.variance)),
                        self.imbalance, self.flow, self.churn,
                        now-self.since if direction else 0, self.samples)
