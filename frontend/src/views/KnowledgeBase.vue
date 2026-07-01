<script setup lang="ts">
/** 知识库查看 */
import { ref, onMounted } from 'vue'
import { listKnowledge, getKnowledgeSection } from '@/api/admin'
import type { KnowledgeSection } from '@/types'
import { BookOpen, FileText, ChevronLeft, ChevronRight, ArrowLeft } from '@lucide/vue'

const sections = ref<KnowledgeSection[]>([])
const sources = ref<string[]>([])
const total = ref(0)
const page = ref(1)
const perPage = 50
const filterSource = ref('')
const loading = ref(true)
const error = ref('')

const selectedSection = ref<KnowledgeSection | null>(null)
const sectionLoading = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listKnowledge(filterSource.value, page.value, perPage)
    sections.value = res.sections
    sources.value = res.sources
    total.value = res.total
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function viewSection(id: number) {
  sectionLoading.value = true
  selectedSection.value = null
  try {
    selectedSection.value = await getKnowledgeSection(id)
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    sectionLoading.value = false
  }
}

function bySource(source: string) {
  filterSource.value = source
  page.value = 1
  load()
}

function totalPages() {
  return Math.ceil(total.value / perPage)
}

function goPage(delta: number) {
  const np = page.value + delta
  if (np >= 1 && np <= totalPages()) {
    page.value = np
    load()
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div v-if="error" class="error-msg">{{ error }}</div>

    <!-- List view -->
    <div v-if="!selectedSection">
      <!-- Page header -->
      <div class="page-header">
        <BookOpen :size="22" class="page-header-icon" />
        <div>
          <h3 class="page-header-title">知识库</h3>
          <p class="page-header-subtitle">共 {{ total }} 个章节</p>
        </div>
      </div>

      <div class="card">
        <!-- Source filter pills -->
        <div v-if="sources.length" class="tab-bar" style="margin-bottom:16px">
          <button
            class="tab-btn"
            :class="{ active: !filterSource }"
            @click="bySource('')"
          >全部</button>
          <button
            v-for="s in sources" :key="s"
            class="tab-btn"
            :class="{ active: filterSource === s }"
            @click="bySource(s)"
          >{{ s }}</button>
        </div>

        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="!sections.length" class="empty">知识库为空</div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:60px">ID</th>
                <th>章节标题</th>
                <th style="width:120px">来源</th>
                <th style="width:80px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="sec in sections" :key="sec.id">
                <td class="cell-id">{{ sec.id }}</td>
                <td class="cell-title">{{ sec.title }}</td>
                <td>
                  <span class="tag tag-blue">{{ sec.source }}</span>
                </td>
                <td>
                  <a href="#" class="action-link" @click.prevent="viewSection(sec.id)">
                    <FileText :size="14" />
                    查看
                  </a>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="totalPages() > 1" class="pagination">
            <button class="btn btn-sm" :disabled="page <= 1" @click="goPage(-1)">
              <ChevronLeft :size="14" />
            </button>
            <span class="page-info">第 {{ page }} / {{ totalPages() }} 页</span>
            <button class="btn btn-sm" :disabled="page >= totalPages()" @click="goPage(1)">
              <ChevronRight :size="14" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Detail view -->
    <div v-if="sectionLoading" class="loading">加载中...</div>
    <div v-if="selectedSection" class="card detail-card">
      <!-- Back button + meta row -->
      <div class="detail-header">
        <button class="btn btn-sm btn-ghost" @click="selectedSection = null">
          <ArrowLeft :size="14" />
          返回列表
        </button>
        <span class="tag tag-blue">{{ selectedSection.source }}</span>
      </div>

      <!-- Title -->
      <h3 class="detail-title">{{ selectedSection.title }}</h3>

      <!-- Content box -->
      <div class="content-box">
        {{ selectedSection.content }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Page header ─────────────────────────── */
.page-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 20px;
}
.page-header-icon {
  color: var(--primary);
  flex-shrink: 0;
}
.page-header-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  line-height: 1.3;
}
.page-header-subtitle {
  font-size: 13px;
  color: var(--text-muted);
  margin: 2px 0 0;
}

/* ── Table cell tweaks ───────────────────── */
.cell-id {
  color: var(--text-muted);
  font-family: var(--mono);
  font-size: 12px;
}
.cell-title {
  font-weight: 500;
  color: var(--primary-700);
}
.action-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 500;
}

/* ── Pagination info ─────────────────────── */
.page-info {
  font-size: 13px;
  color: var(--text-secondary);
  padding: 0 4px;
}

/* ── Detail view ─────────────────────────── */
.detail-card {
  padding: 24px 28px;
}
.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
}
.detail-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  margin: 0 0 16px;
  line-height: 1.4;
}
.content-box {
  background: var(--primary-light);
  border: 1px solid var(--primary-100);
  border-radius: var(--radius);
  padding: 20px 24px;
  white-space: pre-wrap;
  line-height: 1.8;
  font-size: 14px;
  font-family: var(--mono);
  color: var(--text);
}
</style>
