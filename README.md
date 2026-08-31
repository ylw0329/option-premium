# 期权虚值前两档权利金计算程序 (TqSdk 3.10.1)

一次性获取中国期货及股指期权行情，计算每个期权品种最近一个未到期月份的"虚值前两档期权权利金指标"。

## 公式

```
Result = (C_OTM1 + C_OTM2 + P_OTM1 + P_OTM2) / (2 × (ExpireRestDays + 1))
```

- C_OTM1/C_OTM2：CALL 虚值第 1/2 档最新成交价
- P_OTM1/P_OTM2：PUT 虚值第 1/2 档最新成交价
- ExpireRestDays：TqSdk 返回的剩余到期自然日天数；`+1` 为本程序规定

## 环境依赖

```bash
pip install -r requirements.txt
```

固定 `tqsdk==3.10.1`，另需 `pandas`、`openpyxl`。

## 运行

```bash
python main.py
```

- 控制台输出表格
- 保存 `option_premium_result_YYYYMMDD.xlsx`

## 文件结构

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：编排流程、输出表格与 Excel |
| `config.json` | 商品期权兜底列表 + 股指期权指数标的映射 |
| `data_fetcher.py` | TqApi 封装：行情就绪、分批 query_symbol_info |
| `option_discovery.py` | 自动发现商品期权品种、选择最近未到期月份（商品与股指） |
| `option_calculator.py` | 虚值档位选取、公式计算、异常标记 |
| `requirements.txt` | 依赖 |

## 关键实现（基于 TqSdk 3.10.1 源码核对）

- **虚值档位**：`api.query_atm_options(underlying, price, [-1, -2], "CALL"/"PUT")`，`price_level=-1` 虚1、`-2` 虚2；缺失档位返回 `None`。
- **股指期权**：IO→`SSE.000300`、MO→`SSE.000852`、HO→`SSE.000016`，须传 `exercise_year`/`exercise_month`。
- **最近月份**：基于 `query_symbol_info` 的 `last_exercise_datetime`/`expire_rest_days` 选择，不靠合约代码字符串排序。
- **到期天数**：`expire_rest_days + 1`。
- **价格**：`quote.last_price`。

## 配置说明

`config.json`：

- `commodity_options`：仅当自动发现失败时作为兜底列表（大小写不敏感）。可自行增删。
- `index_options`：股指期权品种 → 指数标的映射，可调整。

认证凭据写于 `data_fetcher.py`（沿用 `get_copper_price.py`）。

## 异常处理

单品种异常不中断程序，在表格 `状态` 列标记：`OK`、`缺P虚2`、`C虚1价格无效`、`到期天数无效`、`无虚值期权`、`计算异常: ...` 等。
