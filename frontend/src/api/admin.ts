/** API 客户端 — 封装所有管理 API 调用 */

import axios, { type AxiosInstance } from 'axios'
import type {
  LLMConfigItem,
  LLMActive,
  LLMConfigCreate,
  LLMConfigUpdate,
  TemperatureMap,
  ToolSettingItem,
  ToolConfig,
  MemoryUser,
  MemoryFact,
  KnowledgeSection,
  BotStatus,
  ChatParamItem,
  TokenStats,
  TokenHistoryItem,
  SuspectedBot,
  BotDetectLive,
  WhitelistEntry,
  GroupSummary,
  SummaryHistoryEntry,
  SummaryGroup,
  BotMeta,
  BotSettings,
  BotIdentityCreate,
  CharacterCardMeta,
  CharacterCard,
  LLMListResponse,
  PluginInfo,
  MemeCategory,
  MemeItem,
  MemeSyncStatus,
} from '@/types'

const api: AxiosInstance = axios.create({
  baseURL: '/api/admin',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
})

// ── Token 管理 ──────────────────────────────────

export function getToken(): string {
  return localStorage.getItem('admin_token') || ''
}

export function setToken(token: string): void {
  localStorage.setItem('admin_token', token)
}

export function clearToken(): void {
  localStorage.removeItem('admin_token')
}

// 请求拦截器: 自动注入 Bearer token
api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器: 401 → 清除 token
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      clearToken()
      window.location.reload()
    }
    return Promise.reject(err)
  },
)

// ── 认证 ────────────────────────────────────────

export async function login(token: string): Promise<boolean> {
  try {
    await api.post('/login', { token })
    setToken(token)
    return true
  } catch {
    return false
  }
}

// ── 配置 ────────────────────────────────────────

export async function getConfig(): Promise<Record<string, string>> {
  const { data } = await api.get('/config')
  return data.config
}

export async function updateConfig(kv: Record<string, string>): Promise<void> {
  await api.put('/config', { data: kv })
}

// ── LLM ─────────────────────────────────────────

export async function listLLM(): Promise<LLMConfigItem[]> {
  const { data } = await api.get('/llm/list')
  return data.configs
}

export async function getActiveLLM(): Promise<LLMActive> {
  const { data } = await api.get('/llm/active')
  return data
}

export async function activateLLM(
  llmType: 'llm' | 'vlm',
  configId: number,
): Promise<void> {
  await api.post('/llm/activate', { llm_type: llmType, config_id: configId })
}

export async function createLLM(data: LLMConfigCreate): Promise<number> {
  const { data: res } = await api.post('/llm', data)
  return res.id
}

export async function updateLLM(
  configId: number,
  data: LLMConfigUpdate,
): Promise<void> {
  await api.put(`/llm/${configId}`, data)
}

export async function deleteLLM(configId: number): Promise<void> {
  await api.delete(`/llm/${configId}`)
}

// ── Bot 管理 ─────────────────────────────────────

export async function listBots(): Promise<BotMeta[]> {
  const { data } = await api.get('/bots')
  return data.bots
}

export async function getBotSettings(botId: string): Promise<BotSettings> {
  const { data } = await api.get('/bot-settings', { params: { bot_id: botId } })
  return data
}

export async function updateBotSettings(
  botId: string,
  settings: Record<string, boolean>,
): Promise<void> {
  await api.post('/bot-settings', { bot_id: botId, ...settings })
}

// ── Bot 身份 CRUD ──────────────────────────────────

export async function createBot(data: BotIdentityCreate): Promise<void> {
  await api.post('/bots', data)
}

export async function updateBot(
  botId: string,
  data: Partial<BotIdentityCreate>,
): Promise<void> {
  await api.put(`/bots/${botId}`, data)
}

export async function deleteBot(botId: string): Promise<void> {
  await api.delete(`/bots/${botId}`)
}

// ── 头像上传 ──────────────────────────────────────

export async function uploadAvatar(file: File): Promise<string> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await api.post('/avatars', form, {
    headers: { 'Content-Type': undefined as unknown as string },
  })
  return data.url
}

// ── 角色卡 CRUD ────────────────────────────────────

export async function listCharacters(): Promise<CharacterCardMeta[]> {
  const { data } = await api.get('/characters')
  return data.characters
}

export async function getCharacter(name: string): Promise<CharacterCard> {
  const { data } = await api.get(`/characters/${encodeURIComponent(name)}`)
  return data.card
}

export async function createCharacter(
  payload: { name: string; display_name?: string; card?: object },
): Promise<void> {
  await api.post('/characters', payload)
}

