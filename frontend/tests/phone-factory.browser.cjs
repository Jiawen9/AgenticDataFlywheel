// All API requests are fulfilled locally. No device, model, collector or upload service is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const copy = value => JSON.parse(JSON.stringify(value))
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
const batch = id => ({
  schema_version: 1, batch_id: id, source_job_id: id === 'manual-one' ? null : id,
  kind: id === 'manual-one' ? 'manual_collection' : id === 'augmented-one' ? 'augmentation' : 'task_generation',
  job_status: 'succeeded', knowledge_base_version: null, created_at: '2026-09-21T10:00:00+08:00',
  task_count: 1, apps: ['示例App'], filename: 'collection-batch-' + id + '.xlsx', download_url: '',
  warnings: id === 'manual-one' ? [{ sheet: 'Sheet1', row: 2, field: '一级场景、二级场景', message: '场景缺失，后续汇总显示为未分类' }] : [],
  snapshot: { tasks: [] },
})
async function eventually(check, description, timeout = 15000) {
  const end = Date.now() + timeout
  while (!check()) {
    if (Date.now() >= end) throw new Error('Timed out: ' + description)
    await pause(40)
  }
}
async function main() {
  const output = await fs.mkdtemp(path.join(os.tmpdir(), 'adf-phone-factory-browser-'))
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
      let file = path.resolve(dist, '.' + pathname)
      if (!file.startsWith(dist + path.sep) || !path.extname(file)) file = path.join(dist, 'index.html')
      res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream')
      res.end(await fs.readFile(file))
    } catch { res.statusCode = 404; res.end('missing') }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 }, serviceWorkers: 'block' })
  const errors = [], unexpected = [], calls = [], cases = []
  const batches = [batch('generated-one'), batch('augmented-one')]
  const published = new Set(), accepted = new Map()
  const state = {
    phones: ['phone-1', 'phone-2'], apps: ['示例App', '另一个App'],
    phoneApps: [{ phone_id: 'phone-1', app: '示例App', status: '空闲' }, { phone_id: 'phone-1', app: '另一个App', status: '空闲' }, { phone_id: 'phone-2', app: '示例App', status: '空闲' }],
    vla: ['http://mock-vla:8000/v1'], tasks: [],
  }
  const evaluationTasks = [{ filename: 'evaluation.xlsx', description: '独立评估任务', status: '未运行' }]
  const runs = [], evalRuns = []
  let uploadAttempts = 0, generateAttempts = 0, deleteAttempts = 0, failReports = false
  let holdState = false, releaseState = null, holdMonitor = '', releaseMonitor = null
  const folders = [
    { index: 1, dir_name: 'evaluation-round-one', run_id: 'eval-report-1', modified: 1790000000, files: [{ name: '评估结果.xlsx', size: 1024, modified: 1790000100, file_id: 'report-1' }] },
    { index: 2, dir_name: 'evaluation-round-empty', run_id: 'eval-report-2', modified: 1790000200, files: [] },
  ]
  const apiCalls = (endpoint, method) => calls.filter(item => item.path === endpoint && (!method || item.method === method))
  const modeState = evaluation => ({
    ...copy(state),
    tasks: evaluation ? copy(evaluationTasks) : copy(state.tasks.filter(task => !published.has(task.source_batch_id))),
  })
  const phoneRow = (phone, app = '示例App') => page.getByTestId('phone-apps').locator('.el-table__body .el-table__row')
    .filter({ has: page.getByText(phone, { exact: true }) }).filter({ has: page.getByText(app, { exact: true }) })
  const dialog = () => page.getByRole('dialog', { name: '定制运行', exact: true })
  async function openCustom(phone, app = '示例App') {
    await phoneRow(phone, app).getByRole('button', { name: '定制运行', exact: true }).click()
    await dialog().waitFor()
    assert.equal(await page.getByTestId('custom-run-phone').innerText(), phone)
    assert.equal(await page.getByTestId('custom-run-app').innerText(), app)
    assert.equal(await page.getByTestId('custom-run-phone').locator('input').count(), 0)
    assert.equal(await page.getByTestId('custom-run-app').locator('input').count(), 0)
  }
  async function chooseTask(label) {
    await page.getByTestId('custom-run-task-select').click()
    const option = page.getByRole('option').filter({ hasText: label })
    assert.equal(await option.count(), 1, 'task must appear once: ' + label)
    await option.click()
  }
  async function syncAll() {
    for (const run of runs.filter(item => item.status === 'running')) {
      const row = page.getByTestId('factory-runs').locator('.el-table__body .el-table__row').filter({ hasText: run.collection_run_id })
      await row.getByRole('button', { name: '重试回传', exact: true }).waitFor({ timeout: 15000 })
      await row.getByRole('button', { name: '重试回传', exact: true }).click()
      await row.getByText('2 条轨迹', { exact: true }).waitFor()
    }
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  }
  page.on('pageerror', error => errors.push(String(error)))
  try {
    const mockRoute = async route => {
      const url = new URL(route.request().url())
      if (url.origin !== base) { unexpected.push('external: ' + url.href); return route.abort() }
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const method = route.request().method(), body = route.request().postDataJSON()
      calls.push({ path: url.pathname, method, body, query: Object.fromEntries(url.searchParams), at: Date.now() })
      const send = json => route.fulfill({ json })
      if (url.pathname.endsWith('/lifecycle')) {
        const id = url.pathname.split('/')[3]
        return send({ status: published.has(id) ? 'published' : 'active', batch_id: id, release_id: published.has(id) ? 'rel-browser' : null })
      }
      if (url.pathname === '/api/phone-factory/batches') return send({ batches: batches.filter(item => !published.has(item.batch_id)) })
      const batchMatch = url.pathname.match(/^\/api\/phone-factory\/batches\/([^/]+)(\/workbook)?$/)
      if (batchMatch) {
        if (batchMatch[2]) return route.fulfill({ body: 'frozen workbook' })
        return send(batches.find(item => item.batch_id === batchMatch[1]))
      }
      const api = url.pathname.match(/^\/api\/(phone-factory|model-iter)(\/.*)$/)
      if (!api) { unexpected.push(method + ' ' + url.pathname); return route.fulfill({ status: 404, json: { error: 'Unexpected API' } }) }
      const evaluation = api[1] === 'model-iter', action = api[2]
      if (action === '/state') {
        const snapshot = modeState(evaluation)
        if (holdState && !evaluation) {
          holdState = false
          await new Promise(resolve => { releaseState = resolve })
        }
        return send(snapshot).catch(() => {})
      }
      if (action === '/config' && method === 'GET') return send({ sampling_enabled: false, temperature: 0.7, top_p: 0.85, use_experience_lib: false })
      if (action === '/tasks' && method === 'POST') {
        if (!body.source_batch_id && ++uploadAttempts === 1) return route.fulfill({ status: 502, json: { error: '模拟上传响应丢失，请重试' } })
        const id = body.source_batch_id || 'manual-one', filename = batch(id).filename
        if (!state.tasks.some(item => item.filename === filename)) {
          if (!batches.some(item => item.batch_id === id)) batches.push(batch(id))
          state.tasks.push({ filename, description: body.description, status: '未运行', source_batch_id: id })
        }
        return send({ ...modeState(evaluation), imported_task: { filename, source_batch_id: id, warnings: batch(id).warnings } })
      }
      if (action === '/remote/status') return send({ ok: true, statuses: body.phones.map(id => ({
        phone_id: id, status: [...runs, ...evalRuns].some(run => run.phone_id === id && run.status === 'running') ? '运行中' : '空闲',
      })) })
      if (action === '/remote/adb-devices') return send({ ok: true, devices: state.phones.concat('phone-3').map((id, index) => ({ serial: id, model: '模拟手机 ' + id, battery: 75 - index })) })
      if (action === '/remote/monitor') {
        if (holdMonitor === body.phone_id) {
          holdMonitor = ''
          await new Promise(resolve => { releaseMonitor = resolve })
        } else await pause(80)
        return send({ ok: true, screenshot: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwZkAAAAASUVORK5CYII=', log: body.phone_id + ' 的模拟采集日志', running: false, device_size: { width: 1080, height: 2400 } }).catch(() => {})
      }
      if (action === '/remote/start-run') {
        let run = accepted.get(body.request_id)
        if (!run) {
          const id = evaluation ? 'eval-' + (evalRuns.length + 1) : 'cr-' + (runs.length + 1)
          const source = state.tasks.find(item => item.filename === body.filename)
          run = { run_id: id, collection_run_id: id, batch_id: evaluation ? undefined : source?.source_batch_id, run_mode: evaluation ? 'modeliter' : 'generate', filename: body.filename, phone_id: body.phone_id, phone_ids: [body.phone_id], status: 'running', transfer_status: 'waiting', vla: body.vla, created_at: new Date().toISOString(), errors: [] }
          accepted.set(body.request_id, run)
          if (evaluation) { evalRuns.push(run); evaluationTasks[0].status = '运行中' }
          else { runs.push(run); source.status = '运行中' }
        }
        // The collector has accepted the first request; only its response is lost.
        if (!evaluation && ++generateAttempts === 1) return route.fulfill({ status: 502, json: { error: '模拟网络响应丢失，请重试' } })
        return send({ ...run, ok: true, message: evaluation ? '评估已下发' : '采集已下发' })
      }
      if (action === '/collection-runs') return send({ runs: runs.filter(run => !published.has(run.batch_id)) })
      if (action === '/runs') return send({ runs: evalRuns })
      const sync = action.match(/^\/collection-runs\/([^/]+)\/sync$/)
      if (sync) {
        const run = runs.find(item => item.collection_run_id === sync[1])
        Object.assign(run, { status: 'completed', trajectory_count: 2, transfer_status: 'completed' })
        return send(run)
      }
      if (action === '/remote/del-phone') {
        if (++deleteAttempts === 1) return route.fulfill({ status: 502, json: { error: '模拟停止设备失败' } })
        state.phones = state.phones.filter(id => id !== body.phone_id)
        state.phoneApps = state.phoneApps.filter(item => item.phone_id !== body.phone_id)
        return send({ ok: true, state: modeState(evaluation) })
      }
      if (action === '/reports') {
        if (failReports) { failReports = false; return route.fulfill({ status: 502, json: { error: '模拟报告刷新失败' } }) }
        return send({ ok: true, folders })
      }
      if (action === '/report-download') return route.fulfill({ body: 'frozen evaluation report' })
      unexpected.push(method + ' ' + url.pathname)
      return route.fulfill({ status: 404, json: { error: 'unexpected mock endpoint ' + action } })
    }
    await page.route('**/*', mockRoute)

    await page.goto(base + '/collection/phone-factory')
    await page.getByRole('heading', { name: '手机工厂采集', exact: true }).waitFor()
    await page.getByPlaceholder('任务描述').fill('手工采集任务')
    await page.locator('input[type=file]').setInputFiles({ name: 'manual.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('fixture workbook bytes') })
    const uploadButton = page.locator('.traj-panel .row-form').filter({ has: page.getByPlaceholder('任务描述') }).getByRole('button', { name: '新增', exact: true })
    await uploadButton.click()
    await page.getByText('模拟上传响应丢失，请重试', { exact: true }).waitFor()
    await uploadButton.click()
    await page.locator('.batch-summary').waitFor()
    const uploads = apiCalls('/api/phone-factory/tasks').filter(item => !item.body.source_batch_id)
    assert.equal(uploads.length, 2)
    assert.ok(uploads[0].body.request_id)
    assert.deepEqual(uploads[0].body, uploads[1].body)
    assert.match(await page.locator('.batch-summary').innerText(), /来源：手动上传/)
    assert.match(await page.getByTestId('collection-classification-warning').innerText(), /1 条分类提示.*第 2 行.*未分类/)
    assert.equal(await page.locator('.batch-summary').getByRole('link', { name: '前往预处理' }).getAttribute('href'), '/collection/tree-building?batch_id=manual-one')
    assert.equal(await page.locator('.traj-panel').getByRole('button', { name: '定制运行', exact: true }).count(), 0)
    cases.push('manual upload retry identity and classification/preprocessing mapping', 'custom run entry only on phone rows')

    await openCustom('phone-1')
    await page.getByTestId('custom-run-task-select').click()
    for (const id of ['manual-one', 'generated-one', 'augmented-one']) assert.equal(await page.getByRole('option').filter({ hasText: id }).count(), 1)
    await page.getByRole('option').filter({ hasText: 'manual-one' }).click()
    await page.getByTestId('custom-run-vla').click()
    await page.getByRole('option', { name: 'http://mock-vla:8000/v1', exact: true }).click()
    await dialog().locator('.el-switch').first().click()
    await dialog().getByRole('spinbutton').first().fill('0.35')
    await dialog().getByRole('button', { name: '确定', exact: true }).click()
    await page.getByText('模拟网络响应丢失，请重试', { exact: true }).waitFor()
    await dialog().getByRole('button', { name: '确定', exact: true }).click()
    await dialog().waitFor({ state: 'hidden' })
    let submissions = apiCalls('/api/phone-factory/remote/start-run')
    assert.equal(submissions.length, 2)
    assert.deepEqual(submissions[0].body, submissions[1].body)
    assert.equal(submissions[1].body.phone_id, 'phone-1')
    assert.equal(submissions[1].body.app, '示例App')
    assert.equal(submissions[1].body.config.temperature, 0.35)
    assert.equal(submissions[1].body.config.sampling_enabled, true)
    assert.equal(submissions[1].body.vla, 'http://mock-vla:8000/v1')
    assert.equal(runs.length, 1, 'lost response retry must reuse the accepted run')

    await openCustom('phone-2')
    assert.equal(await dialog().locator('.el-switch input').first().isChecked(), false)
    await chooseTask('manual-one')
    await dialog().getByRole('button', { name: '确定', exact: true }).click()
    await dialog().waitFor({ state: 'hidden' })
    submissions = apiCalls('/api/phone-factory/remote/start-run')
    assert.equal(submissions.length, 3)
    assert.equal(submissions[2].body.phone_id, 'phone-2')
    assert.equal(submissions[2].body.filename, submissions[1].body.filename)
    assert.notEqual(submissions[2].body.request_id, submissions[1].body.request_id)
    assert.equal(submissions[2].body.config.temperature, 0.7)
    assert.equal(submissions[2].body.config.sampling_enabled, false)
    assert.equal(new Set(runs.map(run => run.batch_id)).size, 1)
    assert.equal(new Set(runs.map(run => run.collection_run_id)).size, 2)
    assert.equal(apiCalls('/api/phone-factory/config', 'POST').length, 0)
    assert.equal(calls.filter(item => item.path.endsWith('/tasks/start')).length, 0)
    cases.push('same running batch dispatches on a second idle phone', 'lost response retry keeps request_id', 'per-run sampling never saves global config')
    await syncAll()

    // A generated batch need not already be imported into the task table.
    assert.equal(state.tasks.some(task => task.source_batch_id === 'generated-one'), false)
    await openCustom('phone-2')
    await chooseTask('generated-one')
    await dialog().getByRole('button', { name: '确定', exact: true }).click()
    await dialog().waitFor({ state: 'hidden' })
    assert.equal(state.tasks.some(task => task.source_batch_id === 'generated-one'), true)
    assert.equal(apiCalls('/api/phone-factory/remote/start-run').at(-1).body.filename, batch('generated-one').filename)
    assert.match(await page.locator('.batch-summary').innerText(), /来源：手动上传/)
    assert.equal(await page.locator('.batch-summary').getByRole('link', { name: '前往预处理' }).getAttribute('href'), '/collection/tree-building?batch_id=manual-one')
    await syncAll()
    await page.screenshot({ path: path.join(output, 'collection.png'), fullPage: true })
    cases.push('unimported generated batch is selectable without changing selected batch', 'successful HTTP result synchronization remains available')

    // A delayed old /state response must not repopulate a published batch or its dialog.
    await openCustom('phone-1')
    await chooseTask('manual-one')
    holdState = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await eventually(() => Boolean(releaseState), 'delayed state request')
    published.add('manual-one')
    await page.evaluate(() => {
      const channel = new BroadcastChannel('agentic-data-flywheel.batch-lifecycle.v1')
      channel.postMessage({ type: 'batches-published', event_id: 'browser-published-manual', batch_ids: ['manual-one'], release_id: 'rel-browser' })
      setTimeout(() => channel.close(), 100)
    })
    await dialog().waitFor({ state: 'hidden' })
    releaseState()
    releaseState = null
    await page.getByText(/已发布/).first().waitFor()
    await page.waitForFunction(() => !document.querySelector('.batch-summary'))
    assert.equal(new URL(page.url()).searchParams.has('batch_id'), false)
    await openCustom('phone-1')
    await page.getByTestId('custom-run-task-select').click()
    assert.equal(await page.getByRole('option').filter({ hasText: 'manual-one' }).count(), 0)
    assert.equal(await page.getByRole('option').filter({ hasText: 'generated-one' }).count(), 1)
    await page.keyboard.press('Escape')
    await dialog().getByRole('button', { name: '取消', exact: true }).click()
    cases.push('published batch closes custom dialog and late state cannot restore it')

    const phoneTable = page.getByTestId('phone-apps')
    await phoneTable.getByRole('button', { name: '删除手机', exact: true }).first().click()
    await page.getByRole('button', { name: '停止并删除', exact: true }).click()
    await page.getByText('模拟停止设备失败', { exact: true }).waitFor()
    assert.equal(await phoneTable.locator('.el-table__body .el-table__row').filter({ hasText: 'phone-1' }).count(), 2)
    await phoneTable.getByRole('button', { name: '删除手机', exact: true }).first().click()
    await page.getByRole('button', { name: '停止并删除', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('[data-testid=phone-apps]')?.textContent.includes('phone-1'))
    assert.equal(state.phoneApps.some(row => row.phone_id === 'phone-1'), false)
    cases.push('failed remote delete preserves rows; successful delete removes all phone associations')

    await page.goto(base + '/phone-factory')
    await page.getByTestId('factory-connected-count').getByText('2', { exact: true }).waitFor()
    assert.equal(await page.getByTestId('factory-phone-count').innerText(), '2')
    assert.equal(await page.getByTestId('factory-task-count').innerText(), '1')
    const device = page.locator('[data-device-id="phone-2"]')
    assert.match(await device.innerText(), /运行 App.*示例App/s)
    await device.click()
    await page.getByTestId('phone-live-log').filter({ hasText: 'phone-2 的模拟采集日志' }).waitFor()
    await page.getByTestId('phone-live-screen').waitFor()
    assert.match(await page.getByRole('dialog').innerText(), /运行 App：示例App/)
    const detailBefore = apiCalls('/api/phone-factory/remote/monitor').length
    const listBefore = apiCalls('/api/phone-factory/state').length
    await page.getByTestId('factory-monitor-refresh').click()
    await eventually(() => apiCalls('/api/phone-factory/remote/monitor').length > detailBefore, 'manual detail refresh')
    await eventually(() => apiCalls('/api/phone-factory/remote/monitor').length >= detailBefore + 2, 'one-second detail polling', 3500)
    assert.equal(apiCalls('/api/phone-factory/state').length, listBefore, 'detail polling must not reload device list each second')
    await eventually(() => apiCalls('/api/phone-factory/state').length > listBefore, 'ten-second device polling', 12000)
    await page.screenshot({ path: path.join(output, 'monitor.png'), fullPage: true })

    holdMonitor = 'phone-2'
    await page.getByTestId('factory-monitor-refresh').click()
    await eventually(() => Boolean(releaseMonitor), 'delayed phone-2 monitor response')
    await page.getByRole('dialog').getByLabel('Close this dialog').click()
    await page.locator('[data-device-id="phone-3"]').click()
    await page.getByTestId('phone-live-log').filter({ hasText: 'phone-3 的模拟采集日志' }).waitFor()
    releaseMonitor()
    releaseMonitor = null
    await pause(120)
    assert.equal((await page.getByTestId('phone-live-log').innerText()).includes('phone-2'), false)
    cases.push('monitor restores App and three counts', 'separate 10-second list and 1-second detail polling with manual refresh', 'late monitor response cannot cross devices')

    await page.goto(base + '/model-iteration-evaluation')
    await page.getByRole('heading', { name: '模型迭代评估', exact: true }).waitFor()
    assert.equal(await page.getByText('采集任务批次', { exact: true }).count(), 0)
    assert.equal(await page.getByRole('link', { name: '前往预处理' }).count(), 0)
    await openCustom('phone-2')
    await page.getByTestId('custom-run-task-select').click()
    assert.equal(await page.getByRole('option').filter({ hasText: 'generated-one' }).count(), 0)
    assert.equal(await page.getByRole('option').filter({ hasText: 'augmented-one' }).count(), 0)
    await page.getByRole('option').filter({ hasText: '独立评估任务' }).click()
    await dialog().getByRole('button', { name: '确定', exact: true }).click()
    await dialog().waitFor({ state: 'hidden' })
    await page.getByText('评估已下发', { exact: true }).waitFor()
    const evaluationRequest = apiCalls('/api/model-iter/remote/start-run').at(-1).body
    assert.equal(evaluationRequest.run_mode, 'modeliter')
    assert.equal(evaluationRequest.phone_id, 'phone-2')
    assert.equal(evaluationRequest.app, '示例App')
    assert.equal(evaluationRequest.filename, 'evaluation.xlsx')
    assert.equal(apiCalls('/api/model-iter/config', 'POST').length, 0)
    const reports = page.getByTestId('evaluation-reports')
    await reports.getByText('第 2 轮暂无 xlsx 报告文件', { exact: true }).waitFor()
    assert.match(await reports.innerText(), /第 1 轮.*evaluation-round-one/s)
    assert.match(await reports.innerText(), /轮次时间：.*2026/s)
    await reports.getByRole('button', { name: '下载报告', exact: true }).waitFor()
    failReports = true
    await reports.getByRole('button', { name: '刷新报告', exact: true }).click()
    await reports.getByText(/模拟报告刷新失败/).waitFor()
    assert.equal(await reports.getByRole('button', { name: '下载报告', exact: true }).count(), 1, 'failed refresh preserves known reports')
    await reports.getByRole('button', { name: '刷新报告', exact: true }).click()
    await reports.getByText(/模拟报告刷新失败/).waitFor({ state: 'hidden' })
    const downloadPromise = page.waitForEvent('download')
    await reports.getByRole('button', { name: '下载报告', exact: true }).click()
    const download = await downloadPromise
    assert.equal(download.suggestedFilename(), '评估结果.xlsx')
    assert.deepEqual(apiCalls('/api/model-iter/report-download').at(-1).query, { run_id: 'eval-report-1', file_id: 'report-1' })
    cases.push('evaluation uses phone-row custom run and isolated task options', 'report rounds/timestamps/empty rounds and stable downloads', 'failed report refresh preserves prior reports and can retry')

    await page.screenshot({ path: path.join(output, 'evaluation.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 900 })
    await page.screenshot({ path: path.join(output, 'evaluation-mobile.png'), fullPage: true })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true)
    // A fresh context has no prior publication cache or broadcast history.
    // Focus reconciliation must inspect both selected batch A and custom draft B.
    const focusA = { ...batch('focus-active-one'), kind: 'manual_collection', source_job_id: null }
    const focusB = batch('focus-target-one')
    batches.push(focusA, focusB)
    state.tasks.push({ filename: focusA.filename, description: '焦点检查手工批次', status: '未运行', source_batch_id: focusA.batch_id })
    for (const run of evalRuns) run.status = 'completed'
    const focusPage = await browser.newPage({ viewport: { width: 1560, height: 1080 }, serviceWorkers: 'block' })
    focusPage.on('pageerror', error => errors.push(String(error)))
    try {
      await focusPage.route('**/*', mockRoute)
      await focusPage.goto(base + '/collection/phone-factory?batch_id=' + focusA.batch_id)
      await focusPage.locator('.batch-summary').waitFor()
      const row = focusPage.getByTestId('phone-apps').locator('.el-table__body .el-table__row').filter({ hasText: 'phone-2' })
      await row.getByRole('button', { name: '定制运行', exact: true }).click()
      const custom = focusPage.getByRole('dialog', { name: '定制运行', exact: true })
      await focusPage.getByTestId('custom-run-task-select').click()
      await focusPage.getByRole('option').filter({ hasText: focusB.batch_id }).click()
      await custom.locator('.el-switch').first().click()
      await custom.getByRole('spinbutton').first().fill('0.25')
      published.add(focusA.batch_id)
      const beforeFocus = calls.length
      await focusPage.evaluate(() => window.dispatchEvent(new Event('focus')))
      await focusPage.waitForFunction(() => !document.querySelector('.batch-summary'))
      assert.equal(await custom.isVisible(), true, 'publishing selected A must preserve active custom draft B')
      assert.match(await focusPage.getByTestId('custom-run-task-select').innerText(), /focus-target-one/)
      assert.equal(await custom.locator('.el-switch input').first().isChecked(), true)
      assert.equal(Number(await custom.getByRole('spinbutton').first().inputValue()), 0.25)
      assert.equal(new URL(focusPage.url()).searchParams.has('batch_id'), false)
      await eventually(() => calls.slice(beforeFocus).some(call => call.path === '/api/data-batches/' + focusB.batch_id + '/lifecycle'), 'focus validates the custom target independently')
      assert.equal(calls.filter(call => call.path.endsWith('/config') && call.method === 'POST').length, 0)
      cases.push('focus detects published selected A without a broadcast while preserving active B custom draft')
    } finally {
      await focusPage.screenshot({ path: path.join(output, 'focus-custom-draft.png'), fullPage: true })
      await focusPage.close()
    }
    assert.deepEqual(unexpected, [])
    assert.deepEqual(errors, [])
    const result = { passed: true, cases, apiCalls: calls.length, errors, unexpected, output }
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2))
    console.log(JSON.stringify(result))
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), errors, unexpected, calls }, null, 2))
    console.error('Browser artifacts: ' + output)
    throw error
  } finally {
    releaseState?.()
    releaseMonitor?.()
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
