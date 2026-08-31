# -*- coding: utf-8 -*-
"""TqSdk 3.10.1 期权虚值前两档权利金计算 - 入口。

流程:
1. 加载 config.json
2. 初始化 TqApi
3. 自动发现商品期权品种(失败则用 config 兜底)
4. 并入 IO/MO/HO(选最近未到期月份)
5. 逐品种计算虚值前两档权利金指标
6. 输出表格(控制台 + Excel)
"""
import datetime
import json
import os

import pandas as pd

from data_fetcher import create_api
from option_calculator import build_columns, calculate_all
from option_discovery import (discover_commodity_options, discover_via_config,
                               select_nearest_index_month)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("commodity_options", [])
    cfg.setdefault("index_options", {})
    cfg.setdefault("call_levels", 2)
    cfg.setdefault("put_levels", 2)
    cfg.setdefault("product_names", {})
    cfg.setdefault("risk_free_rate", 0.0115)
    cfg.setdefault("expire_add_days", 1)
    cfg.setdefault("expire_near_threshold", 0)
    cfg.setdefault("expire_near_add", 0)
    cfg.setdefault("expire_close_hour", 15)
    cfg.setdefault("expire_close_minute", 0)
    return cfg


def build_products(api, config: dict) -> dict:
    """构建待计算品种表: {product_id: {underlying, exercise_year, exercise_month, exchange_id}}。"""
    ch, cm = config["expire_close_hour"], config["expire_close_minute"]
    # 商品期权: 自动发现为主
    products = discover_commodity_options(api, ch, cm)
    if not products:
        print("自动发现商品期权为空, 回退到 config.json 列表")
        products = discover_via_config(api, config["commodity_options"], ch, cm)
    print(f"商品期权品种数: {len(products)} -> {sorted(products.keys())}")

    # 股指期权 IO/MO/HO(固定并入)
    for prod, index_symbol in config["index_options"].items():
        pid = str(prod).strip().lower()
        ym = select_nearest_index_month(api, index_symbol, ch, cm)
        if ym is None:
            print(f"股指期权 {prod.upper()} 未找到未到期月份, 跳过")
            continue
        ey, em = ym
        products[pid] = {
            "underlying": index_symbol,
            "exercise_year": ey,
            "exercise_month": em,
            "exchange_id": "CFFEX",
        }
        print(f"股指期权 {prod.upper()} -> 最近月份 {ey % 100:02d}{em:02d}")

    # 按 config 顺序排序: commodity_options + index_options 顺序合并, 不在的按字母序排后
    seq = list(config.get("commodity_options", [])) + list(config.get("index_options", {}).keys())
    order = {str(p).strip().lower(): i for i, p in enumerate(seq)}
    ordered = sorted(products.items(),
                     key=lambda kv: (order.get(kv[0], 10 ** 9), kv[0]))
    return dict(ordered)


def output_results(rows: list, run_date: str, columns: list):
    df = pd.DataFrame(rows, columns=columns)
    # 数值列保留两位小数
    for col in ("四张合约合计", "合计÷2", "最终结果"):
        df[col] = df[col].apply(lambda v: round(v, 2) if isinstance(v, (int, float)) else v)
    # 控制台输出
    with pd.option_context("display.max_rows", None, "display.max_columns", None,
                           "display.width", 200, "display.unicode.east_asian_width", True):
        print("\n========== 期权虚值前两档权利金指标 ==========")
        print(df.to_string(index=False))
        print("=" * 46)
    # Excel(需要 openpyxl); 缺失则回退 CSV
    out_path = f"option_premium_result_{run_date}.xlsx"
    try:
        df.to_excel(out_path, index=False)
        print(f"已保存: {out_path}")
    except ModuleNotFoundError as e:
        if "openpyxl" in str(e).lower():
            csv_path = f"option_premium_result_{run_date}.csv"
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"openpyxl 未安装, 已保存 CSV(可用 Excel 打开): {csv_path}")
            print("如需 xlsx: pip install openpyxl")
        else:
            raise
    except Exception as e:
        csv_path = f"option_premium_result_{run_date}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"保存 Excel 失败({e}), 已回退 CSV: {csv_path}")


def main():
    run_date = datetime.date.today().strftime("%Y%m%d")
    print(f"运行日期: {run_date}")
    config = load_config()

    api = create_api()
    try:
        products = build_products(api, config)
        if not products:
            print("无可计算品种, 退出")
            return
        columns = build_columns(config["call_levels"], config["put_levels"])
        rows = calculate_all(api, products, columns, config["call_levels"], config["put_levels"],
                              config["product_names"],
                              config["expire_add_days"], config["expire_near_threshold"], config["expire_near_add"],
                              config["risk_free_rate"])
        output_results(rows, run_date, columns)
    except Exception as e:
        print(f"程序异常: {type(e).__name__}: {e}")
    finally:
        try:
            api.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
