<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '@/api'
import type { QualitySettings, QualityEvaluatorConfig } from '@/types'

const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ ready: [value: boolean] }>()
const settings = ref<QualitySettings | null>(null), busy = ref(false), error = ref('')
const emptyModels = (): QualityEvaluatorConfig => ({ text: { base_url: '', model: '', api_key_env: '' }, vision: { base_url: '', model: '', api_key_env: '' } })
const models = ref<QualityEvaluatorConfig>(emptyModels())
const roles = [{ key: 'text', label: '文本模型' }, { key: 'vision', label: '视觉模型' }] as const
const cleanModels = (config: QualityEvaluatorConfig): QualityEvaluatorConfig => Object.fromEntries(roles.map(({ key }) => [key, {
  base_url: config[key].base_url.trim(), model: config[key].model.trim(), api_key_env: (config[key].api_key_env || '').trim(),
}])) as unknown as QualityEvaluatorConfig
const modelDirty = computed(() => JSON.stringify(cleanModels(models.value)) !== JSON.stringify(cleanModels(settings.value?.evaluator_config || emptyModels())))
let version = 0, disposed = false
const selectedProfile = computed(() => settings.value?.profiles.find(profile => profile.id === settings.value?.evaluation_profile_id && profile.digest === settings.value?.evaluation_profile_digest))
function notifyReady() { emit('ready', Boolean(!busy.value && !error.value && !modelDirty.value && settings.value?.supported && settings.value?.model_configured && selectedProfile.value)) }
watch(modelDirty, notifyReady)
function accept(result: QualitySettings, resetModels: boolean) {
  settings.value = result
  if (resetModels) models.value = cleanModels(result.evaluator_config || emptyModels())
}
async function load() {
  if (busy.value) return
  const token = ++version
  busy.value = true; error.value = ''; notifyReady()
  try { const result = await api.qualitySettings(); if (!disposed && token === version) accept(result, true) }
  catch (cause) { if (!disposed && token === version) error.value = (cause as Error).message }
  finally { if (!disposed && token === version) { busy.value = false; notifyReady() } }
}
async function save(patch: Parameters<typeof api.saveQualitySettings>[0], resetModels = false) {
  if (props.disabled || busy.value) return
  const token = ++version
  busy.value = true; error.value = ''; notifyReady()
  try { const result = await api.saveQualitySettings(patch); if (!disposed && token === version) accept(result, resetModels) }
  catch (cause) { if (!disposed && token === version) error.value = (cause as Error).message }
  finally { if (!disposed && token === version) { busy.value = false; notifyReady() } }
}
async function selectProfile(id: string) {
  const profile = settings.value?.profiles.find(item => item.id === id)
  if (profile) await save({ evaluation_profile_id: profile.id, evaluation_profile_digest: profile.digest })
}
async function saveModels() {
  if (props.disabled || busy.value) return
  const config = cleanModels(models.value)
  for (const role of roles) {
    if (!config[role.key].base_url || !config[role.key].model) { error.value = `${role.label}需要同时填写接口地址和实际模型名`; notifyReady(); return }
  }
  await save({ evaluator_config: config }, true)
}
onMounted(() => void load())
onBeforeUnmount(() => { disposed = true; ++version })
</script>

<template>
  <section class="fusion-settings" aria-label="质检评测配置">
    <div><b>Rubric + Checklist 融合质检</b><p>Checklist 在平台后端执行，使用下方独立的文本与视觉模型。保存仅影响后续新任务，提交时冻结规则和模型配置，已有任务保持原配置。</p></div>
    <div class="fusion-settings__controls"><label for="quality-profile">Checklist 规则</label><el-select id="quality-profile" :model-value="settings?.evaluation_profile_id || ''" :loading="busy" :disabled="disabled || busy || !settings?.supported" placeholder="选择评测规则" @change="selectProfile"><el-option v-for="profile in settings?.profiles || []" :key="profile.id" :value="profile.id" :label="profile.name || profile.id" /></el-select><el-button text :loading="busy" @click="load">刷新配置</el-button></div>
    <div class="fusion-settings__models">
      <fieldset v-for="role in roles" :key="role.key" :disabled="disabled || busy || !settings?.supported"><legend>{{ role.label }}</legend>
        <label>接口地址<el-input v-model="models[role.key].base_url" placeholder="http://模型服务/v1" /></label>
        <label>模型名<el-input v-model="models[role.key].model" placeholder="服务实际模型 ID" /></label>
        <label>密钥环境变量名（可选）<el-input v-model="models[role.key].api_key_env" placeholder="环境变量名称，不填写密钥" /></label>
      </fieldset>
    </div>
    <div class="fusion-settings__controls"><el-button :loading="busy" :disabled="disabled || busy || !settings?.supported" @click="saveModels">保存质检模型</el-button><span v-if="modelDirty">模型配置尚未保存</span></div>
    <p v-if="error || settings?.reason" class="fusion-settings__error" role="status">{{ error || settings?.reason }}</p>
    <p v-else-if="settings?.supported && !selectedProfile" class="fusion-settings__error">请选择本次质检使用的 Checklist 规则。</p>
    <p v-else-if="settings?.supported && !settings.model_configured" class="fusion-settings__error">请配置并保存文本和视觉模型。</p>
  </section>
</template>

<style scoped>
.fusion-settings{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 20px;padding:14px 16px;margin-top:18px;border:1px solid #bfdbfe;border-radius:12px;background:#f8fbff}.fusion-settings p{margin:4px 0 0;font-size:12px;color:#64748b}.fusion-settings__controls{display:flex;align-items:center;flex-wrap:wrap;gap:9px;font-size:12px}.fusion-settings__controls .el-select{width:220px}.fusion-settings .fusion-settings__error{width:100%;color:#b45309;overflow-wrap:anywhere}.fusion-settings__models{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;width:100%}.fusion-settings__models fieldset{min-width:0;border:1px solid #cbd5e1;border-radius:8px;display:grid;gap:8px}.fusion-settings__models label{display:grid;gap:4px;font-size:12px;color:#475569}.fusion-settings__models legend{font-size:13px;font-weight:600}@media(max-width:720px){.fusion-settings__models{grid-template-columns:1fr}}
</style>
