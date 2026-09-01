<template>
  <div class="phone-factory-page">
    <header class="page-hero">
      <div>
        <span class="eyebrow">PHONE FACTORY COLLECTION</span>
        <h1>手机工厂采集</h1>
        <p>管理采集手机与运行 APP，配置 VLA 接口，上传轨迹生产任务文件。</p>
      </div>
    </header>

    <div class="factory-layout">
      <!-- ============ 左列：新增手机（含手机列表） ============ -->
      <section class="panel">
        <div class="panel-head">
          <h2>新增手机</h2>
          <span class="panel-tip">提示：规划的 APP 需要提前安装并且登录，避免功能受限</span>
        </div>
        <div class="row-form">
          <span class="field-label">手机ID</span>
          <el-input
            v-model="newPhoneId"
            placeholder="示例：3B65AB01LBl00000"
            clearable
            class="field-input"
            @keyup.enter="handleAddPhoneApp"
          />
          <span class="field-label app-gap">运行APP</span>
          <el-select
            v-model="newApp"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="选择或输入运行APP"
            class="field-input"
            @keyup.enter="handleAddPhoneApp"
          >
            <el-option v-for="app in apps" :key="app" :label="app" :value="app" />
          </el-select>
          <el-button type="primary" :loading="saving" @click="handleAddPhoneApp">新增</el-button>
        </div>

        <h3 class="sub-title">手机列表</h3>
        <el-table :data="sortedPhoneApps" border stripe>
          <el-table-column prop="phone_id" label="手机ID" min-width="200" show-overflow-tooltip />
          <el-table-column prop="app" label="运行APP" min-width="160" show-overflow-tooltip />
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }">
              <el-tag :type="row.status === '运行中' ? 'success' : 'info'" effect="light">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120" align="center">
            <template #default="{ row }">
              <el-button link type="danger" :loading="deletingKey === rowKey(row)" @click="handleRemovePhoneApp(row)">
                删除手机
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="!phoneApps.length" class="empty-hint">暂无手机关联，请在上方新增。</p>
      </section>

      <!-- ============ 右列：轨迹生产（含任务列表） ============ -->
      <section class="panel traj-panel">
        <div class="panel-head">
          <h2>轨迹生产</h2>
          <span class="panel-tip">上传目录：/root/uuupppfffiiillleee</span>
        </div>

        <!-- 3.1 VLA接口 -->
        <div class="row-form">
          <span class="field-label">VLA接口</span>
          <el-select
            v-model="newVla"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="ip:port"
            class="field-input"
            @keyup.enter="handleSaveVla"
          >
            <el-option v-for="item in vla" :key="item" :label="item" :value="item" />
          </el-select>
          <el-button :loading="savingVla" @click="handleSaveVla">保存</el-button>
        </div>

        <!-- 3.1.1 是否打开采样 + 是否使用经验库 -->
        <div class="row-form config-row">
          <span class="field-label">是否打开采样</span>
          <el-select v-model="config.sampling_enabled" class="yesno-input" @change="handleConfigChange">
            <el-option :value="true" label="是" />
            <el-option :value="false" label="否" />
          </el-select>
          <template v-if="config.sampling_enabled">
            <span class="field-label sub-label">temperature</span>
            <el-input-number v-model="config.temperature" :min="0" :max="2" :step="0.05" :precision="2" :controls="false" class="num-input" @change="handleConfigChange" />
            <span class="field-label sub-label">top_p</span>
            <el-input-number v-model="config.top_p" :min="0" :max="1" :step="0.05" :precision="2" :controls="false" class="num-input" @change="handleConfigChange" />
          </template>
        </div>
        <div class="row-form config-row">
          <span class="field-label">是否使用经验库</span>
          <el-select v-model="config.use_experience_lib" class="yesno-input" @change="handleConfigChange">
            <el-option :value="true" label="是" />
            <el-option :value="false" label="否" />
          </el-select>
        </div>

        <!-- 3.2 新增任务 -->
        <div class="row-form">
          <span class="field-label">新增任务</span>
          <el-input v-model="newTaskDesc" placeholder="任务描述" clearable class="field-input" @keyup.enter="handleAddTask" />
          <input ref="fileInput" type="file" hidden @change="onFileChange" />
          <el-button @click="fileInput?.click()">选择文件</el-button>
          <span class="file-name" :class="{ 'is-empty': !selectedFile }">{{ selectedFile ? selectedFile.name : '未选择文件' }}</span>
          <el-button type="primary" :loading="savingTask" @click="handleAddTask">新增</el-button>
        </div>

        <!-- 3.3 任务列表 -->
        <h3 class="sub-title">任务列表</h3>
        <el-table :data="tasks" border stripe>
          <el-table-column prop="description" label="任务描述" min-width="200" show-overflow-tooltip />
          <el-table-column prop="filename" label="文件名" min-width="180" show-overflow-tooltip />
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }">
              <el-tag type="warning" effect="light">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="190" align="center">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                :loading="startingTask === row.filename"
                :disabled="row.status === '运行中'"
                @click="handleStartTask(row)"
              >
                {{ row.status === '运行中' ? '运行中' : '开始运行' }}
              </el-button>
              <el-button link type="danger" :loading="deletingTask === row.filename" @click="handleRemoveTask(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="!tasks.length" class="empty-hint">暂无任务，请在上方新增。</p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { phoneFactoryApi, type FactoryConfig, type PhoneAppRow, type TaskRow } from '@/phoneFactoryApi'

