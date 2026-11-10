"""Acquisition, pinned evidence reads and synthetic engine verification."""

import argparse
import json
import tarfile
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .archives import import_free_data_v0
from .opening import build_opening_snapshot
from .pit import DailyPIT, EvidenceUnavailable, freeze_daily_cut
from .snapshots import freeze
from .snapshots import sha256
from .sources import EXCHANGE_ROSTERS, SourceError, fetch_baostock, fetch_exchange_roster, fetch_sina_actions
from .universe import build_audited_calendar, build_listing_snapshot, build_risk_name_snapshot


def smoke(root: Path) -> Path:
    """Persist an upstream-engine integration example with synthetic prices."""
    from .engine import ExecutionSpec, FrozenIntent, OpenEvent, run_open_proxy
    days = [date(2026, 9, day) for day in (14, 15, 16, 17, 18)]
    events = [
        OpenEvent(day, "600519.SH", price, f"{day}T09:00:00+08:00", True, 12, 8)
        for day, price in zip(days, (10, 10.2, 10.5, 10.3, 10.4))
    ]
    intents = [
        FrozenIntent("entry", days[1], f"{days[0]}T18:00:00+08:00",
                     "600519.SH", "buy", 100, 11),
        FrozenIntent("exit", days[3], f"{days[2]}T18:00:00+08:00",
                     "600519.SH", "sell", 100, 9),
    ]
    spec = ExecutionSpec(10000, 0.0003, 5, 0.0005, 0.00001)
    run = run_open_proxy(events, intents, spec, trading_calendar=days)
    folder = root / f"synthetic-{uuid.uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    for name, frame in [
        ("orders", run.result.orders_df), ("executions", run.result.executions_df),
        ("trades", run.result.trades_df),
    ]:
        frame.to_csv(folder / f"{name}.csv", index=False)
    run.result.equity_curve.to_csv(folder / "opening_equity.csv")
    manifest = {
        **run.metadata, "execution_spec": asdict(spec),
        "events": [asdict(e) for e in events], "intents": [asdict(i) for i in intents],
        "rejections": run.rejections, "submissions": run.submissions,
        "implementation_sha256": {
            name: sha256(Path(__file__).with_name(name)) for name in ("cli.py", "engine.py")
        },
        "artifact_sha256": {p.name: sha256(p) for p in folder.glob("*.csv")},
    }
    (folder / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return folder


def account_smoke(root: Path) -> Path:
    """Publish a hand-checkable synthetic account across a dividend and deferred exit."""
    from .account import AccountQuote, CashDividend, EntryPlan, FeePeriod, run_account_path
    from .engine import FrozenIntent, OpenEvent

    days = [date(2026, 6, day) for day in (24, 25, 26, 29, 30)]
    stock, other = "600519.SH", "600000.SH"
    prices = [
        (1, stock, 20, 20, True, 18, 22),
        (2, stock, None, None, False, 17, 21),
        (3, stock, 17, 17, True, 17, 21),
        (4, stock, 19, 19, True, 17, 21),
        (2, other, 2, 2, True, 1.8, 2.2),
        (3, other, 2, 2, True, 1.8, 2.2),
        (4, other, 2.2, 2.2, True, 1.8, 2.2),
    ]
    quotes = [
        AccountQuote(OpenEvent(days[i], symbol, price, f"{days[i]}T09:00:00+08:00", tradable, upper, lower), mark)
        for i, symbol, price, mark, tradable, lower, upper in prices
    ]
    entries = [
        EntryPlan(FrozenIntent(label, days[i], f"{days[i - 1]}T18:00:00+08:00",
                               symbol, "buy", 100, limit), hold_sessions=1)
        for label, i, symbol, limit in (
            ("distribution_holder", 1, stock, 20.5), ("before_cash_release", 2, other, 2.1),
            ("after_cash_release", 3, other, 2.1),
        )
    ]
    fee_periods = [
        FeePeriod(days[0], 0.0003, 5, 0.0005, 0),
        FeePeriod(days[3], 0.001, 5, 0.001, 0),
    ]
    dividends = [CashDividend("synthetic_cash_dividend", stock, days[1], days[2], days[2], "2.35")]
    run = run_account_path(
        quotes, entries, trading_calendar=days, initial_cash=2100, fee_periods=fee_periods, dividends=dividends,
    )
    folder = root / f"synthetic-account-{uuid.uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {
        **run.metadata, "run_id": folder.name, "initial_cash_cny": "2100",
        "trading_calendar": days, "quotes": [asdict(quote) for quote in quotes],
        "entry_plans": [asdict(plan) for plan in entries], "fee_periods": [asdict(p) for p in fee_periods],
        "dividend_terms": [asdict(action) for action in dividends],
        "orders": run.orders, "account_path": run.accounts, "lots": run.lots, "cash_actions": run.cash_actions,
        "implementation_sha256": {
            name: sha256(Path(__file__).with_name(name)) for name in ("cli.py", "engine.py", "account.py")
        },
    }
    path = folder / "run.json"
    staged = folder / ".run.tmp"
    staged.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
                      encoding="utf-8")
    staged.rename(path)
    return path


