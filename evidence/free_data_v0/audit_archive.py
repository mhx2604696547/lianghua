"""Audit the pinned BaoStock release without writing any market input snapshots."""

import bisect
import csv
import hashlib
import io
import json
import re
import tarfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT.parent
ARCHIVE = ROOT / "a-share-raw-v0.tar"
PUBLISHED_HASH = "9e5d9d7386f695f5b255b4d44adb435aec410c5df4276f8db44be111deed0d35"
CALENDAR = ARTIFACTS / "snapshots/trading_calendar-72081395f6b04c8dbc3bc16496c0766e"
DIRECT_BAO = ARTIFACTS / "snapshots/daily_bars-8f55c03555944c28b39ccbb97392e1d1"
SINA = ARTIFACTS / "w1-sina-daily-probe-c02fc150b2c745a7b6bea027bbed3cbb"
BAR = re.compile(r"raw/bars_daily/CN_(SSE|SZSE)_([0-9]{6})\.parquet")
FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "tradestatus", "pctChg", "isST",
)
KNOWN_FLAGS = pa.array(["0", "1"])
SAMPLE = {"600519.SH", "600190.SH", "000976.SZ"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_csv(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def verified_csv(folder):
    manifest = json.loads((folder / "manifest.json").read_bytes())
    body = (folder / "raw.csv").read_bytes()
    assert sha(body) == manifest["files"]["raw.csv"]
    return read_csv(body)


def count(flag):
    return pc.sum(pc.cast(pc.fill_null(flag, False), pa.int64())).as_py() or 0


def number(column):
    return pc.cast(pc.if_else(pc.equal(column, ""), None, column), pa.float64())


def bad_positive(column):
    return pc.invert(pc.fill_null(pc.and_(pc.is_finite(column), pc.greater(column, 0)), False))


def inspect(table, symbol, sessions, session_set, first_day, last_day):
    dates = table["date"].to_pylist()
    date_set = set(dates)
    if None in date_set or not dates or count(pc.is_null(pc.strptime(table["date"], "%Y-%m-%d", "s", error_is_null=True))):
        raise ValueError("invalid member date")
    first, last = min(dates), max(dates)
    if (first < "2010-01-04" or last > "2026-09-24"):
        raise ValueError("outside release dates")
    code, market = symbol.split(".")
    active = pc.equal(table["tradestatus"], "1")
    halted = pc.equal(table["tradestatus"], "0")
    price = {field: number(table[field]) for field in ("open", "high", "low", "close", "preclose")}
    invalid_ohlc = pc.or_kleene(
        pc.or_(bad_positive(price["open"]), bad_positive(price["close"])),
        pc.or_(bad_positive(price["low"]), bad_positive(price["high"])),
    )
    for field in ("open", "close", "low"):
        invalid_ohlc = pc.or_kleene(invalid_ohlc, pc.less(price["high"], price[field]))
    for field in ("open", "close"):
        invalid_ohlc = pc.or_kleene(invalid_ohlc, pc.greater(price["low"], price[field]))
    days = set(sessions[bisect.bisect_left(sessions, first):bisect.bisect_right(sessions, last)])
    between = {day for day in date_set if first_day <= day <= last_day}
    return {
        "symbol": symbol, "rows": len(dates), "first": first, "last": last,
        "duplicate_dates": len(dates) - len(date_set),
        "rows_in_audited_window": sum(first_day <= day <= last_day for day in dates),
        "internal_missing_sessions": len(days - date_set),
        "off_calendar_dates": len(between - session_set),
        "bad_code": len(dates) - count(pc.equal(table["code"], f"{market.lower()}.{code}")),
        "not_unadjusted": len(dates) - count(pc.equal(table["adjustflag"], "3")),
        "risk_warning_rows": count(pc.equal(table["isST"], "1")),
        "suspended_rows": count(halted),
        "unknown_isST": len(dates) - count(pc.is_in(table["isST"], value_set=KNOWN_FLAGS)),
        "unknown_tradestatus": len(dates) - count(pc.is_in(table["tradestatus"], value_set=KNOWN_FLAGS)),
        "active_without_positive_preclose": count(pc.and_kleene(active, bad_positive(price["preclose"]))),
        "active_invalid_ohlc": count(pc.and_kleene(active, invalid_ohlc)),
    }


def main():
    cal_rows = verified_csv(CALENDAR)
    first_day, last_day = "2021-01-01", "2026-09-24"
    sessions = sorted(row["calendar_date"] for row in cal_rows
                      if row["is_trading_day"] == "1" and first_day <= row["calendar_date"] <= last_day)
    session_set = set(sessions)
    stats, bad, sample = [], [], {}
    with ARCHIVE.open("rb") as file:
        assert hashlib.file_digest(file, "sha256").hexdigest() == PUBLISHED_HASH
        file.seek(0)
        with tarfile.open(fileobj=file, mode="r:") as archive:
            members = [(member, BAR.fullmatch(member.name)) for member in archive]
            members = [(m, match) for m, match in members if match is not None]
            assert len({m.name for m, _ in members}) == len(members)
            for member, match in members:
                market = "SH" if match[1] == "SSE" else "SZ"
                symbol = f"{match[2]}.{market}"
                try:
                    if not member.isfile() or not 0 < member.size <= 2_000_000:
                        raise ValueError("unexpected member type or size")
                    body = archive.extractfile(member).read()
                    parquet = pq.ParquetFile(io.BytesIO(body))
                    if tuple(parquet.schema_arrow.names) != FIELDS or parquet.metadata.num_rows > 4300:
                        raise ValueError("unexpected schema or row count")
                    table = parquet.read(use_threads=False)
                    stats.append(inspect(table, symbol, sessions, session_set, first_day, last_day))
                    if symbol in SAMPLE:
                        sample[symbol] = {"member": member.name, "sha256": sha(body), "rows": table.to_pylist()}
                except (ValueError, pa.ArrowException) as exc:
                    bad.append({"symbol": symbol, "member": member.name, "error": str(exc)[:300]})

    if not stats:
        raise RuntimeError("no valid daily member found")
    with (ROOT / "symbol_coverage.csv").open("x", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=tuple(stats[0]))
        writer.writeheader()
        writer.writerows(sorted(stats, key=lambda x: x["symbol"]))
    metrics = Counter()
    nonzero_symbols = Counter()
    for record in stats:
        for key, value in record.items():
            if type(value) is int:
                metrics[key] += value
                nonzero_symbols[key] += int(value > 0)
    direct_bao_rows = verified_csv(DIRECT_BAO)
    direct_dates = {row["date"] for row in direct_bao_rows}
    archived_bao_rows = [row for row in sample["600519.SH"]["rows"] if row["date"] in direct_dates]
    assert archived_bao_rows == direct_bao_rows
    sina_report = json.loads((SINA / "probe.json").read_bytes())
    sina_body = (SINA / "sh600519.csv").read_bytes()
    assert sha(sina_body) == sina_report["results"][0]["sha256"]
    sina_rows = {row["date"]: row for row in read_csv(sina_body)}
    assert set(sina_rows) == direct_dates
    comparisons = []
    for row in archived_bao_rows:
        other = sina_rows[row["date"]]
        fields = ("open", "high", "low", "close", "volume")
        matches = all(Decimal(row[field]) == Decimal(other[field]) for field in fields)
        comparisons.append({"date": row["date"], "prices_and_volume_match": matches,
                            "sina_minus_bao_amount_cny": str(Decimal(other["amount"]) - Decimal(row["amount"]))})
    assert all(row["prices_and_volume_match"] for row in comparisons)
    for symbol, proof in sample.items():
        window = {
            "600519.SH": ("2026-09-14", "2026-09-18"),
            "600190.SH": ("2025-07-15", "2025-07-26"),
            "000976.SZ": ("2024-07-08", "2024-07-10"),
        }[symbol]
        proof["selected_rows"] = [row for row in proof.pop("rows") if window[0] <= row["date"] <= window[1]]
    record = {
        "audit_version": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "scope": "release_shape_and_calendar_overlap",
        "visibility_mode": "reconstructed",
        "membership_ready": False,
        "preopen_state_verified": False,
        "archive_sha256": PUBLISHED_HASH,
        "archive_size": ARCHIVE.stat().st_size,
        "audit_script_sha256": sha(Path(__file__).read_bytes()),
        "calendar_manifest_sha256": sha((CALENDAR / "manifest.json").read_bytes()),
        "calendar_overlap": [first_day, last_day],
        "audited_trading_sessions": len(sessions),
        "missing_session_denominator": "local audited calendar between first and last returned dates per symbol",
        "matched_archive_members": len(members),
        "decoded_members": len(stats),
        "invalid_members": bad,
        "total": dict(metrics),
        "symbols_with_nonzero": dict(nonzero_symbols),
        "known_samples": sample,
        "direct_baostock_overlap_rows": len(direct_bao_rows),
        "sina_overlap": comparisons,
        "sina_failed_queries": [item for item in sina_report["results"] if item["status"] != "captured"],
        "symbol_coverage_sha256": sha((ROOT / "symbol_coverage.csv").read_bytes()),
    }
    (ROOT / "source_audit.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: record[key] for key in (
        "matched_archive_members", "decoded_members", "audited_trading_sessions", "total",
        "symbols_with_nonzero", "direct_baostock_overlap_rows",
    )}, indent=2))
    print(f"invalid_members={len(bad)}")


if __name__ == "__main__":
    main()
