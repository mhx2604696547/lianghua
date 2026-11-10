"""Import explicitly chosen BaoStock daily members from one public release."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pandas as pd

from .snapshots import freeze, sha256
from .sources import BAR_FIELDS, FREE_DATA_V0_SOURCE, AcquiredData, SourceError, bs_symbol, validate_baostock_bars

_RELEASE = {
    "tag": "FREE_DATA_v0",
    "publisher_commit": "7a8d083",
    "url": "https://github.com/noblechang/a-share-data/releases/download/FREE_DATA_v0/a-share-raw-v0.tar",
    "sha256": "9e5d9d7386f695f5b255b4d44adb435aec410c5df4276f8db44be111deed0d35",
}
_RELEASE_DATES = ("2010-01-04", "2026-09-24")
_FIELDS = tuple(BAR_FIELDS.split(","))


def _identity(handle: BinaryIO) -> tuple[int, int, int, int]:
    info = os.fstat(handle.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _daily_member(archive: tarfile.TarFile, member: tarfile.TarInfo, code: str) -> AcquiredData:
    if not member.isfile() or not 0 < member.size <= 2_000_000:
        raise SourceError("invalid_archive_member_type_or_size")
    reader = archive.extractfile(member)
    if reader is None:
        raise SourceError("missing_archive_member_bytes")
    with reader:
        body = reader.read()
    if len(body) != member.size:
        raise SourceError("incomplete_archive_member")
    frame = pd.read_parquet(io.BytesIO(body), engine="pyarrow")
    if tuple(frame.columns) != _FIELDS:
        raise SourceError("unexpected_archive_bar_schema")
    if frame.empty:
        raise SourceError("empty_archive_member")
    validate_baostock_bars(frame, code, *_RELEASE_DATES)
    return AcquiredData(
        "daily_bars", FREE_DATA_V0_SOURCE, frame, datetime.now(timezone.utc).isoformat(),
        {
            "data_kind": "market", "capture_level": "publisher_parquet", "source_fallback": False,
            "coverage_status": "not_audited", "adjustment": "unadjusted", "volume_unit": "share",
            "amount_unit": "CNY", "actual_first_date": frame["date"].min(),
            "actual_last_date": frame["date"].max(),
            "release": {**_RELEASE, "member": member.name, "member_sha256": hashlib.sha256(body).hexdigest()},
        },
    )


def import_free_data_v0(
    archive_path: Path, symbols: tuple[str, ...], snapshots_root: Path, reports_root: Path,
) -> tuple[Path, str]:
    """Publish per-symbol snapshots and a report accounting for every requested symbol."""
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("supply one or more unique symbols")
    requests = {}
    for symbol in symbols:
        code = bs_symbol(symbol)
        venue = {"SH": "SSE", "SZ": "SZSE"}[symbol[-2:]]
        requests[f"raw/bars_daily/CN_{venue}_{symbol[:6]}.parquet"] = (symbol, code)
    snapshots_root = snapshots_root.resolve()
    results = {}
    with archive_path.open("rb") as handle:
        identity = _identity(handle)
        if hashlib.file_digest(handle, "sha256").hexdigest() != _RELEASE["sha256"]:
            raise SourceError("release archive SHA-256 mismatch")
        if _identity(handle) != identity:
            raise SourceError("release archive changed during verification")
        handle.seek(0)
        with tarfile.open(fileobj=handle, mode="r:") as archive:
            matches: dict[str, list[tarfile.TarInfo]] = {}
            for member in archive:
                if member.name in requests:
                    matches.setdefault(member.name, []).append(member)
            for name, (symbol, code) in requests.items():
                selected = matches.get(name, [])
                try:
                    if not selected:
                        raise SourceError("missing_archive_member")
                    if len(selected) != 1:
                        raise SourceError("duplicate_archive_member")
                    data = _daily_member(archive, selected[0], code)
                    if _identity(handle) != identity:
                        raise SourceError("release archive changed during import")
                except (SourceError, ValueError, TypeError, OSError) as exc:
                    results[symbol] = {"status": "unavailable", "member": name, "reason": str(exc)}
                    continue
                path = freeze(data, snapshots_root)
                results[symbol] = {
                    "status": "captured", "member": name, "rows": len(data.frame),
                    "manifest": path.relative_to(snapshots_root).as_posix(), "manifest_sha256": sha256(path),
                }
    captured = sum(item["status"] == "captured" for item in results.values())
    status = "complete" if captured == len(symbols) else ("partial" if captured else "failed")
    folder = reports_root.resolve() / f"free-data-v0-import-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1, "status": status, "scope": "source_import",
        "coverage_status": "not_audited" if status == "complete" else "partial",
        "data_kind": "market", "visibility_mode": "reconstructed", "membership_ready": False,
        "observed_at": datetime.now(timezone.utc).isoformat(), "release": _RELEASE,
        "snapshots_root": str(snapshots_root), "requested_symbols": list(symbols), "results": results,
    }
    staging = folder / ".report.tmp"
    staging.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    path = folder / "import.json"
    staging.rename(path)
    return path, status
