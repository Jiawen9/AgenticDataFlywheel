<template>
  <div class="pipeline-model-fields" data-testid="pipeline-model-fields">
    <fieldset>
      <legend>执行模型（VLA）</legend>
      <p>使用上方 VLA 接口；模型名留空时沿用采集端配置。</p>
      <label>执行模型名<el-input :model-value="modelValue.execution_model" :disabled="disabled" placeholder="服务实际提供的模型名（可留空）" data-testid="execution-model" @update:model-value="update('execution_model', $event)" /></label>
    </fieldset>
    <fieldset>
      <legend>规划模型</legend>
      <el-radio-group :model-value="modelValue.planner_custom" :disabled="disabled" data-testid="planner-mode" @update:model-value="update('planner_custom', Boolean($event))">
        <el-radio :value="false">沿用采集端</el-radio><el-radio :value="true">自定义</el-radio>
      </el-radio-group>
      <template v-if="modelValue.planner_custom">
        <label>接口地址<el-input :model-value="modelValue.planner_base_url" :disabled="disabled" placeholder="http(s)://host:port/v1" data-testid="planner-base-url" @update:model-value="update('planner_base_url', $event)" /></label>
        <label>模型名<el-input :model-value="modelValue.planner_model" :disabled="disabled" placeholder="服务实际提供的模型名" data-testid="planner-model" @update:model-value="update('planner_model', $event)" /></label>
        <details><summary>高级选项</summary><label>密钥环境变量名<el-input :model-value="modelValue.planner_api_key_env" :disabled="disabled" placeholder="留空沿用采集端认证" @update:model-value="update('planner_api_key_env', $event)" /></label><p>仅填写采集服务器上的变量名，不填写密钥。</p></details>
      </template>
      <p>仅调整初始规划；重规划、检查、监督和评测模型继续使用各自配置。</p>
    </fieldset>
  </div>
</template>
<script setup lang="ts">
import type { PipelineModelDraft } from '@/utils/pipelineModels'
const props = defineProps<{ modelValue: PipelineModelDraft; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: PipelineModelDraft] }>()
function update<K extends keyof PipelineModelDraft>(key: K, value: PipelineModelDraft[K]) { emit('update:modelValue', { ...props.modelValue, [key]: value }) }
</script>
<style scoped>
.pipeline-model-fields { display: grid; gap: 12px; margin: 16px 0; min-width: 0; }
fieldset { border: 1px solid var(--line, #dce3ed); border-radius: 10px; padding: 12px; min-width: 0; }
legend { font-weight: 600; font-size: 14px; } fieldset > label, details > label { display: grid; grid-template-columns: 115px minmax(0, 1fr); gap: 12px; align-items: center; margin-top: 10px; font-size: 13px; }
p { color: var(--muted, #64748b); font-size: 12px; line-height: 1.6; margin: 8px 0 0; } summary { cursor: pointer; font-size: 13px; margin-top: 12px; }
@media(max-width:760px) { fieldset > label, details > label { grid-template-columns: 1fr; gap: 6px; } }
</style>
