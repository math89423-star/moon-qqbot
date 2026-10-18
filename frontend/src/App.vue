<script setup lang="ts">
/** 根组件 — 侧边导航 + 路由出口 (2026-08-25: 面板去掉 token 认证, 局域网直开) */
import { ref, onMounted, provide } from 'vue'
import { useRoute } from 'vue-router'
import { listBots } from '@/api/admin'
import Sidebar from '@/components/Sidebar.vue'
import type { BotMeta } from '@/types'

const route = useRoute()

// ── 全局 Bot 上下文 — 从 API 动态加载 ──
const FALLBACK_BOTS: BotMeta[] = [
  { id: '100000001', name: '暮恩', icon: '', color: '#4ecca3', avatar: '', character_card: 'moon', nicknames: [], is_active: true, peer_bot_ids: [], role_description: '蛇娘', rejection_style: { style_label: '', pronoun: '', tone_hint: '' }, llm_slots: [] },
]
const bots = ref<BotMeta[]>(FALLBACK_BOTS)
const currentBot = ref(localStorage.getItem('selected_bot') || '100000001')

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

onMounted(loadBots)

const pageTitle = () => {
  const meta = route.meta as { title?: string }
  return meta.title || '暮恩 管理面板'
}
</script>

<template>
  <div class="app-layout">
    <Sidebar />
    <main class="main-content">
      <header class="topbar">
        <h2>{{ pageTitle() }}</h2>
      </header>
      <div class="page-body">
        <router-view />
      </div>
    </main>
  </div>
</template>

<style scoped>
.app-layout { display: flex; height: 100vh; overflow: hidden; background: var(--bg); }
.main-content { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 28px; background: var(--surface);
  border-bottom: 1px solid var(--border); box-shadow: var(--shadow-sm);
}
.topbar h2 { margin: 0; font-size: 17px; font-weight: 600; color: var(--text); }
.page-body { flex: 1; padding: 24px 28px; overflow-y: auto; }
</style>
