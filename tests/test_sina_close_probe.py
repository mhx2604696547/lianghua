"""A reachable daily endpoint is still only an L0 observation."""

from datetime import datetime, timezone

import pytest
import py_mini_racer

from ashare_research import sina_close_probe


class Response:
    def __init__(self, status_code=200, content=b'var KLC_K2_sh600519="encoded";', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Date": "Tue, 10 Nov 2026 07:13:00 GMT"}


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(sina_close_probe, "_source_components", lambda: (
        "https://example.test/{}/daily.js", "decoder", "1.18.64",
    ))
    monkeypatch.setattr(sina_close_probe, "_get", lambda *_: Response())


def _now():
    return datetime(2026, 11, 10, 7, 13, tzinfo=timezone.utc)


def test_decoder_uses_seconds_and_validates_dates(monkeypatch):
    class JavaScript:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def eval(self, source, *, timeout_sec):
            assert source == "pinned decoder" and timeout_sec == 5

        def call(self, function, payload, *, timeout_sec):
            assert (function, payload, timeout_sec) == ("d", "encoded", 10)
            return [{"date": "2026-11-09T00:00:00.000Z"}]

    monkeypatch.setattr(py_mini_racer, "MiniRacer", JavaScript)
    assert sina_close_probe._decode_dates(
        b'var KLC_K2_sh600519="encoded";', "sh600519", "pinned decoder"
    ) == ["2026-11-09"]


@pytest.mark.parametrize("value", [
    "2026-11-10garbage", "2026-11-10Tinvalid", "2026-11-10",
    "2026-11-10T00:00:00.000Zextra", "2026-02-30T00:00:00.000Z",
])
def test_decoder_rejects_malformed_full_date(value):
    with pytest.raises(ValueError):
        sina_close_probe._decoded_day(value)


def test_target_date_requires_explicit_decoded_row(tmp_path, source, monkeypatch):
    monkeypatch.setattr(sina_close_probe, "_decode_dates", lambda *_: ["2026-11-09", "2026-11-10"])
    path = sina_close_probe.probe("600519.SH", "2026-11-10", tmp_path, now=_now)
    receipt = sina_close_probe.json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "target_date_seen"
    assert receipt["target_date_row_observed"] is True
    assert receipt["same_day_l1_authorized"] is False
    assert receipt["historical_visibility_proven"] is False
    assert receipt["source_response_finalized"] is False
    assert receipt["symbol_identity_basis"] == "request_url_only"
    assert receipt["raw_sha256"] == sina_close_probe.file_sha256(path.parent / "raw.js")


def test_prior_day_response_remains_absent_for_target(tmp_path, source, monkeypatch):
    monkeypatch.setattr(sina_close_probe, "_decode_dates", lambda *_: ["2026-11-09"])
    path = sina_close_probe.probe("600519.SH", "2026-11-10", tmp_path, now=_now)
    receipt = sina_close_probe.json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "target_date_absent"
    assert receipt["last_date"] == "2026-11-09"
    assert receipt["raw_response_captured"] is True
    assert receipt["target_date_row_observed"] is False


def test_non_200_response_is_archived_but_unavailable(tmp_path, source, monkeypatch):
    monkeypatch.setattr(sina_close_probe, "_get", lambda *_: Response(status_code=503))
    path = sina_close_probe.probe("600519.SH", "2026-11-10", tmp_path, now=_now)
    receipt = sina_close_probe.json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "unavailable"
    assert receipt["raw_response_captured"] is True
    assert receipt["http_status"] == 503
    assert receipt["target_date_row_observed"] is False


def test_fetch_failure_has_no_raw_response(tmp_path, source, monkeypatch):
    def fail(*_):
        raise TimeoutError("request timed out")

    monkeypatch.setattr(sina_close_probe, "_get", fail)
    path = sina_close_probe.probe("600519.SH", "2026-11-10", tmp_path, now=_now)
    receipt = sina_close_probe.json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "unavailable"
    assert receipt["raw_response_captured"] is False
    assert not (path.parent / "raw.js").exists()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_invalid_timeout_creates_no_artifact(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        sina_close_probe.probe("600519.SH", "2026-11-10", tmp_path, timeout=timeout)
    assert list(tmp_path.iterdir()) == []