def _pit_arguments(sub) -> None:
    pin = sub.add_parser("pin-daily-cut", help="freeze explicit daily input versions")
    pin.add_argument("--calendar", type=Path)
    pin.add_argument("--bar", action="append", metavar="SYMBOL=MANIFEST", default=[])
    pin.add_argument("--listing-evidence", type=Path)
    pin.add_argument("--risk-name-evidence", type=Path)
    pin.add_argument("--opening-evidence", type=Path)
    read = sub.add_parser("read-daily-cut", help="read a diagnostic slice through L1")
    read.add_argument("--cut-id", required=True)
    read.add_argument("--purpose", choices=["decision", "execution", "outcome"], required=True)
    read.add_argument("--symbol", required=True)
    read.add_argument("--start", required=True)
    read.add_argument("--end")
    read.add_argument("--as-of", required=True)
    read.add_argument("--mode", choices=["verified_visible", "reconstructed"], default="verified_visible")
    listings = sub.add_parser("read-listings", help="read pinned historical listing diagnostics")
    listings.add_argument("--cut-id", required=True)
    listings.add_argument("--session", required=True)
    listings.add_argument("--as-of", required=True)
    listings.add_argument("--symbol", action="append", default=[])
    listings.add_argument("--min-listed-sessions", type=int)
    listings.add_argument("--check-session-status", action="store_true")
    listings.add_argument("--mode", choices=["verified_visible", "reconstructed"], default="verified_visible")
    preopen = sub.add_parser("read-preopen", help="check published execution controls before the proxy open")
    preopen.add_argument("--cut-id", required=True)
    preopen.add_argument("--session", required=True)
    preopen.add_argument("--as-of", required=True)
    preopen.add_argument("--symbol", required=True)
    preopen.add_argument("--side", choices=("buy", "sell"), required=True)
    preopen.add_argument("--mode", choices=("verified_visible", "reconstructed"), default="verified_visible")
    for command in (pin, read, listings, preopen):
        command.add_argument("--snapshots-root", type=Path, default=Path("artifacts/snapshots"))
        command.add_argument("--cuts-root", type=Path, default=Path("artifacts/cuts"))


