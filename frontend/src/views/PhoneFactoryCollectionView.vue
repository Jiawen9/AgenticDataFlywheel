<template>
  <div class="phone-factory-page">
    <BatchPublishedNotice :notice="publishedNotice" />
    <header class="page-hero">
      <div>
        <span class="eyebrow">{{ isEvaluation ? 'MODEL ITERATION EVALUATION' : 'PHONE FACTORY COLLECTION' }}</span>
        <h1>{{ isEvaluation ? '模型迭代评估' : '手机工厂采集' }}</h1>
        <p>{{ isEvaluation ? '选择设备、任务和 VLA 开始评估，查看独立评估轮次与报告。' : '管理采集手机与运行 APP，配置 VLA 接口，上传轨迹生产任务文件。' }}</p>
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
            class="field-input phone-id-input"
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
          <el-button type="primary" :loading="saving" :disabled="Boolean(deletingKey)" @click="handleAddPhoneApp">新增</el-button>
        </div>

        <h3 class="sub-title">手机列表</h3>
        <el-alert v-if="deviceError" :title="deviceError" type="warning" :closable="false" show-icon />
        <el-table :data="sortedPhoneApps" border stripe data-testid="phone-apps">
          <el-table-column prop="phone_id" label="手机ID" min-width="110" show-overflow-tooltip />
          <el-table-column label="连接 / 型号" min-width="110"><template #default="{ row }">{{ deviceFor(row.phone_id)?.model || (deviceFor(row.phone_id) ? '已连接' : '离线 / 未知') }}<span v-if="deviceFor(row.phone_id)?.battery != null"> · {{ deviceFor(row.phone_id)?.battery }}%</span></template></el-table-column>
          <el-table-column prop="app" label="运行APP" min-width="85" show-overflow-tooltip />
          <el-table-column label="状态" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="row.status === '运行中' ? 'success' : 'info'" effect="light">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" align="center">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="taskBusy || saving || phoneIsBusy(row.phone_id)" @click="openCustomRun(row)">定制运行</el-button>
              <el-button link type="danger" :loading="deletingKey === rowKey(row)" :disabled="taskBusy || saving" @click="handleRemovePhoneApp(row)">
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
          <h2>{{ isEvaluation ? '模型评估' : '轨迹生产' }}</h2>
          <span class="panel-tip">上传文件由后端统一保存</span>
        </div>

        <!-- 3.1 VLA接口 -->
        <div class="row-form">
          <span class="field-label">VLA接口</span>
          <el-select
            v-model="newVla"
            :disabled="taskBusy"
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="ip:port"
            class="field-input vla-input"
            @keyup.enter="handleSaveVla"
          >
            <el-option v-for="item in vla" :key="item" :label="item" :value="item" />
          </el-select>
          <el-button :disabled="taskBusy" :loading="savingVla" @click="handleSaveVla">保存</el-button>
        </div>

        <!-- 3.1.1 是否打开采样 + 是否使用经验库 -->
        <div class="row-form config-row">
          <span class="field-label">是否打开采样</span>
          <el-select v-model="config.sampling_enabled" :disabled="taskBusy || savingConfig" class="yesno-input" @change="handleConfigChange">
            <el-option :value="true" label="是" />
            <el-option :value="false" label="否" />
          </el-select>
          <template v-if="config.sampling_enabled">
            <span class="field-label sub-label">temperature</span>
            <el-input-number v-model="config.temperature" :disabled="taskBusy || savingConfig" :min="0" :max="2" :step="0.05" :precision="2" :controls="false" class="num-input" @change="handleConfigChange" />
            <span class="field-label sub-label">top_p</span>
            <el-input-number v-model="config.top_p" :disabled="taskBusy || savingConfig" :min="0" :max="1" :step="0.05" :precision="2" :controls="false" class="num-input" @change="handleConfigChange" />
          </template>
        </div>
        <div class="row-form config-row">
          <span class="field-label">是否使用经验库</span>
          <el-select v-model="config.use_experience_lib" :disabled="taskBusy || savingConfig" class="yesno-input" @change="handleConfigChange">
            <el-option :value="true" label="是" />
            <el-option :value="false" label="否" />
          </el-select>
        </div>

        <template v-if="!isEvaluation">
        <h3 class="sub-title">采集任务批次</h3>
        <div class="row-form">
          <span class="field-label">采集批次</span>
          <el-select :model-value="selectedBatchId" filterable clearable :loading="loadingBatches || loadingBatch" :disabled="taskBusy || runDialogVisible" placeholder="选择已提交的采集批次" class="batch-select" @change="selectCollectionBatch">
            <el-option v-for="batch in collectionBatches" :key="batch.batch_id" :value="batch.batch_id" :label="collectionBatchOptionLabel(batch)" />
          </el-select>
          <el-button :loading="loadingBatches" :disabled="taskBusy || runDialogVisible" @click="collection.loadBatches()">刷新批次</el-button>
        </div>
        <el-alert v-if="batchError" :title="batchError" type="error" :closable="false" show-icon class="batch-alert" />
        <div v-if="selectedBatch && selectedBatchRow" class="batch-summary">
          <p><strong>{{ selectedBatch.task_count }} 条任务</strong> · {{ selectedBatch.apps.join('、') }} · {{ selectedBatch.created_at.slice(0, 19).replace('T', ' ') }}</p>
          <el-alert v-if="selectedBatch.warnings?.length" :title="warningSummary(selectedBatch.warnings)" type="warning" :closable="false" show-icon class="batch-alert" data-testid="collection-classification-warning" />
          <p>{{ selectedBatch.source_job_id ? `来源作业：${selectedBatch.source_job_id}` : '来源：手动上传' }} · {{ selectedBatchRow.status }}</p>
          <el-button type="primary" :loading="startingTask === selectedBatchRow.filename" :disabled="taskBusy" @click="handleStartTask(selectedBatchRow)">开始运行所选批次</el-button>
          <el-button :loading="downloadingBatch" :disabled="taskBusy" @click="downloadCollectionBatch">下载采集表</el-button>
          <router-link :to="{ path: '/collection/tree-building', query: { batch_id: selectedBatch.batch_id } }" class="preprocessing-link">前往预处理</router-link>
        </div>
        <p class="empty-hint">选择批次仅查看信息；手动上传任务也会登记业务批次，采集结果回传完成后可进入预处理。</p>
        </template>

        <!-- 3.2 新增任务 -->
        <div class="row-form">
          <span class="field-label">新增任务</span>
          <el-input v-model="newTaskDesc" :disabled="taskBusy" placeholder="任务描述" clearable class="field-input" @keyup.enter="handleAddTask" />
          <input ref="fileInput" type="file" accept=".xlsx" :disabled="taskBusy" hidden @change="onFileChange" />
          <el-button :disabled="taskBusy" @click="fileInput?.click()">选择文件</el-button>
          <span class="file-name" :class="{ 'is-empty': !selectedFile }">{{ selectedFile ? selectedFile.name : '未选择文件' }}</span>
          <el-button type="primary" :loading="savingTask" :disabled="taskBusy" @click="handleAddTask">新增</el-button>
        </div>

        <!-- 3.3 任务列表 -->
        <h3 class="sub-title">任务列表</h3>
        <el-table :data="tasks" border stripe>
          <el-table-column prop="description" label="任务描述" min-width="140" show-overflow-tooltip />
          <el-table-column prop="filename" label="文件名" min-width="140" show-overflow-tooltip />
          <el-table-column label="状态" width="90" align="center">
            <template #default="{ row }">
              <el-tag type="warning" effect="light">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" align="center">
            <template #default="{ row }">
              <el-button link type="danger" :loading="deletingTask === row.filename" :disabled="taskBusy" @click="handleRemoveTask(row)">
                删除
              </el-button>
              <el-button
                link
                type="primary"
                :loading="startingTask === row.filename"
                :disabled="taskBusy"
                @click="handleStartTask(row)"
              >
                开始运行
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="!tasks.length" class="empty-hint">暂无任务，请在上方新增。</p>

        <h3 class="sub-title">{{ isEvaluation ? '评估运行与报告' : '采集运行与回传' }}</h3>
        <el-alert v-if="runError" :title="runError" type="warning" :closable="false" />
        <el-table :data="runs" border data-testid="factory-runs">
          <el-table-column label="运行编号" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ row.collection_run_id || row.run_id }}</template></el-table-column>
          <el-table-column label="状态" width="110"><template #default="{ row }">{{ runStatus(row.status) }}</template></el-table-column>
          <el-table-column label="结果" min-width="170"><template #default="{ row }">{{ runMessage(row) }}</template></el-table-column>
          <el-table-column v-if="!isEvaluation" label="操作" min-width="100"><template #default="{ row }"><el-button link :loading="syncingRun === (row.collection_run_id || row.run_id)" @click="syncRun(row)">重试回传</el-button></template></el-table-column>
        </el-table>
        <section v-if="isEvaluation" data-testid="evaluation-reports">
          <div class="report-heading"><h3 class="sub-title">模型迭代评估报告</h3><el-button :loading="loadingReports" @click="reportPoller.start()">刷新报告</el-button></div>
          <el-alert v-if="reportError" :title="reportError" type="warning" :closable="false" />
          <p v-if="!reportFolders.length" class="empty-hint">{{ loadingReports ? '正在读取评估报告…' : '暂无评估报告，完成运行后将在这里显示。' }}</p>
          <section v-for="folder in reportFolders" :key="folder.run_id || folder.dir_name" class="report-folder">
            <strong>第 {{ folder.index }} 轮 · {{ folder.dir_name }}</strong>
            <p v-if="folder.run_id">运行标识：{{ folder.run_id }}</p>
            <p>轮次时间：{{ reportTime(folder.modified) }}</p>
            <el-table v-if="folder.files.length" :data="folder.files" border>
              <el-table-column prop="name" label="文件名" min-width="160" />
              <el-table-column label="生成时间" min-width="160"><template #default="{ row }">{{ reportTime(row.modified) }}</template></el-table-column>
              <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" :loading="downloadingReport === `${folder.run_id || folder.dir_name}/${row.file_id || row.name}`" @click="downloadReport(folder, row)">下载报告</el-button></template></el-table-column>
            </el-table>
            <p v-else class="empty-hint">第 {{ folder.index }} 轮暂无 xlsx 报告文件</p>
          </section>
        </section>
        <!-- 定制运行只覆盖此次运行的配置 -->
        <el-dialog v-model="runDialogVisible" title="定制运行" width="480px" :close-on-click-modal="false" :close-on-press-escape="!confirmingRun" :show-close="!confirmingRun">
          <div class="row-form">
            <span class="field-label">手机ID</span>
            <span data-testid="custom-run-phone">{{ runDialogPhoneId }}</span>
          </div>
          <div class="row-form" style="margin-top: 14px">
            <span class="field-label">运行APP</span>
            <span data-testid="custom-run-app">{{ runDialogApp }}</span>
          </div>
          <div class="custom-config">
            <label>VLA 接口<el-select v-model="runDialogVla" data-testid="custom-run-vla" filterable allow-create :disabled="confirmingRun" placeholder="选择 VLA 接口"><el-option v-for="item in vla" :key="item" :value="item" :label="item" /></el-select></label>
            <label>打开采样<el-switch v-model="runDialogConfig.sampling_enabled" :disabled="confirmingRun" /></label>
            <template v-if="runDialogConfig.sampling_enabled"><label>temperature<el-input-number v-model="runDialogConfig.temperature" :min="0" :max="2" :step="0.05" :disabled="confirmingRun" /></label><label>top_p<el-input-number v-model="runDialogConfig.top_p" :min="0" :max="1" :step="0.05" :disabled="confirmingRun" /></label></template>
            <label>使用经验库<el-switch v-model="runDialogConfig.use_experience_lib" :disabled="confirmingRun" /></label>
            <label>任务列表<el-select v-model="runDialogTaskKey" data-testid="custom-run-task-select" filterable :disabled="confirmingRun" placeholder="选择要运行的任务"><el-option v-for="option in runTaskOptions" :key="option.key" :value="option.key" :label="option.label" /></el-select></label>
          </div>
          <template #footer>
            <el-button :disabled="confirmingRun" @click="runDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="confirmingRun" @click="confirmRun">确定</el-button>
          </template>
        </el-dialog>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import BatchPublishedNotice from '@/components/BatchPublishedNotice.vue'