export async function updateCharacter(
  name: string,
  card: CharacterCard,
): Promise<void> {
  await api.put(`/characters/${encodeURIComponent(name)}`, { card })
}

export async function deleteCharacter(name: string): Promise<void> {
  await api.delete(`/characters/${encodeURIComponent(name)}`)
}

// ── LLM 槽位分配 (per-bot) ────────────────────────

export async function getLLMListWithSlots(botId?: string): Promise<LLMListResponse> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.get('/llm/list', { params })
  return data
}

export async function getVLMListWithSlots(botId?: string): Promise<LLMListResponse> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.get('/vlm/list', { params })
  return data
}

export async function assignSlot(
  type: 'llm' | 'vlm',
  slot: string,
  configId: number,
  botId?: string,
): Promise<void> {
  await api.post(`/${type}/activate`, {
    config_id: configId,
    slot,
    bot_id: botId || undefined,
  })
}

// ── 温度 ────────────────────────────────────────

export async function getTemperatures(botId?: string): Promise<TemperatureMap> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.get('/temperature', { params })
  return data.temperatures
}

export async function updateTemperatures(
  temps: TemperatureMap,
  botId?: string,
): Promise<TemperatureMap> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.put('/temperature', { temperatures: temps }, { params })
  return data.temperatures
}

// ── 用户记忆 ────────────────────────────────────

export async function listMemoryUsers(
  page = 1,
  perPage = 20,
  botId = '',
): Promise<{ users: MemoryUser[]; total: number; page: number; per_page: number }> {
  const params: Record<string, string | number> = { page, per_page: perPage }
  if (botId) params.bot_id = botId
  const { data } = await api.get('/memory/users', { params })
  return data
}

export async function searchMemories(
  q: string,
  topN = 20,
  botId = '',
): Promise<{ results: MemoryFact[]; query: string; count: number }> {
  const params: Record<string, string | number> = { q, top_n: topN }
  if (botId) params.bot_id = botId
  const { data } = await api.get('/memory/search', { params })
  return data
}

export async function getUserMemories(
  userId: string,
  page = 1,
  perPage = 50,
  botId = '',
): Promise<{ user_id: string; facts: MemoryFact[]; total: number; page: number; per_page: number }> {
  const params: Record<string, string | number> = { page, per_page: perPage }
  if (botId) params.bot_id = botId
  const { data } = await api.get(`/memory/${userId}`, { params })
  return data
}

export async function deleteUserFact(
  userId: string,
  factKey: string,
  botId = '',
): Promise<void> {
  const params: Record<string, string> = {}
  if (botId) params.bot_id = botId
  await api.delete(`/memory/${encodeURIComponent(userId)}/${encodeURIComponent(factKey)}`, { params })
}

// ── 知识库 ──────────────────────────────────────

export async function listKnowledge(
  source = '',
  page = 1,
  perPage = 50,
): Promise<{
  sections: KnowledgeSection[]
  total: number
  page: number
  per_page: number
  sources: string[]
}> {
  const { data } = await api.get('/knowledge', {
    params: { source, page, per_page: perPage },
  })
  return data
}

export async function getKnowledgeSection(id: number): Promise<KnowledgeSection> {
  const { data } = await api.get(`/knowledge/${id}`)
  return data.section
}

// ── 白名单 (per-bot) ────────────────────────────

export async function getWhitelist(botId?: string): Promise<WhitelistEntry[]> {
  const params: Record<string, string> = {}
  if (botId) params.bot_id = botId
  const { data } = await api.get('/whitelist', { params })
  return data.whitelist
}

/** 获取 per-bot 白名单全量: {bot_id: {group_id: tier}} */
export async function getAllBotWhitelists(): Promise<Record<string, Record<number, string>>> {
  const { data } = await api.get('/whitelist')
  return data.all_bots || {}
}

export async function addWhitelist(
  groupId: number,
  tier: string = 'basic',
  botId?: string,
): Promise<void> {
  await api.post('/whitelist', { group_id: groupId, tier, bot_id: botId })
}

export async function updateWhitelist(
  groupId: number,
  tier: string,
  botId?: string,
): Promise<void> {
  await api.put(`/whitelist/${groupId}`, { tier, bot_id: botId })
}

export async function removeWhitelist(groupId: number, botId?: string): Promise<void> {
  await api.delete(`/whitelist/${groupId}`, { params: { bot_id: botId } })
}

// ── 对话参数 ────────────────────────────────────

export async function getChatParams(botId?: string): Promise<ChatParamItem[]> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.get('/chat-params', { params })
  return data.params
}

export async function updateChatParams(
  params: Record<string, unknown>,
  botId?: string,
): Promise<ChatParamItem[]> {
  const qs = botId ? { bot_id: botId } : {}
  const { data } = await api.put('/chat-params', { params }, { params: qs })
  return data.params
}

