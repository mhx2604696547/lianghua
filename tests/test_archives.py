import hashlib
import io
import json
import tarfile

import pandas as pd
import pytest

from ashare_research import archives
from ashare_research.snapshots import sha256
from ashare_research.sources import BAR_FIELDS, FREE_DATA_V0_SOURCE, SourceError


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    fields = BAR_FIELDS.split(",")
    row = [
        "2024-07-09", "sh.600519", "10.00", "11.00", "9.00", "10.00",
        "9.90", "100", "1000", "3", "0.1", "1", "1", "0",
    ]
    tar_path = tmp_path / "release.tar"
    with tarfile.open(tar_path, "w") as archive:
        for code, frame in (
            ("600519", pd.DataFrame([row], columns=fields)),
            ("600001", pd.DataFrame(columns=fields)),
        ):
            body = frame.to_parquet(index=False)
            member = tarfile.TarInfo(f"raw/bars_daily/CN_SSE_{code}.parquet")
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    monkeypatch.setitem(archives._RELEASE, "sha256", hashlib.sha256(tar_path.read_bytes()).hexdigest())
    return tar_path


def test_import_records_empty_and_missing_members(release_fixture, tmp_path):
    output, reports = tmp_path / "snapshots", tmp_path / "reports"
    requested = ("600519.SH", "600001.SH", "000976.SZ")
    report_path, status = archives.import_free_data_v0(release_fixture, requested, output, reports)
    report = json.loads(report_path.read_bytes())
    assert status == report["status"] == "partial"
    assert report["requested_symbols"] == list(requested)
    assert report["results"]["600001.SH"]["reason"] == "empty_archive_member"
    assert report["results"]["000976.SZ"]["reason"] == "missing_archive_member"
    captured = report["results"]["600519.SH"]
    path = output / captured["manifest"]
    manifest = json.loads(path.read_bytes())
    assert captured["manifest_sha256"] == sha256(path)
    assert manifest["source"] == FREE_DATA_V0_SOURCE
    assert manifest["visibility_mode"] == "reconstructed"
    assert manifest["historical_version_proof"] is None
    assert manifest["metadata"]["release"]["sha256"] == sha256(release_fixture)
    assert manifest["metadata"]["capture_level"] == "publisher_parquet"
    assert len(list(output.glob("*/manifest.json"))) == 1


def test_import_rejects_altered_release_before_publishing(release_fixture, tmp_path):
    with release_fixture.open("ab") as stream:
        stream.write(b"altered")
    output, reports = tmp_path / "snapshots", tmp_path / "reports"
    with pytest.raises(SourceError, match="SHA-256 mismatch"):
        archives.import_free_data_v0(release_fixture, ("600519.SH",), output, reports)
    assert not output.exists()
    assert not reports.exists()
