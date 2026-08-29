<template>
  <section class="scene-tree-section">
    <div class="scene-tree-heading">
      <div><span class="eyebrow">SCENARIO KNOWLEDGE BASE</span><h2>场景能力树</h2></div>
      <div class="tree-legend"><span><i class="legend-dot legend-dot--scene"></i>场景</span><span><i class="legend-dot legend-dot--primary"></i>一级能力</span><span><i class="legend-dot legend-dot--secondary"></i>二级能力</span><span><i class="legend-dot legend-dot--app"></i>涉及 App</span></div>
    </div>
    <div class="scene-tree-shell">
      <div class="tree-levels"><span>根节点</span><span>场景</span><span>一级能力</span><span>二级能力</span><span>涉及 App</span></div>
      <div class="scene-tree-canvas">
        <svg class="tree-edges" viewBox="0 0 1500 820" preserveAspectRatio="none" aria-hidden="true"><path v-for="edge in treeEdges" :key="edge.id" :d="edge.path" /></svg>
        <article v-for="node in treeNodes" :key="node.id" class="capability-node" :class="`capability-node--${node.kind}`" :style="{ left: `${node.x}px`, top: `${node.y}px` }">
          <span v-if="node.kicker">{{ node.kicker }}</span><strong>{{ node.label }}</strong><small v-if="node.description">{{ node.description }}</small>
        </article>
        <article v-for="group in appGroups" :key="group.id" class="app-group" :style="{ left: `${group.x}px`, top: `${group.y}px` }">
          <div v-for="app in group.apps" :key="app.name" class="app-icon-wrap" :title="app.name"><span class="app-icon" :style="{ background: app.background, color: app.color || '#fff' }">{{ app.mark }}</span><small>{{ app.name }}</small></div>
        </article>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type TreeNode = { id: string; label: string; kind: 'root' | 'scene' | 'primary' | 'secondary'; x: number; y: number; kicker?: string; description?: string; parent?: string }
