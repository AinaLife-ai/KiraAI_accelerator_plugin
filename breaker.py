"""熔断器 —— 带**半开恢复**（half-open），不是永久旁路。

用户的质疑（对）："连续失败三次就永久熔断，没有重试复活的机会，会不会太严格？"

**确实太严格了**，而且是设计缺陷：
  - 一次**暂时性**故障（网络抖动、provider 短时 502、用户正巧把 API key 改错了几秒）
    会让接管点**永久失效**，直到用户手动重载插件
  - 更糟的是：用户根本不知道它已经死了（面板只显示"已熔断"，
    但不会说"其实早就可以恢复了"）

## 现在的机制：三态 + 指数退避冷却

```
        失败达到阈值
  CLOSED ──────────────► OPEN
    ▲                     │
    │  试探成功            │ 冷却时间到
    │                     ▼
    └──── HALF_OPEN ◄─────┘
              │
              │ 试探失败（冷却时间翻倍）
              └──────────► OPEN
```

- **CLOSED（正常）**：接管生效，失败计数清零规则不变（一次成功即清零）
- **OPEN（熔断中）**：这段时间走原实现，**但有冷却时间**，到点自动进入 HALF_OPEN
- **HALF_OPEN（试探）**：**放一次调用过去试试**
  - 成功 ⇒ 回到 CLOSED，恢复接管（并写日志 + 面板可见）
  - 失败 ⇒ 回到 OPEN，**冷却时间翻倍**（避免持续抖动）

**冷却退避**：`base * 2^(连续熔断轮数-1)`，上限 `max_cooldown`
默认 base=30s、上限=600s ⇒ 30s → 60s → 120s → 240s → 480s → 600s(封顶)

**为什么这样是对的**：
- 暂时性故障：30 秒后自动恢复，用户无感
- 持续性故障：退避到 10 分钟一次试探，开销可忽略（一次调用而已）
- 永远不会"永久死掉"，除非用户主动关掉这个接管点
"""
from __future__ import annotations

import logging
import time
from enum import Enum

logger = logging.getLogger("kira_accelerator")

# 连续失败这么多次进入熔断
BREAKER_THRESHOLD = 3
# 首次冷却（秒）
BREAKER_BASE_COOLDOWN = 30.0
# 冷却上限（秒）—— 持续故障时最多 10 分钟试探一次
BREAKER_MAX_COOLDOWN = 600.0


class BreakerState(str, Enum):
    CLOSED = "closed"        # 正常：接管生效
    OPEN = "open"            # 熔断中：走原实现，等冷却
    HALF_OPEN = "half_open"  # 试探：放一次过去，看能不能恢复


class Breaker:
    """带半开恢复的熔断器。"""

    def __init__(self, name: str, threshold: int = BREAKER_THRESHOLD,
                 base_cooldown: float = BREAKER_BASE_COOLDOWN,
                 max_cooldown: float = BREAKER_MAX_COOLDOWN):
        self.name = name
        self.threshold = threshold
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown

        self.failures = 0            # 当前这段连续失败次数
        self.state = BreakerState.CLOSED
        self.opened_at = 0.0         # 进入 OPEN 的时刻
        self.cooldown = base_cooldown
        self.trip_count = 0          # 熔断过几轮（用于退避）
        self.recovered_count = 0     # 成功恢复过几次
        self.last_error: BaseException | None = None

    # ── 查询：现在是放行还是旁路？──
    @property
    def tripped(self) -> bool:
        """对外语义保持兼容：只要不是 CLOSED 就算"不在正常接管状态"。

        注意：**返回 True 不代表永久旁路** —— 冷却到点会自动转 HALF_OPEN 试探。
        """
        return self.state is not BreakerState.CLOSED

    def should_bypass(self) -> bool:
        """本次调用是否旁路（走原实现）？

        OPEN 且冷却未到  ⇒ True（旁路）
        OPEN 且冷却已到  ⇒ 转 HALF_OPEN 并返回 False（放这次过去试探）
        HALF_OPEN       ⇒ False（已经在试探）
        CLOSED          ⇒ False
        """
        if self.state is BreakerState.CLOSED:
            return False
        if self.state is BreakerState.OPEN:
            if time.monotonic() - self.opened_at >= self.cooldown:
                self.state = BreakerState.HALF_OPEN
                logger.info(
                    "[accel] 接管点 %s 冷却结束（%.0fs），进入半开试探",
                    self.name, self.cooldown,
                )
                return False
            return True
        return False   # HALF_OPEN：放行

    # ── 记录结果 ──
    def ok(self) -> None:
        was = self.state
        self.failures = 0
        if was is not BreakerState.CLOSED:
            # 试探成功 ⇒ 正式恢复
            self.state = BreakerState.CLOSED
            self.cooldown = self.base_cooldown   # 重置退避
            self.recovered_count += 1
            logger.info("[accel] 接管点 %s 试探成功，已恢复接管（累计恢复 %d 次）",
                        self.name, self.recovered_count)
        else:
            self.state = BreakerState.CLOSED

    def fail(self, exc: BaseException) -> None:
        self.failures += 1
        self.last_error = exc

        if self.state is BreakerState.HALF_OPEN:
            # 试探失败 ⇒ 回 OPEN，冷却翻倍
            self.cooldown = min(self.cooldown * 2, self.max_cooldown)
            self.opened_at = time.monotonic()
            self.state = BreakerState.OPEN
            logger.warning(
                "[accel] 接管点 %s 试探失败，继续熔断；冷却延长至 %.0fs（最后错误：%r）",
                self.name, self.cooldown, exc,
            )
            return

        if self.failures >= self.threshold and self.state is BreakerState.CLOSED:
            self.trip_count += 1
            self.opened_at = time.monotonic()
            self.state = BreakerState.OPEN
            logger.error(
                "[accel] 接管点 %s 连续失败 %d 次，熔断 %.0fs 后自动试探恢复"
                "（最后错误：%r）",
                self.name, self.failures, self.cooldown, exc,
            )

    def reset(self) -> None:
        """手动恢复（面板按钮调用）。"""
        self.failures = 0
        self.state = BreakerState.CLOSED
        self.cooldown = self.base_cooldown
        logger.info("[accel] 接管点 %s 已被手动重置为正常", self.name)

    def snapshot(self) -> dict:
        """给面板用的状态快照。"""
        remaining = 0.0
        if self.state is BreakerState.OPEN:
            remaining = max(0.0, self.cooldown - (time.monotonic() - self.opened_at))
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "cooldown": round(self.cooldown, 1),
            "cooldown_remaining": round(remaining, 1),
            "trip_count": self.trip_count,
            "recovered_count": self.recovered_count,
            "last_error": (type(self.last_error).__name__ if self.last_error else None),
        }


_BREAKERS: dict[str, Breaker] = {}


def breaker_for(name: str) -> Breaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = Breaker(name)
    return _BREAKERS[name]
