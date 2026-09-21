import { onBeforeUnmount, onMounted } from 'vue'

/** One request cycle at a time, including stop/start while an aborted request settles. */
export function createSerialPoller(work: (signal: AbortSignal, current: () => boolean) => Promise<void>, delay = 5000) {
  let wanted = false, pending = false, generation = 0, disposed = false
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  async function tick() {
    if (!wanted || pending) return
    pending = true
    const epoch = generation
    controller = new AbortController()
    try { await work(controller.signal, () => wanted && generation === epoch) }
    catch { /* Each consumer exposes errors; aborts are expected during navigation. */ }
    finally {
      pending = false
      if (wanted) timer = setTimeout(() => void tick(), generation === epoch ? delay : 0)
    }
  }
  function stop() { wanted = false; generation++; clearTimeout(timer); controller?.abort() }
  function start() { if (disposed) return; stop(); wanted = true; void tick() }
  function dispose() { disposed = true; stop() }
  return { start, stop, dispose }
}
export function useFactoryPolling(work: (signal: AbortSignal, current: () => boolean) => Promise<void>, delay = 5000) {
  const poller = createSerialPoller(work, delay)
  const visible = () => { if (document.hidden) poller.stop(); else poller.start() }
  onMounted(() => { document.addEventListener('visibilitychange', visible); window.addEventListener('focus', visible); visible() })
  onBeforeUnmount(() => { poller.dispose(); document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible) })
  return poller
}
