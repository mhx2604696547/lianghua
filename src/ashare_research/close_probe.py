"""Observe one BaoStock daily response at the real clock; never publish an L1 cut."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from .sources import SourceError, bs_symbol, fetch_baostock


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_json(path: Path, value: dict) -> None:
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    staging.replace(path)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    stamp = clock()
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("observation clock must include a timezone")
    return stamp.astimezone(timezone.utc)


def probe(
    symbol: str, session: str, output_root: Path, *, timeout: float = 20,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Path:
    """Save an observed SDK response or its failure; do not infer historical visibility."""
    source_symbol = bs_symbol(symbol)
    date.fromisoformat(session)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    requested_at = _utc_now(now)
    folder = output_root.resolve() / f"close-observation-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": 1, "source": "baostock", "dataset": "daily_bars",
        "symbol": symbol, "session": session, "mode": "source_probe_only",
        "requested_at": requested_at.isoformat(), "timeout_seconds": timeout,
        "source_fallback": False, "same_day_l1_authorized": False,
        "historical_visibility_proven": False, "source_response_finalized": False,
        "target_row_observed": False,
    }
    try:
        acquired = fetch_baostock("daily_bars", session, session, symbol, timeout)
        sdk_version = metadata.version("baostock")
        frame = acquired.frame.astype(object).where(pd.notna(acquired.frame), None)
        row_count = len(frame)
        returned_dates = sorted(set(frame["date"]))
        if row_count != 1 or returned_dates != [session] or set(frame["code"]) != {source_symbol}:
            raise SourceError("source adapter did not return exactly the requested daily row")
        raw = {"columns": list(frame.columns), "rows": frame.values.tolist()}
        _write_json(folder / "raw.json", raw)
        receipt.update(
            status="captured", row_count=row_count, returned_dates=returned_dates,
            fetched_at=acquired.fetched_at, raw_sha256=file_sha256(folder / "raw.json"),
            sdk_version=sdk_version, capture_level="sdk_values", target_row_observed=True,
        )
    except (SourceError, ValueError, KeyError, TypeError, metadata.PackageNotFoundError) as exc:
        receipt.update(status="unavailable", error_code=type(exc).__name__, error=str(exc)[:500])
    finished_at = _utc_now(now)
    receipt["probe_finished_at"] = finished_at.isoformat()
    receipt["observed_shanghai_time"] = finished_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    path = folder / "observation.json"
    _write_json(path, receipt)
    return path


def read_receipt(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe one real-time BaoStock daily response without publishing it")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/runs"))
    args = parser.parse_args()
    path = probe(args.symbol, args.session, args.output_root, timeout=args.timeout)
    receipt = read_receipt(path)
    print(json.dumps({"receipt": str(path), "status": receipt["status"]}, ensure_ascii=False))
    return 0 if receipt["status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
