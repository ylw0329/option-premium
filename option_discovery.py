# -*- coding: utf-8 -*-
"""期权品种发现与最近未到期月份选择（TqSdk 3.10.1）。

设计要点:
- 商品期权自动发现: query_quotes(ins_class="OPTION", exchange_id=商品期货交易所) 取全部未下市商品期权,
  再经 query_symbol_info 取 underlying_symbol(期货合约) 与 last_exercise_datetime,
  合并期货合约的 product_id, 按品种取 last_exercise_datetime 最小(最近)且未到期的标的。
  -> 完全基于 TqSdk 实际到期信息, 不做合约代码字符串排序。
- 兜底: 自动发现为空时, 按 config.json 的 commodity_options 列表逐品种扫描。
- 股指期权 IO/MO/HO: query_options(指数标的) + query_symbol_info,
  按 (exercise_year, exercise_month) 分组取最近未到期月份。
"""
import datetime

import pandas as pd

from data_fetcher import safe_query_symbol_info

# 商品期货期权所在的交易所(不含 CFFEX: CFFEX 仅 IO/MO/HO, 单独处理)
COMMODITY_EXCHANGES = ["SHFE", "DCE", "CZCE", "INE", "GFEX"]

# 期权的 last_exercise_datetime 为秒级 timestamp, 用于排序比较
_COL_UNDERLYING = "underlying_symbol"
_COL_INSTRUMENT = "instrument_id"
_COL_EXPIRE_REST_DAYS = "expire_rest_days"
_COL_LAST_EXERCISE_DT = "last_exercise_datetime"
_COL_EXERCISE_YEAR = "exercise_year"
_COL_EXERCISE_MONTH = "exercise_month"
_COL_EXCHANGE = "exchange_id"
_COL_PRODUCT = "product_id"


def is_market_closed(close_hour: int = 15, close_minute: int = 0) -> bool:
    """判断当前(系统时间)是否已过收盘时间。

    到期日当天(expire_rest_days==0)的合约:
    - 未过收盘时间: 仍可交易, 计算当月合约
    - 已过收盘时间: 合约即将摘牌, 跳到下一个月合约

    商品/股指期权日盘统一 15:00 收盘, 通过 config 的 expire_close_hour/minute 可调。
    """
    now = datetime.datetime.now()
    cutoff = now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    return now >= cutoff


def _to_int_or_none(value):
    try:
        if pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def discover_commodity_options(api, close_hour: int = 15, close_minute: int = 0) -> dict:
    """自动发现商品期权品种及其最近未到期月份的标的期货合约。

    收盘跳月: 若最近月份 expire_rest_days==0(到期当天) 且已过收盘时间, 跳到下一个月份。

    Returns:
        {product_id(lower): {"underlying": "SHFE.au2504", "exercise_year": int|None,
                             "exercise_month": int|None, "exchange_id": str}}
    """
    options = api.query_quotes(ins_class="OPTION", exchange_id=COMMODITY_EXCHANGES, expired=False)
    if not options:
        return {}

    df_opts = safe_query_symbol_info(api, options)
    if df_opts is None or df_opts.empty:
        return {}

    need = [_COL_UNDERLYING, _COL_LAST_EXERCISE_DT, _COL_EXPIRE_REST_DAYS]
    df_opts = df_opts.dropna(subset=need)
    # 只保留未到期(expire_rest_days >= 0): 0 表示到期当天, 仍可交易
    df_opts = df_opts[df_opts[_COL_EXPIRE_REST_DAYS] >= 0]
    if df_opts.empty:
        return {}

    # 收盘跳月: 若已过收盘, 排除 expire_rest_days==0 的当月合约(它们即将摘牌)
    if is_market_closed(close_hour, close_minute):
        df_nonzero = df_opts[df_opts[_COL_EXPIRE_REST_DAYS] > 0]
        if not df_nonzero.empty:
            df_opts = df_nonzero

    # 期货标的 -> product_id
    underlyings = [u for u in df_opts[_COL_UNDERLYING].unique().tolist() if isinstance(u, str) and u]
    if not underlyings:
        return {}
    df_fut = safe_query_symbol_info(api, underlyings)
    if df_fut is None or df_fut.empty:
        return {}
    df_fut = df_fut.dropna(subset=[_COL_PRODUCT, _COL_INSTRUMENT])

    # 期货合约 -> (product_id, exchange_id) 映射(用 dict 避免 merge 同名列冲突)
    fut_map = {row[_COL_INSTRUMENT]: (str(row[_COL_PRODUCT]).lower(), row.get(_COL_EXCHANGE) or "")
               for _, row in df_fut.iterrows() if isinstance(row[_COL_INSTRUMENT], str)}

    # 在期权表上挂期货品种信息
    df_opts = df_opts.copy()
    df_opts["_pid"] = df_opts[_COL_UNDERLYING].map(lambda s: fut_map.get(s, (None, None))[0])
    df_opts["_exch"] = df_opts[_COL_UNDERLYING].map(lambda s: fut_map.get(s, (None, None))[1])
    df_opts = df_opts.dropna(subset=["_pid"])
    if df_opts.empty:
        return {}

    result = {}
    for pid, grp in df_opts.groupby("_pid"):
        pid = str(pid).lower()
        # 同一期货标的下所有期权 last_exercise_datetime 相同; groupby 取 min 仅为去重
        by_under = grp.groupby(_COL_UNDERLYING)[_COL_LAST_EXERCISE_DT].min()
        if by_under.empty:
            continue
        nearest_under = by_under.idxmin()  # 最近未到期的标的
        row = grp[grp[_COL_UNDERLYING] == nearest_under].iloc[0]
        result[pid] = {
            "underlying": nearest_under,
            "exercise_year": _to_int_or_none(row.get(_COL_EXERCISE_YEAR)),
            "exercise_month": _to_int_or_none(row.get(_COL_EXERCISE_MONTH)),
            "exchange_id": grp["_exch"].iloc[0] or "",
        }
    return result


