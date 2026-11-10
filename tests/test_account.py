import json
from datetime import date
from decimal import Decimal

import pytest

from ashare_research.account import AccountQuote, CashDividend, EntryPlan, FeePeriod, run_account_path
from ashare_research.cli import account_smoke
from ashare_research.engine import ExecutionSpec, FrozenIntent, OpenEvent, run_open_proxy


DAYS = [date(2026, 9, i) for i in (14, 15, 16, 17, 18, 21, 22)]
A = "600519.SH"
B = "600000.SH"
FEES = [FeePeriod(DAYS[0], 0.0003, 5, 0.0005, 0.00001)]


def quote(i, price, *, symbol=A, mark=None, tradable=True, capacity=None, known="09:00:00"):
    return AccountQuote(
        OpenEvent(DAYS[i], symbol, price, f"{DAYS[i]}T{known}+08:00", tradable, 12, 8),
        mark, capacity,
    )


def entry(i, *, symbol=A, hold=1, quantity=100, label="entry", limit=11):
    return EntryPlan(
        FrozenIntent(label, DAYS[i], f"{DAYS[i - 1]}T18:00:00+08:00", symbol, "buy", quantity, limit),
        hold,
    )


def test_smoke_reconciles_a_halt_price_limit_receivable_and_changed_fee(tmp_path):
    proof = json.loads(account_smoke(tmp_path).read_bytes())
    assert proof["acceptance"] == "engineering_only" and proof["data_kind"] == "synthetic"
    rows = {row["session"]: row for row in proof["account_path"]}
    assert rows["2026-06-26"]["positions"] == {A: 100}
    assert rows["2026-06-26"]["opening_equity_cny"] is None
    assert rows["2026-06-26"]["missing_opening_marks"] == [A]
    assert Decimal(rows["2026-06-26"]["receivable_cny"]) == 235
    assert Decimal(rows["2026-06-26"]["cash_cny"]) == 95
    assert rows["2026-06-29"]["positions"] == {A: 100, B: 100}
    assert Decimal(rows["2026-06-29"]["opening_equity_cny"]) == 2025
    assert Decimal(rows["2026-06-30"]["opening_equity_cny"]) == Decimal("2232.88")
    assert rows["2026-06-30"]["positions"] == {}
    by_order = {row["order_id"]: row for row in proof["orders"]}
    assert by_order["buy:before_cash_release"]["reason"] == "cash_reservation_insufficient"
    assert by_order["buy:after_cash_release"]["status"] == "filled"
    assert by_order["sell:2026-06-26:distribution_holder"]["reason"] == "suspended"
    assert by_order["sell:2026-06-29:distribution_holder"]["reason"] == "opening_lower_limit"
    assert Decimal(by_order["sell:2026-06-30:distribution_holder"]["fees_cny"]) == Decimal("6.90")
    assert proof["lots"][0]["planned_exit_session"] == "2026-06-26"
    assert proof["lots"][0]["actual_exit_session"] == "2026-06-30"
    assert proof["lots"][0]["actual_holding_sessions"] == 3


def test_registered_dividend_survives_sale_before_payment_and_ex_date_buy_gets_no_rights():
    a = CashDividend("div_a", A, DAYS[1], DAYS[2], DAYS[3], "0.75")
    b = CashDividend("div_b", B, DAYS[1], DAYS[2], DAYS[3], "0.75")
    result = run_account_path(
        [quote(1, 10, mark=10), quote(2, 11, mark=11),
         quote(2, 10, symbol=B, mark=10), quote(3, 11, symbol=B, mark=11)],
        [entry(1), entry(2, symbol=B, label="ex_day_buyer")],
        trading_calendar=DAYS[:5], initial_cash=10000, fee_periods=FEES, dividends=[a, b],
    )
    recorded = {row["action_id"]: row for row in result.cash_actions if row["event"] == "record_close"}
    assert recorded["div_a"]["entitled_shares"] == 100
    assert recorded["div_b"]["entitled_shares"] == 0
    assert result.accounts[2]["positions"] == {B: 100}
    assert Decimal(result.accounts[3]["receivable_cny"]) == 75
    assert Decimal(result.accounts[4]["receivable_cny"]) == 0
    assert Decimal(result.accounts[4]["cash_cny"]) - Decimal(result.accounts[3]["cash_cny"]) == 75


