"""Observe a Sina daily-history response without granting L1 visibility."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .close_probe import _utc_now, _write_json, file_sha256
from .sources import bs_symbol


_DECODED_DATE = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})T00:00:00\.000Z")


def _decoded_day(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Sina decoder returned a non-string date")
    match = _DECODED_DATE.fullmatch(value)
    if match is None:
        raise ValueError("Sina decoder date format changed")
    day = match.group("day")
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("Sina decoder returned an invalid date")
    return day


def _source_components() -> tuple[str, str, str]:
    from importlib import metadata

    from akshare.stock.cons import hk_js_decode, zh_sina_a_stock_hist_url

    return zh_sina_a_stock_hist_url, hk_js_decode, metadata.version("akshare")


def _get(url: str, timeout: float):
    import requests

    return requests.get(url, timeout=timeout)


def _decode_dates(body: bytes, source_symbol: str, decoder_js: str) -> list[str]:
    from py_mini_racer import MiniRacer

    decoded = body.decode("utf-8")
    match = re.match(rf'\s*var KLC_K2_{re.escape(source_symbol)}="([^"]+)"', decoded)
    if match is None:
        raise ValueError("Sina daily payload wrapper or symbol changed")
    with MiniRacer() as js:
        js.eval(decoder_js, timeout_sec=5)
        rows = js.call("d", match.group(1), timeout_sec=10)
    if not isinstance(rows, list):
        raise ValueError("Sina decoder did not return a list")
    dates = []
    for row in rows:
        if not isinstance(row, dict) or "date" not in row:
            raise ValueError("Sina decoder returned a row without a date")
        dates.append(_decoded_day(row["date"]))
    if dates != sorted(set(dates)):
        raise ValueError("Sina daily dates are duplicated or unordered")
    return dates


def _write_bytes(path: Path, data: bytes) -> None:
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_bytes(data)
    staging.replace(path)


def probe(
    symbol: str, session: str, output_root: Path, *, timeout: float = 15,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Path:
    """Capture one fixed-source HTTP response and its date inventory only."""
    source_symbol = bs_symbol(symbol).replace(".", "")
    target = date.fromisoformat(session)
    if target.isoformat() != session:
        raise ValueError("session requires an ISO calendar date")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    requested_at = _utc_now(now)
    folder = output_root.resolve() / f"sina-close-observation-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": 1, "source": "sina", "dataset": "daily_bars_raw",
        "symbol": symbol, "session": session, "mode": "source_probe_only",
        "requested_at": requested_at.isoformat(), "timeout_seconds": timeout,
        "source_fallback": False, "same_day_l1_authorized": False,
        "historical_visibility_proven": False, "source_response_finalized": False,
        "target_date_row_observed": False, "symbol_identity_basis": "request_url_only",
        "raw_response_captured": False,
    }
    try:
        template, decoder_js, sdk_version = _source_components()
        url = template.format(source_symbol)
        response = _get(url, timeout)
        receipt["http_received_at"] = _utc_now(now).isoformat()
        receipt["http_status"] = response.status_code
        receipt["url"] = url
        receipt["sdk_version"] = sdk_version
        receipt["decoder_sha256"] = hashlib.sha256(decoder_js.encode()).hexdigest()
        receipt["http_headers"] = {
            name: response.headers[name]
            for name in (
                "Date", "Last-Modified", "ETag", "Content-Type",
                "Age", "Cache-Control", "Expires", "Via", "X-Cache",
            ) if name in response.headers
        }
        _write_bytes(folder / "raw.js", response.content)
        receipt["raw_response_captured"] = True
        receipt["raw_sha256"] = file_sha256(folder / "raw.js")
        receipt["raw_bytes"] = len(response.content)
        if response.status_code != 200:
            raise ValueError(f"Sina HTTP status {response.status_code}")
        dates = _decode_dates(response.content, source_symbol, decoder_js)
        receipt["decoded_row_count"] = len(dates)
        receipt["first_date"] = dates[0] if dates else None
        receipt["last_date"] = dates[-1] if dates else None
        receipt["target_date_row_observed"] = session in dates
        receipt["status"] = "target_date_seen" if session in dates else "target_date_absent"
    except Exception as exc:
        receipt.update(status="unavailable", error_code=type(exc).__name__, error=str(exc)[:500])
    finished_at = _utc_now(now)
    receipt["probe_finished_at"] = finished_at.isoformat()
    receipt["observed_shanghai_time"] = finished_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    path = folder / "observation.json"
    _write_json(path, receipt)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe one Sina daily response without publishing an L1 cut")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/runs"))
    args = parser.parse_args()
    path = probe(args.symbol, args.session, args.output_root, timeout=args.timeout)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"receipt": str(path), "status": receipt["status"]}, ensure_ascii=False))
    return 0 if receipt["status"] == "target_date_seen" else 2


if __name__ == "__main__":
    raise SystemExit(main())
