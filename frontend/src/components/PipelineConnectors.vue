<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, useId } from 'vue'

const props = defineProps<{ board: HTMLElement | null }>()
const id = useId()
const forwardMarker = `pipeline-forward-${id}`
const returnMarker = `pipeline-return-${id}`
const paths = ref<Array<{ from: string; to: string; kind: 'forward' | 'return'; d: string }>>([])
let frame = 0
let observer: ResizeObserver | undefined

function measure() {
  frame = 0
  const board = props.board
  if (!board) return
  const bounds = board.getBoundingClientRect()
  const nodes = Array.from(board.querySelectorAll<HTMLElement>('[data-pipeline-stage]'), element => {
    const rect = element.getBoundingClientRect()
    return {
      id: element.dataset.pipelineStage!,
      left: rect.left - bounds.left,
      right: rect.right - bounds.left,
      middle: (rect.top + rect.bottom) / 2 - bounds.top,
    }
  })
  const first = nodes[0], last = nodes.at(-1)
  if (!first || !last || first === last || !bounds.width) return
  const vertical = window.matchMedia('(max-width: 760px)').matches
  const left = 7, right = bounds.width - 7, top = 13

  paths.value = nodes.slice(1).map((to, index) => {
    const from = nodes[index]!
    const d = vertical
      ? `M ${from.right + 2} ${from.middle} H ${right} V ${to.middle} H ${to.right + 2}`
      : `M ${from.right + 2} ${from.middle} L ${to.left - 2} ${to.middle}`
    return { from: from.id, to: to.id, kind: 'forward', d }
  })
  paths.value.push({
    from: last.id,
    to: first.id,
    kind: 'return',
    d: vertical
      ? `M ${last.left - 2} ${last.middle} H ${left} V ${first.middle} H ${first.left - 2}`
      : `M ${last.right + 2} ${last.middle} H ${right} V ${top} H ${left} V ${first.middle} H ${first.left - 2}`,
  })
}
function scheduleMeasure() {
  if (!frame) frame = requestAnimationFrame(measure)
}
onMounted(() => {
  window.addEventListener('resize', scheduleMeasure)
  if (props.board && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(scheduleMeasure)
    observer.observe(props.board)
    props.board.querySelectorAll('.macro-group, [data-pipeline-stage]').forEach(node => observer?.observe(node))
  }
  scheduleMeasure()
})
onBeforeUnmount(() => {
  observer?.disconnect()
  if (frame) cancelAnimationFrame(frame)
  window.removeEventListener('resize', scheduleMeasure)
})
</script>

<template>
  <svg class="pipeline-connectors" aria-hidden="true" focusable="false">
    <defs>
      <marker :id="forwardMarker" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
        <path d="M 0 0 L 8 4 L 0 8 Z" fill="#617c98" />
      </marker>
      <marker :id="returnMarker" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
        <path d="M 0 0 L 8 4 L 0 8 Z" fill="#4b8178" />
      </marker>
    </defs>
    <path
      v-for="path in paths"
      :key="path.from"
      :data-connector="path.kind"
      :data-from="path.from"
      :data-to="path.to"
      :d="path.d"
      :marker-end="'url(#' + (path.kind === 'return' ? returnMarker : forwardMarker) + ')'"
    />
  </svg>
</template>

<style scoped>
.pipeline-connectors{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2}
[data-connector]{fill:none;stroke:#617c98;stroke-width:1.5;stroke-linejoin:round}
[data-connector="return"]{stroke:#4b8178}
</style>
