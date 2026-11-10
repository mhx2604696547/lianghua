import json
import subprocess
from unittest.mock import patch

import pandas as pd
import pytest

from ashare_research.sources import (
    BAR_FIELDS, SourceError, bs_symbol, fetch_baostock, fetch_exchange_roster,
)


def response(**changes):
    record = dict(zip(BAR_FIELDS.split(","), [
        "2026-09-14", "sh.600519", "10", "11", "9", "10", "", "100", "1000",
        "3", "0.1", "1", "0", "0",
    ]))
    record.update(changes)
    return subprocess.CompletedProcess(
        [], 0, json.dumps({"columns": list(record), "rows": [list(record.values())]}), ""
    )


def test_bars_retain_missing_reference_and_share_units():
    with patch("ashare_research.sources.subprocess.run", return_value=response()) as call:
        result = fetch_baostock("daily_bars", "2026-09-14", "2026-09-15", "600519.SH")
    assert result.frame.loc[0, "preclose"] == ""
    assert result.frame.loc[0, "volume"] == "100"
    assert result.metadata["volume_unit"] == "share"
    assert json.loads(call.call_args.kwargs["input"])["code"] == "sh.600519"


@pytest.mark.parametrize("change", [
    {"code": "sz.000001"}, {"adjustflag": "2"}, {"date": "2026-09-16"},
    {"tradestatus": "true"}, {"open": "not-a-number"}, {"volume": "-1"},
    {"open": "0"}, {"open": "12"}, {"low": "11"}, {"close": ""},
])
def test_bad_source_data_is_rejected(change):
    with patch("ashare_research.sources.subprocess.run", return_value=response(**change)):
        with pytest.raises((SourceError, ValueError)):
            fetch_baostock("daily_bars", "2026-09-14", "2026-09-15", "600519.SH")


def test_timeout_does_not_fall_back():
    with patch("ashare_research.sources.subprocess.run",
               side_effect=subprocess.TimeoutExpired("worker", 1)) as call:
        with pytest.raises(SourceError, match="no fallback"):
            fetch_baostock("daily_bars", "2026-09-14", "2026-09-15", "600519.SH")
    assert call.call_count == 1


def test_exchange_is_required():
    with pytest.raises(ValueError):
        bs_symbol("600519")


def test_nonfinite_timeout_is_rejected_without_launching_worker():
    with patch("ashare_research.sources.subprocess.run") as call:
        with pytest.raises(ValueError, match="timeout"):
            fetch_baostock("daily_bars", "2026-09-14", "2026-09-15", "600519.SH",
                           timeout=float("nan"))
        call.assert_not_called()


def test_delisted_roster_preserves_ambiguous_records_without_qualifying_membership():
    payload = json.dumps({
        "columns": ["公司代码", "公司简称", "上市日期", "暂停上市日期"],
        "rows": [
            ["600190", "样本 A", "2000-01-01", "2025-08-27"],
            ["600190", "样本 B", "2000-01-01", "2025-08-27"],
        ],
    })
    with (
        patch("ashare_research.sources.metadata.version", return_value="1.18.64"),
        patch(
            "ashare_research.sources.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, payload, ""),
        ),
    ):
        result = fetch_exchange_roster("sse_delisted_raw")
    assert result.frame["公司代码"].tolist() == ["600190", "600190"]
    assert "暂停上市日期" in result.frame.columns
    assert result.metadata["duplicate_codes"] == 1
    assert result.metadata["membership_ready"] is False

