<script setup lang="ts">
/** NSFW 配置 — 成人向隔离区专属 LLM / VLM 槽位 / 档位 / 温度 (2026-10-18)

 * 背景: longcat 等上游对成人向输入整轮打回 (security_audit_fail), 需要一套
 * 独立的模型/档位/温度供 NSFW 场景使用, 不再和 SFW 抢同一槽位。
 * 规则: 本页未配置的项 = 跟随 SFW (「机器人配置」页), 老行为零变化。
 * 开关与名单 (nsfw_enabled / nsfw_groups / nsfw_users / nsfw_private) 仍在
 * 「机器人配置」页管理——本页只管「进区之后用什么模型/档位/温度」。
 */
import { ref, inject, onMounted, watch, type Ref } from 'vue'
import {
  getBotSettings, getLLMListWithSlots, getVLMListWithSlots, assignSlot,
  testLlmConnection, getNsfwTemperatures, updateNsfwTemperatures,
  updateBotSettings,
} from '@/api/admin'
import type { BotSettings, LLMListResponse, NsfwTemperatureView } from '@/types'
import type { LlmTestResult } from '@/api/admin'
import { Cpu, Thermometer, Flame, Info, Eye } from '@lucide/vue'

// ── Bot 上下文 ──
const currentBot = inject<Ref<string>>('currentBot')!
const bots = inject<Ref<{ id: string; name: string; icon: string; color: string }[]>>('bots')!
const switchBot = inject<(id: string) => void>('switchBot')!

// ── Bot 设置 (NSFW 开关状态 / SFW 档位回显) ──
const settings = ref<BotSettings | null>(null)

async function loadSettings() {
  try { settings.value = await getBotSettings(currentBot.value) } catch { /* ignore */ }
}

// ── NSFW 专属 LLM 槽位 ──
const llmList = ref<LLMListResponse | null>(null)
const slotAssigning = ref<string | null>(null)

async function loadLLM() {
  try { llmList.value = await getLLMListWithSlots(currentBot.value, 'nsfw') } catch { /* ignore */ }
}

// 槽位过滤 (与 SFW 页同口径: 退役槽位永不展示)
function displaySlots(): Record<string, number | null> {
  const raw = llmList.value?.slots ?? {}
  const filtered: Record<string, number | null> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (k !== 'llm_judge' && k !== 'llm_opus' && k !== 'llm_secondary') filtered[k] = v
  }
  return filtered
}

function sfwSlots(): Record<string, number | null> {
  return llmList.value?.fallback_slots ?? {}
}

function configs() {
  return llmList.value?.configs ?? []
}

function configById(id: number | null | undefined) {
  if (id == null) return null
  return configs().find(c => c.id === id) ?? null
}

/** 下拉默认项文字: 跟随 SFW（回落目标模型名） */
function followLabel(slotKey: string): string {
  const c = configById(sfwSlots()[slotKey])
  return c ? `跟随 SFW（${c.model_name}）` : '跟随 SFW（未分配）'
}

/** 本槽位 NSFW 实际生效模型 (独立覆盖 → 覆盖模型; 否则回落 SFW 模型) */
function effectiveModel(slotKey: string): string {
  const n = configById(displaySlots()[slotKey])
  if (n) return `${n.model_name}（NSFW 独立）`
  const f = configById(sfwSlots()[slotKey])
  return f ? `${f.model_name}（跟随 SFW）` : '未分配'
}

function slotLabel(slotKey: string): string {
  const map: Record<string, string> = {
    llm_primary: '主链·脑/嘴/私聊',
    llm_lite: '闲聊·后台/压缩',
    llm_pro: '推理·绘图spec',
    llm_gate: '意图闸·Slim',
    shadow_agent: '影子Agent',
  }
  return map[slotKey] || slotKey
}

async function doAssignSlot(slot: string, configId: number | null) {
  slotAssigning.value = slot
  try {
    await assignSlot('llm', slot, configId ?? 0, currentBot.value, 'nsfw')
    await loadLLM()
  } catch { /* ignore */ }
  finally { slotAssigning.value = null }
}

// ── NSFW 专属 VLM 槽位 (识图: 图 / 视频 / 语音) ──
const vlmList = ref<LLMListResponse | null>(null)