def discover_via_config(api, product_list, close_hour: int = 15, close_minute: int = 0) -> dict:
    """兜底: 按 config 列表逐品种扫描, 找最近未到期且存在期权的期货合约。"""
    closed = is_market_closed(close_hour, close_minute)
    result = {}
    for prod in product_list:
        pid = str(prod).strip().lower()
        if not pid:
            continue
        try:
            futures = api.query_quotes(ins_class="FUTURE", product_id=pid, expired=False)
            if not futures:
                continue
            best = None  # (last_exercise_datetime, underlying, year, month, exchange)
            for fut in futures:
                opts = api.query_options(fut, expired=False)
                if not opts:
                    continue
                df = safe_query_symbol_info(api, opts)
                if df is None or df.empty:
                    continue
                df = df.dropna(subset=[_COL_LAST_EXERCISE_DT])
                df = df[df[_COL_EXPIRE_REST_DAYS] >= 0]
                # 收盘跳月: 排除 expire_rest_days==0 的当月合约
                if closed:
                    df_nz = df[df[_COL_EXPIRE_REST_DAYS] > 0]
                    if not df_nz.empty:
                        df = df_nz
                if df.empty:
                    continue
                led = float(df[_COL_LAST_EXERCISE_DT].min())
                if best is None or led < best[0]:
                    row = df.iloc[0]
                    best = (led, fut,
                            _to_int_or_none(row.get(_COL_EXERCISE_YEAR)),
                            _to_int_or_none(row.get(_COL_EXERCISE_MONTH)),
                            row.get(_COL_EXCHANGE) or "")
            if best:
                result[pid] = {
                    "underlying": best[1],
                    "exercise_year": best[2],
                    "exercise_month": best[3],
                    "exchange_id": best[4],
                }
        except Exception as e:
            print(f"[兜底] 品种 {pid.upper()} 发现失败: {e}")
    return result


def select_nearest_index_month(api, index_symbol, close_hour: int = 15, close_minute: int = 0) -> tuple:
    """选择股指期权最近未到期月份。

    收盘跳月: 若最近月份 expire_rest_days==0 且已过收盘, 选下一个月份。

    Returns:
        (exercise_year, exercise_month) 或 None
    """
    try:
        options = api.query_options(index_symbol, expired=False)
    except Exception as e:
        print(f"[股指] {index_symbol} 查询期权失败: {e}")
        return None
    if not options:
        return None
    df = safe_query_symbol_info(api, options)
    if df is None or df.empty:
        return None
    df = df.dropna(subset=[_COL_EXERCISE_YEAR, _COL_EXERCISE_MONTH, _COL_LAST_EXERCISE_DT])
    df = df[df[_COL_EXPIRE_REST_DAYS] >= 0]
    if df.empty:
        return None
    # 收盘跳月: 排除 expire_rest_days==0 的当月合约
    if is_market_closed(close_hour, close_minute):
        df_nz = df[df[_COL_EXPIRE_REST_DAYS] > 0]
        if not df_nz.empty:
            df = df_nz
    grouped = df.groupby([_COL_EXERCISE_YEAR, _COL_EXERCISE_MONTH])[_COL_LAST_EXERCISE_DT].min()
    if grouped.empty:
        return None
    ey, em = grouped.idxmin()
    return int(ey), int(em)
