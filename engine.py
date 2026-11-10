"""Exchange state machine. Single writer; confirmed fills reserve all open risk."""
from collections import defaultdict
from dataclasses import asdict
import json
import math
import time

from config import Config
from execution import (ApiError, HttpError, UnknownWrite, Order, Ledger, TICKERS,
                       MAKER_REBATE, integer, capacity, clean_book)
from signals import Market
from policy import phase, quote, take, sweep, inventory_limit


class MissingOrder(ApiError):
    """Order absent from detail and status snapshots."""


class Recorder:
    def __init__(self, stream=None):
        self.stream = stream

    def emit(self, kind, **fields):
        if self.stream:
            self.stream.write(json.dumps(dict(event=kind, wall_time=time.time(), **fields), allow_nan=False)+'\n')
            self.stream.flush()


class Bot:
    def __init__(self, client, cfg=None, recorder=None, clock=time.monotonic):
        self.api = client.call
        self.cfg = (cfg or Config()).validate()
        self.record = recorder or Recorder()
        self.clock = clock
        self.ledger = Ledger()
        self.markets = {t: Market() for t in TICKERS}
        self.snapshots = {}
        self.intents = {}
        self.notionals = {}
        self.pending = []
        self.toxicity = defaultdict(float)
        self.markout_count = defaultdict(int)
        self.last_take = defaultdict(lambda: -math.inf)
        self.period = None
        self.tick = -1
        self.case_seen = None
        self.active_seen = False
        self.round_changed = False
        self.stopping = False
        self.cautious = False
        self.mode = 'MAKE'
        self.start_pnl = None
        self.peak = 0
        self.last_report = -10
        self.gross_limit = self.cfg.gross_budget
        self.net_limit = self.cfg.net_budget

    def securities(self):
        rows = {r['ticker']: r for r in self.api('GET', 'securities')}
        if not set(TICKERS).issubset(rows):
            raise ValueError('Expected ALGO2e CNR, RY, AC')
        if any(integer(r['position']) for t, r in rows.items() if t not in TICKERS):
            raise ValueError('Unexpected inventory in another security')
        return rows

    @staticmethod
    def pnl(rows):
        try:
            value = sum(float(rows[t]['realized'])+float(rows[t]['unrealized']) for t in TICKERS)
            return value if math.isfinite(value) else None
        except (KeyError, ValueError, TypeError):
            return None

    def start(self):
        case = self.api('GET', 'case')
        self.period = case.get('period')
        rows = self.securities()
        if any(integer(rows[t]['position']) for t in TICKERS) or self.api('GET', 'orders', status='OPEN'):
            raise ValueError('Start flat with no open orders; use one bot and no manual trades')
        limits = self.api('GET', 'limits')
        if not isinstance(limits, list) or not limits:
            raise ValueError('Cannot verify live limits')
        for row in limits:
            g, n = float(row['gross_limit']), float(row['net_limit'])
            if not math.isfinite(g+n) or min(g, n) <= 0:
                raise ValueError('Invalid live limits')
            self.gross_limit = min(self.gross_limit, int(g*.9))
            self.net_limit = min(self.net_limit, int(n*.9))
        self.start_pnl = self.pnl(rows)
        self.record.emit('start', config=asdict(self.cfg), gross_limit=self.gross_limit, net_limit=self.net_limit)
        print(f'Connected. Minimum {self.cfg.minimum_volume:,} shares EACH run; target {self.cfg.target_volume:,}.')
        print(f'Gross/net budgets: {self.gross_limit}/{self.net_limit}.')
        if self.start_pnl is None:
            print('P&L unavailable: dollar stops disabled; share risk limits remain active.')

    def update(self, row):
        oid = integer(row['order_id'])
        o = self.ledger.orders[oid]
        before = o.filled
        self.ledger.update(row)
        delta = o.filled-before
        if not delta:
            return
        # Optional server cumulative VWAP. Never label order limit as execution price.
        fill_price = None
        try:
            vwap = float(row['vwap'])
            if math.isfinite(vwap) and vwap > 0:
                notional = vwap*o.filled
                if before == 0 or oid in self.notionals:
                    fill_price = (notional-self.notionals.get(oid, 0))/delta
                    if not math.isfinite(fill_price) or fill_price <= 0:
                        fill_price = None
                self.notionals[oid] = notional
            else:
                self.notionals.pop(oid, None)
        except (KeyError, ValueError, TypeError):
            self.notionals.pop(oid, None)
        now = self.clock()
        intent = self.intents.get(oid, 'unknown')
        self.record.emit('fill', tick=self.tick, order_id=oid, ticker=o.ticker, action=o.action,
                         quantity=delta, limit_price=o.price, execution_price=fill_price, intent=intent)
        snapshot = self.snapshots.get(o.ticker)
        if snapshot and now-snapshot[1] <= self.cfg.max_book_age:
            for horizon in (.5, 1., 3.):
                self.pending.append((now, horizon, o.ticker, o.action, delta, snapshot[0].mid, fill_price, intent))

    def lookup_once(self, oid):
        try:
            return self.api('GET', f'orders/{oid}')
        except HttpError as exc:
            if exc.status != 404:
                raise
            for status in ('OPEN', 'TRANSACTED', 'CANCELLED'):
                row = next((r for r in self.api('GET', 'orders', status=status)
                            if integer(r['order_id']) == oid), None)
                if row is not None:
                    return row
            raise MissingOrder(f'Order {oid} missing; refusing to guess its fills')

    def lookup(self, oid):
        # Detail and status lists can briefly disagree during a transition.
        for attempt in range(11):
            try:
                return self.lookup_once(oid)
            except MissingOrder:
                if attempt == 10:
                    raise
                time.sleep(0.1)

    def reconcile(self):
        rows = {integer(r['order_id']): r for r in self.api('GET', 'orders', status='OPEN')}
        if any(oid not in self.ledger.orders for oid in rows):
            raise ApiError('Untracked order; account must have one writer')
        for o in list(self.ledger.active()):
            self.update(rows[o.id] if o.id in rows else self.lookup(o.id))

    def cancel(self, o, reason='replace'):
        try:
            self.api('DELETE', f'orders/{o.id}')
        except HttpError as exc:
            if exc.status != 404:
                raise
        # DELETE acknowledgement is not a final fill report. Poll reads only;
        # retain the remaining reservation and never submit while unresolved.
        for attempt in range(11):
            try:
                self.update(self.lookup_once(o.id))
            except MissingOrder:
                pass
            else:
                if o.status != 'OPEN' or o.filled == o.quantity:
                    self.record.emit('cancel', tick=self.tick, order_id=o.id, reason=reason)
                    return
            if attempt < 10:
                time.sleep(0.1)
        raise ApiError(f'Cancellation of order {o.id} not confirmed after 11 checks; '
                       'no replacement submitted')

    def cancel_all(self):
        for o in list(self.ledger.active()):
            self.cancel(o, 'cancel_all')

    def submit(self, ticker, action, qty, price, intent, observed):
        # Last-moment gate, shared by maker, taker and liquidation.
        if self.clock()-observed > self.cfg.max_book_age or qty <= 0 or price <= 0:
            return None
        if self.case_seen is not None:
            estimated_tick = self.tick + max(0, self.clock()-self.case_seen)
            if estimated_tick >= self.cfg.end_tick or (
                    intent in ('MAKE', 'TAKE') and estimated_tick >= self.cfg.last_entry):
                return None
        qty = capacity(self.ledger.positions, self.ledger.active(), ticker, action, int(qty),
                       self.gross_limit, self.net_limit, self.cfg.stock_budget)
        if not qty:
            return None
        for other in self.ledger.active(ticker):
            if other.action != action and ((action == 'BUY' and price >= other.price)
                                          or (action == 'SELL' and price <= other.price)):
                return None
        row = self.api('POST', 'orders', ticker=ticker, action=action, type='LIMIT', quantity=qty, price=price)
        try:
            oid = integer(row['order_id'])
            if oid in self.ledger.orders:
                raise ValueError('Reused order ID')
            self.ledger.orders[oid] = Order(oid, ticker, action, qty, price, self.clock())
            self.intents[oid] = intent
            self.record.emit('submit', tick=self.tick, order_id=oid, ticker=ticker, action=action,
                             quantity=qty, price=price, intent=intent)
            self.update(row)
            return self.ledger.orders[oid]
        except (KeyError, ValueError, TypeError) as exc:
            raise UnknownWrite('Accepted write could not be reconciled; never replay it') from exc

    def book(self, ticker):
        started = self.clock()
        raw = self.api('GET', 'securities/book', ticker=ticker, limit=1000)
        book = clean_book(raw, {str(o.id) for o in self.ledger.orders.values()})
        return book, started

    def observe(self, ticker, book, observed):
        if self.clock()-observed > self.cfg.max_book_age:
            return None
        snap = self.markets[ticker].observe(book, self.clock())
        if snap:
            self.snapshots[ticker] = (snap, observed)
            self.markouts(ticker, snap)
            self.record.emit('book', tick=self.tick, ticker=ticker, bids=book['bids'][:10], asks=book['asks'][:10],
                             signals=asdict(snap), request_seconds=self.clock()-observed)
        return snap

    def markouts(self, ticker, snap):
        now = self.clock()
        keep = []
        for born, horizon, t, side, qty, ref, price, intent in self.pending:
            if t != ticker or now-born < horizon:
                if now-born < 10:
                    keep.append((born, horizon, t, side, qty, ref, price, intent))
                continue
            if now-born > horizon+.75:
                continue  # do not mislabel delayed observations as short-horizon markouts
            sign = 1 if side == 'BUY' else -1
            drift = sign*(snap.mid-ref)
            self.record.emit('markout', tick=self.tick, ticker=t, action=side, quantity=qty,
                             horizon=horizon, elapsed=now-born, post_detection_drift=drift,
                             gross_markout=sign*(snap.mid-price) if price is not None else None, intent=intent)
            if horizon == 1 and intent == 'MAKE':
                key = (t, side)
                self.markout_count[key] += 1
                self.toxicity[key] += .1*(max(0, -drift)-self.toxicity[key])
        self.pending = keep

    def penalty(self, ticker, action):
        key = (ticker, action)
        return min(.03, self.toxicity[key])*min(1, self.markout_count[key]/10)

    def manage_exit(self, ticker, book, observed):
        # No midpoint/ask required to sell, and no bid required to buy back shorts.
        active = self.ledger.active(ticker)
        if active:
            for o in list(active):
                self.cancel(o, 'exit_refresh')
            book, observed = self.book(ticker)
        pos = self.ledger.positions[ticker]
        if not pos:
            return
        action = 'SELL' if pos > 0 else 'BUY'
        slip = self.cfg.urgent_slippage if self.tick >= self.cfg.end_tick-8 else self.cfg.exit_slippage
        result = sweep(book, action, min(5000, abs(pos)), slip)
        if result:
            qty, price = result
            order = self.submit(ticker, action, qty, price, 'EXIT', observed)
            if order and order.remaining:
                self.cancel(order, 'exit_unfilled_remainder')

    def manage(self, ticker):
        book, observed = self.book(ticker)
        if self.mode == 'EXIT':
            self.manage_exit(ticker, book, observed)
            return
        snap = self.observe(ticker, book, observed)
        if snap is None or snap.spread > self.cfg.max_spread:
            for o in list(self.ledger.active(ticker)):
                self.cancel(o, 'invalid_or_stale_book')
            return
        # An order's age is not by itself a reason to lose queue priority.
        changed = False
        for o in list(self.ledger.active(ticker)):
            pos = self.ledger.positions[ticker]
            sign = 1 if o.action == 'BUY' else -1
            reducing = sign*pos < 0
            target = quote(ticker, o.action, snap, pos, self.cfg, self.tick, self.ledger.volume,
                           self.penalty(ticker, o.action), self.cautious)
            edge = sign*(snap.fair-o.price)+MAKER_REBATE[ticker]-self.penalty(ticker, o.action)
            crossing = o.price >= snap.ask if sign == 1 else o.price <= snap.bid
            reduce_invalid = self.mode == 'REDUCE' and (not reducing or o.remaining > abs(pos))
            at_best = o.price >= snap.bid if sign == 1 else o.price <= snap.ask
            limit = inventory_limit(self.cfg, snap, self.tick, self.cautious)
            too_large = not reducing and abs(pos)+o.remaining > limit
            lost_edge = not reducing and edge < 0
            moved = target and abs(o.price-target[0]) >= .01-1e-9
            reprice = moved and (not at_best or abs(o.price-target[0]) >= .02-1e-9)
            reprice = reprice and self.clock()-o.born >= self.cfg.reprice_delay
            if crossing or reduce_invalid or lost_edge or too_large or target is None or reprice:
                self.cancel(o, 'risk_or_edge' if lost_edge or too_large else 'price_or_phase')
                changed = True
        if changed:
            book, observed = self.book(ticker)
            snap = self.observe(ticker, book, observed)
            if snap is None or snap.spread > self.cfg.max_spread:
                return
        pos = self.ledger.positions[ticker]
        # Reducing existing risk can justify paying a spread before the closeout phase.
        dynamic_limit = inventory_limit(self.cfg, snap, self.tick, self.cautious)
        adverse_direction = (snap.fair-snap.mid)*pos < -.003*abs(pos)
        urgent_inventory = abs(pos) > dynamic_limit or (
            abs(pos) >= .7*dynamic_limit and adverse_direction and snap.stable_for >= 1)
        if pos and urgent_inventory and self.clock()-self.last_take[ticker] >= self.cfg.taker_cooldown:
            for o in list(self.ledger.active(ticker)):
                self.cancel(o, 'inventory_reduce')
            book, observed = self.book(ticker)
            pos = self.ledger.positions[ticker]
            if pos:
                action = 'SELL' if pos > 0 else 'BUY'
                result = sweep(book, action, min(self.cfg.base_clip, abs(pos)), 0)
                if result:
                    qty, price = result
                    order = self.submit(ticker, action, qty, price, 'RISK_REDUCE', observed)
                    if order:
                        self.last_take[ticker] = self.clock()
                        if order.remaining:
                            self.cancel(order, 'risk_unfilled_remainder')
            return
        if self.mode == 'MAKE' and not self.cautious and self.tick < self.cfg.qualify_by:
            candidate = take(ticker, snap, pos, self.cfg, max(self.penalty(ticker, 'BUY'), self.penalty(ticker, 'SELL')))
            if candidate and self.clock()-self.last_take[ticker] >= self.cfg.taker_cooldown:
                # Cancel all own quotes, then refresh the decision after racing fills.
                for o in list(self.ledger.active(ticker)):
                    self.cancel(o, 'taker_refresh')
                book, observed = self.book(ticker)
                snap = self.observe(ticker, book, observed)
                pos = self.ledger.positions[ticker]
                candidate = take(ticker, snap, pos, self.cfg, max(self.penalty(ticker, 'BUY'), self.penalty(ticker, 'SELL'))) if snap else None
                if candidate:
                    action, edge = candidate
                    levels = book['asks' if action == 'BUY' else 'bids']
                    dynamic = inventory_limit(self.cfg, snap, self.tick, self.cautious)
                    size = min(self.cfg.taker_clip, levels[0][1], max(0, dynamic-abs(pos)))
                    order = self.submit(ticker, action, size, levels[0][0], 'TAKE', observed)
                    if order:
                        self.last_take[ticker] = self.clock()
                        if order.remaining:
                            self.cancel(order, 'taker_unfilled_remainder')
                    return
                if snap is None:
                    return
        # Reduce exposure first; refresh position after every accepted order.
        sides = ('SELL', 'BUY') if pos > 0 else ('BUY', 'SELL')
        for action in sides:
            if any(o.action == action for o in self.ledger.active(ticker)):
                continue
            pos = self.ledger.positions[ticker]
            reducing = pos > 0 and action == 'SELL' or pos < 0 and action == 'BUY'
            if self.mode == 'REDUCE':
                if not reducing:
                    continue
                price = snap.ask if action == 'SELL' else snap.bid
                qty = min(self.cfg.max_clip, abs(pos))
            else:
                decision = quote(ticker, action, snap, pos, self.cfg, self.tick, self.ledger.volume,
                                 self.penalty(ticker, action), self.cautious)
                if decision is None:
                    continue
                price, qty, _ = decision
            self.submit(ticker, action, qty, price, self.mode, observed)

    def step(self):
        self.case_seen = self.clock()
        case = self.api('GET', 'case')
        tick = float(case['tick'])
        if not math.isfinite(tick):
            raise ValueError('Invalid case tick')
        if case.get('period') != self.period or tick < self.tick:
            self.round_changed = True
            raise ApiError('Case reset: restart this process for the next run')
        self.tick = tick
        if case['status'] != 'ACTIVE' or tick >= self.cfg.end_tick:
            self.cancel_all()
            return not (self.active_seen or self.stopping or tick >= self.cfg.end_tick)
        self.active_seen = True
        # Bounded reconciliation retries tolerate fills between independent snapshots.
        for _ in range(3):
            self.reconcile()
            rows = self.securities()
            if {t: integer(rows[t]['position']) for t in TICKERS} == self.ledger.positions:
                break
        else:
            raise ApiError('Position snapshots do not reconcile; no new exposure')
        value = self.pnl(rows)
        pnl = value-self.start_pnl if value is not None and self.start_pnl is not None else None
        if pnl is not None:
            self.peak = max(self.peak, pnl)
            self.cautious = pnl <= -self.cfg.soft_loss
            if pnl <= -self.cfg.hard_loss or self.peak >= self.cfg.profit_arm and self.peak-pnl >= self.cfg.drawdown:
                self.stopping = True
        self.mode = phase(self.cfg, tick, self.ledger.volume, self.stopping)
        if tick-self.last_report >= 5:
            self.last_report = tick
            needed = max(0, self.cfg.minimum_volume-self.ledger.volume)
            rate = needed/max(1, self.cfg.last_entry-tick)
            print(f't={tick:.1f} volume={self.ledger.volume:,} need={needed:,} rate={rate:.0f}/s P&L={pnl} mode={self.mode}')
            self.record.emit('status', tick=tick, volume=self.ledger.volume, needed=needed, required_rate=rate,
                             pnl=pnl, positions=dict(self.ledger.positions), mode=self.mode, cautious=self.cautious)
        if self.stopping and not any(self.ledger.positions.values()) and not self.ledger.active():
            return False
        # Prior cycle opportunity estimate determines service priority, with rotation for ties.
        offset = int(tick) % 3
        tickers = list(TICKERS[offset:]+TICKERS[:offset])
        def opportunity(t):
            item = self.snapshots.get(t)
            if not item or self.clock()-item[1] > 2:
                return 0
            snap = item[0]
            return snap.spread+2*MAKER_REBATE[t]-snap.sigma-self.penalty(t, 'BUY')-self.penalty(t, 'SELL')
        tickers.sort(key=opportunity, reverse=True)
        for ticker in tickers:
            self.manage(ticker)
        return True

    def cleanup(self):
        if self.round_changed:
            print('Case reset: no old order IDs will be cancelled in the new run.')
            return
        try:
            case = self.api('GET', 'case')
            if case.get('period') != self.period or float(case['tick']) < self.tick:
                print('Reset during cleanup: inspect RIT.')
                return
            # Exclusive-account startup contract permits cancellation of unknown accepted writes.
            for ticker in TICKERS:
                self.api('POST', 'commands/cancel', ticker=ticker)
            self.reconcile()
            rows = self.securities()
            positions = {t: integer(rows[t]['position']) for t in TICKERS}
            opened = self.api('GET', 'orders', status='OPEN')
            qualified = self.ledger.volume >= self.cfg.minimum_volume
            self.record.emit('end', tick=self.tick, volume=self.ledger.volume, qualified=qualified,
                             positions=positions, open_orders=len(opened), server_pnl=self.pnl(rows))
            print(f'Final confirmed volume={self.ledger.volume:,}; qualified={qualified}; positions={positions}; open orders={len(opened)}')
            if opened or any(positions.values()):
                print('ATTENTION: remaining exposure requires inspection in RIT.')
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            print(f'Cleanup not confirmed: {exc}. Inspect RIT immediately.')
