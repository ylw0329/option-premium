# -*- coding: utf-8 -*-
"""虚值档位选取 + 权利金指标计算 + 异常标记（TqSdk 3.10.1）。

### 优化(2026-09-21): 三阶段批量计算
1. 一次性 get_quote_list 获取全部标的报价 (省 ~68 次 get_quote)
2. CALL/PUT 合并查询: 一次 query_options 取全部期权 + 一次 query_symbol_info (省 ~136 次调用)
3. 复用 symbol_info DataFrame: 到期天数/交易所等直接从选档 DataFrame 取 (省 ~68 次)
4. 一次性 get_quote_list 获取全部选中合约报价 (省 ~68 次)

总 API 调用从 ~476 次降至 ~70 次, 预计 4.5 分钟降至约 1 分钟。

### 虚值档位选择
自行按定义分类 OTM1..OTMN, 不用 query_atm_options (tie-break 偏档)。
CALL 虚值 = strike > spot, 取最小 N 个; PUT 虚值 = strike < spot, 取最大 N 个。

### 核心公式(不变):
  Result = (ΣC_i + ΣP_i) / (2 * (expire_rest_days + 1))
"""
import math
import time

from data_fetcher import get_underlying_price, safe_query_symbol_info


def build_columns(call_levels: int, put_levels: int) -> list:
    """根据 C/P 档位数动态生成表格列（顺序即输出顺序）。"""
    cols = ["交易所", "品种", "最近到期月份", "标的价格"]
    for i in range(1, call_levels + 1):
        cols += [f"C虚{i}合约", f"C虚{i}价格", f"C虚{i}IV"]
    for i in range(1, put_levels + 1):
        cols += [f"P虚{i}合约", f"P虚{i}价格", f"P虚{i}IV"]
    cols += ["到期天数", "四张合约合计", "最终结果", "平均IV", "状态"]
    return cols


def _empty_row(columns, exchange_id: str, product_id: str, product_names: dict = None, year=None, month=None) -> dict:
    row = {c: None for c in columns}
    row["交易所"] = exchange_id
    pid = str(product_id).upper()
    name = product_names.get(pid) if product_names else None
    row["品种"] = f"{pid}：{name}" if name else pid
    if year and month:
        row["最近到期月份"] = f"{int(year) % 100:02d}{int(month):02d}"
    row["状态"] = ""
    return row


def _valid_price(value) -> bool:
    try:
        return not math.isnan(value)
    except (TypeError, ValueError):
        return False


