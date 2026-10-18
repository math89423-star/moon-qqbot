<script setup lang="ts">
/** 绘图 · PixAI — 独立配置页（Key / 模型 / 宽高比 / 清晰度 / 探测 / 计费准入）
 *
 * PixAI 是唯一计费后端 (METERED_PROVIDERS)：默认只对主人开放，
 * 普通群聊用户命中它时后端自动降级到免费通道 NovelAI，用户侧无感。
 * 开关真相源在后端 service/draw_access.py，页面只负责写值。
 */
import { ref, onMounted } from 'vue'
import { getDrawConfig, saveDrawConfig, probeDraw } from '@/api/admin'
import type { DrawApiConfig } from '@/types'
import { Image as ImageIcon, KeyRound, CheckCircle, XCircle, Loader, Trash2, Save } from '@lucide/vue'

const cfg = ref<DrawApiConfig | null>(null)
const loading = ref(true)
const saving = ref(false)
const keyInput = ref('')
const model = ref('1983308862240288769')
const aspect = ref('9:16')
const size = ref('1k')
const masterOnly = ref(true)
const nsfwAllow = ref(true)
const probe = ref<{ ok: boolean; reason: string } | null>(null)
const probing = ref(false)
const toast = ref<{ message: string; type: 'success' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

const MODELS = [
  { value: '1983308862240288769', label: 'Tsubaki.2' },
  { value: '1861558740588989558', label: 'Haruka v2' },
  { value: '1954632828118619567', label: 'Hoshino v2' },
]
const ASPECTS = ['1:1', '2:3', '3:2', '3:4', '4:3', '3:5', '5:3', '9:16', '16:9', '1:3', '3:1']
const SIZES = ['1k', '1.5k']

function showToast(message: string, type: 'success' | 'error') {
  if (toastTimer) clearTimeout(toastTimer)
  toast.value = { message, type }
  toastTimer = setTimeout(() => { toast.value = null }, 3500)
}

async function load() {
  loading.value = true
  try {
    const c = await getDrawConfig()
    cfg.value = c
    model.value = c.pixai.model
    aspect.value = c.pixai.aspect
    size.value = c.pixai.size
    if (c.policy) {
      masterOnly.value = c.policy.metered_master_only
      nsfwAllow.value = c.policy.metered_nsfw_allow
    }
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '加载失败', 'error')
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      pixai_model: model.value, pixai_aspect: aspect.value, pixai_size: size.value,
      metered_master_only: masterOnly.value, metered_nsfw_allow: nsfwAllow.value,
    }
    if (keyInput.value.trim()) payload.pixai_key = keyInput.value.trim()
    cfg.value = await saveDrawConfig(payload)
    keyInput.value = ''
    showToast('PixAI 配置已保存', 'success')
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

async function clear() {
  if (!confirm('确定清空 PixAI Key 吗？')) return
  try {
    cfg.value = await saveDrawConfig({ clear_pixai: true })
    showToast('PixAI Key 已清空', 'success')
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '清空失败', 'error')
  }
}

