<script setup lang="ts">
/** 表情包管理 — 类别浏览、图片管理、同步状态、上传/删除 */
import { ref, computed, onMounted } from 'vue'
import {
  listMemeCategories, listMemes,
  clearMemeCategory, deleteMemeCategory,
  updateMemeDesc, getMemeSyncStatus,
  uploadMeme, deleteMeme,
} from '@/api/admin'
import type { MemeCategory, MemeItem, MemeSyncStatus } from '@/types'
import {
  Smile, RefreshCw,
  AlertTriangle, CheckCircle, XCircle, Info,
  Trash2, Eraser, FileText, ImageIcon,
  X, Package, Upload, Plus,
} from '@lucide/vue'

// ── 状态 ──
const categories = ref<MemeCategory[]>([])
const catLoading = ref(true)

const selectedCat = ref<string | null>(null)
const memes = ref<MemeItem[]>([])
const memeTotal = ref(0)
const memeLoading = ref(false)

const editingDesc = ref<string | null>(null)
const editDescValue = ref('')

const syncStatus = ref<MemeSyncStatus | null>(null)
const syncLoading = ref(false)

const confirmAction = ref<{ type: string; category: string; filename?: string } | null>(null)

// ── UI 状态 ──
const lightboxImage = ref<MemeItem | null>(null)
const toast = ref<{ message: string; type: 'success' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

// ── 上传状态 ──
const showUpload = ref(false)
const uploadFile = ref<File | null>(null)
const uploading = ref(false)

// ── 计算属性 ──
const totalImages = computed(() =>
  categories.value.reduce((sum, c) => sum + c.count, 0)
)

const syncOk = computed(() =>
  syncStatus.value &&
  !syncStatus.value.missing_in_config.length &&
  !syncStatus.value.deleted_categories.length
)

// ── Toast ──
function showToast(message: string, type: 'success' | 'error') {
  if (toastTimer) clearTimeout(toastTimer)
  toast.value = { message, type }
  toastTimer = setTimeout(() => { toast.value = null }, 3500)
}

// ── 加载类别 ──
async function loadCategories() {
  catLoading.value = true
  try {
    categories.value = await listMemeCategories()
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '加载失败', 'error')
  } finally {
    catLoading.value = false
  }
}

// ── 查看类别图片 ──
async function viewCategory(cat: string) {
  selectedCat.value = cat
  memeLoading.value = true
  try {
    const res = await listMemes(cat)
    memes.value = res.items
    memeTotal.value = res.total
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '加载失败', 'error')
  } finally {
    memeLoading.value = false
  }
}

function backToCategories() {
  selectedCat.value = null
  memes.value = []
  editingDesc.value = null
  showUpload.value = false
}

// ── 图片 URL ──
function memeUrl(file: string) {
  return `/memes/img/${file}`
}

// ── 编辑描述 ──
function startEditDesc(cat: MemeCategory) {
  editingDesc.value = cat.name
  editDescValue.value = cat.description || ''
}

async function saveDesc() {
  if (!editingDesc.value) return
  try {
    await updateMemeDesc(editingDesc.value, editDescValue.value)
    const cat = categories.value.find(c => c.name === editingDesc.value)
    if (cat) cat.description = editDescValue.value
    showToast(`已更新「${editingDesc.value}」的描述`, 'success')
    editingDesc.value = null
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '保存失败', 'error')
  }
}

function cancelEditDesc() {
  editingDesc.value = null
}

// ── 清空 / 删除类别 ──
function confirmClear(cat: string) {
  confirmAction.value = { type: 'clear', category: cat }
}

function confirmDelete(cat: string) {
  confirmAction.value = { type: 'delete', category: cat }
}

async function executeConfirm() {
  if (!confirmAction.value) return
  const { type, category } = confirmAction.value
  confirmAction.value = null
  try {
    if (type === 'clear') {
      await clearMemeCategory(category)
      showToast(`已清空「${category}」下的所有图片`, 'success')
    } else {
      await deleteMemeCategory(category)
      showToast(`已删除类别「${category}」`, 'success')
    }
    await loadCategories()
    if (selectedCat.value === category) backToCategories()
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '操作失败', 'error')
  }
}

