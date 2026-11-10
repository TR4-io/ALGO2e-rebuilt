"""All price coefficients are dollars/share; defaults require competitive practice."""
from dataclasses import dataclass, asdict
import json
import math


@dataclass(frozen=True)
class Config:
    minimum_volume: int = 100000  # supplied methodology: EACH run
    volume_buffer: float = 0.10
    qualify_by: float = 240
    reduce_at: float = 260
    last_entry: float = 275
    flatten_at: float = 280
    end_tick: float = 300
    gross_budget: int = 18000
    net_budget: int = 18000
    stock_budget: int = 6000
    base_clip: int = 1000
    max_clip: int = 2000
    taker_clip: int = 500
    poll_seconds: float = 0.10
    max_book_age: float = 0.60
    reprice_delay: float = 0.8
    min_edge: float = 0.0025
    taker_buffer: float = 0.008
    taker_cooldown: float = 1.5
    taker_enabled: bool = True
    max_spread: float = 0.30
    soft_loss: float = 500
    hard_loss: float = 1500
    drawdown: float = 900
    profit_arm: float = 500
    exit_slippage: float = 0.10
    urgent_slippage: float = 0.30

    @property
    def target_volume(self):
        return self.minimum_volume + math.ceil(self.minimum_volume * self.volume_buffer)

    def validate(self):
        for name, value in asdict(self).items():
            if name == 'taker_enabled':
                if type(value) is not bool:
                    raise ValueError('taker_enabled must be boolean')
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        for name in ('minimum_volume', 'gross_budget', 'net_budget', 'stock_budget',
                     'base_clip', 'max_clip', 'taker_clip'):
            if type(getattr(self, name)) is not int:
                raise ValueError(f'{name} must be an integer')
        if not (0 < self.qualify_by < self.reduce_at <= self.last_entry < self.flatten_at < self.end_tick <= 300):
            raise ValueError('Invalid round phase times')
        if not (0 < self.taker_clip <= self.max_clip <= 5000 and 0 < self.base_clip <= self.max_clip):
            raise ValueError('Invalid clip sizes')
        if not (0 < self.stock_budget <= 6500 and self.stock_budget <= self.gross_budget <= 22500
                and 0 < self.net_budget <= 22500):
            raise ValueError('Invalid share budgets')
        if not (0 < self.poll_seconds <= self.max_book_age <= 2 and self.soft_loss < self.hard_loss):
            raise ValueError('Invalid timing or loss limits')
        if self.exit_slippage > self.urgent_slippage or self.volume_buffer > 1:
            raise ValueError('Invalid exit or volume buffer')
        return self

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls().validate()
        with open(path, encoding='utf-8') as f:
            return cls(**json.load(f)).validate()