async function doProbe() {
  probing.value = true
  try {
    probe.value = await probeDraw('pixai')
  } catch {
    probe.value = { ok: false, reason: 'network' }
  } finally {
    probing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="draw-page">
    <div v-if="toast" class="toast" :class="toast.type">{{ toast.message }}</div>
    <div v-if="loading" class="loading">加载中…</div>
    <template v-else-if="cfg">
      <section class="card">
        <h3><ImageIcon :size="16" /> PixAI
          <span v-if="cfg.pixai.key_set" class="tag tag-green">已配置 {{ cfg.pixai.key_masked }}</span>
          <span v-else class="tag tag-gray">未配置</span>
        </h3>
        <div class="form-grid">
          <label class="span2">API Key（sk-…）
            <input v-model="keyInput" type="password" autocomplete="off"
              :placeholder="cfg.pixai.key_set ? '已保存，留空=不改，输入=覆盖' : '粘贴 sk- 开头的 Key'" />
          </label>
          <label>模型
            <select v-model="model">
              <option v-for="m in MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
            </select>
          </label>
          <label>宽高比
            <select v-model="aspect">
              <option v-for="a in ASPECTS" :key="a" :value="a">{{ a }}</option>
            </select>
          </label>
          <label>清晰度
            <select v-model="size">
              <option v-for="s in SIZES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
        </div>
        <div class="btn-row">
          <button class="btn primary" :disabled="saving" @click="save"><Save :size="14" /> 保存</button>
          <button class="btn" :disabled="probing" @click="doProbe">
            <Loader v-if="probing" :size="14" class="spin" />
            <KeyRound v-else :size="14" /> 探测连通
          </button>
          <button v-if="cfg.pixai.key_set" class="btn danger" @click="clear"><Trash2 :size="14" /> 清空 Key</button>
          <span v-if="probe" class="probe" :class="probe.ok ? 'ok' : 'fail'">
            <CheckCircle v-if="probe.ok" :size="14" /><XCircle v-else :size="14" />
            {{ probe.ok ? '连通正常（零扣费）' : ('失败：' + probe.reason) }}
          </span>
        </div>
      </section>
      <section v-if="cfg.policy" class="card">
        <h3>计费准入（消耗额度）</h3>
        <p class="hint">
          计费后端：{{ cfg.policy.metered_providers.join('、') || '无' }}。
          非主人命中计费后端时，后端自动改用 NovelAI 免费通道（只进日志，用户侧无感）。
        </p>
        <div class="form-grid">
          <label class="span2">
            <span class="row">
              <input v-model="masterOnly" type="checkbox" />
              计费后端仅主人可用（推荐）
            </span>
          </label>
          <label class="span2">
            <span class="row">
              <input v-model="nsfwAllow" type="checkbox" />
              主人 NSFW 通路额外放行（与主人互动的成人区会话）
            </span>
          </label>
        </div>
        <p class="hint">
          两开关都关掉 = 任何人都能用计费后端（不推荐）。NSFW 通路的范围由
          「机器人配置 → NSFW 隔离区」的群/人白名单决定，名单里只有主人时与「仅主人」等价。
        </p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); }
.row input[type='checkbox'] { width: 15px; height: 15px; }
.draw-page { display: flex; flex-direction: column; gap: 16px; max-width: 860px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; box-shadow: var(--shadow-sm); }
.card h3 { margin: 0 0 12px; font-size: 15px; display: flex; align-items: center; gap: 8px; color: var(--text); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; }
.form-grid label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--muted); }
.form-grid label.span2 { grid-column: 1 / -1; }
.form-grid input, .form-grid select { padding: 8px 10px; font-size: 13px; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
.btn-row { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; font-size: 13px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--text); cursor: pointer; }
.btn.primary { background: #4f46e5; border-color: #4f46e5; color: #fff; }
.btn.danger { color: #dc2626; border-color: #fecaca; }
.btn:disabled { opacity: 0.6; cursor: not-allowed; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 20px; }
.tag-green { background: #dcfce7; color: #166534; }
.tag-gray { background: #f1f5f9; color: #64748b; }
.hint { font-size: 12px; color: var(--muted); margin: 10px 0 0; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { font-size: 12px; padding: 4px 12px; border-radius: 20px; background: #f1f5f9; color: #64748b; }
.chip.on { background: #dcfce7; color: #166534; }
.probe { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.probe.ok { color: #16a34a; }
.probe.fail { color: #dc2626; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading { color: var(--muted); font-size: 13px; }
.toast { padding: 10px 14px; border-radius: 8px; font-size: 13px; background: var(--surface); border: 1px solid var(--border); }
.toast.success { border-color: #86efac; color: #166534; }
.toast.error { border-color: #fca5a5; color: #dc2626; }
</style>