def _norm_cdf(x):
    """标准正态累积分布函数 N(x), 用 math.erf 实现, 无需 scipy。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, T, r, vol, option_class):
    """Black-Scholes 期权理论价格。"""
    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2.0) * T) / (vol * sqrt_T)
    d2 = d1 - vol * sqrt_T
    if option_class == "CALL":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)
    return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def calc_implied_volatility(option_price, spot, strike, T, r, option_class, max_iter=200):
    """用 Black-Scholes + 二分法反解隐含波动率。"""
    if option_price is None or spot is None or strike is None or T is None:
        return None
    if T <= 0 or spot <= 0 or strike <= 0 or option_price <= 0:
        return None
    disc_strike = strike * math.exp(-r * T)
    if option_class == "CALL":
        intrinsic = max(spot - disc_strike, 0.0)
    else:
        intrinsic = max(disc_strike - spot, 0.0)
    if option_price < intrinsic - 1e-6:
        return None
    vol_low, vol_high = 1e-4, 5.0
    for _ in range(max_iter):
        vol_mid = (vol_low + vol_high) / 2.0
        price = _bs_price(spot, strike, T, r, vol_mid, option_class)
        if abs(price - option_price) < 1e-6:
            return vol_mid
        if price > option_price:
            vol_high = vol_mid
        else:
            vol_low = vol_mid
    return (vol_low + vol_high) / 2.0


def _pick_otm_from_df(df, underlying_price, option_class: str, levels: int):
    """从已查回的 symbol_info DataFrame 中按虚值定义选 OTM1..OTMN。

    与旧版 _pick_otm_options 的区别: 不再调用 query_options/query_symbol_info,
    直接对 DataFrame 按 option_class 过滤后选档, 避免重复 API 调用。

    Returns:
        (otm_ids: list[str|None], atm_strike, strikes_list)
    """
    if df is None or df.empty:
        return [None] * levels, None, []

    # 按 option_class 过滤(CALL/PUT)
    if "option_class" in df.columns:
        df = df[df["option_class"] == option_class]

    df = df.dropna(subset=["strike_price", "instrument_id"])
    if df.empty:
        return [None] * levels, None, []

    strikes = sorted(df["strike_price"].astype(float).unique().tolist())
    if not strikes:
        return [None] * levels, None, []

    if option_class == "CALL":
        otm_strikes = [s for s in strikes if s > underlying_price][:levels]
    else:
        otm_strikes = [s for s in reversed(strikes) if s < underlying_price][:levels]

    strike_to_id = {}
    for _, r in df.iterrows():
        s = float(r["strike_price"])
        if s not in strike_to_id:
            strike_to_id[s] = r["instrument_id"]

    otm_ids = [strike_to_id.get(s) for s in otm_strikes]
    while len(otm_ids) < levels:
        otm_ids.append(None)

    atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))
    return otm_ids, atm_strike, strikes


def _safe_int(series):
    s = series.dropna()
    if s.empty:
        return None
    try:
        return int(s.iloc[0])
    except (TypeError, ValueError):
        return None


def calculate_all(api, products: dict, columns: list, call_levels: int, put_levels: int,
                   product_names: dict = None,
                   expire_add_days: int = 1, expire_near_threshold: int = 0, expire_near_add: int = 0,
                   risk_free_rate: float = 0.025) -> list:
    """对全部品种计算。优化: 批量标的报价 + CALL/PUT 合并查询 + 批量期权报价。

    分四阶段:
    1. 一次性 get_quote_list 获取全部标的报价
    2. 逐品种 query_options(不分方向) + query_symbol_info, 选虚值档, 收集所有选中合约
    3. 一次性 get_quote_list 获取全部选中合约报价
    4. 逐品种用预获取数据计算 IV/权利金
    """
    if not products:
        return []

    # ===== Phase 1: 批量获取所有标的报价 =====
    underlying_symbols = [info.get("underlying", "") for info in products.values()
                          if info.get("underlying")]
    underlying_symbols = list(dict.fromkeys(underlying_symbols))  # 去重保序
    print(f"批量获取 {len(underlying_symbols)} 个标的报价...")
    uq_list = api.get_quote_list(underlying_symbols)
    uq_map = dict(zip(underlying_symbols, uq_list))

    # ===== Phase 2: 逐品种查询期权, 选虚值档, 收集所有选中合约 =====
    all_option_symbols = set()
    product_data = {}  # pid -> phase data

    for pid, info in products.items():
        underlying = info.get("underlying", "")
        exchange_id = info.get("exchange_id", "")
        contract_month = info.get("contract_month")
        ey = int(info["exercise_year"]) if info.get("exercise_year") else None
        em = int(info["exercise_month"]) if info.get("exercise_month") else None

        row = _empty_row(columns, exchange_id, pid, product_names, ey, em)
        if contract_month:
            row["最近到期月份"] = contract_month

        uq = uq_map.get(underlying)
        underlying_price, price_field = get_underlying_price(uq) if uq else (None, None)

        if underlying_price is None:
            if uq:
                print(f"  [{pid.upper()}] {underlying} 行情无效: "
                      f"last_price={uq.last_price} pre_settle={uq.pre_settlement}")
            else:
                print(f"  [{pid.upper()}] {underlying} 未获取到行情")
            row["状态"] = "标的价格无效"
            product_data[pid] = {"row": row}
            continue

        if price_field != "last_price":
            print(f"  [{pid.upper()}] {underlying} last_price 为 NaN, 回退 {price_field}={underlying_price}")
        row["标的价格"] = underlying_price

        # 一次查询 CALL+PUT 全部期权(不分方向, 省 1 次 query_options + 1 次 query_symbol_info)
        try:
            kwargs = {"expired": False}
            if ey and em:
                kwargs["exercise_year"] = ey
                kwargs["exercise_month"] = em
            opts = api.query_options(underlying, **kwargs)
        except Exception as e:
            row["状态"] = f"查询期权失败: {e}"
            product_data[pid] = {"row": row}
            continue

        if not opts:
            row["状态"] = "无期权合约"
            product_data[pid] = {"row": row}
            continue

        df_opts = safe_query_symbol_info(api, list(opts))
        if df_opts is None or df_opts.empty:
            row["状态"] = "期权信息查询为空"
            product_data[pid] = {"row": row}
            continue

        # 从 DataFrame 选 CALL/PUT 虚值档(本地过滤, 不再重复 API 调用)
        c_ids, _, _ = _pick_otm_from_df(df_opts, underlying_price, "CALL", call_levels)
        p_ids, _, _ = _pick_otm_from_df(df_opts, underlying_price, "PUT", put_levels)

        symbols = [s for s in (c_ids + p_ids) if s]
        if not symbols:
            row["状态"] = "无虚值期权"
            product_data[pid] = {"row": row}
            continue

        all_option_symbols.update(symbols)

        # 从 df_opts 复用到期天数等信息(不再重复 safe_query_symbol_info)
        erd = None
        opt_exchange = None
        expire_dt = None
        df_sel = df_opts[df_opts["instrument_id"].isin(symbols)]
        if not df_sel.empty:
            erd_series = df_sel["expire_rest_days"].dropna()
            erd_series = erd_series[erd_series >= 0]
            if not erd_series.empty:
                erd = int(erd_series.iloc[0])
            if ey is None and em is None and "exercise_year" in df_sel.columns:
                ey = _safe_int(df_sel["exercise_year"].dropna())
                em = _safe_int(df_sel["exercise_month"].dropna())
            if "exchange_id" in df_sel.columns:
                exch = df_sel["exchange_id"].dropna()
                if not exch.empty:
                    opt_exchange = str(exch.iloc[0])
            if "expire_datetime" in df_sel.columns:
                dt_series = df_sel["expire_datetime"].dropna()
                if not dt_series.empty:
                    expire_dt = float(dt_series.iloc[0])

        if opt_exchange:
            row["交易所"] = opt_exchange
        if ey and em and not contract_month:
            row["最近到期月份"] = f"{ey % 100:02d}{em:02d}"

        product_data[pid] = {
            "row": row,
            "underlying_price": underlying_price,
            "c_ids": c_ids,
            "p_ids": p_ids,
            "erd": erd,
            "expire_dt": expire_dt,
        }

    # ===== Phase 3: 批量获取所有选中合约报价 =====
    all_syms = list(all_option_symbols)
    if all_syms:
        print(f"批量获取 {len(all_syms)} 个期权合约报价...")
        oq_list = api.get_quote_list(all_syms)
        oq_map = dict(zip(all_syms, oq_list))
    else:
        oq_map = {}

    # ===== Phase 4: 逐品种用预获取数据计算结果 =====
    rows = []
    for pid, info in products.items():
        pd_data = product_data.get(pid, {})
        row = pd_data.get("row")
        if row is None:
            row = _empty_row(columns, info.get("exchange_id", ""), pid, product_names)
            row["状态"] = "数据异常"
            rows.append(row)
            print(f"计算 {pid.upper()} -> 状态: {row['状态']}")
            continue

        # Phase 2 出错的品种(标的价格无效/无期权等)
        if "underlying_price" not in pd_data:
            rows.append(row)
            print(f"计算 {pid.upper()} -> 状态: {row['状态']}")
            continue

        _compute_result(row, pd_data, oq_map, call_levels, put_levels,
                        expire_add_days, expire_near_threshold, expire_near_add,
                        risk_free_rate)
        rows.append(row)
        print(f"计算 {pid.upper()} -> 状态: {row['状态']}")

    return rows


def _compute_result(row, pd_data, oq_map, call_levels, put_levels,
                    expire_add_days, expire_near_threshold, expire_near_add,
                    risk_free_rate):
    """用预获取的报价数据填充行结果(IV/权利金/状态)。"""
    c_ids = pd_data["c_ids"]
    p_ids = pd_data["p_ids"]
    underlying_price = pd_data["underlying_price"]
    erd = pd_data.get("erd")
    expire_dt = pd_data.get("expire_dt")

    def _price(sym):
        if not sym:
            return None
        q = oq_map.get(sym)
        if q is None or not _valid_price(q.last_price):
            return None
        return q.last_price

    c_prices = [_price(s) for s in c_ids]
    p_prices = [_price(s) for s in p_ids]

    # 填充合约/价格列
    for i, (sid, sp) in enumerate(zip(c_ids, c_prices), 1):
        row[f"C虚{i}合约"] = sid
        row[f"C虚{i}价格"] = sp
    for i, (sid, sp) in enumerate(zip(p_ids, p_prices), 1):
        row[f"P虚{i}合约"] = sid
        row[f"P虚{i}价格"] = sp

    # 异常收集
    missing = []
    for i, (sid, sp) in enumerate(zip(c_ids, c_prices), 1):
        if sid is None:
            missing.append(f"缺C虚{i}")
        elif sp is None:
            missing.append(f"C虚{i}价格无效")
    for i, (sid, sp) in enumerate(zip(p_ids, p_prices), 1):
        if sid is None:
            missing.append(f"缺P虚{i}")
        elif sp is None:
            missing.append(f"P虚{i}价格无效")

    if erd is None:
        missing.append("到期天数无效")
        days = None
    else:
        if erd <= expire_near_threshold:
            days = erd + expire_near_add
        else:
            days = erd + expire_add_days
        row["到期天数"] = days
        if days <= 0:
            missing.append("到期天数异常")

    # IV 计算(TqSdk 3.10.1 不提供 IV, 用 BS 反解)
    now_ts = time.time()
    T = None
    if expire_dt and expire_dt > now_ts:
        T = (expire_dt - now_ts) / (365.0 * 24 * 3600)
    elif days and days > 0:
        T = days / 365.0
    if T and T > 0:
        for i, sid in enumerate(c_ids, 1):
            if sid and c_prices[i - 1] is not None:
                q = oq_map.get(sid)
                strike = q.strike_price if q and _valid_price(q.strike_price) else None
                iv = calc_implied_volatility(c_prices[i - 1], underlying_price,
                                              strike, T, risk_free_rate, "CALL")
                row[f"C虚{i}IV"] = round(iv * 100, 2) if iv is not None else None
        for i, sid in enumerate(p_ids, 1):
            if sid and p_prices[i - 1] is not None:
                q = oq_map.get(sid)
                strike = q.strike_price if q and _valid_price(q.strike_price) else None
                iv = calc_implied_volatility(p_prices[i - 1], underlying_price,
                                              strike, T, risk_free_rate, "PUT")
                row[f"P虚{i}IV"] = round(iv * 100, 2) if iv is not None else None

    # 平均IV
    ivs = []
    for i in range(1, call_levels + 1):
        v = row.get(f"C虚{i}IV")
        if v is not None: ivs.append(v)
    for i in range(1, put_levels + 1):
        v = row.get(f"P虚{i}IV")
        if v is not None: ivs.append(v)
    row["平均IV"] = round(sum(ivs) / len(ivs), 2) if ivs else None

    prices = c_prices + p_prices
    if all(p is not None for p in prices) and days and days > 0:
        total = sum(prices)
        final = (total / 2.0) / days
        row["四张合约合计"] = total
        row["最终结果"] = final
        row["状态"] = "OK" if not missing else "、".join(missing)
    else:
        row["状态"] = "、".join(missing) if missing else "数据不足"
