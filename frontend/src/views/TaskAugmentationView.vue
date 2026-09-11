<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Upload } from '@element-plus/icons-vue'
import { api, taskGenerationDownloadUrl } from '@/api'
import type { AugmentationSeed, TaskGenerationJob, TaskGenerationResult } from '@/types'
import AugmentationSceneTree from '@/components/AugmentationSceneTree.vue'
import CollectionBatchSubmission from '@/components/CollectionBatchSubmission.vue'
import { useAugmentationPreview } from '@/composables/useAugmentationPreview'
import { findNode, nodePath } from '@/utils/scenarioTree'
import { augmentationResultGroupId, groupAugmentationResults, type AugmentationResultGroup } from '@/utils/augmentationReview'

const review = useAugmentationPreview(api, {
  error: message => ElMessage.error(message),
  async decide() {
    try {
      await ElMessageBox.confirm('当前变体有未保存的修改。保存后继续，或放弃本次修改。', '未保存的修改', { confirmButtonText: '保存并继续', cancelButtonText: '放弃修改', distinguishCancelAndClose: true, closeOnClickModal: false })
      return 'save'
    } catch (action) { return action === 'cancel' ? 'discard' : 'cancel' }
  },
  async confirmDelete(row) {
    try { await ElMessageBox.confirm(`确定${row.deleted ? '恢复' : '删除'}这条变体任务吗？`, '确认操作', { type: 'warning' }); return true }
    catch { return false }
  },
})
const { jobs, selectedJob, preview, results, errors, listError, detailError, loading, refreshing, busy, active, editingId, editingText } = review
const selectedFile = ref<File | null>(null)
const generateN = ref(10)
const fileInput = ref<HTMLInputElement>()
const selectedNodeId = ref('')
const focusedSeedId = ref('')
const focusRevision = ref(0)
const focusedResultId = ref('')
const seedPage = ref(1)
const seedPanelExpanded = ref(false)
const seedPanel = ref<HTMLElement>()
const resultPage = ref(1)
const expandedGroupIds = ref(new Set<string>())
const variantPages = ref<Record<string, number>>({})
const seedStatusFilter = ref('')
const pageSize = 20
const persistedJobKey = 'task-augmentation:selected-job'
const seeds = computed(() => preview.value?.seeds || [])
const seedsById = computed(() => new Map(seeds.value.map(seed => [seed.seed_id, seed])))
const filteredSeeds = computed(() => seeds.value.filter(seed => (!selectedNodeId.value || seed.node_path_ids.includes(selectedNodeId.value))
  && (!seedStatusFilter.value || (seedStatusFilter.value === 'matched' ? seed.mapping_status === 'matched'
    : seedStatusFilter.value === 'failed' ? seed.mapping_status === 'classification_failed' : ['unclassified', 'not_found'].includes(seed.mapping_status)))))
const pagedSeeds = computed(() => filteredSeeds.value.slice((seedPage.value - 1) * pageSize, seedPage.value * pageSize))
const focusedSeed = computed(() => seedsById.value.get(focusedSeedId.value))
// Locating a seed changes focus only. The explicit tree selection filters results.
const filteredResults = computed(() => results.value.filter(row =>
  !selectedNodeId.value || Boolean(row.seed_id && seedsById.value.get(row.seed_id)?.node_path_ids.includes(selectedNodeId.value))))
const resultGroups = computed(() => groupAugmentationResults(filteredResults.value, seeds.value))
const pagedGroups = computed(() => resultGroups.value.slice((resultPage.value - 1) * pageSize, resultPage.value * pageSize))
const focusedGroupId = computed(() => {
  const row = results.value.find(row => row.result_id === focusedResultId.value)
  return row ? augmentationResultGroupId(row) : focusedSeedId.value ? `seed:${focusedSeedId.value}` : ''
})
const activeResults = computed(() => results.value.filter(row => !row.deleted))
const branchLabel = computed(() => selectedNodeId.value && preview.value?.tree ? nodePath(preview.value.tree.scenes, selectedNodeId.value).join(' / ') : '')
const snapshotVersion = computed(() => preview.value?.tree?.version || selectedJob.value?.knowledge_base_version)
const canStart = computed(() => selectedJob.value?.status === 'awaiting_confirmation' && Boolean(preview.value?.available && preview.value?.stats.eligible) && !detailError.value)