def _pit_command(args: argparse.Namespace) -> dict:
    if args.command == "pin-daily-cut":
        bars: dict[str, Path] = {}
        for item in args.bar:
            symbol, separator, manifest = item.partition("=")
            if not separator or not manifest or symbol in bars:
                raise ValueError("--bar requires one unique SYMBOL=MANIFEST per instrument")
            bars[symbol] = Path(manifest)
        cut_id = freeze_daily_cut(
            args.snapshots_root, args.cuts_root, args.calendar, bars,
            listing_manifest=args.listing_evidence,
            risk_name_manifest=args.risk_name_evidence,
            opening_manifest=args.opening_evidence,
        )
        return {"status": "pinned", "lake_cut_id": cut_id, "scope": "diagnostic_only"}
    gate = DailyPIT(args.snapshots_root, args.cuts_root, args.cut_id, mode=args.mode)
    if args.command == "read-preopen":
        return gate.read_preopen_controls(args.symbol, args.session, as_of=args.as_of, side=args.side)
    if args.command == "read-listings":
        frame, receipt = gate.read_listings(
            args.session, as_of=args.as_of, symbols=tuple(args.symbol),
            min_listed_sessions=args.min_listed_sessions,
            check_session_status=args.check_session_status,
        )
        return {"status": "ok", "receipt": asdict(receipt), "rows": frame.to_dict("records")}
    end = args.end or args.start
    if args.purpose == "execution":
        if end != args.start:
            raise ValueError("an opening reference requires exactly one session")
        frame, receipt = gate.read_opening_reference(args.symbol, args.start, as_of=args.as_of)
    else:
        read = gate.read_decision if args.purpose == "decision" else gate.read_outcome
        frame, receipt = read(args.symbol, args.start, end, as_of=args.as_of)
    rows = frame.astype(object).where(frame.notna(), None).to_dict("records")
    return {"status": "ok", "receipt": asdict(receipt), "rows": rows}


def _study_arguments(sub) -> None:
    from .studies import PARTITIONS

    spec = sub.add_parser("freeze-study", help="validate and freeze a research specification")
    spec.add_argument("--spec", required=True, type=Path)
    fixture = sub.add_parser("make-study-fixture", help="publish synthetic bars and a calendar")
    run = sub.add_parser("run-study", help="execute a synthetic study or diagnose a market candidate")
    run.add_argument("--cut-id", required=True)
    run.add_argument("--request-id", required=True)
    run.add_argument("--partition", choices=PARTITIONS, required=True)
    run.add_argument("--hold-sessions", type=int, required=True)
    run.add_argument("--retry", action="store_true")
    run.add_argument("--output", type=Path, default=Path("artifacts/runs"))
    for command in (spec, fixture, run):
        command.add_argument("--book", type=Path, default=Path("artifacts/studies.sqlite3"))
    for command in (fixture, run):
        command.add_argument("--spec-id", required=True)
        command.add_argument("--snapshots-root", type=Path, default=Path("artifacts/snapshots"))
        command.add_argument("--cuts-root", type=Path, default=Path("artifacts/cuts"))


