/** 类型定义 — 与后端 API 结构对齐 */

export interface LLMConfigItem {
  id: number
  name: string
  provider: string
  provider_name: string
  model_name: string
  base_url: string
  is_active: boolean
  is_vlm: boolean
  is_llm: boolean
  api_key_preview: string
  config_type: string
  context_window?: number | null
}

export interface LLMActive {
  active_llm_id: number | null
  active_vlm_id: number | null
  llm?: LLMConfigItem | null
  vlm?: LLMConfigItem | null
}

export interface LLMConfigCreate {
  name: string
  provider: string
  provider_name: string
  api_key: string
  base_url: string
  model_name: string
  vlm_resize_max_dim?: number | null
  token_budget_cap?: number | null
  config_type?: string
  context_window?: number | null
}

export interface LLMConfigUpdate {
  name?: string
  provider?: string
  provider_name?: string
  api_key?: string
  base_url?: string
  model_name?: string
  vlm_resize_max_dim?: number | null
  token_budget_cap?: number | null
  config_type?: string
  context_window?: number | null
}

export interface TemperatureMap {
  [scenario: string]: number
}

export interface MemoryUser {
  user_id: string
  user_name: string
  fact_count: number
  last_active: number
}

export interface MemoryFact {
  id: number
  user_id: string
  user_name: string
  fact_key: string
  fact_value: string
  category: string
  importance: number
  created_at: number
  last_accessed: number
}

export interface KnowledgeSection {
  id: number
  title: string
  source: string
  updated_at: number
  content?: string
}

export interface BotStatus {
  timestamp: number
  db: {
    db_size_kb: number
    user_count: number
    memory_count: number
    knowledge_sections: number
  }
  group_chat?: {
    active_groups: number[]
    group_count: number
  }
  llm?: {
    active_llm_id: number | null
    active_vlm_id: number | null
  }
}

export interface ChatParamItem {
  key: string
  value: number | string | boolean | string[]
  label: string
  desc: string
  type: 'int' | 'float' | 'bool' | 'csv'
  min: number
  max: number
  step: number
  group: string
}

export interface PaginatedResponse<T> {
  total: number
  page: number
  per_page: number
  items: T[]
}

export interface TokenStats {
  period: string
  input_tokens: number
  output_tokens: number
  cache_hit_tokens: number
  cache_miss_tokens: number
  cache_hit_rate: number
  request_count: number
  estimated_cost_cny: number
  by_scenario: Record<string, {
    input_tokens: number
    output_tokens: number
    request_count: number
  }>
}

export interface TokenHistoryItem {
  day: string
  input_tokens: number
  output_tokens: number
  request_count: number
}

// ── 群聊白名单 ─────────────────────────────────────

export interface WhitelistEntry {
  group_id: number
  tier: 'basic' | 'full'
}

// ── Bot 检测 ──────────────────────────────────────────

export interface SuspectedBot {
  id: number
  user_id: string
  user_name: string
  group_id: string
  suspicion_score: number
  marked_by: string
  status: string          // 'flagged' | 'confirmed' | 'false_positive'
  notes: string
  created_at: number
  updated_at: number
  live_signals?: Record<string, number>  // BotDetector 实时信号
}

export interface BotDetectionSignal {
  name: string
  score: number
  weight: number
}

export interface BotLiveItem {
  user_id: string
  user_name: string
  score: number
  signals: Record<string, number>
  sample_count: number
  action_taken: boolean
  first_flagged_at: number
  last_updated: number
}

export interface BotDetectLive {
  live: BotLiveItem[]
  total_tracked: number
  action_taken_count: number
  peer_isolated?: string[]  // 已隔离的 peer ID 列表
}

// ── 群聊总结 ──────────────────────────────────────────

export interface GroupSummary {
  group_id: number
  summary_text: string | null
  message_range_start?: number
  message_range_end?: number
  created_at?: number
}

export interface SummaryHistoryEntry {
  id: number
  summary_text: string
  message_range_start?: number
  message_range_end?: number
  created_at: number
}

export interface SummaryGroup {
  group_id: number
  latest_summary: number
}

// ── 工具拒绝文案风格 ──────────────────────────────────────

export interface RejectionStyle {
  style_label: string
  pronoun: string
  tone_hint: string
}

// ── Bot 管理 ──────────────────────────────────────────

export interface BotMeta {
  id: string
  name: string
  icon: string
  color: string
  avatar: string
  character_card: string
  nicknames: string[]
  is_active: boolean
  peer_bot_ids: string[]
  role_description: string
  rejection_style: RejectionStyle
  llm_slots: string[]
}

