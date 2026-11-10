"""Pinned daily evidence for a trusted research runner, not a strategy data handle."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .opening import OPENING_FIELDS, OPENING_SOURCE, cash_ex_reference, known_opening_rule, price_bands
from .snapshots import sha256, snapshot_data_kind
from .sources import FREE_DATA_V0_SOURCE
from .universe import (
    CALENDAR_AUDIT_METHOD, LISTING_FIELDS, RISK_NAME_FIELDS,
    ListingAgeWindow, SessionEvidence, project_listings,
)


_DIGEST = re.compile(r"[0-9a-f]{64}")
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ)")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAILY_FIELDS = ("date", "code", "open", "high", "low", "close", "volume", "amount")
_OPEN_FIELDS = ("date", "code", "open")
_PRIOR_CLOSE_FIELDS = ("date", "code", "close", "tradestatus")
_CALENDAR_FIELDS = ("calendar_date", "is_trading_day")
_BAR_STATUS_FIELDS = ("date", "code", "isST", "tradestatus")
_BAR_STATUS_SOURCES = {"baostock": "baostock_daily", FREE_DATA_V0_SOURCE: "baostock_free_data_v0"}
STUDY_FIXTURE_SOURCE = "synthetic_study_fixture_v1"
STUDY_FIXTURE_COMPLETION = time(17, 30)


class EvidenceUnavailable(RuntimeError):
    """The requested evidence cannot be established from the frozen inputs."""


@dataclass(frozen=True, slots=True)
class _Pin:
    manifest: str
    sha256: str

    def record(self) -> dict[str, str]:
        return {"manifest": self.manifest, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReadReceipt:
    lake_cut_id: str
    purpose: str
    symbol: str
    start: str
    end: str
    as_of: str
    source: str
    bar_manifest_sha256: str
    calendar_manifest_sha256: str
    data_kind: str
    visibility_mode: str = "reconstructed"
    scope: str = "diagnostic_only"


@dataclass(frozen=True, slots=True)
class ListingReceipt:
    lake_cut_id: str
    session: str
    as_of: str
    listing_manifest_sha256: str
    purpose: str = "listing_diagnostic"
    visibility_mode: str = "reconstructed"
    coverage_status: str = "not_audited"
    membership_ready: bool = False
    scope: str = "diagnostic_only"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise EvidenceUnavailable("artifact resolves outside its configured root")
    return path


def _read_bytes(root: Path, relative: str) -> bytes:
    try:
        return _inside(root, relative).read_bytes()
    except OSError as exc:
        raise EvidenceUnavailable(f"cannot read pinned artifact: {relative}") from exc


def _object(payload: bytes, name: str) -> dict:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceUnavailable(f"invalid {name} JSON") from exc
    if not isinstance(data, dict):
        raise EvidenceUnavailable(f"invalid {name} object")
    return data


def _pin_from_record(record: object, dataset: str) -> _Pin:
    if not isinstance(record, dict) or set(record) != {"manifest", "sha256"}:
        raise EvidenceUnavailable("invalid snapshot pin")
    path, digest = record["manifest"], record["sha256"]
    if not isinstance(path, str) or not re.fullmatch(
        rf"{dataset}-[0-9a-f]{{32}}/manifest\.json", path
    ):
        raise EvidenceUnavailable(f"invalid {dataset} manifest path")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise EvidenceUnavailable("invalid manifest hash")
    return _Pin(path, digest)


def _verified_snapshot(root: Path, pin: _Pin, dataset: str) -> tuple[dict, bytes]:
    manifest_bytes = _read_bytes(root, pin.manifest)
    if _digest(manifest_bytes) != pin.sha256:
        raise EvidenceUnavailable("pinned manifest hash mismatch")
    manifest = _object(manifest_bytes, "snapshot manifest")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("dataset") != dataset
        or not isinstance(manifest.get("source"), str)
        or not manifest["source"]
        or manifest.get("visibility_mode") != "reconstructed"
        or manifest.get("historical_version_proof") is not None
        or not isinstance(manifest.get("metadata"), dict)
        or manifest["metadata"].get("source_fallback", False) is not False
        or type(manifest.get("row_count")) is not int
        or manifest["row_count"] < 1
    ):
        raise EvidenceUnavailable(f"unsupported {dataset} snapshot contract")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"raw.csv", "data.parquet"}:
        raise EvidenceUnavailable("unexpected snapshot artifact list")
    folder = pin.manifest.split("/")[0]
    try:
        raw_hash = sha256(_inside(root, f"{folder}/raw.csv"))
    except OSError as exc:
        raise EvidenceUnavailable("cannot read pinned raw snapshot") from exc
    if raw_hash != files["raw.csv"]:
        raise EvidenceUnavailable("raw snapshot hash mismatch")
    parquet = _read_bytes(root, f"{folder}/data.parquet")
    if _digest(parquet) != files["data.parquet"]:
        raise EvidenceUnavailable("Parquet snapshot hash mismatch")
    # Parse the same bytes that were verified, even if the file changes later.
    return manifest, parquet


def _make_pin(root: Path, path: Path, dataset: str) -> _Pin:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise EvidenceUnavailable("snapshot manifest must be inside the configured root")
    relative = resolved.relative_to(root).as_posix()
    pin = _pin_from_record(
        {"manifest": relative, "sha256": _digest(_read_bytes(root, relative))},
        dataset,
    )
    _verified_snapshot(root, pin, dataset)
    return pin


def _validate_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("symbol must have the form 600519.SH or 000001.SZ")


def freeze_daily_cut(
    snapshots_root: Path,
    cuts_root: Path,
    calendar_manifest: Path | None,
    bar_manifests: Mapping[str, Path],
    *,
    listing_manifest: Path | None = None,
    risk_name_manifest: Path | None = None,
    opening_manifest: Path | None = None,
) -> str:
    """Publish explicit input versions, preserving the layout of existing daily cuts."""
    if not bar_manifests and listing_manifest is None:
        raise ValueError("a cut requires daily inputs or listing evidence")
    if bar_manifests and calendar_manifest is None:
        raise ValueError("daily inputs require a trading calendar")
    if risk_name_manifest is not None and listing_manifest is None:
        raise ValueError("risk name evidence requires a pinned listing archive")
    if opening_manifest is not None and (listing_manifest is None or calendar_manifest is None):
        raise ValueError("opening evidence requires pinned listing and calendar archives")
    root = snapshots_root.resolve()
    calendar = (
        _make_pin(root, calendar_manifest, "trading_calendar").record()
        if calendar_manifest is not None else None
    )
    bars = {}
    for symbol, path in bar_manifests.items():
        _validate_symbol(symbol)
        bars[symbol] = _make_pin(root, path, "daily_bars").record()
    cut = {"version": 1, "calendar": calendar, "bars": bars}
    if listing_manifest is not None:
        cut["version"] = 2
        cut["listings"] = _make_pin(root, listing_manifest, "listing_evidence").record()
    if risk_name_manifest is not None:
        cut["risk_names"] = _make_pin(root, risk_name_manifest, "risk_name_events").record()
    if opening_manifest is not None:
        cut["version"] = 3
        cut["opening"] = _make_pin(root, opening_manifest, "opening_evidence").record()
    payload = json.dumps(cut, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cut_id = _digest(payload)
    cuts_root = cuts_root.resolve()
    cuts_root.mkdir(parents=True, exist_ok=True)
    destination = cuts_root / f"{cut_id}.json"
    staged = tempfile.NamedTemporaryFile(dir=cuts_root, prefix=".cut-", delete=False)
    staging_path = Path(staged.name)
    try:
        with staged:
            staged.write(payload)
            staged.flush()
            os.fsync(staged.fileno())
        try:
            if os.name == "nt":
                # Windows rename fails if the destination exists; no link privilege needed.
                staging_path.rename(destination)
            else:
                os.link(staging_path, destination)
        except FileExistsError:
            if _read_bytes(cuts_root, f"{cut_id}.json") != payload:
                raise EvidenceUnavailable("existing lake cut hash mismatch")
    finally:
        staging_path.unlink(missing_ok=True)
    return cut_id


def _dataset_frame(
    root: Path, pin: _Pin, dataset: str, columns: tuple[str, ...],
) -> tuple[pd.DataFrame, dict]:
    manifest, parquet = _verified_snapshot(root, pin, dataset)
    return _decode_frame(manifest, parquet, dataset, columns), manifest


def _decode_frame(manifest: dict, parquet: bytes, dataset: str, columns: tuple[str, ...]) -> pd.DataFrame:
    declared = manifest.get("columns")
    if not isinstance(declared, list) or not all(col in declared for col in columns):
        raise EvidenceUnavailable(f"missing fields in {dataset} snapshot")
    try:
        frame = pd.read_parquet(io.BytesIO(parquet), columns=list(columns))
    except (KeyError, OSError, ValueError) as exc:
        raise EvidenceUnavailable(f"unreadable {dataset} snapshot") from exc
    if len(frame) != manifest["row_count"] or tuple(frame.columns) != columns:
        raise EvidenceUnavailable(f"{dataset} artifact disagrees with its manifest")
    return frame


def _day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def _dates(values: pd.Series, dataset: str) -> pd.Series:
    try:
        return values.map(_day)
    except ValueError as exc:
        raise EvidenceUnavailable(f"invalid session date in {dataset}") from exc


def _clock(as_of: str) -> datetime:
    stamp = datetime.fromisoformat(as_of)
    if stamp.utcoffset() is None:
        raise ValueError("as_of requires an explicit timezone")
    return stamp.astimezone(_SHANGHAI)


def _calendar_sessions(frame: pd.DataFrame, start: date, end: date) -> set[date]:
    dates = _dates(frame["calendar_date"], "trading_calendar")
    window = dates.between(start, end)
    dates, flags = dates.loc[window], frame.loc[window, "is_trading_day"]
    if len(dates) != (end - start).days + 1 or dates.duplicated().any():
        raise EvidenceUnavailable("incomplete trading calendar window")
    if not flags.isin(("0", "1")).all():
        raise EvidenceUnavailable("unknown trading calendar marker")
    sessions = set(dates.loc[flags == "1"])
    if not sessions:
        raise EvidenceUnavailable("window contains no trading sessions")
    return sessions


class DailyPIT:
    """Open one frozen cut. A trusted runner passes only returned slices downstream."""

    def __init__(
        self, snapshots_root: Path, cuts_root: Path, lake_cut_id: str,
        *, mode: str = "verified_visible",
    ) -> None:
        if not isinstance(lake_cut_id, str) or not _DIGEST.fullmatch(lake_cut_id):
            raise ValueError("invalid lake cut ID")
        if mode not in ("verified_visible", "reconstructed"):
            raise ValueError("unknown visibility mode")
        content = _read_bytes(cuts_root.resolve(), f"{lake_cut_id}.json")
        if _digest(content) != lake_cut_id:
            raise EvidenceUnavailable("lake cut hash mismatch")
        cut = _object(content, "lake cut")
        if type(cut.get("version")) is not int or cut["version"] not in (1, 2, 3):
            raise EvidenceUnavailable("unsupported lake cut version")
        bars = cut.get("bars")
        if not isinstance(bars, dict) or (cut["version"] == 1 and not bars):
            raise EvidenceUnavailable("lake cut has invalid daily inputs")
        for symbol in bars:
            _validate_symbol(symbol)
        self._root = snapshots_root.resolve()
        self._cut_id = lake_cut_id
        self._mode = mode
        calendar = cut.get("calendar")
        self._calendar = (
            _pin_from_record(calendar, "trading_calendar") if calendar is not None else None
        )
        if bars and self._calendar is None:
            raise EvidenceUnavailable("daily inputs have no pinned trading calendar")
        self._bars = {
            symbol: _pin_from_record(record, "daily_bars")
            for symbol, record in bars.items()
        }
        self._listings = (
            _pin_from_record(cut.get("listings"), "listing_evidence") if cut["version"] >= 2 else None
        )
        self._opening = (
            _pin_from_record(cut.get("opening"), "opening_evidence") if cut["version"] == 3 else None
        )
        if self._opening is not None and self._calendar is None:
            raise EvidenceUnavailable("opening evidence has no pinned trading calendar")
        risk_names = cut.get("risk_names")
        if risk_names is not None and self._listings is None:
            raise EvidenceUnavailable("risk names have no pinned listing archive")
        self._risk_names = (
            _pin_from_record(risk_names, "risk_name_events") if risk_names is not None else None
        )

    def _trading_days(self, start: date, end: date) -> set[date]:
        if self._calendar is None:
            raise EvidenceUnavailable("no pinned trading calendar")
        frame, _ = _dataset_frame(
            self._root, self._calendar, "trading_calendar", _CALENDAR_FIELDS
        )
        return _calendar_sessions(frame, start, end)

    def read_trading_sessions(self, start: str, end: str) -> tuple[date, ...]:
        """Return the pinned schedule for reconstructed diagnostics or fixture runs."""
        if self._mode != "reconstructed":
            raise EvidenceUnavailable("no approved historical-version proof for the calendar")
        first, last = _day(start), _day(end)
        if first > last:
            raise ValueError("start must not follow end")
        return tuple(sorted(self._trading_days(first, last)))

    def _listing_age_window(self, day: date, clock: datetime, minimum: int) -> ListingAgeWindow:
        if type(minimum) is not int or minimum < 1:
            raise ValueError("minimum listed sessions must be a positive integer")
        if clock < datetime.combine(day, time(15, 0), tzinfo=_SHANGHAI):
            raise EvidenceUnavailable("listing age requires the completed trading session")
        first, sessions, _ = self._audited_sessions_through(day, purpose="listing age")
        return ListingAgeWindow(minimum, first, tuple(sorted(sessions)))

    def _audited_sessions_through(
        self, day: date, *, purpose: str = "preopen control",
    ) -> tuple[date, set[date], dict]:
        if self._calendar is None:
            raise EvidenceUnavailable(f"{purpose} requires a pinned audited calendar")
        frame, manifest = _dataset_frame(
            self._root, self._calendar, "trading_calendar", _CALENDAR_FIELDS,
        )
        metadata = manifest["metadata"]
        if metadata.get("calendar_audit_method") != CALENDAR_AUDIT_METHOD:
            raise EvidenceUnavailable(f"{purpose} requires a reconciled trading calendar")
        first, last = _day(metadata["first_date"]), _day(metadata["last_date"])
        if not first <= day <= last:
            query = "listing date query" if purpose == "listing age" else "session query"
            raise EvidenceUnavailable(f"{query} is outside the audited calendar")
        sessions = _calendar_sessions(frame, first, day)
        if day not in sessions:
            raise EvidenceUnavailable(f"{purpose} requires a trading date")
        return first, sessions, metadata

    def _session_observations(
        self, day: date, clock: datetime, symbols: tuple[str, ...],
    ) -> dict[str, list[SessionEvidence]]:
        # Archived name points and daily flags carry dates without public availability times.
        if day >= clock.date():
            return {}
        selected = set(symbols)
        observations: dict[str, list[SessionEvidence]] = {}
        if self._risk_names is not None:
            frame, manifest = _dataset_frame(
                self._root, self._risk_names, "risk_name_events", RISK_NAME_FIELDS,
            )
            if manifest["source"] != "szse_short_name_change_points":
                raise EvidenceUnavailable("unsupported risk name source")
            points = frame.loc[frame["session"].eq(day.isoformat())]
            if points["symbol"].duplicated().any() or not points["marker"].isin(("ST", "*ST")).all():
                raise EvidenceUnavailable("ambiguous risk marker observations")
            kind = snapshot_data_kind(manifest["metadata"])
            for symbol in points["symbol"]:
                if not selected or symbol in selected:
                    observations[symbol] = [SessionEvidence(
                        "szse_name_change", self._risk_names.sha256, risk_warning=True, data_kind=kind,
                    )]
        for symbol, pin in self._bars.items():
            if selected and symbol not in selected:
                continue
            manifest, parquet = _verified_snapshot(self._root, pin, "daily_bars")
            source = _BAR_STATUS_SOURCES.get(manifest["source"])
            if source is None:
                continue
            columns = manifest.get("columns")
            if not isinstance(columns, list):
                raise EvidenceUnavailable("invalid status bar column declaration")
            if not set(_BAR_STATUS_FIELDS).issubset(columns):
                continue
            frame = _decode_frame(manifest, parquet, "daily_bars", _BAR_STATUS_FIELDS)
            code, exchange = symbol.split(".")
            if not frame["code"].isin((symbol, f"{exchange.lower()}.{code}")).all():
                raise EvidenceUnavailable("status bars belong to a different instrument")
            rows = frame.loc[frame["date"].eq(day.isoformat())]
            if len(rows) > 1:
                raise EvidenceUnavailable("duplicate daily status observation")
            if rows.empty:
                continue
            risk, trading = (
                value == "1" if isinstance(value, str) and value in ("0", "1") else None
                for value in (rows.iloc[0]["isST"], rows.iloc[0]["tradestatus"])
            )
            if risk is not None or trading is not None:
                observations.setdefault(symbol, []).append(SessionEvidence(
                    source, pin.sha256, risk_warning=risk, normal_trading=trading,
                    data_kind=snapshot_data_kind(manifest["metadata"]),
                ))
        return observations

    def read_listings(
        self, session: str, *, as_of: str, symbols: tuple[str, ...] = (),
        min_listed_sessions: int | None = None,
        check_session_status: bool = False,
    ) -> tuple[pd.DataFrame, ListingReceipt]:
        """Historical listing context and documented entry exclusions."""
        clock, day = _clock(as_of), _day(session)
        if self._mode != "reconstructed":
            raise EvidenceUnavailable("no approved historical-version proof for the roster")
        if self._listings is None:
            raise EvidenceUnavailable("no pinned listing evidence")
        if clock < datetime.combine(day, time(9, 30), tzinfo=_SHANGHAI):
            raise EvidenceUnavailable("cannot read listing state before that session opens")
        for symbol in symbols:
            _validate_symbol(symbol)
        if len(set(symbols)) != len(symbols):
            raise ValueError("listing request contains duplicate symbols")
        if check_session_status and min_listed_sessions is None:
            raise ValueError("session status screening requires an explicit listing age minimum")
        frame, meta = self._listing_inputs(day)
        age_window = (
            self._listing_age_window(day, clock, min_listed_sessions)
            if min_listed_sessions is not None else None
        )
        observations = self._session_observations(day, clock, symbols) if check_session_status else None
        view = project_listings(frame, meta, session, clock.date().isoformat(), symbols, age_window, observations)
        return view, ListingReceipt(self._cut_id, session, clock.isoformat(), self._listings.sha256)

    def _listing_inputs(self, day: date) -> tuple[pd.DataFrame, dict]:
        if self._listings is None:
            raise EvidenceUnavailable("no pinned listing evidence")
        frame, manifest = _dataset_frame(self._root, self._listings, "listing_evidence", LISTING_FIELDS)
        meta = manifest["metadata"]
        if day >= _day(meta["latest_session_exclusive"]):
            raise EvidenceUnavailable("session is outside the archived roster observation window")
        return frame, meta

    def _opening_context(self, symbol: str, day: date) -> tuple[list[dict], dict, dict, dict]:
        if self._opening is None:
            raise EvidenceUnavailable("no pinned opening evidence")
        session = day.isoformat()
        frame, manifest = _dataset_frame(self._root, self._opening, "opening_evidence", OPENING_FIELDS)
        meta = manifest["metadata"]
        if manifest["source"] != OPENING_SOURCE or meta.get("availability_policy") != "next_shanghai_calendar_day":
            raise EvidenceUnavailable("unsupported opening evidence contract")
        events = frame.loc[
            frame["symbol"].eq(symbol) & frame["from"].le(session) & frame["until"].gt(session)
            & frame["not_before_day"].le(session)
        ].to_dict("records")
        listings, listing_meta = self._listing_inputs(day)
        roster = project_listings(listings, listing_meta, session, session, (symbol,)).iloc[0].to_dict()
        return events, meta, roster, listing_meta

    @staticmethod
    def _known_opening_stop(events: list[dict], roster: dict) -> str | None:
        if "announced_delisting_effective" in roster["reasons"]:
            return "delisted"
        if "suspended" in roster["reasons"] or any(event["kind"] == "suspended" for event in events):
            return "suspended"
        return None

    def _preopen_reference(
        self, symbol: str, day: date, clock: datetime, sessions: set[date], notices: list[dict],
    ) -> dict:
        if len(notices) != 1:
            reason = "conflicting_cash_reference_notices" if notices else "no_reviewed_reference_terms"
            return {"state": "unavailable", "reason": reason}
        notice = notices[0]
        prior = max((session for session in sessions if session < day), default=None)
        if prior is None or prior.isoformat() != notice["record_on"]:
            return {"state": "unavailable", "reason": "cash_record_date_is_not_pinned_prior_session"}
        try:
            close, receipt = self._read("preopen_reference_input", symbol, prior, prior, clock, _PRIOR_CLOSE_FIELDS)
            if receipt.source not in _BAR_STATUS_SOURCES:
                raise EvidenceUnavailable("unsupported prior-session trading-status source")
            if close["tradestatus"].item() != "1":
                raise EvidenceUnavailable("prior closing price lacks a trading observation")
            reference = cash_ex_reference(close["close"].item(), notice["cash_per_share"])
        except (EvidenceUnavailable, ValueError) as exc:
            return {"state": "unavailable", "reason": str(exc)}
        return {
            "state": "reconstructed", "price_cny": reference, "formula": "cash_only_ex_reference_v1",
            "prior_session": prior.isoformat(), "prior_close_cny": close["close"].item(),
            "cash_per_share_cny": notice["cash_per_share"], "data_kind": receipt.data_kind,
            "bar_manifest_sha256": receipt.bar_manifest_sha256, "notice_sha256": notice["source_sha256"],
        }

    def read_preopen_controls(self, symbol: str, session: str, *, as_of: str, side: str) -> dict:
        """Resolve published blockers and scheduled price terms before the proxy open."""
        _validate_symbol(symbol)
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        day = _day(session)
        # AKQuant's actual pre-open callback may fall one nanosecond before 09:15.
        clock = pd.Timestamp(as_of)
        if pd.isna(clock) or clock.utcoffset() is None:
            raise ValueError("as_of requires an explicit timezone")
        clock = clock.tz_convert(_SHANGHAI)
        if self._mode != "reconstructed":
            raise EvidenceUnavailable("no approved historical-version proof for opening controls")
        if clock.date() != day or clock.time() >= time(9, 30):
            raise EvidenceUnavailable("preopen cutoff must be on the session before 09:30 Shanghai time")
        _, sessions, calendar_meta = self._audited_sessions_through(day)
        events, meta, roster, listing_meta = self._opening_context(symbol, day)
        stop = self._known_opening_stop(events, roster)
        phases = {event["kind"] for event in events if event["kind"] in {"delisting_first", "delisting_later"}}
        if "risk_warning" in roster["reasons"]:
            phases.add("risk_warning")
        phase = next(iter(phases)) if len(phases) == 1 else ("conflict" if phases else "unknown")
        rule = known_opening_rule(meta, symbol[-2:], session, session) if roster["board_scope"] == "main_a" else None
        notices = [event for event in events if event["kind"] == "cash_dividend"]
        reference = {"state": "unavailable", "reason": stop or "mainboard_price_rule_unverified"}
        if not stop and rule:
            reference = self._preopen_reference(symbol, day, clock, sessions, notices)

        limits = {"state": "unavailable"}
        conditional = None
        if not stop and rule:
            ratio = rule["ratios"].get(phase)
            if phase == "delisting_first":
                limits = {"state": "unbounded_daily_limit"}
            elif ratio:
                limits = {"state": "rate_known", "ratio": ratio}
                if reference["state"] == "reconstructed":
                    limits = {"state": "reconstructed", "ratio": ratio,
                              **price_bands(reference["price_cny"], ratio)}
            elif phase == "unknown" and reference["state"] == "reconstructed":
                ratio = rule["ratios"]["ordinary_mainboard"]
                conditional = {"condition": "ordinary_mainboard_session", "ratio": ratio,
                               **price_bands(reference["price_cny"], ratio)}

        blocker = stop
        if blocker is None and phase == "delisting_first":
            blocker = "unbounded_opening_regime_unsupported"
        if blocker is None and phase == "risk_warning" and side == "buy":
            blocker = "risk_warning_new_entry_excluded"
        reasons = [blocker] if blocker else ["preopen_tradability_unverified"]
        if not blocker and phase == "conflict":
            reasons.append("opening_regime_conflict")
        if not blocker and limits["state"] != "reconstructed":
            reasons.append("applicable_price_limits_unverified")
        kinds = {snapshot_data_kind(item) for item in (meta, listing_meta, calendar_meta)}
        if reference["state"] == "reconstructed":
            kinds.add(reference["data_kind"])
        data_kind = "synthetic" if "synthetic" in kinds else ("market" if kinds == {"market"} else "unspecified")
        return {
            "status": "ok", "scope": "diagnostic_only", "purpose": "preopen_control",
            "visibility_mode": "reconstructed", "data_kind": data_kind,
            "lake_cut_id": self._cut_id, "symbol": symbol, "session": session, "as_of": clock.isoformat(),
            "side": side, "submission": {"state": "blocked" if blocker else "unavailable", "reasons": reasons},
            "known_stop": stop, "board_scope": roster["board_scope"], "phase": phase,
            "daily_price_limits": limits, "conditional_daily_price_limits": conditional,
            "reference": reference, "rule_id": rule["id"] if rule else None,
            "evidence": {
                "opening_manifest_sha256": self._opening.sha256, "calendar_manifest_sha256": self._calendar.sha256,
                "listing_manifest_sha256": self._listings.sha256, "roster_source_hashes": roster["source_hashes"],
                "notices": events, "rule_documents": rule["documents"] if rule else [],
            },
        }

    def read_decision(
        self, symbol: str, start: str, end: str, *, as_of: str,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        """Require a complete, strictly earlier date window for reconstructed bars."""
        return self._read_completed_window("decision", symbol, start, end, as_of)

    def read_study_fixture_decision(
        self, symbol: str, start: str, end: str, *, as_of: str,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        """Expose synthetic bars after their explicitly simulated completion event."""
        clock, first, last = _clock(as_of), _day(start), _day(end)
        if first > last:
            raise ValueError("start must not follow end")
        ready = datetime.combine(last, STUDY_FIXTURE_COMPLETION, tzinfo=_SHANGHAI)
        if clock < ready:
            raise EvidenceUnavailable("synthetic bar has not reached its completion event")
        return self._read("decision", symbol, first, last, clock, _DAILY_FIELDS, fixture_only=True)

    def read_outcome(
        self, symbol: str, start: str, end: str, *, as_of: str,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        """A separate evaluation window, with the same date-only maturity bound."""
        return self._read_completed_window("outcome", symbol, start, end, as_of)

    def _read_completed_window(
        self, purpose: str, symbol: str, start: str, end: str, as_of: str,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        clock, first, last = _clock(as_of), _day(start), _day(end)
        if first > last:
            raise ValueError("start must not follow end")
        if last >= clock.date():
            if purpose == "outcome":
                raise EvidenceUnavailable("date-only outcome window is not mature at this cutoff")
            raise EvidenceUnavailable("date-only bars exclude the cutoff day and future")
        return self._read(purpose, symbol, first, last, clock, _DAILY_FIELDS)

    def read_opening_reference(
        self, symbol: str, session: str, *, as_of: str,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        """A price observation for the 09:30 daily proxy, not a tradeability check."""
        clock, day = _clock(as_of), _day(session)
        if clock < datetime.combine(day, time(9, 30), tzinfo=_SHANGHAI):
            raise EvidenceUnavailable("cannot read before the opening reference event")
        if self._opening is not None and self._mode == "reconstructed":
            _validate_symbol(symbol)
            events, _, roster, _ = self._opening_context(symbol, day)
            stop = self._known_opening_stop(events, roster)
            if stop:
                raise EvidenceUnavailable(f"opening reference blocked by a published {stop} control")
            if any(event["kind"] == "delisting_first" for event in events):
                raise EvidenceUnavailable("opening reference blocked by the unsupported unbounded session")
        frame, receipt = self._read("execution", symbol, day, day, clock, _OPEN_FIELDS)
        observed = frame["open"].item()
        try:
            price = float(observed)
        except (TypeError, ValueError, OverflowError) as exc:
            raise EvidenceUnavailable("opening reference price is missing or invalid") from exc
        if isinstance(observed, bool) or not math.isfinite(price) or price <= 0:
            raise EvidenceUnavailable("opening reference price is missing or invalid")
        return frame, receipt

    def _read(
        self, purpose: str, symbol: str, first: date, last: date,
        clock: datetime, fields: tuple[str, ...],
        *, fixture_only: bool = False,
    ) -> tuple[pd.DataFrame, ReadReceipt]:
        if self._mode != "reconstructed":
            raise EvidenceUnavailable("no approved historical-version proof")
        _validate_symbol(symbol)
        pin = self._bars.get(symbol)
        if pin is None:
            raise EvidenceUnavailable("instrument is not assigned in this cut")
        sessions = self._trading_days(first, last)
        frame, manifest = _dataset_frame(
            self._root, pin, "daily_bars", (*fields, "adjustflag")
        )
        meta = manifest["metadata"]
        if fixture_only and (manifest["source"] != STUDY_FIXTURE_SOURCE or snapshot_data_kind(meta) != "synthetic"):
            raise EvidenceUnavailable("same-day completion rule is available only to the synthetic study fixture")
        code, exchange = symbol.split(".")
        if not frame["code"].isin((symbol, f"{exchange.lower()}.{code}")).all():
            raise EvidenceUnavailable("daily input belongs to a different instrument")
        dates = _dates(frame["date"], "daily_bars")
        mask = dates.between(first, last)
        selected = frame.loc[mask]
        selected_dates = dates.loc[mask]
        if selected_dates.duplicated().any() or set(selected_dates) != sessions:
            raise EvidenceUnavailable("daily coverage disagrees with pinned trading sessions")
        if meta.get("adjustment") != "unadjusted" or not selected["adjustflag"].eq("3").all():
            raise EvidenceUnavailable("daily input is not confirmed unadjusted")
        if "volume" in fields and (
            meta.get("volume_unit"), meta.get("amount_unit")
        ) != ("share", "CNY"):
            raise EvidenceUnavailable("unverified daily volume or amount unit")
        view = selected.loc[:, list(fields)].sort_values("date").reset_index(drop=True)
        assert self._calendar is not None
        return view, ReadReceipt(
            self._cut_id, purpose, symbol, first.isoformat(), last.isoformat(), clock.isoformat(),
            manifest["source"], pin.sha256, self._calendar.sha256,
            snapshot_data_kind(meta),
        )
