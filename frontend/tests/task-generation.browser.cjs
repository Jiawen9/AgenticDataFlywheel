// Build first, then run with Node and an available Playwright installation (NODE_PATH).
// Serves only dist; every API request is intercepted. Never connects to the real backend/model.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')
const output = path.resolve(__dirname, '../../backend_workspace/task-generation-review')
const dist = path.resolve(__dirname, '../dist')
const clone = value => JSON.parse(JSON.stringify(value))
const app = label => ({ id: `app-${label}`, kind: 'app', label, app: label, reference_example: '', use_resource_prior: false })
let tree = { version: 'version-one', warnings: [], leaf_count: 3, execution_unit_count: 3, scenes: [
  { id: 's1', kind: 'scene', label: '视频媒体', children: [{ id: 'c1', kind: 'capability', label: '内容查找', children: [
    { id: 't1', kind: 'sub_capability', label: '搜索节目', children: [app('视频甲'), app('视频乙')] },
    { id: 't2', kind: 'sub_capability', label: '暂未配置的任务类型', children: [] },
  ] }] },
  { id: 's2', kind: 'scene', label: '出行服务', children: [{ id: 'c2', kind: 'capability', label: '路线规划', children: [
    { id: 't3', kind: 'sub_capability', label: '查找公共交通出行路线', children: [app('地图丙')] },
  ] }] },
] }
const row = (id, extra = {}) => ({ result_id: id, task_uuid: id, task: `任务 ${id}：搜索节目并打开详情页。`, app: '视频甲', scene: '视频媒体', capability: '内容查找', sub_capability: '搜索节目', deleted: false, pre_dependency: 'zero', ...extra })
const resultSets = {
  A: [row('pre', { pre_dependency: 'pre_node', dependency_group_id: 'g', sub_capability: '登录准备' }), row('main', { pre_dependency: 'weak', pre_task_uuid: 'pre', dependency_group_id: 'g' }), row('strong', { pre_dependency: 'strong', status: '-2' }), row('error', { dependency_error: '模型依赖判定超时' }), row('deleted', { deleted: true })],
  B: [row('legacy', { task: '历史作业的独立任务' })],
  new: [row('new-main', { app: '地图丙', scene: '出行服务', capability: '路线规划', sub_capability: '查找公共交通出行路线' })],
}
const job = (id, extra = {}) => ({ job_id: id, kind: 'task_generation', status: 'succeeded', stage: 'succeeded', created_at: `2026-09-0${id === 'A' ? '7' : '6'}T10:30:00`, started_at: null, completed_at: null, current_item: 'unit-old', completed_items: 1, total_items: 1, percent: 100, generate_n: 5, result_count: resultSets[id]?.length || 0, errors: [], warnings: [], error: null, knowledge_base_version: 'historical-version', expected_main_tasks: 5, execution_units: [{ execution_unit_id: 'unit-old', task_type_id: 't1', scene: '提交时的旧场景', capability: '旧能力', sub_capability: '旧任务类型', app: '视频甲' }], ...extra })
const jobs = [job('A', { status: 'partial', stage: 'partial', errors: [{ item_id: 'unit-old', error: '模拟某执行单元失败' }] }), job('B', { execution_units: undefined })]
let conflict = true, failPatch = false, delayA = false, failB = false, failTree = false
const submissions = [], patches = [], exportRequests = [], replacements = [], unexpected = [], pageErrors = []

