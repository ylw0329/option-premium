# -*- coding: utf-8 -*-
"""虚值档位选取 + 权利金指标计算 + 异常标记（TqSdk 3.10.1）。

### 虚值档位选择：为何不直接用 query_atm_options(price_level=[-1,-2])

TqSdk 3.10.1 的 `query_atm_options` 文档第 3 条 tie-break 规则：
> "距离相等时取**虚值**行权价作 ATM"

当标的价格正好落在两档中间（非常常见，比如昨结算价刚好居中），官方会把"虚值 1 档"的行权价当作 ATM，
进而 price_level=-1 对应的就是用户眼里的"虚值 2 档"（CALL 表现尤为明显）。
PUT 同理存在相同偏档问题，只是行情若没踩中 tie-break 点不易察觉。

为了让结果与行业常规完全一致（用户描述的 CALL 第一/二档 OTM），本模块**自行分类 OTM1..OTMN**：
1. 用 `query_options` 取全部同月份同方向期权
2. `query_symbol_info` 取回 strike_price
3. 按「距离标的价格差最小者作 ATM；若存在两个档等距，CALL 取**较低行权价**（实值倾向）作 ATM，PUT 取**较高行权价**（实值倾向）作 ATM」
   —— 这对应行业常规的 tie-break 语义，也让 OTM1/OTM2.. 与用户主观感受一致
4. 按排序方向在 ATM 后取 N 档即为 OTM1..OTMN

### 档位数可配置

call_levels / put_levels 由 config.json 指定，默认 2。C 和 P 可分别配置不同档数。

### 核心公式（不变）:
  Result = (ΣC_i + ΣP_i) / (2 * (expire_rest_days + 1))
  分母 2 固定，表示 C/P 两方向之和的平均；与档位数无关。
"""
import math
import time

from data_fetcher import get_quote_ready, get_underlying_price, safe_query_symbol_info


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
    # 品种列显示 "代码：中文名"(若有中文名), 便于后续用冒号分割解析
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
    """用 Black-Scholes + 二分法反解隐含波动率。

    TqSdk 3.10.1 不提供 IV 字段, 需自行反解。
    返回年化波动率(小数, 如 0.25 表示 25%) 或 None。
    """
    if option_price is None or spot is None or strike is None or T is None:
        return None
    if T <= 0 or spot <= 0 or strike <= 0 or option_price <= 0:
        return None
    # 内在价值检查: 期权价格不应低于内在价值(贴现行权价)
    disc_strike = strike * math.exp(-r * T)
    if option_class == "CALL":
        intrinsic = max(spot - disc_strike, 0.0)
    else:
        intrinsic = max(disc_strike - spot, 0.0)
    if option_price < intrinsic - 1e-6:
        return None
    # 二分法: BS 价格随 vol 单调递增
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


def _pick_otm_options(api, underlying_symbol: str, underlying_price,
                      option_class: str, levels: int,
                      exercise_year=None, exercise_month=None):
    """按行业常规定义返回同方向 OTM1..OTMN 合约列表。

    Returns:
        (otm_ids: list[str|None], atm_strike, strikes_list)
    """
    try:
        kwargs = {"option_class": option_class, "expired": False}
        if exercise_year and exercise_month:
            kwargs["exercise_year"] = int(exercise_year)
            kwargs["exercise_month"] = int(exercise_month)
        opts = api.query_options(underlying_symbol, **kwargs)
    except Exception:
        return [None] * levels, None, []
    if not opts:
        return [None] * levels, None, []
    df = safe_query_symbol_info(api, list(opts))
    if df is None or df.empty:
        return [None] * levels, None, []
    df = df.dropna(subset=["strike_price", "instrument_id"])
    if df.empty:
        return [None] * levels, None, []

    # 行权价升序去重
    strikes = sorted(df["strike_price"].astype(float).unique().tolist())
    if not strikes:
        return [None] * levels, None, []

    # 直接按虚值定义选档, 不经过 ATM 分界(避免"最近 strike 落在虚值方向"时整体偏一档):
    #   CALL 虚值 = strike > spot, 取最小的 N 个(即最接近 spot 的虚值在前)
    #   PUT  虚值 = strike < spot, 取最大的 N 个
    if option_class == "CALL":
        otm_strikes = [s for s in strikes if s > underlying_price][:levels]
    else:
        otm_strikes = [s for s in reversed(strikes) if s < underlying_price][:levels]

    # strike -> instrument_id
    strike_to_id = {}
    for _, r in df.iterrows():
        s = float(r["strike_price"])
        if s not in strike_to_id:
            strike_to_id[s] = r["instrument_id"]

    otm_ids = [strike_to_id.get(s) for s in otm_strikes]
    # 不足 N 档补 None
    while len(otm_ids) < levels:
        otm_ids.append(None)

    # ATM 仅用于诊断, 取最接近 spot 的 strike
    atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))
    return otm_ids, atm_strike, strikes


