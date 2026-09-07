<script setup lang="ts">
import { MoreFilled, Plus } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'
import { NODE_LEVELS } from '@/utils/scenarioTree'

export type ScenarioColumnKind = 'capability' | 'sub_capability' | 'app'
export type ScenarioNodeCommand = 'rename' | 'details' | 'add' | 'bind' | 'move' | 'delete' | 'unbind'

const props = withDefaults(defineProps<{
  title: string
  kind: ScenarioColumnKind
  nodes: TaskGenerationTreeNode[]
  selectedId: string
  editing: boolean
  addingKind: TaskGenerationTreeNode['kind'] | ''
  addingParentId: string
  addParentId: string
  addText: string
  renameId: string
  renameText: string
  dropTargetId: string
  highlightId: string
  addLabel: string
  emptyTitle: string
  emptyDescription: string
  addDisabled?: boolean
  showFooterAdd?: boolean
}>(), {
  showFooterAdd: true,
})

const emit = defineEmits<{
  select: [node: TaskGenerationTreeNode]
  command: [payload: { node: TaskGenerationTreeNode; command: ScenarioNodeCommand }]
  startAdd: []
  updateAddText: [value: string]
  commitAdd: []
  cancelAdd: []
  startRename: [node: TaskGenerationTreeNode]
  updateRenameText: [value: string]
  commitRename: [node: TaskGenerationTreeNode]
  cancelRename: []
  dragStart: [node: TaskGenerationTreeNode]
  dragOver: [node: TaskGenerationTreeNode]
  drop: [node: TaskGenerationTreeNode]
}>()

function emitCommand(node: TaskGenerationTreeNode, command: unknown) {
  if (typeof command === 'string') emit('command', { node, command: command as ScenarioNodeCommand })
}

function isRenaming(node: TaskGenerationTreeNode) { return props.renameId === node.id }
function isAdding() { return props.addingKind === props.kind && props.addingParentId === props.addParentId }
function addPlaceholder() {
  const labels: Record<ScenarioColumnKind, string> = { capability: '能力', sub_capability: '子能力', app: 'App' }
  return `输入${labels[props.kind]}名称`
}
function appCount(node: TaskGenerationTreeNode) { return (node.children || []).filter(child => child.kind === 'app').length }
function subCount(node: TaskGenerationTreeNode) { return (node.children || []).filter(child => child.kind === 'sub_capability').length }
</script>

<template>
  <section class="scenario-column">
    <header class="column-header">
      <div>
        <span class="column-kicker">{{ NODE_LEVELS[props.kind] }}</span>
        <h3>{{ props.title }}</h3>
      </div>
      <span class="column-count">{{ props.nodes.length }}</span>
    </header>

    <div class="column-list">
      <div v-for="node in props.nodes" :key="node.id" class="column-item-wrap">
        <article
          class="column-item"
          :class="[
            `column-item--${props.kind}`,
            { selected: props.selectedId === node.id, 'drop-target': props.dropTargetId === node.id, 'search-hit': props.highlightId === node.id },
          ]"
          :draggable="props.editing"
          @click="emit('select', node)"
          @dragstart="emit('dragStart', node)"
          @dragover.prevent="emit('dragOver', node)"
          @drop.prevent="emit('drop', node)"
        >
          <div class="item-main">
            <span class="item-level">{{ NODE_LEVELS[props.kind] }}</span>
            <input
              v-if="isRenaming(node)"
              :value="props.renameText"
              class="inline-rename-input"
              maxlength="200"
              autofocus
              @click.stop
              @input="emit('updateRenameText', ($event.target as HTMLInputElement).value)"
              @keydown.enter.prevent="emit('commitRename', node)"
              @keydown.esc.prevent="emit('cancelRename')"
              @blur="emit('commitRename', node)"
            />
            <span v-else class="item-label" @dblclick.stop="emit('startRename', node)">{{ node.label }}</span>
            <span v-if="props.kind !== 'app'" class="item-arrow">→</span>
          </div>
          <div class="item-meta">
            <span v-if="props.kind === 'capability'">{{ subCount(node) }} 个子能力</span>
            <span v-else-if="props.kind === 'sub_capability'">{{ appCount(node) }} 个 App</span>
            <span v-else>{{ node.resource_count ? `${node.resource_count} 个资源` : 'App 绑定' }}</span>
          </div>
          <el-dropdown v-if="props.editing || props.kind === 'app'" trigger="click" @command="emitCommand(node, $event)">
            <button type="button" class="item-more" aria-label="更多操作" @click.stop><el-icon><MoreFilled /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="details">编辑详情</el-dropdown-item>
                <template v-if="props.editing">
                  <el-dropdown-item command="rename">重命名</el-dropdown-item>
                  <el-dropdown-item v-if="props.kind === 'capability'" command="add">添加子能力</el-dropdown-item>
                  <el-dropdown-item command="move">移动到…</el-dropdown-item>
                  <el-dropdown-item divided :command="props.kind === 'app' ? 'unbind' : 'delete'">{{ props.kind === 'app' ? '取消绑定' : '删除整棵子树' }}</el-dropdown-item>
                </template>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </article>
      </div>

      <div v-if="isAdding()" class="inline-creator">
        <span class="item-level">{{ NODE_LEVELS[props.kind] }}</span>
        <input
          :value="props.addText"
          class="inline-creator-input"
          :placeholder="addPlaceholder()"
          maxlength="200"
          autofocus
          @input="emit('updateAddText', ($event.target as HTMLInputElement).value)"
          @keydown.enter.prevent="emit('commitAdd')"
          @keydown.esc.prevent="emit('cancelAdd')"
        />
        <button type="button" class="creator-cancel" aria-label="取消新增" @click="emit('cancelAdd')">×</button>
      </div>

      <div v-if="!props.nodes.length && !isAdding()" class="column-empty">
        <div class="empty-mark">○</div>
        <strong>{{ props.emptyTitle }}</strong>
        <p>{{ props.emptyDescription }}</p>
      </div>
    </div>

    <footer class="column-footer">
      <button v-if="props.showFooterAdd !== false" type="button" class="column-add" :disabled="props.addDisabled || props.addingKind !== ''" @click="emit('startAdd')">
        <el-icon><Plus /></el-icon>{{ props.addLabel }}
      </button>
      <slot name="footer-extra" />
    </footer>
  </section>
