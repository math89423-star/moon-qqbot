"""暮恩主动行为引擎 — 插件配置。

所有 persona 相关内容均在此定义, 引擎代码零硬编码角色设定。
可通过 AstrBot WebUI 编辑覆盖。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ═════════════════════════════════════════════════════════════════
# 主动行为原因 — 描述"为什么主动发消息"
# ═════════════════════════════════════════════════════════════════

class ProactiveReason(BaseModel):
    """单个主动行为原因的定义。"""
    key: str = Field(description="原因唯一标识, 如 morning_greeting")
    label: str = Field(description="中文描述, 如 '早上问候'")
    daypart: str = Field(
        default="any",
        description="时段约束: morning/noon/afternoon/evening/night/any",
    )
    priority: float = Field(default=1.0, description="选择权重 (越大越优先)")
    intimate: bool = Field(default=False, description="是否为亲密行为 (朋友关系下会被过滤)")


# ═════════════════════════════════════════════════════════════════
# 主动行为动作 — 描述"主动消息怎么发"
# ═════════════════════════════════════════════════════════════════

class ProactiveAction(BaseModel):
    """单个主动行为动作的定义。"""
    key: str = Field(description="动作唯一标识, 如 message")
    label: str = Field(description="中文描述, 如 '发一条文字消息'")
    weight: float = Field(default=1.0, description="选择权重")
    intimate: bool = Field(default=False, description="是否为亲密动作")
    requires_ability: str = Field(
        default="",
        description="依赖的外部能力名称, 空字符串表示无依赖",
    )


# ═════════════════════════════════════════════════════════════════
# 角色约束 — 不同关系角色的主动行为限制
# ═════════════════════════════════════════════════════════════════

class RoleConstraints(BaseModel):
    """单个角色的主动行为约束。"""
    daily_limit: int = Field(default=6, description="每日最大主动消息数")
    idle_minutes: int = Field(default=60, description="用户空闲多久后才可主动 (分钟)")
    interval_minutes: int = Field(default=120, description="两次主动消息最小间隔 (分钟)")
    greeting_idle_minutes: int = Field(default=120, description="问候类主动消息需要空闲多久 (分钟)")
    photo_daily_limit: int = Field(default=0, description="每日图片消息上限")
    screen_peek_daily_limit: int = Field(default=0, description="每日屏幕窥探上限")
    poke_daily_limit: int = Field(default=0, description="每日戳一戳上限")


# ═════════════════════════════════════════════════════════════════
# 插件配置 (顶层)
# ═════════════════════════════════════════════════════════════════

class Config(BaseModel):
    """暮恩主动行为引擎配置。"""

    # ── 角色 ──
    char_name: str = Field(
        default="暮恩",
        description="主动消息中使用的角色名",
    )

    # ── 总开关 ──
    enabled: bool = Field(default=True, description="主动行为引擎总开关")
    check_interval_seconds: int = Field(
        default=60, ge=10, le=600,
        description="调度轮询间隔 (秒)",
    )

    # ── 私聊主动行为 ──
    private_proactive_enabled: bool = Field(
        default=True, description="启用私聊主动行为",
    )
    target_user_ids: list[str] = Field(
        default_factory=list,
        description="主动行为目标用户 QQ 号列表",
    )

    # ── 群聊主动行为 (继承自原有 proactive_speaker) ──
    group_proactive_enabled: bool = Field(
        default=True, description="启用群聊冷场破冰",
    )
    group_silence_threshold: int = Field(
        default=900, ge=60, le=3600,
        description="群静默多久后考虑主动发言 (秒)",
    )
    group_proactive_cooldown: int = Field(
        default=1800, ge=300, le=7200,
        description="群主动发言后冷却时间 (秒)",
    )
    group_proactive_chance: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="每次轮询的主动发言概率 (0-1)",
    )

    # ── 角色约束 ──
    owner_constraints: RoleConstraints = Field(
        default_factory=lambda: RoleConstraints(
            daily_limit=24, idle_minutes=15, interval_minutes=60,
            greeting_idle_minutes=120, photo_daily_limit=4,
            screen_peek_daily_limit=6, poke_daily_limit=4,
        ),
        description="主人角色的主动行为约束",
    )
    friend_constraints: RoleConstraints = Field(
        default_factory=lambda: RoleConstraints(
            daily_limit=2, idle_minutes=180, interval_minutes=360,
            greeting_idle_minutes=240, photo_daily_limit=0,
            screen_peek_daily_limit=0, poke_daily_limit=0,
        ),
        description="朋友角色的主动行为约束",
    )

    # ── 时段与免打扰 ──
    quiet_hours_start: str = Field(
        default="02:00", description="免打扰开始时间 (HH:MM)",
    )
    quiet_hours_end: str = Field(
        default="07:00", description="免打扰结束时间 (HH:MM)",
    )
    user_rest_silence_hours: int = Field(
        default=6, ge=1, le=24,
        description="用户设置'休息'后的静默时长 (小时)",
    )

    # ── 候选池 ──
    candidate_pool_max_size: int = Field(
        default=120, ge=20, le=500,
        description="候选池最大条目数",
    )
    candidate_pool_ttl_hours: float = Field(
        default=36.0, description="候选池条目 TTL (小时)",
    )

    # ── 关系状态机 ──
    enable_relationship_state: bool = Field(
        default=True, description="启用关系状态机 (backoff/hurt/refusing)",
    )
    enable_emotion_gating: bool = Field(
        default=True, description="启用情绪门控",
    )

    # ── 去重 ──
    recent_topic_window: int = Field(
        default=8, ge=2, le=20,
        description="最近话题去重窗口 (条数)",
    )

    # ── 动作权重 ──
    action_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "message": 1.0,
            "sticker": 0.4,
            "photo": 0.15,
        },
        description="各动作的选择权重 (相对值)",
    )

    # ── 主动行为原因 ──
    proactive_reasons: list[ProactiveReason] = Field(
        default_factory=lambda: [
            ProactiveReason(key="morning_greeting", label="早上问候", daypart="morning", priority=1.5),
            ProactiveReason(key="noon_checkin", label="午间关心", daypart="noon", priority=1.2),
            ProactiveReason(key="evening_winddown", label="晚间闲聊", daypart="evening", priority=1.3),
            ProactiveReason(key="check_in", label="日常关心", daypart="any", priority=1.0),
            ProactiveReason(key="break_silence", label="冷场破冰", daypart="any", priority=0.6),
            ProactiveReason(key="activity_share", label="分享动态", daypart="any", priority=0.5),
            ProactiveReason(key="follow_up", label="跟进未完成话题", daypart="any", priority=0.8),
            ProactiveReason(key="poke", label="戳一戳", daypart="any", priority=0.3, intimate=True),
        ],
        description="主动行为原因列表",
    )

    # ── 主动行为动作 ──
    proactive_actions: list[ProactiveAction] = Field(
        default_factory=lambda: [
            ProactiveAction(key="message", label="发一条文字消息", weight=1.0),
            ProactiveAction(key="sticker", label="发一张表情包", weight=0.4),
            ProactiveAction(key="photo", label="发一张生图", weight=0.15, requires_ability="image_gen"),
        ],
        description="主动行为动作列表",
    )

    # ── 亲密语言过滤 ──
    intimate_patterns: list[str] = Field(
        default_factory=lambda: [
            "想你了", "抱抱", "亲亲", "贴贴",
        ],
        description="朋友关系下需要过滤的亲密用语模式 (用于消息清洗)",
    )
