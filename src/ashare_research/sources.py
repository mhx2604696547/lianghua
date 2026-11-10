"""Fixed-source acquisition. These functions belong to L0, never strategy tools."""

from __future__ import annotations

from importlib import metadata
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

BAR_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST"
)
BAOSTOCK_DATASETS = frozenset({"daily_bars", "instruments", "trading_calendar"})
FREE_DATA_V0_SOURCE = "baostock:free_data_v0"


class SourceError(RuntimeError):
    """A source failed; callers must not switch sources or treat this as no data."""


def bs_symbol(symbol: str) -> str:
    """Accept explicit SH/SZ symbols, retaining exchange identity."""
    match = re.fullmatch(r"([0-9]{6})\.(SH|SZ)", symbol)
    if not match:
        raise ValueError("symbol must have the form 600519.SH or 000001.SZ")
    return f"{match[2].lower()}.{match[1]}"


@dataclass(frozen=True)
class AcquiredData:
    dataset: str
    source: str
    frame: pd.DataFrame
    fetched_at: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RosterEndpoint:
    exchange: str
    operation: str
    selector: str
    code_column: str
    other_required_columns: tuple[str, ...]


EXCHANGE_ROSTERS = {
    "sse_listed_raw": RosterEndpoint(
        "sse", "stock_info_sh_name_code", "主板A股", "证券代码", ("上市日期",),
    ),
    "sse_delisted_raw": RosterEndpoint(
        "sse", "stock_info_sh_delist", "沪市", "公司代码", ("上市日期", "暂停上市日期"),
    ),
    "szse_listed_raw": RosterEndpoint(
        "szse", "stock_info_sz_name_code", "A股列表", "A股代码", ("A股上市日期", "板块"),
    ),
    "szse_delisted_raw": RosterEndpoint(
        "szse", "stock_info_sz_delist", "终止上市公司", "证券代码", ("上市日期", "终止上市日期"),
    ),
}


def _date_range(start: str, end: str) -> None:
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("start must not be later than end")