async function loadVLM() {
  try { vlmList.value = await getVLMListWithSlots(currentBot.value, 'nsfw') } catch { /* ignore */ }
}

// 与 SFW 页同口径: VLM 只保留识图 (vlm_secondary 已退役无消费方)
function vlmDisplaySlots(): Record<string, number | null> {
  const raw = vlmList.value?.slots ?? {}
  const filtered: Record<string, number | null> = {}
  if ('vlm_primary' in raw) filtered['vlm_primary'] = raw['vlm_primary']
  return filtered
}

function vlmSfwSlots(): Record<string, number | null> {
  return vlmList.value?.fallback_slots ?? {}
}

function vlmConfigs() {
  return vlmList.value?.configs ?? []
}

function vlmConfigById(id: number | null | undefined) {
  if (id == null) return null
  return vlmConfigs().find(c => c.id === id) ?? null
}

/** VLM 下拉默认项文字: 跟随 SFW（回落目标模型名） */
function vlmFollowLabel(slotKey: string): string {
  const c = vlmConfigById(vlmSfwSlots()[slotKey])
  return c ? `跟随 SFW（${c.model_name}）` : '跟随 SFW（未分配）'
}

/** 本 VLM 槽位实际生效模型 (独立覆盖 → 覆盖模型; 否则回落 SFW 模型) */
function vlmEffectiveModel(slotKey: string): string {
  const n = vlmConfigById(vlmDisplaySlots()[slotKey])
  if (n) return `${n.model_name}（NSFW 独立）`
  const f = vlmConfigById(vlmSfwSlots()[slotKey])
  return f ? `${f.model_name}（跟随 SFW）` : '未分配'
}

function vlmSlotLabel(slotKey: string): string {
  const map: Record<string, string> = { vlm_primary: '识图（图/视频/语音）' }
  return map[slotKey] || slotKey
}

async function doAssignVlmSlot(slot: string, configId: number | null) {
  const key = `vlm:${slot}`
  slotAssigning.value = key
  try {
    await assignSlot('vlm', slot, configId ?? 0, currentBot.value, 'nsfw')
    await loadVLM()
  } catch { /* ignore */ }
  finally { slotAssigning.value = null }
}

