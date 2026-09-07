<script setup lang="ts">
import { ref } from 'vue'
import type { TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import type { ScenarioEditorState } from '@/utils/scenarioStudio'
import ScenarioTreeWorkbench from '@/components/ScenarioTreeWorkbench.vue'

interface EditorHandle {
  beginEdit: () => void
  undo: () => void
  redo: () => void
  save: () => Promise<void>
  discardChanges: () => Promise<boolean>
  focusNode: (target: string | TaskGenerationTreeNode) => void
}

const props = defineProps<{
  tree: TaskGenerationTreeNode[]
  version: string
  initialNodeId?: string
}>()
const emit = defineEmits<{
  (event: 'tree-saved', payload: TaskGenerationTree): void
  (event: 'state-change', state: ScenarioEditorState): void
  (event: 'selection-change', sceneId: string): void
}>()

const workbench = ref<EditorHandle | null>(null)

defineExpose({
  beginEdit: () => workbench.value?.beginEdit(),
  undo: () => workbench.value?.undo(),
  redo: () => workbench.value?.redo(),
  save: () => workbench.value?.save(),
  discardChanges: () => workbench.value?.discardChanges() ?? Promise.resolve(true),
  focusNode: (target: string | TaskGenerationTreeNode) => workbench.value?.focusNode(target),
})
</script>

<template>
  <div class="scenario-editor-wrapper">
    <ScenarioTreeWorkbench
      ref="workbench"
      :initial-tree="props.tree"
      :initial-version="props.version"
      :initial-node-id="props.initialNodeId"
      @tree-saved="emit('tree-saved', $event)"
      @state-change="emit('state-change', $event)"
      @selection-change="emit('selection-change', $event)"
    />
  </div>
</template>
