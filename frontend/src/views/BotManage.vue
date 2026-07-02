<script setup lang="ts">
/** Bot 管理 — Bot 身份 CRUD + 角色卡编辑器 */
import { ref, onMounted } from 'vue'
import {
  listBots, createBot, updateBot, deleteBot, uploadAvatar,
  listCharacters, getCharacter, createCharacter, updateCharacter, deleteCharacter,
} from '@/api/admin'
import type { BotMeta, BotIdentityCreate, CharacterCardMeta, CharacterCard, CharacterCardData, RejectionStyle } from '@/types'
import {
  Bot, Users, Plus, Pencil, Trash2, X, Save, FilePlus, CheckCircle, AlertCircle,
  FileText, Brain, MessageSquare, Mail, Ruler, Hand, Upload,
} from '@lucide/vue'

// ── Tab 状态 ──
type TabKey = 'bots' | 'characters'
const activeTab = ref<TabKey>('bots')

// ═════════════════════════════════════════════
// Tab 1: Bot 身份管理
// ═════════════════════════════════════════════
const bots = ref<BotMeta[]>([])
const botLoading = ref(false)
const botError = ref('')

const botModalOpen = ref(false)
const botEditing = ref<BotMeta | null>(null)
const botForm = ref({
  bot_id: '', name: '', character_card: '',
  nicknames: [] as string[],
  peer_bot_ids: '' as string,
  icon: '', color: '#666666',
  avatar: '' as string,
  role_description: '',
  rejection_style: { style_label: '', pronoun: '', tone_hint: '' } as RejectionStyle,
})
const botSaving = ref(false)
const avatarUploading = ref(false)

// ── 昵称标签输入 ──
const nicknameInput = ref('')

function addNickname() {
  const val = nicknameInput.value.replace(/[,，、\s]+/g, '').trim()
  if (!val) { nicknameInput.value = ''; return }
  if (!botForm.value.nicknames.includes(val)) {
    botForm.value.nicknames.push(val)
  }
  nicknameInput.value = ''
}

function removeNickname(index: number) {
  if (botForm.value.nicknames.length <= 1) return
  botForm.value.nicknames.splice(index, 1)
}

/** 上传头像文件 → 返回 URL */
async function handleAvatarUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  avatarUploading.value = true
  try {
    botForm.value.avatar = await uploadAvatar(file)
  } catch (err: any) {
    botError.value = err?.response?.data?.message || err.message || '头像上传失败'
  } finally {
    avatarUploading.value = false
  }
}

// 可用的角色卡列表 (用于 dropdown)
const charList = ref<CharacterCardMeta[]>([])

async function loadBots() {
  botLoading.value = true
  botError.value = ''
  try {
    bots.value = await listBots()
  } catch (e: any) {
    botError.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    botLoading.value = false
  }
}

async function loadCharList() {
  try { charList.value = await listCharacters() } catch { /* ignore */ }
}

function openCreateBot() {
  botEditing.value = null
  botForm.value = {
    bot_id: '', name: '', character_card: charList.value[0]?.name || '',
    nicknames: [],
    peer_bot_ids: '', icon: '', color: '#666666',
    avatar: '', role_description: '',
    rejection_style: { style_label: '', pronoun: '', tone_hint: '' },
  }
  nicknameInput.value = ''
  botModalOpen.value = true
}

function openEditBot(bot: BotMeta) {
  botEditing.value = bot
  botForm.value = {
    bot_id: bot.id, name: bot.name, character_card: bot.character_card,
    nicknames: bot.nicknames.length ? [...bot.nicknames] : [bot.name],
    peer_bot_ids: bot.peer_bot_ids.join(', '),
    icon: bot.icon, color: bot.color, avatar: bot.avatar || '',
    role_description: bot.role_description,
    rejection_style: bot.rejection_style ?? { style_label: '', pronoun: '', tone_hint: '' },
  }
  nicknameInput.value = ''
  botModalOpen.value = true
}

function closeBotModal() {
  botModalOpen.value = false
  botEditing.value = null
}

