"""Run a frozen volume study on fixture data or diagnose a market candidate."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd

from .opening import price_bands
from .pit import STUDY_FIXTURE_SOURCE, DailyPIT, EvidenceUnavailable, ReadReceipt, freeze_daily_cut
from .snapshots import freeze, sha256
from .sources import AcquiredData
from .studies import PARTITIONS, TrialBook, digest, validate_spec

if TYPE_CHECKING:
    from .account import AccountQuote, AccountRun


_CENT = Decimal("0.01")
_DEPENDENCIES = ("akquant", "numpy", "pandas", "polars", "pyarrow", "tzdata")


def runtime_fingerprint() -> dict:
    package = Path(__file__).parent
    files = {path.relative_to(package).as_posix(): sha256(path)
             for path in sorted(package.rglob("*.py"))}
    return {
        "source_tree_sha256": digest(files), "python": sys.version, "platform": sys.platform,
        "dependencies": {name: version(name) for name in _DEPENDENCIES},
    }


def make_study_fixture(spec: dict, snapshots: Path, cuts: Path) -> str:
    """Publish a synthetic calendar and bars through the validation partition only."""
    validate_spec(spec)
    if spec["profile"] != "synthetic":
        raise ValueError("only synthetic specifications can publish a study fixture")
    first = date.fromisoformat(spec["history_start"])
    last = date.fromisoformat(spec["partitions"]["validation"][1])
    if (last - first).days >= 366 or len(spec["symbols"]) > 12:
        raise ValueError("the fixture generator supports up to 366 dates and 12 instruments")
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    sessions = [day for day in days if day.weekday() < 5]
    captured = datetime.now(timezone.utc).isoformat()
    meta = {"data_kind": "synthetic", "capture_level": "generated_fixture"}
    calendar = freeze(AcquiredData("trading_calendar", STUDY_FIXTURE_SOURCE, pd.DataFrame({
        "calendar_date": [day.isoformat() for day in days],
        "is_trading_day": ["1" if day.weekday() < 5 else "0" for day in days],
    }), captured, meta), snapshots)
    bars = {}
    for offset, symbol in enumerate(sorted(spec["symbols"])):
        rows = []
        prior = 1000 + 200 * offset
        for i, day in enumerate(sessions):
            spike = (i + 2 * offset) % 7 == 3
            close = 1000 + 200 * offset + i * 2 + (65 if spike else 0)
            opening = prior + (i + offset) % 3 - 1
            volume = 3000 if spike else 1000 + (i % 5) * 10
            rows.append({
                "date": day.isoformat(), "code": symbol,
                "open": str(opening * _CENT), "high": str((max(opening, close) + 1) * _CENT),
                "low": str((min(opening, close) - 1) * _CENT), "close": str(close * _CENT),
                "volume": str(volume), "amount": str(volume * close * _CENT), "adjustflag": "3",
            })
            prior = close
        bars[symbol] = freeze(AcquiredData(
            "daily_bars", STUDY_FIXTURE_SOURCE, pd.DataFrame(rows), captured,
            {**meta, "adjustment": "unadjusted", "volume_unit": "share", "amount_unit": "CNY"},
        ), snapshots)
    return freeze_daily_cut(snapshots, cuts, calendar, bars)


def _positive(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvidenceUnavailable(f"{field} is missing or invalid") from exc
    if not number.is_finite() or number <= 0:
        raise EvidenceUnavailable(f"{field} must be finite and positive")
    return number


def _scan(gate: DailyPIT, spec: dict, first: date, day: date) -> dict:
    as_of = f"{day}T{spec['cutoff']}+08:00"
    candidates, receipts = [], []
    for symbol in sorted(spec["symbols"]):
        try:
            frame, receipt = gate.read_study_fixture_decision(
                symbol, first.isoformat(), day.isoformat(), as_of=as_of,
            )
            if len(frame) != spec["lookback"] + 1:
                raise EvidenceUnavailable("trailing window is incomplete")
            prior, close = (_positive(value, "close") for value in frame["close"].iloc[-2:])
            volume = [_positive(value, "volume") for value in frame["volume"]]
            change = (close - prior) * 10000 / prior
            multiple = volume[-1] * spec["lookback"] / sum(volume[:-1])
            if close % _CENT:
                raise EvidenceUnavailable("close does not match the fixture price tick")
        except EvidenceUnavailable as exc:
            raise EvidenceUnavailable(f"{symbol} at {as_of}: {exc}") from exc
        candidates.append({
            "symbol": symbol, "close_cny": str(close), "close_change_bp": str(change),
            "volume_multiple": str(multiple),
            "rule_match": change >= spec["min_return_bp"] and multiple >= Decimal(spec["min_volume_multiple"]),
        })
        receipts.append(asdict(receipt))
    matched = sorted(
        (row for row in candidates if row["rule_match"]),
        key=lambda row: (-Decimal(row["volume_multiple"]), -Decimal(row["close_change_bp"]), row["symbol"]),
    )
    return {
        "session": day.isoformat(), "as_of": as_of, "candidates": candidates,
        "selection": matched[0]["symbol"] if matched else None, "decision_receipts": receipts,
    }


def _check_fixture_receipt(receipt: ReadReceipt) -> None:
    if (receipt.source, receipt.data_kind) != (STUDY_FIXTURE_SOURCE, "synthetic"):
        raise EvidenceUnavailable("synthetic account inputs must use the study fixture")


def _fixture_quotes(
    gate: DailyPIT, history: tuple[date, ...], first: int, symbols: list[str],
) -> tuple[list[AccountQuote], list[dict]]:
    from .account import AccountQuote
    from .engine import OpenEvent

    quotes, receipts = [], []
    # Entry cannot occur on the first session of a partition.
    for i in range(first + 1, len(history)):
        prior, day = history[i - 1].isoformat(), history[i].isoformat()
        known_at = f"{day}T09:00:00+08:00"
        for symbol in symbols:
            reference, reference_receipt = gate.read_study_fixture_decision(
                symbol, prior, prior, as_of=known_at,
            )
            opening, opening_receipt = gate.read_opening_reference(symbol, day, as_of=f"{day}T09:30:00+08:00")
            _check_fixture_receipt(opening_receipt)
            band = price_bands(reference["close"].item(), "0.10")
            open_price = float(opening["open"].item())
            event = OpenEvent(history[i], symbol, open_price, known_at, True,
                              float(band["upper"]), float(band["lower"]))
            quotes.append(AccountQuote(event, open_price))
            receipts.append({"reference": asdict(reference_receipt), "open": asdict(opening_receipt)})
    return quotes, receipts


def _evaluation(spec: dict, strategy: AccountRun, control: AccountRun) -> dict:
    entry_days = [
        {row["session"] for row in path.orders if row["side"] == "buy" and row["filled"] > 0}
        for path in (strategy, control)
    ]
    paired = len(entry_days[0] & entry_days[1])
    evaluation = {
        "metric": spec["metric"]["name"], "paired_filled_entry_sessions": paired,
        "strategy_filled_entry_sessions": len(entry_days[0]), "control_filled_entry_sessions": len(entry_days[1]),
    }
    for path in (strategy, control):
        if not path.metadata["valuation_complete"]:
            return {**evaluation, "state": "unavailable", "reason": "opening_marks_incomplete"}
        if path.metadata["open_positions_at_end"]:
            return {**evaluation, "state": "unavailable", "reason": "unresolved_exits_at_partition_end"}
    initial = Decimal(spec["initial_cash_cny"])
    final, baseline = (Decimal(path.accounts[-1]["opening_equity_cny"]) for path in (strategy, control))
    excess = (final - baseline) * 10000 / initial
    if paired < spec["metric"]["min_qualifying_sessions"]:
        state = "insufficient_fixture_observations"
    else:
        state = "passes_fixture_threshold" if excess >= spec["metric"]["min_net_excess_bp"] else "below_fixture_threshold"
    return {
        **evaluation, "state": state, "strategy_terminal_cny": str(final), "control_terminal_cny": str(baseline),
        "strategy_net_pnl_cny": str(final - initial), "control_net_pnl_cny": str(baseline - initial),
        "terminal_net_excess_bp": str(excess),
    }


def _synthetic_run(gate: DailyPIT, spec: dict, first: str, last: str, holding: int) -> dict:
    from .account import EntryPlan, FeePeriod, run_account_path
    from .engine import FrozenIntent

    history = gate.read_trading_sessions(spec["history_start"], last)
    days = [day for day in history if day >= date.fromisoformat(first)]
    if not days:
        raise EvidenceUnavailable("partition contains no trading sessions")
    phase_start = len(history) - len(days)
    scan_count = max(0, len(days) - holding - 1)
    if scan_count and phase_start < spec["lookback"]:
        raise EvidenceUnavailable("the partition lacks the registered pre-signal history window")
    plans = {"strategy": [], "control": []}
    scans = []
    for i, day in enumerate(days[:scan_count]):
        scan = _scan(gate, spec, history[phase_start + i - spec["lookback"]], day)
        scans.append(scan)
        if scan["selection"] is None:
            scan["control_selection"] = None
            continue
        candidates = {row["symbol"]: row for row in scan["candidates"]}
        seed = spec["control"]["seed"]
        control = min(candidates, key=lambda symbol: digest({"seed": seed, "session": str(day), "symbol": symbol}))
        scan["control_selection"] = control
        for role, symbol in (("strategy", scan["selection"]), ("control", control)):
            close = Decimal(candidates[symbol]["close_cny"])
            limit = (close * (1 + Decimal(spec["entry_limit_buffer_bp"]) / 10000)).quantize(_CENT, rounding=ROUND_DOWN)
            intent = FrozenIntent(str(day), days[i + 1], scan["as_of"], symbol, "buy",
                                  spec["order_lot_shares"], float(limit))
            plans[role].append(EntryPlan(intent, holding))
    # Open events are read after signal and limit selection. The execution view cannot alter them.
    symbols = sorted({plan.intent.symbol for entries in plans.values() for plan in entries})
    quotes, execution_receipts = _fixture_quotes(gate, history, phase_start, symbols)
    fees = [FeePeriod(days[0], **spec["fee_scenario"])]
    accounts = {role: run_account_path(
        quotes, entries, trading_calendar=days, initial_cash=spec["initial_cash_cny"],
        fee_periods=fees, data_kind="synthetic",
    ) for role, entries in plans.items()}
    return {
        "data_kind": "synthetic", "acceptance": "engineering_only",
        "partition_sessions": [day.isoformat() for day in days],
        "unscanned_tail": [day.isoformat() for day in days[scan_count:]],
        "signal_scans": scans, "execution_receipts": execution_receipts,
        "strategy": asdict(accounts["strategy"]), "control": asdict(accounts["control"]),
        "evaluation": _evaluation(spec, accounts["strategy"], accounts["control"]),
    }


def _market_preflight(gate: DailyPIT, spec: dict, first: str, last: str) -> dict:
    sessions = gate.read_trading_sessions(first, last)
    if len(sessions) < 2:
        raise EvidenceUnavailable("market preflight needs two in-partition trading sessions")
    signal, opening = (day.isoformat() for day in sessions[-2:])
    cutoff = f"{signal}T{spec['cutoff']}+08:00"
    submission = f"{opening}T09:14:59.999999999+08:00"
    symbols = tuple(sorted(spec["symbols"]))
    try:
        rows, receipt = gate.read_listings(
            signal, as_of=cutoff, symbols=symbols, min_listed_sessions=spec["min_listed_sessions"],
            check_session_status=True,
        )
        membership = {"receipt": asdict(receipt), "rows": rows.astype(object).where(rows.notna(), None).to_dict("records")}
    except EvidenceUnavailable as exc:
        membership = {"state": "unavailable", "reason": str(exc)}
    observations = []
    for symbol in symbols:
        # Query only the signal-day bar. The ordinary market gate refuses its date-only input.
        try:
            _, decision_receipt = gate.read_decision(symbol, signal, signal, as_of=cutoff)
            decision = {"state": "available", "receipt": asdict(decision_receipt)}
        except EvidenceUnavailable as exc:
            decision = {"state": "unavailable", "reason": str(exc)}
        try:
            controls = gate.read_preopen_controls(symbol, opening, as_of=submission, side="buy")
        except EvidenceUnavailable as exc:
            controls = {"submission": {"state": "unavailable", "reasons": [str(exc)]}}
        observations.append({"symbol": symbol, "decision": decision, "preopen": controls})
    return {
        "data_kind": "unspecified", "acceptance": "diagnostic_only",
        "reason": "market_execution_integration_unavailable",
        "evaluation": {"state": "unavailable"},
        "preflight": {
            "signal_session": signal, "decision_cutoff": cutoff, "execution_session": opening,
            "submission_cutoff": submission, "membership": membership, "membership_ready": False,
            "observations": observations,
        },
    }


def _report(run: dict) -> str:
    request, spec, result = run["request"], run["spec"], run["result"]
    first, last = spec["partitions"][request["partition"]]
    lines = [
        f"# {spec['study']}", "", spec["hypothesis"], "",
        f"- 运行 `{run['run_id']}`，状态 `{run['status']}`",
        f"- 验收级别 `{result['acceptance']}`，数据性质 `{result['data_kind']}`，请求可见性 `{spec['visibility']}`",
        f"- 分区 `{request['partition']}`，{first} 至 {last}，计划持有 {request['hold_sessions']} 个交易日",
        f"- 数据清单 `{request['lake_cut_id']}`，配置 `{request['spec_id']}`",
        "",
    ]
    evaluation = result["evaluation"]
    if "reason" in result:
        lines.extend([f"本次评价不可得。原因 `{result['reason']}`。", ""])
        if "detail" in result:
            lines.extend([str(result["detail"]), ""])
    if "preflight" in result:
        probe = result["preflight"]
        lines.extend([
            f"盘后探针 {probe['decision_cutoff']}，盘前探针 {probe['submission_cutoff']}。历史股票池尚未验收。", "",
            "| 证券 | 盘后日线 | 盘前提交 |", "| --- | --- | --- |",
        ])
        for obs in probe["observations"]:
            decision = obs["decision"]
            submission = obs["preopen"]["submission"]
            lines.append(f"| {obs['symbol']} | {decision.get('reason', decision['state'])} | "
                         f"{', '.join(submission['reasons']) or submission['state']} |")
        lines.append("")
    if "strategy" in result:
        triggered = sum(scan["selection"] is not None for scan in result["signal_scans"])
        lines.extend([
            "合成工程样例。使用开盘价格代理撮合，并在开盘成交后估值。", "",
            f"扫描 {len(result['signal_scans'])} 日，触发 {triggered} 日。双方均买入成交 "
            f"{evaluation['paired_filled_entry_sessions']} 日。区间末尾有 {len(result['unscanned_tail'])} 日未发起新信号。",
            "",
            "| 账户 | 订单数 | 有成交的订单数 | 期末现金（元） | 期末开盘估值（元） |",
            "| --- | --- | --- | --- | --- |",
        ])
        for role, label in (("strategy", "规则"), ("control", "对照")):
            path = result[role]
            final = path["accounts"][-1]
            filled = sum(row["filled"] > 0 for row in path["orders"])
            equity = final["opening_equity_cny"]
            mark = f"{Decimal(equity):.2f}" if equity is not None else "不可得"
            lines.append(f"| {label} | {len(path['orders'])} | {filled} | {Decimal(final['cash_cny']):.2f} | {mark} |")
        lines.extend(["", f"评价 `{evaluation['state']}`。"])
        if "terminal_net_excess_bp" in evaluation:
            lines.append(f"两条账户期末净值差占初始资金 {evaluation['terminal_net_excess_bp']} 个基点。")
        elif "reason" in evaluation:
            lines.append(f"原因 `{evaluation['reason']}`。")
        lines.append("")
    lines.extend(["## 冻结的假设", ""])
    lines.extend(f"- {assumption}" for assumption in spec["assumptions"])
    lines.extend(["", "[运行明细、配置与数据回执](run.json)", ""])
    return "\n".join(lines)


def _publish(output: Path, run: dict) -> dict:
    folder = output.resolve() / run["run_id"]
    folder.mkdir(parents=True, exist_ok=False)
    report = folder / "report.md"
    report.write_text(_report(run), encoding="utf-8", newline="\n")
    path, staged = folder / "run.json", folder / ".run.tmp"
    run["artifacts"] = {report.name: sha256(report)}
    staged.write_text(json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8", newline="\n")
    staged.replace(path)
    return {"run": str(path), "run_sha256": sha256(path),
            "report": str(report), "report_sha256": run["artifacts"][report.name]}


def run_study(
    book: TrialBook, *, spec_id: str, cut_id: str, request_id: str, partition: str, hold_sessions: int,
    snapshots: Path, cuts: Path, output: Path, retry: bool = False,
) -> dict:
    spec = book.spec(spec_id)
    if partition not in PARTITIONS:
        raise ValueError("unknown study partition")
    if type(hold_sessions) is not int or hold_sessions not in spec["holding_choices"]:
        raise ValueError("holding period is outside the frozen choices")
    if not isinstance(cut_id, str) or not re.fullmatch(r"[0-9a-f]{64}", cut_id):
        raise ValueError("invalid lake cut ID")
    request = {
        "spec_id": spec_id, "lake_cut_id": cut_id, "partition": partition,
        "hold_sessions": hold_sessions, "runtime": runtime_fingerprint(),
    }
    attempt = book.begin(request_id, request, retry=retry)
    if attempt["reused"]:
        for name in ("run", "report"):
            if name in attempt and sha256(Path(attempt[name])) != attempt[f"{name}_sha256"]:
                raise ValueError(f"archived {name} hash mismatch")
        return attempt

    status, result = "blocked", {
        "data_kind": "unspecified", "acceptance": "unavailable", "evaluation": {"state": "unavailable"},
    }
    if partition == "holdout":
        result.update(reason="holdout_sealed", data_kind="unread")
    else:
        try:
            first, last = spec["partitions"][partition]
            gate = DailyPIT(snapshots, cuts, cut_id, mode=spec["visibility"])
            if spec["profile"] == "synthetic":
                result = _synthetic_run(gate, spec, first, last, hold_sessions)
                status = "complete"
            else:
                result = _market_preflight(gate, spec, first, last)
        except EvidenceUnavailable as exc:
            result.update(reason="evidence_unavailable", detail=str(exc))
        except KeyboardInterrupt:
            status = "failed"
            result.update(reason="interrupted")
        except Exception as exc:
            status = "failed"
            result.update(reason="trial_execution_failed", detail=f"{type(exc).__name__}: {exc}")
    result["market_backtest_ready"] = False
    run = {
        "schema_version": 1, "run_id": f"study-{uuid4().hex}", "request_id": request_id,
        "attempt_id": attempt["attempt_id"], "request": request, "request_sha256": digest(request),
        "spec": spec, "status": status, "result": result, "archived_at": datetime.now(timezone.utc).isoformat(),
    }
    outcome = {"run_id": run["run_id"], "request_id": request_id, "reason": result.get("reason")}
    try:
        outcome.update(_publish(output, run))
    except (OSError, ValueError, TypeError) as exc:
        status = "failed"
        outcome.update(reason="artifact_publication_failed", detail=str(exc))
    book.finish(attempt["attempt_id"], status, outcome)
    return {**outcome, "attempt_id": attempt["attempt_id"], "status": status, "reused": False}
