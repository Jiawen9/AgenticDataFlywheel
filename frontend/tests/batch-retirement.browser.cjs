// Smoke the built UI with local HTTP fixtures only: no backend/model/cloud work.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = process.env.ADF_BROWSER_ARTIFACTS || path.join(os.tmpdir(), 'adf-batch-retirement-review')
const copy = value => JSON.parse(JSON.stringify(value))
const batch = id => ({ batch_id: id, label: id, kind: 'existing_trajectories', task_count: 2, ready_trajectory_count: 2, ready_step_count: 2, collection_status: 'ready', preprocessing_status: 'succeeded', latest_job: null, annotation_version: 'internal-only', artifacts: [], can_start: false, reason: null })
const summary = (id, status) => ({ task_id: id, goal: '验证 ' + id, trajectory_count: 1, original_step_count: 1, tree_step_count: 1, ignored_step_count: 0, action_node_count: 1, tree_status: status, quality_status: status, status })
const tasks = [summary('TASK-A', 'succeeded'), summary('TASK-B', 'stale')]
const row = (id, number) => ({ step_key: id + ':1', excel_row: number, step: 1, task: '验证 ' + id, meta_task: id + '-1', image: id + '/step1.png', image_url: '', xml: '', actions: '{"action":"wait"}', action: { action: 'wait' }, sop: '等待', summary: '当前基线', thought: '', original_summary: '当前基线', original_thought: '', original_actions_box: '', actions_box: '', deleted: false, edited: false, action_edited: false, bbox_edited: false, sop_edited: false, edit_status: '', cot_status: 'not_needed' })
const group = (id, number, pending) => ({ group_id: 'group-' + id, task_id: id, task: '验证 ' + id, meta_task: id + '-1', quality: '未知', prefix: '', export: !pending, row_count: 1, active_row_count: 1, edited_row_count: 0, action_edit_count: 0, pending_review: pending, pending_review_count: Number(pending), can_adopt_review: pending, rows: [row(id, number)], pending_reviews: pending ? [{ step_key: id + ':1', baseline: { ...row(id, number), summary: '上次基线' }, changes: { summary: '专家保留说明', actions: '{"action":"click","coordinate":[10,20]}' }, reason: '来源已更新，人工修改待复核', can_adopt: true }] : [] })
const recommendation = { status: 'ready', batch_id: 'batch-a', tasks: ['TASK-A', 'TASK-B'].map(id => ({ task_id: id, goal: '验证 ' + id, trajectory_id: id + '-1', global_score: 4.5, passed_threshold: true, trajectory_count: 1, step_count: 1 })) }
function session(groups, revision = 7) { return { session_id: 'internal-session', batch_id: 'batch-a', tree_run_id: 'old-run-not-business', source_id: 'fixture', source: null, storage_revision: revision, pending_review_count: groups.filter(group => group.pending_review).length, selection: recommendation, created_at: '', updated_at: '', row_count: 2, group_count: 2, groups: groups.map(({ rows, pending_reviews, ...rest }) => rest), exports: [] } }