async function saveBot() {
  botSaving.value = true
  try {
    const data: BotIdentityCreate = {
      bot_id: botForm.value.bot_id.trim(),
      name: botForm.value.name.trim(),
      character_card: botForm.value.character_card.trim(),
      nicknames: botForm.value.nicknames.length
        ? botForm.value.nicknames
        : [botForm.value.name.trim()],  // 默认昵称 = bot 名字
      peer_bot_ids: botForm.value.peer_bot_ids.split(/[,，、]/).map(s => s.trim()).filter(Boolean),
      icon: botForm.value.icon, color: botForm.value.color,
      avatar: botForm.value.avatar || undefined,
      role_description: botForm.value.role_description.trim(),
      rejection_style: botForm.value.rejection_style,
    }
    if (!data.bot_id || !data.name || !data.character_card) {
      botError.value = 'QQ号、名称、角色卡为必填项'
      return
    }
    if (botEditing.value) {
      await updateBot(botEditing.value.id, data)
    } else {
      await createBot(data)
    }
    closeBotModal()
    await loadBots()
  } catch (e: any) {
    botError.value = e?.response?.data?.message || e.message || '保存失败'
  } finally {
    botSaving.value = false
  }
}

async function confirmDeleteBot(botId: string) {
  if (!confirm('确定删除此 Bot 身份？此操作不会删除角色卡文件。')) return
  botError.value = ''
  try {
    await deleteBot(botId)
    await loadBots()
  } catch (e: any) {
    botError.value = e?.response?.data?.message || e.message || '删除失败'
  }
}

// ═════════════════════════════════════════════
// Tab 2: 角色卡编辑器
// ═════════════════════════════════════════════
const selectedChar = ref('')
const charLoading = ref(false)
const charError = ref('')
const charSaving = ref(false)
const charSaved = ref(false)
const charData = ref<CharacterCard | null>(null)

// 分组字段的编辑缓冲 (直接修改 charData.data)
const charForm = ref<CharacterCardData | null>(null)

/** 选择角色卡后自动加载 — 无需额外点击 */
async function onCharSelect() {
  charSaved.value = false
  if (!selectedChar.value) {
    charData.value = null
    charForm.value = null
    return
  }
  await loadCharacter()
}

async function loadCharacter() {
  if (!selectedChar.value) return
  charLoading.value = true
  charError.value = ''
  charSaved.value = false
  try {
    charData.value = await getCharacter(selectedChar.value)
    charForm.value = JSON.parse(JSON.stringify(charData.value.data)) // deep copy
  } catch (e: any) {
    charError.value = e?.response?.data?.message || e.message || '加载失败'
  } finally {
    charLoading.value = false
  }
}

async function saveCharacter() {
  if (!charData.value || !charForm.value) return
  charSaving.value = true
  charSaved.value = false
  try {
    const card: CharacterCard = {
      spec: charData.value.spec,
      spec_version: charData.value.spec_version,
      data: charForm.value,
    }
    await updateCharacter(selectedChar.value, card)
    charSaved.value = true
    setTimeout(() => { charSaved.value = false }, 3000)
  } catch (e: any) {
    charError.value = e?.response?.data?.message || e.message || '保存失败'
  } finally {
    charSaving.value = false
  }
}

async function saveCharacterAs() {
  const newName = prompt('新角色卡文件名 (不含 .json):')
  if (!newName) return
  charSaving.value = true
  charSaved.value = false
  try {
    const card: CharacterCard = {
      spec: charData.value?.spec || 'chara_card_v3',
      spec_version: charData.value?.spec_version || '3.0',
      data: charForm.value!,
    }
    await createCharacter({ name: newName.trim(), card })
    selectedChar.value = newName.trim()
    await loadCharList()
    charSaved.value = true
    setTimeout(() => { charSaved.value = false }, 3000)
  } catch (e: any) {
    charError.value = e?.response?.data?.message || e.message || '另存失败'
  } finally {
    charSaving.value = false
  }
}

async function confirmDeleteChar() {
  if (!selectedChar.value) return
  if (!confirm(`确定删除角色卡 "${selectedChar.value}"？\n此操作会删除对应的 JSON 文件，且不可恢复。`)) return
  charError.value = ''
  try {
    await deleteCharacter(selectedChar.value)
    selectedChar.value = ''
    charData.value = null
    charForm.value = null
    await loadCharList()
  } catch (e: any) {
    charError.value = e?.response?.data?.message || e.message || '删除失败'
  }
}

// 辅助: tags ↔ string
function tagsToString(t: string[] | undefined): string {
  return (t || []).join(', ')
}
function stringToTags(s: string): string[] {
  return s.split(/[,，、]/).map(x => x.trim()).filter(Boolean)
}
function linesToString(a: string[] | undefined): string {
  return (a || []).join('\n')
}
function stringToLines(s: string): string[] {
  return s.split('\n').map(x => x.trim()).filter(Boolean)
}

