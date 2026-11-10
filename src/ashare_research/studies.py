"""Freeze research settings and register each attempt before evidence reads."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


PARTITIONS = ("development", "validation", "holdout")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SPEC_FIELDS = set("""
    version study profile hypothesis strategy visibility universe symbols min_listed_sessions history_start
    partitions cutoff lookback min_return_bp min_volume_multiple holding_choices entry_limit_buffer_bp
    initial_cash_cny order_lot_shares fee_scenario control metric assumptions
""".split())


def canonical(document: dict) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(document: dict) -> str:
    return sha256(canonical(document)).hexdigest()


def load_document(path: Path) -> dict:
    def unique(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError("duplicate JSON property")
        return obj

    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def _members(value, required: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{name} requires exactly {', '.join(sorted(required))}")


def _day(value) -> date:
    if not isinstance(value, str):
        raise ValueError("expected an ISO calendar date")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("expected an ISO calendar date")
    return day


def _integer(value, first: int, last: int, name: str) -> None:
    if type(value) is not int or not first <= value <= last:
        raise ValueError(f"{name} is outside its supported range")


def _decimal(value, name: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,8})?", value):
        raise ValueError(f"{name} requires an unsigned decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc


def validate_spec(spec: dict) -> None:
    _members(spec, _SPEC_FIELDS, "research spec")
    if type(spec["version"]) is not int or spec["version"] != 1:
        raise ValueError("unsupported research spec version")
    if not isinstance(spec["study"], str) or not _KEY.fullmatch(spec["study"]):
        raise ValueError("invalid study key")
    if spec["profile"] not in ("synthetic", "market_candidate"):
        raise ValueError("unsupported research profile")
    if not isinstance(spec["hypothesis"], str) or not spec["hypothesis"].strip():
        raise ValueError("a testable hypothesis is required")
    if spec["strategy"] != "volume_confirmed_close_up_v1" or spec["visibility"] != "reconstructed":
        raise ValueError("unsupported strategy or visibility profile")
    if spec["universe"] != "sse_szse_main_a_ex_st":
        raise ValueError("unsupported universe policy")
    symbols = spec["symbols"]
    if not isinstance(symbols, list) or not symbols or any(
        not isinstance(s, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", s) for s in symbols
    ) or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must contain distinct exchange-qualified instruments")
    _integer(spec["min_listed_sessions"], 1, 300, "minimum listing age")
    _members(spec["partitions"], set(PARTITIONS), "partitions")
    end = _day(spec["history_start"])
    for name in PARTITIONS:
        window = spec["partitions"][name]
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("each partition requires inclusive first and last dates")
        first, last = map(_day, window)
        if not end < first <= last:
            raise ValueError("history and study partitions must be ordered and disjoint")
        end = last
    if spec["cutoff"] != "18:00:00":
        raise ValueError("v1 uses an explicit 18:00 Shanghai decision cutoff")
    _integer(spec["lookback"], 2, 250, "lookback")
    _integer(spec["min_return_bp"], 1, 1000, "return threshold")
    if _decimal(spec["min_volume_multiple"], "volume multiple") <= 1:
        raise ValueError("volume multiple must exceed one")
    choices = spec["holding_choices"]
    if not isinstance(choices, list) or not choices or any(type(h) is not int or not 1 <= h <= 5 for h in choices):
        raise ValueError("holding choices must be in the one-to-five session range")
    if choices != sorted(set(choices)):
        raise ValueError("holding choices must be unique and ordered")
    _integer(spec["entry_limit_buffer_bp"], 0, 1000, "entry limit buffer")
    _integer(spec["order_lot_shares"], 100, 100000, "entry shares")
    if spec["order_lot_shares"] % 100 or not _decimal(spec["initial_cash_cny"], "initial cash"):
        raise ValueError("positive cash and whole board lots are required")
    fees = spec["fee_scenario"]
    _members(fees, {"commission_rate", "min_commission", "stamp_tax_rate", "transfer_fee_rate"}, "fee scenario")
    rates = [_decimal(value, name) for name, value in fees.items() if name.endswith("_rate")]
    _decimal(fees["min_commission"], "minimum commission")
    if sum(rates) >= 1:
        raise ValueError("variable fee rates are excessive")
    _members(spec["control"], {"name", "seed"}, "control")
    if spec["control"]["name"] != "opportunity_matched_eligible_v1":
        raise ValueError("unsupported comparison control")
    _integer(spec["control"]["seed"], 0, 2**31 - 1, "comparison seed")
    _members(spec["metric"], {"name", "min_qualifying_sessions", "min_net_excess_bp"}, "evaluation metric")
    if spec["metric"]["name"] != "terminal_net_excess_bp":
        raise ValueError("unsupported primary metric")
    _integer(spec["metric"]["min_qualifying_sessions"], 1, 2000, "evidence minimum")
    _integer(spec["metric"]["min_net_excess_bp"], 0, 10000, "excess threshold")
    assumptions = spec["assumptions"]
    if not isinstance(assumptions, list) or not assumptions or any(
        not isinstance(item, str) or not item.strip() for item in assumptions
    ):
        raise ValueError("record assumptions explicitly before freezing")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class TrialBook:
    """Local SQLite journal. Request, execution attempt and outcome are separate records."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS specs (
                    spec_id TEXT PRIMARY KEY, body BLOB NOT NULL, frozen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, body BLOB NOT NULL, spec_id TEXT NOT NULL REFERENCES specs(spec_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
                    started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                    status TEXT NOT NULL CHECK(status IN ('complete','blocked','failed')),
                    payload BLOB NOT NULL, finished_at TEXT NOT NULL
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def freeze(self, spec: dict) -> str:
        validate_spec(spec)
        spec_id = digest(spec)
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO specs VALUES (?, ?, ?)", (spec_id, canonical(spec), _utc()))
            row = db.execute("SELECT body FROM specs WHERE spec_id=?", (spec_id,)).fetchone()
            if row[0] != canonical(spec):
                raise ValueError("stored study content disagrees with its ID")
        return spec_id

    def spec(self, spec_id: str) -> dict:
        if not isinstance(spec_id, str) or not _DIGEST.fullmatch(spec_id):
            raise ValueError("invalid study ID")
        with closing(self._connect()) as db:
            row = db.execute("SELECT body FROM specs WHERE spec_id=?", (spec_id,)).fetchone()
        if row is None or sha256(row[0]).hexdigest() != spec_id:
            raise ValueError("frozen study is missing or damaged")
        spec = json.loads(row[0])
        validate_spec(spec)
        return spec

    def begin(self, request_id: str, payload: dict, *, retry: bool = False) -> dict:
        if not isinstance(request_id, str) or not _KEY.fullmatch(request_id):
            raise ValueError("request ID must be a short stable key")
        body = canonical(payload)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT body FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if existing and existing[0] != body:
                raise ValueError("request ID is already bound to different inputs")
            if existing:
                last = db.execute("""
                    SELECT a.attempt_id, o.status, o.payload FROM attempts a
                    LEFT JOIN outcomes o USING(attempt_id)
                    WHERE a.request_id=? ORDER BY a.rowid DESC LIMIT 1
                """, (request_id,)).fetchone()
                if not retry or last[1] not in ("blocked", "failed"):
                    outcome = json.loads(last[2]) if last[2] else {}
                    return {**outcome, "attempt_id": last[0], "status": last[1] or "running_or_interrupted",
                            "reused": True}
            else:
                db.execute("INSERT INTO requests VALUES (?, ?, ?)", (request_id, body, payload["spec_id"]))
            attempt = uuid4().hex
            db.execute("INSERT INTO attempts VALUES (?, ?, ?)", (attempt, request_id, _utc()))
            return {"attempt_id": attempt, "status": "started", "reused": False}

    def finish(self, attempt_id: str, status: str, payload: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?)", (attempt_id, status, canonical(payload), _utc()))