function cancelConfirm() {
  confirmAction.value = null
}

// ── 同步状态 ──
async function loadSyncStatus() {
  syncLoading.value = true
  try {
    syncStatus.value = await getMemeSyncStatus()
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '加载失败', 'error')
  } finally {
    syncLoading.value = false
  }
}

// ── 上传图片 ──
function openUpload() {
  showUpload.value = true
  uploadFile.value = null
}

function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement
  if (target.files && target.files.length > 0) {
    uploadFile.value = target.files[0]
  }
}

async function doUpload() {
  if (!uploadFile.value || !selectedCat.value) return
  uploading.value = true
  try {
    await uploadMeme(selectedCat.value, uploadFile.value)
    showToast(`已上传「${uploadFile.value.name}」`, 'success')
    showUpload.value = false
    uploadFile.value = null
    await viewCategory(selectedCat.value)
    await loadCategories()
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '上传失败', 'error')
  } finally {
    uploading.value = false
  }
}

// ── 删除单张图片 ──
function confirmDeleteMeme(filename: string) {
  if (!selectedCat.value) return
  confirmAction.value = { type: 'deleteMeme', category: selectedCat.value, filename }
}

async function executeConfirmMeme() {
  if (!confirmAction.value || confirmAction.value.type !== 'deleteMeme') return
  const { category, filename } = confirmAction.value
  confirmAction.value = null
  try {
    await deleteMeme(category, filename!)
    showToast(`已删除「${filename}」`, 'success')
    await viewCategory(category)
    await loadCategories()
  } catch (e: any) {
    showToast(e?.response?.data?.message || e.message || '删除失败', 'error')
  }
}

onMounted(async () => {
  await loadCategories()
  if (categories.value.length) {
    viewCategory(categories.value[0].name)
  }
})
</script>

