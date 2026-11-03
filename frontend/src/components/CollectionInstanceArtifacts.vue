<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ArtifactDetail, createFactoryApi } from '@/phoneFactoryApi'
const props = defineProps<{ api: ReturnType<typeof createFactoryApi>; runId: string; artifactId: string; executionId?: string }>()
const opened = ref(false), busy = ref(false), error = ref(''), detail = ref<ArtifactDetail>(), page = ref(1)
const files = computed(() => (detail.value?.files || []).slice((page.value - 1) * 10, page.value * 10))
let request: AbortController | undefined, generation = 0
async function load() {
  if (!props.artifactId || !opened.value) return
  request?.abort(); const controller = request = new AbortController(), epoch = ++generation
  busy.value = true; error.value = ''
  const timer = setTimeout(() => controller.abort(), 15000)
  try {
    const value = await props.api.artifactDetail(props.runId, props.artifactId, controller.signal, props.executionId)
    if (epoch === generation && !controller.signal.aborted) detail.value = value
  } catch (cause) { if (epoch === generation) error.value = controller.signal.aborted ? '原件清单读取超时' : (cause as Error).message }
  finally { clearTimeout(timer); if (epoch === generation) busy.value = false }
}
watch(opened, value => { if (value && !detail.value) void load() })
watch(() => [props.runId, props.artifactId, props.executionId], () => { generation++; request?.abort(); opened.value = false; detail.value = undefined; error.value = ''; page.value = 1; busy.value = false })
onBeforeUnmount(() => { generation++; request?.abort() })
</script>
<template>
  <details class="instance-artifacts" :open="opened" @toggle="opened = ($event.target as HTMLDetailsElement).open"><summary>查看本次原件</summary><p v-if="busy">读取中…</p><p v-if="error" role="alert">{{ error }} <el-button link @click="load">重试</el-button></p><ul><li v-for="file in files" :key="file.path"><a :href="api.artifactFileUrl(runId, artifactId, file.path, executionId)" :download="file.path.split('/').at(-1)">{{ file.path }}</a><small> {{ file.size }} B</small></li></ul><el-pagination v-if="(detail?.files.length || 0) > 10" v-model:current-page="page" :page-size="10" :total="detail?.files.length || 0" layout="prev, pager, next" /></details>
</template>
<style scoped>.instance-artifacts{margin:10px 0}summary{cursor:pointer}ul{list-style:none;padding-left:12px}li{margin:8px 0;overflow-wrap:anywhere}small{color:var(--muted)}</style>
