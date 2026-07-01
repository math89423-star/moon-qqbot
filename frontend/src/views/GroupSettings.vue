<script setup lang="ts">
/** 群聊设置 — per-bot 白名单管理 + 双层对话等级控制 */
import { ref, inject, onMounted, watch, type Ref } from 'vue'
import { getWhitelist, addWhitelist, updateWhitelist, removeWhitelist } from '@/api/admin'
import type { WhitelistEntry, BotMeta } from '@/types'
import { MessageSquare, Plus, Trash2, Bot } from '@lucide/vue'

const currentBot = inject<Ref<string>>('currentBot')!
const bots = inject<Ref<BotMeta[]>>('bots')!
const switchBot = inject<(id: string) => void>('switchBot')!

const groups = ref<WhitelistEntry[]>([])
const loading = ref(true)
const error = ref('')
const newGroupId = ref('')
const newTier = ref<'basic' | 'full'>('basic')
const adding = ref(false)
const removing = ref<number | null>(null)
const updatingTier = ref<number | null>(null)

const currentBotInfo = () => bots.value.find(b => b.id === currentBot.value)

async function load() {
  loading.value = true
  error.value = ''
  try {
    groups.value = await getWhitelist(currentBot.value)
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function add() {
  const gid = parseInt(newGroupId.value.trim())
  if (!gid || isNaN(gid)) return
  adding.value = true
  error.value = ''
  try {
    await addWhitelist(gid, newTier.value, currentBot.value)
    await load()
    newGroupId.value = ''
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '添加失败'
  } finally {
    adding.value = false
  }
}

async function toggleTier(entry: WhitelistEntry) {
  const nextTier = entry.tier === 'basic' ? 'full' : 'basic'
  updatingTier.value = entry.group_id
  error.value = ''
  try {
    await updateWhitelist(entry.group_id, nextTier, currentBot.value)
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '切换失败'
  } finally {
    updatingTier.value = null
  }
}

async function remove(gid: number) {
  if (!confirm(`确定从 ${currentBotInfo()?.name || '当前 bot'} 的白名单中移除群 ${gid}？`)) return
  removing.value = gid
  error.value = ''
  try {
    await removeWhitelist(gid, currentBot.value)
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.message || e.message || '移除失败'
  } finally {
    removing.value = null
  }
}

// 切换 bot 时重新加载
watch(currentBot, () => { load() })

onMounted(load)
</script>

<template>
  <div class="page">
    <!-- Page Header -->
    <div class="page-header">
      <MessageSquare :size="24" class="page-header-icon" />
      <div>
        <h2 class="page-title">群聊设置</h2>
        <p class="page-subtitle">管理群聊白名单和对话等级</p>
      </div>
    </div>

    <!-- Error -->
    <div v-if="error" class="error-msg">{{ error }}</div>

    <div class="card">
      <!-- Bot Switcher -->
      <div class="bot-selector">
        <Bot :size="14" class="bot-selector-icon" />
        <template v-for="b in bots" :key="b.id">
          <button
            class="bot-chip"
            :class="{ active: currentBot === b.id }"
            :style="{ '--bot-color': b.color }"
            @click="switchBot(b.id)"
          >
            {{ b.icon }} {{ b.name }}
          </button>
        </template>
      </div>

      <!-- Level Description -->
      <div class="desc-block">
        <span class="tag tag-gray">仅对话</span>
        <span>只响应 @提及、昵称呼叫和回复消息</span>
        <br/>
        <span class="tag tag-green">自然对话</span>
        <span>包含所有触发方式（静默/批量/主动发言）</span>
        <br/>
        <span class="hint">点击等级标签可切换</span>
      </div>

      <!-- Add Form -->
      <div class="add-form">
        <input
          v-model="newGroupId"
          type="text"
          class="input group-id-input"
          placeholder="QQ 群号"
          @keyup.enter="add"
        />
        <select v-model="newTier" class="input tier-select">
          <option value="basic">允许对话</option>
          <option value="full">允许自然对话</option>
        </select>
        <button
          class="btn btn-primary"
          :disabled="adding || !newGroupId.trim()"
          @click="add"
        >
          <Plus :size="14" /> 添加
        </button>
      </div>
    </div>

    <!-- Whitelist Table -->
    <div class="card">
      <div class="card-header">
        <MessageSquare :size="16" class="card-icon" />
        已授权群聊
        <span v-if="groups.length" class="count-badge">{{ groups.length }}</span>
      </div>

      <div v-if="loading" class="loading">
        <div class="loading-spinner"></div>
        <span>加载中...</span>
      </div>

      <div v-else-if="!groups.length" class="empty">
        <MessageSquare :size="40" class="empty-icon" />
        <span>白名单为空，请添加群号</span>
      </div>

      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>群号</th>
              <th>对话等级</th>
              <th class="op-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in groups" :key="entry.group_id">
              <td><span class="group-id">{{ entry.group_id }}</span></td>
              <td>
                <button
                  class="tier-pill"
                  :class="entry.tier === 'basic' ? 'tag-gray' : 'tag-green'"
                  :disabled="updatingTier === entry.group_id"
                  @click="toggleTier(entry)"
                >
                  {{ entry.tier === 'basic' ? '仅对话' : '自然对话' }}
                </button>
              </td>
              <td class="op-col">
                <button
                  class="btn-remove"
                  :disabled="removing === entry.group_id"
                  @click="remove(entry.group_id)"
                  title="移除"
                >
                  <Trash2 :size="14" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
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

/* ── Bot Selector ── */
.bot-selector {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 10px 14px;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px solid #f1f5f9;
}
.bot-selector-icon {
  color: #94a3b8;
  flex-shrink: 0;
}
.bot-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 14px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
  border: 1.5px solid transparent;
  cursor: pointer;
  background: transparent;
  color: #64748b;
  transition: all 0.15s;
}
.bot-chip:hover {
  border-color: var(--bot-color);
  color: #334155;
  background: #f1f5f9;
}
.bot-chip.active {
  background: var(--bot-color);
  color: #fff;
  border-color: var(--bot-color);
}

/* ── Description Block ── */
.desc-block {
  font-size: 13px;
  color: #64748b;
  line-height: 1.8;
  margin-bottom: 16px;
  padding: 12px 14px;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px solid #f1f5f9;
}
.desc-block .tag {
  margin-right: 6px;
}
.hint {
  font-size: 12px;
  color: #94a3b8;
}

/* ── Add Form ── */
.add-form {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.group-id-input {
  width: 180px;
  font-family: ui-monospace, 'Cascadia Code', 'Fira Code', Consolas, monospace;
  font-size: 13px;
}
.tier-select {
  width: 160px;
}

/* ── Count Badge ── */
.count-badge {
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
  margin-left: -4px;
}

/* ── Group ID Cell ── */
.group-id {
  font-family: ui-monospace, 'Cascadia Code', 'Fira Code', Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
  color: #1e293b;
  letter-spacing: -0.3px;
}

/* ── Tier Pill ── */
.tier-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 12px;
  border-radius: 9999px;
  font-size: 12px;
  font-weight: 600;
  border: none;
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
  line-height: 1.5;
}
.tier-pill:disabled {
  opacity: 0.6;
  cursor: wait;
}
.tier-pill.tag-gray {
  background: #f1f5f9;
  color: #475569;
}
.tier-pill.tag-gray:hover {
  background: #e2e8f0;
}
.tier-pill.tag-green {
  background: #ecfdf5;
  color: #065f46;
}
.tier-pill.tag-green:hover {
  background: #d1fae5;
}

/* ── Operations Column ── */
.op-col {
  width: 52px;
  text-align: center;
}

/* ── Remove Button ── */
.btn-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-remove:hover {
  color: #ef4444;
  background: #fef2f2;
}
.btn-remove:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
