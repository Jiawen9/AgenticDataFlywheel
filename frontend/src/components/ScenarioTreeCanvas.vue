<script setup lang="ts">
import { MoreFilled, Plus } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'
import { NODE_LEVELS, type ScenarioTreeDisplayNode, type TreeAddActionNode } from '@/utils/scenarioTree'

const props = defineProps<{
  nodes: ScenarioTreeDisplayNode[]
  selectedId: string
  editing: boolean
  dropTargetId: string
}>()

const emit = defineEmits<{
  select: [node: TaskGenerationTreeNode]
  add: [node: TreeAddActionNode]
  menu: [payload: { node: TaskGenerationTreeNode; command: 'add' | 'move' | 'delete' }]
  dragStart: [node: TaskGenerationTreeNode]
  dragOver: [node: TaskGenerationTreeNode]
  drop: [node: TaskGenerationTreeNode]
}>()

const isAddAction = (node: ScenarioTreeDisplayNode): node is TreeAddActionNode => node.kind === 'add_action'
const level = (node: TaskGenerationTreeNode) => NODE_LEVELS[node.kind]
const realNode = (node: ScenarioTreeDisplayNode) => node as TaskGenerationTreeNode
const hasChildren = (node: ScenarioTreeDisplayNode) => !isAddAction(node) && Boolean(node.children?.length)
const childNodes = (node: ScenarioTreeDisplayNode): ScenarioTreeDisplayNode[] => isAddAction(node) ? [] : node.children || []
function emitMenu(node: TaskGenerationTreeNode, command: unknown) {
  if (command === 'add' || command === 'move' || command === 'delete') emit('menu', { node, command })
}
</script>

<template>
  <div class="canvas-tree">
    <div v-for="node in props.nodes" :key="node.id" class="canvas-branch">
      <button
        v-if="isAddAction(node)"
        type="button"
        class="canvas-add-action"
        @click.stop="emit('add', node)"
      >
        <el-icon><Plus /></el-icon>{{ node.label.replace('＋ ', '') }}
      </button>
      <article
        v-else
        class="scenario-node-card"
        :class="[`level-${node.kind}`, { selected: props.selectedId === node.id, 'drop-target': props.dropTargetId === node.id }]"
        :draggable="props.editing"
        @click="emit('select', realNode(node))"
        @dragstart="emit('dragStart', realNode(node))"
        @dragover.prevent="emit('dragOver', realNode(node))"
        @drop.prevent="emit('drop', realNode(node))"
      >
        <div class="node-card-head">
          <span class="level-tag">{{ level(realNode(node)) }}</span>
          <strong>{{ node.label }}</strong>
          <el-dropdown v-if="props.editing" trigger="click" @command="emitMenu(realNode(node), $event)">
            <button type="button" class="node-more" aria-label="更多操作" @click.stop><el-icon><MoreFilled /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="node.kind !== 'app'" command="add">新增子节点</el-dropdown-item>
                <el-dropdown-item v-if="node.kind !== 'scene'" command="move">移动到…</el-dropdown-item>
                <el-dropdown-item divided command="delete">删除整棵子树</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <small v-if="node.kind === 'sub_capability'">{{ node.children?.filter(child => child.kind === 'app').length || 0 }} 个适用 App</small>
      </article>
      <div v-if="hasChildren(node)" class="canvas-children">
        <ScenarioTreeCanvas
          :nodes="childNodes(node)"
          :selected-id="props.selectedId"
          :editing="props.editing"
          :drop-target-id="props.dropTargetId"
          @select="emit('select', $event)"
          @add="emit('add', $event)"
          @menu="emit('menu', $event)"
          @drag-start="emit('dragStart', $event)"
          @drag-over="emit('dragOver', $event)"
          @drop="emit('drop', $event)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.node-more{display:inline-flex;align-items:center;justify-content:center;padding:3px;border:0;background:transparent;color:#94a3b8;cursor:pointer}.node-more:hover{color:var(--accent-deep)}
.canvas-tree{display:grid;gap:14px;min-width:max-content;padding:24px 12px 30px}.canvas-branch{display:grid;gap:12px;justify-items:center}.scenario-node-card{width:220px;padding:13px 14px;border:1px solid #dbe3ed;border-radius:12px;background:#fff;box-shadow:0 7px 18px rgba(15,23,42,.04);text-align:left;cursor:pointer;transition:border-color .18s,box-shadow .18s,transform .18s}.scenario-node-card:hover{transform:translateY(-1px);box-shadow:0 10px 22px rgba(15,23,42,.08)}.scenario-node-card.selected{border-color:var(--accent);background:#f8fffd;box-shadow:0 0 0 3px rgba(20,184,166,.1)}.scenario-node-card.drop-target{border-color:#2563eb;background:#eff6ff}.node-card-head{display:flex;align-items:center;gap:8px;min-width:0}.node-card-head strong{overflow:hidden;flex:1;color:#1e293b;font-size:14px;text-overflow:ellipsis;white-space:nowrap}.level-tag{color:#64748b;font-size:10px;font-weight:800;letter-spacing:.04em}.scenario-node-card p{display:-webkit-box;margin:8px 0 0;overflow:hidden;color:#64748b;font-size:11px;line-height:1.5;-webkit-box-orient:vertical;-webkit-line-clamp:2}.scenario-node-card small{display:block;margin-top:8px;color:#94a3b8;font-size:10px}.level-scene{width:246px}.level-scene .node-card-head strong{font-size:16px}.level-app{width:190px}.level-app .node-card-head strong{font-size:12px}.canvas-children{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:18px;align-items:start;width:100%;padding:18px 24px 0;border-top:1px solid #dbe3ed;position:relative}.canvas-children:before{content:"";position:absolute;top:-1px;left:50%;width:1px;height:18px;background:#dbe3ed}.canvas-children>.canvas-tree{display:contents}.canvas-add-action{display:inline-flex;align-items:center;gap:5px;padding:6px 10px;border:1px dashed var(--line);border-radius:7px;background:transparent;color:var(--accent-deep);font-size:12px;cursor:pointer}.canvas-add-action:hover{border-color:var(--accent);color:var(--accent)}@media(max-width:900px){.canvas-tree{align-items:start}.canvas-children{grid-template-columns:1fr;padding-left:14px}.scenario-node-card,.level-scene,.level-app{width:min(280px,calc(100vw - 130px))}}
</style>
