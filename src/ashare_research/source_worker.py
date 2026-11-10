"""Fixed source operations inside a subprocess with a caller-owned deadline."""

import contextlib
import io
import json
import sys

from ._vendor.astock_baostock import _rs_to_df, bs, bs_session
from .sources import BAOSTOCK_DATASETS, BAR_FIELDS, EXCHANGE_ROSTERS


def main() -> None:
    """Read one fixed operation from stdin; keep SDK login messages out of JSON."""
    request = json.load(sys.stdin)
    dataset = request["dataset"]
    using_baostock = dataset in BAOSTOCK_DATASETS
    if not using_baostock and dataset not in EXCHANGE_ROSTERS and dataset != "corporate_actions_raw":
        raise ValueError("unsupported worker operation")
    session = bs_session() if using_baostock else contextlib.nullcontext()
    with contextlib.redirect_stdout(io.StringIO()), session:
        if dataset == "daily_bars":
            result = bs.query_history_k_data_plus(
                request["code"], BAR_FIELDS, start_date=request["start"],
                end_date=request["end"], frequency="d", adjustflag="3",
            )
        elif dataset == "instruments":
            result = bs.query_stock_basic(code=request.get("code", ""))
        elif dataset == "trading_calendar":
            result = bs.query_trade_dates(
                start_date=request["start"], end_date=request["end"]
            )
        elif dataset == "corporate_actions_raw":
            import akshare as ak
            frame = ak.stock_history_dividend_detail(symbol=request["code"], indicator="分红")
        else:
            import akshare as ak
            endpoint = EXCHANGE_ROSTERS[dataset]
            # The operation and arguments come from the local allowlist, not the request.
            frame = getattr(ak, endpoint.operation)(symbol=endpoint.selector)
        if using_baostock:
            frame = _rs_to_df(result)
            # The upstream helper checks before iteration; also check cursor failures.
            if result.error_code != "0":
                raise RuntimeError(f"cursor failed: {result.error_code} {result.error_msg}")
    frame = frame.astype(object).where(frame.notna(), None)
    json.dump(
        {"columns": list(frame.columns), "rows": frame.values.tolist()},
        sys.stdout, ensure_ascii=True, allow_nan=False, default=str,
    )


if __name__ == "__main__":
    main()