def test_partial_opening_exit_keeps_remainder_when_quotes_end():
    run = run_account_path(
        [quote(1, 10, mark=10, capacity=200), quote(2, 11, mark=11, capacity=100)],
        [entry(1, quantity=300)],
        trading_calendar=DAYS[:4], initial_cash=10000, fee_periods=FEES,
    )
    assert [row["status"] for row in run.orders] == ["partial", "partial", "blocked"]
    assert run.orders[2]["quantity"] == 100
    assert run.orders[2]["reason"] == "preopen_quote_missing"
    assert run.lots[0]["entry_filled_shares"] == 200
    assert run.lots[0]["last_exit_fill_session"] == str(DAYS[2])
    assert run.lots[0]["actual_exit_session"] is None
    assert run.lots[0]["unresolved_shares"] == 100
    assert run.accounts[-1]["positions"] == {A: 100}
    assert run.accounts[-1]["opening_equity_cny"] is None
    assert run.metadata["open_positions_at_end"] is True


def test_entry_funding_cannot_use_same_open_sale_or_cheaper_realized_price():
    run = run_account_path(
        [quote(1, 10, mark=10), quote(2, 11, mark=11), quote(2, 8.1, symbol=B, mark=8.1)],
        [entry(1, limit=10.5), entry(2, symbol=B, label="next", limit=10)],
        trading_calendar=DAYS[:3], initial_cash=1100, fee_periods=FEES,
    )
    assert run.orders[1]["status"] == "filled"
    assert run.orders[2]["reason"] == "cash_reservation_insufficient"
    assert run.orders[2]["submitted_at"] is None
    assert run.accounts[-1]["positions"] == {}

    single = run_account_path(
        [quote(1, 8.1, mark=8.1)], [entry(1, limit=10)],
        trading_calendar=DAYS[:2], initial_cash=900, fee_periods=FEES,
    )
    assert single.orders[0]["reason"] == "cash_reservation_insufficient"
    assert single.accounts[-1]["positions"] == {}


def test_five_day_holding_uses_sessions_and_matches_submission_clock():
    quotes = [quote(i, 10, mark=10) for i in range(1, 6)] + [quote(6, 11, mark=11)]
    run = run_account_path(quotes, [entry(1, hold=5)], trading_calendar=DAYS,
                           initial_cash=10000, fee_periods=FEES)
    assert [row["session"] for row in run.orders] == [str(DAYS[1]), str(DAYS[6])]
    assert run.orders[1]["submitted_at"].endswith("09:14:59.999999999+08:00")
    assert run.lots[0]["actual_holding_sessions"] == 5
    late = run_account_path(
        [quote(1, 10, mark=10, known="09:15:00")], [entry(1)],
        trading_calendar=DAYS[:2], initial_cash=10000, fee_periods=FEES,
    )
    assert late.orders[0]["reason"] == "state_not_available_at_submission"


def test_simple_account_path_agrees_with_akquant_positions_fees_and_cash():
    quotes = [quote(i, p, mark=p) for i, p in enumerate((10, 10, 11, 11))]
    plan = entry(1)
    managed = run_account_path(quotes, [plan], trading_calendar=DAYS[:4],
                               initial_cash=10000, fee_periods=FEES)
    legacy = run_open_proxy(
        [q.opening for q in quotes],
        [plan.intent, FrozenIntent("manual_exit", DAYS[2], f"{DAYS[1]}T18:00:00+08:00", A, "sell", 100, 9)],
        ExecutionSpec(10000, 0.0003, 5, 0.0005, 0.00001), trading_calendar=DAYS[:4],
    )
    fills = legacy.result.executions_df
    assert fills["side"].tolist() == [row["side"] for row in managed.orders]
    assert fills["quantity"].tolist() == [row["filled"] for row in managed.orders]
    assert fills["commission"].tolist() == pytest.approx([float(row["fees_cny"]) for row in managed.orders])
    assert float(managed.accounts[-1]["cash_cny"]) == pytest.approx(float(legacy.result.cash_curve.iloc[-1]))
    with pytest.raises(ValueError, match="market research"):
        run_account_path(quotes, [plan], trading_calendar=DAYS[:4],
                         initial_cash=10000, fee_periods=FEES, data_kind="market")