// ── 工具设置 ────────────────────────────────────

export async function getToolSettings(botId?: string): Promise<{
  tool_settings: ToolSettingItem[]
  tools: ToolConfig[]
  bot_id: string
}> {
  const params = botId ? { bot_id: botId } : {}
  const { data } = await api.get('/tool-settings', { params })
  return data
}

export async function updateToolSettings(
  payload: {
    tool_settings?: Record<string, unknown>
    tools?: Record<string, { enabled: boolean; min_affinity?: number } | boolean>
  },
  botId?: string,
): Promise<{
  ok: boolean
  tool_settings: ToolSettingItem[]
  tools: ToolConfig[]
  bot_id: string
}> {
  const qs = botId ? { bot_id: botId } : {}
  const { data } = await api.post('/tool-settings', payload, { params: qs })
  return data
}

// ── Token 用量 ──────────────────────────────────

export async function getTokenStats(
  period: string = 'today',
): Promise<TokenStats> {
  const { data } = await api.get('/token-stats', { params: { period } })
  return data
}

export async function getTokenHistory(
  days: number = 7,
): Promise<TokenHistoryItem[]> {
  const { data } = await api.get('/token-history', { params: { days } })
  return data
}

// ── 状态 ────────────────────────────────────────

export async function getStatus(): Promise<BotStatus> {
  const { data } = await api.get('/status')
  return data
}

// ── Bot 检测 ────────────────────────────────────────

export async function listSuspectedBots(
  status: string = '',
  limit: number = 50,
): Promise<SuspectedBot[]> {
  const { data } = await api.get('/bot-detect/list', { params: { status, limit } })
  return data.bots
}

export async function getSuspectedBot(userId: string): Promise<SuspectedBot> {
  const { data } = await api.get(`/bot-detect/${userId}`)
  return data.bot
}

export async function updateSuspectedBot(
  userId: string,
  body: { status?: string; notes?: string },
): Promise<SuspectedBot> {
  const { data } = await api.put(`/bot-detect/${userId}`, body)
  return data.bot
}

export async function resetSuspectedBot(userId: string): Promise<void> {
  await api.post(`/bot-detect/${userId}/reset`)
}

export async function deleteSuspectedBot(userId: string): Promise<void> {
  await api.delete(`/bot-detect/${userId}`)
}

export async function getLiveDetections(): Promise<BotDetectLive> {
  const { data } = await api.get('/bot-detect/live')
  return data
}

// ── 群聊总结 ──────────────────────────────────────

export async function getGroupSummary(
  groupId: number,
): Promise<GroupSummary> {
  const { data } = await api.get(`/summary/${groupId}`)
  return data
}

export async function getGroupSummaryHistory(
  groupId: number,
  limit = 20,
): Promise<{ group_id: number; summaries: SummaryHistoryEntry[]; count: number }> {
  const { data } = await api.get(`/summary/${groupId}/history`, {
    params: { limit },
  })
  return data
}

export async function listSummaryGroups(): Promise<{
  groups: SummaryGroup[]
  count: number
}> {
  const { data } = await api.get('/summary')
  return data
}

// ── 插件发现 ──────────────────────────────────────

export async function listPlugins(): Promise<PluginInfo[]> {
  const { data } = await api.get('/plugins')
  return data.plugins
}

// ── 表情包管理 ────────────────────────────────────

export async function listMemeCategories(): Promise<MemeCategory[]> {
  const { data } = await api.get('/memes/categories')
  return data.categories
}

export async function listMemes(category?: string): Promise<{ items: MemeItem[]; total: number }> {
  const params = category ? { category } : {}
  const { data } = await api.get('/memes', { params })
  return data
}

export async function clearMemeCategory(category: string): Promise<void> {
  await api.post('/memes/category/clear', { category })
}

export async function deleteMemeCategory(category: string): Promise<void> {
  await api.post('/memes/category/delete', { category })
}

export async function updateMemeDesc(category: string, description: string): Promise<void> {
  await api.post('/memes/category/update-desc', { category, description })
}

export async function getMemeSyncStatus(): Promise<MemeSyncStatus> {
  const { data } = await api.get('/memes/sync/status')
  return data
}

export async function uploadMeme(category: string, file: File): Promise<{ ok: boolean; category: string; filename: string; size: number }> {
  const form = new FormData()
  form.append('category', category)
  form.append('file', file)
  const { data } = await api.post('/memes/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export async function deleteMeme(category: string, filename: string): Promise<void> {
  await api.post('/memes/delete', { category, filename })
}
