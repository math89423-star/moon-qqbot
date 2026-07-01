<script setup lang="ts">
/** 用户记忆管理 — 浏览 + 搜索 + 删除 (per-bot 隔离) */
import { ref, inject, onMounted, watch, type Ref } from 'vue'
import {
  listMemoryUsers, searchMemories, getUserMemories, deleteUserFact,
} from '@/api/admin'
import type { MemoryUser, MemoryFact } from '@/types'
import { Users, Search, Trash2, ChevronLeft, ChevronRight, Puzzle } from '@lucide/vue'

const currentBot = inject<Ref<string>>('currentBot')!
const bots = inject<Ref<{ id: string; name: string }[]>>('bots')!
const switchBot = inject<(id: string) => void>('switchBot')!

const botId = ref(currentBot.value)

watch(currentBot, (id) => {
  botId.value = id
  page.value = 1
  selectedUser.value = null
  searchResults.value = []
  loadUsers()
})

const users = ref<MemoryUser[]>([])
const totalUsers = ref(0)
const page = ref(1)
const perPage = 20
const loading = ref(true)
const error = ref('')

const searchQuery = ref('')
const searchResults = ref<MemoryFact[]>([])
const searching = ref(false)

const selectedUser = ref<string | null>(null)
const userFacts = ref<MemoryFact[]>([])
const factsLoading = ref(false)
const deleting = ref<string | null>(null) // fact_key being deleted

