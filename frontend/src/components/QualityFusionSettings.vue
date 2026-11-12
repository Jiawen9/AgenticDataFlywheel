<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '@/api'
import type { QualitySettings } from '@/types'

const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ ready: [value: boolean] }>()
const settings = ref<QualitySettings | null>(null), busy = ref(false), error = ref('')
let version = 0, disposed = false
const selectedProfile = computed(() => settings.value?.profiles.find(profile => profile.id === settings.value?.evaluation_profile_id && profile.digest === settings.value?.evaluation_profile_digest))
function notifyReady() { emit('ready', Boolean(!busy.value && !error.value && settings.value?.supported && settings.value?.model_configured !== false && selectedProfile.value)) }
async function load() {
  if (busy.value) return
  const token = ++version
  busy.value = true; error.value = ''; notifyReady()
  try { const result = await api.qualitySettings(); if (!disposed && token === version) settings.value = result }
  catch (cause) { if (!disposed && token === version) error.value = (cause as Error).message }
  finally { if (!disposed && token === version) { busy.value = false; notifyReady() } }
}
async function selectProfile(id: string) {
  if (props.disabled || busy.value) return
  const profile = settings.value?.profiles.find(item => item.id === id)
  if (!profile) return
  const token = ++version
  busy.value = true; error.value = ''; notifyReady()
  try {
    const result = await api.saveQualitySettings({ evaluation_profile_id: profile.id, evaluation_profile_digest: profile.digest })
    if (!disposed && token === version) settings.value = result
  } catch (cause) { if (!disposed && token === version) error.value = (cause as Error).message }
  finally { if (!disposed && token === version) { busy.value = false; notifyReady() } }
}
onMounted(() => void load())
onBeforeUnmount(() => { disposed = true; ++version })
</script>

<template>
  <section class="fusion-settings" aria-label="质检评测配置">
    <div><b>Rubric + Checklist 融合质检</b><p>评测模型沿用模型迭代配置。新任务提交时冻结规则与模型配置，已有结果保持原样。</p></div>
    <div class="fusion-settings__controls"><label for="quality-profile">Checklist 规则</label><el-select id="quality-profile" :model-value="settings?.evaluation_profile_id || ''" :loading="busy" :disabled="disabled || busy || !settings?.supported" placeholder="选择评测规则" @change="selectProfile"><el-option v-for="profile in settings?.profiles || []" :key="profile.id" :value="profile.id" :label="profile.name || profile.id" /></el-select><el-button text :loading="busy" @click="load">刷新配置</el-button><router-link to="/model-iteration-evaluation">配置评测模型</router-link></div>
    <p v-if="error || settings?.reason" class="fusion-settings__error" role="status">{{ error || settings?.reason }}</p>
    <p v-else-if="settings?.supported && !selectedProfile" class="fusion-settings__error">请选择本次质检使用的 Checklist 规则。</p>
  </section>
</template>

<style scoped>
.fusion-settings{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 20px;padding:14px 16px;margin-top:18px;border:1px solid #bfdbfe;border-radius:12px;background:#f8fbff}.fusion-settings p{margin:4px 0 0;font-size:12px;color:#64748b}.fusion-settings__controls{display:flex;align-items:center;flex-wrap:wrap;gap:9px;font-size:12px}.fusion-settings__controls .el-select{width:220px}.fusion-settings__controls a{color:#2563eb}.fusion-settings .fusion-settings__error{width:100%;color:#b45309;overflow-wrap:anywhere}
</style>