function statusText(status: TaskGenerationJob['status']) {
  return { queued: '排队中', running: '执行中', awaiting_confirmation: '待开始扩增', succeeded: '已完成', partial: '部分完成', failed: '失败', interrupted: '已中断' }[status]
}
function stageText(stage: string) {
  return ({ preparing: '准备场景树快照', classifying: '匹配失败用例', awaiting_confirmation: '待开始扩增', generating: '生成变体任务', augmenting: '生成变体任务', exporting: '整理结果', queued: '排队中' } as Record<string, string>)[stage] || stage
}
function seedMappingText(seed: AugmentationSeed) {
  if (seed.classification_status === 'classifying') return '正在分类'
  return ({ pending: '等待分类', matched: '已关联', unclassified: '未分类', not_found: '分类不在快照中', classification_failed: '分类失败' } as const)[seed.mapping_status]
}
function generationText(seed: AugmentationSeed) {
  return ({ waiting: '等待扩增', generating: '正在扩增', succeeded: '已完成', partial: '部分完成', failed: '扩增失败', skipped: '已跳过' } as const)[seed.generation_status]
}
function seedPath(seed: AugmentationSeed) { return [seed.scene, seed.capability, seed.sub_capability, seed.app].filter(Boolean).join(' / ') }
function seedRowClass({ row }: { row: AugmentationSeed }) { return row.seed_id === focusedSeedId.value ? 'focused-row' : '' }
function resultRowClass({ row }: { row: TaskGenerationResult }) { return row.result_id === focusedResultId.value ? 'focused-row' : '' }
function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) { selectedFile.value = null; input.value = ''; ElMessage.error('只支持 .xlsx 或 .xlsm 文件'); return }
  selectedFile.value = file
}
async function submit() {
  if (!selectedFile.value) return ElMessage.warning('请先选择失败用例 Excel')
  if (await review.create(selectedFile.value, generateN.value)) {
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    ElMessage.success('已提交场景匹配，匹配完成后可确认开始扩增')
  }
}
async function selectJob(job: TaskGenerationJob) { if (selectedJob.value?.job_id !== job.job_id) await review.selectJob(job) }
async function refreshSelected() { if (await review.protect()) await review.refresh() }
async function selectNode(id: string) {
  if (!(await review.protect()) || !preview.value?.tree || !findNode(preview.value.tree.scenes, id)) return
  selectedNodeId.value = id; focusedSeedId.value = ''; focusedResultId.value = ''; seedPage.value = 1; resultPage.value = 1
}
async function locateSeed(seed: AugmentationSeed) {
  if (!(await review.protect())) return
  focusedResultId.value = ''
  focusSeed(seed.seed_id)
  const row = filteredResults.value.find(row => row.seed_id === seed.seed_id)
  if (row) revealResult(row)
}
async function locateResult(row: TaskGenerationResult) {
  if (editingId.value !== row.result_id) await locateResultTarget(row, false)
}
async function locateSourceResult(row: TaskGenerationResult) {
  await locateResultTarget(row, true)
}
async function locateResultTarget(row: TaskGenerationResult, showSource: boolean) {
  if (!(await review.protect())) return
  focusedResultId.value = row.result_id
  focusSeed(row.seed_id)
  revealResult(row)
  if (!showSource || !focusedSeedId.value) return
  seedPanelExpanded.value = true
  if (!filteredSeeds.value.some(seed => seed.seed_id === focusedSeedId.value)) return
  const jobId = selectedJob.value?.job_id, revision = focusRevision.value
  await nextTick()
  // The newly mounted table measures its header in the first frame. Scroll
  // after that resize (and the tree's path positioning) has reached the DOM.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (selectedJob.value?.job_id !== jobId || focusRevision.value !== revision
      || focusedResultId.value !== row.result_id || !seedPanelExpanded.value) return
    seedPanel.value?.querySelector('.seed-table .focused-row')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }))
}
function focusSeed(seedId?: string) {
  // Historical rows without a recorded seed cannot inherit another row's path.
  focusedSeedId.value = seedId && seedsById.value.has(seedId) ? seedId : ''
  focusRevision.value++
  if (!focusedSeedId.value) return
  const index = filteredSeeds.value.findIndex(seed => seed.seed_id === focusedSeedId.value)
  if (index >= 0) seedPage.value = Math.floor(index / pageSize) + 1
  else ElMessage.info('源用例不在当前筛选范围内，可清除筛选查看')
}
async function clearFocus() {
  if (!(await review.protect())) return
  focusedSeedId.value = ''; focusedResultId.value = ''
}
function revealResult(row: TaskGenerationResult) {
  const id = augmentationResultGroupId(row)
  const index = resultGroups.value.findIndex(group => group.id === id)
  if (index < 0) return
  resultPage.value = Math.floor(index / pageSize) + 1
  expandedGroupIds.value = new Set([...expandedGroupIds.value, id])
  const group = resultGroups.value[index]!
  variantPages.value[id] = Math.max(1, Math.floor(group.results.findIndex(result => result.result_id === row.result_id) / pageSize) + 1)
}
async function toggleGroup(group: AugmentationResultGroup) {
  if (busy.value) return
  const closing = expandedGroupIds.value.has(group.id)
  if (closing && group.results.some(row => row.result_id === editingId.value) && !(await review.protect())) return
  const next = new Set(expandedGroupIds.value)
  if (closing) next.delete(group.id)
  else next.add(group.id)
  expandedGroupIds.value = next
}
function variantPage(group: AugmentationResultGroup) {
  return Math.max(1, Math.min(variantPages.value[group.id] || 1, Math.ceil(group.results.length / pageSize)))
}
function pagedVariants(group: AugmentationResultGroup) {
  return group.results.slice((variantPage(group) - 1) * pageSize, variantPage(group) * pageSize)
}
async function changeVariantPage(groupId: string, page: number) {
  if (await review.protect()) variantPages.value[groupId] = page
}
async function beforeHideTreeSelection() {
  if (!(await review.protect())) return false
  selectedNodeId.value = ''; focusedSeedId.value = ''; focusedResultId.value = ''
  seedPage.value = 1; resultPage.value = 1
  return true
}
async function clearFilters() {
  if (!(await review.protect())) return
  selectedNodeId.value = ''; seedStatusFilter.value = ''; seedPage.value = 1; resultPage.value = 1
}
async function changeResultPage(page: number) { if (await review.protect()) resultPage.value = page }
async function changeSeedStatus(value: string) {
  if (!(await review.protect())) return
  seedStatusFilter.value = value || ''; seedPage.value = 1
}
async function exportResults() {
  const result = await review.exportResults()
  if (!result) return
  const link = document.createElement('a')
  link.href = taskGenerationDownloadUrl(result.jobId, result.exported.filename); link.download = result.exported.filename; link.click()
}
function beforeUnload(event: BeforeUnloadEvent) { if (review.dirty.value || busy.value) { event.preventDefault(); event.returnValue = '' } }
watch(() => selectedJob.value?.job_id, id => {
  selectedNodeId.value = ''; focusedSeedId.value = ''; focusedResultId.value = ''; seedStatusFilter.value = ''; seedPage.value = 1; resultPage.value = 1
  expandedGroupIds.value = new Set(); variantPages.value = {}
  seedPanelExpanded.value = false
  if (id) { try { localStorage.setItem(persistedJobKey, id) } catch { /* Storage can be unavailable. */ } }
}, { flush: 'sync' })
watch(() => filteredSeeds.value.length, length => { seedPage.value = Math.max(1, Math.min(seedPage.value, Math.ceil(length / pageSize))) })
watch(() => resultGroups.value.length, length => { resultPage.value = Math.max(1, Math.min(resultPage.value, Math.ceil(length / pageSize))) })
onMounted(() => {
  let preferredId: string | undefined
  try { preferredId = localStorage.getItem(persistedJobKey) || undefined } catch { /* Restore is optional. */ }
  void review.loadJobs(preferredId); window.addEventListener('beforeunload', beforeUnload)
})
onBeforeRouteLeave(() => review.protect())
onBeforeUnmount(() => { review.dispose(); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="page augmentation-page">
    <header class="page-hero"><div><span class="eyebrow">TASK AUGMENTATION</span><h1>任务扩增</h1><p>先查看失败用例在场景树中的归属，再确认生成变体任务。</p></div><div class="hero-metrics"><div><b>{{ activeResults.length }}</b><span>未删除变体</span></div><div><b>{{ jobs.length }}</b><span>扩增作业</span></div><div><b>{{ selectedJob?.generate_n ?? generateN }}</b><span>每用例变体</span></div></div></header>
    <section class="upload-card">
      <div class="upload-copy"><span class="eyebrow">FAILED SEED WORKBOOK</span><h2>上传失败用例</h2><p>支持“任务、涉及APP”表；有“任务结果”列时排除 TRUE。也支持已包含 app/task/scene/capability/sub_capability 的分类表。</p></div>
      <div class="upload-actions"><label class="file-picker"><el-icon><Upload /></el-icon><span>{{ selectedFile?.name || '选择 Excel 文件' }}</span><input ref="fileInput" type="file" accept=".xlsx,.xlsm" :disabled="busy" @change="chooseFile" /></label><div class="count-control"><span>每个用例生成</span><el-input-number v-model="generateN" :min="1" :max="20" :precision="0" :disabled="busy" /></div><el-button type="primary" size="large" :loading="busy" :disabled="!selectedFile" @click="submit">上传并匹配场景</el-button></div>
    </section>
    <section class="jobs-card">
      <div class="section-title"><div><span class="eyebrow">AUGMENTATION QUEUE</span><h2>场景匹配与扩增</h2></div><el-button :disabled="busy || refreshing" @click="selectedJob ? refreshSelected() : review.loadJobs()">刷新</el-button></div>
      <el-alert v-if="listError" type="error" :title="listError" :closable="false" show-icon />
      <div v-if="jobs.length" class="job-tabs"><button v-for="job in jobs" :key="job.job_id" :class="{ active: selectedJob?.job_id === job.job_id }" :disabled="busy" @click="selectJob(job)"><b>{{ job.created_at.slice(0, 16).replace('T', ' ') }}</b><span>{{ statusText(job.status) }} · {{ job.result_count }} 条</span><small>{{ job.input_filename || job.job_id.slice(0, 12) }}</small></button></div>
      <el-alert v-if="detailError" type="error" :title="detailError" description="请点击刷新重试。" :closable="false" show-icon />
      <template v-if="selectedJob">
        <div class="job-meta"><span>{{ statusText(selectedJob.status) }} · {{ selectedJob.input_filename || selectedJob.job_id }}</span><span v-if="snapshotVersion" :title="snapshotVersion">场景树快照版本：{{ snapshotVersion }}</span></div>
        <CollectionBatchSubmission :key="selectedJob.job_id" :job="selectedJob" :result-count="activeResults.length" :busy="busy || loading || refreshing" :protect="review.protect" :run-protected="review.runExternalAction" />
        <div v-if="active" class="progress-card"><div><b>{{ stageText(selectedJob.stage) }}</b><span>{{ selectedJob.current_item || '等待执行' }} · {{ selectedJob.completed_items }} / {{ selectedJob.total_items || '待读取' }}</span></div><el-progress :percentage="selectedJob.percent" /></div>
        <div v-if="selectedJob.status === 'awaiting_confirmation'" class="confirmation-card"><div><b>场景匹配完成，等待确认扩增</b><p>{{ preview?.stats.total || 0 }} 条失败用例 · {{ preview?.stats.matched || 0 }} 条已关联 · {{ preview?.stats.unmatched || 0 }} 条未关联 · {{ preview?.stats.classification_failed || 0 }} 条分类失败</p><p v-if="preview?.stats.eligible">将为 {{ preview.stats.eligible }} 条可扩增用例分别生成 {{ selectedJob.generate_n }} 条变体。未关联用例仍按现有扩增规则处理；分类失败用例跳过。</p><p v-else>没有可扩增的用例，请检查下方分类结果。</p></div><el-button type="primary" :loading="busy" :disabled="!canStart || loading || refreshing" @click="review.start()">开始扩增</el-button></div>
        <details v-if="errors.length || selectedJob.error || selectedJob.warnings?.length" class="job-errors"><summary>{{ errors.length }} 项错误 · {{ selectedJob.warnings?.length || 0 }} 项提示</summary><p v-if="selectedJob.error">{{ selectedJob.error }}</p><p v-for="(error, index) in errors" :key="index">{{ error.item_id || '用例' }}{{ error.stage ? ` · ${stageText(error.stage)}` : '' }}：{{ error.error }}</p><p v-for="warning in selectedJob.warnings" :key="warning">{{ warning }}</p></details>
        <el-skeleton v-if="loading && !preview" :rows="5" animated />
        <el-alert v-else-if="preview && !preview.available" type="info" title="此历史作业没有场景预览记录" description="可继续审核和导出已有变体。上传失败用例创建新作业后，可查看与该作业快照对应的场景树。" :closable="false" show-icon />
        <template v-else-if="preview?.available">
          <div class="preview-toolbar"><h3>关联场景树</h3><div><span>{{ preview.stats.matched }} / {{ preview.stats.total }} 条已关联</span><el-button v-if="selectedNodeId || seedStatusFilter" link type="primary" :disabled="busy" @click="clearFilters">清除筛选</el-button></div></div>
          <div v-if="branchLabel || focusedSeed" class="filter-context"><span v-if="branchLabel">当前分支：{{ branchLabel }}</span><span v-if="focusedSeed">当前定位：Excel 第 {{ focusedSeed.source_row }} 行 · {{ focusedSeed.app }}</span></div>
          <div class="preview-layout">
            <div class="tree-panel"><AugmentationSceneTree v-if="preview.tree" :key="selectedJob.job_id" :tree="preview.tree" :seeds="seeds" :selected-node-id="selectedNodeId" :focused-seed-id="focusedSeedId" :focus-revision="focusRevision" :before-hide-selection="beforeHideTreeSelection" @select="selectNode" /><el-empty v-else description="正在读取该作业的场景树快照" :image-size="60" /></div>
          </div>
        </template>
        <div class="results-heading"><div><h3>变体审核</h3><p>当前 {{ resultGroups.length }} 个源用例 · {{ filteredResults.length }} 条变体；共 {{ activeResults.length }} 条未删除变体。展开源用例查看变体，导出包含全部未删除结果。</p></div><div class="review-actions"><el-button v-if="focusedSeedId || focusedResultId" link type="primary" :disabled="busy" @click="clearFocus">清除定位</el-button><el-button type="primary" plain :icon="Download" :disabled="busy || active || !activeResults.length" @click="exportResults">导出全部未删除结果</el-button></div></div>
        <div v-if="results.length" class="variant-groups">
          <section v-for="group in pagedGroups" :key="group.id" class="variant-group" :class="{ focused: group.id === focusedGroupId }" :data-group-id="group.id">
            <div class="variant-group-header">
              <button type="button" class="variant-group-toggle" :aria-expanded="expandedGroupIds.has(group.id)" :aria-controls="`variants-${selectedJob.job_id}-${group.id}`" :disabled="busy" @click="toggleGroup(group)">
                <span class="variant-group-arrow" aria-hidden="true">{{ expandedGroupIds.has(group.id) ? '▾' : '▸' }}</span>
                <span class="variant-group-summary">
                  <span class="variant-group-meta"><b>{{ group.app || 'App 未记录' }}</b><span>{{ group.sourceRow ? `Excel 第 ${group.sourceRow} 行` : '来源行号未记录' }}</span><span class="variant-group-count">未删除 {{ group.activeCount }} / 全部 {{ group.results.length }} 条变体</span></span>
                  <span class="variant-group-source">{{ group.sourceTask }}</span>
                  <small v-if="group.incompleteSource" class="variant-source-warning">历史来源记录不完整，单独展示</small>
                </span>
              </button>
              <el-button v-if="group.seedId && seedsById.has(group.seedId)" link type="primary" :disabled="busy" @click="locateSeed(seedsById.get(group.seedId)!)">定位场景</el-button>
            </div>
            <div v-if="expandedGroupIds.has(group.id)" :id="`variants-${selectedJob.job_id}-${group.id}`" class="variant-group-body">
        <el-table :data="pagedVariants(group)" row-key="result_id" :row-class-name="resultRowClass" stripe class="result-table" empty-text="当前筛选下没有变体任务" @row-click="locateResult">
          <el-table-column prop="用例编号" label="用例编号" width="165" />
          <el-table-column label="场景与 App" min-width="210"><template #default="{ row }"><b>{{ row.app }} · {{ row.scene }}</b><span class="cell-sub">{{ row.capability }} / {{ row.sub_capability }}</span><el-button v-if="row.seed_id && seedsById.has(row.seed_id)" link type="primary" :disabled="busy" @click.stop="locateSourceResult(row)">定位源用例</el-button></template></el-table-column>
          <el-table-column label="生成变体" min-width="300"><template #default="{ row }"><el-input v-if="editingId === row.result_id" v-model="editingText" type="textarea" :rows="3" :disabled="busy" @click.stop /><span v-else :class="{ deleted: row.deleted }">{{ row.task }}</span></template></el-table-column>
          <el-table-column label="审核" width="115"><template #default="{ row }"><el-tag :type="row.deleted ? 'info' : 'warning'">{{ row.deleted ? '已删除' : row['审核状态'] || '待人工Review' }}</el-tag></template></el-table-column>
          <el-table-column label="操作" width="140" fixed="right"><template #default="{ row }"><div @click.stop><template v-if="editingId === row.result_id"><el-button link type="primary" :disabled="busy" @click="review.saveEdit()">保存</el-button><el-button link :disabled="busy" @click="review.cancelEdit()">取消</el-button></template><template v-else><el-button link type="primary" :disabled="busy || active || row.deleted" @click="review.startEdit(row)">编辑</el-button><el-button link :type="row.deleted ? 'success' : 'danger'" :disabled="busy || active" @click="review.toggleDeleted(row)">{{ row.deleted ? '恢复' : '删除' }}</el-button></template></div></template></el-table-column>
        </el-table>
              <el-pagination v-if="group.results.length > pageSize" class="variant-pagination" :current-page="variantPage(group)" :page-size="pageSize" :total="group.results.length" :disabled="busy" layout="prev, pager, next, total" @update:current-page="changeVariantPage(group.id, $event)" />
            </div>
          </section>
          <el-empty v-if="!pagedGroups.length" description="当前筛选下没有变体任务" :image-size="65" />
        </div>
        <el-empty v-else :description="active ? '扩增完成后在这里审核变体' : selectedJob.status === 'awaiting_confirmation' ? '确认开始扩增后生成变体' : '作业没有生成可审核结果'" :image-size="65" />
        <el-pagination v-if="resultGroups.length > pageSize" class="group-pagination" :current-page="resultPage" :page-size="pageSize" :total="resultGroups.length" :disabled="busy" layout="prev, pager, next, total" @update:current-page="changeResultPage" />
        <section v-if="preview?.available" ref="seedPanel" class="seed-panel" aria-label="失败用例">
          <button type="button" class="seed-panel-toggle" :aria-expanded="seedPanelExpanded" :aria-controls="`seeds-${selectedJob.job_id}`" @click="seedPanelExpanded = !seedPanelExpanded">
            <span class="seed-panel-title"><span aria-hidden="true">{{ seedPanelExpanded ? '▾' : '▸' }}</span><b>失败用例</b><span>共 {{ preview.stats.total }} 条</span></span>
            <span class="seed-panel-counts"><span>未关联 {{ preview.stats.unmatched }}</span><span :class="{ 'seed-failure-count': preview.stats.classification_failed }">分类失败 {{ preview.stats.classification_failed }}</span><span v-if="selectedNodeId || seedStatusFilter" class="seed-filter-count">当前筛选 {{ filteredSeeds.length }} 条</span></span>
            <span class="seed-panel-action">{{ seedPanelExpanded ? '收起' : '展开' }}</span>
          </button>
          <div v-if="seedPanelExpanded" :id="`seeds-${selectedJob.job_id}`" class="seed-panel-body">
            <div class="seed-toolbar"><span>{{ filteredSeeds.length }} 条用例</span><el-select :model-value="seedStatusFilter" size="small" clearable placeholder="全部关联状态" aria-label="筛选场景关联状态" :disabled="busy" @update:model-value="changeSeedStatus"><el-option label="已关联" value="matched" /><el-option label="未关联" value="unmatched" /><el-option label="分类失败" value="failed" /></el-select></div>
            <el-table :data="pagedSeeds" row-key="seed_id" :row-class-name="seedRowClass" :max-height="280" size="small" class="seed-table" empty-text="当前筛选下没有失败用例" @row-click="locateSeed">
              <el-table-column prop="app" label="App" width="105" show-overflow-tooltip />
              <el-table-column label="失败任务" min-width="190"><template #default="{ row }"><span class="seed-task">{{ row.task }}</span></template></el-table-column>
              <el-table-column label="场景关联状态" width="132"><template #default="{ row }"><el-tag size="small" :type="row.mapping_status === 'matched' ? 'success' : row.mapping_status === 'classification_failed' ? 'danger' : 'info'">{{ seedMappingText(row) }}</el-tag></template></el-table-column>
              <el-table-column label="操作" width="160"><template #default="{ row }"><div class="seed-actions" @click.stop>
                <el-popover trigger="click" placement="top-end" :width="380" :popper-style="{ maxWidth: 'calc(100vw - 24px)' }" popper-class="seed-detail-popover">
                  <template #reference><el-button link type="primary" size="small">详情</el-button></template>
                  <div class="seed-detail-content">
                    <h4>失败用例详情</h4>
                    <dl>
                      <dt>App</dt><dd>{{ row.app || '未记录' }}</dd>
                      <dt>原始行号</dt><dd>{{ row.source_row ? `Excel 第 ${row.source_row} 行` : '未记录' }}</dd>
                      <dt>失败任务</dt><dd class="seed-detail-task">{{ row.task }}</dd>
                      <dt>分类来源</dt><dd>{{ row.classification_source === 'excel' ? '沿用表中分类' : row.classification_source === 'model' ? '模型分类' : '未记录' }}</dd>
                      <dt>场景关联</dt><dd>{{ seedMappingText(row) }}</dd>
                      <dt>场景路径</dt><dd>{{ seedPath(row) || '未记录' }}</dd>
                      <dt>扩增情况</dt><dd>{{ generationText(row) }} · {{ row.result_count }} 条变体</dd>
                      <template v-if="row.reason"><dt>匹配说明</dt><dd>{{ row.reason }}</dd></template>
                      <template v-if="row.error"><dt>错误原因</dt><dd class="seed-error">{{ row.error }}</dd></template>
                    </dl>
                  </div>
                </el-popover>
                <el-button link type="primary" size="small" :disabled="busy" @click="locateSeed(row)">定位场景与变体</el-button>
              </div></template></el-table-column>
            </el-table>
            <el-pagination v-if="filteredSeeds.length > pageSize" class="seed-pagination" v-model:current-page="seedPage" :page-size="pageSize" :total="filteredSeeds.length" layout="prev, pager, next, total" small />
          </div>
        </section>
      </template><el-empty v-else description="上传失败用例后在这里查看场景归属" />
    </section>
  </div>
</template>

<style scoped>
.seed-panel { margin-top: 24px; border: 1px solid var(--line); border-radius: 9px; overflow: hidden; }
.seed-panel-toggle { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 18px; width: 100%; min-height: 46px; padding: 10px 12px; border: 0; background: #f8fafc; color: var(--muted); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.seed-panel-toggle:hover { background: #f0fdfa; }
.seed-panel-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.seed-panel-title, .seed-panel-counts { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; }
.seed-panel-title b { color: var(--ink); font-size: 13px; }
.seed-panel-title > span:first-child, .seed-panel-action, .seed-filter-count { color: var(--accent-deep); }
.seed-panel-counts { flex: 1; }
.seed-panel-action { margin-left: auto; }
.seed-failure-count { color: #b4533c; }
.seed-panel-body { padding: 12px; border-top: 1px solid var(--line); }
.seed-task { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-height: 20px; overflow-wrap: anywhere; }
.seed-actions { display: flex; align-items: center; gap: 10px; }
.seed-actions :deep(.el-button) { margin-left: 0; }
.seed-table :deep(.el-table__cell) { padding: 6px 0; }
.seed-panel .seed-pagination { margin: 10px 0 0; }
.seed-detail-content { max-height: min(420px, 60vh); overflow: auto; overscroll-behavior: contain; color: var(--ink); font-size: 12px; line-height: 1.7; }
.seed-detail-content h4 { margin: 0 0 12px; font-size: 14px; }
.seed-detail-content dl { display: grid; grid-template-columns: 62px minmax(0, 1fr); gap: 8px 12px; margin: 0; }
.seed-detail-content dt { color: var(--muted); }
.seed-detail-content dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 660px) { .seed-panel-counts { flex-basis: 50%; }.seed-panel-body { padding: 10px 8px; } }
.variant-groups { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; }
.variant-group { min-width: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.variant-group.focused { border-color: var(--accent); }
.result-table :deep(tr.el-table__row.focused-row > td.el-table__cell) { background: #edf8f4; }
.review-actions { display: flex; flex-shrink: 0; flex-wrap: wrap; align-items: center; gap: 8px; }
.variant-group-header { display: flex; align-items: center; gap: 12px; padding-right: 14px; background: #f8fafc; }
.variant-group-toggle { display: flex; flex: 1; align-items: flex-start; gap: 10px; min-width: 0; padding: 14px; border: 0; background: transparent; color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
.variant-group-toggle:hover { background: #f0fdfa; }
.variant-group-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
.variant-group-toggle:disabled { cursor: wait; opacity: .65; }
.variant-group-arrow { flex: 0 0 16px; color: var(--accent-deep); line-height: 20px; }
.variant-group-summary { display: grid; gap: 7px; min-width: 0; }
.variant-group-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; color: var(--muted); font-size: 11px; }
.variant-group-meta b { color: var(--ink); font-size: 12px; }
.variant-group-count { color: var(--accent-deep); }
.variant-group-source { font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.variant-source-warning { color: #b45309; font-size: 11px; }
.variant-group-body { padding: 0 12px; border-top: 1px solid var(--line); }
.variant-group-header > .el-button { flex-shrink: 0; }
.augmentation-page :deep(.el-table) { max-width: 100%; }
.augmentation-page{width:min(1680px,100%);margin:0 auto}.upload-card,.jobs-card{border:1px solid var(--line);border-radius:14px;background:#fff}.upload-card{display:flex;align-items:center;justify-content:space-between;gap:28px;margin:20px 0;padding:22px}.upload-copy{max-width:670px}.upload-copy h2{margin:5px 0 8px;font-size:21px}.upload-copy p{margin:0;color:var(--muted);font-size:13px;line-height:1.75}.upload-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end}.file-picker{display:flex;align-items:center;gap:8px;max-width:230px;padding:11px 14px;border:1px dashed #94a3b8;border-radius:8px;color:#475569;font-size:12px;cursor:pointer}.file-picker span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-picker input{position:absolute;opacity:0;width:1px;height:1px}.file-picker:has(input:disabled){opacity:.5;cursor:default}.count-control{display:grid;gap:4px;color:var(--muted);font-size:11px}.jobs-card{padding:18px 20px}.section-title,.preview-toolbar,.results-heading{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:14px}.section-title h2{margin:4px 0 0;font-size:20px}h3{margin:0;font-size:15px}.job-tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:12px}.job-tabs button{display:grid;gap:4px;min-width:170px;max-width:260px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink);text-align:left;cursor:pointer}.job-tabs button.active{border-color:#5eead4;background:#f0fdfa}.job-tabs span,.job-tabs small{color:var(--muted);font-size:11px}.job-tabs small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.job-meta{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;margin:10px 0 16px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.progress-card{display:flex;align-items:center;gap:20px;margin-bottom:12px;padding:13px 15px;border:1px solid #bfdbfe;border-radius:9px;background:#eff6ff}.progress-card>div{display:grid;gap:4px;min-width:240px}.progress-card span{color:var(--muted);font-size:12px}.progress-card .el-progress{flex:1}.confirmation-card{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:16px 18px;margin-bottom:18px;border:1px solid #a5d8cb;border-radius:9px;background:#f3faf7}.confirmation-card b{font-size:14px}.confirmation-card p,.results-heading p{margin:7px 0 0;color:var(--muted);font-size:12px;line-height:1.7}.confirmation-card>.el-button{flex-shrink:0}.job-errors{margin:12px 0;padding:10px 14px;border:1px solid #edd9c6;border-radius:8px;color:#895928;font-size:12px}.job-errors summary{cursor:pointer}.job-errors p{line-height:1.7;overflow-wrap:anywhere}.preview-toolbar{margin:24px 0 12px}.preview-toolbar>div{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--muted)}.filter-context{display:flex;flex-wrap:wrap;gap:7px 18px;margin-bottom:12px;padding:10px 12px;background:#f4f8f7;color:#416355;font-size:12px;line-height:1.6}.preview-layout{display:grid;grid-template-columns:1fr;gap:18px;align-items:start}.tree-panel{min-width:0}.seed-panel{min-width:0}.seed-toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;color:var(--muted);font-size:12px}.seed-toolbar .el-select{width:175px}.seed-table p{margin:7px 0;line-height:1.65;overflow-wrap:anywhere}.seed-ident{display:grid;gap:4px}.seed-ident b{font-size:12px}.seed-ident span{font-size:10px;color:var(--muted)}.seed-error{color:#b4533c;font-size:12px}.seed-table :deep(.focused-row){--el-table-tr-bg-color:#edf8f4}.seed-table :deep(.el-table__row){cursor:pointer}.results-heading{margin-top:28px;padding-top:20px;border-top:1px solid var(--line);align-items:flex-start}.results-heading>.el-button{flex-shrink:0}.result-table{margin-top:12px}.cell-sub{display:block;margin-top:5px;color:var(--muted);font-size:11px;line-height:1.65;overflow-wrap:anywhere}.deleted{color:#94a3b8;text-decoration:line-through}.el-alert{margin:10px 0}.el-pagination{margin:16px 0;justify-content:flex-end}.el-skeleton{padding:20px 0}@media(max-width:1200px){.upload-card{align-items:stretch;flex-direction:column}.upload-actions{justify-content:flex-start}}@media(max-width:900px){.preview-layout{grid-template-columns:1fr}.progress-card{align-items:stretch;flex-direction:column;gap:8px}.progress-card .el-progress{width:100%}.confirmation-card,.results-heading{align-items:flex-start;flex-direction:column}.jobs-card{padding:16px 12px}.preview-toolbar{align-items:flex-start;flex-wrap:wrap}}
</style>
