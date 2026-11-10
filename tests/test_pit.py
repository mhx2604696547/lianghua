import hashlib
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from ashare_research.pit import DailyPIT, EvidenceUnavailable, freeze_daily_cut
from ashare_research.snapshots import freeze
from ashare_research.sources import AcquiredData


SYMBOL = "600519.SH"
DAY = "2026-09-14"
NEXT = "2026-09-15"
CUTOFF = f"{NEXT}T18:00:00+08:00"
OBSERVED_AT = datetime(2026, 11, 7, tzinfo=timezone.utc).isoformat()


def calendar(root, days=(DAY, NEXT), flags=("1", "1")):
    return freeze(
        AcquiredData(
            "trading_calendar", "test:synthetic",
            pd.DataFrame({"calendar_date": days, "is_trading_day": flags}),
            OBSERVED_AT, {"data_kind": "synthetic"},
        ),
        root,
    )


def bars(root, first_close=10.5, second_high=12):
    return freeze(
        AcquiredData(
            "daily_bars", "test:synthetic",
            pd.DataFrame({
                "date": [DAY, NEXT], "code": ["sh.600519"] * 2,
                "open": [10, 11], "high": [12, second_high], "low": [9, 10],
                "close": [first_close, 11.5], "volume": [100, 200], "amount": [1050, 2300],
                "adjustflag": ["3"] * 2,
            }),
            OBSERVED_AT,
            {"data_kind": "synthetic", "adjustment": "unadjusted",
             "volume_unit": "share", "amount_unit": "CNY"},
        ),
        root,
    )


def test_an_existing_cut_keeps_its_values_after_a_correction_is_added(tmp_path):
    snapshots = tmp_path / "snapshots"
    cuts = tmp_path / "cuts"
    days = calendar(snapshots)
    first = freeze_daily_cut(snapshots, cuts, days, {SYMBOL: bars(snapshots)})
    correction = freeze_daily_cut(snapshots, cuts, days, {SYMBOL: bars(snapshots, 11.25)})

    def close_and_receipt(cut_id):
        gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
        frame, receipt = gate.read_decision(SYMBOL, DAY, DAY, as_of=CUTOFF)
        return frame["close"].item(), receipt.lake_cut_id

    assert close_and_receipt(first) == (10.5, first)
    assert close_and_receipt(correction) == (11.25, correction)
    assert first != correction


def test_opening_reference_is_released_only_at_open_and_contains_no_later_fields(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    days = calendar(snapshots)
    for later_high in (12, 99):
        cut_id = freeze_daily_cut(snapshots, cuts, days, {SYMBOL: bars(snapshots, second_high=later_high)})
        gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
        with pytest.raises(EvidenceUnavailable, match="before the opening"):
            gate.read_opening_reference(SYMBOL, NEXT, as_of=f"{NEXT}T01:29:59Z")
        opening, receipt = gate.read_opening_reference(SYMBOL, NEXT, as_of=f"{NEXT}T01:30:00Z")
        assert opening.to_dict("records") == [{"date": NEXT, "code": "sh.600519", "open": 11}]
        assert receipt.purpose == "execution"
        assert receipt.as_of == f"{NEXT}T09:30:00+08:00"


def test_outcome_maturity_does_not_move_the_decision_cutoff(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    cut_id = freeze_daily_cut(snapshots, cuts, calendar(snapshots), {SYMBOL: bars(snapshots)})
    gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
    with pytest.raises(EvidenceUnavailable, match="not mature"):
        gate.read_outcome(SYMBOL, DAY, NEXT, as_of=CUTOFF)
    outcome, receipt = gate.read_outcome(SYMBOL, DAY, NEXT, as_of=f"{NEXT}T16:00:00Z")
    assert outcome["close"].tolist() == [10.5, 11.5]
    assert receipt.purpose == "outcome"
    with pytest.raises(EvidenceUnavailable, match="cutoff day"):
        gate.read_decision(SYMBOL, DAY, NEXT, as_of=CUTOFF)


def test_default_strict_read_does_not_promote_a_backfill(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    cut_id = freeze_daily_cut(snapshots, cuts, calendar(snapshots), {SYMBOL: bars(snapshots)})
    gate = DailyPIT(snapshots, cuts, cut_id)
    with pytest.raises(EvidenceUnavailable, match="version"):
        gate.read_decision(SYMBOL, DAY, DAY, as_of=CUTOFF)


def test_cutoff_requires_timezone(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    cut_id = freeze_daily_cut(snapshots, cuts, calendar(snapshots), {SYMBOL: bars(snapshots)})
    gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
    with pytest.raises(ValueError, match="timezone"):
        gate.read_decision(SYMBOL, DAY, DAY, as_of=NEXT)


def test_editing_a_snapshot_and_its_own_hash_does_not_change_a_pinned_cut(tmp_path):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    daily = bars(snapshots)
    cut_id = freeze_daily_cut(snapshots, cuts, calendar(snapshots), {SYMBOL: daily})
    gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")

    raw = daily.parent / "raw.csv"
    raw.write_bytes(raw.read_bytes() + b"changed\n")
    with pytest.raises(EvidenceUnavailable, match="raw snapshot hash"):
        gate.read_decision(SYMBOL, DAY, DAY, as_of=CUTOFF)
    changed = json.loads(daily.read_bytes())
    changed["files"]["raw.csv"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    daily.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(EvidenceUnavailable, match="manifest hash"):
        gate.read_decision(SYMBOL, DAY, DAY, as_of=CUTOFF)


@pytest.mark.parametrize(
    ("calendar_days", "calendar_flags", "message"),
    [
        ((DAY, NEXT), ("1", "1"), "incomplete trading calendar"),
        ((DAY, NEXT, "2026-09-16"), ("1", "1", "1"), "daily coverage"),
    ],
)
def test_missing_calendar_or_bar_rows_never_shorten_a_window(
    tmp_path, calendar_days, calendar_flags, message,
):
    snapshots, cuts = tmp_path / "snapshots", tmp_path / "cuts"
    cal = calendar(snapshots, calendar_days, calendar_flags)
    cut_id = freeze_daily_cut(snapshots, cuts, cal, {SYMBOL: bars(snapshots)})
    gate = DailyPIT(snapshots, cuts, cut_id, mode="reconstructed")
    with pytest.raises(EvidenceUnavailable, match=message):
        gate.read_decision(SYMBOL, DAY, "2026-09-16", as_of="2026-09-17T18:00:00+08:00")


def test_cut_cannot_admit_a_snapshot_outside_the_configured_root(tmp_path):
    snapshots = tmp_path / "snapshots"
    outside = bars(tmp_path / "elsewhere")
    with pytest.raises(EvidenceUnavailable, match="inside the configured root"):
        freeze_daily_cut(snapshots, tmp_path / "cuts", calendar(snapshots), {SYMBOL: outside})
