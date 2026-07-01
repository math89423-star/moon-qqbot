<script setup lang="ts">
/** Bot 检测打标 — 查看/管理被 BotDetector 标记的疑似 Bot 用户 */
import { ref, onMounted, computed } from 'vue'
import {
  listSuspectedBots, getLiveDetections,
  updateSuspectedBot, resetSuspectedBot, deleteSuspectedBot,
} from '@/api/admin'
import type { SuspectedBot, BotDetectLive } from '@/types'
import {
  ScanEye, Bot, User, ShieldX, AlertTriangle, CheckCircle,
  RefreshCw, Trash2, RotateCcw, Search, ChevronDown, ChevronUp,
} from '@lucide/vue'

const dbBots = ref<SuspectedBot[]>([])
const live = ref<BotDetectLive | null>(null)
const loading = ref(true)
const error = ref('')
const statusFilter = ref('')
const searchQuery = ref('')

// 详情展开
const expanded = ref<Set<string>>(new Set())

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const [bots, liveData] = await Promise.all([
      listSuspectedBots(statusFilter.value),
      getLiveDetections(),
    ])
    dbBots.value = bots
    live.value = liveData
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '获取数据失败'
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

const filteredBots = computed(() => {
  if (!searchQuery.value.trim()) return dbBots.value
  const q = searchQuery.value.trim().toLowerCase()
  return dbBots.value.filter(b =>
    b.user_id.includes(q) ||
    b.user_name.toLowerCase().includes(q) ||
    b.notes.toLowerCase().includes(q)
  )
})

function toggleExpand(userId: string) {
  if (expanded.value.has(userId)) {
    expanded.value.delete(userId)
  } else {
    expanded.value.add(userId)
  }
}

async function doUpdateStatus(userId: string, status: string) {
  try {
    await updateSuspectedBot(userId, { status })
    await refresh()
  } catch (e: any) {
    alert(e?.response?.data?.message || e.message || '更新失败')
  }
}

async function doReset(userId: string) {
  if (!confirm(`确定清除用户 ${userId.slice(0, 8)}... 的所有检测数据？`)) return
  try {
    await resetSuspectedBot(userId)
    await refresh()
  } catch (e: any) {
    alert(e?.response?.data?.message || e.message || '重置失败')
  }
}

async function doDelete(userId: string) {
  if (!confirm(`确定从 DB 删除用户 ${userId.slice(0, 8)}... 的标记记录？`)) return
  try {
    await deleteSuspectedBot(userId)
    await refresh()
  } catch (e: any) {
    alert(e?.response?.data?.message || e.message || '删除失败')
  }
}

function statusBadge(status: string) {
  switch (status) {
    case 'flagged':    return { text: '待审核', cls: 'tag-orange' }
    case 'confirmed':  return { text: '已确认', cls: 'tag tag-red' }
    case 'false_positive': return { text: '误判', cls: 'tag-green' }
    default:           return { text: status || '未知', cls: 'tag-gray' }
  }
}

function statusIcon(status: string) {
  switch (status) {
    case 'flagged':    return AlertTriangle
    case 'confirmed':  return Bot
    case 'false_positive': return CheckCircle
    default:           return null
  }
}

function scoreColor(score: number) {
  if (score >= 0.8) return '#e53e3e'
  if (score >= 0.7) return '#f59e0b'
  if (score >= 0.5) return '#eab308'
  return '#888'
}

function scoreLabel(score: number) {
  if (score >= 0.8) return '高危'
  if (score >= 0.7) return '可疑'
  if (score >= 0.5) return '注意'
  return '低'
}

function fmtTime(ts: number) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

const signalLabels: Record<string, string> = {
  latency_variance: '响应间隔规律',
  response_inevitability: '从不漏回',
  pattern_regularity: '句式规律',
  nocturnal_activity: '凌晨出没',
  trigger_selectivity: '触发选择性',
  social_mention: '群友指控',
}

const counts = computed(() => {
  const c = { flagged: 0, confirmed: 0, false_positive: 0, total: dbBots.value.length }
  for (const b of dbBots.value) {
    if (b.status === 'flagged') c.flagged++
    else if (b.status === 'confirmed') c.confirmed++
    else if (b.status === 'false_positive') c.false_positive++
  }
  return c
})
</script>