<template>
  <div class="meme-page">
    <!-- ═══ Toast 通知 ═══ -->
    <Transition name="toast">
      <div v-if="toast" :class="['toast', `toast-${toast.type}`]">
        <CheckCircle v-if="toast.type === 'success'" :size="16" />
        <AlertTriangle v-else :size="16" />
        <span>{{ toast.message }}</span>
        <button class="toast-close" @click="toast = null"><X :size="14" /></button>
      </div>
    </Transition>

    <!-- ═══ 确认对话框 ═══ -->
    <Transition name="modal">
      <div v-if="confirmAction" class="modal-overlay" @mousedown.self="cancelConfirm">
        <div class="modal-box">
          <div :class="['modal-icon', confirmAction.type === 'delete' || confirmAction.type === 'deleteMeme' ? 'icon-danger' : 'icon-warn']">
            <AlertTriangle :size="28" />
          </div>
          <h3>
            {{ confirmAction.type === 'clear' ? '清空类别'
              : confirmAction.type === 'deleteMeme' ? '删除图片'
              : '删除类别' }}
          </h3>
          <p class="modal-desc">
            <template v-if="confirmAction.type === 'clear'">
              确定要清空 <strong>「{{ confirmAction.category }}」</strong> 下的所有图片吗？
              <br /><span class="modal-hint">类别目录会被保留，图片文件将被删除。</span>
            </template>
            <template v-else-if="confirmAction.type === 'deleteMeme'">
              确定要删除图片 <strong>「{{ confirmAction.filename }}」</strong> 吗？
              <br /><span class="modal-danger-hint">此操作不可撤销。</span>
            </template>
            <template v-else>
              确定要<strong>永久删除</strong>类别 <strong>「{{ confirmAction.category }}」</strong> 吗？
              <br /><span class="modal-danger-hint">此操作会删除类别目录及所有图片，不可撤销。</span>
            </template>
          </p>
          <div class="modal-actions">
            <button class="btn" @click="cancelConfirm">取消</button>
            <button
              :class="['btn', confirmAction.type === 'delete' || confirmAction.type === 'deleteMeme' ? 'btn-danger' : 'btn-warn']"
              @click="confirmAction.type === 'deleteMeme' ? executeConfirmMeme() : executeConfirm()"
            >
              确认{{ confirmAction.type === 'clear' ? '清空' : '删除' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>

    <!-- ═══ 图片灯箱 ═══ -->
    <Transition name="modal">
      <div v-if="lightboxImage" class="lightbox-overlay" @mousedown.self="lightboxImage = null">
        <div class="lightbox-box">
          <button class="lightbox-close" @click="lightboxImage = null"><X :size="22" /></button>
          <img
            :src="memeUrl(lightboxImage.file)"
            :alt="lightboxImage.desc"
            class="lightbox-img"
          />
          <div class="lightbox-info">
            <span class="lightbox-filename">{{ lightboxImage.file }}</span>
            <span v-if="lightboxImage.intensity" class="tag tag-orange">{{ lightboxImage.intensity }}</span>
            <span v-if="lightboxImage.desc" class="lightbox-desc">{{ lightboxImage.desc }}</span>
            <span v-if="lightboxImage.tags?.length" class="lightbox-tags">
              <span v-for="t in lightboxImage.tags" :key="t" class="tag tag-purple" style="margin-right:4px">{{ t }}</span>
            </span>
          </div>
        </div>
      </div>
    </Transition>

    <!-- ═══ 页面头部 ═══ -->
    <div class="page-header">
      <div class="page-title-row">
        <div class="page-icon-box"><Smile :size="22" /></div>
        <div>
          <h1>表情包管理</h1>
          <p class="page-subtitle">管理表情包类别、图片与同步状态</p>
        </div>
      </div>
    </div>

    <!-- ═══ 统计卡片 ═══ -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background:var(--primary-light);color:var(--primary)">
          <Package :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ categories.length }}</div>
          <div class="stat-label">类别</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:#eff6ff;color:#3b82f6">
          <ImageIcon :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ totalImages }}</div>
          <div class="stat-label">图片总数</div>
        </div>
      </div>
      <div class="stat-card">
        <div
          class="stat-icon"
          :style="syncOk
            ? 'background:#ecfdf5;color:#10b981'
            : syncStatus
              ? 'background:#fef2f2;color:#ef4444'
              : 'background:#f8fafc;color:#94a3b8'"
        >
          <CheckCircle v-if="syncOk" :size="20" />
          <XCircle v-else-if="syncStatus" :size="20" />
          <RefreshCw v-else :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">
            {{ syncStatus ? (syncOk ? '已同步' : '异常') : '—' }}
          </div>
          <div class="stat-label">同步状态</div>
        </div>
      </div>
    </div>

    <!-- ═══ 类别标签栏 + 工具栏 ═══ -->
    <div class="toolbar" style="flex-wrap:nowrap">
      <div class="tab-bar" style="margin-bottom:0; flex:1; overflow-x:auto; flex-wrap:nowrap">
        <button
          v-for="cat in categories"
          :key="cat.name"
          :class="['tab-btn', { active: selectedCat === cat.name }]"
          @click="viewCategory(cat.name)"
          style="white-space:nowrap; flex-shrink:0"
        >
          {{ cat.name }}
          <span style="font-size:10px;opacity:0.6;margin-left:3px">{{ cat.count }}</span>
        </button>
      </div>
      <button class="btn btn-sm" @click="loadSyncStatus" :disabled="syncLoading" style="flex-shrink:0">
        <RefreshCw :size="13" /> 同步
      </button>
      <button class="btn btn-sm btn-primary" @click="loadCategories" :disabled="catLoading" style="flex-shrink:0">
        <RefreshCw :size="13" /> 刷新
      </button>
    </div>

    <!-- 同步状态面板 -->
    <Transition name="slide">
      <div v-if="syncStatus" class="card" style="border-left:3px solid var(--warning);margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-weight:600;font-size:13px">
          <Info :size="15" style="color:var(--warning)" /> 同步检测结果
          <button class="btn btn-xs btn-ghost" @click="syncStatus = null" style="margin-left:auto"><X :size="14" /></button>
        </div>
        <div v-if="syncStatus.missing_in_config.length" style="font-size:12px;color:var(--warning);margin-bottom:4px">
          ⚠ 缺少描述配置：{{ syncStatus.missing_in_config.join('、') }}
        </div>
        <div v-if="syncStatus.deleted_categories.length" style="font-size:12px;color:var(--danger);margin-bottom:4px">
          ✕ 配置指向已删除目录：{{ syncStatus.deleted_categories.join('、') }}
        </div>
        <div v-if="syncOk" style="font-size:12px;color:var(--success)">✓ 配置与文件系统完全同步</div>
      </div>
    </Transition>

    <!-- ═══ 加载/空状态 ═══ -->
    <div v-if="catLoading" class="loading">
      <div class="loading-spinner"></div>
      <span>加载类别中…</span>
    </div>

    <div v-else-if="!categories.length" class="empty">
      <Smile :size="48" class="empty-icon" />
      <p>暂无表情包类别。通过 QQ 群内 <code>/表情管理</code> 命令添加。</p>
    </div>

    <!-- ═══ 选中类别的图片 + 操作 ═══ -->
    <template v-if="selectedCat && !catLoading">
      <div class="card">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span style="font-weight:600;font-size:14px">{{ selectedCat }}</span>
          <span style="font-size:12px;color:var(--text-muted)">{{ memeTotal }} 张</span>
          <span class="toolbar-spacer" />
          <button class="btn btn-sm" @click="startEditDesc({ name: selectedCat, count: memeTotal, description: categories.find(c => c.name === selectedCat)?.description || '' } as any)">
            <FileText :size="13" /> 编辑描述
          </button>
          <button class="btn btn-sm btn-primary" @click="openUpload">
            <Upload :size="13" /> 上传
          </button>
          <button class="btn btn-sm" @click="confirmClear(selectedCat)">
            <Eraser :size="13" /> 清空
          </button>
          <button class="btn btn-sm btn-danger" @click="confirmDelete(selectedCat)">
            <Trash2 :size="13" /> 删除类别
          </button>
        </div>

        <!-- 行内编辑描述 -->
        <div v-if="editingDesc === selectedCat" style="display:flex;gap:8px;margin-top:10px;align-items:center">
          <input v-model="editDescValue" class="input" placeholder="类别描述…"
            @keyup.enter="saveDesc" @keyup.escape="cancelEditDesc" autofocus style="max-width:320px" />
          <button class="btn btn-sm btn-primary" @click="saveDesc">保存</button>
          <button class="btn btn-sm" @click="cancelEditDesc">取消</button>
        </div>
      </div>

      <!-- 上传卡片 -->
      <Transition name="slide">
        <div v-if="showUpload" class="card" style="border-left:3px solid var(--primary)">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;font-weight:600;font-size:13px">
            <Upload :size="16" style="color:var(--primary)" /> 上传图片到「{{ selectedCat }}」
            <button class="btn btn-xs btn-ghost" @click="showUpload = false" style="margin-left:auto"><X :size="14" /></button>
          </div>
          <label style="display:block;padding:24px;text-align:center;border:2px dashed var(--border);border-radius:var(--radius);cursor:pointer;transition:border-color 0.15s"
            :style="{ borderColor: uploadFile ? 'var(--primary)' : '' }">
            <input ref="fileInput" type="file" accept="image/*" @change="onFileChange" style="display:none" />
            <template v-if="!uploadFile">
              <Plus :size="28" style="color:var(--text-muted);margin-bottom:6px" />
              <div style="font-size:13px;color:var(--text)">点击选择图片文件</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:4px">支持 JPG / PNG / GIF / WebP</div>
            </template>
            <template v-else>
              <ImageIcon :size="28" style="color:var(--primary);margin-bottom:6px" />
              <div style="font-size:13px;color:var(--text);font-weight:500">{{ uploadFile.name }}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:4px">{{ (uploadFile.size / 1024).toFixed(1) }} KB</div>
            </template>
          </label>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
            <button class="btn btn-sm" @click="showUpload = false">取消</button>
            <button class="btn btn-sm btn-primary" :disabled="!uploadFile || uploading" @click="doUpload">
              <Upload :size="13" /> {{ uploading ? '上传中…' : '确认上传' }}
            </button>
          </div>
        </div>
      </Transition>

      <div v-if="memeLoading" class="loading">
        <div class="loading-spinner"></div>
        <span>加载图片中…</span>
      </div>

      <div v-else-if="!memes.length" class="empty">
        <ImageIcon :size="48" class="empty-icon" />
        <p>该类别暂无图片。点击「上传」添加。</p>
      </div>

      <div v-else class="meme-grid">
        <div v-for="meme in memes" :key="meme.file" class="meme-item">
          <img :src="memeUrl(meme.file)" :alt="meme.desc" loading="lazy" @click="lightboxImage = meme" />
          <div class="meme-overlay">
            <button class="meme-delete-btn" title="删除图片" @click.stop="confirmDeleteMeme(meme.file)">
              <Trash2 :size="14" />
            </button>
            <span class="meme-filename" @click="lightboxImage = meme">{{ meme.file.split('/').pop() }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════
   MemeManager — Indigo-Blue Theme
   ═══════════════════════════════════════════ */

/* ── 页面头部 ── */
.page-header { margin-bottom: 24px; }
.page-title-row { display: flex; align-items: center; gap: 12px; }
.page-icon-box {
  width: 44px; height: 44px; border-radius: var(--radius);
  display: flex; align-items: center; justify-content: center;
  background: var(--primary-light); color: var(--primary); flex-shrink: 0;
}
.page-title-row h1 { font-size: 20px; font-weight: 700; color: var(--text); margin: 0; }
.page-subtitle { margin: 2px 0 0 0; color: var(--text-muted); font-size: 13px; }

/* ── 工具栏 ── */
.search-box {
  flex: 0 1 320px; min-width: 200px; position: relative;
}
.search-icon {
  position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
  color: var(--text-muted); pointer-events: none;
}
.search-input { padding-left: 34px; padding-right: 30px; }
.search-clear {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  padding: 3px; display: flex; align-items: center; border-radius: 4px;
}
.search-clear:hover { color: var(--text); background: var(--surface-hover); }

/* ── 同步面板 ── */
.sync-panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 16px; overflow: hidden;
  box-shadow: var(--shadow-sm);
}
.sync-panel-header {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; background: var(--surface-hover);
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  border-bottom: 1px solid var(--border-light);
}
.sync-dismiss {
  margin-left: auto; background: none; border: none;
  color: var(--text-muted); cursor: pointer; padding: 2px; border-radius: 4px;
  display: flex; align-items: center;
}
.sync-dismiss:hover { color: var(--text); background: var(--border); }
.sync-panel-body {
  padding: 10px 14px; display: flex; flex-direction: column; gap: 6px;
}
.sync-item {
  display: flex; align-items: flex-start; gap: 8px;
  font-size: 13px; padding: 8px 10px; border-radius: var(--radius-sm);
  line-height: 1.5;
}
.sync-item-warn {
  color: #92400e; background: var(--warning-light); border: 1px solid #fde68a;
}
.sync-item-error {
  color: #991b1b; background: var(--danger-light); border: 1px solid #fecaca;
}
.sync-item-ok {
  color: #065f46; background: var(--success-light); border: 1px solid #a7f3d0;
}

