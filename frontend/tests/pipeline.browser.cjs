// Every API is fulfilled locally: no backend, phone, model or upload service is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises'), path = require('node:path'), http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = process.env.ADF_BROWSER_ARTIFACTS || path.resolve(__dirname, '../../backend_workspace/tmp/pipeline-validation/browser')
const clone = value => JSON.parse(JSON.stringify(value))
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const ids = ['collection', 'preprocessing', 'tree', 'quality', 'correction', 'cot', 'publication', 'overview']
const labels = ['采集回传', '预处理与标框', '轨迹树构建', '轨迹质检', '人工修正', 'COT', '数据发布', '看板汇总']
const diagram = [
  ['evaluation', '迭代评估', [['seed-evaluation', '种子任务集评测'], ['badcase-extraction', 'BadCase 提取']]],
  ['generation', '任务生成', [['badcase-augmentation', 'BadCase 扩增']]],
  ['collection', '轨迹采集', [['phone-factory', '手机工厂并行采集'], ['bounding-box', '标框'], ['page-summary', '页面总结'], ['tree-building', '轨迹树构建']]],
  ['quality', '轨迹质检', [['rubrics-generation', 'Rubrics 生成'], ['rubrics-ranking', 'Rubrics 相对排序']]],
  ['publishing', '数据发布', [['training-data-archive', '训练数据归档']]],
  ['training', '模型训练', [['data-mixture', '训练数据配比'], ['dataset-split', '训练集/验证集划分'], ['model-training', '模型训练'], ['training-validation', '训练有效性验证']]],
  ['model-publishing', '模型发布', [['trained-model-archive', '增训模型归档']]],
]
const nodeTargets = { 'phone-factory': 'collection', 'bounding-box': 'preprocessing', 'page-summary': 'tree', 'tree-building': 'tree', 'rubrics-generation': 'quality', 'rubrics-ranking': 'quality', 'training-data-archive': 'publication' }
const disabledStages = ['evaluation', 'generation', 'training', 'model-publishing']
const now = '2026-09-22T10:00:00+08:00'
const batch = id => ({ batch_id: id, label: id, kind: 'existing_trajectories', task_count: 1, ready_trajectory_count: 1, ready_step_count: 1, collection_status: 'ready', preprocessing_status: 'succeeded', latest_job: { job_id: 'unrelated-latest', batch_id: id, status: 'running' }, annotation_version: 'current', artifacts: [], can_start: true, reason: null })
const factoryBatch = id => ({ schema_version: 1, batch_id: id, source_job_id: null, kind: 'manual_collection', job_status: 'succeeded', created_at: now, task_count: 1, apps: ['App'], filename: id + '.xlsx', snapshot: { tasks: [{ task_id: 'A', collection_case_id: 'A', task: 'mock task', app: 'App' }] } })
const task = { task_id: 'A', goal: '模拟任务', warning: '', first_trajectory: 'A-1', trajectory_count: 1, step_count: 1, annotated: true, tree_status: 'succeeded', quality_status: 'succeeded', status: 'succeeded' }
const correctionRow = { step_key: 'A:1', excel_row: 2, step: 1, task: '模拟任务', meta_task: 'A-1', image: '', image_url: '', xml: '', actions: '{"action":"wait"}', action: { action: 'wait' }, sop: '等待', summary: '原始内容', thought: '', original_summary: '原始内容', original_thought: '', original_actions_box: '', actions_box: '', deleted: false, edited: false, action_edited: false, bbox_edited: false, sop_edited: false, edit_status: '', cot_status: 'not_needed' }
const group = { group_id: 'group-A', task_id: 'A', task: '模拟任务', meta_task: 'A-1', quality: '成功', prefix: '', export: true, row_count: 1, active_row_count: 1, edited_row_count: 0, action_edit_count: 0, pending_review: false, pending_review_count: 0, rows: [correctionRow], pending_reviews: [] }
const alternative = { ...clone(group), group_id: 'group-A-second', meta_task: 'A-2', export: false, rows: [{ ...correctionRow, step_key: 'A:second:1', excel_row: 3, meta_task: 'A-2' }] }
let sessionRevision = 12
const recommendation = { status: 'ready', batch_id: 'batch-manual', tasks: [{ task_id: 'A', goal: '模拟任务', trajectory_id: 'A-1', global_score: 4.5, passed_threshold: true, trajectory_count: 1, step_count: 1 }] }
recommendation.candidates = [{ ...recommendation.tasks[0], group_id: 'group-A' }, { ...recommendation.tasks[0], group_id: 'group-A-second', trajectory_id: 'A-2', global_score: 4 }]
function session() { return { session_id: 'bound-session', batch_id: 'batch-manual', tree_run_id: 'batch-manual', source_id: 'fixture', source: null, storage_revision: sessionRevision, pending_review_count: 0, selection: recommendation, created_at: now, updated_at: now, row_count: 2, group_count: 2, groups: [group, alternative], exports: [] } }
function run(body, index) { return { ...body, pipeline_id: 'pipeline-' + index, status: 'running', current_step: 'collection', storage_revision: 1, task_ids: ['A'], created_at: now, updated_at: now, collection_run_ids: [], steps: ids.map((id, i) => ({ id, label: labels[i], status: 'pending', job_ids: [], jobs: [], percent: 0 })) } }
function job(id, stage = 'classifying') { return { job_id: id, batch_id: 'batch-manual', status: 'running', stage, task_ids: ['A'], created_at: now, percent: 42, total_steps: 10, completed_steps: 4, classified_steps: 4, current_task: 'A', current_trajectory: 'A-1', current_step: 1, artifacts: [], total_tasks: 1, completed_tasks: 0, evaluated_trajectories: 0, total_trajectories: 1, model_name: 'mock', logs: [], errors: [], error: null } }
async function main() {
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try { const pathname = decodeURIComponent(new URL(req.url, 'http://local').pathname); let file = path.resolve(dist, '.' + pathname); if (!file.startsWith(dist + path.sep) || !path.extname(file)) file = path.join(dist, 'index.html'); res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream'); res.end(await fs.readFile(file)) } catch { res.statusCode = 404; res.end('missing') }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1560, height: 1080 }, serviceWorkers: 'block' })
  const calls = [], errors = [], unexpected = [], cases = [], pipelines = [], releases = []
  const factory = { phones: ['phone-1'], apps: ['App'], phoneApps: [{ phone_id: 'phone-1', app: 'App', status: '空闲' }], vla: ['http://mock-vla'], tasks: [] }
  const config = { sampling_enabled: true, temperature: 0.7, top_p: 0.85, use_experience_lib: false }
  const mutations = () => calls.filter(c => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(c.method) && !['/api/phone-factory/remote/status', '/api/phone-factory/remote/adb-devices'].includes(c.name))
  let failCreateOnce = false
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), name = url.pathname, method = request.method(), body = request.postDataJSON()
      if (url.origin !== base) { unexpected.push('external ' + request.url()); return route.abort() }
      if (!name.startsWith('/api/')) return route.continue()
      calls.push({ name, method, body, query: Object.fromEntries(url.searchParams) })
      const send = value => route.fulfill({ json: clone(value) })
      if (name === '/api/pipelines' && method === 'POST') {
        let value = pipelines.find(p => p.request_id === body.request_id)
        if (!value) { value = run(body, pipelines.length + 1); pipelines.push(value) }
        if (failCreateOnce) { failCreateOnce = false; return route.fulfill({ status: 502, json: { detail: '模拟创建响应丢失，请重试' } }) }
        return send({ pipeline: value })
      }
      if (name === '/api/pipelines') return send({ pipelines: pipelines.filter(p => (!url.searchParams.get('batch_id') || p.batch_id === url.searchParams.get('batch_id')) && (!url.searchParams.get('active_only') || !['succeeded', 'terminated', 'published_summary_failed'].includes(p.status))) })
      if (name.startsWith('/api/pipelines/')) {
        const id = name.split('/')[3], value = pipelines.find(p => p.pipeline_id === id), action = name.split('/')[4]
        if (action) { assert.equal(body.expected_revision, value.storage_revision); value.storage_revision++; if (action === 'confirm-correction') { assert.equal(body.session_revision, sessionRevision); value.status = 'running'; value.current_step = 'cot'; value.steps.find(s => s.id === 'correction').status = 'succeeded' } else value.status = ({ pause: 'paused', resume: 'running', retry: 'running', terminate: 'terminated' })[action] }
        return send({ pipeline: value })
      }
      if (name.endsWith('/lifecycle')) return send({ batch_id: name.split('/')[3], status: releases.some(r => r.batch_ids.includes(name.split('/')[3])) ? 'published' : 'active', release_id: releases[0]?.release_id || null, published_at: null })
      if (name === '/api/trajectory-preprocessing/batches') return send({ batches: ['batch-manual', 'batch-auto'].filter(id => !releases.some(r => r.batch_ids.includes(id))).map(batch) })
      if (name === '/api/phone-factory/batches') return send({ batches: ['batch-manual', 'batch-auto'].map(factoryBatch) })
      if (/^\/api\/phone-factory\/batches\/[^/]+$/.test(name)) return send(factoryBatch(name.split('/').at(-1)))
      if (name === '/api/phone-factory/state') return send(factory)
      if (name === '/api/phone-factory/config') return send(config)
      if (name === '/api/phone-factory/remote/status') return send({ ok: true, statuses: [{ phone_id: 'phone-1', status: '空闲' }] })
      if (name === '/api/phone-factory/remote/adb-devices') return send({ ok: true, devices: [] })
      if (name === '/api/phone-factory/collection-runs') return send({ runs: [] })
      if (name === '/api/tasks') return send({ tasks: [task] })
      if (name === '/api/tree-builds' || name === '/api/quality-jobs' || name === '/api/correction/cot-jobs') return send({ jobs: [job('unrelated-latest')] })
      if (name.startsWith('/api/trajectory-preprocessing/jobs/')) return send(job(name.split('/').at(-1), 'annotating'))
      if (name.startsWith('/api/tree-builds/')) return send(job(name.split('/').at(-1)))
      if (name.startsWith('/api/quality-jobs/')) return send(job(name.split('/').at(-1), 'evaluating'))
      if (name.startsWith('/api/correction/cot-jobs/')) return send({ ...job(name.split('/').at(-1)), session_id: 'bound-session' })
      if (/\/api\/data-batches\/[^/]+\/tree$/.test(name)) return send({ batch_id: name.split('/')[3], run_id: name.split('/')[3], task_count: 1, tasks: [task], task_ids: ['A'], completed_at: now, model_name: 'mock', total_original_steps: 1, total_tree_steps: 1, revision: 1 })
      if (/\/api\/data-batches\/[^/]+\/quality$/.test(name)) return send({ batch_id: name.split('/')[3], tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true, average_score: 4.5, passed_count: 1 }] })
      if (name === '/api/correction/recommendation') return send(recommendation)
      if (name === '/api/correction/batches') return send({ batches: [{ batch_id: 'batch-manual', tree_run_id: 'batch-manual', tree_completed_at: now, quality_completed_at: now, total_task_count: 1, reviewed_task_count: 1, status: 'ready', is_default: true }], default_batch_id: 'batch-manual' })
      if (name === '/api/correction/sessions') return send({ sessions: pipelines[0]?.session_id ? [session()] : [] })
      if (name === '/api/correction/sessions/bound-session') return send({ session: session() })
      if (name.endsWith('/export') && method === 'PATCH') { assert.equal(body.expected_revision, sessionRevision); const selected = name.includes('group-A-second') ? alternative : group; selected.export = body.export; sessionRevision++; return send({ group: selected, storage_revision: sessionRevision }) }
      if (name.endsWith('/tasks/group-A-second')) return send({ group: alternative })
      if (name.endsWith('/tasks/group-A')) return send({ group })
      if (name.endsWith('/cot')) return send({ session_id: 'bound-session', storage_revision: 12, pending_review_count: 0, groups: [{ group_id: group.group_id, task: group.task, trajectory_id: group.meta_task, pending_review: false, pending_review_count: 0, rows: [{ ...correctionRow, task_id: 'A', trajectory_id: 'A-1', action: correctionRow.actions, original_action: correctionRow.actions, history: '', status: 'not_needed' }] }] })
      if (name.includes('/assets/')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="#e2e8f0"/></svg>' })
      if (name === '/api/training-data-overview') return send({ conversions: [{ release_id: 'release-pipeline', name: 'Pipeline 发布', status: 'succeeded', warnings: [], error: null, updated_at: now }], workbook_url: '/api/training-data-overview/workbook' })
      if (name === '/api/dataset-releases/candidates') return send({ candidates: [] })
      if (name === '/api/dataset-releases') return send({ releases })
      if (name === '/api/dataset-upload-capabilities') return send({ internal: { configured: false, reason: '模拟服务，无上传' } })
      if (name === '/api/dataset-releases/release-pipeline') return send({ release: releases[0] })
      if (name === '/api/training-data-overview/releases/release-pipeline') return send({ release_id: 'release-pipeline', status: 'succeeded', rows: 1, warnings: [], error: null })
      if (name.includes('/overview')) return send({ release_id: 'release-pipeline', status: 'succeeded', warnings: [], error: null, rows: 1 })
      unexpected.push(method + ' ' + name); return route.fulfill({ status: 501, json: { detail: 'Unmocked API blocked: ' + name } })
    })
    await page.goto(base + '/pipeline')
    await page.evaluate(() => { localStorage.setItem('automatic-pipeline-circuit-v3', '{"status":"completed"}'); localStorage.setItem('unrelated-value', 'keep') })
    await page.reload(); await page.getByRole('heading', { name: '自动 Pipeline', exact: true }).waitFor()
    assert.equal(await page.evaluate(() => localStorage.getItem('automatic-pipeline-circuit-v3')), null); assert.equal(await page.evaluate(() => localStorage.getItem('unrelated-value')), 'keep')
    assert.equal(await page.locator('.stage-column').count(), 7); assert.equal(await page.locator('.step-node').count(), 15)
    for (const [stageId, label, children] of diagram) {
      const stage = page.getByTestId('pipeline-stage-' + stageId)
      assert.equal(await stage.locator('strong').innerText(), label); assert.equal(await stage.isDisabled(), true)
      for (const [childId, childLabel] of children) assert.equal(await page.getByTestId('pipeline-node-' + childId).innerText(), childLabel)
    }
    const geometry = await page.evaluate(() => {
      const style = selector => getComputedStyle(document.querySelector(selector))
      const board = style('.circuit-board'), stage = style('.stage-node'), node = style('.step-node'), title = style('.stage-node strong'), shell = style('.circuit-shell')
      return { columns: board.gridTemplateColumns, gap: board.columnGap, padding: board.padding, minHeight: board.minHeight, main: [stage.width, stage.height], child: [node.width, node.height, node.borderRadius, node.fontSize], title: [title.top, title.fontSize], shell: [shell.backgroundColor, shell.borderColor, shell.borderRadius], canvas: style('.circuit-scroll').backgroundImage, connector: style('.bus-segment').width }
    })
    assert.deepEqual(geometry, { columns: Array(7).fill('176px').join(' '), gap: '36px', padding: '82px 62px 42px', minHeight: '505px', main: ['44px', '44px'], child: ['176px', '40px', '20px', '11px'], title: ['-34px', '15px'], shell: ['rgb(251, 252, 255)', 'rgb(215, 222, 243)', '8px'], canvas: 'none', connector: '168px' })
    await fs.writeFile(path.join(output, 'original-diagram-geometry.json'), JSON.stringify({ baseline: '6f85f34', viewport: { width: 1560, height: 1080 }, geometry }, null, 2))
    await page.screenshot({ path: path.join(output, 'pipeline-restored-waiting.png'), fullPage: true })
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.screenshot({ path: path.join(output, 'pipeline-restored-wide.png'), fullPage: true })
    await page.setViewportSize({ width: 720, height: 1080 })
    const scroll = page.locator('.circuit-scroll')
    assert.equal(await scroll.evaluate(element => element.scrollWidth > element.clientWidth && getComputedStyle(element).overflowX === 'auto'), true)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'diagram scrolling must not overflow the whole page')
    await scroll.evaluate(element => { element.scrollLeft = element.scrollWidth })
    assert.equal(await scroll.evaluate(element => element.scrollLeft > 0), true)
    await page.screenshot({ path: path.join(output, 'pipeline-restored-narrow.png'), fullPage: true })
    await page.setViewportSize({ width: 1560, height: 1080 }); await scroll.evaluate(element => { element.scrollLeft = 0 })
    cases.push('原版七模块十五子节点原文、44/176px尺寸、间距连线与配色恢复；窄屏流程图横向滚动')
    await page.getByRole('button', { name: '新建 Pipeline', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '新建 Pipeline', exact: true })
    await dialog.getByPlaceholder('例如：手机采集批次验收').fill('人工验收')
    await dialog.locator('.el-form-item').filter({ hasText: '业务批次' }).locator('.el-select__wrapper').click(); await page.getByRole('option', { name: /batch-manual/ }).click()
    failCreateOnce = true
    await dialog.getByRole('button', { name: '创建并开始', exact: true }).click(); await dialog.getByText('模拟创建响应丢失，请重试', { exact: true }).waitFor()
    await dialog.getByRole('button', { name: '创建并开始', exact: true }).click(); await page.waitForURL('**pipeline_id=pipeline-1')
    const creates = mutations().filter(c => c.name === '/api/pipelines'); assert.equal(creates.length, 2); assert.equal(creates[0].body.request_id, creates[1].body.request_id); assert.equal(pipelines.length, 1)
    cases.push('创建响应丢失复用 request_id，不重复创建；仅清除旧演示缓存')
    for (const stageId of disabledStages) {
      const stage = page.getByTestId('pipeline-stage-' + stageId), before = mutations().length, url = page.url()
      assert.equal(await stage.isDisabled(), true); assert.match(await stage.getAttribute('title'), /尚未接入自动执行/)
      await stage.evaluate(element => element.click()); assert.equal(page.url(), url); assert.equal(mutations().length, before)
    }
    for (const [testId, id] of [...Object.entries(nodeTargets).map(([node, id]) => ['pipeline-node-' + node, id]), ...['correction', 'cot', 'overview'].map(id => ['pipeline-step-' + id, id]), ...[['collection', 'collection'], ['quality', 'quality'], ['publishing', 'publication']].map(([stage, id]) => ['pipeline-stage-' + stage, id])]) {
      await page.goto(base + '/pipeline?pipeline_id=pipeline-1'); await page.getByTestId(testId).waitFor()
      const before = mutations().length
      await page.getByTestId(testId).click(); await page.getByTestId('pipeline-status-bar').waitFor()
      const url = new URL(page.url()); assert.equal(url.searchParams.get('pipeline_id'), 'pipeline-1'); assert.equal(url.searchParams.get('step_id'), id)
      await pause(100); assert.equal(mutations().length, before, 'navigation must not submit: ' + id)
    }
    cases.push('已接入主节点及子节点、状态区修正/COT/汇总真实点击导航，不创建作业或会话；未接入模块禁用')
    const manual = pipelines[0]
    const tree = manual.steps.find(s => s.id === 'tree'), quality = manual.steps.find(s => s.id === 'quality')
    manual.current_step = 'tree'; manual.task_ids = ['A', 'B']
    for (const id of ['collection', 'preprocessing']) Object.assign(manual.steps.find(step => step.id === id), { status: 'succeeded', percent: 100 })
    Object.assign(tree, { status: 'running', job_ids: ['tree-A', 'tree-B'], jobs: [{ ...job('tree-A', 'building'), status: 'succeeded' }, { ...job('tree-B', 'classifying_and_observing'), current_task: 'B' }], percent: 42 })
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1')
    const nodeStatus = async (id, attr, expected) => page.waitForFunction(({ id, attr, expected }) => document.querySelector('[data-testid="pipeline-node-' + id + '"]')?.getAttribute(attr) === expected, { id, attr, expected })
    await nodeStatus('page-summary', 'data-active', 'true')
    assert.notEqual(await page.getByTestId('pipeline-node-tree-building').getAttribute('data-status'), 'completed')
    const treeNavigation = mutations().length
    await page.getByTestId('pipeline-stage-collection').click(); await page.getByTestId('pipeline-status-bar').waitFor()
    assert.equal(new URL(page.url()).searchParams.get('step_id'), 'tree'); assert.equal(mutations().length, treeNavigation)
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1')
    await nodeStatus('page-summary', 'data-active', 'true')
    await page.getByRole('button', { name: '暂停', exact: true }).click()
    await page.getByRole('button', { name: '继续', exact: true }).waitFor()
    await nodeStatus('page-summary', 'data-active', 'true')
    assert.notEqual(await page.getByTestId('pipeline-node-tree-building').getAttribute('data-status'), 'completed')
    await page.getByRole('button', { name: '继续', exact: true }).click(); await page.getByRole('button', { name: '暂停', exact: true }).waitFor()
    tree.jobs[1].stage = 'building'
    await nodeStatus('tree-building', 'data-active', 'true')
    assert.notEqual(await page.getByTestId('pipeline-node-page-summary').getAttribute('data-status'), 'completed')
    tree.jobs[1].stage = 'classifying_and_observing'
    await nodeStatus('page-summary', 'data-active', 'true')
    assert.notEqual(await page.getByTestId('pipeline-node-tree-building').getAttribute('data-status'), 'completed')
    tree.status = 'succeeded'; tree.percent = 100
    await nodeStatus('page-summary', 'data-status', 'completed'); await nodeStatus('tree-building', 'data-status', 'completed')
    manual.current_step = 'quality'; Object.assign(quality, { status: 'running', job_ids: ['quality-A', 'quality-B'], jobs: [{ ...job('quality-A', 'evaluating'), status: 'succeeded' }, { ...job('quality-B', 'generating_rubric'), current_task: 'B' }], percent: 42 })
    await nodeStatus('rubrics-generation', 'data-active', 'true')
    quality.jobs[1].stage = 'restoring_cached_results'
    await nodeStatus('rubrics-generation', 'data-active', 'false'); await nodeStatus('rubrics-ranking', 'data-active', 'false')
    assert.equal(await page.getByTestId('pipeline-stage-quality').getAttribute('data-status'), 'running')
    assert.match(await page.getByTestId('pipeline-node-rubrics-ranking').getAttribute('title'), /后台未提供可细分工序/)
    await page.getByTestId('pipeline-run-status').getByText('quality-B', { exact: true }).waitFor()
    await page.setViewportSize({ width: 1920, height: 1080 }); await pause(350)
    await page.screenshot({ path: path.join(output, 'pipeline-restored-unknown-stage.png'), fullPage: true })
    await page.setViewportSize({ width: 720, height: 1080 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'run status must wrap without page overflow')
    await page.screenshot({ path: path.join(output, 'pipeline-restored-active-narrow.png'), fullPage: true })
    await page.setViewportSize({ width: 1560, height: 1080 })
    quality.jobs[1].stage = 'evaluating'; await nodeStatus('rubrics-ranking', 'data-active', 'true')
    assert.notEqual(await page.getByTestId('pipeline-node-rubrics-generation').getAttribute('data-status'), 'completed')
    await pause(350)
    assert.equal(await page.getByTestId('pipeline-node-rubrics-ranking').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(236, 254, 255)')
    await page.screenshot({ path: path.join(output, 'pipeline-restored-real-progress.png'), fullPage: true })
    const mainBefore = mutations().length
    await page.getByTestId('pipeline-stage-collection').click(); await page.getByTestId('pipeline-status-bar').waitFor()
    assert.equal(new URL(page.url()).searchParams.get('step_id'), 'collection')
    assert.equal(mutations().length, mainBefore)
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1')
    quality.status = 'succeeded'; await nodeStatus('rubrics-generation', 'data-status', 'completed'); await nodeStatus('rubrics-ranking', 'data-status', 'completed')
    await pause(350); assert.equal(await page.getByTestId('pipeline-node-rubrics-ranking').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(238, 242, 255)')
    Object.assign(quality, { status: 'pending', job_ids: [], jobs: [], percent: 0 })
    cases.push('多任务多子作业在页面总结/建树和Rubrics阶段交替，整批成功前子节点不提前完成；原运行/完成配色保留')
    await page.goto(base + '/quality?pipeline_id=pipeline-1&step_id=quality&batch_id=batch-manual')
    await page.getByTestId('pipeline-status-bar').getByText('轨迹质检 · 等待前置步骤', { exact: true }).waitFor()
    const waitingCalls = mutations().length
    manual.current_step = 'quality'; manual.steps.find(s => s.id === 'tree').status = 'succeeded'
    Object.assign(manual.steps.find(s => s.id === 'quality'), { status: 'running', job_ids: ['quality-bound'], jobs: [job('quality-bound', 'evaluating')], percent: 42 })
    await page.getByTestId('pipeline-status-bar').getByText('quality-bound · running', { exact: true }).waitFor(); await page.getByText('42%', { exact: true }).first().waitFor()
    assert.equal(await page.getByRole('button', { name: /提交轨迹质检/ }).isDisabled(), true)
    await page.screenshot({ path: path.join(output, 'quality-bound-progress.png'), fullPage: true })
    await page.reload(); await page.getByTestId('pipeline-status-bar').getByText('quality-bound · running', { exact: true }).waitFor()
    assert.equal(mutations().length, waitingCalls)
    cases.push('等待页自动发现质检 exact job，真实进度与刷新恢复，未采用 unrelated-latest')
    await page.goto(base + '/quality?batch_id=batch-manual'); await page.getByTestId('pipeline-status-bar').waitFor()
    assert.equal(await page.getByRole('button', { name: /提交轨迹质检/ }).isDisabled(), true)
    cases.push('去掉 Pipeline 路由仍按业务批次识别管理关系并禁写')
    manual.session_id = 'bound-session'; manual.status = 'waiting_for_correction'; manual.current_step = 'correction'; manual.steps.find(s => s.id === 'correction').status = 'waiting'
    await page.goto(base + '/correction/expert-action?pipeline_id=pipeline-1&step_id=correction&batch_id=batch-manual')
    await page.getByRole('button', { name: '完成修正并继续', exact: true }).waitFor()
    await page.locator('.task-title').first().click()
    await page.locator('.trajectory-title').filter({ hasText: 'A-2' }).getByRole('button', { name: '选择此轨迹', exact: true }).click()
    await page.locator('.trajectory-title').filter({ hasText: 'A-2' }).getByRole('button', { name: '已选择', exact: true }).waitFor()
    assert.equal(group.export, false); assert.equal(alternative.export, true); assert.equal(sessionRevision, 14)
    await page.locator('.trajectory-title').filter({ hasText: 'A-2' }).click()
    assert.equal(await page.locator('.action-select input').isDisabled(), false)
    await page.screenshot({ path: path.join(output, 'manual-correction-gate.png'), fullPage: true })
    await page.getByRole('button', { name: '完成修正并继续', exact: true }).click()
    await page.getByTestId('pipeline-status-bar').getByText('正在执行 · batch-manual', { exact: true }).waitFor()
    assert.equal(await page.locator('.action-select input').isDisabled(), true)
    cases.push('仅人工修正关卡可编辑，可用另一候选替换Top1；确认携带最新双修订号并锁定')
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1'); await page.getByRole('button', { name: '新建 Pipeline', exact: true }).click()
    await dialog.getByPlaceholder('例如：手机采集批次验收').fill('自动验收'); await dialog.getByText('下发采集', { exact: true }).click()
    await dialog.locator('.el-form-item').filter({ hasText: '业务批次' }).locator('.el-select__wrapper').click(); await page.getByRole('option', { name: /batch-auto/ }).click()
    await dialog.getByText('自动发布', { exact: true }).click(); await dialog.locator('.el-input-number input').first().fill('4.2')
    await dialog.locator('.el-form-item').filter({ hasText: '手机' }).locator('.el-select__wrapper').click(); await page.getByRole('option', { name: 'phone-1', exact: true }).click()
    await dialog.locator('.el-form-item').filter({ hasText: 'App' }).locator('.el-select__wrapper').click(); await page.getByRole('option', { name: 'App', exact: true }).click()
    await dialog.getByRole('button', { name: '创建并开始', exact: true }).click(); await page.waitForURL('**pipeline_id=pipeline-2')
    assert.equal(pipelines[1].threshold, 4.2); assert.deepEqual(pipelines[1].collection_config, { phone_id: 'phone-1', app: 'App', vla: 'http://mock-vla', config }); assert.equal(mutations().some(c => c.name === '/api/phone-factory/config'), false)
    cases.push('自动模式阈值及采集 VLA/采样实际参数传递，不修改全局配置')
    Object.assign(manual, { status: 'published_summary_failed', release_id: 'release-pipeline', current_step: 'overview' }); manual.steps.forEach(s => s.status = 'succeeded'); manual.steps.find(s => s.id === 'overview').status = 'failed'
    releases.push({ release_id: 'release-pipeline', name: 'Pipeline 发布', batch_ids: ['batch-manual'], source_kind: 'workflow', pipeline_id: 'pipeline-1', created_at: now, source_count: 1, task_count: 1, trajectory_count: 1, step_count: 1, local_available: true, upload_status: 'not_uploaded', excel_paths: [{ filename: 'selected.xlsx', path: 'selected.xlsx', rows: 1, sha256: 'frozen', available: true }], trajectory_paths: [], sources: [], warnings: [] })
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1'); await page.getByText('已发布，汇总失败', { exact: true }).first().waitFor()
    await nodeStatus('training-data-archive', 'data-status', 'completed')
    assert.equal(await page.getByTestId('pipeline-stage-publishing').getAttribute('data-status'), 'completed')
    for (const stageId of disabledStages) {
      assert.equal(await page.getByTestId('pipeline-stage-' + stageId).isDisabled(), true)
      assert.notEqual(await page.getByTestId('pipeline-stage-' + stageId).getAttribute('data-status'), 'completed')
    }
    await page.getByTestId('pipeline-step-overview').getByText('看板汇总 · 失败', { exact: true }).waitFor()
    await page.screenshot({ path: path.join(output, 'pipeline-restored-summary-failed.png'), fullPage: true })
    await page.getByRole('button', { name: '重试汇总', exact: true }).click(); await page.getByRole('button', { name: '暂停', exact: true }).waitFor()
    manual.status = 'succeeded'; manual.steps.find(s => s.id === 'overview').status = 'succeeded'
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1'); await page.getByRole('button', { name: '查看发布详情', exact: true }).click(); await page.getByRole('dialog').waitFor()
    await page.getByText('Pipeline 发布', { exact: true }).first().waitFor();
    const releaseDialog = page.getByRole('dialog', { name: '数据集详情' })
    await releaseDialog.getByText('Pipeline', { exact: true }).waitFor(); await releaseDialog.getByText('批次来源数', { exact: true }).waitFor(); await releaseDialog.getByText('发布 Excel', { exact: true }).waitFor()
    assert.equal(await releaseDialog.getByText('会话来源数', { exact: true }).count(), 0); assert.equal(await releaseDialog.getByText('专家纠偏', { exact: true }).count(), 0)
    await page.screenshot({ path: path.join(output, 'published-details.png'), fullPage: true })
    await page.goto(base + '/pipeline?pipeline_id=pipeline-1'); await page.getByText('已发布并完成汇总', { exact: true }).first().waitFor()
    assert.equal(await page.getByTestId('pipeline-step-overview').count(), 1); cases.push('发布后历史保留；汇总失败不取消归档完成，重试汇总和发布详情可用；未接入模块不误亮')
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.screenshot({ path: path.join(output, 'pipeline-history.png'), fullPage: true })
    await page.setViewportSize({ width: 1560, height: 1080 })
    Object.assign(pipelines[1], { status: 'terminated', history_only: true })
    const historicalPages = [['preprocessing', '/collection/tree-building'], ['quality', '/quality'], ['correction', '/correction/expert-action'], ['cot', '/correction/cot-generation']]
    for (const [step, pathname] of historicalPages) {
      const offset = calls.length
      await page.goto(base + pathname + '?pipeline_id=pipeline-2&step_id=' + step + '&batch_id=batch-auto')
      await page.getByText('流程已结束，仅展示本次运行记录；当前批次结果可能已更新，请从业务页面重新选择批次查看。', { exact: true }).waitFor()
      await pause(150)
      assert.equal(calls.slice(offset).some(c => c.name === '/api/tasks' || /^\/api\/data-batches\/batch-auto\/(tree|quality)$/.test(c.name) || c.name.startsWith('/api/correction/sessions/')), false, 'terminated history must not load current results: ' + step)
    }
    cases.push('已终止未发布历史只显示绑定运行记录，不读取可能更新的当前过程件')
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ passed: true, cases, requests: calls.length, errors, unexpected }, null, 2))
    console.log(JSON.stringify({ passed: true, cases, output }))
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), errors, unexpected, calls: calls.slice(-30), url: page.url() }, null, 2)); throw error
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