<template>
  <div class="page">
    <!-- 页面标题栏 -->
    <div class="page-header">
      <ScanEye :size="24" class="page-header-icon" />
      <div>
        <h2 class="page-title">Bot 检测</h2>
        <p class="page-subtitle">
          共 <strong>{{ counts.total }}</strong> 个标记
          <span v-if="counts.flagged" class="tag tag-orange" style="margin-left:6px">
            <AlertTriangle :size="10" /> {{ counts.flagged }}
          </span>
          <span v-if="counts.confirmed" class="tag tag-red" style="margin-left:4px">
            <Bot :size="10" /> {{ counts.confirmed }}
          </span>
          <span v-if="counts.false_positive" class="tag tag-green" style="margin-left:4px">
            <CheckCircle :size="10" /> {{ counts.false_positive }}
          </span>
        </p>
      </div>
      <button class="btn btn-sm" style="margin-left:auto" @click="refresh">
        <RefreshCw :size="14" /> 刷新
      </button>
    </div>

    <div v-if="error" class="error-msg">
      <AlertTriangle :size="16" style="margin-right:6px;flex-shrink:0" />
      <span>{{ error }}</span>
    </div>

    <!-- 实时统计卡片 -->
    <div v-if="live" class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background:var(--primary-light);color:var(--primary)">
          <Bot :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ live.total_tracked }}</div>
          <div class="stat-label">实时追踪中</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:var(--warning-light);color:var(--warning)">
          <AlertTriangle :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ live.action_taken_count }}</div>
          <div class="stat-label">已执行动作</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:var(--primary-light);color:var(--primary-700)">
          <ShieldX :size="20" />
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ live.peer_isolated?.length ?? 0 }}</div>
          <div class="stat-label">Peer 隔离中</div>
        </div>
      </div>
    </div>

    <!-- 过滤栏 + 搜索 -->
    <div class="card">
      <div class="toolbar">
        <div class="tab-bar" style="margin-bottom:0; background:transparent; padding:0">
          <button
            :class="['tab-btn', { active: statusFilter === '' }]"
            @click="statusFilter = ''; refresh()"
          >全部</button>
          <button
            :class="['tab-btn', { active: statusFilter === 'flagged' }]"
            @click="statusFilter = 'flagged'; refresh()"
          >
            <AlertTriangle :size="12" /> 待审核
          </button>
          <button
            :class="['tab-btn', { active: statusFilter === 'confirmed' }]"
            @click="statusFilter = 'confirmed'; refresh()"
          >
            <Bot :size="12" /> 已确认
          </button>
          <button
            :class="['tab-btn', { active: statusFilter === 'false_positive' }]"
            @click="statusFilter = 'false_positive'; refresh()"
          >
            <CheckCircle :size="12" /> 误判
          </button>
        </div>
        <div class="toolbar-spacer" />
        <div class="search-bar">
          <Search :size="14" style="color:var(--text-muted);flex-shrink:0" />
          <input
            v-model="searchQuery"
            placeholder="搜索用户 ID / 昵称 / 备注..."
            class="input"
            style="max-width:280px"
          />
        </div>
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="loading && !dbBots.length" class="loading">
      <div class="loading-spinner" />
      加载中...
    </div>

    <!-- 表格 -->
    <div v-if="filteredBots.length" class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:154px">用户 ID</th>
              <th style="width:100px">昵称</th>
              <th style="width:120px">嫌疑分</th>
              <th style="width:58px">信号</th>
              <th style="width:80px">状态</th>
              <th style="width:58px">来源</th>
              <th style="width:140px">更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="b in filteredBots" :key="b.user_id">
              <tr
                :class="['data-row', { 'row-expanded': expanded.has(b.user_id) }]"
                @click="toggleExpand(b.user_id)"
              >
                <td class="cell-userid">
                  <component
                    :is="expanded.has(b.user_id) ? ChevronUp : ChevronDown"
                    :size="13"
                    class="expand-arrow"
                  />
                  <span class="mono">{{ b.user_id.slice(0, 12) }}{{ b.user_id.length > 12 ? '...' : '' }}</span>
                </td>
                <td>
                  <span class="user-name">{{ b.user_name || '—' }}</span>
                </td>
                <td>
                  <div class="score-cell">
                    <div class="score-bar-bg">
                      <div
                        class="score-bar-fill"
                        :style="{
                          width: (b.suspicion_score * 100) + '%',
                          backgroundColor: scoreColor(b.suspicion_score),
                        }"
                      ></div>
                    </div>
                    <span class="score-val" :style="{ color: scoreColor(b.suspicion_score) }">
                      {{ b.suspicion_score.toFixed(2) }}
                    </span>
                    <span
                      class="score-tag"
                      :style="{
                        backgroundColor: scoreColor(b.suspicion_score) + '18',
                        color: scoreColor(b.suspicion_score),
                      }"
                    >{{ scoreLabel(b.suspicion_score) }}</span>
                  </div>
                </td>
                <td class="cell-muted">{{ Object.keys(b.live_signals || {}).length || '—' }}</td>
                <td>
                  <span :class="'status-badge ' + statusBadge(b.status).cls">
                    <component
                      :is="statusIcon(b.status)"
                      v-if="statusIcon(b.status)"
                      :size="11"
                    />
                    {{ statusBadge(b.status).text }}
                  </span>
                </td>
                <td>
                  <span
                    :class="b.marked_by === 'auto' ? 'tag tag-gray' : 'tag source-manual'"
                  >{{ b.marked_by === 'auto' ? '自动' : '手动' }}</span>
                </td>
                <td class="cell-muted cell-time">{{ fmtTime(b.updated_at) }}</td>
                <td class="cell-actions" @click.stop>
                  <button
                    v-if="b.status === 'flagged'"
                    class="btn btn-xs btn-danger"
                    @click="doUpdateStatus(b.user_id, 'confirmed')"
                    title="确认为 Bot"
                  >
                    <Bot :size="12" />
                  </button>
                  <button
                    v-if="b.status === 'flagged'"
                    class="btn btn-xs btn-soft-green"
                    @click="doUpdateStatus(b.user_id, 'false_positive')"
                    title="标记为误判"
                  >
                    <User :size="12" />
                  </button>
                  <button
                    v-if="b.status === 'confirmed'"
                    class="btn btn-xs btn-soft-indigo"
                    @click="doUpdateStatus(b.user_id, 'false_positive')"
                    title="改为误判"
                  >
                    <ShieldX :size="12" />
                  </button>
                  <button
                    class="btn btn-xs btn-soft-amber"
                    @click="doReset(b.user_id)"
                    title="清除所有检测数据"
                  >
                    <RotateCcw :size="12" />
                  </button>
                  <button
                    class="btn btn-xs btn-soft-red"
                    @click="doDelete(b.user_id)"
                    title="仅删 DB 记录"
                  >
                    <Trash2 :size="12" />
                  </button>
                </td>
              </tr>
              <!-- 展开行: 信号详情 -->
              <tr v-if="expanded.has(b.user_id)" class="expand-row">
                <td :colspan="8">
                  <div class="signal-panel">
                    <div class="signal-panel-head">
                      <ScanEye :size="14" class="signal-panel-icon" />
                      <span>实时信号详情</span>
                    </div>
                    <div v-if="b.live_signals && Object.keys(b.live_signals).length" class="signal-grid">
                      <div
                        v-for="(val, key) in b.live_signals"
                        :key="key"
                        class="signal-item"
                      >
                        <div class="signal-label">{{ signalLabels[key] || key }}</div>
                        <div class="signal-bar-bg">
                          <div
                            class="signal-bar-fill"
                            :style="{
                              width: (val * 100) + '%',
                              backgroundColor: scoreColor(val),
                            }"
                          ></div>
                        </div>
                        <div class="signal-val" :style="{ color: scoreColor(val) }">
                          {{ val.toFixed(2) }}
                        </div>
                      </div>
                    </div>
                    <div v-else class="signal-empty">暂无实时信号数据</div>
                    <div v-if="b.notes" class="signal-notes">
                      <span class="signal-notes-label">备注：</span>{{ b.notes }}
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 空状态 -->
    <div v-if="!loading && !filteredBots.length && !error" class="empty">
      <ScanEye :size="40" class="empty-icon" />
      <p>
        <template v-if="statusFilter">没有「{{ statusBadge(statusFilter).text }}」状态的记录</template>
        <template v-else>暂无疑似 Bot 记录</template>
      </p>
    </div>
  </div>