/* ── 类别网格 ── */
.cat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}
.cat-card {
  background: var(--surface); border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm); border: 1px solid var(--border-light);
  overflow: hidden; cursor: pointer; transition: all 0.2s; position: relative;
}
.cat-card:hover {
  box-shadow: var(--shadow-md); transform: translateY(-2px);
  border-color: var(--border);
}
.cat-card-accent { height: 3px; }
.cat-card-body {
  display: flex; align-items: flex-start; gap: 14px; padding: 18px;
}
.cat-avatar {
  width: 46px; height: 46px; border-radius: var(--radius);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 19px; font-weight: 700; flex-shrink: 0;
}
.cat-info { flex: 1; min-width: 0; }
.cat-name {
  font-size: 15px; font-weight: 600; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.cat-count { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
.cat-desc {
  font-size: 12px; color: var(--text-secondary); margin-top: 6px;
  line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.cat-card-actions {
  display: flex; gap: 3px; padding: 0 18px 14px;
}
.icon-btn {
  display: flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--surface);
  color: var(--text-muted); cursor: pointer; transition: all 0.15s;
}
.icon-btn:hover { background: var(--surface-hover); color: var(--text); border-color: #cbd5e1; }
.icon-btn-warn:hover { background: var(--warning-light); color: #d97706; border-color: #fcd34d; }
.icon-btn-danger:hover { background: var(--danger-light); color: var(--danger); border-color: #fecaca; }

/* ── 行内编辑 ── */
.inline-edit {
  padding: 14px 18px; border-top: 1px solid var(--border-light);
  background: var(--surface-hover);
}
.inline-edit .input { margin-bottom: 8px; }
.inline-edit-actions { display: flex; gap: 6px; justify-content: flex-end; }

/* ── 面包屑返回 ── */
.breadcrumb-bar {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 16px; padding: 10px 0;
}
.back-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 14px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--surface);
  color: var(--text-secondary); font-size: 13px; font-weight: 500;
  cursor: pointer; transition: all 0.15s; font-family: var(--sans);
}
.back-btn:hover {
  background: var(--primary-light); color: var(--primary);
  border-color: var(--primary-100);
}
.breadcrumb-sep { color: var(--border); font-size: 16px; }
.breadcrumb-current { font-size: 15px; font-weight: 600; color: var(--text); }
.breadcrumb-count { font-size: 13px; color: var(--text-muted); margin-left: 4px; }

/* ── 图片工具栏 ── */
.image-toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 16px;
}

/* ── 上传卡片 ── */
.upload-card { margin-bottom: 16px; }
.upload-header {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 600; color: var(--text);
  margin-bottom: 14px;
}
.upload-body { display: flex; flex-direction: column; gap: 14px; }
.upload-dropzone {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 32px 20px; border: 2px dashed var(--border);
  border-radius: var(--radius); cursor: pointer; transition: all 0.15s;
  text-align: center;
}
.upload-dropzone:hover { border-color: var(--primary); background: var(--primary-50); }
.upload-dropzone.has-file { border-color: var(--primary); border-style: solid; background: var(--primary-light); }
.upload-plus { color: var(--primary); opacity: 0.6; }
.upload-hint { font-size: 13px; font-weight: 500; color: var(--text-secondary); }
.upload-formats { font-size: 11px; color: var(--text-muted); }
.upload-filename { font-size: 13px; font-weight: 600; color: var(--text); word-break: break-all; }
.upload-size { font-size: 12px; color: var(--text-muted); }
.upload-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* ── 图片网格 ── */
.meme-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 10px;
}
.meme-item {
  position: relative; aspect-ratio: 1; border-radius: var(--radius);
  overflow: hidden; background: var(--surface-hover);
  border: 1px solid var(--border-light); transition: all 0.2s;
}
.meme-item:hover {
  transform: scale(1.03); box-shadow: var(--shadow-md);
  border-color: var(--border); z-index: 1;
}
.meme-item img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform 0.3s; cursor: pointer;
}
.meme-item:hover img { transform: scale(1.08); }
.meme-overlay {
  position: absolute; inset: 0;
  background: linear-gradient(to top, rgba(0,0,0,0.65) 0%, transparent 50%);
  display: flex; flex-direction: column; align-items: center; justify-content: space-between;
  padding: 10px; opacity: 0; transition: opacity 0.2s; color: #fff;
}
.meme-item:hover .meme-overlay { opacity: 1; }
.meme-delete-btn {
  align-self: flex-end;
  display: flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border: none; border-radius: var(--radius-sm);
  background: rgba(239,68,68,0.85); color: #fff; cursor: pointer;
  transition: background 0.15s;
}
.meme-delete-btn:hover { background: #ef4444; }
.meme-filename {
  font-size: 11px; text-align: center; cursor: pointer;
  overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; max-width: 100%;
}

/* ── 灯箱 ── */
.lightbox-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.8);
  display: flex; align-items: center; justify-content: center;
  z-index: 2000; padding: 40px;
}
.lightbox-box {
  position: relative; background: var(--surface); border-radius: var(--radius-lg);
  overflow: hidden; max-width: 90vw; max-height: 90vh;
  box-shadow: var(--shadow-lg); display: flex; flex-direction: column;
}
.lightbox-close {
  position: absolute; top: 12px; right: 12px; z-index: 10;
  background: rgba(0,0,0,0.5); border: none; color: #fff;
  width: 36px; height: 36px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; transition: background 0.15s;
}
.lightbox-close:hover { background: rgba(0,0,0,0.7); }
.lightbox-img {
  max-width: 80vw; max-height: 65vh;
  object-fit: contain; display: block; background: #1e293b;
}
.lightbox-info {
  padding: 14px 18px; display: flex; align-items: center;
  gap: 10px; flex-wrap: wrap; font-size: 13px;
  border-top: 1px solid var(--border-light);
}
.lightbox-filename {
  font-weight: 600; color: var(--text);
  font-family: var(--mono); font-size: 12px;
}
.lightbox-desc { color: var(--text-secondary); margin-left: auto; }
.lightbox-tags { display: flex; flex-wrap: wrap; gap: 4px; }

