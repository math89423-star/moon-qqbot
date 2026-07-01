<script setup lang="ts">
/** 机器人配置 — LLM/VLM 槽位(含 Provider CRUD) + 开关 + 温度 + 参数 + 用量 */
import { ref, inject, onMounted, watch, type Ref } from 'vue'
import {
  getBotSettings, updateBotSettings,
  getLLMListWithSlots, getVLMListWithSlots, assignSlot,
  createLLM, updateLLM, deleteLLM,
  getTemperatures, updateTemperatures,
  getChatParams, updateChatParams,
  getTokenStats, getStatus,
  getToolSettings, updateToolSettings,
} from '@/api/admin'
import type {
  BotSettings, LLMListResponse, LLMConfigItem, LLMConfigCreate, LLMConfigUpdate,
  TemperatureMap, ChatParamItem, TokenStats, ToolSettingItem, ToolConfig,
} from '@/types'
import {
  Cpu, Server, Thermometer, Sliders, BarChart3,
  MessageSquare, User, Brain,
  Plus, Pencil, Trash2, X, Wrench,
} from '@lucide/vue'

// ── Bot 上下文 ──
const currentBot = inject<Ref<string>>('currentBot')!
const bots = inject<Ref<{ id: string; name: string; icon: string; color: string }[]>>('bots')!
const switchBot = inject<(id: string) => void>('switchBot')!

// ── Tab 状态 ──
type TabKey = 'llm' | 'vlm' | 'temp' | 'chat' | 'tools' | 'tokens'
const activeTab = ref<TabKey>('llm')
const tabs: { key: TabKey; label: string; icon: any }[] = [
  { key: 'llm', label: 'LLM 槽位', icon: Cpu },
  { key: 'vlm', label: 'VLM 槽位', icon: Server },
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

async function toggleChat(type: 'group_chat_enabled' | 'private_chat_enabled' | 'reasoning_enabled', val: boolean) {
  if (!settings.value) return
  try {
    await updateBotSettings(currentBot.value, { [type]: val })
    settings.value[type] = val
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

const LLM_PROVIDERS = ['deepseek', 'openai', 'anthropic', 'ollama', 'vectorengine', 'openrouter', 'together', 'custom']
const VLM_PROVIDERS = ['gpt4v', 'claude', 'gemini', 'nano_banana', 'llama']
const ALL_PROVIDERS = [...new Set([...LLM_PROVIDERS, ...VLM_PROVIDERS])]

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
}

function cancelCreate() {
  showCreate.value = false
  errorMsg.value = ''
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
  }
  errorMsg.value = ''
}

function cancelEdit() {
  editingId.value = null
  editForm.value = {}
  errorMsg.value = ''
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
    llm: loadLLM, vlm: loadVLM, temp: loadTemps, chat: loadChatParams,
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
  // 过滤已移除的槽位 (仲裁功能 2026-06-30 删除)
  const filtered: Record<string, number | null> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (k !== 'llm_judge' && k !== 'llm_opus') filtered[k] = v
  }
  return filtered
}

function slotLabel(slotKey: string, tab: string): string {
  if (tab === 'llm') {
    const map: Record<string, string> = {
      llm_primary: '普通聊天',
      llm_secondary: '进阶',
      llm_gate: '意图闸',
    }
    return map[slotKey] || slotKey
  }
  return slotKey === 'vlm_primary' ? '识图' : '绘图'
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
      <!-- 槽位下拉 -->
      <div class="slot-selects">
        <div v-for="(_, slotKey) in currentSlots()" :key="slotKey" class="slot-field">
          <label>{{ slotLabel(slotKey, activeTab) }}</label>
          <select :value="currentSlots()[slotKey] ?? ''"
            @change="doAssignSlot(activeTab, slotKey, parseInt(($event.target as HTMLSelectElement).value) || null)">
            <option value="">— 未分配 —</option>
            <option v-for="c in currentConfigs()" :key="c.id" :value="c.id">{{ c.name }} ({{ getProviderLabel(c.provider) }})</option>
          </select>
        </div>
      </div>

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
            <input v-model="createForm.provider" list="provider-list" class="input" placeholder="输入或选择" autocomplete="off" />
            <datalist id="provider-list">
              <option v-for="p in ALL_PROVIDERS" :key="p" :value="p" />
            </datalist>
          </div>
          <div class="form-field">
            <label>模型名 <span class="required">*</span></label>
            <input v-model="createForm.model_name" placeholder="deepseek-v4-flash" class="input" />
          </div>
          <div class="form-field" style="grid-column: 1 / -1">
            <label>Base URL</label>
            <input v-model="createForm.base_url" placeholder="https://api.deepseek.com/v1" class="input" />
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
              <span v-else class="badge badge-loput">暮恩</span>
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
          <div class="form-field"><label>Provider</label><input v-model="editForm.provider" list="edit-provider-list" class="input" placeholder="输入或选择" autocomplete="off" />
            <datalist id="edit-provider-list"><option v-for="p in ALL_PROVIDERS" :key="p" :value="p" /></datalist>
          </div>
          <div class="form-field"><label>模型名</label><input v-model="editForm.model_name" class="input" /></div>
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

/* ── Panel ── */
.panel { padding: 4px 0; }

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

/* ── Row actions ── */
.row-actions { display: flex; gap: 4px; }
.btn-danger-outline { color: var(--danger); border-color: #fecaca; }
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
