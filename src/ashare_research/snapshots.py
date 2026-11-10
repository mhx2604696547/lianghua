"""L0 acquisition artifacts. All downstream evidence reads go through pit.py."""

from __future__ import annotations

import hashlib
import json
import copy
import re
import uuid
from collections.abc import Mapping
from pathlib import Path

from .sources import AcquiredData


def snapshot_data_kind(metadata: Mapping[str, object]) -> str:
    """Read the declared input kind without promoting unlabelled legacy artifacts."""
    kind = metadata.get("data_kind")
    return kind if isinstance(kind, str) and kind in ("market", "synthetic") else "unspecified"


def sha256(path: Path) -> str:
    """Hash exact artifact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze(data: AcquiredData, root: Path) -> Path:
    """Publish a manifest only after raw and canonical data have been written."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", data.dataset):
        raise ValueError("invalid dataset name")
    root = root.resolve()
    folder = root / f"{data.dataset}-{uuid.uuid4().hex}"
    if not folder.resolve().is_relative_to(root):
        raise ValueError("snapshot must remain within its root")
    frame = data.frame.copy(deep=True)
    metadata = copy.deepcopy(data.metadata)
    folder.mkdir(parents=True, exist_ok=False)
    raw = folder / "raw.csv"
    canonical = folder / "data.parquet"
    frame.to_csv(raw, index=False, encoding="utf-8")
    frame.to_parquet(canonical, index=False)
    manifest = {
        "schema_version": 1, "dataset": data.dataset, "source": data.source,
        "fetched_at": data.fetched_at, "row_count": len(frame),
        "columns": list(frame.columns), "metadata": metadata,
        "visibility_mode": "reconstructed",
        "historical_version_proof": None,
        "files": {"raw.csv": sha256(raw), "data.parquet": sha256(canonical)},
    }
    path = folder / "manifest.json"
    staging = folder / ".manifest.tmp"
    with staging.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
    staging.replace(path)
    return path