// ---------- 状态 ----------
const apps = ref<string[]>([])
const phoneApps = ref<PhoneAppRow[]>([])
const vla = ref<string[]>([])
const tasks = ref<TaskRow[]>([])

const config = reactive<FactoryConfig>({
  sampling_enabled: false,
  temperature: 0.7,
  top_p: 0.85,
  use_experience_lib: false,
})

const newPhoneId = ref('')
const newApp = ref('')
const newVla = ref('')
const newTaskDesc = ref('')
const selectedFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

const saving = ref(false)
const savingVla = ref(false)
const savingTask = ref(false)
const savingConfig = ref(false)
const deletingKey = ref('')
const deletingTask = ref('')
const startingTask = ref('')

const rowKey = (row: PhoneAppRow) => `${row.phone_id}||${row.app}`

// 手机ID列排好序，逐个显示；同一手机多 APP 时手机ID列可重复
const sortedPhoneApps = computed(() =>
  [...phoneApps.value].sort((a, b) => (a.phone_id < b.phone_id ? -1 : a.phone_id > b.phone_id ? 1 : a.app.localeCompare(b.app))),
)

// ---------- 数据加载：每次都从文件读取 ----------
async function loadState() {
  const [state, savedConfig] = await Promise.all([phoneFactoryApi.state(), phoneFactoryApi.config()])
  apps.value = state.apps
  phoneApps.value = state.phoneApps
  vla.value = state.vla
  tasks.value = state.tasks
  Object.assign(config, savedConfig)
}

