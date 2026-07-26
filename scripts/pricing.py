"""模型官方价格表与成本折算。

口径：USD / 百万 token。人民币标价按 1 USD = 7.2 CNY 换算（与 qoder-cost-compare 一致）。
缓存价 = 缓存命中（cache read）单价；不支持隐式缓存的模型按标准输入价计。
cache_creation（缓存写入）统一按输入价计（简化，误差可忽略）。

价格变动时升 PRICE_VERSION，历史结果不重算。
"""
from __future__ import annotations

import argparse

PRICE_VERSION = "2026-07-v1"
CNY_PER_USD = 7.2

# 原始标价：Ultimate/Performance 为 USD/M，其余为 CNY/M（百炼刊例）
_RAW_TABLE = {
    "ultimate":         {"input": (5.0, "USD"),  "output": (25.0, "USD"),  "cache": (0.50, "USD")},
    "performance":      {"input": (5.0, "USD"),  "output": (30.0, "USD"),  "cache": (0.50, "USD")},
    "deepseek-v4-pro":  {"input": (12.0, "CNY"), "output": (24.0, "CNY"), "cache": (2.4, "CNY")},
    "deepseek-v4-flash": {"input": (1.0, "CNY"), "output": (2.0, "CNY"),  "cache": (0.2, "CNY")},
    "qwen3.7-max":      {"input": (12.0, "CNY"), "output": (36.0, "CNY"), "cache": (2.4, "CNY")},
    "qwen3.7-plus":     {"input": (2.0, "CNY"),  "output": (8.0, "CNY"),  "cache": (0.4, "CNY")},
    "glm-5.2":          {"input": (8.0, "CNY"),  "output": (28.0, "CNY"), "cache": (1.6, "CNY")},
    "minimax-m3":       {"input": (4.2, "CNY"),  "output": (16.8, "CNY"), "cache": (0.84, "CNY")},
    # 百炼侧不支持隐式缓存，缓存价 = 输入价
    "kimi-k2.7-code":   {"input": (6.5, "CNY"),  "output": (27.0, "CNY"), "cache": (6.5, "CNY")},
}

# 模型名别名（大小写不敏感）→ 价格表 key
ALIASES = {
    "ultimate": "ultimate",
    "claude-opus-4-8": "ultimate",
    "performance": "performance",
    "gpt-5.5": "performance",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "qwen3.7-max": "qwen3.7-max",
    "qwen3.7-plus": "qwen3.7-plus",
    "glm-5.2": "glm-5.2",
    "minimax-m3": "minimax-m3",
    "kimi-k2.7-code": "kimi-k2.7-code",
    # 本机各 CLI 日志中出现的模型 id → 最近价格档（如价差大请补独立条目）
    "qwen3.8-max-preview": "qwen3.7-max",
    "glm-5.1": "glm-5.2",
    "kimi-code/k3": "kimi-k2.7-code",
    "kimi-for-coding": "kimi-k2.7-code",
    "kimi-code/kimi-for-coding": "kimi-k2.7-code",
}


def _to_usd_per_m(value: float, currency: str) -> float:
    return value if currency == "USD" else value / CNY_PER_USD


TABLE: dict[str, dict[str, float]] = {
    key: {part: _to_usd_per_m(v, c) for part, (v, c) in parts.items()}
    for key, parts in _RAW_TABLE.items()
}


def resolve_model(model: str) -> str | None:
    """把用户填的模型名归一到价格表 key；未知返回 None。"""
    key = model.strip().lower()
    if key in TABLE:
        return key
    return ALIASES.get(key)


def price_parts(model: str) -> dict[str, float] | None:
    key = resolve_model(model)
    return TABLE.get(key) if key else None


def cost_usd(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
) -> float | None:
    """按官方价折算 USD；未知模型返回 None。"""
    parts = price_parts(model)
    if parts is None:
        return None
    usd = (
        input_tokens * parts["input"]
        + cache_read_tokens * parts["cache"]
        + cache_creation_tokens * parts["input"]
        + output_tokens * parts["output"]
    ) / 1_000_000
    return round(usd, 6)


def cost_breakdown(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
) -> dict | None:
    """返回逐段成本明细（token × 单价 = 小计），供报告展开展示以核验单价。

    每行含：项目、token 数、原始标价（CNY/USD per M）、换算后 USD/M 单价、小计 USD。
    未知模型返回 None。
    """
    key = resolve_model(model)
    if key is None:
        return None
    parts = TABLE[key]
    raw = _RAW_TABLE[key]

    def line(label: str, tokens: int, usd_per_m: float, raw_part) -> dict:
        raw_val, raw_cur = raw_part
        return {
            "label": label,
            "tokens": tokens,
            "raw_price": raw_val,
            "raw_currency": raw_cur,
            "usd_per_m": round(usd_per_m, 6),
            "subtotal_usd": round(tokens * usd_per_m / 1_000_000, 6),
        }

    lines = [
        line("输入(非缓存)", input_tokens, parts["input"], raw["input"]),
        line("缓存命中", cache_read_tokens, parts["cache"], raw["cache"]),
        line("输出", output_tokens, parts["output"], raw["output"]),
    ]
    if cache_creation_tokens:
        # 缓存写入按输入价计
        lines.append(line("缓存写入", cache_creation_tokens, parts["input"], raw["input"]))
    total = round(sum(ln["subtotal_usd"] for ln in lines), 6)
    return {
        "model_key": key,
        "price_version": PRICE_VERSION,
        "cny_per_usd": CNY_PER_USD,
        "lines": lines,
        "total_usd": total,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="按价格表折算成本")
    ap.add_argument("--model", required=True)
    ap.add_argument("--input", type=int, default=0, help="非缓存输入 token")
    ap.add_argument("--cache", type=int, default=0, help="缓存命中 token")
    ap.add_argument("--output", type=int, default=0)
    args = ap.parse_args()
    cost = cost_usd(args.model, args.input, args.cache, args.output)
    if cost is None:
        print(f"未知模型: {args.model}（价格表 version={PRICE_VERSION}）")
        return 1
    print(f"price_version={PRICE_VERSION} model={resolve_model(args.model)} cost_usd={cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