import { useBatchLifecycle } from '@/composables/useBatchLifecycle'
import { eventMatchesRoute, publishedBatch, withoutBatchQuery, type PublishedBatchEvent } from '@/utils/batchLifecycle'

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { phoneFactoryApi, modelIterationApi, factoryBatchesApi, newRunRequestId, type FactoryConfig, type PhoneAppRow, type TaskRow, type FactoryBatchSummary, type FactoryState, type FactoryRun, type ReportFolder, type ReportFile, type AdbDevice, type CollectionWarning } from '@/phoneFactoryApi'
import { useFactoryPolling } from '@/composables/useFactoryPolling'
import { usePhoneCollectionBatches } from '@/composables/usePhoneCollectionBatches'

const props = withDefaults(defineProps<{ mode?: 'generate' | 'modeliter' }>(), { mode: 'generate' })
const isEvaluation = props.mode === 'modeliter'
const factoryApi = isEvaluation ? modelIterationApi : phoneFactoryApi
// ---------- 状态 ----------
const apps = ref<string[]>([])
const phoneApps = ref<PhoneAppRow[]>([])
const vla = ref<string[]>([])
const tasks = ref<TaskRow[]>([])
const loadingFactory = ref(true)

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
const uploadRequestId = ref('')
watch([newTaskDesc, selectedFile], () => { uploadRequestId.value = '' }, { flush: 'sync' })
const fileInput = ref<HTMLInputElement | null>(null)

