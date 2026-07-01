<script setup lang="ts">
/** 群聊总结管理 — 浏览各群的历史总结 */
import { ref, onMounted } from 'vue'
import {
  listSummaryGroups, getGroupSummary, getGroupSummaryHistory,
} from '@/api/admin'
import type { SummaryGroup, SummaryHistoryEntry } from '@/types'
import { ScrollText, ChevronLeft, RefreshCw } from '@lucide/vue'

const groups = ref<SummaryGroup[]>([])
const loading = ref(true)
const error = ref('')

const selectedGroup = ref<number | null>(null)
const latestSummary = ref<string | null>(null)
const history = ref<SummaryHistoryEntry[]>([])
const histLoading = ref(false)

async function loadGroups() {
  loading.value = true
  error.value = ''
  try {
    const res = await listSummaryGroups()
    groups.value = res.groups
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function selectGroup(groupId: number) {
  selectedGroup.value = groupId
  histLoading.value = true
  try {
    const [sum, hist] = await Promise.all([
      getGroupSummary(groupId),
      getGroupSummaryHistory(groupId),
    ])
    latestSummary.value = sum.summary_text
    history.value = hist.summaries
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    histLoading.value = false
  }
}

function backToList() {
  selectedGroup.value = null
  latestSummary.value = null
  history.value = []
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

onMounted(loadGroups)
</script>

<template>
  <div class="summary-page">
    <div v-if="error" class="error-msg">{{ error }}</div>

    <!-- Page header -->
    <div class="page-header">
      <div class="page-header-left">
        <ScrollText :size="24" class="page-icon" />
        <div>
          <h2 class="page-title">群聊总结</h2>
          <p class="page-subtitle">浏览各群自动生成的历史总结</p>
        </div>
      </div>
    </div>

    <!-- Group list -->
    <div v-if="!selectedGroup">
      <div class="card">
        <div class="card-header">
          <span class="card-header-title">有总结的群 ({{ groups.length }})</span>
          <button class="btn btn-primary btn-sm" @click="loadGroups" :disabled="loading">
            <RefreshCw :size="14" class="btn-icon" /> 刷新
          </button>
        </div>
        <div v-if="loading" class="loading">加载中...</div>
        <div v-else-if="!groups.length" class="empty">
          <div class="empty-icon">--</div>
          暂无群聊总结数据
          <br /><small class="empty-hint">bot 会在群聊累积 100 条消息后自动生成总结</small>
        </div>
        <div v-else class="table-wrap">
          <table class="group-table">
            <thead><tr><th>群 ID</th><th>最近总结时间</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="g in groups" :key="g.group_id">
                <td><code class="group-id">{{ g.group_id }}</code></td>
                <td class="time-cell">{{ fmtTime(g.latest_summary) }}</td>
                <td><a href="#" class="view-link" @click.prevent="selectGroup(g.group_id)">查看</a></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Selected group detail -->
    <div v-if="selectedGroup">
      <div class="card">
        <div class="card-header">
          <button class="btn btn-ghost btn-sm" @click="backToList">
            <ChevronLeft :size="16" class="btn-icon" /> 返回列表
          </button>
          <span class="card-header-title">群 <code class="group-id">{{ selectedGroup }}</code> 的总结</span>
        </div>
        <div v-if="histLoading" class="loading">加载中...</div>
        <div v-else-if="!latestSummary" class="empty">该群暂无总结</div>
        <div v-else class="summary-highlight-card">
          <div class="summary-label">最新总结</div>
          <div class="summary-text">{{ latestSummary }}</div>
        </div>
      </div>

      <!-- History timeline -->
      <div v-if="history.length" class="card history-card">
        <div class="card-header">
          <span class="card-header-title">历史总结 ({{ history.length }})</span>
        </div>
        <div class="timeline">
          <div v-for="h in history" :key="h.id" class="timeline-item">
            <div class="timeline-dot"></div>
            <div class="timeline-content">
              <div class="timeline-meta">
                <span class="timeline-time">{{ fmtTime(h.created_at) }}</span>
                <span v-if="h.message_range_start" class="timeline-range tag tag-blue">
                  #{{ h.message_range_start }} - #{{ h.message_range_end }}
                </span>
              </div>
              <div class="timeline-summary">{{ h.summary_text }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.summary-page {
  width: 100%;
}

/* ---- Page header ---- */
.page-header {
  margin-bottom: 24px;
}
.page-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.page-icon {
  color: #6366f1;
  flex-shrink: 0;
}
.page-title {
  font-size: 22px;
  font-weight: 700;
  color: #1e293b;
  margin: 0;
  line-height: 1.3;
}
.page-subtitle {
  font-size: 14px;
  color: #94a3b8;
  margin: 2px 0 0;
}

/* ---- Card overrides ---- */
:deep(.card) {
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  overflow: hidden;
}
:deep(.card-header) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  background: #ffffff;
  border-bottom: 1px solid #f1f5f9;
}
.card-header-title {
  font-size: 15px;
  font-weight: 600;
  color: #334155;
}

/* ---- Error message ---- */
.error-msg {
  background: #fef2f2;
  color: #dc2626;
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 16px;
  font-size: 14px;
}

/* ---- Group table ---- */
.group-table {
  width: 100%;
  border-collapse: collapse;
}
.group-table th {
  text-align: left;
  padding: 12px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
}
.group-table td {
  padding: 14px 16px;
  font-size: 14px;
  color: #334155;
  border-bottom: 1px solid #f1f5f9;
}
.group-table tr:last-child td {
  border-bottom: none;
}
.group-table tr:hover td {
  background: #f8fafc;
}
.group-id {
  font-family: 'SFMono-Regular', 'Menlo', 'Consolas', monospace;
  font-size: 13px;
  color: #6366f1;
  background: #eef2ff;
  padding: 2px 8px;
  border-radius: 4px;
}
.time-cell {
  font-size: 13px;
  color: #94a3b8;
}
.view-link {
  color: #6366f1;
  text-decoration: none;
  font-weight: 500;
  font-size: 13px;
  transition: color 0.15s;
}
.view-link:hover {
  color: #4f46e5;
  text-decoration: underline;
}

/* ---- Button icon spacing ---- */
.btn-icon {
  margin-right: 4px;
  vertical-align: middle;
}

/* ---- Empty state ---- */
.empty-hint {
  color: #94a3b8;
  font-size: 12px;
}

/* ---- Summary highlight card ---- */
.summary-highlight-card {
  background: #eef2ff;
  border-radius: 10px;
  margin: 16px;
  padding: 20px;
  border: 1px solid #c7d2fe;
}
.summary-label {
  font-size: 12px;
  font-weight: 600;
  color: #6366f1;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}
.summary-text {
  font-size: 14px;
  line-height: 1.8;
  color: #334155;
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---- Timeline ---- */
.history-card {
  margin-top: 16px;
}
.timeline {
  position: relative;
  padding: 8px 0 8px 32px;
}
.timeline::before {
  content: '';
  position: absolute;
  left: 15px;
  top: 20px;
  bottom: 20px;
  width: 2px;
  background: #e2e8f0;
  border-radius: 1px;
}
.timeline-item {
  position: relative;
  padding: 14px 0 14px 20px;
}
.timeline-dot {
  position: absolute;
  left: -25px;
  top: 20px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #6366f1;
  border: 2px solid #ffffff;
  box-shadow: 0 0 0 2px #6366f1;
  z-index: 1;
}
.timeline-content {
  min-width: 0;
}
.timeline-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.timeline-time {
  font-size: 12px;
  color: #94a3b8;
}
.timeline-range {
  font-size: 11px;
}
.timeline-summary {
  font-size: 14px;
  line-height: 1.7;
  color: #475569;
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---- Loading ---- */
:deep(.loading) {
  text-align: center;
  padding: 48px 20px;
  color: #94a3b8;
  font-size: 14px;
}

/* ---- Empty ---- */
:deep(.empty) {
  text-align: center;
  padding: 48px 20px;
  color: #94a3b8;
  font-size: 14px;
}
</style>
