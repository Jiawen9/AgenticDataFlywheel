/** Real built Vue pages; every API request is fulfilled in CDP with local fixtures.
 * Run from frontend: node tests/business-pages.browser.mjs
 * No backend/model/device service or business data is used.
 */
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dist = path.join(frontend, 'dist')
const output = process.env.BUSINESS_UI_OUTPUT ? path.resolve(process.env.BUSINESS_UI_OUTPUT) : path.join(frontend, '.ui-preview', 'business-pages')
await mkdir(output, { recursive: true })
const stamp = '2026-11-17T00:00:00Z'
const number = id => Number(String(id).match(/\d+/)?.[0] || 1)
const id = (prefix, n) => prefix + String(n).padStart(3, '0')
const indices = Array.from({ length: 100 }, (_, i) => i + 1)
const batches = indices.map(n => ({ batch_id: id('b', n), kind: 'existing_trajectories', label: `示例批次 ${n}`, task_count: 100, ready_trajectory_count: 100, ready_step_count: 200, collection_status: 'completed', preprocessing_status: 'succeeded', latest_job: null, annotation_version: 'annotation-v1', artifacts: [], can_start: false, reason: null }))
const correctionBatches = batches.map(row => ({ ...row, tree_run_id: row.batch_id, tree_completed_at: stamp, quality_completed_at: stamp, total_task_count: 100, reviewed_task_count: 100, status: 'ready', is_default: false }))
const groups = indices.map(n => ({ group_id: id('group', n), task_id: id('t', n), task: `任务 ${n}`, meta_task: id('tr', n), quality: 'good', prefix: '', export: true, row_count: 2, active_row_count: 2, edited_row_count: 1, action_edit_count: 1, cot_row_count: 1, generated_row_count: 0 }))
const session = n => ({ session_id: id('s', n), batch_id: id('b', n), storage_revision: 1, pending_review_count: 0, source_id: 'source', source: null, tree_run_id: id('b', n), selection: { status: 'ready', tasks: [] }, created_at: stamp, updated_at: stamp, row_count: 200, edited_row_count: 100, eligible_cot_row_count: 100, group_count: 100, groups: [], exports: [] })
const sessions = indices.map(session)
const jobs = indices.map(n => ({ job_id: id('job', n), kind: 'task_generation', status: 'succeeded', stage: 'succeeded', created_at: stamp, started_at: stamp, completed_at: stamp, current_item: null, completed_items: 10, total_items: 10, percent: 100, generate_n: 10, result_count: 100, errors: [], warnings: [], error: null, knowledge_base_version: 'kb-v1' }))
const result = n => ({ result_id: id('result', n), app: '示例 App', scene: '场景', capability: '能力', sub_capability: '子能力', task: `生成任务 ${n}`, deleted: false, pre_dependency: 'zero' })
const candidate = n => ({ session_id: id('s', n), batch_id: id('b', n), tree_run_id: id('b', n), created_at: stamp, updated_at: stamp, ready: true, reason: '', latest_excel: { filename: '发布表.xlsx', created_at: stamp, rows: 200, path: 'mock/发布表.xlsx' }, task_count: 100, trajectory_count: 100, step_count: 200 })
const release = n => ({ release_id: id('release', n), name: `发布数据集 ${n}`, batch_ids: [id('b', n)], source_kind: 'correction', created_at: stamp, excel_count: 1, excel_paths: [{ filename: '发布表.xlsx', path: 'mock/发布表.xlsx', sha256: 'fixture', rows: 200, created_at: stamp }], trajectory_paths: [], source_count: 1, task_count: 100, trajectory_count: 100, step_count: 200, upload_status: 'not_uploaded', upload_job_id: null, upload_error: null, s3_uri: null, uploaded_at: null, uploaded_files: 0, uploaded_bytes: 0, local_available: true })
function envelope(rows, query, extra = {}) {
  const offset = Number(query.get('offset') || 0), limit = Number(query.get('limit') || 10)
  return { items: rows.slice(offset, offset + limit), latest: rows.slice(0, 3), total: rows.length, filtered_total: rows.length, scope_counts: { total: rows.length, active: 0, abnormal: 0 }, offset, limit, has_more: offset + limit < rows.length, counts_complete: true, index_state: 'ready', updated_at: stamp, ...extra }
}
const requests = [], unknown = [], errors = [], consoleErrors = [], blocked = []
function mock(request) {
  const url = new URL(request.url), p = url.pathname, q = url.searchParams
  requests.push({ path: p, query: Object.fromEntries(q), method: request.method })
  if (p === '/api/display-identifiers/resolve') return { items: (JSON.parse(request.postData || '{}').items || []).map(item => ({ ...item, scope: item.scope || '', display_id: item.id, mapped: true })) }
  if (request.method !== 'GET') { unknown.push({ path: p, method: request.method }); return { detail: 'Smoke test never mutates business state' } }
  if (p === '/api/pipelines') return envelope([], q)
  if (p === '/api/phone-factory/state' && q.get('view') === 'monitor') return { devices: indices.slice(0, 10).map(n => ({ id: id('phone', n), apps: ['示例 App'], model: '离线示例手机', battery: 90, connected: true, status: '空闲' })), task_count: 100, stale: false, refreshing: false, observed_at: stamp, error: null }
  if (p === '/api/training-data-overview') return { schema_version: 1, version: 'overview-v1', updated_at: stamp, overview: { total_trajectories: 100, subtask_trajectories: 100, total_steps: 200, show_manual_refine_steps: true, manual_refine_steps: 30, manual_known_steps: 200, manual_unknown_steps: 0, level1_scenes: 1, level2_scenes: 1, total_apps: 1, avg_steps_per_trajectory: 2 }, filters: { sources: ['示例来源'], scenes: [{ name: '示例场景', level2_scenes: ['示例能力'] }], apps: ['示例 App'], date_range: { min_date: '2026-11-01', max_date: '2026-11-17' } }, trend: [{ date: '2026-11-17', total_trajectories: 100, subtask_trajectories: 100, total_steps: 200 }], app_stats: [{ name: '示例 App', total_trajectories: 100, total_steps: 200, subtask_trajectories: 100 }], scene_stats: [{ name: '示例场景', total_trajectories: 100, total_steps: 200, subtask_trajectories: 100 }], action_stats: [{ category: 'click', count: 200 }], step_stats: [{ steps: 2, count: 100 }], conversions: [], warnings: [], workbook_url: null }
  if (p.endsWith('/lifecycle')) return { batch_id: p.split('/')[3], status: 'active', published_at: null, release_id: null }
  if (p === '/api/trajectory-preprocessing/batches') return envelope(q.has('batch_id') ? batches.filter(row => row.batch_id === q.get('batch_id')) : batches, q)
  if (p === '/api/tree-builds' || p === '/api/quality-jobs' || p === '/api/correction/cot-jobs') return envelope([], q)
  if (p === '/api/quality-jobs/settings') return { supported: true, model_configured: true, profiles: [{ id: 'fixture', digest: 'fixture', name: '离线规则' }], evaluation_profile_id: 'fixture', evaluation_profile_digest: 'fixture', evaluator_config: { text: { base_url: 'http://fixture.invalid', model: 'fixture' }, vision: { base_url: 'http://fixture.invalid', model: 'fixture' } } }
  if (p === '/api/tasks' || /^\/api\/data-batches\/[^/]+\/tree$/.test(p)) {
    const batch = q.get('batch_id') || p.split('/')[3]
    const tasks = indices.map(n => ({ task_id: id('t', n), goal: `检查任务 ${n}`, warning: '', first_trajectory: id('tr', n), trajectory_count: 1, step_count: 2, original_step_count: 2, tree_step_count: 2, ignored_step_count: 0, action_node_count: 2, annotated: true, tree_status: 'succeeded', quality_status: 'succeeded', status: 'succeeded', rubric_ready: true, fusion_complete: true, results_available: true, average_score: 4, passed_count: 1 }))
    const value = envelope(tasks, q)
    return { ...value, tasks: value.items, batch_id: batch, run_id: batch, task_count: 100, task_ids: tasks.map(t => t.task_id), ready_task_ids: tasks.map(t => t.task_id), quality_counts: { completed: 100, pending: 0, stale: 0 }, annotation_version: 'annotation-v1', revision: 'tree-v1', quality_revision: 'quality-v1' }
  }
  if (p === '/api/correction/batches') return envelope(correctionBatches, q)
  if (p === '/api/correction/sessions') return envelope(q.has('batch_id') ? sessions.filter(s => s.batch_id === q.get('batch_id')) : sessions, q)
  if (/^\/api\/correction\/sessions\/[^/]+$/.test(p)) return { session: session(number(p.split('/').at(-1))) }
  if (/^\/api\/correction\/sessions\/[^/]+\/tasks$/.test(p)) return envelope(groups, q, { storage_revision: 1 })
  if (p === '/api/task-generation/jobs') return envelope(jobs.map(job => ({ ...job, kind: q.get('kind') || 'task_generation' })), q)
  if (p === '/api/task-generation/tree') return { index_state: 'ready', knowledge_bases: [], tree: { version: 'tree-v1', scenes: [1, 2, 3].map(n => ({ id: id('scene', n), kind: 'scene', label: `示例场景 ${n}`, children: [{ id: id('cap', n), kind: 'capability', label: '二级能力', children: [{ id: id('type', n), kind: 'sub_capability', label: '示例任务类型', children: [{ id: id('app', n), kind: 'app', label: '示例 App', app: '示例 App' }] }] }] })) } }
  if (p === '/api/task-generation/collection-batches') return { batches: [] }
  if (/^\/api\/task-generation\/jobs\/[^/]+$/.test(p)) return jobs[number(p.split('/').at(-1)) - 1]
  if (p.endsWith('/results')) { const value = envelope(indices.map(result), q); return { results: value.items, groups: value.items.map(row => ({ group_id: row.result_id, results: [row] })), errors: [], total: 100, offset: value.offset, limit: value.limit, counts: { all: 100, active: 100, main: 100, prerequisites: 0, strong: 0 }, apps: ['示例 App'], types: [['["场景","能力","子能力"]', '场景 / 能力 / 子能力']] } }
  if (p === '/api/dataset-releases/candidates') return envelope(indices.map(candidate), q)
  if (p === '/api/dataset-releases') return envelope(indices.map(release), q)
  if (/^\/api\/dataset-releases\/[^/]+$/.test(p)) return { release: release(number(p.split('/').at(-1))) }
  if (p === '/api/dataset-upload-capabilities') return { internal: { configured: false, reason: '浏览器离线验证，不配置上传' } }
  unknown.push({ path: p, query: Object.fromEntries(q) })
  return { detail: `Missing smoke fixture: ${p}` }
}
const server = createServer(async (req, res) => {
  try {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
    let file = path.resolve(dist, '.' + pathname)
    if (!file.startsWith(dist + path.sep)) file = path.join(dist, 'index.html')
    try { if (!(await stat(file)).isFile()) file = path.join(dist, 'index.html') } catch { file = path.join(dist, 'index.html') }
    const data = await readFile(file)
    res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' })[path.extname(file)] || 'text/html')
    res.end(data)
  } catch (error) { res.statusCode = 500; res.end(String(error)) }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const profile = path.join(output, 'chrome-profile-' + Date.now())
const chrome = spawn(process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--disable-extensions', '--remote-debugging-port=0', '--remote-allow-origins=*', '--user-data-dir=' + profile, '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1', 'about:blank'], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] })
let stderr = '', socket, seq = 0
chrome.stderr.on('data', data => { stderr += data })
const pending = new Map(), delay = ms => new Promise(resolve => setTimeout(resolve, ms)), scenarios = []
let chromeStopped = false, cleanupPromise
const chromeExited = new Promise(resolve => chrome.once('exit', () => { chromeStopped = true; resolve() }))
const cleanup = () => cleanupPromise ||= (async () => {
  socket?.close()
  if (!chromeStopped) chrome.kill()
  await Promise.race([chromeExited, delay(5000)])
  await new Promise(resolve => server.close(resolve))
  if (chromeStopped && path.dirname(profile) === output && path.basename(profile).startsWith('chrome-profile-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }).catch(error => console.warn('Temporary Chrome profile cleanup:', error.message))
  } else if (!chromeStopped) console.warn('Own Chrome did not exit; retained its profile:', profile)
})()
process.once('SIGINT', async () => { await cleanup(); process.exit(130) })
const assert = (value, message) => { if (!value) throw new Error(message) }
try {
  let port
  for (let i = 0; i < 100; i++) { try { port = (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break } catch { await delay(100) } }
  assert(port, 'Chrome debugging endpoint unavailable: ' + stderr)
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === 'page')
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
  const call = (method, params = {}) => new Promise((resolve, reject) => { const id = ++seq; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })) })
  socket.onmessage = event => {
    const value = JSON.parse(event.data)
    if (value.id) { const item = pending.get(value.id); if (item) { pending.delete(value.id); value.error ? item.reject(value.error) : item.resolve(value.result) } }
    else if (value.method === 'Runtime.exceptionThrown') errors.push(value.params.exceptionDetails)
    else if (value.method === 'Runtime.consoleAPICalled' && value.params.type === 'error') consoleErrors.push(value.params.args.map(arg => arg.value || arg.description).join(' '))
    else if (value.method === 'Fetch.requestPaused') {
      const { requestId, request } = value.params
      if (new URL(request.url).pathname.startsWith('/api/')) {
        let body
        try { body = mock(request) } catch (error) { body = { detail: String(error) }; unknown.push({ path: request.url, error: String(error) }) }
        void call('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }, { name: 'Access-Control-Allow-Origin', value: '*' }], body: Buffer.from(JSON.stringify(body)).toString('base64') })
      } else if (!request.url.startsWith(origin) && !request.url.startsWith('data:')) { blocked.push(request.url); void call('Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' }) }
      else void call('Fetch.continueRequest', { requestId })
    }
  }
  const evaluate = async expression => { const reply = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (reply.exceptionDetails) throw new Error(reply.exceptionDetails.text + ': ' + reply.exceptionDetails.exception?.description); return reply.result?.value }
  async function until(check, message, timeout = 8000) { const deadline = Date.now() + timeout; while (Date.now() < deadline) { if (await check()) return; await delay(80) } throw new Error(message) }
  await call('Runtime.enable'); await call('Page.enable'); await call('Fetch.enable', { patterns: [{ urlPattern: '*' }] })
  await call('Page.addScriptToEvaluateOnNewDocument', { source: 'try { sessionStorage.clear(); localStorage.clear() } catch {}' })
  const specs = [
    { name: 'preprocessing', route: '/collection/tree-building', list: '/api/trajectory-preprocessing/batches', forbidden: r => r.path === '/api/tasks' || r.path.includes('/collection-batches/'), select: true },
    { name: 'quality', route: '/quality', list: '/api/trajectory-preprocessing/batches', forbidden: r => /^\/api\/data-batches\/[^/]+\/tree$/.test(r.path) || r.path.endsWith('/quality'), select: true },
    { name: 'correction', route: '/correction/expert-action', list: '/api/correction/batches', forbidden: r => /^\/api\/correction\/sessions\/[^/]+/.test(r.path), select: true },
    { name: 'cot', route: '/correction/cot-generation', list: '/api/correction/sessions', forbidden: r => /^\/api\/correction\/sessions\/[^/]+/.test(r.path), select: true },
    { name: 'generation', route: '/task-generation/generate', list: '/api/task-generation/jobs', forbidden: r => /\/jobs\/[^/]+/.test(r.path) || r.path.endsWith('/tree'), select: false },
    { name: 'publishing', route: '/data-publishing/archive', list: '/api/dataset-releases/candidates', forbidden: r => /^\/api\/dataset-releases\/(?!candidates$)[^/]+$/.test(r.path), select: false },
  ]
  for (const width of [1440, 390]) for (const spec of specs) {
    const start = requests.length, errorsBefore = errors.length
    const item = { page: spec.name, width, firstListOnly: false, pageSize20: false, selectedRetained: false }
    try {
      await call('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false })
      await call('Page.navigate', { url: origin + spec.route })
      await until(async () => requests.slice(start).some(r => r.path === spec.list && r.query.limit === '10') && await evaluate('Boolean(document.querySelector(".app-main .el-pagination"))'), 'Initial summary did not render: ' + spec.name)
      await delay(500)
      assert(!requests.slice(start).some(spec.forbidden), 'Initial page fetched heavy detail: ' + JSON.stringify(requests.slice(start).filter(spec.forbidden)))
      item.firstListOnly = true
      const sizeStart = requests.length
      await evaluate('document.querySelector(".app-main .el-pagination .el-pagination__sizes .el-select__wrapper").click()')
      await until(() => evaluate('[...document.querySelectorAll(".el-select-dropdown__item")].some(n=>n.getBoundingClientRect().height && /^20\\s*\\//.test(n.textContent.trim()))'), 'Page-size options did not open')
      await evaluate('[...document.querySelectorAll(".el-select-dropdown__item")].find(n=>n.getBoundingClientRect().height && /^20\\s*\\//.test(n.textContent.trim())).click()')
      await until(() => requests.slice(sizeStart).some(r => r.path === spec.list && r.query.limit === '20' && (r.query.offset || '0') === '0'), '20-row server request missing')
      await delay(250); item.pageSize20 = true
      let selected
      if (spec.name === 'preprocessing') {
        await evaluate('document.querySelector(".batch-toggle").click()'); await delay(400)
        selected = await evaluate('location.search')
      } else if (spec.select) {
        await evaluate('document.querySelector(".app-main .el-select__wrapper").click()')
        await until(() => evaluate('[...document.querySelectorAll(".el-select-dropdown__item")].some(n=>n.getBoundingClientRect().height && /b001/.test(n.textContent))'), 'Batch options did not open')
        await evaluate('[...document.querySelectorAll(".el-select-dropdown__item")].find(n=>n.getBoundingClientRect().height && /b001/.test(n.textContent)).click()')
        await delay(450)
        selected = await evaluate('document.querySelector(".app-main .el-select .el-select__selected-item:not(.el-select__placeholder)")?.textContent || document.querySelector(".app-main .el-select")?.textContent')
      } else if (spec.name === 'generation') {
        await evaluate('document.querySelector(".job-list button").click()')
        await until(() => evaluate('Boolean(document.querySelector(".job-heading"))'), 'Selected job detail missing')
        selected = await evaluate('document.querySelector(".job-heading")?.textContent')
      } else {
        await evaluate('document.querySelector(".candidate input[type=checkbox]").click()')
        await delay(100)
        assert(await evaluate('document.querySelectorAll(".candidate.selected").length') === 1, 'Candidate not selected')
      }
      const pageStart = requests.length
      await evaluate('document.querySelector(".app-main .el-pagination .btn-next").click()')
      await until(() => requests.slice(pageStart).some(r => r.path === spec.list && r.query.offset === '20'), 'Next page did not request offset 20')
      await delay(300)
      if (spec.name === 'preprocessing') item.selectedRetained = selected === await evaluate('location.search')
      else if (spec.select) {
        const after = await evaluate('document.querySelector(".app-main .el-select .el-select__selected-item:not(.el-select__placeholder)")?.textContent || document.querySelector(".app-main .el-select")?.textContent')
        item.selectedRetained = after === selected
      } else if (spec.name === 'generation') item.selectedRetained = selected === await evaluate('document.querySelector(".job-heading")?.textContent')
      else {
        await evaluate('document.querySelector(".app-main .el-pagination .btn-prev").click()'); await delay(350)
        item.selectedRetained = await evaluate('document.querySelectorAll(".candidate.selected").length') === 1
      }
      assert(item.selectedRetained, 'Selection changed when paging history')
      item.layout = await evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth,title:document.querySelector(".app-main h1")?.textContent})')
      if (item.layout.scrollWidth > width + 1) item.layout.offenders = await evaluate('[...document.querySelectorAll("body *")].map(e=>({tag:e.tagName,cls:e.className?.baseVal??e.className,parent:e.parentElement?.className,right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)})).filter(e=>e.right>innerWidth+2&&e.width>0).slice(-35)')
      assert(item.layout.scrollWidth <= width + 1, `Horizontal page overflow: ${item.layout.scrollWidth}px in ${width}px viewport`)
      assert(errors.length === errorsBefore, 'Uncaught browser exception')
      item.passed = true
    } catch (error) { item.passed = false; item.error = String(error); item.body = await evaluate('document.body.innerText.slice(-2200)').catch(() => '') }
    const shot = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    await writeFile(path.join(output, `${spec.name}-${width}.png`), Buffer.from(shot.data, 'base64'))
    scenarios.push(item); console.log(JSON.stringify(item))
  }
  const firstScreens = [
    { name: 'monitor', route: '/phone-factory', api: '/api/phone-factory/state', selector: '.device-card', forbidden: r => r.path.includes('/monitor/') || r.path.includes('/remote/') },
    { name: 'overview', route: '/data-publishing/overview', api: '/api/training-data-overview', selector: '[data-testid=overview-metric-total_trajectories]', forbidden: r => r.path.includes('/workbook') },
    { name: 'mixture', route: '/model-training/data-mixture', api: '/api/dataset-releases', selector: '.mixture-page .el-pagination', forbidden: r => /^\/api\/dataset-releases\/[^/]+$/.test(r.path) },
    { name: 'augmentation', route: '/task-generation/augmentation', api: '/api/task-generation/jobs', selector: '.job-tabs button', forbidden: r => /\/jobs\/[^/]+/.test(r.path) || r.path.endsWith('/tree') },
    { name: 'scenario', route: '/scenario-studio', api: '/api/task-generation/tree', selector: '.scenario-studio-page h1', forbidden: r => r.path.endsWith('/tree') && r.query.view !== 'summary' || r.path.includes('/workbook') },
  ]
  for (const width of [1440, 390]) for (const spec of firstScreens) {
    const start = requests.length, errorsBefore = errors.length, item = { page: spec.name, width, firstScreenOnly: true }
    try {
      await call('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false })
      await call('Page.navigate', { url: origin + spec.route })
      await until(async () => requests.slice(start).some(r => r.path === spec.api) && await evaluate(`Boolean(document.querySelector(${JSON.stringify(spec.selector)}))`), 'First screen did not render: ' + spec.name)
      await delay(650)
      assert(!requests.slice(start).some(spec.forbidden), 'First screen fetched heavy details: ' + spec.name)
      item.layout = await evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,title:document.querySelector(".app-main h1")?.textContent})')
      if (item.layout.scrollWidth > width + 1) item.layout.offenders = await evaluate('[...document.querySelectorAll("body *")].map(e=>({tag:e.tagName,cls:e.className?.baseVal??e.className,parent:e.parentElement?.className,right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)})).filter(e=>e.right>innerWidth+2&&e.width>0).slice(-20)')
      assert(item.layout.scrollWidth <= width + 1, `Horizontal page overflow: ${item.layout.scrollWidth}px in ${width}px viewport`)
      assert(errors.length === errorsBefore, 'Uncaught browser exception')
      item.passed = true
    } catch (error) { item.passed = false; item.error = String(error); item.body = await evaluate('document.body.innerText.slice(-1500)').catch(() => '') }
    const shot = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    await writeFile(path.join(output, `${spec.name}-${width}.png`), Buffer.from(shot.data, 'base64'))
    scenarios.push(item); console.log(JSON.stringify(item))
  }
  const report = { scenarios, errors, consoleErrors, unknown, blocked, requests, scope: 'Built frontend + CDP mock API only; no backend, model, phone or business data.' }
  await writeFile(path.join(output, 'verification.json'), JSON.stringify(report, null, 2))
  console.log('REPORT ' + path.join(output, 'verification.json'))
  if (scenarios.some(item => !item.passed) || errors.length || consoleErrors.length || unknown.length || blocked.length) process.exitCode = 1
  await call('Browser.close')
} finally {
  await cleanup()
}