</template>

<style scoped>
/* ── Bot Detect Page ── */
.bot-detect-page {
  max-width: 1200px;
}

/* ── Page Header ── */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}

.page-header-left {
  display: flex;
  align-items: center;
  gap: 14px;
}

.page-icon-wrap {
  width: 42px;
  height: 42px;
  background: linear-gradient(135deg, #6366f1, #818cf8);
  border-radius: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  box-shadow: 0 2px 8px rgba(99,102,241,0.30);
  flex-shrink: 0;
}

.page-title {
  font-size: 20px;
  font-weight: 700;
  color: #1e293b;
  margin: 0;
  line-height: 1.3;
}

.page-subtitle {
  font-size: 13px;
  color: #94a3b8;
  margin: 2px 0 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

.page-subtitle strong {
  color: #475569;
  font-weight: 600;
}

/* ── Error Message ── */
.error-msg {
  display: flex;
  align-items: center;
  padding: 10px 16px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  color: #dc2626;
  font-size: 13px;
  margin-bottom: 16px;
}

/* ── Stats Row ── */
.stat-row {
  display: flex;
  gap: 16px;
  margin-bottom: 20px;
}

.stat-card {
  flex: 1;
  min-width: 140px;
  background: #fff;
  border-radius: 12px;
  padding: 18px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  border: 1px solid #f1f5f9;
  transition: box-shadow 0.15s ease;
}

.stat-card:hover {
  box-shadow: 0 4px 14px rgba(0,0,0,0.08);
}

.stat-icon {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-icon-blue {
  background: linear-gradient(135deg, #3b82f6, #60a5fa);
  color: #fff;
  box-shadow: 0 2px 6px rgba(59,130,246,0.30);
}

.stat-icon-amber {
  background: linear-gradient(135deg, #f59e0b, #fbbf24);
  color: #fff;
  box-shadow: 0 2px 6px rgba(245,158,11,0.30);
}

.stat-icon-indigo {
  background: linear-gradient(135deg, #6366f1, #818cf8);
  color: #fff;
  box-shadow: 0 2px 6px rgba(99,102,241,0.30);
}

.stat-body {
  flex: 1;
}

.stat-num {
  font-size: 27px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.1;
}

.stat-label {
  font-size: 12px;
  color: #94a3b8;
  margin-top: 4px;
}

/* ── Filter Card ── */
.filter-card {
  padding: 12px 16px;
  margin-bottom: 16px;
  background: #fff;
  border: 1px solid #f1f5f9;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-group {
  display: flex;
  gap: 4px;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 14px;
  border: 1px solid #e2e8f0;
  background: #fff;
  color: #64748b;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
  line-height: 1.4;
}

.pill:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
  color: #475569;
}

.pill-active {
  background: #6366f1;
  border-color: #6366f1;
  color: #fff;
}

.pill-active:hover {
  background: #4f46e5;
  border-color: #4f46e5;
  color: #fff;
}

.search-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 4px 12px;
  min-width: 220px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.search-wrap:focus-within {
  border-color: #6366f1;
  box-shadow: 0 0 0 2px rgba(99,102,241,0.12);
}

.search-icon {
  color: #94a3b8;
  flex-shrink: 0;
}

.search-wrap .input {
  border: none;
  background: transparent;
  font-size: 13px;
  padding: 6px 0;
  width: 100%;
  outline: none;
  color: #334155;
}

.search-wrap .input::placeholder {
  color: #94a3b8;
}

/* ── Table Card ── */
.table-card {
  padding: 0;
  overflow: hidden;
  background: #fff;
  border: 1px solid #f1f5f9;
  border-radius: 10px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}

.table-wrap {
  overflow-x: auto;
}

.table-wrap table {
  width: 100%;
  border-collapse: collapse;
}

.table-wrap thead th {
  text-align: left;
  padding: 11px 14px;
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: #f8fafc;
  border-bottom: 1px solid #eef2f6;
  white-space: nowrap;
}

.table-wrap tbody tr.data-row {
  border-bottom: 1px solid #f1f5f9;
  transition: background 0.1s ease;
  cursor: pointer;
}

.table-wrap tbody tr.data-row:hover {
  background: #f8fafc;
}

.table-wrap tbody tr.data-row:last-of-type {
  border-bottom: none;
}

.table-wrap tbody tr.data-row.row-expanded {
  background: #f8fafc;
}

.table-wrap tbody td {
  padding: 11px 14px;
  font-size: 13px;
  color: #334155;
  vertical-align: middle;
}

/* ── Cells ── */
.cell-userid {
  display: flex;
  align-items: center;
  gap: 5px;
}

.cell-userid .mono {
  font-family: var(--mono, 'SF Mono', 'Fira Code', 'Cascadia Code', monospace);
  font-size: 12px;
  color: #64748b;
}

.expand-arrow {
  color: #94a3b8;
  flex-shrink: 0;
  transition: transform 0.15s ease;
}

.user-name {
  font-weight: 500;
  color: #1e293b;
}

.cell-muted {
  color: #94a3b8;
  font-size: 12px;
}

.cell-time {
  white-space: nowrap;
}

.cell-actions {
  white-space: nowrap;
}

.cell-actions > :deep(.btn) {
  margin-right: 3px;
}

.cell-actions > :deep(.btn):last-child {
  margin-right: 0;
}

/* ── Score Cell ── */
.score-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.score-bar-bg {
  width: 56px;
  height: 6px;
  background: #f1f5f9;
  border-radius: 3px;
  overflow: hidden;
  flex-shrink: 0;
}

.score-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.score-val {
  font-size: 12px;
  font-weight: 600;
  min-width: 32px;
  font-variant-numeric: tabular-nums;
}

.score-tag {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
}

/* ── Status Badge ── */
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 500;
}

/* ── Action Button Variants ── */
.btn-xs {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 5px;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  transition: all 0.12s ease;
  line-height: 1;
}

.btn-xs:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
}

.btn-soft-green {
  color: #16a34a;
  border-color: #bbf7d0;
}

.btn-soft-green:hover {
  background: #f0fdf4;
  border-color: #86efac;
}

.btn-soft-indigo {
  color: #6366f1;
  border-color: #c7d2fe;
}

.btn-soft-indigo:hover {
  background: #eef2ff;
  border-color: #a5b4fc;
}

.btn-soft-amber {
  color: #d97706;
  border-color: #fde68a;
}

.btn-soft-amber:hover {
  background: #fffbeb;
  border-color: #fcd34d;
}

.btn-soft-red {
  color: #dc2626;
  border-color: #fecaca;
}

.btn-soft-red:hover {
  background: #fef2f2;
  border-color: #fca5a5;
}

/* ── Expand Row (Signal Details) ── */
.expand-row td {
  padding: 0;
  background: #fafbfc;
  border-bottom: 1px solid #f1f5f9;
}

.expand-row:last-of-type td {
  border-bottom: none;
}

.signal-panel {
  padding: 16px 24px 20px 66px;
  border-top: 1px dashed #e2e8f0;
}

.signal-panel-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #6366f1;
  margin-bottom: 14px;
}

.signal-panel-icon {
  flex-shrink: 0;
}

.signal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 8px;
}

.signal-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #f1f5f9;
}