const saving = ref(false)
const savingVla = ref(false)
const savingTask = ref(false)
const savingConfig = ref(false)
const deletingKey = ref('')
const deletingTask = ref('')
const startingTask = ref('')
const confirmingRun = ref(false)

// 开始运行 / 定制运行 弹窗状态
const runDialogVisible = ref(false)
const runDialogPhoneId = ref('')
const runDialogApp = ref('')
const runDialogTaskKey = ref('')
const runDialogVla = ref('')
const runDialogConfig = reactive<FactoryConfig>({ ...config })
const devices = ref<AdbDevice[]>([])
const deviceError = ref('')
const runError = ref('')
const runs = ref<FactoryRun[]>([])
const reportFolders = ref<ReportFolder[]>([])
const loadingReports = ref(false), reportError = ref('')
const syncingRun = ref('')
const downloadingReport = ref('')
let stateEpoch = 0
const requestIds = new Map<string, string>()
const warningSummary = (warnings: CollectionWarning[]) => {
  const first = warnings[0]
  return first ? `${warnings.length} 条分类提示：${first.sheet} 第 ${first.row} 行（${first.field}）：${first.message}` : ''
}
const deviceFor = (id: string) => devices.value.find(device => device.serial === id)
const route = useRoute(), router = useRouter()
const manualTaskBusy = computed(() => savingTask.value || Boolean(deletingTask.value) || Boolean(startingTask.value) || confirmingRun.value)
const collection = usePhoneCollectionBatches(factoryBatchesApi, factoryApi, {
  setTasks: rows => { tasks.value = activeFactoryTasks(rows) },
  isBlocked: () => manualTaskBusy.value || runDialogVisible.value,
  runOptions: () => ({ vla: newVla.value.trim(), run_mode: props.mode, config: { ...config } }),
})
const { batches: collectionBatches, selectedBatchId, selectedBatch, loadingBatches, loadingBatch, downloading: downloadingBatch, error: batchError } = collection
// A production task keeps its batch identity even before its workbook is registered.
const runTaskOptions = computed(() => {
  const options = new Map<string, { key: string; label: string; task: TaskRow }>()
  for (const task of activeFactoryTasks(tasks.value)) {
    const key = !isEvaluation && task.source_batch_id ? `batch:${task.source_batch_id}` : `task:${task.filename}`
    options.set(key, { key, label: `${task.description} · ${task.source_batch_id || task.filename}`, task })
  }
  if (!isEvaluation) for (const batch of collectionBatches.value) {
    if (publishedBatch(batch.batch_id)) continue
    const key = `batch:${batch.batch_id}`
    const task = options.get(key)?.task || { description: `采集批次 ${batch.batch_id}`, filename: batch.filename, source_batch_id: batch.batch_id, status: '未运行' }
    options.set(key, { key, label: collectionBatchOptionLabel(batch), task })
  }
  return [...options.values()]
})
const runTargetTask = computed(() => runTaskOptions.value.find(option => option.key === runDialogTaskKey.value)?.task || null)
let disposed = false
const activeFactoryTasks = (rows: TaskRow[]) => isEvaluation ? rows : rows.filter(item => !publishedBatch(item.source_batch_id))
const lifecycle = useBatchLifecycle({
  currentBatch: () => isEvaluation ? '' : selectedBatchId.value || String(route.query.collection_batch_id ?? route.query.batch_id ?? ''),
  onPublished,
  async refreshChoices() {
    if (isEvaluation) return
    // The batch area and the phone's draft can refer to two independent batches.
    const target = runDialogVisible.value && runDialogTaskKey.value.startsWith('batch:') ? runDialogTaskKey.value.slice(6) : ''
    if (target && target !== selectedBatchId.value) await lifecycle.checkBatch(target)
    await collection.loadBatches()
  },
})
const { notice: publishedNotice } = lifecycle
function onPublished(event: PublishedBatchEvent) {
  if (isEvaluation) return
  const targetPublished = event.batch_ids.some(id => runDialogTaskKey.value === `batch:${id}`)
  const selectedPublished = eventMatchesRoute(event, selectedBatchId.value, route.query)
  const current = selectedPublished || targetPublished
  stateEpoch++
  collection.retireBatches(event.batch_ids, selectedPublished); tasks.value = activeFactoryTasks(tasks.value)
  runs.value = runs.value.filter(item => !item.batch_id || !event.batch_ids.includes(item.batch_id))
  if (targetPublished) { runDialogTaskKey.value = ''; runDialogVisible.value = false }
  if (!current) return
  publishedNotice.value = event
  if (selectedPublished) void router.replace({ query: withoutBatchQuery(route.query) })
}
const taskBusy = computed(() => loadingFactory.value || manualTaskBusy.value || collection.busy.value || Boolean(deletingKey.value))
const collectionBatchKindLabel = (kind: FactoryBatchSummary['kind']) => kind === 'manual_collection' ? '手动上传' : kind === 'augmentation' ? '泛化扩增' : '任务生成'
const collectionBatchTimeLabel = (createdAt: string) => createdAt.slice(0, 19).replace('T', ' ')
const collectionBatchOptionLabel = (batch: FactoryBatchSummary) =>
  `${batch.batch_id} · ${collectionBatchKindLabel(batch.kind)} · ${collectionBatchTimeLabel(batch.created_at)} · ${batch.task_count} 条任务`
