"""Deterministic, synthetic account path for a daily opening execution scenario."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd

from .engine import FrozenIntent, OpenEvent, _aware, preopen_blocker
from .sources import bs_symbol


_CENT = Decimal("0.01")
_LOT_SIZE = 100
_CALLBACK = "09:14:59.999999999+08:00"


@dataclass(frozen=True, slots=True)
class AccountQuote:
    opening: OpenEvent
    mark_price: float | None
    fillable_shares: int | None = None


@dataclass(frozen=True, slots=True)
class EntryPlan:
    intent: FrozenIntent
    hold_sessions: int


@dataclass(frozen=True, slots=True)
class FeePeriod:
    effective_on: date
    commission_rate: str | float
    min_commission: str | float
    stamp_tax_rate: str | float
    transfer_fee_rate: str | float


@dataclass(frozen=True, slots=True)
class CashDividend:
    action_id: str
    symbol: str
    record_on: date
    ex_on: date
    pay_on: date
    cash_per_share: str


@dataclass(slots=True)
class AccountRun:
    orders: list[dict]
    accounts: list[dict]
    lots: list[dict]
    cash_actions: list[dict]
    metadata: dict


@dataclass(slots=True)
class _Lot:
    plan: EntryPlan
    entered: int
    due: int
    initial: int
    remaining: int
    last_exit: int | None = None


def _decimal(value: str | float | int, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("expected a finite monetary value") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and not number):
        raise ValueError("expected a finite monetary value in range")
    return number


def _price(value: float) -> Decimal:
    price = _decimal(value)
    if price % _CENT:
        raise ValueError("opening proxy requires prices on the CNY cent tick")
    return price


def _fees(notional: Decimal, side: str, policy: FeePeriod) -> Decimal:
    commission = max(notional * Decimal(str(policy.commission_rate)), Decimal(str(policy.min_commission)))
    tax = notional * Decimal(str(policy.stamp_tax_rate)) if side == "sell" else Decimal(0)
    return commission + tax + notional * Decimal(str(policy.transfer_fee_rate))


def _fee_schedule(days: list[date], periods: list[FeePeriod]) -> dict[date, FeePeriod]:
    ordered = sorted(periods, key=lambda row: row.effective_on)
    if not ordered or ordered[0].effective_on > days[0]:
        raise ValueError("fee schedule does not cover the first session")
    if len({p.effective_on for p in ordered}) != len(ordered):
        raise ValueError("fee schedule has duplicate effective dates")
    for policy in ordered:
        for name, value in asdict(policy).items():
            if name == "effective_on":
                continue
            number = _decimal(value, allow_zero=True)
            if name.endswith("_rate") and number >= 1:
                raise ValueError("fee rate must be less than one")
        combined_rate = sum(Decimal(str(value)) for value in (
            policy.commission_rate, policy.stamp_tax_rate, policy.transfer_fee_rate,
        ))
        if combined_rate >= 1:
            raise ValueError("combined variable fees must be less than the trade notional")
    schedule, index = {}, 0
    for day in days:
        while index + 1 < len(ordered) and ordered[index + 1].effective_on <= day:
            index += 1
        schedule[day] = ordered[index]
    return schedule


def _quote_map(quotes: list[AccountQuote], index: dict[date, int]) -> dict[tuple[date, str], AccountQuote]:
    by_key = {}
    for quote in quotes:
        event = quote.opening
        bs_symbol(event.symbol)
        key = (event.session, event.symbol)
        if event.session not in index or key in by_key:
            raise ValueError("opening quote must uniquely match a calendar session")
        if event.tradable is not None and type(event.tradable) is not bool:
            raise ValueError("tradable must be true, false or unknown")
        _aware(event.state_known_at)
        for price in (event.open_price, quote.mark_price, event.upper_limit, event.lower_limit):
            if price is not None:
                _price(price)
        if event.upper_limit is not None and event.lower_limit is not None and event.lower_limit >= event.upper_limit:
            raise ValueError("invalid daily price limit order")
        if quote.fillable_shares is not None and (
            type(quote.fillable_shares) is not int or quote.fillable_shares < 0
        ):
            raise ValueError("opening share capacity must be a nonnegative integer")
        by_key[key] = quote
    return by_key


def _entries_by_day(entries: list[EntryPlan], index: dict[date, int]) -> dict[date, list[EntryPlan]]:
    result = defaultdict(list)
    seen = set()
    for plan in entries:
        intent = plan.intent
        bs_symbol(intent.symbol)
        if not intent.intent_id or intent.intent_id in seen or intent.side != "buy":
            raise ValueError("entry plans require distinct buy intent IDs")
        seen.add(intent.intent_id)
        if type(plan.hold_sessions) is not int or not 1 <= plan.hold_sessions <= 5:
            raise ValueError("holding period must be one to five sessions")
        if type(intent.quantity) is not int or intent.quantity <= 0 or intent.quantity % _LOT_SIZE:
            raise ValueError("entry quantity must be a positive whole lot")
        _price(intent.limit_price)
        decided = _aware(intent.decided_at)
        signal, entry = index.get(decided.date()), index.get(intent.execution_day)
        if signal is None or entry != signal + 1 or decided.time() < time(15, 0):
            raise ValueError("entry must follow the preceding completed trading session")
        result[intent.execution_day].append(plan)
    return {day: sorted(plans, key=lambda p: p.intent.intent_id) for day, plans in result.items()}


def _dividends_by_record(
    dividends: list[CashDividend], days: list[date], index: dict[date, int],
) -> dict[date, list[CashDividend]]:
    result = defaultdict(list)
    identities, record_keys = set(), set()
    for action in dividends:
        bs_symbol(action.symbol)
        _decimal(action.cash_per_share)
        key = (action.symbol, action.record_on)
        if not action.action_id or action.action_id in identities or key in record_keys:
            raise ValueError("duplicate cash dividend or record date for this security")
        if action.record_on not in index or not action.record_on < action.ex_on <= action.pay_on:
            raise ValueError("cash dividend dates are outside the supported record/ex/pay order")
        after_record = index[action.record_on] + 1
        if after_record < len(days) and days[after_record] != action.ex_on:
            raise ValueError("this cash scenario requires the next trading session as ex-date")
        identities.add(action.action_id)
        record_keys.add(key)
        result[action.record_on].append(action)
    return result


def _prepare(
    day: date, callback: pd.Timestamp, plan: EntryPlan, lot: _Lot | None,
    quote: AccountQuote | None, spendable: Decimal, fee: FeePeriod,
) -> tuple[dict, Decimal | None]:
    intent = plan.intent
    side = "buy" if lot is None else "sell"
    qty = intent.quantity if lot is None else lot.remaining
    order = {
        "order_id": f"buy:{intent.intent_id}" if lot is None else f"sell:{day}:{intent.intent_id}",
        "parent_intent_id": intent.intent_id, "symbol": intent.symbol, "session": day.isoformat(),
        "side": side, "quantity": qty, "filled": 0, "signal_as_of": intent.decided_at,
        "checked_at": callback.isoformat(), "submitted_at": None, "fill_at": None,
        "status": "blocked", "reason": None, "fee_effective_on": fee.effective_on.isoformat(),
    }
    reason = preopen_blocker(quote.opening, callback) if quote else "preopen_quote_missing"
    if reason:
        order["reason"] = reason
        return order, None
    assert quote is not None and quote.opening.lower_limit is not None and quote.opening.upper_limit is not None
    lower, upper = _price(quote.opening.lower_limit), _price(quote.opening.upper_limit)
    limit = min(_price(intent.limit_price), upper - _CENT) if side == "buy" else lower + _CENT
    if not lower <= limit <= upper:
        order["reason"] = "limit_outside_pinned_band"
        return order, None
    if side == "buy":
        reserve = limit * qty + _fees(limit * qty, side, fee)
    else:
        # A low-priced partial exit can cost more than its proceeds at the minimum commission.
        minimum_proceeds = limit * _LOT_SIZE
        reserve = max(Decimal(0), _fees(minimum_proceeds, side, fee) - minimum_proceeds)
    if reserve > spendable:
        order["reason"] = "cash_reservation_insufficient"
        return order, None
    order.update(status="submitted", submitted_at=callback.isoformat(), effective_limit_cny=str(limit),
                 reserved_cny=str(reserve))
    return order, reserve


def _fill(order: dict, quote: AccountQuote, capacity: int | None, fee: FeePeriod) -> tuple[int, Decimal]:
    event = quote.opening
    order["status"] = "unfilled"
    if event.open_price is None:
        order["reason"] = "opening_print_unavailable"
        return 0, Decimal(0)
    price = _price(event.open_price)
    assert event.lower_limit is not None and event.upper_limit is not None
    lower, upper = _price(event.lower_limit), _price(event.upper_limit)
    side, limit = order["side"], Decimal(order["effective_limit_cny"])
    if not lower <= price <= upper:
        order["reason"] = "opening_outside_pinned_band"
    elif side == "buy" and price == upper:
        order["reason"] = "opening_upper_limit"
    elif side == "sell" and price == lower:
        order["reason"] = "opening_lower_limit"
    elif (side == "buy" and price > limit) or (side == "sell" and price < limit):
        order["reason"] = "opening_outside_order_limit"
    if order["reason"]:
        return 0, Decimal(0)
    wanted = order["quantity"]
    qty = wanted if capacity is None else min(wanted, capacity // _LOT_SIZE * _LOT_SIZE)
    if not qty:
        order["reason"] = "opening_share_capacity_exhausted"
        return 0, Decimal(0)
    notional = price * qty
    fee_amount = _fees(notional, side, fee)
    cash_delta = notional - fee_amount if side == "sell" else -notional - fee_amount
    order.update(
        status="filled" if qty == wanted else "partial", filled=qty, fill_at=event.timestamp.isoformat(),
        price_cny=str(price), fees_cny=str(fee_amount), cash_delta_cny=str(cash_delta),
        reason=None if qty == wanted else "opening_share_capacity_partial",
    )
    return qty, cash_delta


def run_account_path(
    quotes: list[AccountQuote], entries: list[EntryPlan], *,
    trading_calendar: list[date], initial_cash: str | float, fee_periods: list[FeePeriod],
    dividends: list[CashDividend] | None = None, data_kind: str = "synthetic",
) -> AccountRun:
    """Advance a single virtual account through record dates, opening orders and marks."""
    if data_kind != "synthetic":
        raise ValueError("market research is not enabled for the account path")
    days = sorted(trading_calendar)
    index = {day: i for i, day in enumerate(days)}
    if not days or len(index) != len(days):
        raise ValueError("an explicit unique trading calendar is required")
    cash = _decimal(initial_cash)
    fee_schedule = _fee_schedule(days, fee_periods)
    by_quote = _quote_map(quotes, index)
    by_entry = _entries_by_day(entries, index)
    by_record = _dividends_by_record(dividends or [], days, index)
    lots: list[_Lot] = []
    accounts, orders, actions, rights = [], [], [], []
    for i, day in enumerate(days):
        callback = _aware(f"{day}T{_CALLBACK}")
        fees = fee_schedule[day]
        for right in rights:
            if right["state"] == "pending_ex_date" and right["action"].ex_on == day:
                right["state"] = "receivable"
                actions.append({"action_id": right["action"].action_id, "session": str(day),
                                "event": "receivable", "amount_cny": str(right["amount"])})
            if right["state"] == "receivable" and day > right["action"].pay_on:
                cash += right["amount"]
                right["state"] = "credited"
                actions.append({"action_id": right["action"].action_id, "session": str(day),
                                "event": "cash_credited", "amount_cny": str(right["amount"])})
        spendable = cash
        due = sorted((lot for lot in lots if lot.remaining and lot.due <= i),
                     key=lambda lot: (lot.due, lot.plan.intent.intent_id))
        attempts = [(lot.plan, lot) for lot in due] + [(plan, None) for plan in by_entry.get(day, [])]
        pending = []
        for plan, lot in attempts:
            quote = by_quote.get((day, plan.intent.symbol))
            order, reserve = _prepare(day, callback, plan, lot, quote, spendable, fees)
            orders.append(order)
            if reserve is not None:
                spendable -= reserve
                pending.append((order, plan, lot, quote))
        capacity = {}
        for order, plan, lot, quote in pending:
            symbol = order["symbol"]
            available = capacity.setdefault(symbol, quote.fillable_shares)
            filled, delta = _fill(order, quote, available, fees)
            if not filled:
                continue
            if available is not None:
                capacity[symbol] = available - filled
            cash += delta
            if lot is None:
                lots.append(_Lot(plan, i, i + plan.hold_sessions, filled, filled))
            else:
                lot.remaining -= filled
                lot.last_exit = i
        if cash < 0:
            raise RuntimeError("negative cash after opening settlement")
        positions: dict[str, int] = defaultdict(int)
        for lot in lots:
            if lot.remaining:
                positions[lot.plan.intent.symbol] += lot.remaining
        receivable = sum((right["amount"] for right in rights if right["state"] == "receivable"), Decimal(0))
        missing, marked_value = [], Decimal(0)
        for symbol, qty in sorted(positions.items()):
            quote = by_quote.get((day, symbol))
            if quote is None or quote.mark_price is None:
                missing.append(symbol)
            else:
                marked_value += qty * _price(quote.mark_price)
        accounts.append({
            "session": str(day), "cash_cny": str(cash), "receivable_cny": str(receivable),
            "positions": dict(sorted(positions.items())), "missing_opening_marks": missing,
            "opening_equity_cny": None if missing else str(cash + receivable + marked_value),
        })
        for action in by_record.get(day, []):
            qty = positions.get(action.symbol, 0)
            amount = (qty * _decimal(action.cash_per_share)).quantize(_CENT, rounding=ROUND_HALF_UP)
            actions.append({"action_id": action.action_id, "session": str(day), "event": "record_close",
                            "symbol": action.symbol, "entitled_shares": qty, "gross_amount_cny": str(amount)})
            if qty:
                rights.append({"action": action, "amount": amount, "state": "pending_ex_date"})

    lot_records = [{
        "parent_intent_id": lot.plan.intent.intent_id, "symbol": lot.plan.intent.symbol,
        "entry_session": str(days[lot.entered]), "entry_filled_shares": lot.initial,
        "planned_holding_sessions": lot.plan.hold_sessions,
        "planned_exit_session": str(days[lot.due]) if lot.due < len(days) else None,
        "last_exit_fill_session": str(days[lot.last_exit]) if lot.last_exit is not None else None,
        "actual_exit_session": str(days[lot.last_exit]) if lot.last_exit is not None and not lot.remaining else None,
        "actual_holding_sessions": lot.last_exit - lot.entered if lot.last_exit is not None and not lot.remaining else None,
        "unresolved_shares": lot.remaining,
    } for lot in lots]
    return AccountRun(orders, accounts, lot_records, actions, {
        "data_kind": "synthetic", "acceptance": "engineering_only",
        "engine": "daily_open_account_v1", "fill_fidelity": "daily_proxy",
        "valuation": "opening_marks_after_fills", "valuation_complete": all(
            row["opening_equity_cny"] is not None for row in accounts
        ),
        "open_positions_at_end": any(lot.remaining for lot in lots),
        "cash_dividend_policy": "gross_receivable_on_ex_date_credit_first_session_after_pay_date",
        "trade_fee_policy": "unrounded_decimal_fee_scenario",
        "order_funding": "buy_limit_and_fees_reserved_before_open_excluding_same_open_sale_proceeds",
    })