/** 新建角色卡 — 打开 prompt 输入新文件名 */
async function createNewChar() {
  const name = prompt('新角色卡文件名 (不含 .json):')
  if (!name) return
  charError.value = ''
  // 立即清空编辑区，防止残留上一个角色卡的编辑内容
  charData.value = null
  charForm.value = null
  try {
    await createCharacter({ name: name.trim(), display_name: name.trim() })
    charList.value = await listCharacters()
    selectedChar.value = name.trim()
    // selectedChar 是编程式赋值，不会触发 <select> 的 @change → 手动加载新卡
    await loadCharacter()
  } catch (e: any) {
    charError.value = e?.response?.data?.message || e.message || '创建失败'
  }
}

// ── 生命周期 ──
onMounted(() => {
  loadBots()
  loadCharList()
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <Bot :size="22" />
      <div>
        <h2>Bot 管理</h2>
        <span class="subtitle">Bot 身份注册 & 角色卡编辑</span>
      </div>
    </div>

    <!-- Tab bar -->
    <div class="tab-bar">
      <button
        v-for="tab in [{ key: 'bots', label: 'Bot 身份' }, { key: 'characters', label: '角色卡编辑器' }]"
        :key="tab.key"
        :class="['tab-btn', { active: activeTab === tab.key }]"
        @click="activeTab = tab.key as TabKey"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- ═══════════════════════════════════════════ -->
    <!-- Tab 1: Bot 身份管理 -->
    <!-- ═══════════════════════════════════════════ -->
    <template v-if="activeTab === 'bots'">
      <div class="toolbar">
        <button class="btn btn-primary" @click="openCreateBot">
          <Plus :size="15" /> 注册新 Bot
        </button>
        <span class="toolbar-spacer" />
        <span v-if="bots.length" class="hint">{{ bots.length }} 个 Bot</span>
      </div>

      <div v-if="botError" class="error-msg">
        <AlertCircle :size="15" /> {{ botError }}
        <button class="btn btn-xs btn-ghost" @click="botError = ''">✕</button>
      </div>

      <div v-if="botLoading" class="loading">
        <div class="loading-spinner" />
        加载中...
      </div>

      <div v-else-if="!bots.length" class="empty">
        <Bot :size="36" class="empty-icon" />
        <p>暂无注册的 Bot。点击上方按钮创建第一个。</p>
      </div>

      <template v-else>
        <div class="bot-grid">
          <div v-for="bot in bots" :key="bot.id" class="bot-card card">
            <div class="bot-card-top">
              <img v-if="bot.avatar" :src="bot.avatar" class="bot-card-avatar" />
              <Bot v-else :size="32" class="bot-card-icon-fallback" :style="{ color: bot.color }" />
              <div class="bot-card-info">
                <div class="bot-card-name">{{ bot.name }}</div>
                <div class="bot-card-id">QQ: {{ bot.id }}</div>
                <div class="bot-card-meta">
                  <span class="tag tag-blue">角色卡: {{ bot.character_card }}</span>
                  <span v-if="bot.is_active" class="tag tag-green">活跃</span>
                  <span v-else class="tag tag-gray">停用</span>
                  <span v-if="bot.role_description" class="tag tag-purple">{{ bot.role_description }}</span>
                </div>
                <div v-if="bot.nicknames.length" class="bot-card-nicknames">
                  昵称: {{ bot.nicknames.join(', ') }}
                </div>
                <div v-if="bot.peer_bot_ids.length" class="bot-card-peers">
                  Peer: {{ bot.peer_bot_ids.join(', ') }}
                </div>
              </div>
            </div>
            <div class="bot-card-actions">
              <button class="btn btn-sm btn-ghost" title="编辑" @click="openEditBot(bot)">
                <Pencil :size="13" />
              </button>
              <button class="btn btn-sm btn-ghost" title="删除" @click="confirmDeleteBot(bot.id)">
                <Trash2 :size="13" />
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- Bot 编辑/创建 Modal -->
      <div v-if="botModalOpen" class="modal-overlay" @mousedown.self="closeBotModal">
        <div class="modal">
          <div class="modal-header">
            <h3>{{ botEditing ? '编辑 Bot' : '注册新 Bot' }}</h3>
            <button class="btn-close" @click="closeBotModal"><X :size="18" /></button>
          </div>
          <div class="modal-body">
            <div class="form-grid">
              <div class="form-field">
                <label>QQ 号 <span class="req">*</span></label>
                <input v-model="botForm.bot_id" class="input" placeholder="3581173900" :disabled="!!botEditing" />
              </div>
              <div class="form-field">
                <label>名称 <span class="req">*</span></label>
                <input v-model="botForm.name" class="input" placeholder="洛普特" />
              </div>
              <div class="form-field">
                <label>角色卡 <span class="req">*</span></label>
                <select v-model="botForm.character_card" class="input">
                  <option v-for="c in charList" :key="c.name" :value="c.name">{{ c.display_name }} ({{ c.name }})</option>
                </select>
              </div>
              <div class="form-field">
                <label>角色描述</label>
                <input v-model="botForm.role_description" class="input" placeholder="蛇系 / 猫娘 / ..." />
              </div>
              <div class="form-field" style="grid-column: 1 / -1">
                <label>工具拒绝文案风格 <span class="hint">留空则通过角色描述自动匹配</span></label>
                <div style="display:flex; gap:12px; flex-wrap:wrap;">
                  <div style="flex:1; min-width:130px;">
                    <span class="field-sublabel">风格标签</span>
                    <input v-model="botForm.rejection_style.style_label" class="input" placeholder="知性沉稳语气" />
                  </div>
                  <div style="flex:1; min-width:80px;">
                    <span class="field-sublabel">自称</span>
                    <input v-model="botForm.rejection_style.pronoun" class="input" placeholder="我 / 人家" />
                  </div>
                  <div style="flex:1; min-width:80px;">
                    <span class="field-sublabel">语气</span>
                    <input v-model="botForm.rejection_style.tone_hint" class="input" placeholder="沉稳 / 俏皮" />
                  </div>
                </div>
              </div>
              <div class="form-field" style="grid-column: 1 / -1">
                <label>昵称</label>
                <div class="tag-input-wrap" @click="($refs.nickInput as HTMLInputElement)?.focus()">
                  <span v-for="(tag, i) in botForm.nicknames" :key="i" class="tag-chip">
                    {{ tag }}
                    <button
                      v-if="botForm.nicknames.length > 1"
                      class="tag-chip-x"
                      @click.stop="removeNickname(i)"
                      title="移除"
                    >×</button>
                  </span>
                  <input
                    ref="nickInput"
                    v-model="nicknameInput"
                    class="tag-input-field"
                    placeholder="输入后按回车添加..."
                    @keydown.enter.prevent="addNickname"
                    @keydown.,.prevent="addNickname"
                  />
                </div>
              </div>
              <div class="form-field">
                <label>主题色</label>
                <div style="display:flex; align-items:center; gap:8px;">
                  <input v-model="botForm.color" class="input input-sm" placeholder="#4ecca3" style="flex:1" />
                  <span :style="{ width:'28px', height:'28px', borderRadius:'6px', background: botForm.color, border:'1px solid var(--border)', flexShrink:0 }" />
                </div>
              </div>
              <div class="form-field" style="grid-column: 1 / -1">
                <label>Peer Bot QQ (逗号分隔)</label>
                <input v-model="botForm.peer_bot_ids" class="input" placeholder="3969478803" />
              </div>
              <div class="form-field" style="grid-column: 1 / -1">
                <label>头像 (上传图片或填写 URL)</label>
                <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                  <div class="avatar-preview">
                    <img v-if="botForm.avatar" :src="botForm.avatar" class="avatar-img" />
                    <Bot v-else :size="24" class="avatar-placeholder-icon" />
                  </div>
                  <div style="flex:1; min-width:200px;">
                    <input :value="botForm.avatar" @input="e => botForm.avatar = (e.target as HTMLInputElement).value" class="input" placeholder="/avatars/xxx.png 或 https://..." style="margin-bottom:6px" />
                    <label class="btn btn-sm" style="cursor:pointer; display:inline-flex; align-items:center; gap:4px;">
                      <Upload :size="14" /> {{ avatarUploading ? '上传中...' : '选择文件上传' }}
                      <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none" @change="handleAvatarUpload" :disabled="avatarUploading" />
                    </label>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn" @click="closeBotModal">取消</button>
            <button class="btn btn-primary" :disabled="botSaving" @click="saveBot">
              {{ botSaving ? '保存中...' : (botEditing ? '更新' : '创建') }}
            </button>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══════════════════════════════════════════ -->
    <!-- Tab 2: 角色卡编辑器 -->
    <!-- ═══════════════════════════════════════════ -->
    <template v-if="activeTab === 'characters'">
      <!-- 选择器 + 顶部操作栏 -->
      <div class="card">
        <div class="card-header"><Users :size="16" class="card-icon" /> 选择角色卡</div>
        <div class="toolbar">
          <select v-model="selectedChar" class="input" style="max-width:280px" @change="onCharSelect">
            <option value="">-- 选择角色卡 --</option>
            <option v-for="c in charList" :key="c.name" :value="c.name">{{ c.display_name }} ({{ c.name }}.json)</option>
          </select>
          <button class="btn" @click="createNewChar">
            <FilePlus :size="14" /> 新建
          </button>
          <span class="toolbar-spacer" />
          <!-- 顶部保存按钮 — 编辑时始终可见 -->
          <template v-if="charForm">
            <button class="btn btn-primary" :disabled="charSaving" @click="saveCharacter">
              <Save :size="14" /> {{ charSaving ? '保存中...' : '保存' }}
            </button>
            <button class="btn" :disabled="charSaving" @click="saveCharacterAs">
              <FilePlus :size="14" /> 另存为...
            </button>
            <span v-if="charSaved" class="success-msg" style="margin:0; padding:4px 10px; font-size:12px;">
              <CheckCircle :size="13" /> 已保存
            </span>
          </template>
        </div>
      </div>

      <div v-if="charError" class="error-msg">
        <AlertCircle :size="15" /> {{ charError }}
        <button class="btn btn-xs btn-ghost" @click="charError = ''">✕</button>
      </div>

      <div v-if="charLoading" class="loading">
        <div class="loading-spinner" />
        加载角色卡...
      </div>

      <div v-if="selectedChar && !charForm && !charLoading" class="empty">
        <Users :size="36" class="empty-icon" />
        <p>从上方下拉菜单选择角色卡即可编辑</p>
      </div>

      <!-- 编辑器 (分组卡片, 仅在加载后显示) -->
      <template v-if="charForm">
        <!-- §1 基本信息 -->
        <div class="card">
          <div class="card-header char-section-header"><FileText :size="18" class="card-icon" /> 基本信息</div>
          <div class="form-grid">
            <div class="form-field">
              <label>名称</label>
              <input v-model="charForm.name" class="input" />
            </div>
            <div class="form-field">
              <label>角色描述 (role_description)</label>
              <input v-model="charForm.role_description" class="input" placeholder="蛇系 / 猫娘" />
            </div>
            <div class="form-field">
              <label>版本</label>
              <input v-model="charForm.character_version" class="input" />
            </div>
            <div class="form-field">
              <label>话量 (0-1)</label>
              <input v-model="charForm.talkativeness" class="input" placeholder="0.5" />
            </div>
            <div class="form-field" style="grid-column: 1 / -1">
              <label>描述 (description)</label>
              <textarea v-model="charForm.description" class="input" rows="3" />
            </div>
            <div class="form-field" style="grid-column: 1 / -1">
              <label>性格 (personality)</label>
              <textarea v-model="charForm.personality" class="input" rows="3" />
            </div>
            <div class="form-field" style="grid-column: 1 / -1">
              <label>场景 (scenario)</label>
              <textarea v-model="charForm.scenario" class="input" rows="2" />
            </div>
            <div class="form-field" style="grid-column: 1 / -1">
              <label>Tags (逗号分隔)</label>
              <input :value="tagsToString(charForm.tags)" @input="e => charForm!.tags = stringToTags((e.target as HTMLInputElement).value)" class="input" />
            </div>
            <div class="form-field" style="grid-column: 1 / -1">
              <label>作者备注 (creator_notes)</label>
              <input v-model="charForm.creator_notes" class="input" />
            </div>
          </div>
        </div>

        <!-- §2 核心人格 -->
        <div class="card">
          <div class="card-header char-section-header"><Brain :size="18" class="card-icon" /> 核心人格 (group_persona)<span class="hint" style="margin-left:auto">{{ charForm.group_persona?.length || 0 }} 字</span></div>
          <textarea v-model="charForm.group_persona" class="input" style="min-height:300px; font-size:12px" />
        </div>

        <!-- §3 Few-shot 示例 -->
        <div class="card">
          <div class="card-header char-section-header"><MessageSquare :size="18" class="card-icon" /> Few-shot 示例 (group_mes_example)<span class="hint" style="margin-left:auto">{{ charForm.group_mes_example?.length || 0 }} 字</span></div>
          <textarea v-model="charForm.group_mes_example" class="input" style="min-height:200px; font-size:12px" />
        </div>

        <!-- §4 输出自检 -->
        <div class="card">
          <div class="card-header char-section-header"><CheckCircle :size="18" class="card-icon" /> 输出自检 (post_history_instructions)</div>
          <textarea v-model="charForm.post_history_instructions" class="input" rows="6" />
        </div>

        <!-- §5 私聊系统 -->
        <div class="card">
          <div class="card-header char-section-header"><Mail :size="18" class="card-icon" /> 私聊系统 prompt</div>
          <div class="form-field">
            <label>system_prompt (私聊)</label>
            <textarea v-model="charForm.system_prompt" class="input" style="min-height:200px; font-size:12px" />
          </div>
          <div class="form-field">
            <label>first_mes (首发消息)</label>
            <textarea v-model="charForm.first_mes" class="input" rows="3" />
          </div>
          <div class="form-field">
            <label>mes_example (私聊 few-shot)</label>
            <textarea v-model="charForm.mes_example" class="input" style="min-height:150px; font-size:12px" />
          </div>
        </div>

        <!-- §6 行为规则 -->
        <div class="card">
          <div class="card-header char-section-header"><Ruler :size="18" class="card-icon" /> 行为规则</div>
          <div class="form-field">
            <label>颜文字规则 (kaomoji_rule)</label>
            <textarea v-model="charForm.kaomoji_rule" class="input" rows="3" />
          </div>
          <div class="form-field">
            <label>表情包指南 (sticker_guide)</label>
            <textarea v-model="charForm.sticker_guide" class="input" rows="4" />
          </div>
          <div class="form-field">
            <label>同伴规则 (companion_rules)</label>
            <textarea v-model="charForm.companion_rules" class="input" rows="4" />
          </div>
        </div>

        <!-- §7 开场白 -->
        <div class="card">
          <div class="card-header char-section-header"><Hand :size="18" class="card-icon" /> 开场白</div>
          <div class="form-field">
            <label>通用开场白 (alternate_greetings, 每行一条)</label>
            <textarea :value="linesToString(charForm.alternate_greetings)" @input="e => charForm!.alternate_greetings = stringToLines((e.target as HTMLTextAreaElement).value)" class="input" rows="4" />
          </div>
          <div class="form-field">
            <label>群聊专用开场白 (group_only_greetings, 每行一条)</label>
            <textarea :value="linesToString(charForm.group_only_greetings)" @input="e => charForm!.group_only_greetings = stringToLines((e.target as HTMLTextAreaElement).value)" class="input" rows="4" />
          </div>
        </div>

        <!-- 操作栏 -->
        <div class="toolbar sticky-bottom">
          <button class="btn btn-primary" :disabled="charSaving" @click="saveCharacter">
            <Save :size="14" /> {{ charSaving ? '保存中...' : '保存' }}
          </button>
          <button class="btn" :disabled="charSaving" @click="saveCharacterAs">
            <FilePlus :size="14" /> 另存为新卡...
          </button>
          <button class="btn btn-danger-outline" :disabled="charSaving" @click="confirmDeleteChar">
            <Trash2 :size="14" /> 删除此角色卡
          </button>
          <span class="toolbar-spacer" />
          <span v-if="charSaved" class="success-msg" style="margin:0; padding:6px 12px">
            <CheckCircle :size="14" /> 已保存
          </span>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page { padding: 0; max-width: none; }
.page-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 18px;
}
.page-header h2 { font-size: 20px; margin: 0; }
.subtitle { font-size: 12px; color: var(--text-muted); }
.hint { font-size: 11px; color: var(--text-muted); margin-left: auto; font-weight: 400; }
.field-sublabel { font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px; }
.req { color: var(--danger); }

/* ── Bot 卡片网格 ── */
.bot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.bot-card {
  display: flex; flex-direction: column; justify-content: space-between;
  padding: 16px 20px;
}
.bot-card-top { display: flex; gap: 14px; align-items: flex-start; }
.bot-card-icon-fallback { flex-shrink: 0; opacity: 0.85; }
.bot-card-avatar {
  width: 48px; height: 48px; border-radius: 50%; object-fit: cover;
  flex-shrink: 0; border: 2px solid var(--border);
}
.bot-card-info { flex: 1; min-width: 0; }
.bot-card-name { font-size: 15px; font-weight: 600; color: var(--text); }
.bot-card-id { font-size: 11px; color: var(--text-muted); font-family: var(--mono); margin-top: 1px; }
.bot-card-meta { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.bot-card-nicknames, .bot-card-peers { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }
.bot-card-actions { display: flex; gap: 4px; justify-content: flex-end; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border-light); }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.35);
  display: flex; align-items: center; justify-content: center;
  z-index: 200; backdrop-filter: blur(3px);
}
.modal {
  background: var(--surface); padding: 0; border-radius: var(--radius-lg);
  width: 560px; max-width: 92vw; box-shadow: var(--shadow-lg); overflow: hidden;
}
.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 24px; border-bottom: 1px solid var(--border-light);
}
.modal-header h3 { margin: 0; font-size: 16px; }
.modal-body { padding: 20px 24px; max-height: 60vh; overflow-y: auto; }
.modal-footer {
  display: flex; gap: 10px; justify-content: flex-end;
  padding: 14px 24px; border-top: 1px solid var(--border-light);
}
.btn-close {
  background: none; border: none; cursor: pointer; color: var(--text-muted);
  padding: 4px; border-radius: var(--radius-sm); display: flex;
}

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-field { display: flex; flex-direction: column; gap: 5px; }
.form-field label { font-size: 12px; font-weight: 500; color: var(--text-secondary); }

