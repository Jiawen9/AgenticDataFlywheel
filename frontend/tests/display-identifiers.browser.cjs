// Every API request is fulfilled here; no collector, phone, backend or model is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os'), http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const long = (prefix, number) => prefix + '_' + number.toString(16).padStart(64, 'a')
const batchId = long('manual', 1), runId = long('cr', 1), evalId = long('eval', 1)
const batchAlias = 'b261026-001', runAlias = 'cr261026-001', evalAlias = 'eval261026-001'
const filename = `collection-batch-${batchId}.xlsx`, originalFilename = '导航回归用例.xlsx'
const batches = Array.from({ length: 8 }, (_, i) => ({ schema_version: 1, batch_id: i ? long('manual', i + 1) : batchId, source_job_id: null,
  kind: 'manual_collection', job_status: 'succeeded', created_at: '2026-10-26T10:00:00+08:00', task_count: 1, apps: ['地图'],
  filename: i ? `collection-batch-${long('manual', i + 1)}.xlsx` : filename, snapshot: { tasks: [] } }))
const runs = Array.from({ length: 8 }, (_, i) => ({ collection_run_id: long('cr', i + 1), run_id: long('cr', i + 1), batch_id: batches[i].batch_id,
  status: 'running', transfer_status: 'waiting', filename: batches[i].filename, created_at: '2026-10-26T11:00:00+08:00', errors: [] }))
const evalRuns = [{ run_id: evalId, status: 'running', execution_mode: 'collect_and_evaluate', evaluation_profile_id: '规则V1', source: { run_id: runId } }]
const aliases = new Map([...batches.map((value, i) => [value.batch_id, `b261026-${String(i + 1).padStart(3, '0')}`]),
  ...runs.map((value, i) => [value.run_id, `cr261026-${String(i + 1).padStart(3, '0')}`]), [evalId, evalAlias]])
const historicalId = long('cr_history', 99)
runs.push({ collection_run_id: historicalId, run_id: historicalId, batch_id: batchId, status: 'completed', transfer_status: 'completed', filename: '历史轨迹.xlsx', errors: [] })
const state = { phones: [], apps: ['地图'], phoneApps: [], vla: ['http://mock-vla/v1'],
  tasks: [{ description: '地图导航回归', filename, original_filename: originalFilename, source_batch_id: batchId, status: '未运行' }] }
const config = { sampling_enabled: false, temperature: 0.7, top_p: 0.85, use_experience_lib: false, capture_logprobs: false }
const evaluator = { text: { base_url: 'http://mock-text/v1', model: 'text', api_key_env: 'TEXT' }, vision: { base_url: 'http://mock-vision/v1', model: 'vision', api_key_env: 'VISION' } }
const summary = { total: 1, collection_completed: 1, collection_failed: 0, evaluation_completed: 0, evaluation_failed: 0, evaluation_running: 1, evaluation_pending: 0, success: 0, partial_success: 0, fail: 0, success_rate: null }
const pause = ms => new Promise(done => setTimeout(done, ms))
async function eventually(check, description) {
  const deadline = Date.now() + 15_000
  while (!check()) { if (Date.now() > deadline) throw new Error(`Timed out: ${description}`); await pause(25) }
}

