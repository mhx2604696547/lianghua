import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ashare_research.pit import STUDY_FIXTURE_SOURCE, DailyPIT, EvidenceUnavailable, freeze_daily_cut
from ashare_research.snapshots import freeze
from ashare_research.sources import AcquiredData
from ashare_research.studies import TrialBook, load_document


SPECS = Path(__file__).resolve().parents[1] / "specs"


@pytest.fixture
def study(tmp_path):
    spec = load_document(SPECS / "synthetic-volume-study-v1.json")
    book = TrialBook(tmp_path / "trials.sqlite3")
    return spec, book, book.freeze(spec)


def test_freeze_and_trial_identity(study):
    spec, book, spec_id = study
    assert book.freeze(dict(reversed(list(spec.items())))) == spec_id
    assert book.spec(spec_id) == spec
    invalid = {**spec, "partitions": {
        **spec["partitions"], "validation": ["2026-08-20", "2026-09-04"],
    }}
    with pytest.raises(ValueError, match="ordered and disjoint"):
        book.freeze(invalid)

    request = {"spec_id": spec_id, "hold_sessions": 3}
    started = book.begin("first", request)
    pending = book.begin("first", request, retry=True)
    assert pending["status"] == "running_or_interrupted"
    assert pending["attempt_id"] == started["attempt_id"]
    with pytest.raises(ValueError, match="different inputs"):
        book.begin("first", {**request, "hold_sessions": 2})
    book.finish(started["attempt_id"], "blocked", {"reason": "example_missing_evidence"})
    assert book.begin("first", request)["reason"] == "example_missing_evidence"
    retry = book.begin("first", request, retry=True)
    assert retry["status"] == "started" and retry["attempt_id"] != started["attempt_id"]


def test_same_day_fixture_clock_cannot_unlock_market_snapshot(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    day = "2026-08-03"
    captured = f"{day}T18:00:00+08:00"
    calendar = freeze(AcquiredData("trading_calendar", "sample", pd.DataFrame({
        "calendar_date": [day], "is_trading_day": ["1"],
    }), captured, {}), snapshots)
    # The source marker alone cannot make a market-labelled snapshot a fixture.
    bars = freeze(AcquiredData("daily_bars", STUDY_FIXTURE_SOURCE, pd.DataFrame([{
        "date": day, "code": "600519.SH", "open": "10.00", "high": "10.00",
        "low": "10.00", "close": "10.00", "volume": "100", "amount": "1000",
        "adjustflag": "3",
    }]), captured, {
        "data_kind": "market", "adjustment": "unadjusted", "volume_unit": "share", "amount_unit": "CNY",
    }), snapshots)
    cut_id = freeze_daily_cut(snapshots, cuts, calendar, {"600519.SH": bars})
    with pytest.raises(EvidenceUnavailable, match="proof for the calendar"):
        DailyPIT(snapshots, cuts, cut_id).read_trading_sessions(day, day)
    pit = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
    with pytest.raises(EvidenceUnavailable, match="synthetic study fixture"):
        pit.read_study_fixture_decision("600519.SH", day, day, as_of=captured)


def test_synthetic_trial_matches_fills_and_preserves_artifact_identity(tmp_path, study):
    from ashare_research.study_run import make_study_fixture, run_study

    spec, book, spec_id = study
    snapshots, cuts, output = (tmp_path / name for name in ("snapshots", "cuts", "runs"))
    cut_id = make_study_fixture(spec, snapshots, cuts)
    pit = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
    day, symbol = "2026-08-04", "000001.SZ"
    with pytest.raises(EvidenceUnavailable, match="completion event"):
        pit.read_study_fixture_decision(symbol, day, day, as_of=f"{day}T17:29:59+08:00")
    with pytest.raises(EvidenceUnavailable, match="cutoff day"):
        pit.read_decision(symbol, day, day, as_of=f"{day}T18:00:00+08:00")
    frame, receipt = pit.read_study_fixture_decision(symbol, day, day, as_of=f"{day}T17:30:00+08:00")
    assert frame["close"].item() == "11.13"
    assert receipt.data_kind == "synthetic"

    inputs = {
        "spec_id": spec_id, "cut_id": cut_id, "request_id": "sample", "partition": "development",
        "hold_sessions": 3, "snapshots": snapshots, "cuts": cuts, "output": output,
    }
    response = run_study(book, **inputs)
    assert response["status"] == "complete"
    archive = json.loads(Path(response["run"]).read_bytes())
    result = archive["result"]
    assert result["data_kind"] == "synthetic" and result["acceptance"] == "engineering_only"
    signal = next(scan for scan in result["signal_scans"] if scan["session"] == day)
    assert signal["selection"] == symbol
    assert signal["decision_receipts"][0]["as_of"] == f"{day}T18:00:00+08:00"
    assert all(scan["session"] <= "2026-08-17" for scan in result["signal_scans"])
    orders = result["strategy"]["orders"]
    buy = next(order for order in orders if order["side"] == "buy" and order["symbol"] == symbol)
    sell = next(order for order in orders if order["side"] == "sell"
                and order["parent_intent_id"] == buy["parent_intent_id"])
    assert buy["session"] == "2026-08-05" and Decimal(buy["price_cny"]) == Decimal("11.13")
    assert sell["session"] == "2026-08-10" and Decimal(sell["price_cny"]) == Decimal("10.54")
    assert all(datetime.fromisoformat(buy[key]) < datetime.fromisoformat(buy[next_key]) for key, next_key in (
        ("signal_as_of", "submitted_at"), ("submitted_at", "fill_at"),
    ))
    cash = Decimal(spec["initial_cash_cny"])
    for order in orders:
        if order["filled"]:
            cash += Decimal(order["cash_delta_cny"])
    assert Decimal(result["evaluation"]["strategy_terminal_cny"]) == cash
    assert result["evaluation"]["paired_filled_entry_sessions"] >= 4
    assert run_study(book, **inputs) == {**response, "reused": True}
    rerun = run_study(book, **{**inputs, "request_id": "repeat"})
    assert json.loads(Path(rerun["run"]).read_bytes())["result"] == result
    Path(response["report"]).write_text("altered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        run_study(book, **inputs)


def test_holdout_is_blocked_before_opening_a_cut(tmp_path, study, monkeypatch):
    from ashare_research import study_run

    _, book, spec_id = study

    def unexpected_read(*args, **kwargs):
        pytest.fail("the sealed holdout must not open any cut")

    monkeypatch.setattr(study_run, "DailyPIT", unexpected_read)
    response = study_run.run_study(
        book, spec_id=spec_id, cut_id="f" * 64, request_id="sealed", partition="holdout",
        hold_sessions=3, snapshots=tmp_path / "absent-snapshots", cuts=tmp_path / "absent-cuts",
        output=tmp_path / "runs",
    )
    assert response["status"] == "blocked"
    result = json.loads(Path(response["run"]).read_bytes())["result"]
    assert result["reason"] == "holdout_sealed" and result["evaluation"]["state"] == "unavailable"
    assert "signal_scans" not in result