async function loadUsers() {
  loading.value = true
  error.value = ''
  try {
    const res = await listMemoryUsers(page.value, perPage, botId.value)
    users.value = res.users
    totalUsers.value = res.total
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function doSearch() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  try {
    const res = await searchMemories(searchQuery.value.trim(), 20, botId.value)
    searchResults.value = res.results
    selectedUser.value = null
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '搜索失败'
  } finally {
    searching.value = false
  }
}

async function selectUser(userId: string) {
  selectedUser.value = userId
  searchResults.value = []
  factsLoading.value = true
  try {
    const res = await getUserMemories(userId, 1, 50, botId.value)
    userFacts.value = res.facts
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    factsLoading.value = false
  }
}

async function deleteFact(userId: string, factKey: string) {
  if (!confirm(`确定删除这条记忆? ${factKey}`)) return
  deleting.value = factKey
  try {
    await deleteUserFact(userId, factKey, botId.value)
    userFacts.value = userFacts.value.filter((f: MemoryFact) => f.fact_key !== factKey)
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '删除失败'
  } finally {
    deleting.value = null
  }
}

function backToList() {
  selectedUser.value = null
  userFacts.value = []
  searchResults.value = []
  searchQuery.value = ''
}

function totalPages() {
  return Math.ceil(totalUsers.value / perPage)
}

function goPage(delta: number) {
  const np = page.value + delta
  if (np >= 1 && np <= totalPages()) {
    page.value = np
    loadUsers()
  }
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

onMounted(loadUsers)
</script>

<template>
  <div class="page">
    <!-- Page Header -->
    <div class="page-header">
      <Puzzle :size="24" class="page-header-icon" />
      <div>
        <h2 class="page-title">用户记忆</h2>
        <p class="page-subtitle">浏览和管理用户长期记忆 (per-bot 隔离)</p>
      </div>
      <div style="margin-left: auto;">
        <select class="bot-selector" :value="botId" @change="switchBot(($event.target as HTMLSelectElement).value)">
          <option v-for="b in bots" :key="b.id" :value="b.id">{{ b.name }}</option>
        </select>
      </div>
    </div>

    <!-- Error -->
    <div v-if="error" class="error-msg">{{ error }}</div>

    <!-- Search bar (only show on list view) -->
    <div v-if="!selectedUser" class="card search-card">
      <div class="search-bar">
        <Search :size="16" class="search-icon" />
        <input
          v-model="searchQuery" type="text" class="input"
          placeholder="搜索记忆内容..."
          @keyup.enter="doSearch"
        />
        <button class="btn btn-primary" :disabled="searching" @click="doSearch">
          {{ searching ? '搜索中...' : '搜索' }}
        </button>
      </div>
    </div>

    <!-- Search results -->
    <div v-if="searchResults.length" class="card">
      <div class="card-header">
        <Search :size="15" class="card-icon" />
        搜索结果
        <span class="result-count">{{ searchResults.length }} 条</span>
        <button class="btn btn-sm btn-ghost" style="margin-left:auto" @click="searchResults = []">
          清除
        </button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>用户</th>
              <th>分类</th>
              <th>内容</th>
              <th>重要性</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in searchResults" :key="f.id">
              <td>
                <a href="#" class="user-link" @click.prevent="selectUser(f.user_id)">{{ f.user_name || f.user_id }}</a>
              </td>
              <td><span class="tag tag-blue">{{ f.category }}</span></td>
              <td class="fact-value">{{ f.fact_value }}</td>
              <td>
                <span
                  class="tag importance-tag"
                  :class="f.importance >= 0.8 ? 'tag-orange' : f.importance >= 0.5 ? 'tag-blue' : 'tag-gray'"
                >{{ f.importance?.toFixed(2) }}</span>
              </td>
              <td class="time-cell">{{ fmtTime(f.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- User list -->
    <div v-if="!selectedUser && !searchResults.length">
      <div class="card">
        <div class="card-header">
          <Users :size="16" class="card-icon" />
          有记忆的用户
          <span class="result-count">{{ totalUsers }} 人</span>
        </div>

        <div v-if="loading" class="loading">
          <div class="loading-spinner"></div>
          <span>加载中...</span>
        </div>

        <div v-else-if="!users.length" class="empty">
          <Users :size="40" class="empty-icon" />
          <span>暂无用户记忆数据</span>
        </div>

        <div v-else>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>QQ 号</th>
                  <th>名称</th>
                  <th>记忆数</th>
                  <th>最近活跃</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="u in users" :key="u.user_id">
                  <td class="user-id">{{ u.user_id }}</td>
                  <td>{{ u.user_name || '—' }}</td>
                  <td><span class="fact-count-badge">{{ u.fact_count }}</span></td>
                  <td class="time-cell">{{ fmtTime(u.last_active) }}</td>
                  <td><a href="#" class="view-link" @click.prevent="selectUser(u.user_id)">查看</a></td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Pagination -->
          <div v-if="totalPages() > 1" class="pagination">
            <button class="btn btn-sm" :disabled="page <= 1" @click="goPage(-1)">
              <ChevronLeft :size="14" />
            </button>
            <button
              v-for="p in totalPages()"
              :key="p"
              class="btn btn-sm page-btn"
              :class="{ active: p === page }"
              @click="page = p; loadUsers()"
            >{{ p }}</button>
            <button class="btn btn-sm" :disabled="page >= totalPages()" @click="goPage(1)">
              <ChevronRight :size="14" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Selected user facts -->
    <div v-if="selectedUser">
      <div class="card">
        <div class="card-header detail-header">
          <button class="btn btn-sm btn-ghost back-btn" @click="backToList">
            <ChevronLeft :size="14" />
            返回
          </button>
          <Users :size="16" class="card-icon" />
          <span class="detail-user-name">{{ selectedUser }}</span>
          <span class="result-count">{{ userFacts.length }} 条记忆</span>
        </div>

        <div v-if="factsLoading" class="loading">
          <div class="loading-spinner"></div>
          <span>加载中...</span>
        </div>

        <div v-else-if="!userFacts.length" class="empty">
          <Users :size="40" class="empty-icon" />
          <span>该用户暂无记忆</span>
        </div>

        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>分类</th>
                <th>内容</th>
                <th>重要性</th>
                <th>创建时间</th>
                <th style="width:60px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in userFacts" :key="f.fact_key">
                <td><span class="tag tag-blue">{{ f.category }}</span></td>
                <td class="fact-value">{{ f.fact_value }}</td>
                <td>
                  <div class="importance-cell">
                    <div
                      class="importance-bar"
                      :class="f.importance >= 0.8 ? 'bar-high' : f.importance >= 0.5 ? 'bar-mid' : 'bar-low'"
                      :style="{ width: (f.importance * 100) + '%' }"
                    ></div>
                    <span
                      class="tag importance-tag"
                      :class="f.importance >= 0.8 ? 'tag-orange' : f.importance >= 0.5 ? 'tag-blue' : 'tag-gray'"
                    >{{ f.importance?.toFixed(2) }}</span>
                  </div>
                </td>
                <td class="time-cell">{{ fmtTime(f.created_at) }}</td>
                <td>
                  <button
                    class="btn btn-sm btn-ghost delete-btn"
                    :disabled="deleting === f.fact_key"
                    @click="deleteFact(selectedUser!, f.fact_key)"
                    title="删除这条记忆"
                  >
                    <Trash2 :size="14" />
                    {{ deleting === f.fact_key ? '...' : '' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Page Layout ── */
.page {
  width: 100%;
}

/* ── Page Header ── */
.page-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 24px;
  padding: 24px 0 4px;
}
.page-header-icon {
  color: #6366f1;
  width: 44px;
  height: 44px;
  padding: 10px;
  background: #eef2ff;
  border-radius: 12px;
  flex-shrink: 0;
}
.page-title {
  font-size: 22px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.3;
}
.page-subtitle {
  font-size: 13px;
  color: #94a3b8;
  margin: 2px 0 0;
}

/* ── Search Card ── */
.search-card {
  padding: 16px 20px;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.search-icon {
  color: #94a3b8;
  flex-shrink: 0;
}
.search-bar .input {
  flex: 1;
}

/* ── Result Count ── */
.result-count {
  font-size: 12px;
  font-weight: 400;
  color: #94a3b8;
  margin-left: -2px;
}

/* ── Table Cells ── */
.user-id {
  font-family: ui-monospace, 'Cascadia Code', 'Fira Code', Consolas, monospace;
  font-size: 12px;
  letter-spacing: -0.2px;
}
.fact-value {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.time-cell {
  font-size: 11px;
  color: #94a3b8;
  white-space: nowrap;
}

/* ── Links ── */
.user-link {
  font-weight: 500;
}
.view-link {
  font-size: 12px;
  font-weight: 500;
}

/* ── Fact Count Badge ── */
.fact-count-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  padding: 1px 8px;
  border-radius: 9999px;
  font-size: 11px;
  font-weight: 600;
  background: #eef2ff;
  color: #4f46e5;
}

/* ── Importance ── */
.importance-tag {
  min-width: 42px;
  text-align: center;
}
.importance-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.importance-bar {
  height: 6px;
  border-radius: 3px;
  min-width: 4px;
  max-width: 60px;
  transition: width 0.2s;
}
.bar-high {
  background: linear-gradient(90deg, #f97316, #ef4444);
}
.bar-mid {
  background: linear-gradient(90deg, #6366f1, #818cf8);
}
.bar-low {
  background: #cbd5e1;
}

/* ── Detail Header ── */
.detail-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.back-btn {
  margin-right: 2px;
  padding: 4px 8px;
}
.detail-user-name {
  font-family: ui-monospace, 'Cascadia Code', 'Fira Code', Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
  color: #6366f1;
}

/* ── Delete Button ── */
.delete-btn {
  color: #94a3b8;
  padding: 4px 8px;
}
.delete-btn:hover {
  color: #ef4444;
  background: #fef2f2;
}

/* ── Pagination Page Buttons ── */
.page-btn.active {
  background: #6366f1;
  color: #fff;
  border-color: #6366f1;
}
.page-btn.active:hover {
  background: #4f46e5;
  border-color: #4f46e5;
}
</style>
