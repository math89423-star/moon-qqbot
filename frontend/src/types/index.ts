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
  llm_slots: Record<string, number | null>
  vlm_slots: Record<string, number | null>
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
  bot: 'moon' | 'both'
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
