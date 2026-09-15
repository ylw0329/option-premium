# -*- coding: utf-8 -*-
"""把最新的 option_premium_result_*.csv 转成 docs/results.json 供网页前端读取。

由 GitHub Actions 在 main.py 之后运行。
也可本地手动运行: python generate_web_data.py

额外功能: 把当天数据追加到 docs/history.json(按日期索引), 供网页历史趋势查看。
"""
import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

HISTORY_DIR = os.path.join("docs", "history")
RESULTS_JSON = os.path.join("docs", "results.json")
HISTORY_JSON = os.path.join("docs", "history.json")


def find_latest_csv() -> str:
    """找到最新的结果 CSV 文件(在 docs/history/ 下)。"""
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, "option_premium_result_*.csv")))
    if not files:
        return ""
    return files[-1]


def append_to_history(run_date: str, data: list) -> None:
    """把当天数据追加到 docs/history.json。

    history.json 结构: {"2026-09-15": [品种行数组], "2026-09-14": [...]}
    同一日期重复运行会覆盖更新, 不重复追加。
    """
    if not run_date:
        return
    history = {}
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, dict):
                history = {}
        except (json.JSONDecodeError, OSError):
            history = {}
    history[run_date] = data
    # 按日期倒序排列(最新在前), 便于前端读取
    history = dict(sorted(history.items(), key=lambda kv: kv[0], reverse=True))
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"已追加历史数据 {run_date} -> {HISTORY_JSON} (共 {len(history)} 天)")


def main():
    csv_path = find_latest_csv()
    if not csv_path:
        print("未找到 option_premium_result_*.csv, 跳过生成网页数据")
        return

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    # 列名顺序即表格列顺序
    columns = df.columns.tolist()
    # NaN -> null: 先转 object 再替换, 否则 float 列的 None 会被 pandas 转回 NaN,
    # json.dump 会输出非法 JSON 字面量 NaN, 导致浏览器 JSON.parse 失败
    data = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")

    # 从文件名提取日期(如 option_premium_result_20260831.csv -> 2026-08-31)
    fname = os.path.basename(csv_path)
    run_date = ""
    if len(fname) >= 23:
        d = fname[-12:-4]  # 20260831
        if len(d) == 8 and d.isdigit():
            run_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    result = {
        "generated_at": datetime.now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "run_date": run_date,
        "columns": columns,
        "data": data,
    }

    os.makedirs("docs", exist_ok=True)
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"已生成 {RESULTS_JSON} ({len(data)} 行数据)")

    # 追加到历史汇总
    append_to_history(run_date, data)


if __name__ == "__main__":
    main()
