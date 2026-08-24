<script setup lang="ts">
defineProps<{
  data: {
    action: string
    summary: string
    occurrenceCount: number
    depth: number
    root: boolean
    childCount?: number
  }
}>()
</script>

<template>
  <div class="tree-node" :class="{ 'tree-node--root': data.root }">
    <div class="tree-node__top">
      <span>{{ data.root ? 'ROOT' : data.action }}</span>
      <b v-if="!data.root">×{{ data.occurrenceCount }}</b>
    </div>
    <p>{{ data.summary || '共同起点' }}</p>
    <small>DEPTH {{ data.depth }}<template v-if="data.childCount && data.childCount > 1"> · {{ data.childCount }} 分支</template></small>
  </div>
</template>

<style scoped>
.tree-node { width: 148px; height: 54px; padding: 7px 9px; overflow: hidden; border: 1px solid #cbd5e1; border-left: 3px solid #16a394; border-radius: 9px; background: rgba(255,255,255,.98); box-shadow: 0 5px 14px rgba(15,23,42,.08); }
.tree-node--root { border-left-color: #0f172a; background: #0f172a; color: white; }
.tree-node__top { display: flex; justify-content: space-between; gap: 6px; color: #0f766e; font-size: 10px; font-weight: 900; text-transform: uppercase; }
.tree-node--root .tree-node__top { color: #5eead4; }
.tree-node__top span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-node__top b { flex: 0 0 auto; padding: 0 5px; border-radius: 999px; background: #ccfbf1; color: #0f766e; }
.tree-node p { margin: 4px 0 2px; overflow: hidden; color: inherit; font-size: 9px; line-height: 1.2; text-overflow: ellipsis; white-space: nowrap; }
.tree-node small { color: #94a3b8; font-size: 7px; letter-spacing: .08em; }
</style>