def calc_product(api, columns, call_levels: int, put_levels: int,
                 product_id: str, underlying_symbol: str, exchange_id: str,
                 product_names: dict = None,
                 expire_add_days: int = 1, expire_near_threshold: int = 0, expire_near_add: int = 0,
                 risk_free_rate: float = 0.025,
                 exercise_year=None, exercise_month=None) -> dict:
    """计算单品种的虚值前 N 档权利金指标。单品种异常不向上抛出。

    到期天数规则:
    - 默认: days = expire_rest_days + expire_add_days
    - 当 expire_rest_days <= expire_near_threshold 时: days = expire_rest_days + expire_near_add (替换, 非叠加)
    """
    row = _empty_row(columns, exchange_id, product_id, product_names, exercise_year, exercise_month)
    try:
        # 1. 标的价格
        uq = get_quote_ready(api, underlying_symbol, max_seconds=60)
        underlying_price, price_field = get_underlying_price(uq)
        if underlying_price is None:
            print(f"  [{product_id.upper()}] {underlying_symbol} 行情无效: "
                  f"last_price={uq.last_price} pre_settle={uq.pre_settlement} "
                  f"pre_close={uq.pre_close} open={uq.open}")
            row["状态"] = "标的价格无效"
            return row
        if price_field != "last_price":
            print(f"  [{product_id.upper()}] {underlying_symbol} last_price 为 NaN, "
                  f"回退使用 {price_field}={underlying_price}")
        row["标的价格"] = underlying_price

        # 2. 虚值前 N 档（自行按行业常规定义分类，绕开 query_atm_options 的 tie-break 偏档 bug）
        ey = int(exercise_year) if exercise_year else None
        em = int(exercise_month) if exercise_month else None
        c_ids, _, _ = _pick_otm_options(api, underlying_symbol, underlying_price, "CALL", call_levels, ey, em)
        p_ids, _, _ = _pick_otm_options(api, underlying_symbol, underlying_price, "PUT", put_levels, ey, em)

        symbols = [s for s in (c_ids + p_ids) if s]
        if not symbols:
            row["状态"] = "无虚值期权"
            return row

        # 3. 期权合约信息(到期天数/月份/交易所/到期时间戳)
        df = safe_query_symbol_info(api, symbols)
        erd = None
        opt_exchange = None
        expire_dt = None
        if df is not None and not df.empty:
            erd_series = df["expire_rest_days"].dropna()
            erd_series = erd_series[erd_series >= 0]
            if not erd_series.empty:
                erd = int(erd_series.iloc[0])
            if ey is None and em is None:
                ey = _safe_int(df["exercise_year"].dropna())
                em = _safe_int(df["exercise_month"].dropna())
            exch = df["exchange_id"].dropna()
            if not exch.empty:
                opt_exchange = str(exch.iloc[0])
            # 取到期时间戳(秒级), 用于 IV 精确年化
            dt_series = df["expire_datetime"].dropna()
            if not dt_series.empty:
                expire_dt = float(dt_series.iloc[0])
        if opt_exchange:
            row["交易所"] = opt_exchange
        if ey and em:
            row["最近到期月份"] = f"{ey % 100:02d}{em:02d}"

        # 4. 期权最新价(批量订阅, get_quote_list 返回时已初始化完成, 无需再 wait)
        quote_list = api.get_quote_list(symbols)
        quotes = dict(zip(symbols, quote_list))

        def _price(sym):
            if not sym:
                return None
            q = quotes.get(sym)
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

        # 5. 异常收集
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
            # 到期天数: 默认 erd + expire_add_days;
            # 当 erd <= expire_near_threshold 时用 erd + expire_near_add 替换(非叠加)
            if erd <= expire_near_threshold:
                days = erd + expire_near_add
            else:
                days = erd + expire_add_days
            row["到期天数"] = days
            if days <= 0:
                missing.append("到期天数异常")

        # 计算每个期权的隐含波动率(TqSdk 3.10.1 不提供 IV, 用 BS 反解)
        # T 精确到秒: (到期时间戳 - 当前时间戳) / (365*24*3600)
        now_ts = time.time()
        T = None
        if expire_dt and expire_dt > now_ts:
            T = (expire_dt - now_ts) / (365.0 * 24 * 3600)
        elif days and days > 0:
            T = days / 365.0
        if T and T > 0:
            for i, sid in enumerate(c_ids, 1):
                if sid and c_prices[i - 1] is not None:
                    q = quotes.get(sid)
                    strike = q.strike_price if q and _valid_price(q.strike_price) else None
                    iv = calc_implied_volatility(c_prices[i - 1], underlying_price,
                                                  strike, T, risk_free_rate, "CALL")
                    row[f"C虚{i}IV"] = round(iv, 4) if iv is not None else None
            for i, sid in enumerate(p_ids, 1):
                if sid and p_prices[i - 1] is not None:
                    q = quotes.get(sid)
                    strike = q.strike_price if q and _valid_price(q.strike_price) else None
                    iv = calc_implied_volatility(p_prices[i - 1], underlying_price,
                                                  strike, T, risk_free_rate, "PUT")
                    row[f"P虚{i}IV"] = round(iv, 4) if iv is not None else None

        # 计算平均隐含波动率: 所有 C 虚*IV 和 P 虚*IV 的算术平均(过滤 None)
        ivs = []
        for i in range(1, call_levels + 1):
            v = row.get(f"C虚{i}IV")
            if v is not None: ivs.append(v)
        for i in range(1, put_levels + 1):
            v = row.get(f"P虚{i}IV")
            if v is not None: ivs.append(v)
        row["平均IV"] = round(sum(ivs) / len(ivs), 4) if ivs else None

        prices = c_prices + p_prices
        if all(p is not None for p in prices) and days and days > 0:
            total = sum(prices)
            final = (total / 2.0) / days
            row["四张合约合计"] = total
            row["最终结果"] = final
            row["状态"] = "OK" if not missing else "、".join(missing)
        else:
            row["状态"] = "、".join(missing) if missing else "数据不足"
        return row

    except Exception as e:
        row["状态"] = f"计算异常: {type(e).__name__}: {e}"
        return row


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
    """对全部品种逐个计算。products 已按期望顺序排好。"""
    rows = []
    for pid, info in products.items():
        print(f"计算 {str(pid).upper()} ...")
        row = calc_product(api, columns, call_levels, put_levels, pid,
                           info.get("underlying", ""),
                           info.get("exchange_id", ""),
                           product_names,
                           expire_add_days, expire_near_threshold, expire_near_add,
                           risk_free_rate,
                           info.get("exercise_year"),
                           info.get("exercise_month"))
        rows.append(row)
        print(f"  -> 状态: {row['状态']}")
    return rows
