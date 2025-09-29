from __future__ import annotations

from fastapi import APIRouter, HTTPException
from typing import Any, Dict, List
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/market", tags=["market"])

try:
    import akshare as ak  # type: ignore
    HAS_AKSHARE = True
except Exception:
    HAS_AKSHARE = False


def _df_to_records(df) -> List[Dict[str, Any]]:
    try:
        # Normalize common column names
        cols = {c.lower(): c for c in df.columns}
        # date column candidates
        date_col = None
        for k in ("date", "日期", "trade_date"):
            if k in cols:
                date_col = cols[k]
                break
        # close column candidates
        close_col = None
        for k in ("close", "收盘", "收盘价", "close_price"):
            if k in cols:
                close_col = cols[k]
                break
        if not date_col or not close_col:
            return []
        out = []
        for _, row in df.iterrows():
            # accept datetime/date/str/int(YYYYMMDD)
            d = row[date_col]
            if isinstance(d, (int, float)):
                try:
                    s = str(int(d))
                    if len(s) == 8:
                        dts = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
                    else:
                        dts = str(d)
                except Exception:
                    dts = str(d)
            elif isinstance(d, datetime):
                dts = d.strftime("%Y-%m-%d")
            else:
                dts = str(d)
            try:
                c = float(row[close_col])
            except Exception:
                continue
            out.append({"date": dts, "close": c})
        return out
    except Exception:
        return []


@router.get("/index")
def get_index_series(symbol: str = "sh000001", days: int = 60):
    """Fetch index daily close series using akshare if available.

    - symbol: e.g., sh000001(上证综指), sh000300(沪深300), sz399006(创业板), sh000905(中证500)
    - days: number of days to return (approximate; filters by latest N records)
    """
    if not HAS_AKSHARE:
        raise HTTPException(status_code=501, detail="akshare 未安装。请在服务器安装: pip install akshare")

    df = None
    last_err = None
    # Try a few akshare functions for robustness across versions
    try_funcs = [
        ("stock_zh_index_daily", {"symbol": symbol}),
        ("stock_zh_index_daily_em", {"symbol": symbol}),
        ("index_zh_a_hist", {"symbol": symbol, "period": "daily", "start_date": "20000101", "end_date": None, "adjust": ""}),
    ]
    for fname, kwargs in try_funcs:
        try:
            fn = getattr(ak, fname, None)
            if not fn:
                continue
            res = fn(**kwargs)
            if res is not None and len(res) > 0:
                df = res
                break
        except Exception as e:  # pragma: no cover - tolerant to akshare layout
            last_err = e
            continue

    if df is None:
        msg = f"未能通过 akshare 获取 {symbol} 数据"
        if last_err:
            msg += f": {last_err}"
        raise HTTPException(status_code=502, detail=msg)

    records = _df_to_records(df)
    if not records:
        raise HTTPException(status_code=502, detail="akshare 数据格式无法解析")

    # Keep last N days by tailing; data is usually ascending by date
    records = records[-max(1, int(days)) :]
    return {"symbol": symbol, "count": len(records), "items": records}

