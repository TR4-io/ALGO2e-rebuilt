"""Pure decisions; no HTTP and no order mutations."""
import math
from execution import MAKER_REBATE, TAKER_FEE
from signals import clamp


def floor_tick(x):
    return round(math.floor((x+1e-9)/.01)*.01, 2)


def ceil_tick(x):
    return round(math.ceil((x-1e-9)/.01)*.01, 2)


def phase(cfg, tick, volume, halted):
    if halted or tick >= cfg.flatten_at:
        return 'EXIT'
    if tick >= cfg.last_entry or tick >= cfg.reduce_at and volume >= cfg.minimum_volume:
        return 'REDUCE'
    return 'MAKE'


def pressure(cfg, tick, volume):
    if volume >= cfg.target_volume:
        return 0.0
    target = cfg.target_volume*min(1, max(0, tick)/cfg.qualify_by)
    return clamp((target-volume)/max(1, cfg.target_volume*.12), 0, 1)


def inventory_limit(cfg, snap, tick, cautious):
    limit = cfg.stock_budget/(1+snap.sigma/.03)
    limit *= .5 if cautious else 1
    if tick > cfg.qualify_by:
        limit *= max(.25, (cfg.flatten_at-tick)/(cfg.flatten_at-cfg.qualify_by))
    return max(500, int(limit))


def quote(ticker, action, snap, position, cfg, tick, volume, toxicity=0, cautious=False):
    sign = 1 if action == 'BUY' else -1
    limit = inventory_limit(cfg, snap, tick, cautious)
    reducing = sign*position < 0
    if not reducing and abs(position) >= limit:
        return None
    p = pressure(cfg, tick, volume)
    edge = cfg.min_edge*(1-.35*p)+.25*snap.sigma+toxicity+.003*snap.churn
    skew = position/limit*max(.02, 1.5*snap.sigma)
    center = snap.fair-skew
    improve = .01 if snap.spread >= .03-1e-9 and (p > .3 or reducing) else 0
    if action == 'BUY':
        price = floor_tick(min(snap.bid+improve, center+MAKER_REBATE[ticker]-edge, snap.ask-.01))
    else:
        price = ceil_tick(max(snap.ask-improve, center-MAKER_REBATE[ticker]+edge, snap.bid+.01))
    # Inventory preference cannot authorize adding exposure at negative raw edge.
    raw_edge = sign*(snap.fair-price)+MAKER_REBATE[ticker]-toxicity
    if price <= 0 or not reducing and raw_edge < cfg.min_edge*(1-.35*p):
        return None
    size = int(cfg.base_clip*(1+p)/(1+snap.sigma/.025+snap.churn))
    if cautious or volume >= cfg.target_volume:
        size = int(size*.5)
    size = min(cfg.max_clip, max(100, size//50*50))
    size = min(size, abs(position) if reducing else max(0, limit-abs(position)))
    return price, int(size), raw_edge


def take(ticker, snap, position, cfg, toxicity=0):
    if not cfg.taker_enabled or snap.samples < 5 or snap.stable_for < .8 or snap.churn > .6:
        return None
    # Charge BOTH taker legs; use a bid/ask exit estimate, not midpoint liquidation.
    buffer = cfg.taker_buffer + .5*snap.sigma + toxicity
    buy_edge = snap.fair-snap.spread/2-snap.ask-2*TAKER_FEE[ticker]-buffer
    sell_edge = snap.bid-(snap.fair+snap.spread/2)-2*TAKER_FEE[ticker]-buffer
    action, edge = ('BUY', buy_edge) if buy_edge > sell_edge else ('SELL', sell_edge)
    sign = 1 if action == 'BUY' else -1
    if edge <= 0 or sign*position >= cfg.stock_budget/3:
        return None
    return action, edge


def sweep(book, action, wanted, slippage):
    levels = book['asks' if action == 'BUY' else 'bids']
    if not levels:
        return None
    best = levels[0][0]
    qty, price = 0, best
    for p, available in levels:
        if abs(p-best) > slippage+1e-9:
            break
        qty += min(wanted-qty, available)
        price = p
        if qty >= wanted:
            break
    return (int(qty), price) if qty else None
