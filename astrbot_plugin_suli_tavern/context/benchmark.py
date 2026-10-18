"""Context Runtime 性能基准测试。

用于验证:
  1. Persona Kernel 编译耗时
  2. IntentRouter 路由耗时
  3. Token Budget 计算耗时
  4. RuntimeContext 组装耗时

运行方式:
  python -m astrbot_plugin_suli_tavern.context.benchmark
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

# ── 加载角色卡 ──
_CHAR_DIR = Path(__file__).resolve().parent.parent / "characters"


def _load_character(name: str) -> dict:
    """加载角色卡。"""
    path = _CHAR_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"角色卡不存在: {path}")
    with open(path, encoding="utf-8") as f:
        card = json.load(f)
    return card.get("data", card)


def benchmark_persona_kernel():
    """测试 Persona Kernel 编译性能。"""
    from .context_runtime import compile_persona_kernel

    print("=" * 60)
    print("Persona Kernel 编译基准测试")
    print("=" * 60)

    for name in ["loput", "luna", "moon"]:
        try:
            char = _load_character(name)
        except FileNotFoundError:
            print(f"  {name}: 跳过 (文件不存在)")
            continue

        sp = char.get("system_prompt", "")
        print(f"\n{name}:")
        print(f"  原始 system_prompt: {len(sp)} chars (~{len(sp)//1.5:.0f} tokens)")

        # 测试编译耗时
        times = []
        for _ in range(10):
            start = time.perf_counter()
            kernel = compile_persona_kernel(char, max_tokens=2500)
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        avg_ms = sum(times) / len(times)
        print(f"  Kernel 编译: {avg_ms:.2f} ms (avg of 10)")
        print(f"  Kernel 大小: ~{kernel.token_estimate} tokens")
        print(f"  压缩率: {(1 - kernel.token_estimate / (len(sp)//1.5)) * 100:.1f}%")


def benchmark_intent_router():
    """测试 IntentRouter 路由性能。"""
    from ..intelligence.intent_router import IntentRouter, RouteType

    print("\n" + "=" * 60)
    print("IntentRouter 路由基准测试")
    print("=" * 60)

    router = IntentRouter()

    test_cases = [
        ("早上好", RouteType.FAST),
        ("好的", RouteType.FAST),
        ("拜拜", RouteType.FAST),
        ("为什么猫会喵喵叫？", RouteType.THINK),
        ("帮我分析一下这个代码", RouteType.THINK),
        ("搜索一下最近的新闻", RouteType.AGENT),
        ("画一只猫", RouteType.AGENT),
        ("[图片]", RouteType.IGNORE),
        ("今天天气真好，你有什么计划吗？", RouteType.FAST),  # 短消息走 FAST
        ("请帮我解释一下量子计算的原理，并比较它与经典计算的区别", RouteType.THINK),
    ]

    times = []
    correct = 0
    for msg, expected in test_cases:
        start = time.perf_counter()
        decision = router.route(msg)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

        is_correct = decision.route == expected
        correct += is_correct
        status = "✓" if is_correct else "✗"
        print(f"  {status} '{msg[:20]}...' → {decision.route.value} (expected: {expected.value})")

    avg_ms = sum(times) / len(times)
    accuracy = correct / len(test_cases) * 100
    print(f"\n  平均路由耗时: {avg_ms:.2f} ms")
    print(f"  准确率: {accuracy:.1f}% ({correct}/{len(test_cases)})")


def benchmark_context_runtime():
    """测试 ContextRuntimeEngine 组装性能。"""
    from .context_runtime import BUDGET_FAST_CHAT, get_context_runtime

    print("\n" + "=" * 60)
    print("ContextRuntimeEngine 组装基准测试")
    print("=" * 60)

    engine = get_context_runtime()

    for name in ["loput", "luna"]:
        try:
            char = _load_character(name)
        except FileNotFoundError:
            print(f"  {name}: 跳过 (文件不存在)")
            continue

        # 模拟上下文
        recent_messages = [
            {"user_name": "User", "content": f"测试消息 {i}", "user_id": "123456"}
            for i in range(10)
        ]

        times = []
        for _ in range(10):
            start = time.perf_counter()
            ctx = engine.build_runtime_context(
                character=char,
                bot_id=(os.getenv("BOT_QQ_MAIN", "") or os.getenv("BOT_ID_MAIN", "") or "BOT_QQ"),
                user_id="123456",
                group_id="1045088761",
                budget=BUDGET_FAST_CHAT,
                recent_messages=recent_messages,
                memory_hints="用户喜欢猫",
                world_book_entries=["世界设定1"],
                current_mood="warm",
                relationship_level=3,
            )
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        avg_ms = sum(times) / len(times)
        print(f"\n{name}:")
        print(f"  组装耗时: {avg_ms:.2f} ms (avg of 10)")
        print(f"  static_prefix: {len(ctx.static_prefix)} chars (~{len(ctx.static_prefix)//1.5:.0f} tokens)")
        print(f"  dynamic_suffix: {len(ctx.dynamic_suffix)} chars (~{len(ctx.dynamic_suffix)//1.5:.0f} tokens)")
        print(f"  总预算: ~{ctx.budget_used} tokens")


def main():
    """运行所有基准测试。"""
    print("Context Runtime 性能基准测试")
    print("=" * 60)

    benchmark_persona_kernel()
    benchmark_intent_router()
    benchmark_context_runtime()

    print("\n" + "=" * 60)
    print("基准测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
