<script setup lang="ts">
/** 根组件 — Token 门控 + 侧边导航 + 路由出口 */
import { ref, onMounted, provide } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getToken, login, listBots } from '@/api/admin'
import { Bot, LogOut } from '@lucide/vue'
import Sidebar from '@/components/Sidebar.vue'
import type { BotMeta } from '@/types'

const router = useRouter()
const route = useRoute()

const authed = ref(false)
const tokenInput = ref('')
const loginError = ref('')
const loggingIn = ref(false)

// ── 全局 Bot 上下文 — 从 API 动态加载 ──
const FALLBACK_BOTS: BotMeta[] = [
  { id: '3581173900', name: '暮恩', icon: '', color: '#4ecca3', avatar: '', character_card: 'moon', nicknames: [], is_active: true, peer_bot_ids: [], role_description: '蛇娘', rejection_style: { style_label: '', pronoun: '', tone_hint: '' }, llm_slots: [] },
]
const bots = ref<BotMeta[]>(FALLBACK_BOTS)
const currentBot = ref(localStorage.getItem('selected_bot') || '3581173900')

function switchBot(botId: string) {
  currentBot.value = botId
  localStorage.setItem('selected_bot', botId)
}

async function loadBots() {
  try {
    const list = await listBots()
    if (list.length > 0) {
      bots.value = list
      // 如果当前选中的 bot 不在列表中, 切换到第一个
      if (!list.find(b => b.id === currentBot.value)) {
        currentBot.value = list[0].id
      }
    }
  } catch {
    // API 失败时保持 fallback 列表
  }
}

provide('currentBot', currentBot)
provide('bots', bots)
provide('switchBot', switchBot)

onMounted(() => {
  if (getToken()) {
    authed.value = true
    loadBots()
  }
})

async function doLogin() {
  if (!tokenInput.value.trim()) return
  loggingIn.value = true
  loginError.value = ''
  try {
    const ok = await login(tokenInput.value.trim())
    if (ok) {
      authed.value = true
      router.push('/')
    } else {
      loginError.value = 'Token 无效，请检查后重试'
    }
  } catch {
    loginError.value = '连接失败，请确认 bot 已启动'
  } finally {
    loggingIn.value = false
  }
}

function doLogout() {
  localStorage.removeItem('admin_token')
  authed.value = false
  router.push('/')
}

const pageTitle = () => {
  const meta = route.meta as { title?: string }
  return meta.title || 'suli_qqbot'
}
</script>

<template>
  <div v-if="!authed" class="login-page">
    <div class="login-card">
      <h1><Bot :size="26" class="login-logo" /> suli_qqbot</h1>
      <p class="subtitle">管理面板 · 请输入 Admin Token</p>
      <form @submit.prevent="doLogin">
        <input
          v-model="tokenInput"
          type="password"
          placeholder="Admin Token"
          class="input"
          autofocus
        />
        <button type="submit" class="btn btn-primary" :disabled="loggingIn">
          {{ loggingIn ? '验证中...' : '登录' }}
        </button>
      </form>
      <p v-if="loginError" class="error">{{ loginError }}</p>
      <p class="hint">Token 在首次启动 bot 时自动生成，请查看日志</p>
    </div>
  </div>

  <div v-else class="app-layout">
    <Sidebar />
    <main class="main-content">
      <header class="topbar">
        <h2>{{ pageTitle() }}</h2>
        <button class="btn btn-sm logout-btn" @click="doLogout">
          <LogOut :size="14" />
          退出
        </button>
      </header>
      <div class="page-body">
        <router-view />
      </div>
    </main>
  </div>
</template>

<style scoped>
.login-page {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 50%, #f8fafc 100%);
}
.login-card {
  background: #fff; padding: 44px 40px; border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg); text-align: center;
  width: 380px; max-width: 90vw;
}
.login-card h1 {
  margin: 0 0 4px; font-size: 26px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  color: var(--text);
}
.login-logo { color: var(--primary); vertical-align: middle; }
.subtitle { color: var(--text-muted); margin-bottom: 24px; font-size: 14px; }
.login-card .input { width: 100%; margin-bottom: 14px; }
.error { color: var(--danger); margin-top: 10px; font-size: 13px; }
.hint { color: var(--text-muted); font-size: 12px; margin-top: 16px; }

.app-layout { display: flex; height: 100vh; overflow: hidden; background: var(--bg); }
.main-content { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 28px; background: var(--surface);
  border-bottom: 1px solid var(--border); box-shadow: var(--shadow-sm);
}
.topbar h2 { margin: 0; font-size: 17px; font-weight: 600; color: var(--text); }
.page-body { flex: 1; padding: 24px 28px; overflow-y: auto; }
.logout-btn { display: inline-flex; align-items: center; gap: 5px; }
</style>
