<template>
  <span v-if="id" class="display-id">
    <el-popover trigger="click" placement="top" :width="340">
      <template #reference><button type="button" class="display-id-label" :title="`查看完整编号：${id}`" :aria-label="`查看完整编号 ${label}`" @click.stop @keydown.stop>{{ label }}</button></template>
      <p class="full-id-caption">完整编号</p><code class="full-id">{{ id }}</code>
      <p v-if="copyError" role="alert" class="copy-error">{{ copyError }}</p>
    </el-popover>
    <button v-if="copyable" type="button" class="copy-id" :title="copied ? '已复制完整编号' : '复制完整编号'" :aria-label="`复制完整编号 ${label}`" @click.stop="copy" @keydown.stop>{{ copied ? '✓' : '⧉' }}</button>
    <span v-if="copied" class="sr-only" role="status">已复制完整编号</span>
  </span>
  <span v-else>—</span>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useDisplayIdentifiers, type DisplayIdentifierKind } from '@/composables/useDisplayIdentifiers'
const props = withDefaults(defineProps<{ kind: DisplayIdentifierKind; id?: string | null; scope?: string | null; copyable?: boolean }>(), { copyable: true })
const { displayId } = useDisplayIdentifiers()
const label = computed(() => displayId(props.kind, props.id, props.scope))
const copied = ref(false), copyError = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
async function copy() {
  if (!props.id) return
  copyError.value = ''
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(props.id)
    else {
      const input = document.createElement('textarea')
      input.value = props.id; input.style.position = 'fixed'; input.style.opacity = '0'
      document.body.appendChild(input)
      try { input.select(); if (!document.execCommand('copy')) throw new Error('copy unavailable') }
      finally { input.remove() }
    }
    copied.value = true; clearTimeout(timer); timer = setTimeout(() => { copied.value = false }, 1800)
  } catch { copyError.value = '复制失败，请点击编号查看并手动复制。' }
}
onBeforeUnmount(() => { clearTimeout(timer) })
</script>

<style scoped>
.display-id { display: inline-flex; align-items: center; gap: 5px; max-width: 100%; min-width: 0; vertical-align: middle; }
.display-id-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.display-id-label, .copy-id { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; cursor: pointer; }
.display-id-label:hover { color: var(--el-color-primary, #2563eb); text-decoration: underline; }
.display-id-label:focus-visible, .copy-id:focus-visible { outline: 2px solid var(--el-color-primary, #2563eb); outline-offset: 2px; }
.copy-id { flex: none; color: var(--muted, #64748b); padding: 2px; }
.full-id { display: block; white-space: normal; overflow-wrap: anywhere; user-select: all; }
.full-id-caption { margin: 0 0 8px; font-weight: 600; }.copy-error { color: #b91c1c; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
</style>
