<script setup lang="ts">
/** 绘图 · NovelAI — 独立配置页（Token / 双档模型 / 分辨率 / 步数 / 探测）
 *
 * 模型两档 (2026-10-12 用户裁定): 主人默认 V5 full，普通群聊用户走 V4.5 full。
 * 档位只在后端 service/draw_access.py 判定，页面只负责写值。
 */
import { ref, onMounted } from 'vue'
import { getDrawConfig, saveDrawConfig, probeDraw } from '@/api/admin'
import type { DrawApiConfig } from '@/types'
import { Palette, KeyRound, CheckCircle, XCircle, Loader, Trash2, Save } from '@lucide/vue'

const cfg = ref<DrawApiConfig | null>(null)
const loading = ref(true)
const saving = ref(false)
const tokenInput = ref('')
const model = ref('nai-diffusion-5-full')
const modelPublic = ref('nai-diffusion-4-5-full')
const size = ref('832x1216')
const steps = ref(23)
const probe = ref<{ ok: boolean; reason: string } | null>(null)
const probing = ref(false)
const toast = ref<{ message: string; type: 'success' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

const MODELS = [
  { value: 'nai-diffusion-5-full', label: 'NovelAI Diffusion V5 Full' },
  { value: 'nai-diffusion-5-curated', label: 'NovelAI Diffusion V5 Curated' },
  { value: 'nai-diffusion-4-5-full', label: 'NovelAI Diffusion V4.5 Full' },
  { value: 'nai-diffusion-4-5-curated', label: 'NovelAI Diffusion V4.5 Curated' },
  { value: 'nai-diffusion-4-full', label: 'NovelAI Diffusion V4 Full' },
  { value: 'nai-diffusion-4-curated-preview', label: 'NovelAI Diffusion V4 Curated' },
  { value: 'nai-diffusion-3', label: 'NovelAI Diffusion Anime V3' },
  { value: 'nai-diffusion-furry-3', label: 'NovelAI Diffusion Furry V3' },
]
const SIZES = ['832x1216', '1216x832', '1024x1024', '512x768', '768x512']

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
    model.value = c.novelai.model
    modelPublic.value = c.novelai.model_public || 'nai-diffusion-4-5-full'
    size.value = c.novelai.size
    steps.value = Number(c.novelai.steps) || 23
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
      novelai_model: model.value,
      novelai_model_public: modelPublic.value,
      novelai_size: size.value,
      novelai_steps: steps.value,
    }
    if (tokenInput.value.trim()) payload.novelai_token = tokenInput.value.trim()
    cfg.value = await saveDrawConfig(payload)
    tokenInput.value = ''
    showToast('NovelAI 配置已保存', 'success')
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

async function clear() {
  if (!confirm('确定清空 NovelAI Token 吗？')) return
  try {
    cfg.value = await saveDrawConfig({ clear_novelai: true })
    showToast('NovelAI Token 已清空', 'success')
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '清空失败', 'error')
  }
}

async function doProbe() {
  probing.value = true
  try {
    probe.value = await probeDraw('novelai')
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
        <h3><Palette :size="16" /> NovelAI
          <span v-if="cfg.novelai.token_set" class="tag tag-green">已配置 {{ cfg.novelai.token_masked }}</span>
          <span v-else class="tag tag-gray">未配置</span>
        </h3>
        <div class="form-grid">
          <label class="span2">Token（Persistent API Token）
            <input v-model="tokenInput" type="password" autocomplete="off"
              :placeholder="cfg.novelai.token_set ? '已保存，留空=不改，输入=覆盖' : '粘贴你的 Token'" />
          </label>
          <label>模型 · 主人
            <select v-model="model">
              <option v-for="m in MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
            </select>
          </label>
          <label>模型 · 普通群聊用户
            <select v-model="modelPublic">
              <option v-for="m in MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
            </select>
          </label>
          <label>分辨率
            <select v-model="size">
              <option v-for="s in SIZES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label>步数（Opus 免费要求 ≤28）
            <input v-model.number="steps" type="number" min="1" max="28" />
          </label>
        </div>
        <p class="hint">
          主人档默认 V5 full（不支持参考图）；普通档默认 V4.5 full（支持参考图）。
          带参考图的请求若命中不支持 Vibe 的档位，会临时借用另一档模型，不丢图。
          <span v-if="cfg.novelai.model_vibe_ok === false && cfg.novelai.model_public_vibe_ok === true">
            ⚠️ 当前主人档 {{ cfg.novelai.model }} 不支持参考图 Vibe。
          </span>
        </p>
        <div class="btn-row">
          <button class="btn primary" :disabled="saving" @click="save"><Save :size="14" /> 保存</button>
          <button class="btn" :disabled="probing" @click="doProbe">
            <Loader v-if="probing" :size="14" class="spin" />
            <KeyRound v-else :size="14" /> 探测连通
          </button>
          <button v-if="cfg.novelai.token_set" class="btn danger" @click="clear"><Trash2 :size="14" /> 清空 Token</button>
          <span v-if="probe" class="probe" :class="probe.ok ? 'ok' : 'fail'">
            <CheckCircle v-if="probe.ok" :size="14" /><XCircle v-else :size="14" />
            {{ probe.ok ? '连通正常（零扣费）' : ('失败：' + probe.reason) }}
          </span>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
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