const selectedBatchRow = computed<TaskRow | null>(() => {
  const batch = selectedBatch.value
  if (!batch) return null
  return tasks.value.find(task => task.source_batch_id === batch.batch_id) || {
    description: `采集批次 ${batch.batch_id}`, filename: batch.filename, source_batch_id: batch.batch_id, status: '未运行',
  }
})
async function selectCollectionBatch(value: unknown) { const id = typeof value === 'string' ? value : ''; if (!await lifecycle.checkBatch(id)) return; publishedNotice.value = null; await collection.selectBatch(id) }
async function downloadCollectionBatch() {
  if (taskBusy.value) return
  try { await collection.downloadBatch() }
  catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
}
watch(() => route.query.batch_id ?? route.query.collection_batch_id, value => { if (!isEvaluation && typeof value === 'string') void selectCollectionBatch(value) })

const rowKey = (row: PhoneAppRow) => `${row.phone_id}||${row.app}`

// Occupancy belongs to a physical phone, including its other App associations.
const phoneIsBusy = (phoneId: string) => phoneApps.value.some(row => row.phone_id === phoneId && ['运行中', '排队中', '下发中'].includes(row.status))

// 手机ID列排好序，逐个显示；同一手机多 APP 时手机ID列可重复
const sortedPhoneApps = computed(() =>
  [...phoneApps.value].sort((a, b) => (a.phone_id < b.phone_id ? -1 : a.phone_id > b.phone_id ? 1 : a.app.localeCompare(b.app))),
)