export interface BotSettings {
  bot_id: string
  bot_name: string
  group_chat_enabled: boolean
  private_chat_enabled: boolean
  reasoning_enabled: boolean
  thinking_effort: string
  slot_efforts: Record<string, string>
  shadow_agent_enabled: boolean
  shadow_agent_model: string
  owner_qq: string
  nsfw_enabled: boolean
  nsfw_groups: string
  nsfw_users: string
  /** 私聊 NSFW 通道 — 私聊没有群号, 群白名单不适用, 单列一个开关。默认 false */
  nsfw_private: boolean
  llm_slots: Record<string, number | null>
  vlm_slots: Record<string, number | null>
  /** NSFW 专属 LLM 槽位 (null = 跟随 SFW; 2026-10-18) */
  nsfw_slots: Record<string, number | null>
  /** NSFW 专属槽位档位 (follow = 跟随 SFW / 全局) */
  nsfw_slot_efforts: Record<string, string>
}

export interface BotIdentityCreate {
  bot_id: string
  name: string
  character_card: string
  nicknames?: string[]
  peer_bot_ids?: string[]
  icon?: string
  color?: string
  avatar?: string
  role_description?: string
  rejection_style?: RejectionStyle
  llm_slots?: string[]
}

// ── 角色卡 ──────────────────────────────────────────

export interface CharacterCardMeta {
  name: string
  display_name: string
}

export interface CharacterCard {
  spec: string
  spec_version: string
  data: CharacterCardData
}

export interface CharacterCardData {
  name: string
  description: string
  personality: string
  scenario: string
  talkativeness: string
  first_mes: string
  mes_example: string
  system_prompt: string
  group_persona: string
  group_mes_example: string
  post_history_instructions: string
  role_description: string
  kaomoji_rule: string
  sticker_guide: string
  companion_rules: string
  nicknames?: string[]
  tags?: string[]
  alternate_greetings?: string[]
  group_only_greetings?: string[]
  character_version?: string
  creator_notes?: string
  [key: string]: unknown
}

// ── 工具设置 ──────────────────────────────────────────

export interface ToolSettingItem {
  key: string
  value: boolean | number
  label: string
  desc: string
  type: 'bool' | 'int' | 'float'
  min: number
  max: number
  step: number
  group: string
}

/** 统一工具注册表中的单个工具 — per-bot, per-tool 配置 */
export interface ToolConfig {
  name: string
  label: string
  category: string
  bot: 'loput' | 'luna' | 'both'
  desc: string
  enabled: boolean
  min_affinity: number
}

/** GET /api/admin/tool-settings 返回 */
export interface ToolSettingsResponse {
  tool_settings: ToolSettingItem[]
  tools: ToolConfig[]
  bot_id: string
}

export interface LLMListResponse {
  configs: LLMConfigItem[]
  slots: Record<string, number | null>
  bot_id: string
  bot_name: string
  /** 请求 scope ('sfw' | 'nsfw'; 2026-10-18 NSFW 分页) */
  scope?: string
  /** scope=nsfw 时: SFW 槽位映射 (显示「跟随 SFW」回落目标) */
  fallback_slots?: Record<string, number | null>
}

/** NSFW 温度视图 — 生效值 / 覆盖标记 / SFW 回落值 (2026-10-18) */
export interface NsfwTemperatureView {
  temperatures: TemperatureMap
  overrides: Record<string, boolean>
  sfw: TemperatureMap
}

// ── 插件发现 ──────────────────────────────────────────

export interface PluginInfo {
  id: string
  name: string
  route: string
  icon: string
  type: 'core' | 'enhanced'
  has_page: boolean
  description: string
}

// ── 表情包管理 ────────────────────────────────────────

export interface MemeCategory {
  name: string
  count: number
  description: string
}

export interface MemeItem {
  file: string
  tags: string[]
  desc: string
  intensity: string
}

export interface MemeSyncStatus {
  missing_in_config: string[]
  deleted_categories: string[]
}

// ── 绘图 API (NovelAI + PixAI) ─────────────────────────

export interface DrawNovelAIConfig {
  token_set: boolean
  token_masked: string
  /** 主人档模型（默认 V5 full） */
  model: string
  /** 普通用户档模型（默认 V4.5 full，支持参考图 Vibe） */
  model_public: string
  /** 主人档模型是否支持参考图 (Vibe) — 只做提示 */
  model_vibe_ok: boolean
  /** 普通档模型是否支持参考图 (Vibe) — 只做提示 */
  model_public_vibe_ok: boolean
  size: string
  steps: string
}

export interface DrawPixAIConfig {
  key_set: boolean
  key_masked: string
  model: string
  aspect: string
  size: string
}

/** 绘图访问策略 — 计费后端准入 (2026-10-12 用户裁定) */
export interface DrawPolicyConfig {
  /** 计费后端名单（默认 ["pixai"]） */
  metered_providers: string[]
  /** 计费后端仅主人可用 */
  metered_master_only: boolean
  /** 主人 NSFW 通路额外放行计费后端 */
  metered_nsfw_allow: boolean
}

export interface DrawSpecConfig {
  route: string
  effort: string
  tokens: string
  temperature: string
  timeout: string
}

export interface DrawApiConfig {
  novelai: DrawNovelAIConfig
  pixai: DrawPixAIConfig
  policy?: DrawPolicyConfig
  spec?: DrawSpecConfig
  assignments: Record<string, 'novelai' | 'pixai' | 'none'>
  bots: { id: string; name: string }[]
}