</template>

<style scoped>
.scenario-column{display:flex;min-width:0;min-height:480px;flex-direction:column;background:rgba(255,255,255,.52)}.column-header{display:flex;align-items:flex-start;justify-content:space-between;padding:20px 20px 14px;border-bottom:1px solid var(--line)}.column-header h3{margin:3px 0 0;color:#1e293b;font-size:14px;font-weight:800}.column-kicker,.item-level{color:#94a3b8;font-size:10px;font-weight:900;letter-spacing:.08em}.column-count{min-width:23px;padding:3px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:10px;text-align:center}.column-list{display:grid;align-content:start;gap:4px;min-height:360px;padding:10px}.column-item{position:relative;display:grid;gap:5px;min-width:0;padding:12px 34px 11px 12px;border:1px solid transparent;border-radius:9px;background:transparent;cursor:pointer;transition:background .16s,border-color .16s,box-shadow .16s}.column-item:hover{border-color:#d8e4e6;background:#fff;box-shadow:0 5px 15px rgba(15,23,42,.04)}.column-item.selected{border-color:#8bd8ce;background:#f8fffd;box-shadow:0 0 0 2px rgba(20,184,166,.08)}.column-item.drop-target{border-color:#60a5fa;background:#eff6ff}.column-item.search-hit{animation:search-hit 1.2s ease}.item-main{display:flex;align-items:center;gap:8px;min-width:0}.item-main .item-level{flex:0 0 auto}.item-label{overflow:hidden;color:#334155;font-size:13px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.column-item--app{border-color:#e6edf0;background:rgba(248,250,252,.74)}.column-item--app .item-label{font-size:12px}.column-item--app .item-level{color:#0f766e}.item-arrow{margin-left:auto;color:#94a3b8;font-size:14px}.item-meta{padding-left:29px;overflow:hidden;color:#94a3b8;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.item-more{position:absolute;top:10px;right:8px;display:grid;place-items:center;width:22px;height:22px;padding:0;border:0;border-radius:5px;background:transparent;color:#94a3b8;opacity:0;cursor:pointer}.column-item:hover .item-more,.column-item.selected .item-more{opacity:1}.item-more:hover{background:#f1f5f9;color:var(--accent-deep)}.inline-rename-input,.inline-creator-input{min-width:0;border:0;border-bottom:1px solid var(--accent);outline:0;background:transparent;color:#1e293b;font:inherit}.inline-rename-input{flex:1;font-size:13px;font-weight:700}.inline-creator{display:flex;align-items:center;gap:8px;margin:2px 0;padding:10px 9px;border:1px solid #99d9d1;border-radius:9px;background:#fff}.inline-creator-input{flex:1;font-size:12px}.creator-cancel{border:0;background:transparent;color:#94a3b8;font-size:18px;line-height:1;cursor:pointer}.creator-cancel:hover{color:#475569}.column-empty{display:grid;justify-items:center;gap:5px;padding:55px 20px 40px;text-align:center}.empty-mark{display:grid;place-items:center;width:33px;height:33px;border:1px solid #cbd5e1;border-radius:50%;color:#94a3b8;font-size:18px}.column-empty strong{color:#475569;font-size:12px}.column-empty p{max-width:190px;margin:0;color:#94a3b8;font-size:11px;line-height:1.6}.column-footer{margin-top:auto;padding:11px 14px 15px;border-top:1px solid #edf2f4}.column-add{display:inline-flex;align-items:center;gap:6px;padding:6px 7px;border:0;border-radius:6px;background:transparent;color:var(--accent-deep);font-size:12px;cursor:pointer}.column-add:hover:not(:disabled){background:#f0fdfa}.column-add:disabled{color:#cbd5e1;cursor:not-allowed}@keyframes search-hit{0%,100%{box-shadow:0 0 0 0 rgba(20,184,166,0)}35%{box-shadow:0 0 0 4px rgba(20,184,166,.2)}}
</style>
