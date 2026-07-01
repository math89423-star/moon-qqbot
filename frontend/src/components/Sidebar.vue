<script setup lang="ts">
/** 侧边导航栏 — 核心页面 + 动态增强插件入口 */
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import {
  Puzzle, BookOpen,
  MessageSquare, ScanEye, ScrollText, Settings,
  Smile, Bot, Diamond,
} from '@lucide/vue'
import type { Component } from 'vue'
import type { PluginInfo } from '@/types'
import { listPlugins } from '@/api/admin'

const route = useRoute()

interface NavItem {
  path: string
  label: string
  icon: Component
}

/** 核心页面 — 始终显示 */
const coreItems: NavItem[] = [
  { path: '/bot-manage', label: 'Bot 管理',  icon: Bot },
  { path: '/bots',        label: '机器人配置', icon: Settings },
  { path: '/memories',    label: '用户记忆',  icon: Puzzle },
  { path: '/knowledge',   label: '知识库',    icon: BookOpen },
  { path: '/groups',      label: '群聊设置',  icon: MessageSquare },
  { path: '/bot-detect', label: 'Bot 检测',  icon: ScanEye },
  { path: '/summary',    label: '群聊总结',  icon: ScrollText },
]

/** 增强插件导航 — 按插件发现结果动态填充 */
const enhancedItems = ref<NavItem[]>([])

// 图标映射 (icon 名 → Lucide 组件)
const iconMap: Record<string, Component> = { smile: Smile }

onMounted(async () => {
  try {
    const plugins: PluginInfo[] = await listPlugins()
    for (const p of plugins) {
      if (p.has_page) {
        enhancedItems.value.push({
          path: p.route,
          label: p.name,
          icon: iconMap[p.icon] || Puzzle,
        })
      }
    }
  } catch {
    // 插件发现失败时静默降级——核心页面不受影响
  }
})
</script>

<template>
  <nav class="sidebar">
    <div class="sidebar-brand"><Diamond :size="14" class="brand-icon" />粟藜bot控制面板</div>
    <ul>
      <li v-for="item in coreItems" :key="item.path">
        <router-link
          :to="item.path"
          :class="{ active: route.path === item.path }"
        >
          <component :is="item.icon" :size="16" class="nav-icon" />
          <span>{{ item.label }}</span>
        </router-link>
      </li>
      <!-- 增强插件分隔线 -->
      <template v-if="enhancedItems.length">
        <li class="nav-divider"><span>增强插件</span></li>
        <li v-for="item in enhancedItems" :key="item.path">
          <router-link
            :to="item.path"
            :class="{ active: route.path === item.path }"
          >
            <component :is="item.icon" :size="16" class="nav-icon" />
            <span>{{ item.label }}</span>
          </router-link>
        </li>
      </template>
    </ul>
  </nav>
</template>

<style scoped>
.sidebar {
  width: 220px;
  background: linear-gradient(180deg, #312e81 0%, #1e1b4b 100%);
  color: #c7d2fe;
  display: flex; flex-direction: column; flex-shrink: 0;
}
.sidebar-brand {
  padding: 20px 18px; font-size: 16px; font-weight: 700;
  color: #fff; letter-spacing: 0.3px;
  border-bottom: 1px solid rgba(255,255,255,0.08);
  display: flex; align-items: center; gap: 8px;
}
.brand-icon { color: #818cf8; flex-shrink: 0; }
.sidebar ul { list-style: none; padding: 8px 0; margin: 0; overflow-y: auto; flex: 1; }
.sidebar li { margin: 0; }
.sidebar a {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 18px; color: #a5b4fc;
  text-decoration: none; font-size: 13px; transition: all 0.15s;
  border-left: 3px solid transparent; margin: 1px 0;
}
.nav-icon { flex-shrink: 0; opacity: 0.65; transition: opacity 0.15s; }
.sidebar a:hover { color: #eef2ff; background: rgba(255,255,255,0.06); }
.sidebar a:hover .nav-icon { opacity: 1; }
.sidebar a.active {
  color: #fff; background: rgba(99,102,241,0.2);
  border-left-color: #818cf8;
}
.sidebar a.active .nav-icon { opacity: 1; color: #a5b4fc; }

.nav-divider {
  padding: 14px 18px 6px;
  font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.8px;
  color: #6366f1;
  border-top: 1px solid rgba(255,255,255,0.06);
  margin-top: 6px;
}
</style>