// ---------- 1. 新增手机 ----------
async function handleAddPhoneApp() {
  const phoneId = newPhoneId.value.trim()
  const app = newApp.value.trim()
  if (!phoneId) return ElMessage.warning('请输入手机ID')
  if (!app) return ElMessage.warning('请选择或输入运行APP')
  saving.value = true
  try {
    const state = await phoneFactoryApi.addPhoneApp(phoneId, app)
    phoneApps.value = state.phoneApps
    apps.value = state.apps
    ElMessage.success(`已关联 手机 ${phoneId} ↔ ${app}`)
    newPhoneId.value = ''
    newApp.value = ''
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

// ---------- 2. 删除手机关联（只删对应APP这一条） ----------
async function handleRemovePhoneApp(row: PhoneAppRow) {
  deletingKey.value = rowKey(row)
  try {
    const state = await phoneFactoryApi.removePhoneApp(row.phone_id, row.app)
    phoneApps.value = state.phoneApps
    ElMessage.success(`已删除 手机 ${row.phone_id} 的 APP ${row.app}`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    deletingKey.value = ''
  }
}

// ---------- 3.1 保存VLA接口 ----------
async function handleSaveVla() {
  const value = newVla.value.trim()
  if (!value) return ElMessage.warning('请输入VLA接口（ip:port）')
  savingVla.value = true
  try {
    const state = await phoneFactoryApi.saveVla(value)
    vla.value = state.vla
    ElMessage.success(`已保存 VLA 接口 ${value}`)
    newVla.value = ''
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingVla.value = false
  }
}

// ---------- 3.2 选择文件 ----------
function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] || null
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}

// ---------- 3.2 新增任务（上传文件 + 登记关联） ----------
async function handleAddTask() {
  const description = newTaskDesc.value.trim()
  if (!description) return ElMessage.warning('请输入任务描述')
  if (!selectedFile.value) return ElMessage.warning('请先选择文件')
  savingTask.value = true
  try {
    const contentBase64 = await fileToBase64(selectedFile.value)
    const state = await phoneFactoryApi.addTask(description, selectedFile.value.name, contentBase64)
    tasks.value = state.tasks
    ElMessage.success(`任务「${description}」已新增，文件已上传`)
    newTaskDesc.value = ''
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingTask.value = false
  }
}

// ---------- 3.3 删除任务（界面与关联文件同步删除） ----------
async function handleRemoveTask(row: TaskRow) {
  deletingTask.value = row.filename
  try {
    const state = await phoneFactoryApi.removeTask(row.filename)
    tasks.value = state.tasks
    ElMessage.success(`已删除任务「${row.description}」`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    deletingTask.value = ''
  }
}

// ---------- 3.3 开始运行任务（未运行 -> 运行中） ----------
async function handleStartTask(row: TaskRow) {
  startingTask.value = row.filename
  try {
    const state = await phoneFactoryApi.startTask(row.filename)
    tasks.value = state.tasks
    ElMessage.success(`任务「${row.description}」已开始运行`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    startingTask.value = ''
  }
}

// ---------- 3.1.1 保存采样/经验库配置 ----------
async function handleConfigChange() {
  savingConfig.value = true
  try {
    const saved = await phoneFactoryApi.saveConfig({ ...config })
    Object.assign(config, saved)
    ElMessage.success('采样配置已保存')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingConfig.value = false
  }
}

onMounted(() => {
  loadState().catch((error) => ElMessage.error(`加载数据失败：${(error as Error).message}`))
})
</script>

<style scoped>
.phone-factory-page { width: min(1680px, 100%); min-height: 100vh; margin: 0 auto; padding: 34px 42px 50px; }
.page-hero { margin-bottom: 26px; }
.page-hero h1 { margin: 6px 0 4px; }

/* 左右双栏：左列=新增手机（含手机列表），右列=轨迹生产（含任务列表），两列互不交叉 */
.factory-layout { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; align-items: start; }

.panel { padding: 20px 22px; border: 1px solid var(--line); border-radius: 16px; background: rgba(255,255,255,.88); box-shadow: 0 10px 30px rgba(15,23,42,.05); }
.panel-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.panel-head h2 { margin: 0; font-size: 19px; letter-spacing: -.01em; }
.panel-tip { color: var(--accent-deep); font-size: 12px; font-weight: 700; }
.sub-title { margin: 20px 0 10px; font-size: 14px; color: var(--muted); letter-spacing: .02em; }
.sub-title::before { content: ''; display: inline-block; width: 6px; height: 6px; margin-right: 8px; border-radius: 50%; background: var(--accent); vertical-align: 2px; }

/* 行内表单：标签紧贴输入框，输入框宽度 = 原 150px 的 115% */
.row-form { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.field-label { flex: none; color: var(--ink); font-size: 13px; font-weight: 800; white-space: nowrap; }
.field-input { width: 173px; flex: none; }
.row-form + .row-form { margin-top: 12px; }
.config-row { margin-top: 12px; }
.config-row + .config-row { margin-top: 10px; }

/* 新增手机：运行APP 标签与前面手机ID输入框拉开约 3 个汉字的距离 */
.app-gap { margin-left: 42px; }

/* 轨迹生产：标签统一宽度并右对齐，输入框左对齐 */
.traj-panel .field-label { width: 112px; text-align: right; }
/* 是/否 单选下拉框：一个字的选项，无需很长 */
.traj-panel .yesno-input { width: 90px; flex: none; }
/* temperature/top_p：标签无需对齐主标签宽度，数字框宽度减半 */
.traj-panel .sub-label { width: auto; min-width: 0; text-align: left; }
.traj-panel .num-input { width: 86px; flex: none; }

.file-name { max-width: 200px; overflow: hidden; color: var(--accent-deep); font-size: 13px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.file-name.is-empty { color: var(--muted); font-weight: 400; }
.empty-hint { margin: 14px 0 2px; color: var(--muted); font-size: 13px; text-align: center; }
:deep(.el-table) { --el-table-header-bg-color: #f8fafc; }

/* 窄屏时回退为上下结构 */
@media (max-width: 1240px) {
  .factory-layout { grid-template-columns: 1fr; }
}
</style>