def _source_request(request: dict[str, str], timeout: float) -> pd.DataFrame:
    """Use a process deadline: the SDK has a process-global socket/session."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "ashare_research.source_worker"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceError("source request timed out; no fallback was attempted") from exc
    if completed.returncode:
        raise SourceError(f"source worker failed: {completed.stderr.strip()[:500]}")
    try:
        payload = json.loads(completed.stdout)
        frame = pd.DataFrame(payload["rows"], columns=payload["columns"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError("source worker returned an invalid response") from exc
    if frame.empty:
        raise SourceError("empty source response: coverage has not been established")
    return frame


def validate_baostock_bars(frame: pd.DataFrame, code: str, start: str, end: str) -> None:
    expected = set(BAR_FIELDS.split(","))
    if not expected.issubset(frame.columns):
        raise SourceError(f"missing bar fields: {sorted(expected - set(frame.columns))}")
    if set(frame["code"]) != {code}:
        raise SourceError("response contains a different instrument")
    if set(frame["adjustflag"]) != {"3"}:
        raise SourceError("execution data must be unadjusted")
    dates = pd.to_datetime(frame["date"], errors="raise")
    if dates.duplicated().any() or not dates.between(start, end).all():
        raise SourceError("duplicate or out-of-range bar dates")
    for field in ("tradestatus", "isST"):
        if not set(frame[field]).issubset({"", "0", "1"}):
            raise SourceError(f"unexpected {field} value")
    for field in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        values = pd.to_numeric(frame[field].replace("", None), errors="raise")
        if (values < 0).any() or not all(math.isfinite(v) for v in values.dropna()):
            raise SourceError(f"invalid numeric values in {field}")
    prices = frame[["open", "high", "low", "close"]].replace("", None)
    prices = prices.apply(pd.to_numeric, errors="raise")
    active = prices.loc[frame["tradestatus"] == "1"]
    if active.isna().any().any() or (active <= 0).any().any():
        raise SourceError("normal trading rows require finite positive OHLC")
    if ((active["high"] < active[["open", "close", "low"]].max(axis=1))
            | (active["low"] > active[["open", "close", "high"]].min(axis=1))).any():
        raise SourceError("inconsistent OHLC bounds")


def fetch_baostock(
    dataset: str, start: str, end: str, symbol: str | None = None, timeout: float = 45
) -> AcquiredData:
    """Fetch raw daily bars, instrument master or calendar without source fallback."""
    _date_range(start, end)
    if dataset not in BAOSTOCK_DATASETS:
        raise ValueError("unsupported BaoStock dataset")
    if dataset == "daily_bars" and symbol is None:
        raise ValueError("daily_bars requires a symbol")
    request = {"dataset": dataset, "start": start, "end": end}
    if symbol is not None:
        request["code"] = bs_symbol(symbol)
    frame = _source_request(request, timeout)
    if dataset == "daily_bars":
        validate_baostock_bars(frame, request["code"], start, end)
    elif dataset == "instruments":
        if not {"code", "ipoDate", "outDate", "type", "status"}.issubset(frame.columns):
            raise SourceError("instrument response schema changed")
        if symbol is not None and set(frame["code"]) != {request["code"]}:
            raise SourceError("response contains a different instrument")
    else:
        if not {"calendar_date", "is_trading_day"}.issubset(frame.columns):
            raise SourceError("calendar response schema changed")
        dates = pd.to_datetime(frame["calendar_date"], errors="raise")
        if dates.duplicated().any() or not dates.between(start, end).all():
            raise SourceError("duplicate or out-of-range calendar dates")
        if not set(frame["is_trading_day"]).issubset({"0", "1"}):
            raise SourceError("unknown trading-day marker")
    return AcquiredData(
        dataset, "baostock", frame, datetime.now(timezone.utc).isoformat(),
        {"adjustment": "unadjusted" if dataset == "daily_bars" else None,
         "volume_unit": "share" if dataset == "daily_bars" else None,
         "amount_unit": "CNY" if dataset == "daily_bars" else None,
         "coverage_status": "not_audited", "source_fallback": False},
    )


def fetch_sina_actions(symbol: str, timeout: float = 45) -> AcquiredData:
    """Delegate dividend extraction to AKShare; retain original source fields/units."""
    code = bs_symbol(symbol).split(".")[1]
    try:
        sdk_version = metadata.version("akshare")
    except metadata.PackageNotFoundError as exc:
        raise SourceError("install the sources extra to use the AKShare adapter") from exc
    frame = _source_request({"dataset": "corporate_actions_raw", "code": code}, timeout)
    required = {"公告日期", "除权除息日", "股权登记日", "送股", "转增", "派息"}
    if not required.issubset(frame.columns):
        raise SourceError("Sina dividend response schema changed")
    if frame.empty:
        raise SourceError("empty dividend response is not evidence of complete history")
    return AcquiredData(
        "corporate_actions_raw", "akshare:sina", frame,
        datetime.now(timezone.utc).isoformat(),
        {"symbol": symbol, "sdk_version": sdk_version,
         "unit_policy": "original source units; not converted to per-share cash",
         "ledger_ready": False, "coverage_status": "not_audited",
         "source_fallback": False},
    )


def fetch_exchange_roster(dataset: str, timeout: float = 30) -> AcquiredData:
    """Capture the exchange roster as SDK values; preserve ambiguous identities and dates."""
    endpoint = EXCHANGE_ROSTERS.get(dataset)
    if endpoint is None:
        raise ValueError("unsupported exchange roster")
    try:
        sdk_version = metadata.version("akshare")
    except metadata.PackageNotFoundError as exc:
        raise SourceError("install the sources extra to use the exchange roster adapter") from exc
    frame = _source_request({"dataset": dataset}, timeout)
    required = {endpoint.code_column, *endpoint.other_required_columns}
    if not frame.columns.is_unique:
        raise SourceError("roster response has ambiguous columns")
    if not required.issubset(frame.columns):
        raise SourceError(f"roster schema changed: {sorted(required - set(frame.columns))}")
    codes = frame[endpoint.code_column].astype("string")
    invalid = ~codes.str.fullmatch(r"[0-9]{6}").fillna(False)
    return AcquiredData(
        dataset, f"akshare:{endpoint.exchange}", frame, datetime.now(timezone.utc).isoformat(),
        {
            "sdk_version": sdk_version,
            "endpoint": endpoint.operation,
            "parameters": {"symbol": endpoint.selector},
            "capture_level": "sdk_values",
            "date_semantics": "original_sdk_labels_unverified",
            "data_kind": "market",
            "duplicate_codes": int(codes.duplicated().sum()),
            "invalid_or_missing_codes": int(invalid.sum()),
            "coverage_status": "not_audited",
            "membership_ready": False,
            "source_fallback": False,
        },
    )

