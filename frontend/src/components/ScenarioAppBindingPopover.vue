<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Plus, Search } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'

const props = defineProps<{
  apps: TaskGenerationTreeNode[]
  selectedApps: string[]
  disabled?: boolean
}>()

const emit = defineEmits<{
  open: []
  confirm: [apps: string[]]
}>()

const visible = ref(false)
const searchText = ref('')
const selected = ref<string[]>([])
function sameAppName(left: string, right: string) {
  return left.trim().toLowerCase() === right.trim().toLowerCase()
}
function appExists(label: string) {
  return props.apps.some(app => sameAppName(app.label, label))
}
const filteredApps = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return props.apps.filter(app => !keyword || app.label.toLowerCase().includes(keyword))
})
const createCandidate = computed(() => {
  const label = searchText.value.trim()
  return label && !appExists(label) ? label : ''
})
const pendingNewApps = computed(() => selected.value.filter(label => !appExists(label)))
const canConfirm = computed(() => selected.value.length > 0 || props.selectedApps.length > 0)

watch(visible, value => {
  if (value) {
    selected.value = [...props.selectedApps]
    searchText.value = ''
  }
})

function toggle(app: string) {
  selected.value = selected.value.includes(app)
    ? selected.value.filter(item => item !== app)
    : [...selected.value, app]
}

function addNewApp() {
  const label = createCandidate.value
  if (!label || selected.value.some(item => sameAppName(item, label))) return
  selected.value = [...selected.value, label]
  searchText.value = ''
}

function removePendingApp(label: string) {
  selected.value = selected.value.filter(item => item !== label)
}

function confirm() {
  emit('confirm', [...selected.value])
  visible.value = false
}
</script>

<template>
  <el-popover v-model:visible="visible" placement="top-start" :width="320" trigger="click">
    <template #reference>
      <button type="button" class="bind-trigger" :disabled="props.disabled" @click="emit('open')">
        <el-icon><Plus /></el-icon>新增 App
      </button>
    </template>
    <div class="binding-popover">
      <div class="binding-title"><strong>新增或绑定 App</strong><span>{{ selected.length }} 已选</span></div>
      <el-input v-model="searchText" size="small" clearable :prefix-icon="Search" maxlength="200" placeholder="搜索已有 App，或输入新 App 名称" />
      <button v-if="createCandidate" type="button" class="binding-create" @click="addNewApp">
        <el-icon><Plus /></el-icon><span>新增“{{ createCandidate }}”</span>
      </button>
      <div v-if="pendingNewApps.length" class="pending-list">
        <span class="binding-section-label">待新增</span>
        <span v-for="app in pendingNewApps" :key="app" class="pending-chip">
          {{ app }}
          <button type="button" aria-label="移除待新增 App" @click="removePendingApp(app)">×</button>
        </span>
      </div>
      <div v-if="filteredApps.length" class="binding-list">
        <button v-for="app in filteredApps" :key="app.id" type="button" class="binding-option" :class="{ selected: selected.includes(app.label) }" @click="toggle(app.label)">
          <span class="app-symbol">○</span><span>{{ app.label }}</span><el-icon v-if="selected.includes(app.label)"><Check /></el-icon>
        </button>
      </div>
      <div v-else-if="!createCandidate" class="binding-empty">暂无匹配的已有 App</div>
      <div class="binding-foot"><span>可绑定已有 App，也可以新增 App</span><el-button size="small" type="primary" :disabled="!canConfirm" @click="confirm">保存 {{ selected.length }} 个 App</el-button></div>
    </div>
  </el-popover>
</template>

<style scoped>
.bind-trigger{display:inline-flex;align-items:center;gap:6px;padding:6px 7px;border:0;border-radius:6px;background:transparent;color:var(--accent-deep);font-size:12px;cursor:pointer}.bind-trigger:hover:not(:disabled){background:#f0fdfa}.bind-trigger:disabled{color:#cbd5e1;cursor:not-allowed}.binding-popover{display:grid;gap:10px}.binding-title,.binding-foot{display:flex;align-items:center;justify-content:space-between;gap:8px}.binding-title strong{color:#334155;font-size:13px}.binding-title span,.binding-foot span{color:#94a3b8;font-size:10px}.binding-create{display:flex;align-items:center;gap:7px;padding:8px 7px;border:1px dashed #99d9d1;border-radius:6px;background:#f8fffd;color:var(--accent-deep);font-size:12px;text-align:left;cursor:pointer}.binding-create:hover{border-color:var(--accent);background:#f0fdfa}.pending-list{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.binding-section-label{color:#94a3b8;font-size:10px}.pending-chip{display:inline-flex;align-items:center;gap:4px;padding:3px 6px;border:1px solid #99d9d1;border-radius:999px;background:#f0fdfa;color:#0f766e;font-size:11px}.pending-chip button{padding:0;border:0;background:transparent;color:#0f766e;font-size:14px;line-height:1;cursor:pointer}.binding-list{display:grid;max-height:220px;overflow:auto;margin:0 -5px;padding:2px 5px}.binding-option{display:flex;align-items:center;gap:8px;padding:8px 7px;border:0;border-radius:6px;background:transparent;color:#475569;font-size:12px;text-align:left;cursor:pointer}.binding-option:hover{background:#f8fafc}.binding-option.selected{background:#f0fdfa;color:var(--accent-deep)}.binding-option .app-symbol{color:#94a3b8}.binding-option .el-icon:last-child{margin-left:auto}.binding-empty{padding:22px 0;color:#94a3b8;font-size:11px;text-align:center}.binding-foot{padding-top:9px;border-top:1px solid var(--line)}
</style>
