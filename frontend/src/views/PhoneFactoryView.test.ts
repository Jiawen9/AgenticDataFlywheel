import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, type App } from 'vue'
import * as Vue from 'vue'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as polling from '@/composables/useFactoryPolling'
import type { FactoryState, PhoneMonitor } from '@/phoneFactoryApi'

const api = vi.hoisted(() => ({ state: vi.fn(), adbDevices: vi.fn(), remoteStatus: vi.fn(), monitor: vi.fn() }))
// Vitest's Node transform emits SSR components; compile this component's actual
// client render function for the lightweight Vue host below (no DOM dependency).
const source = readFileSync(new URL('./PhoneFactoryView.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'phone-factory-monitor-test', inlineTemplate: true })
const compiled = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } })
const exports: { default?: any } = {}
new Function('require', 'exports', compiled.outputText)((name: string) => {
  if (name === 'vue') return Vue
  if (name === '@/phoneFactoryApi') return { phoneFactoryApi: api }
  if (name === '@/composables/useFactoryPolling') return polling
  throw new Error(`Unexpected import ${name}`)
}, exports)
const PhoneFactoryView = exports.default

type Node = { tag: string; text: string; props: Record<string, any>; children: Node[]; parent: Node | null; scrollHeight: number; scrollTop: number }
const node = (tag: string, text = ''): Node => ({ tag, text, props: {}, children: [], parent: null, scrollHeight: 240, scrollTop: 0 })
const renderer = createRenderer<Node, Node>({
  createElement: tag => node(tag), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (element, text) => { element.text = text },
  setElementText: (element, text) => { element.text = text; element.children = [] },
  patchProp: (element, key, _previous, value) => { element.props[key] = value },
  insert(element, parent, anchor = null) {
    if (element.parent) { const oldIndex = element.parent.children.indexOf(element); if (oldIndex >= 0) element.parent.children.splice(oldIndex, 1) }
    element.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, element)
  },
  remove(element) { if (element.parent) { const index = element.parent.children.indexOf(element); if (index >= 0) element.parent.children.splice(index, 1) }; element.parent = null },
  parentNode: element => element.parent,
  nextSibling: element => element.parent?.children[element.parent.children.indexOf(element) + 1] ?? null,
})
function all(root: Node): Node[] { return [root, ...root.children.flatMap(all)] }
function text(root: Node): string { return root.text + root.children.map(text).join('') }
function find(root: Node, id: string) { const found = all(root).find(item => item.props['data-testid'] === id); if (!found) throw new Error(`Missing ${id}`); return found }
function click(element: Node) { element.props.onClick() }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const snapshot = (overrides: Partial<PhoneMonitor> = {}): PhoneMonitor => ({ ok: true, screenshot: 'first-frame', log: 'first line', running: true, device_size: { width: 1080, height: 2400 }, ...overrides })
let state: FactoryState
let fakeDocument: { hidden: boolean; body: { classList: { add: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> } }; addEventListener: (name: string, handler: () => void) => void; removeEventListener: (name: string, handler: () => void) => void }
let documentEvents: Map<string, Set<() => void>>, windowEvents: Map<string, Set<() => void>>
const mounted: App[] = []
function events(target: Map<string, Set<() => void>>) {
  return { addEventListener(name: string, handler: () => void) { if (!target.has(name)) target.set(name, new Set()); target.get(name)!.add(handler) }, removeEventListener(name: string, handler: () => void) { target.get(name)?.delete(handler) } }
}
function visibility(hidden: boolean) { fakeDocument.hidden = hidden; documentEvents.get('visibilitychange')?.forEach(handler => handler()) }
async function settle() { await vi.advanceTimersByTimeAsync(0); await nextTick() }
async function mount() {
  const root = node('root'), app = renderer.createApp(PhoneFactoryView)
  const passthrough = defineComponent({ inheritAttrs: false, setup: (_props, { attrs, slots }) => () => h('control', attrs, slots.default?.()) })
  app.component('el-button', passthrough); app.component('el-tag', passthrough); app.component('router-link', passthrough)
  app.component('el-alert', defineComponent({ props: ['title'], setup: props => () => h('alert', String(props.title)) }))
  app.component('el-empty', defineComponent({ props: ['description'], setup: props => () => h('empty', String(props.description)) }))
  app.component('el-dialog', defineComponent({ props: ['modelValue'], emits: ['close'], setup: (props, { emit, slots }) => () => props.modelValue ? h('dialog', { onClose: () => emit('close') }, slots.default?.()) : null }))
  app.mount(root); mounted.push(app); await settle()
  return { root, app, device: (id: string) => { const found = all(root).find(item => item.props['data-device-id'] === id); if (!found) throw new Error(`Missing device ${id}`); return found } }
}
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks()
  documentEvents = new Map(); windowEvents = new Map()
  fakeDocument = { hidden: false, body: { classList: { add: vi.fn(), remove: vi.fn() } }, ...events(documentEvents) }
  vi.stubGlobal('document', fakeDocument); vi.stubGlobal('window', events(windowEvents))
  state = { phones: ['p1', 'p2'], apps: ['App A', 'App B'], phoneApps: [{ phone_id: 'p1', app: 'App A', status: '运行中' }, { phone_id: 'p1', app: 'App B', status: '运行中' }, { phone_id: 'p2', app: 'App A', status: '空闲' }], vla: [], tasks: [{ description: 'task', filename: 'task.xlsx', status: '运行中' }] }
  api.state.mockImplementation(async () => state)
  api.adbDevices.mockImplementation(async () => ({ ok: true, devices: state.phones.includes('p1') ? [{ serial: 'p1', model: 'Phone One', battery: 72 }] : [] }))
  api.remoteStatus.mockImplementation(async (ids: string[]) => ({ ok: true, statuses: ids.map(phone_id => ({ phone_id, status: phone_id === 'p1' ? '运行中' : '空闲' })) }))
  api.monitor.mockImplementation(async () => snapshot())
})
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('phone factory monitoring', () => {
  it('restores three counters and all device fields without counting App associations as phones', async () => {
    const { root, device } = await mount()
    expect(text(find(root, 'factory-connected-count'))).toBe('1')
    expect(text(find(root, 'factory-phone-count'))).toBe('2')
    expect(text(find(root, 'factory-task-count'))).toBe('1')
    expect(text(device('p1'))).toContain('#001')
    for (const value of ['序列号p1', '资产编号p1', '手机型号Phone One', '运行 AppApp A、App B', '电量72%', '状态运行中']) expect(text(device('p1'))).toContain(value)
    click(device('p1')); await settle()
    expect(text(root)).toContain('运行 App：App A、App B')
    expect(find(root, 'phone-live-screen').props.src).toBe('data:image/png;base64,first-frame')
    expect(text(root)).toContain('1080 × 2400')
  })

  it('polls devices every ten seconds and selected monitor every second independently', async () => {
    const { root, device } = await mount()
    click(device('p1')); await settle()
    await vi.advanceTimersByTimeAsync(3000)
    expect(api.monitor).toHaveBeenCalledTimes(4); expect(api.state).toHaveBeenCalledTimes(1)
    click(find(root, 'factory-monitor-refresh')); await settle()
    expect(api.monitor).toHaveBeenCalledTimes(5); expect(api.state).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(7000)
    expect(api.state).toHaveBeenCalledTimes(2)
    expect(api.monitor).toHaveBeenCalledTimes(12)
  })

  it('manual device refresh updates counters and associations without restarting monitoring', async () => {
    const { root, device } = await mount()
    click(device('p1')); await settle()
    state = { ...state, phoneApps: [{ phone_id: 'p1', app: 'New App', status: '运行中' }], tasks: [] }
    click(find(root, 'factory-devices-refresh')); await settle()
    expect(text(find(root, 'factory-task-count'))).toBe('0')
    expect(text(root)).toContain('运行 App：New App')
    expect(api.monitor).toHaveBeenCalledTimes(1)
  })

  it('serializes monitor refreshes and rejects old responses after switching devices', async () => {
    const pending = deferred<PhoneMonitor>(); api.monitor.mockImplementationOnce(() => pending.promise)
    const { root, device } = await mount()
    click(device('p1')); await settle()
    const oldSignal = api.monitor.mock.calls[0]![1] as AbortSignal
    await vi.advanceTimersByTimeAsync(4000); expect(api.monitor).toHaveBeenCalledTimes(1)
    click(device('p2')); await settle()
    expect(oldSignal.aborted).toBe(true); expect(api.monitor).toHaveBeenCalledTimes(1)
    api.monitor.mockImplementation(async () => snapshot({ screenshot: 'p2-frame' }))
    pending.resolve(snapshot({ screenshot: 'p1-late-frame' })); await settle(); await vi.advanceTimersByTimeAsync(1)
    expect(api.monitor).toHaveBeenLastCalledWith('p2', expect.any(AbortSignal))
    expect(find(root, 'phone-live-screen').props.src).toBe('data:image/png;base64,p2-frame')
  })

  it('stops closed detail requests and prevents late responses from reopening it', async () => {
    const pending = deferred<PhoneMonitor>(); api.monitor.mockImplementationOnce(() => pending.promise)
    const { root, device } = await mount()
    click(device('p1')); await settle()
    const signal = api.monitor.mock.calls[0]![1] as AbortSignal
    all(root).find(item => item.tag === 'dialog')!.props.onClose(); await nextTick()
    expect(signal.aborted).toBe(true)
    pending.resolve(snapshot()); await vi.advanceTimersByTimeAsync(5000)
    expect(api.monitor).toHaveBeenCalledTimes(1)
    expect(all(root).some(item => item.tag === 'dialog')).toBe(false)
    expect(api.state).toHaveBeenCalledTimes(1)
  })

  it('closes and stops a monitor when its device disappears from fresh device state', async () => {
    const { root, device } = await mount()
    click(device('p1')); await settle()
    state = { ...state, phones: [], phoneApps: [] }
    click(find(root, 'factory-devices-refresh')); await settle()
    expect(all(root).some(item => item.tag === 'dialog')).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(api.monitor).toHaveBeenCalledTimes(1)
    expect(text(find(root, 'factory-phone-count'))).toBe('0')
  })

  it('pauses both cycles while hidden and resumes selected detail on visibility or focus', async () => {
    const { device, app } = await mount()
    click(device('p1')); await settle()
    visibility(true); await vi.advanceTimersByTimeAsync(30000)
    expect(api.monitor).toHaveBeenCalledTimes(1); expect(api.state).toHaveBeenCalledTimes(1)
    visibility(false); await settle()
    expect(api.monitor).toHaveBeenCalledTimes(2); expect(api.state).toHaveBeenCalledTimes(2)
    windowEvents.get('focus')?.forEach(handler => handler()); await settle()
    expect(api.monitor).toHaveBeenCalledTimes(3); expect(api.state).toHaveBeenCalledTimes(3)
    app.unmount(); mounted.splice(mounted.indexOf(app), 1)
    await vi.advanceTimersByTimeAsync(30000)
    expect(api.monitor).toHaveBeenCalledTimes(3); expect(documentEvents.get('visibilitychange')?.size).toBe(0)
  })

  it('scrolls appended logs to the bottom and distinguishes running and idle empty logs', async () => {
    const { root, device } = await mount()
    click(device('p1')); await settle()
    let log = find(root, 'phone-live-log'); expect(log.scrollTop).toBe(log.scrollHeight)
    log.scrollTop = 0; log.scrollHeight = 600
    api.monitor.mockResolvedValueOnce(snapshot({ log: 'first line\nsecond line' }))
    await vi.advanceTimersByTimeAsync(1000); await nextTick()
    log = find(root, 'phone-live-log'); expect(text(log)).toContain('second line'); expect(log.scrollTop).toBe(600)
    api.monitor.mockResolvedValueOnce(snapshot({ log: '', running: true }))
    await vi.advanceTimersByTimeAsync(1000)
    expect(text(find(root, 'phone-live-log'))).toBe('采集中，暂无日志输出')
    api.monitor.mockResolvedValueOnce(snapshot({ log: '', running: false }))
    await vi.advanceTimersByTimeAsync(1000)
    expect(text(find(root, 'phone-live-log'))).toBe('暂无采集日志')
  })
})