/* ── Toast 通知 ── */
.toast {
  position: fixed; top: 20px; right: 20px; z-index: 3000;
  display: flex; align-items: center; gap: 8px;
  padding: 12px 16px; border-radius: var(--radius);
  font-size: 13px; font-weight: 500;
  box-shadow: var(--shadow-lg); max-width: 420px;
}
.toast-success {
  background: var(--success-light); color: #065f46;
  border: 1px solid #a7f3d0;
}
.toast-error {
  background: var(--danger-light); color: #991b1b;
  border: 1px solid #fecaca;
}
.toast-close {
  margin-left: 8px; background: none; border: none;
  color: inherit; opacity: 0.5; cursor: pointer; padding: 2px;
  display: flex; align-items: center; border-radius: 4px;
}
.toast-close:hover { opacity: 1; background: rgba(0,0,0,0.06); }

/* Toast transitions */
.toast-enter-active { transition: all 0.3s ease-out; }
.toast-leave-active { transition: all 0.2s ease-in; }
.toast-enter-from { opacity: 0; transform: translateX(40px); }
.toast-leave-to { opacity: 0; transform: translateX(40px); }

/* Modal transitions */
.modal-enter-active { transition: all 0.2s ease-out; }
.modal-leave-active { transition: all 0.15s ease-in; }
.modal-enter-from, .modal-leave-to { opacity: 0; }
.modal-enter-from .modal-box, .modal-leave-to .modal-box { transform: scale(0.95); }