// ── NSFW 槽位档位 (follow = 跟随 SFW 链) ──
const SLOT_EFFORT_SLOTS = new Set(['llm_primary', 'llm_lite', 'llm_pro', 'llm_gate', 'shadow_agent'])
const SLOT_EFFORT_OPTIONS = [
  { value: 'follow', label: '跟随 SFW' },
  { value: 'off', label: '关闭' },
  { value: 'minimal', label: 'minimal' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max' },
]

function showSlotEffort(slotKey: string): boolean {
  return SLOT_EFFORT_SLOTS.has(slotKey)
}

function nsfwEffort(slotKey: string): string {
  return settings.value?.nsfw_slot_efforts?.[slotKey] || 'follow'
}

async function saveNsfwEffort(slotKey: string, val: string) {
  if (!settings.value) return
  try {
    const next = { ...(settings.value.nsfw_slot_efforts || {}), [slotKey]: val }
    await updateBotSettings(currentBot.value, { nsfw_slot_efforts: next })
    settings.value.nsfw_slot_efforts = next
  } catch { /* ignore */ }
}

/** 行内测试用生效档位: NSFW 槽档 → SFW 槽档 → 全局默认档 */
function effectiveEffortFor(slotKey: string): string {
  const n = nsfwEffort(slotKey)
  if (n && n !== 'follow') return n
  const s = settings.value?.slot_efforts?.[slotKey]
  if (s && s !== 'follow') return s
  return settings.value?.thinking_effort || 'minimal'
}

// ── 行内测试探针 ──
const testPrompt = ref('请只回复：连通正常')
const rowTestRunning = ref<Record<string, boolean>>({})
const rowTestResult = ref<Record<string, LlmTestResult | null>>({})

async function runRowTest(slotKey: string) {
  rowTestResult.value[slotKey] = null
  rowTestRunning.value[slotKey] = true
  try {
    rowTestResult.value[slotKey] = await testLlmConnection(
      currentBot.value, slotKey,
      effectiveEffortFor(slotKey),
      testPrompt.value || '请只回复：连通正常',
      'nsfw',
    )
  } catch (e) {
    rowTestResult.value[slotKey] = { ok: false, error: `请求失败：${e}` }
  } finally {
    rowTestRunning.value[slotKey] = false
  }
}

// ── NSFW 温度 (独立覆盖; 关闭开关 = 跟随 SFW) ──
const TEMP_LABELS: Record<string, [string, string]> = {
  tavern_group: ['群聊', '群聊自然对话（成人向主场景）'],
  memory_extract: ['记忆提取', '低温度保证准确性'],
  context_compress: ['上下文压缩', '低温度保证信息不丢失'],
  cross_validation: ['交叉验证', '极低温度保证公正'],
}

const tempView = ref<NsfwTemperatureView | null>(null)
const tempDraft = ref<Record<string, { on: boolean; value: number }>>({})
const tempSaving = ref(false)
const tempSaved = ref(false)

function applyTempView(v: NsfwTemperatureView) {
  tempView.value = v
  const draft: Record<string, { on: boolean; value: number }> = {}
  for (const key of Object.keys(TEMP_LABELS)) {
    draft[key] = { on: !!v.overrides[key], value: v.temperatures[key] ?? 0.7 }
  }
  tempDraft.value = draft
}

async function loadTemps() {
  try {
    applyTempView(await getNsfwTemperatures(currentBot.value))
  } catch { /* ignore */ }
}

async function saveTemps() {
  tempSaving.value = true
  tempSaved.value = false
  try {
    const payload: Record<string, number | null> = {}
    for (const key of Object.keys(TEMP_LABELS)) {
      const d = tempDraft.value[key]
      payload[key] = d?.on ? d.value : null
    }
    applyTempView(await updateNsfwTemperatures(payload, currentBot.value))
    tempSaved.value = true
    setTimeout(() => (tempSaved.value = false), 2000)
  } catch { /* ignore */ }
  finally { tempSaving.value = false }
}

// ── 加载 & Bot 切换 ──
function loadAll() {
  loadSettings()
  loadLLM()
  loadVLM()
  loadTemps()
}

watch(currentBot, loadAll)
onMounted(loadAll)

function getProviderLabel(p: string) {
  const map: Record<string, string> = {
    deepseek: 'DeepSeek', openai: 'OpenAI', anthropic: 'Anthropic',
    gemini: 'Gemini', ollama: 'Ollama', llama: 'Llama',
    gpt4v: 'GPT-4V', claude: 'Claude', nano_banana: 'Nano Banana',
    vectorengine: 'VectorEngine', openrouter: 'OpenRouter', together: 'Together',
    custom: '自定义', opencode: 'OpenCode',
  }
  return map[p] || p
}
</script>

<template>
  <div class="nsfw-config-page">
    <!-- Bot 状态条 -->
    <div class="bot-bar" v-if="settings">
      <div class="bot-info">
        <select class="bot-selector" :value="currentBot" @change="switchBot(($event.target as HTMLSelectElement).value)">
          <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
        </select>
        <span class="bot-qq">{{ settings.bot_id }}</span>
      </div>
      <div class="bot-toggles">
        <span class="toggle-label">
          <Flame :size="14" />
          <span>群聊成人区</span>
          <span :class="['badge', settings.nsfw_enabled ? 'badge-nsfw-on' : 'badge-nsfw-off']">
            {{ settings.nsfw_enabled ? '已开启' : '已关闭' }}
          </span>
        </span>
        <span class="toggle-label">
          <span>私聊通道</span>
          <span :class="['badge', settings.nsfw_private ? 'badge-nsfw-on' : 'badge-nsfw-off']">
            {{ settings.nsfw_private ? '已开启' : '已关闭' }}
          </span>
        </span>
      </div>
    </div>

    <!-- 说明卡 -->
    <div class="info-card">
      <div class="info-title"><Info :size="15" /><span>NSFW 场景专属 LLM 配置</span></div>
      <p class="info-desc">
        当对话命中成人向隔离区（群白名单 + 用户白名单 / 私聊通道）时，脑、嘴、后台、绘图与识图的模型解析会优先使用本页配置；
        <strong>未配置的项自动跟随「机器人配置」页（SFW）</strong>，所以只改需要区分的槽位即可。
        面板配置优先于环境变量兜底（SULI_NSFW_MODEL 仅在未配槽位时生效）。
      </p>
      <p class="info-desc">
        隔离区开关与生效名单（群 / 用户）在「机器人配置」页管理；本页只负责「进区之后用什么模型 / 档位 / 温度」。
        模型配置库与 SFW 页共用——需要新增模型先去「机器人配置」页添加。
      </p>
    </div>

    <!-- NSFW 专属 LLM 槽位 -->
    <div class="panel">
      <div class="section-label"><Cpu :size="13" style="vertical-align:-2px" /> NSFW 专属槽位</div>
      <div class="slot-selects">
        <div v-for="(_, slotKey) in displaySlots()" :key="slotKey" class="slot-field">
          <label>{{ slotLabel(String(slotKey)) }}</label>
          <div class="slot-row">
            <select :value="displaySlots()[slotKey] ?? ''" :disabled="slotAssigning === String(slotKey)"
              @change="doAssignSlot(String(slotKey), parseInt(($event.target as HTMLSelectElement).value) || null)">
              <option value="">{{ followLabel(String(slotKey)) }}</option>
              <option v-for="c in configs()" :key="c.id" :value="c.id">
                {{ c.name }} · {{ c.model_name }} ({{ getProviderLabel(c.provider) }})
              </option>
            </select>
            <select v-if="showSlotEffort(String(slotKey))" class="effort-select" :value="nsfwEffort(String(slotKey))"
              @change="saveNsfwEffort(String(slotKey), ($event.target as HTMLSelectElement).value)"
              title="NSFW 场景下该槽位的思考档位: 跟随 SFW = 用 SFW 槽档/全局档">
              <option v-for="o in SLOT_EFFORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <button class="btn btn-xs" :disabled="rowTestRunning[String(slotKey)]"
              @click="runRowTest(String(slotKey))" title="按 NSFW 链解析（含回落）测连通">
              {{ rowTestRunning[String(slotKey)] ? '测试中…' : '测试' }}
            </button>
          </div>
          <p class="field-hint">实际生效: {{ effectiveModel(String(slotKey)) }}</p>
          <div v-if="rowTestResult[String(slotKey)]" class="fetch-hint"
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
      <p class="field-hint">
        「测试」按 NSFW 链解析实际模型（配了独立槽位用独立模型，没配则回落 SFW）——即 NSFW 场景真实会用的那一个。
      </p>
    </div>

    <!-- NSFW 专属 VLM 识图槽位 -->
    <div class="panel">
      <div class="section-label"><Eye :size="13" style="vertical-align:-2px" /> NSFW 专属 VLM 槽位（识图）</div>
      <div class="slot-selects">
        <div v-for="(_, slotKey) in vlmDisplaySlots()" :key="slotKey" class="slot-field">
          <label>{{ vlmSlotLabel(String(slotKey)) }}</label>
          <div class="slot-row">
            <select :value="vlmDisplaySlots()[slotKey] ?? ''" :disabled="slotAssigning === `vlm:${String(slotKey)}`"
              @change="doAssignVlmSlot(String(slotKey), parseInt(($event.target as HTMLSelectElement).value) || null)">
              <option value="">{{ vlmFollowLabel(String(slotKey)) }}</option>
              <option v-for="c in vlmConfigs()" :key="c.id" :value="c.id">
                {{ c.name }} · {{ c.model_name }} ({{ getProviderLabel(c.provider) }})
              </option>
            </select>
          </div>
          <p class="field-hint">实际生效: {{ vlmEffectiveModel(String(slotKey)) }}</p>
        </div>
      </div>
      <p class="field-hint">
        进 NSFW 区后的看图 / 视频 / 语音解析走这里配置的 VLM；未配置则跟随「机器人配置」页的 VLM 槽位。
        上游对成人向图片整轮打回时，在此换一个不过审的 VLM 即可（VLM 配置先在上方模型库或「机器人配置」页添加）。
      </p>
    </div>

    <!-- NSFW 温度 -->
    <div class="panel">
      <div class="section-label"><Thermometer :size="13" style="vertical-align:-2px" /> NSFW 温度覆盖</div>
      <div class="temp-grid">
        <div v-for="([scenario, [label, desc]]) in Object.entries(TEMP_LABELS)" :key="scenario" class="temp-field">
          <label class="temp-head">
            <input type="checkbox" v-model="tempDraft[scenario]!.on" />
            <strong>{{ label }}</strong>
            <small>{{ desc }}</small>
          </label>
          <div class="temp-input-group">
            <input type="number" min="0" max="2" step="0.05" class="input temp-num"
              :disabled="!tempDraft[scenario]?.on" v-model.number="tempDraft[scenario]!.value" />
            <span class="temp-follow" v-if="!tempDraft[scenario]?.on">
              跟随 SFW：{{ tempView?.sfw?.[scenario]?.toFixed(2) ?? '—' }}
            </span>
            <span class="temp-follow overridden" v-else>独立覆盖</span>
          </div>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="tempSaving" @click="saveTemps" style="margin-top:12px">
        {{ tempSaving ? '保存中...' : tempSaved ? '已保存!' : '保存 NSFW 温度' }}
      </button>
      <p class="field-hint" style="margin-top:8px">勾选 = 写独立覆盖；取消勾选并保存 = 清除覆盖，恢复跟随 SFW。</p>
    </div>
  </div>
</template>

<style scoped>
.nsfw-config-page { width: 100%; }

/* ── Bot 状态条 ── */
.bot-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; margin-bottom: 18px;
  background: linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%);
  border: 1px solid #fed7aa; border-radius: var(--radius-lg);
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
.bot-toggles { display: flex; gap: 18px; align-items: center; }
.toggle-label {
  display: flex; align-items: center; gap: 7px;
  font-size: 13px; color: var(--text-secondary); user-select: none;
}