.signal-label {
  font-size: 11px;
  color: #64748b;
  min-width: 80px;
  font-weight: 500;
  flex-shrink: 0;
}

.signal-bar-bg {
  flex: 1;
  height: 6px;
  background: #f1f5f9;
  border-radius: 3px;
  overflow: hidden;
}

.signal-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
}

.signal-val {
  font-size: 11px;
  font-weight: 600;
  min-width: 36px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.signal-empty {
  color: #94a3b8;
  font-size: 12px;
  padding: 10px 14px;
  background: #fff;
  border-radius: 8px;
  border: 1px dashed #e2e8f0;
  text-align: center;
}

.signal-notes {
  margin-top: 12px;
  font-size: 12px;
  color: #475569;
  padding: 10px 14px;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #f1f5f9;
}

.signal-notes-label {
  color: #94a3b8;
  font-weight: 500;
}

/* ── Empty State ── */
.empty-state {
  text-align: center;
  padding: 60px 20px;
  background: #fff;
  border-radius: 12px;
  border: 1px solid #f1f5f9;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}

.empty-icon {
  color: #cbd5e1;
  margin-bottom: 12px;
}

.empty-text {
  font-size: 14px;
  color: #94a3b8;
  margin: 0;
}

/* ── Loading ── */
.loading {
  text-align: center;
  padding: 40px;
  color: #94a3b8;
  font-size: 14px;
}

/* ── Global Tag Overrides for this view ── */
:deep(.tag) {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

:deep(.tag-red) {
  background: #fef2f2;
  color: #dc2626;
  border: 1px solid #fecaca;
}

:deep(.tag-orange) {
  background: #fff7ed;
  color: #ea580c;
  border: 1px solid #fed7aa;
}

:deep(.tag-green) {
  background: #f0fdf4;
  color: #16a34a;
  border: 1px solid #bbf7d0;
}

:deep(.tag-gray) {
  background: #f8fafc;
  color: #64748b;
  border: 1px solid #e2e8f0;
}

:deep(.tag-blue) {
  background: #eef2ff;
  color: #6366f1;
  border: 1px solid #c7d2fe;
}

/* ── Source "手动" badge ── */
.source-manual {
  background: #eef2ff;
  color: #6366f1;
  border: 1px solid #c7d2fe;
}
</style>