/* Slide transitions */
.slide-enter-active { transition: all 0.25s ease-out; }
.slide-leave-active { transition: all 0.15s ease-in; }
.slide-enter-from { opacity: 0; transform: translateY(-8px); }
.slide-leave-to { opacity: 0; transform: translateY(-8px); }

/* ── 确认模态框 ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000; backdrop-filter: blur(2px);
}
.modal-box {
  background: var(--surface); border-radius: var(--radius-lg); padding: 32px;
  max-width: 440px; width: 90vw; text-align: center;
  box-shadow: var(--shadow-lg);
}
.modal-icon {
  width: 56px; height: 56px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto 16px;
}
.icon-warn { background: var(--warning-light); color: #d97706; }
.icon-danger { background: var(--danger-light); color: var(--danger); }
.modal-box h3 { margin: 0 0 10px; font-size: 18px; font-weight: 700; color: var(--text); }
.modal-desc {
  color: var(--text-secondary); line-height: 1.7;
  margin: 0 0 24px; font-size: 14px;
}
.modal-hint { color: var(--text-muted); font-size: 12px; }
.modal-danger-hint { color: var(--danger); font-size: 12px; font-weight: 500; }
.modal-actions { display: flex; gap: 10px; justify-content: center; }

/* ── 按钮变体 ── */
.btn-warn { background: #f59e0b; color: #fff; border-color: #d97706; }
.btn-warn:hover { background: #d97706; }

/* ── 加载/旋转动画 ── */
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