async function main() {
  const output = process.env.ADF_BROWSER_ARTIFACTS || await fs.mkdtemp(path.join(os.tmpdir(), 'adf-display-identifiers-'))
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
      let file = path.resolve(dist, '.' + pathname)
      if (!file.startsWith(dist + path.sep) || !path.extname(file)) file = path.join(dist, 'index.html')
      res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream')
      res.end(await fs.readFile(file))
    } catch { res.statusCode = 404; res.end('missing') }
  })
  await new Promise(done => server.listen(0, '127.0.0.1', done))
  const base = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 }, serviceWorkers: 'block' })
  const errors = [], unexpected = [], calls = [], cases = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    window.__copiedDisplayId = ''
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { window.__copiedDisplayId = value } } })
  })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== base) { unexpected.push(url.href); return route.abort() }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const method = route.request().method(), body = route.request().postDataJSON()
    calls.push({ path: url.pathname, method, body, query: Object.fromEntries(url.searchParams) })
    const send = json => route.fulfill({ json })
    if (url.pathname === '/api/display-identifiers/resolve') {
      assert.equal(method, 'POST')
      return send({ items: body.items.map(item => ({ ...item, scope: item.scope || '', display_id: aliases.get(item.id) || item.id, mapped: aliases.has(item.id) })) })
    }
    if (url.pathname === '/api/pipelines') return send({ pipelines: [] })
    if (url.pathname.endsWith('/lifecycle')) return send({ batch_id: url.pathname.split('/')[3], status: 'active', release_id: null })
    if (url.pathname === '/api/phone-factory/batches') return send({ batches })
    const batch = url.pathname.match(/^\/api\/phone-factory\/batches\/([^/]+)(\/workbook)?$/)
    if (batch) return batch[2] ? route.fulfill({ body: 'frozen workbook fixture' }) : send(batches.find(value => value.batch_id === batch[1]))
    const endpoint = url.pathname.match(/^\/api\/(phone-factory|model-iter)(\/.*)$/)
    if (endpoint) {
      const evaluation = endpoint[1] === 'model-iter', action = endpoint[2]
      if (action === '/state') return send(state)
      if (action === '/config') return send(config)
      if (action === '/remote/status') return send({ ok: true, statuses: [] })
      if (action === '/remote/adb-devices') return send({ ok: true, devices: [] })
      if (action === '/collection-runs') return send({ runs })
      if (action === '/runs') return send({ runs: evalRuns })
      if (action === '/tasks' && method === 'POST') return send({ ...state, imported_task: state.tasks[0] })
      if (action === '/remote/start-run') return send({ ok: true, run_id: evaluation ? evalId : runId, collection_run_id: evaluation ? undefined : runId, batch_id: evaluation ? undefined : batchId })
      if (action === '/evaluation-config') return send(evaluator)
      if (action === '/evaluation-capabilities') return send({ ok: true, supported: true, offline_supported: true, profiles_supported: true, message: '本地模拟服务' })
      if (action === '/evaluation-profiles') return send({ profiles: [{ id: '规则V1', name: '固定规则', digest: 'fixture-rule-hash' }], errors: [] })
      if (action === '/evaluation-sources') return send({ sources: [{ run_id: runId, status: 'completed', trajectory_count: 1 }], allowed_roots: [] })
      if (action === '/evaluation-preview') return send({ preview_id: 'fixture-preview', valid: true, trajectory_count: 1, cases: [{ source_trajectory_id: 'original-trajectory', case_id: 'CASE-001', task: '地图导航' }], errors: [], source: { run_id: runId }, source_digest: 'fixture-source' })
      if (action === '/reports') return send({ ok: true, folders: [{ index: 1, dir_name: evalId, run_id: evalId, modified: 1792983600, files: [{ name: '评估结果.xlsx', file_id: 'fixture-report', size: 100 }] }] })
      if (action === '/report-download') return route.fulfill({ body: 'unchanged report fixture' })
      const sync = action.match(/^\/collection-runs\/([^/]+)\/sync$/)
      if (sync) return send(runs.find(run => run.collection_run_id === sync[1]))
      const results = action.match(/^\/runs\/([^/]+)\/evaluations$/)
      if (results) return send({ ...evalRuns[0], run_id: results[1], summary, cases: [{ case_key: 'fixture-case', case_id: 'CASE-001', phone_id: 'PHONE-ORIGINAL', collection_status: 'completed', evaluation_status: 'running', attempt_count: 1, errors: [] }] })
      const cancel = action.match(/^\/runs\/([^/]+)\/cancel$/)
      if (cancel) { evalRuns[0].status = 'cancelled'; return send(evalRuns[0]) }
    }
    unexpected.push(`${method} ${url.pathname}`)
    return route.fulfill({ status: 404, json: { error: 'Unexpected fixture request' } })
  })
  try {
    await page.goto(`${base}/collection/phone-factory?batch_id=${batchId}`)
    await page.getByRole('heading', { name: '手机工厂采集', exact: true }).waitFor()
    await page.locator('.batch-summary').getByRole('button', { name: `查看完整编号 ${batchAlias}`, exact: true }).waitFor()
    const runTable = page.getByTestId('factory-runs'), row = runTable.locator('.el-table__body .el-table__row').filter({ hasText: runAlias })
    await row.getByRole('button', { name: `查看完整编号 ${runAlias}`, exact: true }).waitFor()
    assert.equal(await page.getByText(originalFilename, { exact: true }).count(), 1)
    assert.equal(await page.locator('.batch-summary').getByRole('link', { name: '前往预处理' }).getAttribute('href'), `/collection/tree-building?batch_id=${batchId}`)
    assert.ok(!(await runTable.innerText()).includes(runId), 'long identifier is not the visible table label')
    assert.ok((await runTable.innerText()).includes(historicalId), 'unmapped history retains its original identifier')
    await row.getByRole('button', { name: `复制完整编号 ${runAlias}`, exact: true }).click()
    assert.equal(await page.evaluate(() => window.__copiedDisplayId), runId)
    await row.getByRole('button', { name: `查看完整编号 ${runAlias}`, exact: true }).click()
    await page.locator('.el-popover:visible code').filter({ hasText: runId }).waitFor()
    await page.getByRole('heading', { name: '手机工厂采集', exact: true }).click()
    cases.push('mapped run and batch labels; original filename; full-ID inspection/copy and route')

    for (const query of [runId, runAlias]) {
      await page.getByRole('textbox', { name: '搜索运行', exact: true }).fill(query)
      assert.equal(await runTable.locator('.el-table__body .el-table__row').count(), 1)
      assert.match(await runTable.innerText(), /cr261026-001/)
    }
    await row.getByRole('button', { name: '重试回传', exact: true }).click()
    await eventually(() => calls.some(call => call.path === `/api/phone-factory/collection-runs/${runId}/sync` && call.method === 'POST'), 'sync original run')
    await page.getByRole('textbox', { name: '搜索运行', exact: true }).fill('')
    const batchPicker = page.locator('.batch-select')
    for (const query of [batchId, batchAlias]) {
      await batchPicker.click(); await batchPicker.locator('input').fill(query)
      await page.getByRole('option').filter({ hasText: batchAlias }).waitFor()
      assert.equal(await page.getByRole('option').filter({ hasText: batchAlias }).count(), 1)
      await page.getByRole('option').filter({ hasText: batchAlias }).click()
    }
    await page.getByRole('button', { name: '开始运行所选批次', exact: true }).click()
    await eventually(() => calls.some(call => call.path === '/api/phone-factory/remote/start-run'), 'start fixture collection')
    assert.equal(calls.find(call => call.path === '/api/phone-factory/tasks' && call.method === 'POST').body.source_batch_id, batchId)
    assert.equal(calls.find(call => call.path === '/api/phone-factory/remote/start-run').body.filename, filename)
    const resolveCalls = calls.filter(call => call.path === '/api/display-identifiers/resolve')
    assert.ok(resolveCalls.some(call => call.body.items.length >= 8), 'rows must share a batch lookup')
    assert.ok(resolveCalls.length < 8, 'eight rows must not cause one request each')
    const lookedUp = resolveCalls.flatMap(call => call.body.items).map(item => JSON.stringify([item.kind, item.scope || '', item.id]))
    assert.equal(new Set(lookedUp).size, lookedUp.length, 'positive mappings are reused across controls')
    cases.push('full ID and alias both searchable; POST values unchanged; batch request coalescing')
    await page.screenshot({ path: path.join(output, 'collection-identifiers.png'), fullPage: true })

    await page.goto(`${base}/model-iteration-evaluation`)
    await page.getByTestId('model-iteration-panel').waitFor()
    const evaluationPanel = page.getByTestId('model-iteration-panel')
    await evaluationPanel.getByRole('button', { name: `查看完整编号 ${evalAlias}`, exact: true }).waitFor()
    const runPicker = evaluationPanel.locator('.run-picker')
    for (const query of [evalId, evalAlias]) {
      await runPicker.click(); await runPicker.locator('input').fill(query)
      await page.getByRole('option').filter({ hasText: evalAlias }).click()
    }
    await eventually(() => calls.some(call => call.path === `/api/model-iter/runs/${evalId}/evaluations`), 'evaluation uses original run')
    const folder = page.locator('.report-folder').first()
    await folder.getByRole('button', { name: `查看完整编号 ${evalAlias}`, exact: true }).waitFor()
    assert.equal((await folder.innerText()).split(evalAlias).length - 1, 1, 'report title does not repeat run ID')
    await folder.getByRole('button', { name: '下载报告', exact: true }).click()
    await eventually(() => calls.some(call => call.path === '/api/model-iter/report-download'), 'download fixture report')
    assert.equal(calls.find(call => call.path === '/api/model-iter/report-download').query.run_id, evalId)
    assert.equal(calls.find(call => call.path === '/api/model-iter/report-download').query.name, '评估结果.xlsx')
    await evaluationPanel.locator('.el-radio-button').filter({ hasText: '已有轨迹单独评测' }).click()
    assert.equal(await page.getByPlaceholder('9011 服务器上的完整轨迹目录').count(), 0, 'run source without a selection does not display the directory input')
    const sourcePicker = page.getByTestId('offline-evaluation').locator('.el-select')
    await sourcePicker.click(); await sourcePicker.locator('input').fill(runId)
    await page.getByRole('option').filter({ hasText: runAlias }).click()
    await page.getByRole('button', { name: '检查轨迹并预览', exact: true }).click()
    await eventually(() => calls.some(call => call.path === '/api/model-iter/evaluation-preview'), 'preview original source run')
    assert.deepEqual(calls.find(call => call.path === '/api/model-iter/evaluation-preview').body, { source_run_id: runId })
    await evaluationPanel.getByRole('button', { name: `复制完整编号 ${evalAlias}`, exact: true }).click()
    assert.equal(await page.evaluate(() => window.__copiedDisplayId), evalId)
    cases.push('evaluation picker and source aliases preserve source/run request identity; reports preserve filenames')
    await page.screenshot({ path: path.join(output, 'evaluation-identifiers.png'), fullPage: true })
    await page.setViewportSize({ width: 760, height: 1080 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true, 'identifiers do not widen mobile layout')
    await page.screenshot({ path: path.join(output, 'evaluation-identifiers-narrow.png'), fullPage: true })
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
    const result = { passed: true, cases, output, errors, unexpected, apiCalls: calls.length }
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result))
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), errors, unexpected, calls }, null, 2))
    console.error('Browser artifacts: ' + output); throw error
  } finally { await browser.close(); await new Promise(done => server.close(done)) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