/* ── 说明卡 ── */
.info-card {
  padding: 16px 18px; margin-bottom: 18px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.info-title {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 700; color: #9a3412;
}
.info-title svg { color: #ea580c; }
.info-desc { font-size: 12px; color: var(--text-secondary); margin: 8px 0 0; line-height: 1.7; }

/* ── Panel / 槽位 ── */
.panel { padding: 4px 0; margin-bottom: 10px; }
.section-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--text-muted); margin: 18px 0 10px; padding-bottom: 5px;
  border-bottom: 1px solid var(--border-light);
}
.slot-selects { display: flex; gap: 16px; margin-bottom: 10px; flex-wrap: wrap; }
.slot-field { display: flex; flex-direction: column; gap: 5px; }
.slot-field label {
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.slot-field select {
  padding: 8px 12px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 13px;
  background: var(--surface); min-width: 260px; color: var(--text);
}
.slot-field select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,0.1); }
.slot-field select:disabled { opacity: 0.5; }
.slot-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.slot-row .effort-select {
  padding: 4px 8px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 12px;
  background: var(--bg); color: var(--text); cursor: pointer;
  min-width: 110px; max-width: 150px;
}
.field-hint { font-size: 11px; color: var(--text-muted); margin: 6px 0 0; line-height: 1.5; }
.fetch-hint { font-size: 12px; color: var(--text-muted); display: block; margin-top: 4px; }

/* ── 徽章 ── */
.badge {
  display: inline-flex; align-items: center;
  font-size: 11px; padding: 2px 10px; border-radius: 9999px; font-weight: 500;
}
.badge-nsfw-on { background: #ffedd5; color: #9a3412; }
.badge-nsfw-off { background: var(--primary-light); color: var(--text-muted); }

/* ── 温度 ── */
.temp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 800px) { .temp-grid { grid-template-columns: 1fr; } }
.temp-field { display: flex; flex-direction: column; gap: 6px; }
.temp-head { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); cursor: pointer; }
.temp-head small { font-weight: 400; color: var(--text-muted); font-size: 11px; }
.temp-input-group { display: flex; align-items: center; gap: 10px; }
.temp-num { width: 100px; text-align: center; font-weight: 600; }
.temp-num:disabled { opacity: 0.45; }
.temp-follow { font-size: 12px; color: var(--text-muted); }
.temp-follow.overridden { color: #9a3412; font-weight: 600; }
</style>
