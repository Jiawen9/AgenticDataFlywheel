<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useExternalDatasetImport } from '@/composables/useExternalDatasetImport'
import type { DatasetImportIssue } from '@/datasetImportApi'
import type { DatasetRelease } from '@/types'

const emit = defineEmits<{ released: [release: DatasetRelease] }>()
const { visible, name, file, fields, preview, sheets, previewing, publishing, error, canPublish, selectFile, open, close, prepare, publish, dispose } = useExternalDatasetImport()
const fileInput = ref<HTMLInputElement>()
function chooseFile(event: Event) { selectFile((event.target as HTMLInputElement).files?.[0] ?? null) }
function beforeClose(done: () => void) { if (close()) done() }
function issueLocation(issue: DatasetImportIssue) {
  return [issue.sheet, issue.row == null ? '' : '第 ' + issue.row + ' 行', issue.field].filter(Boolean).join(' · ')
}
async function publishFile() {
  const release = await publish()
  if (release) {
    if (fileInput.value) fileInput.value.value = ''
    emit('released', release)
  }
}
onBeforeRouteLeave(() => {
  if (!publishing.value) return true
  ElMessage.warning('表格正在发布，请稍候')
  return false
})
onBeforeUnmount(dispose)
</script>

<template>
  <el-button data-testid="external-import-open" :icon="Upload" @click="open">上传外部表格</el-button>
  <el-dialog v-model="visible" title="上传外部表格并发布" width="min(850px, 94vw)" data-testid="external-import-dialog" :before-close="beforeClose" :show-close="!publishing" :close-on-click-modal="!publishing" :close-on-press-escape="!publishing">
    <div class="external-import">
      <p class="intro">选择已有的轨迹 Excel，预览并校验后发布到数据集。</p>
      <fieldset :disabled="publishing" class="import-fields">
        <label class="full-width">数据集名称<input v-model="name" data-testid="external-import-name" maxlength="120" placeholder="输入数据集名称" /></label>
        <label class="full-width">Excel 文件<input ref="fileInput" type="file" accept=".xlsx,.xlsm" data-testid="external-import-file" @change="chooseFile" /><small>{{ file ? file.name : '支持 .xlsx、.xlsm，最大 50 MiB' }}</small></label>
        <label>数据来源<input v-model="fields.data_source" data-testid="external-import-data-source" maxlength="120" /></label>
        <label>数据日期<input v-model="fields.data_date" type="date" data-testid="external-import-data-date" /></label>
        <p class="classification-hint full-width">表内分类优先，以下输入仅补全缺失值；表格与输入都未提供时，App / 场景归入未记录 / 未分类。</p>
        <label>App（可选）<input v-model="fields.app" data-testid="external-import-app" placeholder="仅补充表格中缺失的 App" /></label>
        <label>一级场景（可选）<input v-model="fields.level1" data-testid="external-import-level1" placeholder="仅补充缺失的一级场景" /></label>
        <label>二级场景（可选）<input v-model="fields.level2" data-testid="external-import-level2" placeholder="仅补充缺失的二级场景" /></label>
        <label v-if="sheets.length">工作表<select v-model="fields.sheet_name" data-testid="external-import-sheet"><option value="">使用活动工作表</option><option v-for="sheet in sheets" :key="sheet" :value="sheet">{{ sheet }}</option></select></label>
      </fieldset>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon data-testid="external-import-error" />
      <template v-if="preview">
        <el-alert v-if="preview.duplicate_release" type="info" :closable="false" show-icon data-testid="external-import-duplicate" :title="'检测到已发布的相同内容：' + preview.duplicate_release.name + '（' + preview.duplicate_release.release_id + '）'" />
        <section class="preview-summary" data-testid="external-import-summary">
          <div class="preview-heading"><h3>预览结果</h3><el-tag :type="preview.valid ? 'success' : 'danger'">{{ preview.valid ? '校验通过' : '校验未通过' }}</el-tag></div>
          <p>工作表：{{ preview.sheet_name || '—' }} · {{ preview.summary.trajectory_count }} 条轨迹 · {{ preview.summary.step_count }} 步</p>
          <div v-if="preview.summary.apps.length || preview.summary.scenes.length" class="summary-tables">
            <table v-if="preview.summary.apps.length"><caption>App 分布</caption><thead><tr><th>App</th><th>轨迹数</th><th>步骤数</th></tr></thead><tbody><tr v-for="item in preview.summary.apps" :key="item.name"><td>{{ item.name || '未填写' }}</td><td>{{ item.trajectory_count }}</td><td>{{ item.step_count }}</td></tr></tbody></table>
            <table v-if="preview.summary.scenes.length"><caption>场景分布</caption><thead><tr><th>一级场景</th><th>二级场景</th><th>轨迹数</th><th>步骤数</th></tr></thead><tbody><tr v-for="item in preview.summary.scenes" :key="item.level1 + '/' + item.level2"><td>{{ item.level1 || '未填写' }}</td><td>{{ item.level2 || '未填写' }}</td><td>{{ item.trajectory_count }}</td><td>{{ item.step_count }}</td></tr></tbody></table>
          </div>
        </section>
        <section v-if="preview.errors.length" class="issue-list errors" data-testid="external-import-errors"><h3>需要修正的问题</h3><ul><li v-for="(issue, index) in preview.errors" :key="index"><span>{{ issueLocation(issue) }}</span>{{ issue.message }}</li></ul></section>
        <section v-if="preview.warnings.length" class="issue-list warnings" data-testid="external-import-warnings"><h3>提示</h3><ul><li v-for="(issue, index) in preview.warnings" :key="index"><span>{{ issueLocation(issue) }}</span>{{ issue.message }}</li></ul></section>
      </template>
    </div>
    <template #footer>
      <el-button data-testid="external-import-cancel" :disabled="publishing" @click="close">取消</el-button>
      <el-button data-testid="external-import-preview" :loading="previewing" :disabled="publishing || !file" @click="prepare">{{ preview ? '重新预览' : '预览表格' }}</el-button>
      <el-button type="primary" data-testid="external-import-publish" :loading="publishing" :disabled="!canPublish" @click="publishFile">发布数据集</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.external-import { display: grid; gap: 16px; }