def _study_command(args: argparse.Namespace) -> dict:
    from .studies import TrialBook, load_document

    book = TrialBook(args.book)
    if args.command == "freeze-study":
        return {"status": "frozen", "spec_id": book.freeze(load_document(args.spec))}
    from .study_run import make_study_fixture, run_study

    if args.command == "make-study-fixture":
        cut = make_study_fixture(book.spec(args.spec_id), args.snapshots_root, args.cuts_root)
        return {"status": "fixture_ready", "data_kind": "synthetic", "lake_cut_id": cut}
    return run_study(
        book, spec_id=args.spec_id, cut_id=args.cut_id, request_id=args.request_id, partition=args.partition,
        hold_sessions=args.hold_sessions, snapshots=args.snapshots_root, cuts=args.cuts_root, output=args.output,
        retry=args.retry,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("smoke", help="synthetic engineering verification only")
    check.add_argument("--output", type=Path, default=Path("artifacts/runs"))
    account = sub.add_parser("account-smoke", help="synthetic daily account and settlement scenario")
    account.add_argument("--output", type=Path, default=Path("artifacts/runs"))
    ingest = sub.add_parser("ingest-baostock")
    ingest.add_argument("--dataset", required=True,
                        choices=["daily_bars", "instruments", "trading_calendar"])
    ingest.add_argument("--symbol")
    ingest.add_argument("--start", required=True)
    ingest.add_argument("--end", required=True)
    ingest.add_argument("--timeout", type=float, default=45)
    ingest.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    archive = sub.add_parser("import-free-data-v0", help="import daily bars from a verified public archive")
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--symbol", action="append", required=True)
    archive.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    archive.add_argument("--reports-root", type=Path, default=Path("artifacts/runs"))
    actions = sub.add_parser("ingest-sina-actions")
    actions.add_argument("--symbol", required=True)
    actions.add_argument("--timeout", type=float, default=45)
    actions.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    roster = sub.add_parser("ingest-exchange-roster", help="capture an official roster for source audit")
    roster.add_argument("--dataset", choices=sorted(EXCHANGE_ROSTERS), required=True)
    roster.add_argument("--timeout", type=float, default=30)
    roster.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    listing = sub.add_parser("build-listing-evidence", help="normalize an archived exchange evidence bundle")
    listing.add_argument("--roster-report", type=Path, required=True)
    listing.add_argument("--sse-delists", type=Path, required=True)
    listing.add_argument("--notice-facts", type=Path, required=True)
    listing.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    calendar = sub.add_parser("build-trading-calendar", help="reconcile archived trading dates and closure notices")
    calendar.add_argument("--dates-report", type=Path, required=True)
    calendar.add_argument("--closure-facts", type=Path, required=True)
    calendar.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    risk_names = sub.add_parser("build-risk-name-evidence", help="normalize positive archived name observations")
    risk_names.add_argument("--status-report", type=Path, required=True)
    risk_names.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    opening = sub.add_parser("build-opening-evidence", help="freeze reviewed opening notices and dated rules")
    opening.add_argument("--review", type=Path, required=True)
    opening.add_argument("--output", type=Path, default=Path("artifacts/snapshots"))
    _pit_arguments(sub)
    _study_arguments(sub)
    args = parser.parse_args()
    if args.command == "smoke":
        print(smoke(args.output).resolve())
    elif args.command == "account-smoke":
        print(account_smoke(args.output).resolve())
    elif args.command in {"freeze-study", "make-study-fixture", "run-study"}:
        try:
            result = _study_command(args)
        except (EvidenceUnavailable, OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if result["status"] == "failed":
            raise SystemExit(1)
        if result["status"] in {"blocked", "running_or_interrupted"}:
            raise SystemExit(2)
    elif args.command == "import-free-data-v0":
        try:
            report, status = import_free_data_v0(args.archive, tuple(args.symbol), args.output, args.reports_root)
        except (OSError, SourceError, ValueError, tarfile.TarError) as exc:
            parser.error(str(exc))
        print(json.dumps({"status": status, "report": str(report)}, ensure_ascii=False))
        if status != "complete":
            raise SystemExit(2)
    elif args.command in {"build-listing-evidence", "build-trading-calendar", "build-risk-name-evidence",
                          "build-opening-evidence"}:
        try:
            if args.command == "build-listing-evidence":
                path = build_listing_snapshot(args.roster_report, args.sse_delists, args.notice_facts, args.output)
            elif args.command == "build-trading-calendar":
                path = build_audited_calendar(args.dates_report, args.closure_facts, args.output)
            elif args.command == "build-risk-name-evidence":
                path = build_risk_name_snapshot(args.status_report, args.output)
            else:
                path = build_opening_snapshot(args.review, args.output)
            print(path.resolve())
        except (OSError, KeyError, TypeError, ValueError) as exc:
            parser.error(str(exc))
    elif args.command in {"pin-daily-cut", "read-daily-cut", "read-listings", "read-preopen"}:
        try:
            print(json.dumps(_pit_command(args), ensure_ascii=False, allow_nan=False))
        except EvidenceUnavailable as exc:
            print(json.dumps({"status": "unavailable", "reason": str(exc)}, ensure_ascii=False))
            raise SystemExit(2) from None
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if args.command == "ingest-baostock":
            acquired = fetch_baostock(args.dataset, args.start, args.end, args.symbol, args.timeout)
        elif args.command == "ingest-exchange-roster":
            acquired = fetch_exchange_roster(args.dataset, args.timeout)
        else:
            acquired = fetch_sina_actions(args.symbol, args.timeout)
        print(freeze(acquired, args.output).resolve())


if __name__ == "__main__":
    main()

