import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from ashare_research.pit import DailyPIT, EvidenceUnavailable, freeze_daily_cut
from ashare_research.snapshots import freeze, sha256
from ashare_research.sources import AcquiredData
from ashare_research.universe import CALENDAR_AUDIT_METHOD, LISTING_FIELDS, RISK_NAME_FIELDS, build_audited_calendar


@pytest.fixture
def listing_cut(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    rows = [
        ("600190.SH", "1999-06-09", "2025-07-28", "sse_main_a", "a" * 64),
        ("900952.SH", "1998-05-19", "2025-07-28", "sse_main_b", "b" * 64),
        ("000976.SZ", "2000-06-01", "2024-08-27", "szse_roster", "c" * 64),
        ("001100.SZ", "2020-01-02", None, "szse_roster", "c" * 64),
        ("003041.SZ", "2021-04-06", None, "szse_roster", "c" * 64),
        ("601975.SH", "2019-01-08", None, "sse_main_a", "a" * 64),
    ]
    notices = [
        {"symbol": "600190.SH", "kind": "delisted", "from": "2025-07-25", "until": None,
         "not_before_day": "2025-07-19", "source_sha256": "d" * 64},
        {"symbol": "000976.SZ", "kind": "suspended", "from": "2023-05-04", "until": "2023-05-05",
         "not_before_day": "2023-04-29", "source_sha256": "e" * 64},
        {"symbol": "000976.SZ", "kind": "risk_warning", "from": "2023-05-05", "until": "2023-05-06",
         "not_before_day": "2023-05-06", "source_sha256": "f" * 64},
    ]
    meta = {
        "latest_session_exclusive": "2026-11-09", "notice_events": notices,
        "szse_main_a": {
            "effective_on": "2021-04-06", "not_before_day": "2021-04-01",
            "first": "000001", "last": "004999", "source_sha256": "1" * 64,
            "ranges": [["000001", "000999"], ["001200", "001999"], ["002001", "004999"]],
        },
    }
    manifest = freeze(AcquiredData(
        "listing_evidence", "test:synthetic", pd.DataFrame(rows, columns=LISTING_FIELDS),
        datetime(2026, 11, 9, tzinfo=timezone.utc).isoformat(), meta,
    ), snapshots)
    cut_id = freeze_daily_cut(snapshots, cuts, None, {}, listing_manifest=manifest)
    return snapshots, cuts, cut_id


def read_day(gate, day, *symbols, as_of=None):
    view, receipt = gate.read_listings(day, as_of=as_of or f"{day}T18:00:00+08:00", symbols=symbols)
    return view.set_index("symbol"), receipt


def test_a_and_b_shares_keep_distinct_identity_and_announced_end_wins(listing_cut):
    gate = DailyPIT(*listing_cut, mode="reconstructed")
    before, _ = read_day(gate, "2021-05-14", "600190.SH", "900952.SH")
    assert before.loc["600190.SH", "listing_state"] == "candidate"
    assert before.loc["600190.SH", "source_hashes"] == ["a" * 64]
    assert before.loc["900952.SH", "entry_state"] == "excluded"
    assert before.loc["900952.SH", "board_scope"] == "outside_main_a"

    end, receipt = read_day(gate, "2025-07-25", "600190.SH")
    assert end.loc["600190.SH", "listing_state"] == "delisted"
    assert end.loc["600190.SH", "reasons"] == ["announced_delisting_effective"]
    assert receipt.membership_ready is False


def test_effective_and_publication_dates_gate_status_independently(listing_cut):
    gate = DailyPIT(*listing_cut, mode="reconstructed")
    before, _ = read_day(gate, "2023-05-03", "000976.SZ")
    assert before.loc["000976.SZ", "entry_state"] == "unknown"
    stopped, _ = read_day(gate, "2023-05-04", "000976.SZ")
    assert stopped.loc["000976.SZ", "reasons"] == ["suspended"]
    late, _ = read_day(gate, "2023-05-05", "000976.SZ")
    assert late.loc["000976.SZ", "entry_state"] == "unknown"
    observed, _ = read_day(gate, "2023-05-05", "000976.SZ", as_of="2023-05-06T00:00:00+08:00")
    assert observed.loc["000976.SZ", "reasons"] == ["risk_warning"]


def test_future_listings_and_unverified_terminal_dates_fail_closed(listing_cut):
    gate = DailyPIT(*listing_cut, mode="reconstructed")
    before, _ = read_day(gate, "2019-01-07")
    assert "601975.SH" not in before.index
    board, _ = read_day(gate, "2021-04-02", "000976.SZ")
    assert board.loc["000976.SZ", "board_scope"] == "unknown"
    reserved, _ = read_day(gate, "2021-05-14", "001100.SZ")
    assert reserved.loc["001100.SZ", "board_scope"] == "unknown"
    after, _ = read_day(gate, "2024-08-27", "000976.SZ")
    assert after.loc["000976.SZ", "listing_state"] == "unknown"
    assert after.loc["000976.SZ", "reasons"] == ["reported_terminal_date_unconfirmed"]
    with pytest.raises(EvidenceUnavailable, match="historical-version proof"):
        read_day(DailyPIT(*listing_cut), "2021-05-14", "000976.SZ")


@pytest.fixture
def age_inputs(listing_cut):
    snapshots, cuts, _ = listing_cut
    listing = next(snapshots.glob("listing_evidence-*/manifest.json"))
    cal = freeze(AcquiredData(
        "trading_calendar", "test:synthetic",
        pd.DataFrame({
            "calendar_date": [f"2021-04-{day:02}" for day in range(2, 10)],
            "is_trading_day": list("10001111"),
        }),
        "2021-04-09T09:00:00+08:00",
        {"calendar_audit_method": CALENDAR_AUDIT_METHOD, "first_date": "2021-04-02", "last_date": "2021-04-09"},
    ), snapshots)
    return snapshots, cuts, cal, listing


@pytest.fixture
def age_gate(age_inputs):
    snapshots, cuts, cal, listing = age_inputs
    cut_id = freeze_daily_cut(snapshots, cuts, cal, {}, listing_manifest=listing)
    return DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")


def test_age_exclusion_requires_complete_listing_window(age_gate):
    view, _ = age_gate.read_listings(
        "2021-04-08", as_of="2021-04-08T18:00:00+08:00",
        symbols=("003041.SZ", "000976.SZ"), min_listed_sessions=4,
    )
    rows = view.set_index("symbol")
    assert rows.loc["003041.SZ", "listing_age"] == {
        "minimum": 4, "sessions": 3, "basis": "exact", "state": "too_new",
    }
    assert rows.loc["003041.SZ", "entry_state"] == "excluded"
    assert rows.loc["000976.SZ", "listing_age"] == {
        "minimum": 4, "sessions": 4, "basis": "lower_bound", "state": "met",
    }
    assert rows.loc["000976.SZ", "entry_state"] == "unknown"
    more, _ = age_gate.read_listings(
        "2021-04-08", as_of="2021-04-08T18:00:00+08:00",
        symbols=("000976.SZ",), min_listed_sessions=5,
    )
    assert more.iloc[0]["listing_age"]["state"] == "unknown"
    assert more.iloc[0]["entry_state"] == "unknown"


def test_age_screen_rejects_holiday_and_incomplete_session(age_gate):
    with pytest.raises(EvidenceUnavailable, match="requires a trading date"):
        age_gate.read_listings(
            "2021-04-05", as_of="2021-04-05T18:00:00+08:00", min_listed_sessions=4,
        )
    with pytest.raises(EvidenceUnavailable, match="completed trading session"):
        age_gate.read_listings(
            "2021-04-08", as_of="2021-04-08T14:59:59+08:00", min_listed_sessions=4,
        )


def _status_gate(age_inputs, *, add_risk_point):
    snapshots, cuts, calendar, listing = age_inputs
    observation = "2026-11-09T09:00:00+08:00"
    bars = freeze(AcquiredData(
        "daily_bars", "baostock", pd.DataFrame({
            "date": ["2021-04-08"], "code": ["sz.003041"], "isST": ["0"], "tradestatus": ["1"],
        }), observation, {"data_kind": "synthetic"},
    ), snapshots)
    names = None
    if add_risk_point:
        names = freeze(AcquiredData(
            "risk_name_events", "szse_short_name_change_points",
            pd.DataFrame([("003041.SZ", "2021-04-08", "ST")], columns=RISK_NAME_FIELDS),
            observation, {"data_kind": "synthetic"},
        ), snapshots)
    cut_id = freeze_daily_cut(
        snapshots, cuts, calendar, {"003041.SZ": bars},
        listing_manifest=listing, risk_name_manifest=names,
    )
    return DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")


def _screen_status(gate, day, cutoff):
    rows, receipt = gate.read_listings(
        day, as_of=cutoff, symbols=("003041.SZ",), min_listed_sessions=3, check_session_status=True,
    )
    assert receipt.membership_ready is False
    return rows.iloc[0]


def test_same_day_status_is_unavailable_and_completed_observation_can_pass_screen(age_inputs):
    gate = _status_gate(age_inputs, add_risk_point=False)
    earlier = _screen_status(gate, "2021-04-08", "2021-04-08T18:00:00+08:00")
    assert earlier["session_status"]["risk_warning"] == "unknown"
    assert earlier["session_status"]["data_kind"] == "unspecified"
    assert earlier["entry_state"] == "unknown"
    later = _screen_status(gate, "2021-04-08", "2021-04-09T18:00:00+08:00")
    assert later["session_status"]["risk_warning"] == "clear"
    assert later["session_status"]["trading"] == "normal"
    assert later["session_status"]["data_kind"] == "synthetic"
    assert later["session_status"]["evidence"][0]["data_kind"] == "synthetic"
    assert later["entry_state"] == "eligible"


def test_conflicting_status_cannot_pass_and_name_points_do_not_extend(age_inputs):
    gate = _status_gate(age_inputs, add_risk_point=True)
    conflict = _screen_status(gate, "2021-04-08", "2021-04-09T18:00:00+08:00")
    assert conflict["session_status"]["risk_warning"] == "conflict"
    assert conflict["entry_state"] == "unknown"
    following = _screen_status(gate, "2021-04-09", "2021-04-10T18:00:00+08:00")
    assert following["session_status"]["risk_warning"] == "unknown"
    assert following["session_status"]["trading"] == "unknown"
    assert following["entry_state"] == "unknown"


def test_calendar_audit_refuses_a_missing_observed_session(tmp_path):
    snapshot = freeze(AcquiredData(
        "sina_trading_dates_raw", "akshare:sina",
        pd.DataFrame({"trade_date": ["2021-01-04", "2021-01-06"]}),
        "2021-01-07T09:00:00+08:00", {},
    ), tmp_path / "raw")
    report = tmp_path / "dates.json"
    report.write_text(json.dumps({
        "status": "raw_captured", "endpoint": "tool_trade_date_hist_sina",
        "manifest": str(snapshot), "manifest_sha256": sha256(snapshot), "rows": 2,
    }), encoding="utf-8")
    docs = []
    for venue, host in (("SH", "www.sse.com.cn"), ("SZ", "www.szse.cn")):
        doc = tmp_path / f"{venue}.html"
        doc.write_text("synthetic fixture", encoding="utf-8")
        docs.append({
            "year": 2021, "venue": venue, "file": doc.name, "url": f"https://{host}/fixture",
            "status": "captured", "http_status": 200, "sha256": sha256(doc),
        })
    (tmp_path / "captures.json").write_text(json.dumps({"documents": docs}), encoding="utf-8")
    closure = tmp_path / "closures.json"
    closure.write_text(json.dumps({"closures": {"2021": [["2021-01-01", "2021-01-03"]]}}), encoding="utf-8")
    output = tmp_path / "normalized"
    with pytest.raises(ValueError, match="sources disagree on: 2021-01-05"):
        build_audited_calendar(report, closure, output)
    assert not output.exists()