async function main() {
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
      let filename = path.resolve(dist, '.' + pathname)
      if (!filename.startsWith(dist + path.sep) || !path.extname(filename)) filename = path.join(dist, 'index.html')
      const body = await fs.readFile(filename)
      res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(filename)] || 'application/octet-stream')
      res.end(body)
    } catch { res.statusCode = 404; res.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const errors = [], requests = [], reviews = [], cotSubmissions = [], datasetExports = []
  const published = new Set(), releases = []
  let failPublication = true, releaseTree = null, markTreeRequested
  const treeRequested = new Promise(resolve => { markTreeRequested = resolve })
  let groups = [group('TASK-A', 2, true), group('TASK-B', 3, false)], revision = 7
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, serviceWorkers: 'block' })
    context.on('page', next => next.on('pageerror', error => errors.push(error.message)))
    const page = await context.newPage()
    await context.addInitScript(() => { window.open = () => null })
    await context.route('**/*', async route => {
      const request = route.request()
      if (!request.url().startsWith(base + '/')) return route.abort()
      const url = new URL(request.url()), name = url.pathname
      if (!name.startsWith('/api/')) return route.continue()
      requests.push({ name, method: request.method(), query: url.search })
      const json = value => route.fulfill({ json: copy(value) })
      if (name === '/api/training-data-overview') return json({ conversions: [], workbook_url: null })
      if (name.endsWith('/lifecycle')) { const id = name.split('/')[3]; return json({ batch_id: id, status: published.has(id) ? 'published' : 'active', published_at: published.has(id) ? 'now' : null, release_id: published.has(id) ? 'release-new' : null }) }
      if (name === '/api/data-batches/batch-a/tasks/TASK-A/tree') return new Promise(resolve => { markTreeRequested(); releaseTree = async () => { await json({ id: 999, label: 'SHOULD-NOT-REAPPEAR' }); resolve() } })
      if (name === '/api/data-batches/batch-a/tasks/TASK-A/quality') return json({ evaluations: {}, task_id: 'TASK-A' })
      if (name === '/api/phone-factory/state') return json({ phones: [], apps: [], phoneApps: [], vla: [], tasks: [{ description: 'old batch', filename: 'batch-a.xlsx', source_batch_id: 'batch-a', status: '未运行' }, { description: 'manual preserved', filename: 'manual.xlsx', status: '未运行' }] })
      if (name === '/api/phone-factory/config') return json({ sampling_enabled: false, temperature: 0.7, top_p: 0.85, use_experience_lib: false })
      if (name === '/api/task-generation/collection-batches') return json({ batches: ['batch-a', 'batch-b'].filter(id => !published.has(id)).map(id => ({ batch_id: id, kind: 'task_generation', created_at: '2026-09-20', task_count: 1, apps: ['App'], filename: id + '.xlsx' })) })
      if (name.startsWith('/api/task-generation/collection-batches/')) return json({ batch_id: name.split('/')[4], kind: 'task_generation', source_job_id: 'job-a', created_at: '2026-09-20', task_count: 1, apps: ['App'], filename: 'batch-a.xlsx', snapshot: { tasks: [] } })
      if (name === '/api/dataset-upload-jobs/saved-upload') return json({ job: { job_id: 'saved-upload', release_id: 'historical-release', mode: 'internal', status: 'uploading', stage: 'uploading', percent: 20, completed_files: 1, total_files: 5, completed_bytes: 0, total_bytes: 100, file_results: [] } })
      if (name === '/api/trajectory-preprocessing/batches') return json({ batches: [batch('batch-a'), batch('batch-b')].filter(item => !published.has(item.batch_id)) })
      if (name === '/api/tree-runs/current-run') return json({ run_id: 'current-run', batch_id: 'batch-b' })
      if (name === '/api/tree-runs/current-correction-run') return json({ run_id: 'current-correction-run', batch_id: 'batch-a' })
      if (name === '/api/tree-runs/expired-run') return route.fulfill({ status: 410, json: { detail: '该运行已失效，请查看批次当前结果' } })
      if (name === '/api/tree-runs/unknown-run') return route.fulfill({ status: 404, json: { detail: '任务集不存在' } })
      if (name === '/api/tree-builds') return json({ jobs: [] })
      if (name === '/api/tasks') return json({ tasks: tasks.map(task => ({ ...task, step_count: 1, annotated: true, warning: '', first_trajectory: task.task_id + '-1' })) })
      if (name === '/api/quality-jobs') return json({ jobs: [] })
      if (/\/api\/data-batches\/[^/]+\/tree$/.test(name)) {
        const id = name.split('/')[3], current = id === 'batch-a' ? tasks : [summary('TASK-C', 'pending')]
        return json({ batch_id: id, run_id: id, task_count: current.length, tasks: current, task_ids: current.map(task => task.task_id), completed_at: '', model_name: '', total_original_steps: current.length, total_tree_steps: 1, revision: 1 })
      }
      if (/\/api\/data-batches\/[^/]+\/quality$/.test(name)) return json({ batch_id: name.split('/')[3], tasks: name.includes('batch-a') ? [{ task_id: 'TASK-A', status: 'succeeded', rubric_ready: true, average_score: 4.5, passed_count: 1 }, { task_id: 'TASK-B', status: 'stale', rubric_ready: false }] : [{ task_id: 'TASK-C', status: 'pending', rubric_ready: false }] })
      if (name === '/api/correction/recommendation') return json(recommendation)
      if (name === '/api/correction/batches' && published.has('batch-a')) return json({ batches: [], default_batch_id: null })
      if (name === '/api/correction/batches') return json({ default_batch_id: 'batch-a', batches: [{ batch_id: 'batch-a', tree_run_id: 'old-run-not-business', tree_completed_at: '', quality_completed_at: '', total_task_count: 2, reviewed_task_count: 2, is_default: true, status: 'ready' }] })
      if (name === '/api/correction/sessions') return json({ sessions: published.has('batch-a') ? [] : [session(groups, revision)] })
      if (name === '/api/correction/sessions/internal-session') return json({ session: session(groups, revision) })
      if (name.endsWith('/review')) {
        const body = request.postDataJSON(); reviews.push(body)
        assert.equal(body.expected_revision, revision)
        const id = decodeURIComponent(name.split('/').at(-2)), current = groups.find(group => group.group_id === id)
        current.pending_review = false; current.pending_review_count = 0; current.pending_reviews = []
        if (body.decision === 'adopt') { current.rows[0].summary = '专家保留说明'; current.edited_row_count = 1 }
        revision++
        return json({ session: session(groups, revision), group: current })
      }
      if (name.includes('/tasks/group-')) return json({ group: groups.find(group => group.group_id === decodeURIComponent(name.split('/').at(-1))) })
      if (name.endsWith('/dataset-export')) {
        const body = request.postDataJSON(); datasetExports.push(body)
        assert.equal(body.expected_revision, revision)
        revision++
        return json({ filename: 'current.xlsx', storage_revision: revision, summary: { changed_rows: 1 } })
      }
      if (name.endsWith('/cot')) return json({ session_id: 'internal-session', storage_revision: revision, pending_review_count: session(groups).pending_review_count, groups: groups.map(group => ({ group_id: group.group_id, task: group.task, trajectory_id: group.meta_task, pending_review: group.pending_review, pending_review_count: group.pending_review_count, rows: group.rows.map(row => ({ ...row, task_id: group.task_id, trajectory_id: group.meta_task, action: row.actions, original_action: row.actions, history: '', status: 'pending' })) })) })
      if (name === '/api/correction/cot-jobs' && request.method() === 'GET') return json({ jobs: [] })
      if (name === '/api/correction/cot-jobs' && request.method() === 'POST') { const body = request.postDataJSON(); cotSubmissions.push(body); return json({ ...body, job_id: 'mock-only', status: 'succeeded', stage: 'succeeded', total_steps: 1, completed_steps: 1, percent: 100 }) }
      if (name.includes('/assets/')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="600"><rect width="300" height="600" fill="#e2e8f0"/><text x="60" y="120" fill="#334155">Fixture screenshot</text></svg>' })
      if (name === '/api/dataset-releases/candidates') return json({ candidates: ['batch-a', 'batch-b'].filter(id => !published.has(id)).map(id => ({ session_id: id === 'batch-a' ? 'internal-session' : 'other-session', batch_id: id, tree_run_id: id, ready: true, reason: '', updated_at: '', latest_excel: { filename: 'current.xlsx', created_at: '', rows: 2, path: '' }, task_count: 2, trajectory_count: 2, step_count: 2 })) })
      if (name === '/api/dataset-releases' && request.method() === 'POST') {
        if (failPublication) { failPublication = false; return route.fulfill({ status: 500, json: { detail: 'mock publication failed' } }) }
        assert.deepEqual(request.postDataJSON().session_ids, ['internal-session'])
        published.add('batch-a')
        const release = { release_id: 'release-new', batch_ids: ['batch-a'], name: 'Retirement check', created_at: '2026-09-20', excel_paths: [{ path: 'current.xlsx', filename: 'current.xlsx', rows: 2, sha256: 'fixed', available: true }], trajectory_paths: [], source_count: 1, task_count: 2, trajectory_count: 2, step_count: 2, upload_status: 'not_uploaded', local_available: true }
        releases.push(release); return json({ release })
      }
      if (name === '/api/dataset-releases') return json({ releases })
      if (name === '/api/dataset-releases/release-new') return json({ release: releases[0] })
      if (name === '/api/dataset-upload-capabilities') return json({ internal: { configured: false, reason: 'fixture' } })
      return route.fulfill({ status: 404, json: { detail: 'Unmocked API: ' + name } })
    })

    const open = async url => { const tab = await context.newPage(); await tab.goto(base + url); return tab }
    const collection = await open('/collection/tree-building?batch_id=batch-a')
    await collection.getByText('建树已完成 1 / 待处理 0 / 已失效 1', { exact: false }).waitFor()
    const quality = await open('/quality?batch_id=batch-a')
    await quality.locator('.task-row').first().waitFor()
    const unaffected = await open('/quality?batch_id=batch-b')
    await unaffected.getByText('TASK-C', { exact: true }).waitFor()
    const correction = await open('/correction/expert-action?batch_id=batch-a')
    await correction.locator('.task-title').filter({ hasText: 'TASK-A' }).click()
    await correction.locator('.trajectory-title').filter({ hasText: 'TASK-A-1' }).click()
    await correction.locator('.action-select .el-select__wrapper').click()
    await correction.getByRole('option', { name: 'type', exact: true }).click()
    await correction.getByPlaceholder('输入内容').fill('unsaved correction')
    await correction.getByText('有未保存修改', { exact: true }).waitFor()
    const cot = await open('/correction/cot-generation?batch_id=batch-a')
    await cot.locator('.editable-result').first().getByRole('button', { name: '编辑', exact: true }).click()
    await cot.locator('textarea').fill('unsaved thought')
    const phone = await open('/collection/phone-factory?collection_batch_id=batch-a')
    await phone.locator('.batch-summary').waitFor()
    await quality.locator('.task-row').filter({ hasText: 'TASK-A' }).getByRole('button', { name: '查看轨迹树' }).click()
    await treeRequested
    await page.goto(base + '/data-publishing/archive')
    await page.evaluate(() => localStorage.setItem('agentic-data-flywheel.active-internal-dataset-upload', 'saved-upload'))
    await page.reload()
    await page.locator('.candidate').filter({ hasText: 'batch-a' }).locator('.el-checkbox').click()
    await page.getByPlaceholder('输入数据集名称，例如：爱奇艺 GUI 轨迹数据集 v1').fill('Retirement check')
    await page.getByRole('button', { name: '创建并发布', exact: true }).click()
    await page.getByText('mock publication failed', { exact: true }).waitFor()
    assert.equal(await collection.locator('.batch-published-notice').count(), 0)
    assert.equal(await correction.getByText('有未保存修改', { exact: true }).count(), 1)
    assert.equal(await cot.locator('textarea').inputValue(), 'unsaved thought')
    assert.equal(await page.locator('.candidate').filter({ hasText: 'batch-a' }).getByRole('checkbox').isChecked(), true)
    await page.getByRole('button', { name: '创建并发布', exact: true }).click()
    const retiredTabs = [collection, quality, correction, cot, phone]
    for (const tab of retiredTabs) {
      await tab.locator('.batch-published-notice').waitFor()
      await tab.waitForURL(url => !url.searchParams.has('batch_id') && !url.searchParams.has('collection_batch_id'))
      assert.equal(await tab.locator('.task-row,.task-title,.step-item,.batch-summary').count(), 0)
    }
    await releaseTree()
    await quality.waitForLoadState('networkidle')
    assert.equal(await quality.getByText('SHOULD-NOT-REAPPEAR').count(), 0)
    assert.equal(await correction.locator('.correction-editor').count(), 0)
    assert.equal(await cot.locator('textarea').count(), 0)
    assert.equal(requests.some(item => item.method === 'PATCH'), false)
    assert.equal(await unaffected.getByText('TASK-C', { exact: true }).count(), 1)
    assert.equal(new URL(unaffected.url()).searchParams.get('batch_id'), 'batch-b')
    await page.locator('.el-dialog').getByText('release-new', { exact: true }).waitFor()
    assert.equal(await page.locator('.candidate').filter({ hasText: 'batch-a' }).count(), 0)
    assert.equal(await page.locator('.candidate').filter({ hasText: 'batch-b' }).count(), 1)
    assert.equal(await page.evaluate(() => localStorage.getItem('agentic-data-flywheel.active-internal-dataset-upload')), 'saved-upload')
    assert.equal(await phone.getByText('manual preserved', { exact: true }).count(), 1)
    // No broadcast for B: a tab waking later must learn publication from the server.
    published.add('batch-b')
    await unaffected.evaluate(() => window.dispatchEvent(new Event('focus')))
    await unaffected.locator('.batch-published-notice').waitFor()
    assert.equal(await unaffected.locator('.task-row').count(), 0)
    const reopened = await open('/quality?batch_id=batch-a')
    await reopened.locator('.batch-published-notice').waitFor()
    assert.equal(await reopened.locator('.task-row').count(), 0)
    await page.screenshot({ path: path.join(output, 'published-history.png'), fullPage: true })
    await cot.screenshot({ path: path.join(output, 'cot-retired.png'), fullPage: true })
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, retiredPages: 5, otherBatchPreserved: true, failedPublicationPreserved: true, lateReplyRejected: true, focusRecovery: true, uploadMarkerPreserved: true, output }))
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
