# -*- coding: utf-8 -*-
"""把最新的 option_premium_result_*.csv 转成 docs/results.json 供网页前端读取。

由 GitHub Actions 在 main.py 之后运行。
也可本地手动运行: python generate_web_data.py
"""
import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


def find_latest_csv() -> str:
    """找到最新的结果 CSV 文件。"""
    files = sorted(glob.glob("option_premium_result_*.csv"))
    if not files:
        return ""
    return files[-1]


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
    out_path = os.path.join("docs", "results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"已生成 {out_path} ({len(data)} 行数据)")


if __name__ == "__main__":
    main()