async function mock(route) {
  const request = route.request(), url = new URL(request.url()), endpoint = url.pathname
  const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  if (endpoint === '/api/task-generation/tree') return failTree ? send({ detail: '模拟资源加载失败' }, 503) : send(tree)
  if (endpoint === '/api/task-generation/knowledge-bases') return send({ knowledge_bases: ['scene_tree', 'control_prior', 'resource_prior'].map(kind => ({ kind, valid: true, exists: true, filename: `${kind}.xlsx`, rows: 3, sheets: ['sheet'], size_bytes: 10 })) })
  if (endpoint.startsWith('/api/task-generation/knowledge-bases/') && request.method() === 'PUT') {
    replacements.push(endpoint)
    tree = { ...tree, version: 'version-replaced' }
    return send({ knowledge_base: { kind: endpoint.split('/').at(-1), valid: true, exists: true, filename: 'replacement.xlsx', rows: 3, size_bytes: 10 } })
  }
  if (endpoint === '/api/task-generation/jobs') {
    if (request.method() === 'POST') {
      submissions.push(request.postDataJSON())
      if (conflict) {
        conflict = false; tree = clone(tree); tree.version = 'version-two'
        tree.scenes[0].children[0].children[0].children = [app('视频甲')]
        return send({ detail: '知识库已更新' }, 409)
      }
      const created = job('new', { status: 'running', stage: 'generating', completed_items: 0, percent: 0, result_count: 0 })
      jobs.unshift(created); return send(created)
    }
    const current = jobs.find(j => j.job_id === 'new')
    if (current) Object.assign(current, { status: 'succeeded', stage: 'succeeded', completed_items: 1, percent: 100, result_count: 1 })
    return send({ jobs: jobs.map(({ execution_units, ...summary }) => summary) })
  }
  const match = endpoint.match(/^\/api\/task-generation\/jobs\/([^/]+)(.*)$/)
  if (match) {
    const [, id, suffix] = match
    if (!suffix) {
      if (id === 'B' && failB) { failB = false; return send({ detail: '模拟详情加载失败' }, 503) }
      if (id === 'A' && delayA) await new Promise(resolve => setTimeout(resolve, 400))
      return send(jobs.find(j => j.job_id === id))
    }
    if (suffix === '/results') return send({ results: resultSets[id], errors: jobs.find(j => j.job_id === id).errors })
    if (suffix.startsWith('/results/') && request.method() === 'PATCH') {
      const resultId = suffix.split('/').at(-1), patch = request.postDataJSON()
      patches.push({ id, resultId, patch })
      if (failPatch) { failPatch = false; return send({ detail: '模拟保存失败' }, 500) }
      const target = resultSets[id].find(r => r.result_id === resultId)
      if ('deleted' in patch) resultSets[id].filter(r => r.result_id === resultId || target.dependency_group_id && r.dependency_group_id === target.dependency_group_id).forEach(r => { r.deleted = patch.deleted })
      Object.assign(target, patch); return send({ result: target })
    }
    if (suffix === '/export') { exportRequests.push({ id, body: request.postData() }); return send({ filename: 'mock.xlsx', created_at: '', download_url: '', row_count: resultSets[id].filter(r => !r.deleted).length }) }
    if (suffix.startsWith('/exports/')) return route.fulfill({ status: 200, contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: Buffer.from('mock download; no real Excel data') })
  }
  unexpected.push(`${request.method()} ${endpoint}`)
  return send({ detail: 'Unexpected API request blocked by browser test' }, 501)
}

