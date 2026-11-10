"""A live close observation never grants historical decision access."""

from datetime import datetime, timezone

import pandas as pd
import pytest

from ashare_research import close_probe
from ashare_research.sources import AcquiredData, SourceError


def test_probe_saves_sdk_values_and_keeps_research_unavailable(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"date": "2026-11-10", "code": "sh.600519", "close": "100.01"}])
    monkeypatch.setattr(close_probe, "fetch_baostock", lambda *args, **kwargs: AcquiredData(
        "daily_bars", "baostock", frame, "2026-11-10T07:12:01+00:00", {},
    ))
    result = close_probe.probe("600519.SH", "2026-11-10", tmp_path,
                               now=lambda: datetime(2026, 11, 10, 7, 12, tzinfo=timezone.utc))
    receipt = close_probe.read_receipt(result)
    assert receipt["status"] == "captured" and receipt["row_count"] == 1
    assert receipt["same_day_l1_authorized"] is False
    assert receipt["historical_visibility_proven"] is False
    assert receipt["target_row_observed"] is True
    assert receipt["raw_sha256"] == close_probe.file_sha256(result.parent / "raw.json")
    assert (result.parent / "raw.json").read_text(encoding="utf-8").find("100.01") >= 0


def test_probe_failure_is_recorded_without_raw_or_fallback(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise SourceError("source request timed out; no fallback was attempted")

    monkeypatch.setattr(close_probe, "fetch_baostock", fail)
    path = close_probe.probe("600519.SH", "2026-11-10", tmp_path,
                             now=lambda: datetime(2026, 11, 10, 7, 12, tzinfo=timezone.utc))
    receipt = close_probe.read_receipt(path)
    assert receipt["status"] == "unavailable"
    assert receipt["error_code"] == "SourceError"
    assert receipt["same_day_l1_authorized"] is False
    assert receipt["target_row_observed"] is False
    assert not (path.parent / "raw.json").exists()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_invalid_timeout_is_rejected_before_any_artifact(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        close_probe.probe("600519.SH", "2026-11-10", tmp_path, timeout=timeout)
    assert list(tmp_path.iterdir()) == []


def test_sdk_version_failure_does_not_publish_raw(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"date": "2026-11-10", "code": "sh.600519", "close": "100.01"}])
    monkeypatch.setattr(close_probe, "fetch_baostock", lambda *args, **kwargs: AcquiredData(
        "daily_bars", "baostock", frame, "2026-11-10T07:12:01+00:00", {},
    ))
    def missing(_):
        raise close_probe.metadata.PackageNotFoundError("baostock")
    monkeypatch.setattr(close_probe.metadata, "version", missing)
    path = close_probe.probe("600519.SH", "2026-11-10", tmp_path)
    assert close_probe.read_receipt(path)["status"] == "unavailable"
    assert not (path.parent / "raw.json").exists()


def test_naive_clock_is_rejected_before_any_artifact(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        close_probe.probe("600519.SH", "2026-11-10", tmp_path,
                          now=lambda: datetime(2026, 11, 10, 7, 12))
    assert list(tmp_path.iterdir()) == []


def test_bad_internal_frame_does_not_publish_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(close_probe, "fetch_baostock", lambda *args, **kwargs: AcquiredData(
        "daily_bars", "baostock", pd.DataFrame([{"code": "sh.600519"}]),
        "2026-11-10T07:12:01+00:00", {},
    ))
    path = close_probe.probe("600519.SH", "2026-11-10", tmp_path)
    assert close_probe.read_receipt(path)["status"] == "unavailable"
    assert not (path.parent / "raw.json").exists()


@pytest.mark.parametrize("rows", [
    [],
    [{"date": "2026-11-09", "code": "sh.600519"}],
    [{"date": "2026-11-10", "code": "sh.600000"}],
])
def test_probe_requires_exact_target_row_before_capture(tmp_path, monkeypatch, rows):
    frame = pd.DataFrame(rows, columns=["date", "code"])
    monkeypatch.setattr(close_probe, "fetch_baostock", lambda *args, **kwargs: AcquiredData(
        "daily_bars", "baostock", frame, "2026-11-10T07:12:01+00:00", {},
    ))
    path = close_probe.probe("600519.SH", "2026-11-10", tmp_path)
    receipt = close_probe.read_receipt(path)
    assert receipt["status"] == "unavailable"
    assert receipt["target_row_observed"] is False
    assert not (path.parent / "raw.json").exists()
