<script setup lang="ts">
import type { AgentCapabilities, AgentType } from '@/phoneFactoryApi'
defineProps<{ modelValue: AgentType; capabilities: AgentCapabilities | null; disabled?: boolean }>()
defineEmits<{ 'update:modelValue': [value: AgentType] }>()
</script>

<template>
  <el-select :model-value="modelValue" :disabled="disabled" aria-label="采集引擎" @update:model-value="$emit('update:modelValue', $event)">
    <el-option value="newagent" label="NewAgent" :disabled="capabilities?.newagent?.available === false" />
    <el-option value="pipelineagent" label="PipelineAgent" :disabled="!capabilities?.pipelineagent?.available" />
  </el-select>
</template>
