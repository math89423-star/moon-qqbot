<script setup lang="ts">
/** 机器人配置 — LLM/VLM 槽位(含 Provider CRUD) + 开关 + 温度 + 参数 + 用量 */
import { ref, inject, onMounted, watch, type Ref } from 'vue'
import {
  fetchModelMeta, fetchRemoteModels,
  getBotSettings, updateBotSettings,
  getLLMListWithSlots, getVLMListWithSlots, assignSlot,
  createLLM, updateLLM, deleteLLM,
  getTemperatures, updateTemperatures,
  getChatParams, updateChatParams,
  getTokenStats, getStatus,
  getToolSettings, updateToolSettings,
  getDrawConfig, saveDrawConfig,
  testLlmConnection,
} from '@/api/admin'
import type {
  BotSettings, LLMListResponse, LLMConfigItem, LLMConfigCreate, LLMConfigUpdate,
  TemperatureMap, ChatParamItem, TokenStats, ToolSettingItem, ToolConfig,
  DrawApiConfig,
} from '@/types'
import type { ModelMeta, LlmTestResult } from '@/api/admin'
import {
  Cpu, Server, Thermometer, Sliders, BarChart3,
  MessageSquare, User, Brain, Eye, Shield, Flame,
  Plus, Pencil, Trash2, X, Wrench, Palette,
} from '@lucide/vue'

// ── Bot 上下文 ──
const currentBot = inject<Ref<string>>('currentBot')!
const bots = inject<Ref<{ id: string; name: string; icon: string; color: string }[]>>('bots')!
const switchBot = inject<(id: string) => void>('switchBot')!

// ── Tab 状态 ──
type TabKey = 'llm' | 'vlm' | 'draw' | 'temp' | 'chat' | 'tools' | 'tokens'
const activeTab = ref<TabKey>('llm')
const tabs: { key: TabKey; label: string; icon: any }[] = [
  { key: 'llm', label: 'LLM 槽位', icon: Cpu },
  { key: 'vlm', label: 'VLM 槽位', icon: Server },
  { key: 'draw', label: '绘图', icon: Palette },
  { key: 'temp', label: '温度设置', icon: Thermometer },
  { key: 'chat', label: '对话参数', icon: Sliders },
  { key: 'tools', label: '工具设置', icon: Wrench },
  { key: 'tokens', label: '用量统计', icon: BarChart3 },
]

// ── Bot 设置 ──
const settings = ref<BotSettings | null>(null)
const settingsLoading = ref(false)

async function loadSettings() {
  settingsLoading.value = true
  try { settings.value = await getBotSettings(currentBot.value) } catch { /* ignore */ }
  finally { settingsLoading.value = false }
}

async function toggleChat(type: 'group_chat_enabled' | 'private_chat_enabled' | 'reasoning_enabled' | 'shadow_agent_enabled' | 'nsfw_enabled' | 'nsfw_private', val: boolean) {
  if (!settings.value) return
  try {
    await updateBotSettings(currentBot.value, { [type]: val })
    settings.value[type] = val
  } catch { /* ignore */ }
}