const treeNodes: TreeNode[] = [
  { id: 'root', label: 'GUI Agent', kicker: '能力全景', description: '移动端通用智能体', kind: 'root', x: 38, y: 365 },
  { id: 'content', label: '内容娱乐', kind: 'scene', x: 270, y: 104, parent: 'root' }, { id: 'life', label: '生活服务', kind: 'scene', x: 270, y: 365, parent: 'root' }, { id: 'social', label: '社交创作', kind: 'scene', x: 270, y: 626, parent: 'root' },
  { id: 'discover', label: '内容发现', kind: 'primary', x: 500, y: 52, parent: 'content' }, { id: 'consume', label: '内容消费', kind: 'primary', x: 500, y: 186, parent: 'content' }, { id: 'travel', label: '出行导航', kind: 'primary', x: 500, y: 316, parent: 'life' }, { id: 'commerce', label: '交易服务', kind: 'primary', x: 500, y: 450, parent: 'life' }, { id: 'communicate', label: '社交互动', kind: 'primary', x: 500, y: 580, parent: 'social' }, { id: 'creation', label: '内容创作', kind: 'primary', x: 500, y: 714, parent: 'social' },
  { id: 'search', label: '搜索与筛选', kind: 'secondary', x: 735, y: 20, parent: 'discover' }, { id: 'ranking', label: '榜单与推荐', kind: 'secondary', x: 735, y: 88, parent: 'discover' }, { id: 'playback', label: '播放与控制', kind: 'secondary', x: 735, y: 156, parent: 'consume' }, { id: 'favorite', label: '收藏与追更', kind: 'secondary', x: 735, y: 224, parent: 'consume' },
  { id: 'route', label: '地点与路线', kind: 'secondary', x: 735, y: 292, parent: 'travel' }, { id: 'transport', label: '票务与行程', kind: 'secondary', x: 735, y: 360, parent: 'travel' }, { id: 'shopping', label: '选购与下单', kind: 'secondary', x: 735, y: 428, parent: 'commerce' }, { id: 'local', label: '本地生活', kind: 'secondary', x: 735, y: 496, parent: 'commerce' },
  { id: 'message', label: '消息与群聊', kind: 'secondary', x: 735, y: 564, parent: 'communicate' }, { id: 'community', label: '动态与社区', kind: 'secondary', x: 735, y: 632, parent: 'communicate' }, { id: 'publish', label: '图文与视频发布', kind: 'secondary', x: 735, y: 700, parent: 'creation' }, { id: 'edit', label: '编辑与管理', kind: 'secondary', x: 735, y: 768, parent: 'creation' },
]
const appGroups = [
  { id: 'apps-search', parent: 'search', x: 1000, y: 9, apps: [{ name: '爱奇艺', mark: 'iQIYI', background: '#19c35b' }, { name: '腾讯视频', mark: '▶', background: 'linear-gradient(135deg,#19c36a,#168cff)' }, { name: '优酷', mark: 'YOU', background: 'linear-gradient(135deg,#ff315b,#15a6ff)' }] },
  { id: 'apps-ranking', parent: 'ranking', x: 1000, y: 77, apps: [{ name: '哔哩哔哩', mark: 'BILI', background: '#fb7299' }, { name: '抖音', mark: '♪', background: '#111827' }] },
  { id: 'apps-playback', parent: 'playback', x: 1000, y: 145, apps: [{ name: '爱奇艺', mark: 'iQIYI', background: '#19c35b' }, { name: '腾讯视频', mark: '▶', background: '#168cff' }] },
  { id: 'apps-favorite', parent: 'favorite', x: 1000, y: 213, apps: [{ name: '优酷', mark: 'YOU', background: '#ff315b' }, { name: '哔哩哔哩', mark: 'BILI', background: '#fb7299' }] },
  { id: 'apps-route', parent: 'route', x: 1000, y: 281, apps: [{ name: '高德地图', mark: 'A', background: '#3478f6' }, { name: '百度地图', mark: '⌖', background: '#4768ee' }] },
  { id: 'apps-transport', parent: 'transport', x: 1000, y: 349, apps: [{ name: '携程', mark: 'C', background: '#287dfa' }, { name: '铁路12306', mark: '路', background: '#1677aa' }] },
  { id: 'apps-shopping', parent: 'shopping', x: 1000, y: 417, apps: [{ name: '淘宝', mark: '淘', background: '#ff5000' }, { name: '京东', mark: 'JD', background: '#e1251b' }, { name: '拼多多', mark: '拼', background: '#e02e24' }] },
  { id: 'apps-local', parent: 'local', x: 1000, y: 485, apps: [{ name: '美团', mark: 'M', background: '#ffd100', color: '#171717' }, { name: '饿了么', mark: '饿', background: '#1677ff' }] },
  { id: 'apps-message', parent: 'message', x: 1000, y: 553, apps: [{ name: '微信', mark: '微', background: '#07c160' }, { name: 'QQ', mark: 'QQ', background: '#12b7f5' }] },
  { id: 'apps-community', parent: 'community', x: 1000, y: 621, apps: [{ name: '微博', mark: '微', background: '#ff8200' }, { name: '小红书', mark: 'RED', background: '#ff2442' }] },
  { id: 'apps-publish', parent: 'publish', x: 1000, y: 689, apps: [{ name: '抖音', mark: '♪', background: '#111827' }, { name: '快手', mark: 'K', background: '#ff4906' }, { name: '小红书', mark: 'RED', background: '#ff2442' }] },
  { id: 'apps-edit', parent: 'edit', x: 1000, y: 757, apps: [{ name: '剪映', mark: '剪', background: '#111827' }, { name: '醒图', mark: '醒', background: '#7657ff' }] },
]
const treeEdges = computed(() => {
  const nodes = new Map(treeNodes.map((node) => [node.id, node]))
  const edges = treeNodes.filter((node) => node.parent).map((node) => {
    const parent = nodes.get(node.parent!)!; const x1 = parent.x + (parent.kind === 'root' ? 164 : 150); const y1 = parent.y + (parent.kind === 'root' ? 45 : 34); const x2 = node.x; const y2 = node.y + 34; const middle = (x1 + x2) / 2
    return { id: `${parent.id}-${node.id}`, path: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }
  })
  return [...edges, ...appGroups.map((group) => { const parent = nodes.get(group.parent)!; const x1 = parent.x + 150; const y1 = parent.y + 30; const middle = (x1 + group.x) / 2; return { id: `${parent.id}-${group.id}`, path: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${group.y + 30}, ${group.x} ${group.y + 30}` } })]
})
</script>

<style scoped>
.scene-tree-section { width: 100%; }.scene-tree-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 18px; padding: 0 4px; }.scene-tree-heading h2 { margin: 5px 0 0; font-size: 25px; }.tree-legend { display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted); font-size: 11px; }.tree-legend span { display: flex; align-items: center; gap: 5px; }.legend-dot { width: 8px; height: 8px; border-radius: 50%; }.legend-dot--scene { background: #0ea5e9; }.legend-dot--primary { background: #14b8a6; }.legend-dot--secondary { background: #64748b; }.legend-dot--app { background: #f59e0b; }
.scene-tree-shell { overflow-x: auto; border: 1px solid rgba(255,255,255,.9); border-radius: 24px; background: linear-gradient(145deg,rgba(255,255,255,.9),rgba(241,245,249,.72)); box-shadow: 0 22px 56px rgba(15,23,42,.09); }.tree-levels { display: grid; grid-template-columns: 230px 230px 235px 265px 1fr; min-width: 1500px; padding: 13px 28px; border-bottom: 1px solid var(--line); color: #94a3b8; font-size: 9px; font-weight: 900; letter-spacing: .14em; }.scene-tree-canvas { position: relative; width: 1500px; height: 820px; background-image: linear-gradient(rgba(148,163,184,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.055) 1px,transparent 1px); background-size: 28px 28px; }.tree-edges { position: absolute; inset: 0; width: 1500px; height: 820px; pointer-events: none; }.tree-edges path { fill: none; stroke: rgba(100,116,139,.32); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.capability-node { position: absolute; display: grid; width: 150px; min-height: 60px; padding: 11px 14px; place-content: center start; border: 1px solid white; border-radius: 13px; background: white; box-shadow: 0 8px 22px rgba(15,23,42,.08); }.capability-node > span { color: #5eead4; font-size: 8px; font-weight: 900; letter-spacing: .13em; }.capability-node strong { font-size: 13px; }.capability-node small { margin-top: 3px; color: #94a3b8; font-size: 9px; }.capability-node--root { width: 164px; min-height: 90px; border: 0; border-radius: 19px; background: linear-gradient(145deg,#0f172a,#134e4a); color: white; box-shadow: 0 18px 38px rgba(15,23,42,.24); }.capability-node--root strong { font-size: 19px; }.capability-node--scene { border-left: 4px solid #0ea5e9; }.capability-node--scene strong { font-size: 15px; }.capability-node--primary { border-left: 4px solid #14b8a6; }.capability-node--secondary { min-height: 60px; border-left: 3px solid #94a3b8; background: rgba(255,255,255,.88); }
.app-group { position: absolute; display: flex; align-items: center; gap: 14px; min-width: 380px; height: 60px; padding: 5px 12px; border: 1px solid rgba(226,232,240,.85); border-radius: 15px; background: rgba(255,255,255,.64); }.app-icon-wrap { display: grid; grid-template-columns: 38px auto; align-items: center; gap: 7px; }.app-icon { display: grid; width: 38px; height: 38px; place-items: center; border-radius: 10px; box-shadow: 0 5px 12px rgba(15,23,42,.14); font-size: 9px; font-weight: 950; letter-spacing: -.04em; }.app-icon-wrap small { max-width: 54px; color: #475569; font-size: 9px; white-space: nowrap; }
@media (max-width: 800px) { .scene-tree-heading { align-items: flex-start; flex-direction: column; } }
</style>
