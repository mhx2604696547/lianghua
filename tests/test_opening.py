import pandas as pd
import pytest

from ashare_research.opening import OPENING_FIELDS, OPENING_SOURCE, price_bands
from ashare_research.pit import DailyPIT, EvidenceUnavailable, freeze_daily_cut
from ashare_research.snapshots import freeze
from ashare_research.sources import AcquiredData
from ashare_research.universe import CALENDAR_AUDIT_METHOD, LISTING_FIELDS


@pytest.fixture
def opening_gate(tmp_path):
    root, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    stamp = "2026-07-20T00:00:00Z"

    def pin(dataset, source, frame, **meta):
        return freeze(AcquiredData(dataset, source, frame, stamp, {"data_kind": "synthetic", **meta}), root)

    dates = [f"2026-07-0{day}" for day in range(2, 9)]
    calendar = pin("trading_calendar", "fixture", pd.DataFrame({
        "calendar_date": dates, "is_trading_day": list("1100111"),
    }), calendar_audit_method=CALENDAR_AUDIT_METHOD, first_date=dates[0], last_date=dates[-1])
    listings = pin("listing_evidence", "fixture", pd.DataFrame([
        ("600519.SH", "2010-01-01", None, "sse_main_a", "a" * 64),
        ("600190.SH", "2010-01-01", None, "sse_main_a", "b" * 64),
    ], columns=LISTING_FIELDS), latest_session_exclusive="2026-07-09", szse_main_a={}, notice_events=[{
        "symbol": "600519.SH", "kind": "risk_warning", "from": "2026-07-03", "until": "2026-07-08",
        "not_before_day": "2026-07-02", "source_sha256": "c" * 64,
    }])
    events = [
        ("600519.SH", "cash_dividend", "2026-07-06", "2026-07-07", "2026-07-03", "0.51",
         "d" * 64, "https://example.test/cash", "2026-07-01", "2026-07-02"),
        ("600190.SH", "delisting_first", "2026-07-06", "2026-07-07", "", "",
         "e" * 64, "https://example.test/delisting", "2026-07-02", "2026-07-03"),
        ("600190.SH", "suspended", "2026-07-06", "2026-07-08", "", "",
         "f" * 64, "https://example.test/halt", "2026-07-06", "2026-07-07"),
    ]
    rules = [{
        "venue": "SH", "id": rule_id, "from": start, "until": end, "not_before_day": "2026-07-01",
        "ratios": {"ordinary_mainboard": "0.10", "risk_warning": ratio, "delisting_later": "0.10"},
        "documents": [],
    } for rule_id, start, end, ratio in (
        ("older", "2026-07-01", "2026-07-06", "0.05"),
        ("newer", "2026-07-06", "2026-07-09", "0.10"),
    )]
    opening = pin("opening_evidence", OPENING_SOURCE, pd.DataFrame(events, columns=OPENING_FIELDS),
                  availability_policy="next_shanghai_calendar_day", rules=rules)

    def new_gate(*, mutate=False, mode="reconstructed", source="baostock"):
        bars = pd.DataFrame({
            "date": ["2026-07-03", "2026-07-06"], "code": ["sh.600519"] * 2,
            "open": ["10.00", "9.50"], "high": ["10.00", "100.00" if mutate else "9.80"],
            "low": ["10.00", "0.50" if mutate else "9.00"],
            "close": ["10.00", "7.00" if mutate else "9.30"],
            "volume": ["100", "5" if mutate else "100"], "amount": ["1000", "900"],
            "adjustflag": ["3", "3"], "tradestatus": ["1", "0" if mutate else "1"],
        })
        quotes = pin("daily_bars", source, bars, adjustment="unadjusted")
        cut = freeze_daily_cut(root, cuts, calendar, {"600519.SH": quotes},
                               listing_manifest=listings, opening_manifest=opening)
        return DailyPIT(root, cuts, cut, mode=mode)

    return new_gate


def preopen(gate, day, symbol="600519.SH", side="sell"):
    return gate.read_preopen_controls(symbol, day, as_of=f"{day}T09:14:59.999999999+08:00", side=side)


def test_risk_warning_rule_changes_on_effective_session_and_never_deletes_exit(opening_gate):
    gate = opening_gate()
    old = preopen(gate, "2026-07-03")
    assert old["as_of"] == "2026-07-03T09:14:59.999999999+08:00"
    assert old["rule_id"] == "older"
    assert old["daily_price_limits"] == {"state": "rate_known", "ratio": "0.05"}
    new = preopen(gate, "2026-07-06")
    assert new["rule_id"] == "newer"
    assert new["daily_price_limits"] == {
        "state": "reconstructed", "ratio": "0.10", "lower": "8.54", "upper": "10.44",
    }
    assert new["submission"] == {"state": "unavailable", "reasons": ["preopen_tradability_unverified"]}
    assert preopen(gate, "2026-07-06", side="buy")["submission"] == {
        "state": "blocked", "reasons": ["risk_warning_new_entry_excluded"],
    }


def test_ex_reference_uses_prior_session_and_ignores_same_day_completion(opening_gate):
    original = preopen(opening_gate(), "2026-07-06")
    revised = preopen(opening_gate(mutate=True), "2026-07-06")
    assert original["reference"]["prior_session"] == "2026-07-03"
    assert original["reference"]["price_cny"] == "9.49"
    for field in ("daily_price_limits", "submission"):
        assert original[field] == revised[field]
    assert revised["reference"]["price_cny"] == "9.49"
    assert revised["data_kind"] == "synthetic"
    unsupported = preopen(opening_gate(source="fixture"), "2026-07-06")
    assert unsupported["reference"] == {
        "state": "unavailable", "reason": "unsupported prior-session trading-status source",
    }


def test_date_only_halt_is_never_used_before_visible_and_blocks_stored_open(opening_gate):
    gate = opening_gate()
    first = preopen(gate, "2026-07-06", "600190.SH")
    assert first["known_stop"] is None
    assert first["daily_price_limits"] == {"state": "unbounded_daily_limit"}
    assert first["submission"]["reasons"] == ["unbounded_opening_regime_unsupported"]
    halt = preopen(gate, "2026-07-07", "600190.SH")
    assert halt["known_stop"] == "suspended"
    assert halt["submission"]["reasons"] == ["suspended"]
    with pytest.raises(EvidenceUnavailable, match="published suspended"):
        gate.read_opening_reference("600190.SH", "2026-07-07", as_of="2026-07-07T09:30:00+08:00")
    with pytest.raises(EvidenceUnavailable, match="historical-version proof"):
        preopen(opening_gate(mode="verified_visible"), "2026-07-06")


@pytest.mark.parametrize(("price", "ratio", "expected"), [
    ("0.01", "0.05", {"lower": "0.01", "upper": "0.02"}),
    ("0.10", "0.05", {"lower": "0.09", "upper": "0.11"}),
    ("10.05", "0.10", {"lower": "9.05", "upper": "11.06"}),
])
def test_bands_follow_cent_rounding_and_exchange_minimum_tick(price, ratio, expected):
    assert price_bands(price, ratio) == expected