// ── 默认档位 (per-bot 单一真相源; auto=Gate 按意图动态决定) ──
const THINKING_EFFORTS = [
  { value: 'auto', label: '自动 (Gate 决定)' },
  { value: 'off', label: '关闭' },
  { value: 'minimal', label: 'minimal' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max' },
]

async function saveThinkingEffort(val: string) {
  if (!settings.value) return
  try {
    await updateBotSettings(currentBot.value, { thinking_effort: val })
    settings.value.thinking_effort = val
  } catch { /* ignore */ }
}

// ── 槽位旁档位下拉 (档位跟着槽位/模型走; 跟随全局=用顶栏档位) ──
// 与后端 BotConfigService.SLOT_EFFORT_SLOTS 同步 (llm_secondary 已退役无消费方)。
const SLOT_EFFORT_SLOTS = new Set(['llm_primary', 'llm_lite', 'llm_pro', 'llm_gate', 'shadow_agent'])
const SLOT_EFFORT_OPTIONS = [
  { value: 'follow', label: '跟随全局' },
  { value: 'off', label: '关闭' },
  { value: 'minimal', label: 'minimal' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max' },
]

function showSlotEffort(slotKey: string): boolean {
  return activeTab.value === 'llm' && SLOT_EFFORT_SLOTS.has(slotKey)
}

function slotEffort(slotKey: string): string {
  return settings.value?.slot_efforts?.[slotKey] || 'follow'
}

async function saveSlotEffort(slotKey: string, val: string) {
  if (!settings.value) return
  try {
    const next = { ...(settings.value.slot_efforts || {}), [slotKey]: val }
    await updateBotSettings(currentBot.value, { slot_efforts: next })
    settings.value.slot_efforts = next
  } catch { /* ignore */ }
}

// ── 行内测试探针文本 (key 不出后端) ──
const testPrompt = ref('请只回复：连通正常')

// ── 行内测试 (点哪行测哪行: 用该槽位当前生效档位打探针) ──
const rowTestRunning = ref<Record<string, boolean>>({})
const rowTestResult = ref<Record<string, LlmTestResult | null>>({})

function effectiveEffortFor(slotKey: string): string {
  const s = slotEffort(slotKey)
  if (s && s !== 'follow') return s
  return settings.value?.thinking_effort || 'minimal'
}

async function runRowTest(slotKey: string) {
  rowTestResult.value[slotKey] = null
  rowTestRunning.value[slotKey] = true
  try {
    rowTestResult.value[slotKey] = await testLlmConnection(
      currentBot.value, slotKey,
      effectiveEffortFor(slotKey),
      testPrompt.value || '请只回复：连通正常',
    )
  } catch (e) {
    rowTestResult.value[slotKey] = { ok: false, error: `请求失败：${e}` }
  } finally {
    rowTestRunning.value[slotKey] = false
  }
}

async function saveOwnerQQ() {
  if (!settings.value) return
  try {
    await updateBotSettings(currentBot.value, { owner_qq: settings.value.owner_qq })
  } catch { /* ignore */ }
}

// ── NSFW 隔离区 (默认关闭; 名单为空=仅超管; 逗号分隔, 数字 only) ──
const nsfwSaved = ref('')
let nsfwSavedTimer: number | undefined
async function saveNsfw() {
  if (!settings.value) return
  try {
    await updateBotSettings(currentBot.value, {
      nsfw_groups: settings.value.nsfw_groups || '',
      nsfw_users: settings.value.nsfw_users || '',
    })
    nsfwSaved.value = new Date().toLocaleTimeString()
    window.clearTimeout(nsfwSavedTimer)
    nsfwSavedTimer = window.setTimeout(() => { nsfwSaved.value = '' }, 2500)
  } catch { /* ignore */ }
}

// ── LLM 槽位 ──
const llmList = ref<LLMListResponse | null>(null)
const vlmList = ref<LLMListResponse | null>(null)
const slotAssigning = ref<string | null>(null)

async function loadLLM() {
  try { llmList.value = await getLLMListWithSlots(currentBot.value) } catch { /* ignore */ }
}
async function loadVLM() {
  try { vlmList.value = await getVLMListWithSlots(currentBot.value) } catch { /* ignore */ }
}

async function doAssignSlot(
  type: 'llm' | 'vlm', slot: string, configId: number | null,
) {
  const key = `${type}:${slot}`
  slotAssigning.value = key
  try {
    await assignSlot(type, slot, configId ?? 0, currentBot.value)
    if (type === 'llm') await loadLLM()
    else await loadVLM()
  } catch { /* ignore */ }
  finally { slotAssigning.value = null }
}

// ── Provider CRUD ──
const errorMsg = ref('')
const showCreate = ref(false)
const creating = ref(false)

// ── 绘图分配 (按 Bot 指派 NovelAI / PixAI / 停用；Key 与参数去各自独立配置页) ──
const drawCfg = ref<DrawApiConfig | null>(null)
const drawLoading = ref(false)
const drawSaving = ref(false)
const drawAssign = ref<Record<string, string>>({})

async function loadDraw() {
  drawLoading.value = true
  try {
    const c = await getDrawConfig()
    drawCfg.value = c
    drawAssign.value = { ...c.assignments }
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '绘图配置加载失败'
  } finally {
    drawLoading.value = false
  }
}

async function saveDrawAssign() {
  drawSaving.value = true
  try {
    const c = await saveDrawConfig({ assignments: drawAssign.value })
    drawCfg.value = c
    drawAssign.value = { ...c.assignments }
    errorMsg.value = ''
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '保存失败'
  } finally {
    drawSaving.value = false
  }
}

const LLM_PROVIDERS = ['deepseek', 'openai', 'anthropic', 'ollama', 'vectorengine', 'openrouter', 'together', 'custom']
const VLM_PROVIDERS = ['gpt4v', 'claude', 'gemini', 'nano_banana', 'llama']
const ALL_PROVIDERS = [...new Set([...LLM_PROVIDERS, ...VLM_PROVIDERS, 'opencode', 'siliconflow', 'moonshot'])]

// ── Provider 预设: 选名自动填 base_url + 备选模型 ──
// 注意: opencode models 只是离线兜底, 真实列表以「获取模型」拉取为准
// (go/v1 全量约 40+ 个, 含 muse-spark-1.3-contributor / mimo-v2.5 等)
const PROVIDER_PRESETS: Record<string, { base_url: string; models: string[] }> = {
  deepseek: { base_url: 'https://api.deepseek.com/v1', models: ['deepseek-chat', 'deepseek-reasoner'] },
  openai: { base_url: 'https://api.openai.com/v1', models: ['gpt-4o', 'gpt-4o-mini'] },
  anthropic: { base_url: 'https://api.anthropic.com/v1/', models: ['claude-opus-4', 'claude-sonnet-4'] },
  openrouter: { base_url: 'https://openrouter.ai/api/v1', models: [] },
  opencode: {
    base_url: 'https://opencode.ai/zen/go/v1',
    models: [
      'muse-spark-1.3-contributor',
      'muse-spark-1.2-contributor',
      'mimo-v2.5',
      'mimo-v2.5-pro',
      'mimo-v2.6-flash',
      'deepseek-v4-flash',
      'glm-5.3',
      'qwen3.8-max',
    ],
  },
  siliconflow: { base_url: 'https://api.siliconflow.cn/v1', models: ['deepseek-ai/DeepSeek-V3', 'Qwen/Qwen3-8B'] },
  groq: { base_url: 'https://api.groq.com/openai/v1', models: ['llama-3.3-70b-versatile'] },
  moonshot: { base_url: 'https://api.moonshot.cn/v1', models: ['kimi-k2-0711-preview'] },
  together: { base_url: 'https://api.together.xyz/v1', models: [] },
  ollama: { base_url: 'http://host.docker.internal:11434/v1', models: [] },
}

const remoteModels = ref<string[]>([])
const fetchingModels = ref(false)
// 拉取成功提示 (errorMsg 在弹窗背后看不见, 单独给按钮旁反馈)
const fetchMsg = ref('')
// 模型元数据 (effort 上限 + 能力位, 选模型后自动填充, create/edit 各存一份)
const createMeta = ref<ModelMeta | null>(null)
const editMeta = ref<ModelMeta | null>(null)

function metaOf(form: 'create' | 'edit'): ModelMeta | null {
  return form === 'create' ? createMeta.value : editMeta.value
}

function effortHint(meta: ModelMeta | null): string {
  if (!meta || !meta.supported_efforts?.length) return ''
  const src = meta.max_effort_source === 'static' ? '已核定' : '默认全档'
  const caps: string[] = []
  if (meta.reasoning === true) caps.push('推理')
  else if (meta.reasoning === false) caps.push('非推理')
  if (meta.tool_call === true) caps.push('工具调用')
  const capStr = caps.length ? ` · ${caps.join('/')}` : ''
  return `支持档位 ${meta.supported_efforts.join(' / ')}（最高 ${meta.max_effort}，${src}${capStr}）`
}

// ── OpenCode Go 订阅: 聊天走 /chat/completions, Muse Spark 走 /responses ──
// base 存全路径 (…/go/v1 或 …/go/v1/responses), 运行时原样使用, 不再二次拼接
const OPENCODE_GO_URL = 'https://opencode.ai/zen/go/v1'
const OPENCODE_GO_RESPONSES_URL = 'https://opencode.ai/zen/go/v1/responses'
const OPENCODE_URLS = [OPENCODE_GO_URL, OPENCODE_GO_RESPONSES_URL]

/** muse-spark 模型 → go 订阅 /responses 全路径, 非 spark 返回 null */
function resolveBaseUrlForMuseSpark(model: string): string | null {
  const m = model.trim().toLowerCase()
  if (!m.includes('muse-spark')) return null
  return OPENCODE_GO_RESPONSES_URL
}

/** 选了 muse-spark 自动纠正 base_url (+provider), 不动用户自定义镜像 */
function autoBaseUrlForMuseSpark(f: Record<string, unknown>): void {
  const cur = String(f.base_url ?? '')
  const want = resolveBaseUrlForMuseSpark(String(f.model_name ?? ''))
  if (want) {
    const knownUrls = [...Object.values(PROVIDER_PRESETS).map(p => p.base_url), ...OPENCODE_URLS]
    if (!cur || knownUrls.includes(cur) || cur.includes('opencode.ai')) {
      if (cur !== want) {
        f.base_url = want
        if (String(f.provider ?? '') !== 'opencode') f.provider = 'opencode'
        fetchMsg.value = `Muse Spark 已自动切换 Base URL → ${want}`
      }
    }
    return
  }
  // 反向纠正：已切走非 spark 模型，但基址还停在 /responses（spark 专线）→ 切回 go/v1。
  // 10-12 教训：deepseek-v4.1-flash 配 /responses，主决策经 Responses 通道 + reasoning_effort 被网关拒收，全员 fallback“我先记下了”。
  if (cur === OPENCODE_GO_RESPONSES_URL) {
    f.base_url = OPENCODE_GO_URL
    fetchMsg.value = `非 Spark 模型已自动切回 Base URL → ${OPENCODE_GO_URL}`
  }
}

function providerOptions(current?: string): string[] {
  const set = new Set([...ALL_PROVIDERS, ...(current ? [current] : [])])
  return [...set].sort()
}

function onProviderChange(form: 'create' | 'edit') {
  const f = (form === 'create' ? createForm.value : editForm.value) as Record<string, unknown> & { base_url?: string }
  const preset = PROVIDER_PRESETS[String(f.provider ?? '')]
  if (!preset) { remoteModels.value = []; fetchMsg.value = ''; return }
  // base_url 为空或等于其他预设值时才覆盖, 不动用户手填的自定义地址
  // opencode 例外: go/v1 与 go/v1/responses 同属 go 订阅, 已在其中则不强制回 GO
  const knownUrls = [...Object.values(PROVIDER_PRESETS).map(p => p.base_url), ...OPENCODE_URLS]
  if (String(f.provider ?? '') === 'opencode' && OPENCODE_URLS.includes(String(f.base_url ?? ''))) {
    // 保持当前 go 端点, 仅刷新备选模型
  } else if (!f.base_url || knownUrls.includes(f.base_url)) {
    f.base_url = preset.base_url
  }
  // provider 切到 opencode 时若模型已是 spark, 直接升到 /responses
  remoteModels.value = preset.models
  fetchMsg.value = ''
  autoBaseUrlForMuseSpark(f as Record<string, unknown>)
}

function modelOptions(form: 'create' | 'edit'): string[] {
  const f = (form === 'create' ? createForm.value : editForm.value) as Record<string, unknown>
  return [...new Set([...remoteModels.value, ...(PROVIDER_PRESETS[String(f.provider ?? '')]?.models ?? [])])]
}

async function doFetchModels(form: 'create' | 'edit') {
  const f = (form === 'create' ? createForm.value : editForm.value) as Record<string, unknown>
  if (!f.base_url) { errorMsg.value = '先填 Base URL'; return }
  fetchingModels.value = true
  errorMsg.value = ''
  fetchMsg.value = ''
  try {
    const configId = form === 'edit' ? editingId.value ?? undefined : undefined
    remoteModels.value = await fetchRemoteModels(
      String(f.base_url), String(f.api_key ?? ''), configId,
    )
    if (!remoteModels.value.length) {
      errorMsg.value = 'Provider 返回空模型列表'
    } else {
      // datalist 按输入值过滤, 输入框已有 mimo-v2.5 时用户看不到全量 → 给明确计数 + 下拉直选
      fetchMsg.value = `已拉取 ${remoteModels.value.length} 个模型, 请从下拉选择`
    }
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '拉取模型列表失败'
  } finally { fetchingModels.value = false }
}

function pickModel(form: 'create' | 'edit', model: string) {
  const f = (form === 'create' ? createForm.value : editForm.value) as Record<string, unknown>
  if (!model) return
  f.model_name = model
  onModelChange(form)
}

async function onModelChange(form: 'create' | 'edit') {
  const f = (form === 'create' ? createForm.value : editForm.value) as Record<string, unknown>
  const setMeta = form === 'create' ? createMeta : editMeta
  if (!f.model_name) { setMeta.value = null; return }
  // 选了 muse-spark 先纠正 base_url (+provider), 再查元数据
  autoBaseUrlForMuseSpark(f)
  try {
    const meta = await fetchModelMeta(String(f.model_name), String(f.provider ?? ''))
    setMeta.value = meta
    if (meta.context_window) f.context_window = meta.context_window
  } catch { setMeta.value = null /* 元数据失败静默, 用户可手填 */ }
}

function defaultProvider(tab: TabKey): string {
  return tab === 'vlm' ? 'claude' : 'deepseek'
}

const createForm = ref<LLMConfigCreate>({
  name: '', provider: 'deepseek', provider_name: '', api_key: '', base_url: '', model_name: '',
})

function openCreate() {
  createForm.value = {
    name: '', provider: defaultProvider(activeTab.value), provider_name: '', api_key: '',
    base_url: '', model_name: '',
  }
  showCreate.value = true
  errorMsg.value = ''
  fetchMsg.value = ''
  createMeta.value = null
}

function cancelCreate() {
  showCreate.value = false
  errorMsg.value = ''
  fetchMsg.value = ''
}

async function doCreate() {
  if (!createForm.value.name || !createForm.value.model_name) {
    errorMsg.value = '名称和模型名为必填项'
    return
  }
  creating.value = true
  errorMsg.value = ''
  try {
    createForm.value.config_type = activeTab.value === 'vlm' ? 'vlm' : 'llm'
    const cw = createForm.value.context_window
    if (cw) createForm.value.context_window = Number(cw) || null
    await createLLM(createForm.value)
    showCreate.value = false
    if (activeTab.value === 'llm') await loadLLM()
    else await loadVLM()
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '创建失败'
  } finally { creating.value = false }
}

// ── 编辑 ──
const editingId = ref<number | null>(null)
const saving = ref(false)
const editForm = ref<LLMConfigUpdate>({})

function startEdit(c: LLMConfigItem) {
  editingId.value = c.id
  editForm.value = {
    name: c.name, provider: c.provider, provider_name: c.provider_name,
    model_name: c.model_name, base_url: c.base_url, api_key: '',
    context_window: c.context_window ?? null,
  }
  errorMsg.value = ''
  fetchMsg.value = ''
  editMeta.value = null
  // 进编辑弹窗即预填该 provider 离线兜底, 避免下拉只有一项
  remoteModels.value = PROVIDER_PRESETS[String(c.provider ?? '')]?.models ?? []
  // 有模型名则顺手查一次 effort 上限
  if (c.model_name) void onModelChange('edit')
}

function cancelEdit() {
  editingId.value = null
  editForm.value = {}
  errorMsg.value = ''
  fetchMsg.value = ''
}

async function doUpdate() {
  if (!editingId.value) return
  const data: LLMConfigUpdate = {}
  for (const [k, v] of Object.entries(editForm.value)) {
    if (v !== '' && v !== undefined) (data as Record<string, unknown>)[k] = v
  }
  saving.value = true
  errorMsg.value = ''
  try {
    await updateLLM(editingId.value, data)
    editingId.value = null
    editForm.value = {}
    if (activeTab.value === 'llm') await loadLLM()
    else await loadVLM()
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '更新失败'
  } finally { saving.value = false }
}

// ── 删除 ──
const deleting = ref<number | null>(null)

async function doDelete(configId: number) {
  if (!confirm('确定删除此配置？')) return
  deleting.value = configId
  errorMsg.value = ''
  try {
    await deleteLLM(configId)
    if (activeTab.value === 'llm') await loadLLM()
    else await loadVLM()
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e.message || '删除失败'
  } finally { deleting.value = null }
}

// ── 温度 ──
// ── 温度默认值 (与后端 _TEMP_DEFAULTS 一致，避免 API 返回前闪白) ──
const TEMP_DEFAULTS: TemperatureMap = {
  tavern_group: 0.8,
  memory_extract: 0.2,
  context_compress: 0.3,
  cross_validation: 0.1,
}
const temps = ref<TemperatureMap>({ ...TEMP_DEFAULTS })
const tempSaving = ref(false)
const tempSaved = ref(false)

const TEMP_LABELS: Record<string, [string, string]> = {
  tavern_group: ['群聊', '群聊自然对话'],
  memory_extract: ['记忆提取', '低温度保证准确性'],
  context_compress: ['上下文压缩', '低温度保证信息不丢失'],
  cross_validation: ['交叉验证', '极低温度保证公正'],
}

async function loadTemps() {
  try {
    const data = await getTemperatures(currentBot.value)
    for (const key of Object.keys(TEMP_DEFAULTS)) {
      temps.value[key] = data[key] ?? TEMP_DEFAULTS[key]
    }
  } catch (e) {
    // API 失败时保持 TEMP_DEFAULTS，不影响使用
  }
}
async function saveTemps() {
  tempSaving.value = true
  tempSaved.value = false
  try {
    temps.value = await updateTemperatures({ ...temps.value }, currentBot.value)
    tempSaved.value = true
    setTimeout(() => (tempSaved.value = false), 2000)
  } catch { /* ignore */ }
  finally { tempSaving.value = false }
}

// ── 对话参数 ──
const chatParams = ref<ChatParamItem[]>([])
const chatParamValues = ref<Record<string, any>>({})
const chatParamSaving = ref(false)
const chatParamSaved = ref(false)

async function loadChatParams() {
  try {
    chatParams.value = await getChatParams(currentBot.value)
    const vals: Record<string, any> = {}
    chatParams.value.forEach(p => { vals[p.key] = p.value })
    chatParamValues.value = vals
  } catch { /* ignore */ }
}
async function saveChatParams() {
  chatParamSaving.value = true
  chatParamSaved.value = false
  try {
    await updateChatParams(chatParamValues.value, currentBot.value)
    chatParamSaved.value = true
    setTimeout(() => (chatParamSaved.value = false), 2000)
  } catch { /* ignore */ }
  finally { chatParamSaving.value = false }
}

// ── 工具设置 ──
const toolSettings = ref<ToolSettingItem[]>([])
const toolConfigs = ref<ToolConfig[]>([])
const toolValues = ref<Record<string, any>>({})
const toolEnabled = ref<Record<string, boolean>>({})
const toolAffinity = ref<Record<string, number>>({})  // per-tool min_affinity
const toolSaving = ref(false)

// 好感等级选项 (与后端 affinity.py 对齐)
const AFFINITY_OPTIONS = [
  { value: -2, label: '黑名单 (-2)' },
  { value: -1, label: '疏远 (-1)' },
  { value: 0, label: '陌生 (0)' },
  { value: 1, label: '普通 (1)' },
  { value: 2, label: '熟悉 (2)' },
  { value: 3, label: '喜欢 (3)' },
  { value: 4, label: '亲密 (4)' },
  { value: 5, label: '珍视 (5)' },
]

function groupToolSettings() {
  const groups: Record<string, ToolSettingItem[]> = {}
  toolSettings.value.forEach(s => {
    const g = s.group || '其他'
    if (!groups[g]) groups[g] = []
    groups[g].push(s)
  })
  return groups
}

/** 按 category 分组 per-tool 配置 */
function groupedToolConfigs(): Record<string, ToolConfig[]> {
  const groups: Record<string, ToolConfig[]> = {}
  for (const t of toolConfigs.value) {
    const cat = t.category || '其他'
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(t)
  }
  // 每组内按 label 排序
  for (const g of Object.values(groups)) g.sort((a, b) => a.label.localeCompare(b.label))
  return groups
}

/** 切換单个工具的启停 */
function toggleTool(name: string, enabled: boolean) {
  toolEnabled.value[name] = enabled
}

async function loadToolSettings() {
  try {
    const data = await getToolSettings(currentBot.value)
    toolSettings.value = data.tool_settings
    toolConfigs.value = data.tools
    const vals: Record<string, any> = {}
    toolSettings.value.forEach(s => {
      if (s.type === 'float') vals[s.key] = parseFloat(String(s.value))
      else if (s.type === 'int') vals[s.key] = parseInt(String(s.value))
      else vals[s.key] = s.value
    })
    toolValues.value = vals
    // 提取 per-tool enabled + min_affinity 状态
    const enabled: Record<string, boolean> = {}
    const aff: Record<string, number> = {}
    data.tools.forEach((t: ToolConfig) => {
      enabled[t.name] = t.enabled
      aff[t.name] = t.min_affinity ?? 1
    })
    toolEnabled.value = enabled
    toolAffinity.value = aff
  } catch { /* ignore */ }
}

async function saveToolSettings() {
  toolSaving.value = true
  try {
    // 构建 per-tool payload: {name: {enabled, min_affinity}}
    const toolsPayload: Record<string, { enabled: boolean; min_affinity?: number }> = {}
    for (const t of toolConfigs.value) {
      toolsPayload[t.name] = {
        enabled: toolEnabled.value[t.name] !== false,
        min_affinity: toolAffinity.value[t.name] ?? 1,
      }
    }
    const data = await updateToolSettings(
      { tool_settings: toolValues.value, tools: toolsPayload },
      currentBot.value,
    )
    toolSettings.value = data.tool_settings
    toolConfigs.value = data.tools
    const enabled: Record<string, boolean> = {}
    const aff: Record<string, number> = {}
    data.tools.forEach((t: ToolConfig) => {
      enabled[t.name] = t.enabled
      aff[t.name] = t.min_affinity ?? 1
    })
    toolEnabled.value = enabled
    toolAffinity.value = aff
  } catch { /* ignore */ }
  finally { toolSaving.value = false }
}

// ── Token 用量 ──
const tokenStats = ref<TokenStats | null>(null)
const dbStats = ref<Record<string, any> | null>(null)
const tokenPeriod = ref('today')

async function loadTokens() {
  try {
    tokenStats.value = await getTokenStats(tokenPeriod.value)
    const status = await getStatus()
    dbStats.value = status.db
  } catch { /* ignore */ }
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

// ── Tab 切换 ──
function switchTab(key: TabKey) {
  activeTab.value = key
  showCreate.value = false
  editingId.value = null
  const loaders: Record<TabKey, () => void> = {
    llm: loadLLM, vlm: loadVLM, draw: loadDraw, temp: loadTemps, chat: loadChatParams,
    tools: loadToolSettings, tokens: loadTokens,
  }
  loaders[key]()
}

// ── Bot 切换时重新加载 ──
watch(currentBot, () => {
  loadSettings()
  switchTab(activeTab.value)
})

onMounted(() => { loadSettings(); loadLLM() })

function getProviderLabel(p: string) {
  const map: Record<string, string> = {
    deepseek: 'DeepSeek', openai: 'OpenAI', anthropic: 'Anthropic',
    gemini: 'Gemini', ollama: 'Ollama', llama: 'Llama',
    gpt4v: 'GPT-4V', claude: 'Claude', nano_banana: 'Nano Banana',
    vectorengine: 'VectorEngine', openrouter: 'OpenRouter', together: 'Together',
    custom: '自定义',
  }
  return map[p] || p
}

function groupedParams() {
  const groups: Record<string, ChatParamItem[]> = {}
  chatParams.value.forEach(p => {
    const g = p.group || '其他'
    if (!groups[g]) groups[g] = []
    groups[g].push(p)
  })
  return groups
}

// ── 获取当前 tab 的配置列表和槽位 ──
function currentList() { return activeTab.value === 'llm' ? llmList : vlmList }
function currentConfigs(): LLMConfigItem[] { return currentList()?.value?.configs ?? [] }
function currentSlots(): Record<string, number | null> {
  const raw = currentList()?.value?.slots ?? {}
  // 过滤已移除/退役的槽位 (仲裁功能 2026-06-30 删除; llm_secondary 无消费方 2026-10-15 退役)
  const filtered: Record<string, number | null> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (k !== 'llm_judge' && k !== 'llm_opus' && k !== 'llm_secondary') filtered[k] = v
  }
  // VLM 页只保留识图 (vlm_secondary 已退役无消费方，绘图走独立绘图页指派)
  if (activeTab.value === 'vlm') {
    const kept: Record<string, number | null> = {}
    if ('vlm_primary' in filtered) kept['vlm_primary'] = filtered['vlm_primary']
    return kept
  }
  return filtered
}

function slotLabel(slotKey: string, tab: string): string {
  if (tab === 'llm') {
    // 链路统一命名 (与后端 CHAIN_SLOT_MAP 同源)：一行 = 一条链路
    const map: Record<string, string> = {
      llm_primary: '主链·脑/嘴/私聊',
      llm_lite: '闲聊·后台/压缩',
      llm_pro: '推理·绘图spec',
      llm_gate: '意图闸·Slim',
      llm_judge: '仲裁',
      shadow_agent: '影子Agent',
    }
    return map[slotKey] || slotKey
  }
  return '识图'  // VLM 页仅剩 vlm_primary (绘图槽已退役，见 currentSlots)
}
</script>

<template>
  <div class="bot-config-page">
    <!-- Bot 状态卡片 -->
    <div class="bot-bar" v-if="settings">
      <div class="bot-info">
        <select class="bot-selector" :value="currentBot" @change="switchBot(($event.target as HTMLSelectElement).value)">
          <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
        </select>
        <span class="bot-qq">{{ settings.bot_id }}</span>
      </div>
      <div class="bot-toggles">
        <label class="toggle-label">
          <MessageSquare :size="14" />
          <span>群聊</span>
          <input type="checkbox" :checked="settings.group_chat_enabled"
            @change="toggleChat('group_chat_enabled', ($event.target as HTMLInputElement).checked)" />
        </label>
        <label class="toggle-label">
          <User :size="14" />
          <span>私聊</span>
          <input type="checkbox" :checked="settings.private_chat_enabled"
            @change="toggleChat('private_chat_enabled', ($event.target as HTMLInputElement).checked)" />
        </label>
        <label class="toggle-label">
          <Brain :size="14" />
          <span>思考</span>
          <input type="checkbox" :checked="settings.reasoning_enabled"
            @change="toggleChat('reasoning_enabled', ($event.target as HTMLInputElement).checked)" />
        </label>
        <label class="toggle-label">
          <span>默认档</span>
          <select class="effort-select" :value="settings.thinking_effort || 'auto'"
            @change="saveThinkingEffort(($event.target as HTMLSelectElement).value)"
            title="默认档位: 全局唯一设置, 槽位跟随全局=用它; 自动=Gate 按意图决定, 关闭=硬关">
            <option v-for="o in THINKING_EFFORTS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>
        <label class="toggle-label">
          <Eye :size="14" />
          <span>影子</span>
          <input type="checkbox" :checked="settings.shadow_agent_enabled"
            @change="toggleChat('shadow_agent_enabled', ($event.target as HTMLInputElement).checked)" />
        </label>
      </div>
    </div>

    <!-- 管理与隔离区设置 -->
    <div class="admin-card" v-if="settings">
      <div class="admin-field">
        <label class="field-label">
          <Shield :size="14" />
          <span>管理员 QQ</span>
        </label>
        <input class="input admin-input" type="text" v-model="settings.owner_qq" placeholder="100000005"
          @blur="saveOwnerQQ" @keyup.enter="($event.target as HTMLInputElement).blur()" />
        <p class="field-hint">主人鉴权唯一依据——显示名可伪造，QQ 号不可。</p>
      </div>
      <div class="nsfw-panel" :class="{ on: settings.nsfw_enabled }">
        <div class="nsfw-head">
          <div class="nsfw-title">
            <Flame :size="16" />
            <span>成人向隔离区</span>
            <span :class="['badge', settings.nsfw_enabled ? 'badge-nsfw-on' : 'badge-nsfw-off']">
              {{ settings.nsfw_enabled ? '已开启' : '已关闭' }}
            </span>
          </div>
          <label class="switch" title="仅对当前 Bot 生效，默认关闭">
            <input type="checkbox" :checked="settings.nsfw_enabled"
              @change="toggleChat('nsfw_enabled', ($event.target as HTMLInputElement).checked)" />
            <span class="slider"></span>
          </label>
        </div>
        <p class="nsfw-desc">
          指定群 + 指定人放宽成人合意内容，未成年 / 非合意 / 性暴力 / 解除伦理约束永远拦截。
          改动即时写入 DB，重启对应 Bot 容器生效。
        </p>
        <div v-if="settings.nsfw_enabled" class="nsfw-grid">
          <div class="nsfw-field">
            <label>生效群 <span class="required">*</span></label>
            <input class="input" type="text" v-model="settings.nsfw_groups" placeholder="例如 123456789, 987654321"
              @blur="saveNsfw" @keyup.enter="($event.target as HTMLInputElement).blur()" />
            <p class="field-hint">群号，逗号分隔；为空则任何群都不生效。</p>
          </div>
          <div class="nsfw-field">
            <label>生效用户</label>
            <input class="input" type="text" v-model="settings.nsfw_users" placeholder="留空 = 仅超管"
              @blur="saveNsfw" @keyup.enter="($event.target as HTMLInputElement).blur()" />
            <p class="field-hint">QQ 号，逗号分隔；留空时只有超管能触发。</p>
          </div>
          <div class="nsfw-field nsfw-field-wide">
            <label>私聊通道</label>
            <div class="nsfw-inline-toggle">
              <label class="switch" title="私聊没有群号，群白名单不适用">
                <input type="checkbox" :checked="settings.nsfw_private"
                  @change="toggleChat('nsfw_private', ($event.target as HTMLInputElement).checked)" />
                <span class="slider"></span>
              </label>
              <span :class="['badge', settings.nsfw_private ? 'badge-nsfw-on' : 'badge-nsfw-off']">
                {{ settings.nsfw_private ? '已开启' : '已关闭' }}
              </span>
            </div>
            <p class="field-hint">
              私聊没有群号，群白名单不适用，所以单列一个开关。
              开启后「生效用户」名单在私聊同样生效（留空 = 仅超管）。
              只开群内 NSFW 的话，这里保持关闭即可——两者互不影响。
            </p>
          </div>
        </div>
        <p v-if="nsfwSaved" class="nsfw-saved">已保存 {{ nsfwSaved }}</p>
      </div>
    </div>

    <!-- Tab 导航 -->
    <div class="tab-bar">
      <button v-for="t in tabs" :key="t.key" :class="['tab-btn', { active: activeTab === t.key }]" @click="switchTab(t.key)">
        <component :is="t.icon" :size="15" />
        <span>{{ t.label }}</span>
      </button>
    </div>

    <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>

    <!-- LLM / VLM 槽位 (含 Provider CRUD) -->
    <div v-if="activeTab === 'llm' || activeTab === 'vlm'" class="panel">
      <p v-if="activeTab === 'llm'" class="nsfw-link-hint">
        <Flame :size="12" />
        <span>NSFW 隔离区场景的模型 / 档位 / 温度在
          <router-link to="/nsfw-config">「NSFW 配置」</router-link> 页单独设置；未配置项自动跟随本页（SFW）。</span>
      </p>
      <!-- 槽位下拉 -->
      <div class="slot-selects">
        <div v-for="(_, slotKey) in currentSlots()" :key="slotKey" class="slot-field">
          <label>{{ slotLabel(slotKey, activeTab) }}</label>
          <div class="slot-row">
            <select :value="currentSlots()[slotKey] ?? ''"
              @change="doAssignSlot(activeTab, slotKey, parseInt(($event.target as HTMLSelectElement).value) || null)">
              <option value="">— 未分配 —</option>
              <option v-for="c in currentConfigs()" :key="c.id" :value="c.id">{{ c.name }} ({{ getProviderLabel(c.provider) }})</option>
            </select>
            <select v-if="showSlotEffort(String(slotKey))" class="effort-select" :value="slotEffort(String(slotKey))"
              @change="saveSlotEffort(String(slotKey), ($event.target as HTMLSelectElement).value)"
              title="该槽位的思考档位: 跟随全局=用顶栏档位">
              <option v-for="o in SLOT_EFFORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <button v-if="activeTab === 'llm'" class="btn btn-xs" :disabled="rowTestRunning[String(slotKey)]"
              @click="runRowTest(String(slotKey))" title="用本行当前生效档位测连通">
              {{ rowTestRunning[String(slotKey)] ? '测试中…' : '测试' }}
            </button>
          </div>
          <div v-if="activeTab === 'llm' && rowTestResult[String(slotKey)]" class="fetch-hint"
            :class="rowTestResult[String(slotKey)]!.ok ? '' : 'error-msg'" style="font-size:12px">
            <template v-if="rowTestResult[String(slotKey)]!.ok">
              ✅ {{ rowTestResult[String(slotKey)]!.output_preview }} ·
              token {{ rowTestResult[String(slotKey)]!.usage?.prompt_tokens }}/{{ rowTestResult[String(slotKey)]!.usage?.completion_tokens }}（思考{{ rowTestResult[String(slotKey)]!.usage?.reasoning_tokens ?? 0 }}）
              <template v-if="rowTestResult[String(slotKey)]!.tier_warning">⚠️ {{ rowTestResult[String(slotKey)]!.tier_warning }}</template>
            </template>
            <template v-else>❌ {{ rowTestResult[String(slotKey)]!.error }}</template>
          </div>
        </div>
      </div>

      <!-- 行内测试：见各槽位行尾“测试”按钮 -->

      <!-- 新建面板 -->
      <div v-if="showCreate" class="create-panel">
        <div class="panel-title">新建 {{ activeTab === 'vlm' ? 'VLM' : 'LLM' }} 配置</div>
        <div class="form-grid">
          <div class="form-field">
            <label>名称 <span class="required">*</span></label>
            <input v-model="createForm.name" placeholder="例如: DeepSeek 生产" class="input" />
          </div>
          <div class="form-field">
            <label>Provider</label>
            <select v-model="createForm.provider" class="input" @change="onProviderChange('create')">
              <option v-for="p in providerOptions(createForm.provider)" :key="p" :value="p">{{ p }}</option>
            </select>
          </div>
          <div class="form-field">
            <label>模型名 <span class="required">*</span></label>
            <div style="display:flex; gap:8px">
              <input v-model="createForm.model_name" list="create-model-list" placeholder="deepseek-v4-flash" class="input" @change="onModelChange('create')" />
              <button class="btn btn-sm" :disabled="fetchingModels" @click="doFetchModels('create')">{{ fetchingModels ? '拉取中…' : '获取模型' }}</button>
            </div>
            <datalist id="create-model-list"><option v-for="m in modelOptions('create')" :key="m" :value="m" /></datalist>
            <select
              v-if="modelOptions('create').length > 1"
              class="input"
              :value="String(createForm.model_name ?? '')"
              @change="pickModel('create', ($event.target as HTMLSelectElement).value)"
              title="从拉取列表直选"
            >
              <option value="">— 从拉取列表选择 ({{ modelOptions('create').length }} 个) —</option>
              <option v-for="m in modelOptions('create')" :key="m" :value="m">{{ m }}</option>
            </select>
            <small v-if="fetchMsg" class="fetch-hint">{{ fetchMsg }}</small>
            <small v-if="effortHint(metaOf('create'))" class="fetch-hint">⚡ {{ effortHint(metaOf('create')) }}</small>
          </div>
          <div class="form-field">
            <label>上下文窗口 (tokens)</label>
            <input v-model="createForm.context_window" type="number" class="input" placeholder="选模型后自动填充" />
          </div>
          <div class="form-field" style="grid-column: 1 / -1">
            <label>Base URL</label>
            <input v-model="createForm.base_url" placeholder="选 Provider 自动填充" class="input" />
          </div>
          <div class="form-field" style="grid-column: 1 / -1">
            <label>API Key</label>
            <input v-model="createForm.api_key" type="password" placeholder="sk-..." class="input" />
          </div>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" :disabled="creating" @click="doCreate">{{ creating ? '创建中...' : '创建' }}</button>
          <button class="btn" @click="cancelCreate">取消</button>
        </div>
      </div>

      <!-- 配置列表 + 操作栏 -->
      <div class="table-toolbar">
        <span>{{ currentConfigs().length }} 个配置</span>
        <button class="btn btn-primary btn-sm" @click="openCreate" v-if="!showCreate">
          <Plus :size="14" style="margin-right:4px" />新建配置
        </button>
      </div>

      <div class="overflow-auto">
        <table v-if="currentConfigs().length > 0">
          <thead>
            <tr><th>ID</th><th>名称</th><th>Provider</th><th>模型</th><th>Key</th><th>已分配槽位</th><th style="width:80px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in currentConfigs()" :key="c.id">
              <td>{{ c.id }}</td>
              <td><strong>{{ c.name }}</strong></td>
              <td><span class="badge" :class="activeTab === 'llm' ? 'badge-llm' : 'badge-vlm'">{{ getProviderLabel(c.provider) }}</span></td>
              <td>{{ c.model_name || '—' }}</td>
              <td><code class="masked-key">{{ c.api_key_preview }}</code></td>
              <td>
                <template v-for="(slotId, slotKey) in currentSlots()" :key="slotKey">
                  <span v-if="slotId === c.id" class="badge badge-active" style="margin-right:4px">{{ slotLabel(slotKey, activeTab) }}</span>
                </template>
                <span v-if="!Object.values(currentSlots()).includes(c.id)" class="dimmed">—</span>
              </td>
              <td>
                <div class="row-actions">
                  <button class="btn btn-sm" @click="startEdit(c)"><Pencil :size="12" /></button>
                  <button class="btn btn-sm btn-danger-outline" :disabled="deleting === c.id" @click="doDelete(c.id)"><Trash2 :size="12" /></button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-state">暂无配置，点击「新建配置」添加 Provider</div>
      </div>
    </div>

    <!-- 绘图分配 (按 Bot 指派供应商；Key 与参数去各自独立配置页) -->
    <div v-if="activeTab === 'draw'" class="panel">
      <div v-if="drawLoading" class="empty-state">加载中…</div>
      <template v-else-if="drawCfg">
        <div class="assign-list">
          <div v-for="b in drawCfg.bots" :key="b.id" class="assign-row">
            <span class="assign-name">{{ b.name }} <span class="dimmed">{{ b.id }}</span></span>
            <label><input type="radio" :name="'draw-' + b.id" value="novelai" v-model="drawAssign[b.id]" />
              NovelAI <span v-if="!drawCfg.novelai.token_set" class="dimmed">（未配 Key）</span></label>
            <label><input type="radio" :name="'draw-' + b.id" value="pixai" v-model="drawAssign[b.id]" />
              PixAI <span v-if="!drawCfg.pixai.key_set" class="dimmed">（未配 Key）</span></label>
            <label><input type="radio" :name="'draw-' + b.id" value="none" v-model="drawAssign[b.id]" /> 停用</label>
          </div>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" :disabled="drawSaving" @click="saveDrawAssign">{{ drawSaving ? '保存中...' : '保存分配' }}</button>
        </div>
        <p class="field-hint" style="margin-top:12px">构思（意图→提示词）走 LLM 槽位下的推理位，模型与档位去 LLM 槽位页调整。</p>
      </template>
    </div>

    <!-- 温度设置 -->
    <div v-if="activeTab === 'temp'" class="panel">
      <div class="form-grid">
        <div v-for="([scenario, [label, desc]]) in Object.entries(TEMP_LABELS)" :key="scenario" class="form-field">
          <label><strong>{{ label }}</strong> <small>{{ desc }}</small></label>
          <div class="temp-input-group">
            <input type="number" min="0" max="2" step="0.05" v-model.number="temps[scenario]" class="input temp-num" />
          </div>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="tempSaving" @click="saveTemps" style="margin-top:12px">
        {{ tempSaving ? '保存中...' : tempSaved ? '已保存!' : '保存温度设置' }}
      </button>
    </div>

    <!-- 对话参数 -->
    <div v-if="activeTab === 'chat'" class="panel">
      <div v-for="(items, group) in groupedParams()" :key="group">
        <div class="section-label">{{ group }}</div>
        <div class="form-grid">
          <div v-for="p in items" :key="p.key" class="form-field">
            <label><strong>{{ p.label }}</strong> <small>{{ p.desc }}</small></label>
            <template v-if="p.type === 'bool'">
              <select v-model="chatParamValues[p.key]"><option :value="true">开启</option><option :value="false">关闭</option></select>
            </template>
            <template v-else-if="p.type === 'float'">
              <div class="range-group">
                <input type="range" :min="p.min" :max="p.max" :step="p.step" v-model.number="chatParamValues[p.key]" />
                <span class="range-val">{{ parseFloat(String(chatParamValues[p.key])).toFixed(2) }}</span>
              </div>
            </template>
            <template v-else>
              <input :type="p.type === 'int' ? 'number' : 'text'" v-model="chatParamValues[p.key]" :min="p.min" :max="p.max" :step="p.step" />
            </template>
          </div>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="chatParamSaving" @click="saveChatParams" style="margin-top:12px">
        {{ chatParamSaving ? '保存中...' : chatParamSaved ? '已保存!' : '保存对话参数' }}
      </button>
    </div>

    <!-- 工具设置 -->
    <div v-if="activeTab === 'tools'" class="panel">
      <!-- 全局设置 -->
      <div v-for="(items, group) in groupToolSettings()" :key="group">
        <div class="section-label">{{ group }}</div>
        <div class="form-grid">
          <div v-for="s in items" :key="s.key" class="form-field">
            <label><strong>{{ s.label }}</strong> <small>{{ s.desc }}</small></label>
            <template v-if="s.type === 'bool'">
              <select v-model="toolValues[s.key]">
                <option :value="true">开启</option><option :value="false">关闭</option>
              </select>
            </template>
            <template v-else-if="s.key === 'tool_min_affinity'">
              <select v-model.number="toolValues[s.key]">
                <option v-for="opt in AFFINITY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </template>
            <template v-else>
              <div class="range-group">
                <input type="range" :min="s.min" :max="s.max" :step="s.step"
                  v-model.number="toolValues[s.key]" />
                <span class="range-val">{{ s.type === 'float' ? parseFloat(String(toolValues[s.key])).toFixed(1) : toolValues[s.key] }}{{ s.key === 'tool_call_timeout' ? ' 秒' : '' }}</span>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- 统一工具列表 — 按分类分组 -->
      <div v-for="(tools, cat) in groupedToolConfigs()" :key="cat" style="margin-top:16px">
        <div class="section-label">{{ cat }}</div>
        <div class="tool-table">
          <div v-for="t in tools" :key="t.name" class="tool-row">
            <div class="tool-info">
              <span class="tool-label">{{ t.label }}</span>
              <span class="tool-name-tag">{{ t.name }}</span>
              <span v-if="t.bot === 'both'" class="badge badge-shared">共享</span>
              <span v-else-if="t.bot === 'loput'" class="badge badge-loput">主 bot</span>
              <span v-else class="badge badge-luna">对照 bot</span>
              <span class="tool-desc">{{ t.desc }}</span>
            </div>
            <div class="tool-controls">
              <label class="toggle-label">
                <input type="checkbox" :checked="toolEnabled[t.name] !== false"
                  @change="toggleTool(t.name, ($event.target as HTMLInputElement).checked)" />
                <span>{{ toolEnabled[t.name] !== false ? '启用' : '禁用' }}</span>
              </label>
              <select class="affinity-select" v-model.number="toolAffinity[t.name]"
                :title="'最低好感要求: ' + AFFINITY_OPTIONS.find(o => o.value === toolAffinity[t.name])?.label">
                <option v-for="opt in AFFINITY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <button class="btn btn-primary" :disabled="toolSaving" @click="saveToolSettings" style="margin-top:12px">
        {{ toolSaving ? '保存中...' : '保存工具设置' }}
      </button>
    </div>

    <!-- 用量统计 -->
    <div v-if="activeTab === 'tokens'" class="panel">
      <div class="toolbar">
        <select v-model="tokenPeriod" @change="loadTokens">
          <option value="today">今天</option><option value="week">本周</option><option value="month">本月</option><option value="all">全部</option>
        </select>
      </div>
      <div class="stats-grid" v-if="tokenStats">
        <div class="stat-card"><div class="stat-val">{{ fmtNum(tokenStats.input_tokens) }}</div><div class="stat-lbl">Input Tokens</div></div>
        <div class="stat-card"><div class="stat-val">{{ fmtNum(tokenStats.output_tokens) }}</div><div class="stat-lbl">Output Tokens</div></div>
        <div class="stat-card"><div class="stat-val">{{ tokenStats.cache_hit_rate }}%</div><div class="stat-lbl">Cache Hit</div></div>
        <div class="stat-card"><div class="stat-val">{{ tokenStats.request_count }}</div><div class="stat-lbl">请求次数</div></div>
        <div class="stat-card"><div class="stat-val">¥{{ tokenStats.estimated_cost_cny }}</div><div class="stat-lbl">估算费用</div></div>
      </div>
      <div v-if="tokenStats?.by_scenario && Object.keys(tokenStats.by_scenario).length" style="margin-top:16px">
        <h4>按场景分布</h4>
        <div class="overflow-auto">
          <table>
            <thead><tr><th>场景</th><th>Input</th><th>Output</th><th>请求数</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in tokenStats.by_scenario" :key="k">
                <td>{{ k }}</td><td>{{ fmtNum(v.input_tokens) }}</td><td>{{ fmtNum(v.output_tokens) }}</td><td>{{ v.request_count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div v-if="dbStats" style="margin-top:16px">
        <h4>数据库</h4>
        <div class="stats-grid">
          <div class="stat-card"><div class="stat-val">{{ dbStats.user_count ?? 0 }}</div><div class="stat-lbl">用户数</div></div>
          <div class="stat-card"><div class="stat-val">{{ dbStats.memory_count ?? 0 }}</div><div class="stat-lbl">记忆条数</div></div>
          <div class="stat-card"><div class="stat-val">{{ dbStats.knowledge_sections ?? 0 }}</div><div class="stat-lbl">知识库章节</div></div>
          <div class="stat-card"><div class="stat-val">{{ dbStats.suspected_bots ?? 0 }}</div><div class="stat-lbl">疑似 Bot</div></div>
          <div class="stat-card"><div class="stat-val">{{ dbStats.db_size_kb ?? 0 }} KB</div><div class="stat-lbl">数据库</div></div>
        </div>
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <div v-if="editingId" class="modal-overlay" @mousedown.self="cancelEdit">
      <div class="modal">
        <div class="modal-header">
          <h3>编辑配置 #{{ editingId }}</h3>
          <button class="btn-close" @click="cancelEdit"><X :size="18" /></button>
        </div>
        <div class="form-grid" style="padding:18px 22px">
          <div class="form-field"><label>名称</label><input v-model="editForm.name" class="input" /></div>
          <div class="form-field"><label>Provider</label>
            <select v-model="editForm.provider" class="input" @change="onProviderChange('edit')">
              <option v-for="p in providerOptions(String(editForm.provider ?? ''))" :key="p" :value="p">{{ p }}</option>
            </select>
          </div>
          <div class="form-field"><label>模型名</label>
            <div style="display:flex; gap:8px">
              <input v-model="editForm.model_name" list="edit-model-list" class="input" @change="onModelChange('edit')" />
              <button class="btn btn-sm" :disabled="fetchingModels" @click="doFetchModels('edit')">{{ fetchingModels ? '拉取中…' : '获取模型' }}</button>
            </div>
            <datalist id="edit-model-list"><option v-for="m in modelOptions('edit')" :key="m" :value="m" /></datalist>
            <select
              v-if="modelOptions('edit').length > 1"
              class="input"
              :value="String(editForm.model_name ?? '')"
              @change="pickModel('edit', ($event.target as HTMLSelectElement).value)"
              title="从拉取列表直选"
            >
              <option value="">— 从拉取列表选择 ({{ modelOptions('edit').length }} 个) —</option>
              <option v-for="m in modelOptions('edit')" :key="m" :value="m">{{ m }}</option>
            </select>
            <small v-if="fetchMsg" class="fetch-hint">{{ fetchMsg }}</small>
            <small v-if="effortHint(metaOf('edit'))" class="fetch-hint">⚡ {{ effortHint(metaOf('edit')) }}</small>
            <small v-if="errorMsg" class="fetch-err">{{ errorMsg }}</small>
          </div>
          <div class="form-field"><label>上下文窗口 (tokens)</label><input v-model="editForm.context_window" type="number" class="input" placeholder="选模型后自动填充" /></div>
          <div class="form-field" style="grid-column: 1 / -1"><label>Base URL</label><input v-model="editForm.base_url" class="input" /></div>
          <div class="form-field" style="grid-column: 1 / -1"><label>API Key <span class="hint">留空则不修改</span></label><input v-model="editForm.api_key" type="password" placeholder="留空则不修改" class="input" /></div>
        </div>
        <div class="modal-footer">
          <button class="btn" @click="cancelEdit">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="doUpdate">{{ saving ? '保存中...' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bot-config-page { width: 100%; }

/* ── Bot 状态条 ── */
.bot-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; margin-bottom: 18px;
  background: linear-gradient(135deg, var(--primary-light) 0%, #e0e7ff 100%);
  border: 1px solid var(--primary-100); border-radius: var(--radius-lg);
  flex-wrap: wrap; gap: 12px;
}
.bot-info { display: flex; align-items: center; gap: 10px; }
.bot-selector {
  font-size: 15px; font-weight: 700; color: var(--text);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 4px 28px 4px 10px; background: var(--bg);
  cursor: pointer; outline: none; appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%236b7280'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 8px center;
}
.bot-selector:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(99,102,241,0.15); }
.bot-qq { font-size: 13px; color: var(--text-muted); font-family: var(--mono); }
.bot-toggles { display: flex; gap: 18px; }
.toggle-label {
  display: flex; align-items: center; gap: 7px;
  font-size: 13px; color: var(--text-secondary); cursor: pointer; user-select: none;
}
.toggle-label input[type="checkbox"] {
  width: 16px; height: 16px; cursor: pointer; accent-color: var(--primary);
}
.effort-select {
  padding: 4px 8px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 12px;
  background: var(--bg); color: var(--text); cursor: pointer;
  max-width: 150px;
}

/* ── 管理与隔离区卡片 ── */
.admin-card {
  display: grid; grid-template-columns: 280px 1fr; gap: 16px;
  margin-bottom: 18px;
}
@media (max-width: 900px) { .admin-card { grid-template-columns: 1fr; } }
.admin-field {
  padding: 16px 18px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius-lg);
}
.field-label {
  display: flex; align-items: center; gap: 7px;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  margin-bottom: 8px;
}
.admin-input { width: 100%; }
.field-hint { font-size: 11px; color: var(--text-muted); margin: 6px 0 0; line-height: 1.5; }

/* ── NSFW 隔离区面板 ── */
.nsfw-panel {
  padding: 16px 18px; border-radius: var(--radius-lg);
  background: var(--surface); border: 1px solid var(--border);
  transition: border-color 0.2s, background 0.2s;
}
.nsfw-panel.on {
  background: linear-gradient(135deg, #fff7ed 0%, var(--danger-light) 100%);
  border-color: #fecaca;
}
.nsfw-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.nsfw-title {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 700; color: var(--text);
}
.nsfw-panel.on .nsfw-title { color: #9a3412; }
.nsfw-title svg { color: var(--text-muted); }
.nsfw-panel.on .nsfw-title svg { color: #ea580c; }
.badge-nsfw-on { background: #ffedd5; color: #9a3412; }
.badge-nsfw-off { background: var(--primary-light); color: var(--text-muted); }
.nsfw-desc { font-size: 12px; color: var(--text-secondary); margin: 8px 0 0; line-height: 1.6; }
.nsfw-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 12px; }
@media (max-width: 700px) { .nsfw-grid { grid-template-columns: 1fr; } }
/* 私聊通道横跨两列 —— 它跟「生效群」是并列的通道选择，不是第三个名单 */
.nsfw-field-wide { grid-column: 1 / -1; }
.nsfw-inline-toggle { display: flex; align-items: center; gap: 10px; }
.nsfw-field { display: flex; flex-direction: column; gap: 5px; }
.nsfw-field label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.nsfw-field .required { color: var(--danger); }
.nsfw-saved { font-size: 11px; color: #065f46; margin: 8px 0 0; }

/* ── 开关 (switch) ── */
.switch { position: relative; display: inline-block; width: 40px; height: 22px; flex-shrink: 0; cursor: pointer; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider {
  position: absolute; inset: 0; border-radius: 9999px;
  background: var(--border); transition: background 0.2s;
}
.switch .slider::before {
  content: ""; position: absolute; width: 16px; height: 16px;
  left: 3px; top: 3px; border-radius: 50%;
  background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.25);
  transition: transform 0.2s;
}
.switch input:checked + .slider { background: #ea580c; }
.switch input:checked + .slider::before { transform: translateX(18px); }

/* ── Panel ── */
.panel { padding: 4px 0; }

/* ── NSFW 配置页提示 (LLM 槽位页 → /nsfw-config) ── */
.nsfw-link-hint {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-muted);
  margin: 0 0 12px; padding: 8px 12px;
  background: #fff7ed; border: 1px solid #fed7aa; border-radius: var(--radius);
}
.nsfw-link-hint svg { color: #ea580c; flex-shrink: 0; }
.nsfw-link-hint a { color: #9a3412; font-weight: 600; }

/* ── 槽位选择 ── */
.slot-selects { display: flex; gap: 16px; margin-bottom: 18px; flex-wrap: wrap; }
.slot-field { display: flex; flex-direction: column; gap: 5px; }
.slot-field label {
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.slot-field select {
  padding: 8px 12px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 13px;
  background: var(--surface); min-width: 240px; color: var(--text);
}
.slot-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.slot-row .effort-select { min-width: 110px; }
.slot-field select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,0.1); }

/* ── Table toolbar ── */
.table-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; font-size: 13px; color: var(--text-muted);
}

/* ── 新建面板 ── */
.create-panel {
  padding: 18px; margin-bottom: 18px;
  background: var(--surface-hover); border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.panel-title { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 14px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-field { display: flex; flex-direction: column; gap: 5px; }
.form-field label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.form-field label small { font-weight: 400; color: var(--text-muted); font-size: 11px; }
.form-field .required { color: var(--danger); }
.form-field .hint { font-weight: 400; color: var(--text-muted); font-size: 11px; }
.form-actions { display: flex; gap: 8px; margin-top: 14px; }

/* ── 表单元素 ── */
.range-group { display: flex; align-items: center; gap: 10px; }
.range-group input[type="range"] { flex: 1; }
.range-val { min-width: 3rem; text-align: right; font-weight: 700; font-size: 14px; color: var(--text); }
.temp-input-group { display: flex; align-items: center; }
.temp-num { width: 100px; text-align: center; font-weight: 600; }
.section-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--text-muted); margin: 18px 0 10px; padding-bottom: 5px;
  border-bottom: 1px solid var(--border-light);
}

/* ── 统计 ── */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
.stat-card {
  text-align: center; padding: 16px 10px;
  background: var(--surface); border-radius: var(--radius);
  border: 1px solid var(--border-light);
  transition: box-shadow 0.2s;
}
.stat-card:hover { box-shadow: var(--shadow-sm); }
.stat-val { font-size: 22px; font-weight: 700; color: var(--primary); }
.stat-lbl { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

/* ── 徽章 / 标签 ── */
.badge {
  display: inline-flex; align-items: center;
  font-size: 11px; padding: 2px 10px; border-radius: 9999px; font-weight: 500;
}
.badge-llm { background: var(--primary-light); color: var(--primary-700); }
.badge-vlm { background: #f5f3ff; color: #6d28d9; }
.badge-active { background: #ecfdf5; color: #065f46; }
.badge-shared { background: var(--primary-light); color: var(--primary-700); }
.badge-loput { background: #ecfdf5; color: #065f46; }
.badge-luna { background: #fdf2f8; color: #9d174d; }

/* ── Row actions ── */
.row-actions { display: flex; gap: 4px; }
.btn-danger-outline { color: var(--danger); border-color: #fecaca; }

/* ── 绘图分配 Tab ── */
.draw-hint { font-size: 12px; color: var(--text-muted); margin: 0 0 12px; }
.assign-list { display: flex; flex-direction: column; gap: 10px; }
.assign-row { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; padding: 10px 12px; border: 1px solid var(--border-light); border-radius: 8px; }
.assign-name { font-size: 13px; font-weight: 600; color: var(--text); min-width: 180px; }
.assign-row label { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); }
.btn-danger-outline:hover:not(:disabled) { background: var(--danger); color: #fff; border-color: var(--danger); }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.35);
  display: flex; align-items: center; justify-content: center;
  z-index: 200; backdrop-filter: blur(3px);
}
.modal {
  background: var(--surface); padding: 0; border-radius: var(--radius-lg);
  width: 540px; max-width: 92vw; box-shadow: var(--shadow-lg); overflow: hidden;
}
.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 24px; border-bottom: 1px solid var(--border-light);
}
.modal-header h3 { margin: 0; font-size: 16px; color: var(--text); }
.btn-close {
  background: none; border: none; cursor: pointer; color: var(--text-muted);
  padding: 4px; border-radius: var(--radius-sm); display: flex;
}
.btn-close:hover { background: var(--surface-hover); color: var(--text); }
.modal-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 14px 24px; border-top: 1px solid var(--border-light);
  background: var(--surface-hover);
}

/* ── 通用 ── */
.masked-key { font-size: 12px; font-family: var(--mono); }
.fetch-hint { font-size: 11px; color: #065f46; margin-top: 4px; }
.fetch-err { font-size: 11px; color: var(--danger); margin-top: 4px; }
.dimmed { color: var(--text-muted); font-size: 12px; }
.empty-state { text-align: center; padding: 28px; color: var(--text-muted); font-size: 13px; }
.overflow-auto { overflow-x: auto; }
.error-msg {
  background: var(--danger-light); color: #991b1b;
  border: 1px solid #fecaca; border-radius: var(--radius-sm);
  padding: 10px 14px; margin-bottom: 14px; font-size: 13px;
}

/* ── 工具统一表格 ── */
.tool-table {
  display: flex; flex-direction: column; gap: 1px;
  background: var(--border); border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden;
}
.tool-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 11px 16px; background: var(--surface); gap: 14px; flex-wrap: wrap;
}
.tool-row:hover { background: var(--surface-hover); }
.tool-info { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; flex-wrap: wrap; }
.tool-label { font-size: 14px; font-weight: 600; color: var(--text); }
.tool-name-tag {
  font-size: 11px; font-family: var(--mono); color: var(--text-secondary);
  background: var(--surface-hover); padding: 2px 8px; border-radius: 4px;
}
.tool-desc {
  font-size: 12px; color: var(--text-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tool-controls { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.affinity-select {
  font-size: 11px; padding: 4px 8px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text-secondary);
  background: var(--surface); cursor: pointer; min-width: 90px;
}
.affinity-select:hover, .affinity-select:focus { border-color: var(--primary); outline: none; }

/* ── 表格 ── */
table { font-size: 13px; width: 100%; }
th {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--text-muted); text-align: left; padding: 8px 12px;
}
td { padding: 8px 12px; }
.toolbar { display: flex; gap: 8px; margin-bottom: 14px; }
.toolbar select {
  padding: 7px 12px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); font-size: 13px;
  background: var(--surface); color: var(--text);
}
h4 { font-size: 14px; font-weight: 600; color: var(--text-secondary); margin: 0 0 10px; }

/* ── 输入框 ── */
input[type="text"], input[type="number"], input[type="password"] {
  padding: 7px 11px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 13px;
  font-family: var(--sans); color: var(--text); background: var(--surface);
  transition: border-color 0.15s;
}
input:focus, select:focus {
  outline: none; border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(99,102,241,0.1);
}
select {
  padding: 7px 11px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 13px;
  background: var(--surface); color: var(--text);
}
</style>
