/** Vue Router — SPA 路由定义 */

import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      redirect: '/bots',
    },
    {
      path: '/bots',
      name: 'botConfig',
      component: () => import('@/views/BotConfig.vue'),
      meta: { title: '机器人配置' },
    },
    {
      path: '/nsfw-config',
      name: 'nsfwConfig',
      component: () => import('@/views/NsfwConfig.vue'),
      meta: { title: 'NSFW 配置' },
    },
    {
      path: '/bot-manage',
      name: 'botManage',
      component: () => import('@/views/BotManage.vue'),
      meta: { title: 'Bot 管理' },
    },
    {
      path: '/memories',
      name: 'memories',
      component: () => import('@/views/UserMemories.vue'),
      meta: { title: '用户记忆' },
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeBase.vue'),
      meta: { title: '知识库' },
    },
    {
      path: '/groups',
      name: 'groups',
      component: () => import('@/views/GroupSettings.vue'),
      meta: { title: '群聊设置' },
    },
    {
      path: '/bot-detect',
      name: 'botDetect',
      component: () => import('@/views/BotDetect.vue'),
      meta: { title: 'Bot 检测' },
    },
    {
      path: '/summary',
      name: 'summary',
      component: () => import('@/views/GroupSummary.vue'),
      meta: { title: '群聊总结' },
    },
    {
      path: '/memes',
      name: 'memes',
      component: () => import('@/views/MemeManager.vue'),
      meta: { title: '表情包管理' },
    },
    {
      path: '/draw-novelai',
      name: 'drawNovelai',
      component: () => import('@/views/DrawNovelai.vue'),
      meta: { title: '绘图 · NovelAI' },
    },
    {
      path: '/draw-pixai',
      name: 'drawPixai',
      component: () => import('@/views/DrawPixai.vue'),
      meta: { title: '绘图 · PixAI' },
    },
  ],
})

export default router
