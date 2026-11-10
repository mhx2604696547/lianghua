from dataclasses import replace
from datetime import date
from datetime import time

import pytest
import pandas as pd

from ashare_research.engine import (ExecutionSpec, FrozenIntent, OpenEvent,
                                   project_open_events, run_open_proxy as _run)

DAYS = [date(2026, 9, day) for day in (14, 15, 16, 17)]
SPEC = ExecutionSpec(10000, 0.0003, 5, 0.0005, 0)


def run_open_proxy(events, intents, spec, **kwargs):
    return _run(events, intents, spec, trading_calendar=DAYS, **kwargs)


def events(prices=(10, 10, 11, 11)):
    return [OpenEvent(d, "600519.SH", p, f"{d}T09:00:00+08:00", True, 12, 8)
            for d, p in zip(DAYS, prices)]


def intent(day=1, side="buy", limit=11, quantity=100, name="entry"):
    return FrozenIntent(name, DAYS[day], f"{DAYS[day-1]}T18:00:00+08:00",
                        "600519.SH", side, quantity, limit)


def test_roundtrip_uses_upstream_account_and_fees():
    run = run_open_proxy(events(), [intent(), intent(2, "sell", 9, name="exit")], SPEC)
    assert len(run.result.executions_df) == 2
    assert run.result.executions_df["price"].tolist() == [10, 11]
    assert run.result.executions_df["commission"].tolist() == pytest.approx([5, 5.55])
    assert float(run.result.equity_curve.squeeze().iloc[-1]) == pytest.approx(10089.45)
    actual = pd.to_datetime(run.result.executions_df["timestamp"])
    assert all(stamp.time() == time(9, 30) and stamp.nanosecond == 1 for stamp in actual)
    assert all(pd.Timestamp(s["callback_at"]).time() < time(9, 30) for s in run.submissions)
    assert run.metadata["valuation"] == "opening_prices"
    assert run.metadata["acceptance"] == "engineering_only"


def test_open_at_limit_is_not_backfilled_by_later_prices():
    rows = events((10, 12, 11, 11))
    run = run_open_proxy(rows, [intent(limit=13)], SPEC)
    assert run.result.executions_df.empty


def test_suspended_and_unknown_states_do_not_generate_orders():
    for known in (False, None):
        rows = events()
        rows[1] = replace(rows[1], tradable=known)
        run = run_open_proxy(rows, [intent()], SPEC)
        assert run.result.executions_df.empty
        assert len(run.rejections) == 1


def test_unknown_limits_are_not_assumed_normal():
    rows = events()
    rows[1] = replace(rows[1], upper_limit=None)
    run = run_open_proxy(rows, [intent()], SPEC)
    assert run.rejections[0]["reason"] == "price_limits_unavailable"


def test_no_market_performance_mode():
    with pytest.raises(ValueError, match="not enabled"):
        run_open_proxy(events(), [intent()], SPEC, data_kind="market")


def test_decision_cannot_be_made_at_current_open():
    item = replace(intent(), decided_at=f"{DAYS[1]}T09:30:00+08:00")
    with pytest.raises(ValueError, match="prior-session"):
        run_open_proxy(events(), [item], SPEC)


def test_unknown_open_valuation_fails_instead_of_zero_return():
    rows = events()
    rows[1] = replace(rows[1], open_price=float("nan"))
    with pytest.raises(ValueError, match="valuation"):
        run_open_proxy(rows, [intent()], SPEC)


def test_missing_entire_session_fails_instead_of_carrying_old_prices():
    rows = events()
    del rows[2]
    with pytest.raises(ValueError, match="every session"):
        run_open_proxy(rows, [intent()], SPEC)


def test_off_tick_order_is_rejected():
    with pytest.raises(ValueError, match="tick_size"):
        run_open_proxy(events(), [intent(limit=10.001)], SPEC)


def test_late_preopen_state_is_not_used_by_earlier_submission():
    rows = events()
    rows[1] = replace(rows[1], state_known_at=f"{DAYS[1]}T09:20:00+08:00")
    run = run_open_proxy(rows, [intent()], SPEC)
    assert run.rejections[0]["reason"] == "state_not_available_at_submission"
    assert run.result.executions_df.empty


def test_first_session_and_monday_open_do_not_depend_on_previous_bar():
    days = [date(2026, 9, d) for d in (18, 21, 22)]
    rows = [OpenEvent(d, "600519.SH", 10, f"{d}T09:00:00+08:00", True, 12, 8)
            for d in days]
    plans = [FrozenIntent("first", days[0], "2026-09-17T18:00:00+08:00",
                          "600519.SH", "buy", 100, 11),
             FrozenIntent("monday", days[1], "2026-09-18T18:00:00+08:00",
                          "600519.SH", "sell", 100, 9)]
    run = _run(rows, plans, SPEC, trading_calendar=days)
    assert run.result.executions_df["side"].tolist() == ["buy", "sell"]


def test_changing_full_day_fields_does_not_change_opening_execution():
    frame = pd.DataFrame([{"date": e.session, "symbol": e.symbol, "open": e.open_price,
                           "state_known_at": e.state_known_at, "tradable": e.tradable,
                           "upper_limit": e.upper_limit, "lower_limit": e.lower_limit,
                           "high": 11, "low": 9, "close": 10, "volume": 1000}
                          for e in events()])
    plans = [intent(), intent(2, "sell", 9, name="exit")]
    before = run_open_proxy(project_open_events(frame), plans, SPEC).result
    frame.loc[:, ["high", "low", "close", "volume"]] = [99, 1, 88, 0]
    after = run_open_proxy(project_open_events(frame), plans, SPEC).result
    fields = ["symbol", "side", "quantity", "price", "commission", "timestamp"]
    pd.testing.assert_frame_equal(before.executions_df[fields], after.executions_df[fields])
    pd.testing.assert_series_equal(before.equity_curve.squeeze(), after.equity_curve.squeeze())


def test_intraday_sale_is_locked_by_upstream_t_plus_one(monkeypatch):
    import akquant as aq
    import ashare_research.engine as module

    class IntradayProbe(module._FrozenIntentStrategy):
        def on_bar(self, bar):
            today = pd.Timestamp(bar.timestamp, unit="ns", tz="UTC").tz_convert(
                "Asia/Shanghai").date()
            if today == DAYS[1] and self.get_position(bar.symbol) >= 100:
                assert self.get_available_position(bar.symbol) == 0
                self.sell(bar.symbol, 100, price=9, fill_mode=aq.CurrentClose())

    monkeypatch.setattr(module, "_FrozenIntentStrategy", IntradayProbe)
    run = run_open_proxy(events(), [intent(), intent(2, "sell", 9, name="exit")], SPEC)
    orders = run.result.orders_df
    rejected = orders.loc[orders["status"].astype(str).str.contains("reject")]
    assert len(rejected) == 1
    assert "available position" in rejected.iloc[0]["reject_reason"]
    assert len(run.result.executions_df) == 2


def test_down_limit_sale_stays_unfilled_until_a_new_eligible_intent():
    plans = [intent(), intent(2, "sell", 7, name="blocked_exit"),
             intent(3, "sell", 9, name="retry_exit")]
    run = run_open_proxy(events((10, 10, 8, 11)), plans, SPEC)
    assert run.result.executions_df["price"].tolist() == [10, 11]
    assert len(run.result.orders_df) == 3


def test_insufficient_cash_is_rejected_by_upstream():
    run = run_open_proxy(events(), [intent()], replace(SPEC, initial_cash=100))
    assert run.result.executions_df.empty
    assert run.result.orders_df["status"].astype(str).str.contains("reject").all()