.intro { margin: 0; color: var(--muted); font-size: 13px; }
.import-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; border: 0; padding: 0; margin: 0; min-width: 0; }
.import-fields label { display: grid; gap: 6px; color: var(--ink); font-size: 13px; min-width: 0; }
.import-fields .full-width { grid-column: 1 / -1; }
.import-fields input, .import-fields select { min-width: 0; width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; background: white; color: var(--ink); }
.import-fields input:focus, .import-fields select:focus { outline: 2px solid color-mix(in srgb, var(--accent) 30%, transparent); border-color: var(--accent-deep); }
.import-fields input:disabled, .import-fields select:disabled { cursor: not-allowed; opacity: .65; }
.classification-hint { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.import-fields small { color: var(--muted); overflow-wrap: anywhere; }
.preview-summary, .issue-list { border: 1px solid var(--line); border-radius: 9px; padding: 14px; }
.preview-heading { display: flex; justify-content: space-between; align-items: center; }
h3 { margin: 0; font-size: 15px; }
.preview-summary p { color: var(--muted); font-size: 13px; }
.summary-tables { display: grid; gap: 12px; max-height: 260px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
caption { text-align: left; font-weight: 600; margin-bottom: 6px; }
th, td { text-align: left; padding: 7px; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
th { color: var(--muted); font-weight: 500; }
.issue-list ul { margin: 10px 0 0; padding-left: 20px; max-height: 200px; overflow-y: auto; font-size: 13px; }
.issue-list li { margin-top: 6px; overflow-wrap: anywhere; }
.issue-list span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 3px; }
.errors { color: #b91c1c; }.warnings { color: #92400e; }
@media (max-width: 650px) { .import-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