// 状态由后端统一维护；设备是否忙不改变其他任务的状态。
async function loadState() {
  const [state, savedConfig] = await Promise.all([factoryApi.state(), factoryApi.config()])
  if (disposed) return
  applyState(state)
  Object.assign(config, savedConfig)
  if (!newVla.value) newVla.value = state.vla[0] || ''
}

// ---------- 1. 新增手机 ----------
async function handleAddPhoneApp() {
  const phoneId = newPhoneId.value.trim()
  const app = newApp.value.trim()
  if (!phoneId) return ElMessage.warning('请输入手机ID')
  if (!app) return ElMessage.warning('请选择或输入运行APP')
  if (saving.value || deletingKey.value) return
  saving.value = true; stateEpoch++
  try {
    const state = await factoryApi.addPhoneApp(phoneId, app)
    phoneApps.value = state.phoneApps
    apps.value = state.apps
    ElMessage.success(`已关联 手机 ${phoneId} ↔ ${app}`)
    newPhoneId.value = ''
    newApp.value = ''
    // 同一手机新增 App 时，远端已存在的设备目录由服务端幂等复用。
    try {
      await factoryApi.remoteAddPhone(phoneId)
    } catch (error) {
      ElMessage.warning(`已本地保存，但通知 server 失败：${(error as Error).message}`)
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    stateEpoch++; saving.value = false
  }
}

// 删除远端设备成功之后，由后端统一删除本地手机和全部 App 关联。
async function handleRemovePhoneApp(row: PhoneAppRow) {
  if (deletingKey.value || saving.value || taskBusy.value) return
  try { await ElMessageBox.confirm(`将停止手机 ${row.phone_id} 参与的整个运行（包括同次运行的其他手机），并删除选中手机的设备工作目录及全部 App 关联。已归档轨迹和报告继续保留。`, '删除手机', { type: 'warning', confirmButtonText: '停止并删除', cancelButtonText: '取消' }) }
  catch { return }
  deletingKey.value = rowKey(row); stateEpoch++
  try {
    const response = await factoryApi.remoteDeletePhone(row.phone_id)
    const state = response.state || await factoryApi.state()
    if (!disposed) { applyState(state); ElMessage.success(`已删除手机 ${row.phone_id}`) }
  } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { stateEpoch++; deletingKey.value = '' }
}

// ---------- 3.1 保存VLA接口 ----------
async function handleSaveVla() {
  const value = newVla.value.trim()
  if (!value) return ElMessage.warning('请输入VLA接口（ip:port）')
  if (savingVla.value) return
  savingVla.value = true; stateEpoch++
  try {
    const state = await factoryApi.saveVla(value)
    vla.value = state.vla
    ElMessage.success(`已保存 VLA 接口 ${value}`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    stateEpoch++; savingVla.value = false
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
  if (taskBusy.value) return
  const description = newTaskDesc.value.trim()
  if (!description) return ElMessage.warning('请输入任务描述')
  if (!selectedFile.value) return ElMessage.warning('请先选择文件')
  if (!/\.xlsx$/i.test(selectedFile.value.name)) return ElMessage.warning('请选择 Excel 采集任务表（.xlsx）')
  const file = selectedFile.value
  const requestId = uploadRequestId.value || newRunRequestId()
  uploadRequestId.value = requestId
  savingTask.value = true; stateEpoch++
  try {
    const contentBase64 = await fileToBase64(file)
    const state = await factoryApi.addTask(description, file.name, contentBase64, undefined, requestId)
    tasks.value = activeFactoryTasks(state.tasks)
    ElMessage.success(`任务「${description}」已新增，文件已上传`)
    if (state.imported_task?.warnings?.length) ElMessage.warning(warningSummary(state.imported_task.warnings))
    if (!isEvaluation) {
      const added = state.imported_task || state.tasks.find(item => item.filename === file.name)
      // Defer refreshing until the upload busy state is released.
      setTimeout(() => { if (!disposed) void collection.loadBatches(added?.source_batch_id) }, 0)
    }
    newTaskDesc.value = ''
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    stateEpoch++; savingTask.value = false
  }
}

// ---------- 3.3 删除任务（界面与关联文件同步删除） ----------
async function handleRemoveTask(row: TaskRow) {
  if (taskBusy.value) return
  deletingTask.value = row.filename; stateEpoch++
  try {
    const state = await factoryApi.removeTask(row.filename)
    tasks.value = activeFactoryTasks(state.tasks)
    ElMessage.success(`已删除任务「${row.description}」`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    stateEpoch++; deletingTask.value = ''
  }
}

// 定制运行从手机行进入，任务和参数只属于本次下发。
function openCustomRun(row: PhoneAppRow) {
  if (taskBusy.value || phoneIsBusy(row.phone_id)) return
  runDialogTaskKey.value = ''
  runDialogPhoneId.value = row.phone_id
  runDialogApp.value = row.app
  runDialogVla.value = newVla.value.trim() || vla.value[0] || ''
  Object.assign(runDialogConfig, config)
  runDialogVisible.value = true
}

// ---------- 3.3 开始运行：不弹窗，把任务文件与 手机ID/运行APP 关联文件 发送到 server 端，
// 关联文件中的所有手机都执行 ----------
async function handleStartTask(row: TaskRow) {
  if (taskBusy.value) return
  if (!newVla.value.trim()) return ElMessage.warning('请选择 VLA 接口')
  startingTask.value = row.filename; stateEpoch++
  try {
    if (!isEvaluation && row.source_batch_id) {
      const remote = await collection.runBatch(row.source_batch_id)
      if (!disposed && !publishedBatch(row.source_batch_id)) ElMessage.success(remote.message || '采集批次已下发')
    } else { await dispatchTask(row, '', '', newVla.value.trim(), { ...config }) }
  } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { stateEpoch++; startingTask.value = '' }
}

// ---------- 3.3 定制运行确认：把任务文件、关联文件 + 手机ID/运行APP 发送到 server 端，
// 仅指定的 手机ID 执行 ----------
async function confirmRun() {
  if (taskBusy.value) return
  if (!runTargetTask.value) return ElMessage.warning('请选择要运行的任务')
  if (!runDialogPhoneId.value) return ElMessage.warning('请选择手机ID')
  if (!runDialogApp.value) return ElMessage.warning('请选择运行APP')
  if (!runDialogVla.value.trim()) return ElMessage.warning('请选择 VLA 接口')
  const task = runTargetTask.value
  confirmingRun.value = true; stateEpoch++
  try {
    if (!isEvaluation && task.source_batch_id) {
      const remote = await collection.runBatch(task.source_batch_id, runDialogPhoneId.value, runDialogApp.value, { vla: runDialogVla.value.trim(), run_mode: 'generate', config: { ...runDialogConfig } })
      if (!disposed && !publishedBatch(task.source_batch_id)) ElMessage.success(remote.message || '定制采集任务已下发')
    } else { await dispatchTask(task, runDialogPhoneId.value, runDialogApp.value, runDialogVla.value.trim(), { ...runDialogConfig }) }
    if (!disposed && (isEvaluation || !publishedBatch(task.source_batch_id))) runDialogVisible.value = false
  } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { stateEpoch++; confirmingRun.value = false }
}

async function dispatchTask(task: TaskRow, phoneId: string, app: string, vla: string, config: FactoryConfig) {
  const parameters = { filename: task.filename, phone_id: phoneId, app, vla, config, run_mode: props.mode }
  const key = JSON.stringify(parameters)
  const requestId = requestIds.get(key) || newRunRequestId()
  requestIds.set(key, requestId)
  const remote = await factoryApi.remoteStartRun({ ...parameters, request_id: requestId })
  requestIds.delete(key)
  if (disposed) return
  ElMessage.success(remote.message || '任务已下发')
  try { const state = await factoryApi.state(); if (!disposed) applyState(state) } catch { /* Subsequent polling restores state. */ }
  if (!isEvaluation) setTimeout(() => { if (!disposed) void collection.loadBatches(remote.batch_id) }, 0)
}

async function handleConfigChange() {
  savingConfig.value = true
  try {
    const saved = await factoryApi.saveConfig({ ...config })
    Object.assign(config, saved)
    ElMessage.success('采样配置已保存')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingConfig.value = false
  }
}

function applyState(state: FactoryState) {
  apps.value = state.apps; phoneApps.value = state.phoneApps; vla.value = state.vla; tasks.value = activeFactoryTasks(state.tasks)
}
useFactoryPolling(async (signal, current) => {
  const epoch = stateEpoch
  try {
    const state = await factoryApi.state(signal)
    if (!current()) return
    if (epoch === stateEpoch && !taskBusy.value && !saving.value && !deletingKey.value) applyState(state)
    const status = await factoryApi.remoteStatus(state.phones, signal)
    const adb = await factoryApi.adbDevices(signal)
    if (current() && epoch === stateEpoch) {
      const statuses = new Map(status.statuses.map(item => [item.phone_id, item.status]))
      phoneApps.value = phoneApps.value.map(row => ({ ...row, status: statuses.get(row.phone_id) || '未知' }))
      devices.value = adb.devices; deviceError.value = ''
    }
  } catch (error) { if (current()) deviceError.value = `设备状态暂不可用：${(error as Error).message}` }
  if (!current()) return
  try {
    const result = await factoryApi.runs(signal)
    if (current()) { runs.value = result.runs.filter(item => isEvaluation || !publishedBatch(item.batch_id)); runError.value = '' }
  } catch (error) { if (current()) runError.value = `运行状态暂不可用：${(error as Error).message}` }
})
const reportPoller = isEvaluation ? useFactoryPolling(async (signal, current) => {
  loadingReports.value = true
  try {
    const result = await factoryApi.reports(signal)
    if (current()) { reportFolders.value = result.folders; reportError.value = '' }
  } catch (error) { if (current()) reportError.value = `报告读取失败：${(error as Error).message}` }
  finally { if (current()) loadingReports.value = false }
}) : { start() {} }
const reportTime = (value?: number) => value != null && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
const runStatus = (status: string) => ({ dispatching: '正在下发', queued: '排队中', running: '运行中', completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', partial: '部分完成', ready: '结果已就绪', waiting: '等待回传', syncing: '正在回传', pending: '等待中' } as Record<string, string>)[status] || status
const runMessage = (run: FactoryRun) => run.transfer_error || run.dispatch_error || run.error || run.errors?.map(item => item.error || item.message).filter(Boolean).join('；') || (run.trajectory_count != null ? `${run.trajectory_count} 条轨迹` : runStatus(run.transfer_status || ''))
async function syncRun(run: FactoryRun) {
  const id = run.collection_run_id || run.run_id
  if (!id || syncingRun.value) return
  syncingRun.value = id
  try { const result = await factoryApi.syncRun(id); if (!disposed) { runs.value = runs.value.map(item => (item.collection_run_id || item.run_id) === id ? result : item); ElMessage.success('已请求同步采集结果') } }
  catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { syncingRun.value = '' }
}
async function downloadReport(folder: ReportFolder, file: ReportFile) {
  if (downloadingReport.value) return
  downloadingReport.value = `${folder.run_id || folder.dir_name}/${file.file_id || file.name}`
  try {
    const blob = await factoryApi.reportDownload(folder, file)
    if (disposed) return
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = file.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 0)
    ElMessage.success(`已开始下载 ${file.name}`)
  } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { downloadingReport.value = '' }
}

onMounted(() => {
  document.body.classList.add('factory-responsive')
  loadState().catch((error) => ElMessage.error(`加载数据失败：${(error as Error).message}`)).finally(() => { loadingFactory.value = false })
  if (isEvaluation) return
  const requested = typeof route.query.batch_id === 'string' ? route.query.batch_id : typeof route.query.collection_batch_id === 'string' ? route.query.collection_batch_id : undefined
  void lifecycle.checkBatch(requested || '').then(active => collection.loadBatches(active ? requested : undefined)).catch(cause => { batchError.value = (cause as Error).message })
})
onBeforeUnmount(() => { document.body.classList.remove('factory-responsive'); disposed = true; collection.dispose() })
</script>

<style scoped>
:global(body.factory-responsive) { min-width: 0; }
.report-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.report-folder p { overflow-wrap: anywhere; color: var(--muted); font-size: 13px; }.custom-config { display: grid; gap: 14px; margin-top: 20px; }.custom-config label { display: flex; align-items: center; justify-content: space-between; gap: 14px; }.custom-config .el-select { width: 240px; }.report-folder { margin-top: 14px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }.report-file { display:flex; justify-content:space-between; gap:12px; margin-top:8px; overflow-wrap:anywhere; }
.batch-select { width: min(480px, 100%); }.batch-summary { padding: 12px; border: 1px solid #dbe3ed; border-radius: 8px; background: #f8fffd; }.batch-summary p { margin: 0 0 9px; color: #64748b; font-size: 12px; overflow-wrap: anywhere; }.batch-alert { margin: 10px 0; }
.phone-factory-page { width: min(1680px, 100%); min-height: 100vh; margin: 0 auto; padding: 34px 42px 50px; }
.page-hero { margin-bottom: 26px; }
.page-hero h1 { margin: 6px 0 4px; }

/* 左右双栏：左列=新增手机（含手机列表），右列=轨迹生产（含任务列表），两列互不交叉
   左列整体比右列窄 20 个英文字符（约 140px），手机ID/运行APP 两列合计减少 20 字符 */
.factory-layout { display: grid; grid-template-columns: minmax(0, calc(50% - 70px)) minmax(0, calc(50% + 70px)); gap: 22px; align-items: start; }

.panel { min-width: 0; padding: 20px 22px; border: 1px solid var(--line); border-radius: 16px; background: rgba(255,255,255,.88); box-shadow: 0 10px 30px rgba(15,23,42,.05); }
.panel-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.panel-head h2 { margin: 0; font-size: 19px; letter-spacing: -.01em; }
.panel-tip { color: var(--accent-deep); font-size: 12px; font-weight: 700; }
.sub-title { margin: 20px 0 10px; font-size: 14px; color: var(--muted); letter-spacing: .02em; }
.sub-title::before { content: ''; display: inline-block; width: 6px; height: 6px; margin-right: 8px; border-radius: 50%; background: var(--accent); vertical-align: 2px; }

/* 行内表单：标签紧贴输入框，输入框宽度 = 原 150px 的 115% */
.row-form { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.field-label { flex: none; color: var(--ink); font-size: 13px; font-weight: 800; white-space: nowrap; }
.field-input { width: 173px; flex: none; }
/* 手机ID 输入框：比默认加宽 10 个英文字符 */
.phone-id-input { width: 243px; }
.row-form + .row-form { margin-top: 12px; }
.config-row { margin-top: 12px; }
.config-row + .config-row { margin-top: 10px; }

/* 新增手机：运行APP 标签与前面手机ID输入框拉开约 3 个汉字的距离 */
.app-gap { margin-left: 42px; }

/* 轨迹生产：标签统一宽度并右对齐，输入框左对齐 */
.traj-panel .field-label { width: 112px; text-align: right; }
/* VLA接口 输入框：比默认加宽 10 个英文字符 */
.traj-panel .vla-input { width: 243px; }
/* 是/否 单选下拉框：一个字的选项，无需很长 */
.traj-panel .yesno-input { width: 90px; flex: none; }
/* temperature/top_p：标签无需对齐主标签宽度，数字框宽度减半 */
.traj-panel .sub-label { width: auto; min-width: 0; text-align: left; }
.traj-panel .num-input { width: 86px; flex: none; }

.file-name { max-width: 200px; overflow: hidden; color: var(--accent-deep); font-size: 13px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.file-name.is-empty { color: var(--muted); font-weight: 400; }
.empty-hint { margin: 14px 0 2px; color: var(--muted); font-size: 13px; text-align: center; }
:deep(.el-table) { --el-table-header-bg-color: #f8fafc; }

@media (max-width: 760px) {
  .phone-factory-page { padding: 22px 16px; box-sizing: border-box; }
  .panel { padding: 16px; }.field-input, .batch-select, .traj-panel .vla-input { max-width: 100%; }.app-gap { margin-left: 0; }.traj-panel .field-label { width: auto; }.row-form { gap: 10px; }
}

/* 窄屏时回退为上下结构 */
@media (max-width: 1240px) {
  .factory-layout { grid-template-columns: 1fr; }
}
</style>
