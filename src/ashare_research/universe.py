"""Normalize archived exchange rosters for historical listing diagnostics."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .snapshots import freeze, snapshot_data_kind
from .sources import AcquiredData, EXCHANGE_ROSTERS


LISTING_FIELDS = ("symbol", "listed_on", "reported_terminal_on", "roster_scope", "source_sha256")
RISK_NAME_FIELDS = ("symbol", "session", "marker")
CALENDAR_AUDIT_METHOD = "exchange_notices_vs_sina_v1"
_CODE = re.compile(r"[0-9]{6}")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ROSTER_FIELDS = {
    "sse_listed_raw": ("证券代码", "上市日期", None, "SH", "sse_main_a"),
    "szse_listed_raw": ("A股代码", "A股上市日期", None, "SZ", "szse_roster"),
    "szse_delisted_raw": ("证券代码", "上市日期", "终止上市日期", "SZ", "szse_roster"),
}


def _file(path: Path, expected_hash: str | None = None) -> tuple[bytes, str]:
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError(f"archived evidence hash mismatch: {path.name}")
    return body, digest


def _local_reference(parent: Path, name: str) -> Path:
    path = (parent / name).resolve()
    if not path.is_relative_to(parent.resolve()):
        raise ValueError("evidence reference escapes its archive")
    return path


def _shanghai_day(stamp: str) -> date:
    observed = datetime.fromisoformat(stamp)
    if observed.utcoffset() is None:
        raise ValueError("evidence observation time requires a timezone")
    return observed.astimezone(_SHANGHAI).date()


def _verified_csv(path: Path, expected_hash: str) -> tuple[dict, list[dict], dict]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or ""):
        raise ValueError(f"missing archived manifest hash: {path.name}")
    body, manifest_hash = _file(path, expected_hash)
    manifest = json.loads(body)
    raw, digest = _file(_local_reference(path.parent, "raw.csv"), manifest["files"]["raw.csv"])
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    columns = reader.fieldnames
    if not columns or columns != manifest["columns"] or len(set(columns)) != len(columns):
        raise ValueError(f"archive header disagrees with its manifest: {path.parent.name}")
    rows = list(reader)
    if not rows or len(rows) != manifest["row_count"]:
        raise ValueError(f"archive row count mismatch: {path.parent.name}")
    return manifest, rows, {
        "manifest_sha256": manifest_hash, "sha256": digest, "observed_at": manifest["fetched_at"],
    }


def _listing_row(
    code: str, listed: str, terminal: str | None, exchange: str, scope: str, digest: str,
) -> dict:
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError("roster lacks a valid six-digit security code")
    start = date.fromisoformat(listed)
    end = date.fromisoformat(terminal) if terminal else None
    if end is not None and end <= start:
        raise ValueError(f"invalid listing dates for {code}.{exchange}")
    return {
        "symbol": f"{code}.{exchange}",
        "listed_on": start.isoformat(),
        "reported_terminal_on": end.isoformat() if end else None,
        "roster_scope": scope,
        "source_sha256": digest,
    }


def _sdk_rosters(report_path: Path) -> tuple[list[dict], list[dict]]:
    report, _ = _file(report_path)
    entries = json.loads(report)["rosters"]
    listings, sources = [], []
    for dataset, fields in _ROSTER_FIELDS.items():
        verified = [item for item in entries if item["dataset"] == dataset]
        if len(verified) != 1 or verified[0]["status"] != "verified_capture":
            raise ValueError(f"exactly one verified archive is required for {dataset}")
        record = verified[0]
        path = _local_reference(report_path.parent, record["manifest"])
        manifest, rows, lineage = _verified_csv(path, record["manifest_sha256"])
        meta, endpoint = manifest["metadata"], EXCHANGE_ROSTERS[dataset]
        if (
            manifest["dataset"] != dataset or manifest["source"] != f"akshare:{endpoint.exchange}"
            or meta.get("capture_level") != "sdk_values" or meta.get("endpoint") != endpoint.operation
            or meta.get("parameters") != {"symbol": endpoint.selector}
        ):
            raise ValueError(f"unexpected source contract for {dataset}")
        code, start, end, exchange, scope = fields
        listings.extend(
            _listing_row(row[code], row[start], row[end] if end else None, exchange, scope, lineage["sha256"])
            for row in rows
        )
        sources.append({"dataset": dataset, **lineage, "rows": len(rows)})
    return listings, sources


def _sse_terminal_rosters(report_path: Path) -> tuple[list[dict], list[dict]]:
    report, _ = _file(report_path)
    checks = json.loads(report)["checks"]
    listings, sources = [], []
    for kind, stock_type, code_column in (
        ("main_a_delisted", "1", "A_STOCK_CODE"),
        ("main_b_delisted", "2", "B_STOCK_CODE"),
    ):
        captures = [record for record in checks if record["name"] == kind]
        if len(captures) != 1:
            raise ValueError(f"exactly one raw exchange capture is required for {kind}")
        capture = captures[0]
        if (
            capture["status"] != "raw_captured" or capture.get("http_status") != 200
            or capture["parameters"]["STOCK_TYPE"] != stock_type
            or capture["parameters"]["COMPANY_STATUS"] != "3"
            or not capture.get("sha256")
        ):
            raise ValueError(f"unexpected SSE capture contract for {kind}")
        raw, digest = _file(
            _local_reference(report_path.parent, capture["file"]), capture["sha256"],
        )
        body = json.loads(raw)
        rows, page = body["result"], body["pageHelp"]
        if not rows or (
            int(page["pageNo"]), int(page["pageCount"]), int(page["total"])
        ) != (1, 1, len(rows)) or len(rows) != capture["rows"]:
            raise ValueError(f"incomplete exchange response for {kind}")
        scope = "sse_main_a" if stock_type == "1" else "sse_main_b"
        for row in rows:
            if row["STOCK_TYPE"] != stock_type or row["LIST_BOARD"] != "1":
                raise ValueError(f"wrong security class in {kind}")
            if not row["DELIST_DATE"]:
                raise ValueError(f"missing reported terminal date in {kind}")
            listings.append(_listing_row(
                row[code_column], row["LIST_DATE"], row["DELIST_DATE"], "SH", scope, digest,
            ))
        sources.append({
            "dataset": kind, "sha256": digest, "observed_at": capture["observed_at"],
            "url": capture["url"], "rows": len(rows),
        })
    return listings, sources


def _notice_index(parent: Path, name: str) -> tuple[dict, str]:
    capture_path = _local_reference(parent, name)
    manifest, capture_hash = _file(capture_path)
    documents = json.loads(manifest)["documents"]
    captures = {doc["id"]: doc for doc in documents}
    if len(captures) != len(documents):
        raise ValueError("notice archive contains duplicate document IDs")
    return captures, capture_hash


def _verified_notice(parent: Path, captures: dict, doc_id: str) -> dict:
    doc = captures[doc_id]
    if doc["status"] != "captured" or not doc.get("sha256") or not doc.get("publication_day"):
        raise ValueError(f"unavailable notice: {doc_id}")
    _, digest = _file(_local_reference(parent, doc["file"]), doc["sha256"])
    publication_day = date.fromisoformat(doc["publication_day"])
    if publication_day > _shanghai_day(doc["observed_at"]):
        raise ValueError("notice date follows capture")
    return {
        "source_sha256": digest, "source_url": doc["url"],
        "publication_day": publication_day.isoformat(),
        "not_before_day": (publication_day + timedelta(days=1)).isoformat(),
    }


def _notice_inputs(facts_path: Path) -> tuple[list[dict], dict, dict]:
    raw_facts, facts_hash = _file(facts_path)
    facts = json.loads(raw_facts)
    parent = facts_path.parent
    captures, capture_hash = _notice_index(parent, facts.get("capture_index", "captures.json"))

    events, delist_dates = [], {}
    for fact in facts["events"]:
        start = date.fromisoformat(fact["from"])
        end = date.fromisoformat(fact["until"]) if fact.get("until") else None
        kind = fact["kind"]
        if kind == "delisted":
            if end is not None:
                raise ValueError("delisting notice cannot carry a resumption date")
        elif kind not in {"suspended", "risk_warning"} or end is None or end <= start:
            raise ValueError("entry blocker requires a bounded evidence interval")
        if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", fact["symbol"]):
            raise ValueError("notice requires an exchange-qualified security code")
        if kind == "delisted" and delist_dates.setdefault(fact["symbol"], start) != start:
            raise ValueError(f"conflicting delisting notices for {fact['symbol']}")
        events.append({
            "symbol": fact["symbol"], "kind": kind, "from": start.isoformat(),
            "until": end.isoformat() if end else None,
            **_verified_notice(parent, captures, fact["notice"]),
        })
    board = facts["szse_main_a"]
    start = date.fromisoformat(board["effective_on"]).isoformat()
    if not _CODE.fullmatch(board["first"]) or not _CODE.fullmatch(board["last"]):
        raise ValueError("invalid SZSE code bounds")
    if board["first"] > board["last"]:
        raise ValueError("reversed SZSE code bounds")
    rule = {
        "effective_on": start, "first": board["first"], "last": board["last"],
        **_verified_notice(parent, captures, board["notice"]),
    }
    ranges = board.get("ranges")
    if ranges is not None:
        if not ranges:
            raise ValueError("the SZSE allowlist cannot be empty")
        for first, last in ranges:
            if (
                not _CODE.fullmatch(first) or not _CODE.fullmatch(last)
                or not board["first"] <= first <= last <= board["last"]
            ):
                raise ValueError("invalid SZSE allowlist interval")
        rule["ranges"] = ranges
        rule["allocation_review"] = _verified_notice(parent, captures, board["allocation_notice"])
    return events, rule, {"facts_sha256": facts_hash, "capture_index_sha256": capture_hash}


def build_listing_snapshot(
    roster_report: Path, sse_report: Path, notice_facts: Path, output: Path,
) -> Path:
    """Build an offline source index from exact archived bytes and reviewed notice facts."""
    listed, sdk_inputs = _sdk_rosters(roster_report)
    terminal, raw_inputs = _sse_terminal_rosters(sse_report)
    events, rule, notice_lineage = _notice_inputs(notice_facts)
    inputs = sdk_inputs + raw_inputs
    latest_exclusive = min(_shanghai_day(item["observed_at"]) for item in inputs)
    return freeze(AcquiredData(
        "listing_evidence", "exchange_rosters_and_notices",
        pd.DataFrame(listed + terminal, columns=LISTING_FIELDS),
        datetime.now(timezone.utc).isoformat(),
        {
            "data_kind": "market", "coverage_status": "not_audited", "membership_ready": False,
            "source_fallback": False, "source_inputs": inputs, "latest_session_exclusive": latest_exclusive.isoformat(),
            "notice_events": events, "szse_main_a": rule, "notice_lineage": notice_lineage,
            "notice_availability_policy": "next_shanghai_calendar_day",
        },
    ), output)


def _dates_inclusive(first: date, last: date):
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


def build_audited_calendar(
    calendar_report: Path, closure_facts: Path, output: Path,
) -> Path:
    """Publish observed sessions only when both venue schedules reconcile day by day."""
    report_body, report_hash = _file(calendar_report)
    report = json.loads(report_body)
    if report["status"] != "raw_captured" or report["endpoint"] != "tool_trade_date_hist_sina":
        raise ValueError("an archived Sina trading date report is required")
    manifest, rows, lineage = _verified_csv(
        _local_reference(calendar_report.parent, report["manifest"]), report["manifest_sha256"],
    )
    if (
        manifest["dataset"] != "sina_trading_dates_raw" or manifest["source"] != "akshare:sina"
        or manifest["columns"] != ["trade_date"] or report["rows"] != len(rows)
    ):
        raise ValueError("unexpected trading date source contract")
    observed = {date.fromisoformat(row["trade_date"]) for row in rows}
    if len(observed) != len(rows):
        raise ValueError("duplicate observed trading dates")
    facts_body, facts_hash = _file(closure_facts)
    closures = json.loads(facts_body)["closures"]
    years = sorted(int(year) for year in closures)
    if not years or years != list(range(years[0], years[-1] + 1)):
        raise ValueError("closure schedules must cover consecutive years")
    capture_body, capture_hash = _file(closure_facts.with_name("captures.json"))
    documents = json.loads(capture_body)["documents"]
    publications = []
    for year in years:
        for venue, host in (("SH", "www.sse.com.cn"), ("SZ", "www.szse.cn")):
            docs = [doc for doc in documents if (doc["year"], doc["venue"]) == (year, venue)]
            if len(docs) != 1:
                raise ValueError(f"exactly one {venue} schedule is required for {year}")
            doc = docs[0]
            if (
                doc["status"] != "captured" or doc["http_status"] != 200
                or not doc.get("sha256") or not doc["url"].startswith(f"https://{host}/")
            ):
                raise ValueError(f"invalid {venue} schedule capture for {year}")
            _file(_local_reference(closure_facts.parent, doc["file"]), doc["sha256"])
            publications.append({"year": year, "venue": venue, "url": doc["url"], "sha256": doc["sha256"]})

    first = date(years[0], 1, 1)
    last = min(date(years[-1], 12, 31), _shanghai_day(lineage["observed_at"]) - timedelta(days=1))
    if last < first:
        raise ValueError("no completed calendar dates in the requested window")
    closed = set()
    for year in years:
        periods = closures[str(year)]
        if not periods:
            raise ValueError(f"missing closure dates for {year}")
        for a, b in periods:
            start, end = date.fromisoformat(a), date.fromisoformat(b)
            if start > end or end.year != year or start.year < year - 1:
                raise ValueError(f"invalid closure interval for {year}")
            closed.update(_dates_inclusive(start, end))
    days = tuple(_dates_inclusive(first, last))
    differences = [
        day.isoformat() for day in days
        if (day in observed) != (day.weekday() < 5 and day not in closed)
    ]
    if differences:
        raise ValueError("calendar sources disagree on: " + ", ".join(differences[:12]))
    frame = pd.DataFrame({
        "calendar_date": [day.isoformat() for day in days],
        "is_trading_day": ["1" if day in observed else "0" for day in days],
    })
    return freeze(AcquiredData(
        "trading_calendar", "sina_reconciled_with_sh_sz_notices", frame,
        datetime.now(timezone.utc).isoformat(),
        {
            "calendar_audit_method": CALENDAR_AUDIT_METHOD,
            "first_date": first.isoformat(), "last_date": last.isoformat(),
            "sessions": int(frame["is_trading_day"].eq("1").sum()),
            "source_fallback": False, "data_kind": "market",
            "observed_dates": {**lineage, "report_sha256": report_hash},
            "notice_facts_sha256": facts_hash, "notice_index_sha256": capture_hash,
            "exchange_notices": publications,
        },
    ), output)


@dataclass(frozen=True, slots=True)
class ListingAgeWindow:
    minimum: int
    first_date: date
    trading_dates: tuple[date, ...]

    def assess(self, listed_on: str) -> dict:
        listed = date.fromisoformat(listed_on)
        offset = bisect_left(self.trading_dates, listed)
        exact = listed >= self.first_date
        if exact and (offset == len(self.trading_dates) or self.trading_dates[offset] != listed):
            return {"minimum": self.minimum, "sessions": None, "basis": "unknown", "state": "unknown"}
        count = len(self.trading_dates) - offset
        state = "met" if count >= self.minimum else ("too_new" if exact else "unknown")
        return {
            "minimum": self.minimum, "sessions": count, "basis": "exact" if exact else "lower_bound",
            "state": state,
        }


def _normalized_name(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name).split()).upper()


def build_risk_name_snapshot(status_report: Path, output: Path) -> Path:
    """Preserve positive risk markers on observed exchange change dates."""
    body, report_hash = _file(status_report)
    report = json.loads(body)
    if report["status"] != "raw_captured" or report["endpoint"] != "stock_info_sz_change_name":
        raise ValueError("an archived SZSE name change report is required")
    manifest, rows, lineage = _verified_csv(
        _local_reference(status_report.parent, report["manifest"]), report["manifest_sha256"],
    )
    if (
        manifest["dataset"] != "szse_short_name_changes_raw" or manifest["source"] != "akshare:szse"
        or manifest["metadata"].get("parameters") != {"symbol": "简称变更"}
        or len(rows) != report["rows"]
    ):
        raise ValueError("unexpected SZSE name change source contract")
    observed_on = _shanghai_day(lineage["observed_at"])
    changes: dict[tuple[str, date], tuple[str, str]] = {}
    for row in rows:
        day = datetime.fromisoformat(row["变更日期"]).date()
        if day < date(2021, 1, 1):
            continue
        code = row["证券代码"]
        if not _CODE.fullmatch(code) or day >= observed_on:
            raise ValueError("invalid or unobserved name change date")
        before, after = (_normalized_name(row[key]) for key in ("变更前简称", "变更后简称"))
        if not before or not after:
            raise ValueError("incomplete name change record")
        key, names = (code, day), (before, after)
        if changes.setdefault(key, names) != names:
            raise ValueError(f"ambiguous name changes for {code}.SZ on {day}")
    points = []
    prior_names: dict[str, str] = {}
    breaks = 0
    for (code, day), (before, after) in sorted(changes.items()):
        if code in prior_names and prior_names[code] != before:
            breaks += 1
        prior_names[code] = after
        marker = re.match(r"^(\*ST|ST).+", after)
        if marker:
            points.append((f"{code}.SZ", day.isoformat(), marker.group(1)))
    if not points:
        raise ValueError("no observed risk-marked names in the study window")
    return freeze(AcquiredData(
        "risk_name_events", "szse_short_name_change_points",
        pd.DataFrame(points, columns=RISK_NAME_FIELDS),
        datetime.now(timezone.utc).isoformat(),
        {
            "data_kind": "market", "source_fallback": False, "coverage_status": "not_audited",
            "observed_change_rows": len(changes), "observed_codes": len(prior_names),
            "chain_breaks": breaks, "first_study_date": "2021-01-01",
            "source_input": {**lineage, "report_sha256": report_hash},
            "marker_definition": "https://www.szse.cn/www/investor/knowledge/t20220414_592386.html",
            "availability_policy": "exact_session_before_cutoff_day",
        },
    ), output)


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    source: str
    evidence_sha256: str
    risk_warning: bool | None = None
    normal_trading: bool | None = None
    data_kind: str = "unspecified"


def _state(values: set[bool], positive: str, negative: str) -> str:
    if len(values) > 1:
        return "conflict"
    if True in values:
        return positive
    return negative if False in values else "unknown"


def _session_status(observations: list[SessionEvidence]) -> dict:
    risk = {item.risk_warning for item in observations if item.risk_warning is not None}
    trading = {item.normal_trading for item in observations if item.normal_trading is not None}
    kinds = {item.data_kind for item in observations}
    data_kind = "synthetic" if "synthetic" in kinds else ("market" if kinds == {"market"} else "unspecified")
    return {
        "risk_warning": _state(risk, "active", "clear"),
        "trading": _state(trading, "normal", "suspended"),
        "data_kind": data_kind,
        "evidence": [asdict(item) for item in observations],
    }


def _board_scope(row: dict, rule: dict, session: str, cutoff_day: str) -> str:
    scope = row["roster_scope"]
    if scope == "sse_main_a":
        return "main_a"
    if scope == "sse_main_b":
        return "outside_main_a"
    code = row["symbol"].split(".")[0]
    ranges = rule.get("ranges")
    if ranges is None:
        # The earlier broad range did not separate the reserved/CDR subrange.
        if "001000" <= code <= "001199":
            return "unknown"
        ranges = ((rule["first"], rule["last"]),)
    if (
        scope == "szse_roster" and rule["not_before_day"] <= cutoff_day
        and rule["effective_on"] <= session
        and any(first <= code <= last for first, last in ranges)
    ):
        return "main_a"
    return "unknown"


def project_listings(
    frame: pd.DataFrame, meta: dict, session: str, cutoff_day: str, symbols: tuple[str, ...],
    age_window: ListingAgeWindow | None = None,
    session_observations: dict[str, list[SessionEvidence]] | None = None,
) -> pd.DataFrame:
    """Project active evidence through the caller's L1 cutoff."""
    earlier = frame[frame["listed_on"] <= session]
    if symbols:
        earlier = earlier[earlier["symbol"].isin(symbols)]
    by_symbol = {symbol: group.to_dict("records") for symbol, group in earlier.groupby("symbol")}
    active_notices: dict[str, list[dict]] = {}
    for event in meta["notice_events"]:
        if (
            event["not_before_day"] <= cutoff_day and event["from"] <= session
            and (event["until"] is None or session < event["until"])
        ):
            active_notices.setdefault(event["symbol"], []).append(event)

    results = []
    for symbol in symbols or tuple(sorted(by_symbol)):
        records = by_symbol.get(symbol, [])
        row = records[0] if len(records) == 1 else None
        listing = "unknown"
        board = "unknown"
        reasons = []
        refs = []
        if row is None:
            reasons.append("ambiguous_listing_history" if records else "no_observed_listing")
        else:
            refs.append(row["source_sha256"])
            board = _board_scope(row, meta["szse_main_a"], session, cutoff_day)
            if board == "main_a" and row["roster_scope"] == "szse_roster":
                refs.append(meta["szse_main_a"]["source_sha256"])
            ends = [
                event for event in active_notices.get(symbol, [])
                if event["kind"] == "delisted" and row["listed_on"] <= event["from"]
            ]
            if ends:
                listing = "delisted"
                reasons.append("announced_delisting_effective")
                refs.extend(event["source_sha256"] for event in ends)
            elif pd.notna(row["reported_terminal_on"]) and row["reported_terminal_on"] <= session:
                reasons.append("reported_terminal_date_unconfirmed")
            else:
                listing = "candidate"
        blockers = [
            event for event in active_notices.get(symbol, [])
            if event["kind"] != "delisted" and row is not None and row["listed_on"] <= event["from"]
        ]
        reasons.extend(event["kind"] for event in blockers)
        refs.extend(event["source_sha256"] for event in blockers)
        if board == "outside_main_a":
            reasons.append("outside_main_a")
        excluded = listing == "delisted" or board == "outside_main_a" or bool(blockers)
        age = None
        if age_window is not None and listing == "candidate" and board == "main_a" and row is not None:
            age = age_window.assess(row["listed_on"])
            if age["state"] == "too_new":
                excluded = True
                reasons.append("listed_sessions_below_minimum")
        status = None
        if session_observations is not None:
            observations = list(session_observations.get(symbol, ())) if listing == "candidate" else []
            observations.extend(SessionEvidence(
                "published_notice", event["source_sha256"],
                risk_warning=True if event["kind"] == "risk_warning" else None,
                normal_trading=False if event["kind"] == "suspended" else None,
                data_kind=snapshot_data_kind(meta),
            ) for event in blockers)
            refs.extend(item.evidence_sha256 for item in observations)
            status = _session_status(observations)
            if "conflict" in (status["risk_warning"], status["trading"]):
                reasons.append("session_status_conflict")
            if status["risk_warning"] == "active":
                excluded = True
                if "risk_warning" not in reasons:
                    reasons.append("session_risk_warning")
            if status["trading"] == "suspended":
                excluded = True
                if "suspended" not in reasons:
                    reasons.append("session_suspended")
        status_clear = status is not None and status["risk_warning"] == "clear" and status["trading"] == "normal"
        eligible = (
            not excluded and listing == "candidate" and board == "main_a" and status_clear
            and age is not None and age["state"] == "met"
        )
        if not excluded and listing == "candidate":
            if board == "unknown":
                reasons.append("historical_board_unverified")
            if not status_clear:
                reasons.append("risk_and_tradability_coverage_missing")
            if age is None or age["state"] == "unknown":
                reasons.append("listing_age_unverified")
        record = {
            "symbol": symbol, "listing_state": listing, "board_scope": board,
            "entry_state": "excluded" if excluded else ("eligible" if eligible else "unknown"),
            "reasons": sorted(set(reasons)), "source_hashes": sorted(set(refs)),
        }
        if age_window is not None:
            record["listing_age"] = age
        if status is not None:
            record["session_status"] = status
        results.append(record)
    columns = (
        "symbol", "listing_state", "board_scope", "entry_state", "reasons", "source_hashes",
    )
    if age_window is not None:
        columns += ("listing_age",)
    if session_observations is not None:
        columns += ("session_status",)
    return pd.DataFrame(results, columns=columns)
