"""Reviewed exchange rules and notice facts for L1 opening controls."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from .snapshots import freeze
from .sources import AcquiredData, bs_symbol
from .universe import _file, _notice_index, _verified_notice


OPENING_SOURCE = "reviewed_opening_notices_and_rules"
OPENING_FIELDS = (
    "symbol", "kind", "from", "until", "record_on", "cash_per_share",
    "source_sha256", "source_url", "publication_day", "not_before_day",
)
_KINDS = {"suspended", "delisting_first", "delisting_later", "cash_dividend"}
_RATES = {"ordinary_mainboard", "risk_warning", "delisting_later"}
_TICK = Decimal("0.01")


def positive_decimal(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,8})?", value):
        raise ValueError("expected a positive decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal value") from exc
    if parsed <= 0:
        raise ValueError("expected a positive decimal value")
    return parsed


def cash_ex_reference(previous_close: str, cash_per_share: str) -> str:
    close = positive_decimal(previous_close)
    if close % _TICK:
        raise ValueError("previous A-share close is off tick")
    adjusted = (close - positive_decimal(cash_per_share)).quantize(_TICK, rounding=ROUND_HALF_UP)
    if adjusted <= 0:
        raise ValueError("cash-only adjustment does not produce a positive reference")
    return str(adjusted)


def price_bands(reference: str, ratio: str) -> dict[str, str]:
    price, rate = positive_decimal(reference), positive_decimal(ratio)
    if price % _TICK or rate >= 1:
        raise ValueError("invalid A-share reference price or limit ratio")
    high = (price * (1 + rate)).quantize(_TICK, rounding=ROUND_HALF_UP)
    low = (price * (1 - rate)).quantize(_TICK, rounding=ROUND_HALF_UP)
    return {"lower": str(max(_TICK, min(low, price - _TICK))), "upper": str(max(high, price + _TICK))}


def build_opening_snapshot(review_path: Path, output: Path) -> Path:
    """Freeze only curated, hash-checked notices and dated mainboard rule releases."""
    raw, review_hash = _file(review_path)
    review = json.loads(raw)
    parent = review_path.parent
    captures, index_hash = _notice_index(parent, review["capture_index"])
    rules = []
    for rule in review["rules"]:
        start, end = date.fromisoformat(rule["from"]), date.fromisoformat(rule["until"])
        if rule["venue"] not in {"SH", "SZ"} or end <= start or not rule["id"]:
            raise ValueError("invalid mainboard rule scope")
        if set(rule["ratios"]) != _RATES or any(positive_decimal(rate) >= 1 for rate in rule["ratios"].values()):
            raise ValueError("incomplete mainboard price-limit ratios")
        documents = [_verified_notice(parent, captures, key) for key in rule["documents"]]
        if not documents:
            raise ValueError("rule release requires published documents")
        visible_from = max(item["not_before_day"] for item in documents)
        if visible_from > start.isoformat():
            raise ValueError("rule effective date precedes available evidence")
        rules.append({
            "id": rule["id"], "venue": rule["venue"], "from": start.isoformat(), "until": end.isoformat(),
            "not_before_day": visible_from, "ratios": rule["ratios"], "documents": documents,
        })
    if not rules or len({rule["id"] for rule in rules}) != len(rules):
        raise ValueError("missing or duplicate mainboard rule releases")
    for venue in ("SH", "SZ"):
        ranges = sorted((r["from"], r["until"]) for r in rules if r["venue"] == venue)
        if any(old[1] > new[0] for old, new in zip(ranges, ranges[1:])):
            raise ValueError("overlapping mainboard rule releases")

    rows = []
    for event in review["events"]:
        bs_symbol(event["symbol"])
        start, end = date.fromisoformat(event["from"]), date.fromisoformat(event["until"])
        kind = event["kind"]
        if kind not in _KINDS or end <= start:
            raise ValueError("invalid opening notice interval")
        record_on, cash = event.get("record_on", ""), event.get("cash_per_share", "")
        if kind == "cash_dividend":
            record_day = date.fromisoformat(record_on)
            positive_decimal(cash)
            if record_day >= start or end != start + timedelta(days=1):
                raise ValueError("cash-only adjustment requires a single ex-date after record date")
            record_on = record_day.isoformat()
        elif record_on or cash:
            raise ValueError("cash terms are only allowed on a cash dividend notice")
        if kind == "delisting_first" and end != start + timedelta(days=1):
            raise ValueError("delisting first session must be a single date")
        rows.append({
            "symbol": event["symbol"], "kind": kind, "from": start.isoformat(), "until": end.isoformat(),
            "record_on": record_on, "cash_per_share": cash,
            **_verified_notice(parent, captures, event["notice"]),
        })
    if not rows:
        raise ValueError("opening evidence requires reviewed notice facts")
    return freeze(AcquiredData(
        "opening_evidence", OPENING_SOURCE, pd.DataFrame(rows, columns=OPENING_FIELDS),
        datetime.now(timezone.utc).isoformat(),
        {
            "data_kind": "market", "coverage_status": "not_audited", "source_fallback": False,
            "availability_policy": "next_shanghai_calendar_day",
            "rules": rules, "review_sha256": review_hash, "capture_index_sha256": index_hash,
        },
    ), output)


def known_opening_rule(meta: dict, venue: str, day: str, cutoff_day: str) -> dict | None:
    valid = [
        rule for rule in meta["rules"]
        if rule["venue"] == venue and rule["from"] <= day < rule["until"] and rule["not_before_day"] <= cutoff_day
    ]
    if len(valid) > 1:
        raise ValueError("ambiguous mainboard rule on the requested date")
    return valid[0] if valid else None