/* ── Toolbar ── */
.toolbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 16px;
}
.toolbar-spacer { flex: 1; }
.sticky-bottom {
  position: sticky; bottom: 0; background: var(--bg);
  padding: 14px 0; margin-top: 16px;
  border-top: 1px solid var(--border-light); z-index: 10;
}

.btn-danger-outline { color: var(--danger); border-color: #fecaca; }
.btn-danger-outline:hover:not(:disabled) { background: var(--danger); color: #fff; border-color: var(--danger); }

/* ── Avatar ── */
.avatar-preview {
  width: 56px; height: 56px; border-radius: 50%; overflow: hidden;
  border: 2px solid var(--border); flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg);
}
.avatar-img { width: 100%; height: 100%; object-fit: cover; }
.avatar-placeholder-icon { opacity: 0.6; }

/* ── Character editor section headers — bolder & distinct ── */
.char-section-header {
  font-size: 15px !important;
  font-weight: 700 !important;
  color: #4338ca !important;
  padding-bottom: 10px !important;
  margin-bottom: 18px !important;
  border-bottom: 2px solid #e0e7ff !important;
  letter-spacing: 0.2px;
}
.char-section-header :deep(.card-icon) {
  color: #6366f1;
  opacity: 1;
}

/* ── Tag input ── */
.tag-input-wrap {
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
  padding: 6px 10px; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius); min-height: 38px; cursor: text;
  transition: border-color 0.15s;
}
.tag-input-wrap:focus-within { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(99,102,241,0.15); }
.tag-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; background: #e0e7ff; color: #3730a3;
  border-radius: 20px; font-size: 12px; font-weight: 500;
  line-height: 1.6; user-select: none;
}
.tag-chip-x {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%; border: none;
  background: transparent; color: #6366f1; cursor: pointer;
  font-size: 13px; line-height: 1; padding: 0; transition: background 0.1s;
}
.tag-chip-x:hover { background: #c7d2fe; color: #312e81; }
.tag-input-field {
  flex: 1; min-width: 120px; border: none; outline: none;
  background: transparent; font-size: 13px; padding: 2px 0;
  color: var(--text); font-family: inherit;
}
.tag-input-field::placeholder { color: var(--text-muted); }

/* ── Card header icons ── */
.card-icon { flex-shrink: 0; opacity: 0.7; margin-right: 2px; }

/* ── Character editor fields — wider defaults ── */
.card :deep(textarea.input),
.card :deep(.input[style*="min-height"]) {
  min-width: 100%;
  font-family: var(--mono);
  line-height: 1.6;
}
</style>
