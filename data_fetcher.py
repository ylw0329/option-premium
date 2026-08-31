# -*- coding: utf-8 -*-
"""TqSdk 3.10.1 行情与合约查询封装。

仅封装本程序所需的薄薄一层：
- create_api: 创建 TqApi（默认 _stock=True, 支持 query_options/query_atm_options/query_symbol_info）
- get_quote_ready: 获取行情并等待 last_price 就绪
- wait_for_last_price: 等待一组 quote 的 last_price 全部有效
- get_underlying_price: 取标的价格（last_price 优先, NaN 时回退 pre_settlement/pre_close/open）
- safe_query_symbol_info: 分批 query_symbol_info, 避免一次传入过多合约
"""
import math
import os
import time

import pandas as pd
from tqsdk import TqApi, TqAuth

# 认证凭据: 优先环境变量(GitHub Actions 用 Secrets), 回退本地 auth_config.py(被 gitignore)
AUTH_USER = os.environ.get("TQ_USER", "")
AUTH_PASSWORD = os.environ.get("TQ_PASSWORD", "")
if not AUTH_USER or not AUTH_PASSWORD:
    try:
        from auth_config import AUTH_USER as _U, AUTH_PASSWORD as _P
        AUTH_USER, AUTH_PASSWORD = _U, _P
    except ImportError:
        pass


def create_api() -> TqApi:
    """创建并返回 TqApi 实例。

    默认 _stock=True, query_options/query_atm_options/query_symbol_info 均可用。
    账号优先从环境变量 TQ_USER/TQ_PASSWORD 读取(GitHub Actions),
    否则从本地 auth_config.py 读取(被 .gitignore 忽略)。
    """
    if not AUTH_USER or not AUTH_PASSWORD:
        raise RuntimeError("未配置 TqSdk 凭据: 请设置环境变量 TQ_USER/TQ_PASSWORD 或创建 auth_config.py")
    return TqApi(auth=TqAuth(AUTH_USER, AUTH_PASSWORD))


def _is_valid_price(value) -> bool:
    """price 是否有效（非 NaN）。"""
    try:
        return not math.isnan(value)
    except (TypeError, ValueError):
        return False


def wait_for_last_price(api: TqApi, quotes, max_seconds: int = 30) -> bool:
    """等待一组 quote 的 last_price 全部有效。

    Returns:
        True 若全部就绪; False 若超时仍存在无效值。
    """
    quotes = [q for q in quotes if q is not None]
    if not quotes:
        return False
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        if all(_is_valid_price(q.last_price) for q in quotes):
            return True
        # 单次等待最长 5 秒, 避免某品种卡死整个程序
        api.wait_update(deadline=min(deadline, time.time() + 5))
    return all(_is_valid_price(q.last_price) for q in quotes)


def get_quote_ready(api: TqApi, symbol: str, max_seconds: int = 60):
    """获取指定合约行情。

    get_quote 内部已同步等待行情初始化完成(_task.done), 返回时字段已填值;
    last_price 若为 NaN 是合约本身无最新成交价(非交易时段/无流动性), 等待无意义。
    max_seconds 参数仅为兼容签名保留, 实际由 get_quote 内部超时控制(30s)。
    """
    return api.get_quote(symbol)


# 标的价格候选字段（按优先级）
_UNDERLYING_PRICE_FIELDS = ("last_price", "pre_settlement", "pre_close", "open", "close")


def get_underlying_price(quote) -> tuple:
    """取标的价格: last_price 优先, NaN 时依次回退 pre_settlement/pre_close/open/close。

    query_atm_options 的 underlying_price 官方文档明确"可以是任意值"。

    Returns:
        (price, field_name) 或 (None, None)
    """
    for field in _UNDERLYING_PRICE_FIELDS:
        v = getattr(quote, field, None)
        if _is_valid_price(v):
            return v, field
    return None, None


def safe_query_symbol_info(api: TqApi, symbols, chunk_size: int = 500) -> pd.DataFrame:
    """分批 query_symbol_info, 返回合并后的 DataFrame。

    TqSymbolDataFrame 的 instrument_id 是普通列（非索引）, 可直接用 df["instrument_id"] 访问。
    """
    if not symbols:
        return pd.DataFrame()
    frames = []
    for i in range(0, len(symbols), chunk_size):
        chunk = list(symbols[i:i + chunk_size])
        df = api.query_symbol_info(chunk)
        frames.append(df)
    if len(frames) == 1:
        return frames[0]
    return pd.concat(frames, ignore_index=True)