async function run() {
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://local').pathname)
      if (pathname.startsWith('/api/')) { res.writeHead(501); res.end('API must be mocked'); return }
      let filename = path.resolve(dist, `.${pathname}`)
      if (!filename.startsWith(`${dist}${path.sep}`) && filename !== dist) { res.writeHead(403); res.end(); return }
      if (!path.extname(filename)) filename = path.join(dist, 'index.html')
      const body = await fs.readFile(filename)
      res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(filename)] || 'application/octet-stream')
      res.end(body)
    } catch { res.writeHead(404); res.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 }, acceptDownloads: true })
  await context.route('**/api/**', mock)
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => pageErrors.push(error.message))
  const base = `http://127.0.0.1:${server.address().port}`
  const shot = name => page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true, animations: 'disabled' })
  const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Page must not overflow horizontally')
  const chooseJob = id => page.locator(`[data-job-id="${id}"]`).click()
  const editMain = async text => { await page.locator('[data-result-id="main"]').getByRole('button', { name: '编辑', exact: true }).click(); await page.getByRole('textbox', { name: '编辑任务文本' }).fill(text) }
  try {
    await page.goto(`${base}/task-generation/generate`)
    await page.locator('.candidate').first().waitFor()
    assert.equal(await page.locator('.candidate').count(), 3)
    const pool = page.getByRole('button', { name: '任务池', exact: true })
    assert.equal(await pool.getAttribute('aria-expanded'), 'true')
    assert.ok((await pool.getAttribute('class')).includes('active'))
    await pool.click(); assert.equal(await page.getByRole('link', { name: '任务生成', exact: true }).isVisible(), false)
    await pool.click()
    await page.locator('.directory-label').filter({ hasText: '视频媒体' }).click()
    assert.equal(await page.locator('.candidate').count(), 2)
    assert.equal(await page.locator('[data-type-id="t2"] input[type=checkbox]').isDisabled(), true)
    await page.getByRole('textbox', { name: '搜索任务类型' }).fill('视频乙')
    await page.getByRole('button', { name: '全选当前结果' }).click()
    await page.locator('[data-type-id="t1"] .el-checkbox-group label').filter({ hasText: '视频甲' }).click()
    assert.equal(await page.locator('.candidate .is-indeterminate').count(), 1)
    await page.getByRole('textbox', { name: '搜索任务类型' }).fill('')
    await page.locator('.directory-label').filter({ hasText: '出行服务' }).click()
    await page.getByRole('button', { name: '全选当前结果' }).click()
    assert.equal(await page.locator('.estimate strong').innerText(), '10')
    await page.getByRole('button', { name: '刷新资源', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.resource-strip .is-loading'))
    assert.equal(await page.locator('.estimate strong').innerText(), '10')
    await shot('configuration-desktop'); await noOverflow()
    await page.getByRole('button', { name: '提交任务生成', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.estimate strong')?.textContent === '5')
    assert.equal(submissions.length, 1)
    assert.equal(await page.getByRole('tab', { name: '新建生成' }).getAttribute('aria-selected'), 'true')
    await page.getByRole('button', { name: '提交任务生成', exact: true }).click()
    await page.locator('[data-result-id="new-main"]').waitFor()
    assert.deepEqual(submissions[1], { version: 'version-two', selections: [{ node_id: 't3', apps: ['地图丙'] }], generate_n: 5 })
    await page.getByRole('tab', { name: '新建生成' }).click()
    assert.equal(await page.locator('.estimate strong').innerText(), '5')
    await page.getByRole('tab', { name: /生成记录/ }).click()
    await chooseJob('A'); await page.locator('[data-result-id="main"]').waitFor()
    assert.equal(await page.locator('.result-group').count(), 3)
    assert.equal(await page.locator('[data-result-id="pre"]').count(), 0)
    await page.locator('.job-issues summary').click()
    assert.ok((await page.locator('.job-issues').innerText()).includes('提交时的旧场景 / 旧能力 / 旧任务类型 · 视频甲'))
    await page.getByRole('button', { name: /展开 1 条前置任务/ }).click()
    assert.equal(await page.locator('[data-result-id="pre"]').count(), 1)
    await editMain('模拟人工修改')
    failPatch = true; await chooseJob('B')
    await page.getByRole('button', { name: '保存后继续', exact: true }).click()
    await page.getByText('模拟保存失败', { exact: true }).waitFor()
    assert.equal(await page.getByRole('textbox', { name: '编辑任务文本' }).inputValue(), '模拟人工修改')
    assert.ok((await page.locator('.job-heading').innerText()).includes('作业 A'))
    await chooseJob('B'); await page.locator('.el-message-box__headerbtn').click()
    assert.ok((await page.locator('.job-heading').innerText()).includes('作业 A'))
    // Filter changes must use the same guard and keep the original filter on cancellation.
    await page.getByText('显示已删除', { exact: true }).click()
    await page.locator('.el-message-box__headerbtn').click()
    assert.equal(await page.locator('[data-result-id="deleted"]').count(), 0)
    await chooseJob('B'); await page.getByRole('button', { name: '保存后继续', exact: true }).click()
    await page.locator('[data-result-id="legacy"]').waitFor()
    assert.equal(resultSets.A.find(r => r.result_id === 'main').task, '模拟人工修改')
    await chooseJob('A'); await page.locator('[data-result-id="main"]').waitFor()
    await editMain('放弃这次修改'); await page.getByRole('tab', { name: '新建生成' }).click()
    await page.getByRole('button', { name: '放弃修改', exact: true }).click()
    await page.getByRole('tab', { name: /生成记录/ }).click()
    assert.equal(await page.getByRole('textbox', { name: '编辑任务文本' }).count(), 0)
    await page.locator('[data-result-id="main"]').getByRole('button', { name: '删除', exact: true }).click()
    await page.getByRole('button', { name: '确定', exact: true }).click()
    await page.locator('[data-result-id="main"]').waitFor({ state: 'hidden' })
    assert.equal(resultSets.A.find(r => r.result_id === 'pre').deleted, true)
    await page.getByText('显示已删除', { exact: true }).click()
    await page.locator('[data-result-id="main"]').getByRole('button', { name: '恢复', exact: true }).click()
    await page.getByRole('button', { name: '确定', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('[data-result-id="main"]')?.classList.contains('deleted') === false)
    await page.getByText('显示已删除', { exact: true }).click()
    await shot('records-desktop'); await noOverflow()
    // Export still includes all nondeleted rows under a restrictive display filter.
    await page.getByText('全部依赖类型', { exact: true }).click()
    await page.getByRole('option', { name: '强依赖', exact: true }).click()
    assert.equal(await page.locator('.result-group').count(), 1)
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出全部未删除结果' }).click(); await download
    assert.deepEqual(exportRequests, [{ id: 'A', body: null }])
    assert.equal(resultSets.A.filter(r => !r.deleted).length, 4)
    // Rapid selection and retry preserve the selected job identity.
    await chooseJob('B'); await page.locator('[data-result-id="legacy"]').waitFor()
    delayA = true; await chooseJob('A'); await chooseJob('B')
    await page.locator('[data-result-id="legacy"]').waitFor(); await page.waitForTimeout(500)
    assert.ok((await page.locator('.job-heading').innerText()).includes('作业 B')); delayA = false
    await chooseJob('A'); await page.locator('[data-result-id="main"]').waitFor()
    failB = true; await chooseJob('B'); await page.getByText('模拟详情加载失败', { exact: true }).waitFor()
    await page.getByRole('button', { name: '重试加载结果' }).click(); await page.locator('[data-result-id="legacy"]').waitFor()
    // Dirty route leave and beforeunload guard.
    await chooseJob('A'); await page.locator('[data-result-id="main"]').waitFor(); await editMain('离开保护')
    assert.equal(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented }), true)
    await page.getByRole('link', { name: '任务泛化扩增', exact: true }).click()
    await page.locator('.el-message-box__headerbtn').click(); assert.ok(page.url().endsWith('/task-generation/generate'))
    await page.getByRole('link', { name: '任务泛化扩增', exact: true }).click(); await page.getByRole('button', { name: '放弃修改', exact: true }).click()
    await page.waitForURL('**/task-generation/augmentation'); assert.ok((await pool.getAttribute('class')).includes('active'))
    // Old route and existing pages remain accessible; resource replacement stays in the drawer.
    await page.goto(`${base}/task-generation`); await page.waitForURL('**/task-generation/generate')
    await page.locator('.candidate').first().waitFor()
    await page.getByRole('button', { name: '管理生成资源', exact: true }).click()
    await page.getByLabel('替换资源先验', { exact: true }).setInputFiles({ name: 'replacement.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('mock upload, never sent to backend') })
    await page.getByRole('button', { name: '替换', exact: true }).click()
    await page.getByText('知识库新版本已发布', { exact: true }).waitFor()
    assert.deepEqual(replacements, ['/api/task-generation/knowledge-bases/resource_prior'])
    await page.locator('.el-drawer__close-btn').click()
    for (const width of [1024, 390]) {
      await page.setViewportSize({ width, height: 900 }); await noOverflow(); await shot(`configuration-${width}`)
      await page.getByRole('tab', { name: /生成记录/ }).click(); await page.locator('.job-heading').waitFor()
      await noOverflow(); await shot(`records-${width}`)
      await page.getByRole('tab', { name: '新建生成' }).click()
    }
    await page.goto(`${base}/task-generation/scenario-tree?l1=s2`)
    await page.locator('.column-browser').waitFor(); assert.ok(page.url().includes('/scenario-studio?'))
    assert.ok((await page.locator('.scene-tab.active').innerText()).includes('出行服务'))
    await page.goto(`${base}/home`); await page.getByRole('heading').first().waitFor()
    // Explicit failed and empty resources do not enable submission.
    failTree = true; await page.goto(`${base}/task-generation/generate`)
    await page.getByText('模拟资源加载失败', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '提交任务生成', exact: true }).isDisabled(), true)
    failTree = false; tree = { ...tree, scenes: [], leaf_count: 0, execution_unit_count: 0 }
    await page.getByRole('button', { name: '重试加载资源', exact: true }).click()
    await page.getByText('场景树暂无任务类型', { exact: true }).waitFor(); await noOverflow()
    assert.deepEqual(unexpected, []); assert.deepEqual(pageErrors, [])
    await fs.writeFile(path.join(output, 'acceptance.json'), JSON.stringify({ passed: true, submissions, patches, exportRequests, replacements, unexpected, pageErrors, screenshots: ['configuration-desktop', 'records-desktop', 'configuration-1024', 'records-1024', 'configuration-390', 'records-390'] }, null, 2))
    console.log('PASS: task pool navigation, selection, conflict, polling, grouped review, protection, export, routes and responsive layouts')
  } catch (error) { await shot('failure'); console.error({ unexpected, pageErrors }); throw error }
  finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
}
run().catch(error => { console.error(error); process.exitCode = 1 })
