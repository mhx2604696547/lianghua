"""Reuse AKQuant accounting on explicit opening-event projections.

This spike accepts synthetic scenarios only. Market-data studies remain disabled
until the PIT/universe/corporate-action integration has its own acceptance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import akquant as aq
import pandas as pd


def _aware(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.tz_convert("Asia/Shanghai")


@dataclass(frozen=True)
class OpenEvent:
    session: date
    symbol: str
    open_price: float | None
    state_known_at: str
    tradable: bool | None
    upper_limit: float | None
    lower_limit: float | None

    @property
    def timestamp(self) -> pd.Timestamp:
        return pd.Timestamp(f"{self.session.isoformat()} 09:30", tz="Asia/Shanghai")


def preopen_blocker(observation: OpenEvent, callback_at: pd.Timestamp) -> str | None:
    if _aware(observation.state_known_at) > callback_at:
        return "state_not_available_at_submission"
    if observation.tradable is not True:
        return "suspended" if observation.tradable is False else "state_unknown"
    if observation.upper_limit is None or observation.lower_limit is None:
        return "price_limits_unavailable"
    return None


def project_open_events(frame: pd.DataFrame) -> list[OpenEvent]:
    """Select opening data and externally supplied pre-open state, ignoring H/L/C/V.

    State columns must be supplied separately from daily trading-status summaries.
    This helper is only exposed to the synthetic engineering runner for now.
    """
    columns = ["date", "symbol", "open", "state_known_at", "tradable",
               "upper_limit", "lower_limit"]
    return [OpenEvent(pd.Timestamp(r[0]).date(), *r[1:])
            for r in frame[columns].itertuples(index=False, name=None)]


@dataclass(frozen=True)
class FrozenIntent:
    intent_id: str
    execution_day: date
    decided_at: str
    symbol: str
    side: str
    quantity: int
    limit_price: float


@dataclass(frozen=True)
class ExecutionSpec:
    initial_cash: float
    commission_rate: float
    min_commission: float
    stamp_tax_rate: float
    transfer_fee_rate: float
    tick_size: float = 0.01


@dataclass
class ProxyRun:
    result: Any
    rejections: list[dict]
    submissions: list[dict]
    metadata: dict


class _FrozenIntentStrategy(aq.Strategy):
    def __init__(self):
        super().__init__()
        self.events = {}
        self.intents = []
        self.spec = None
        self.seen = set()
        self.rejections = []
        self.submissions = []

    def on_start(self):
        for symbol in sorted({event.symbol for event in self.events.values()}):
            self.subscribe(symbol)

    def on_pre_open(self, event):
        day = pd.Timestamp(event["trading_date"]).date()
        if day in self.seen:
            return
        self.seen.add(day)
        callback_at = pd.Timestamp(event["timestamp"])
        if callback_at.tzinfo is None:
            callback_at = callback_at.tz_localize("UTC")
        callback_at = callback_at.tz_convert("Asia/Shanghai")
        for intent in self.intents:
            if intent.execution_day != day:
                continue
            observation = self.events[(day, intent.symbol)]
            reason = preopen_blocker(observation, callback_at)
            if reason:
                self.rejections.append({"intent_id": intent.intent_id, "reason": reason})
                continue
            # A rule-based bound, fixed before observing the opening price.
            tick = Decimal(str(self.spec.tick_size))
            limit = Decimal(str(intent.limit_price))
            if intent.side == "buy":
                limit = min(limit, Decimal(str(observation.upper_limit)) - tick)
            else:
                limit = max(limit, Decimal(str(observation.lower_limit)) + tick)
            if limit <= 0:
                self.rejections.append({"intent_id": intent.intent_id, "reason": "invalid_limit"})
                continue
            submit = self.buy if intent.side == "buy" else self.sell
            order_id = submit(
                symbol=intent.symbol, quantity=intent.quantity, price=float(limit),
                time_in_force=aq.TimeInForce.Day,
            )
            self.submissions.append({
                "intent_id": intent.intent_id, "decided_at": intent.decided_at,
                "effective_limit": str(limit), "order_id": str(order_id),
                "callback_at": callback_at.isoformat(),
            })

    def on_bar(self, bar):
        pass


def run_open_proxy(
    events: list[OpenEvent], intents: list[FrozenIntent], spec: ExecutionSpec,
    *, trading_calendar: list[date], data_kind: str = "synthetic",
) -> ProxyRun:
    """Run an explicitly synthetic integration scenario, with no full-day inputs."""
    if data_kind != "synthetic":
        raise ValueError("market research is not enabled by this integration spike")
    if not events:
        raise ValueError("events must not be empty")
    if not trading_calendar or len(set(trading_calendar)) != len(trading_calendar):
        raise ValueError("an explicit unique trading calendar is required")
    for name, value in asdict(spec).items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid execution parameter: {name}")
    if spec.initial_cash <= 0 or spec.tick_size <= 0:
        raise ValueError("initial_cash and tick_size must be positive")
    keys = {(e.session, e.symbol) for e in events}
    if len(keys) != len(events):
        raise ValueError("duplicate opening event")
    expected_keys = {(day, symbol) for day in trading_calendar
                     for symbol in {e.symbol for e in events}}
    if keys != expected_keys:
        raise ValueError("every instrument needs an opening valuation in every session")
    tick = Decimal(str(spec.tick_size))
    for e in events:
        if e.open_price is None or not math.isfinite(e.open_price) or e.open_price <= 0:
            raise ValueError("missing or invalid opening valuation")
        if _aware(e.state_known_at) >= e.timestamp:
            raise ValueError("trading status must be known before the opening")
        if e.tradable is not None and type(e.tradable) is not bool:
            raise ValueError("tradable must be true, false or unknown")
        for value in (e.upper_limit, e.lower_limit):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("invalid price limit")
        if (e.upper_limit is not None and e.lower_limit is not None
                and e.upper_limit <= e.lower_limit):
            raise ValueError("invalid price-limit ordering")
        for price in (e.open_price, e.upper_limit, e.lower_limit):
            if price is not None and Decimal(str(price)) % tick:
                raise ValueError("event price is not aligned to tick_size")
        if (e.tradable is True and e.upper_limit is not None
                and e.lower_limit is not None
                and not e.lower_limit <= e.open_price <= e.upper_limit):
            raise ValueError("opening price violates the supplied regular-session limits")
    event_map = {(e.session, e.symbol): e for e in events}
    if len({i.intent_id for i in intents}) != len(intents):
        raise ValueError("duplicate intent id")
    for i in intents:
        if (i.execution_day, i.symbol) not in event_map:
            raise ValueError("intent has no corresponding execution event")
        if i.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if type(i.quantity) is not int or i.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if i.side == "buy" and i.quantity % 100:
            raise ValueError("buy quantity must be a multiple of 100")
        if not math.isfinite(i.limit_price) or i.limit_price <= 0:
            raise ValueError("limit price must be finite and positive")
        if Decimal(str(i.limit_price)) % tick:
            raise ValueError("intent price is not aligned to tick_size")
        if _aware(i.decided_at).date() >= i.execution_day:
            raise ValueError("this after-close profile requires a prior-session decision")
    # This is an execution carrier, not a synthetic market-volume observation.
    bars = [
        aq.Bar(
            # The engine classifies a point Bar at end-1ns. Offset only its
            # carrier boundary; no later price or market information is supplied.
            timestamp=int(e.timestamp.value) + 1, symbol=e.symbol,
            open=e.open_price, high=e.open_price, low=e.open_price, close=e.open_price,
            volume=1.0 if e.tradable is True else 0.0,
        )
        for e in sorted(events, key=lambda e: (e.session, e.symbol))
    ]
    strategy = _FrozenIntentStrategy()
    strategy.events = event_map
    strategy.intents = intents
    strategy.spec = spec
    result = aq.run_backtest(
        data=bars, strategy=strategy, symbols=sorted({e.symbol for e in events}),
        trading_calendar=sorted(trading_calendar),
        fill_policy=aq.NextOpen(), timezone="Asia/Shanghai",
        initial_cash=spec.initial_cash, t_plus_one=True, lot_size=100,
        commission_rate=spec.commission_rate, min_commission=spec.min_commission,
        stamp_tax_rate=spec.stamp_tax_rate, transfer_fee_rate=spec.transfer_fee_rate,
        slippage={"type": "zero"}, volume_limit_pct=0,
        warmup_period=0, history_depth=0, show_progress=False,
    )
    handled = {item["intent_id"] for item in strategy.submissions + strategy.rejections}
    if handled != {intent.intent_id for intent in intents}:
        raise RuntimeError("the engine did not process every frozen intent")
    return ProxyRun(
        result, strategy.rejections, strategy.submissions,
        {"data_kind": "synthetic", "acceptance": "engineering_only",
         "engine": "akquant", "engine_version": aq.__version__,
         "fill_fidelity": "daily_proxy", "valuation": "opening_prices",
         "logical_open_time": "09:30 Asia/Shanghai", "carrier_boundary_offset_ns": 1,
         "volume_policy": "execution carrier only; no market liquidity claim",
         "corporate_actions": "not_supported", "fee_regime": "single_static_scenario"},
    )

