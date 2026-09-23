// Smoke the built UI with local HTTP fixtures only: no backend/model/cloud work.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = process.env.ADF_BROWSER_ARTIFACTS || path.join(os.tmpdir(), 'adf-single-batch-review')
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
  let groups = [group('TASK-A', 2, true), group('TASK-B', 3, false)], revision = 7
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 }, serviceWorkers: 'block' })
    await page.addInitScript(() => { window.open = () => null })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request()
      if (!request.url().startsWith(base + '/')) return route.abort()
      const url = new URL(request.url()), name = url.pathname
      if (!name.startsWith('/api/')) return route.continue()
      requests.push({ name, method: request.method(), query: url.search })
      const json = value => route.fulfill({ json: copy(value) })
      if (name === '/api/pipelines') return json({ pipelines: [] })
      if (name.endsWith('/lifecycle')) return json({ batch_id: name.split('/')[3], status: 'active', published_at: null, release_id: null })
      if (name === '/api/trajectory-preprocessing/batches') return json({ batches: [batch('batch-a'), batch('batch-b')] })
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
      if (name === '/api/correction/batches') return json({ default_batch_id: 'batch-a', batches: [{ batch_id: 'batch-a', tree_run_id: 'old-run-not-business', tree_completed_at: '', quality_completed_at: '', total_task_count: 2, reviewed_task_count: 2, is_default: true, status: 'ready' }] })
      if (name === '/api/correction/sessions') return json({ sessions: [session(groups, revision)] })
      if (name === '/api/correction/sessions/deleted-session') return route.fulfill({ status: 404, json: { detail: '指定批次或纠偏记录不存在或已失效，请重新选择批次' } })
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
      if (name === '/api/dataset-releases/candidates') return json({ candidates: [{ session_id: 'internal-session', batch_id: 'batch-a', tree_run_id: 'old-run-not-business', ready: true, reason: '', updated_at: '', latest_excel: { filename: 'current.xlsx', created_at: '', rows: 2, path: '' }, task_count: 2, trajectory_count: 2, step_count: 2 }] })
      if (name === '/api/dataset-releases') return json({ releases: [] })
      if (name === '/api/dataset-upload-capabilities') return json({ internal: { configured: false, reason: 'fixture' } })
      return route.fulfill({ status: 404, json: { detail: 'Unmocked API: ' + name } })
    })

    await page.goto(base + '/collection/tree-building?batch_id=batch-a')
    await page.getByText('建树已完成 1 / 待处理 0 / 已失效 1', { exact: false }).waitFor()
    assert.equal(await page.getByText('标框版本', { exact: false }).count(), 0)
    await page.screenshot({ path: path.join(output, 'collection.png'), fullPage: true })

    await page.goto(base + '/quality?batch_id=batch-a')
    await page.getByText('已完成 1 · 待处理 0 · 已失效 1').waitFor()
    assert.equal(await page.locator('.task-row').count(), 2)
    assert.equal(await page.locator('.task-row').filter({ hasText: 'TASK-B' }).getByRole('checkbox').isDisabled(), true)
    await page.locator('.run-selector .el-select__wrapper').click()
    await page.getByRole('option', { name: /batch-b/ }).click()
    await page.getByText('TASK-C', { exact: true }).waitFor()
    assert.match(page.url(), /batch_id=batch-b/)
    assert.equal(await page.locator('.task-row').count(), 1)
    await page.screenshot({ path: path.join(output, 'quality.png'), fullPage: true })

    await page.goto(base + '/correction/expert-action?batch_id=batch-a')
    await page.locator('.task-title').filter({ hasText: 'TASK-A' }).click()
    await page.locator('.trajectory-title').filter({ hasText: 'TASK-A-1' }).click()
    await page.getByText('专家保留说明', { exact: true }).waitFor()
    await page.getByRole('columnheader', { name: '当前步骤', exact: true }).waitFor()
    assert.equal(await page.locator('.trajectory-title').filter({ hasText: 'TASK-A-1' }).getByRole('button', { name: '加入导出' }).isDisabled(), true)
    await page.screenshot({ path: path.join(output, 'correction-review.png'), fullPage: true })
    await page.getByRole('button', { name: '采用保留的修改', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.review-panel'))
    assert.equal(reviews.at(-1).decision, 'adopt')
    assert.equal(reviews.at(-1).expected_revision, 7)

    groups = [group('TASK-A', 2, true), group('TASK-B', 3, false)]; revision = 10
    await page.reload()
    await page.locator('.task-title').filter({ hasText: 'TASK-A' }).click()
    await page.locator('.trajectory-title').filter({ hasText: 'TASK-A-1' }).click()
    await page.getByRole('button', { name: '放弃保留的修改', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.review-panel'))
    assert.equal(reviews.at(-1).decision, 'discard')
    assert.equal(reviews.at(-1).expected_revision, 10)

    groups = [group('TASK-A', 2, true), group('TASK-B', 3, false)]; revision = 12
    await page.goto(base + '/correction/cot-generation?batch_id=batch-a')
    await page.getByRole('button', { name: '批量生成 bbox + COT', exact: true }).click()
    await page.getByRole('button', { name: '确认覆盖生成', exact: true }).click()
    await page.getByText('生成完成', { exact: true }).waitFor()
    assert.deepEqual(cotSubmissions.at(-1).group_ids, ['group-TASK-B'])
    assert.equal(cotSubmissions.at(-1).expected_revision, 12)
    await page.getByRole('button', { name: '导出数据集', exact: true }).click()
    await page.getByText('完整数据集已导出，替换 1 个步骤', { exact: true }).waitFor()
    await page.getByRole('button', { name: '导出数据集', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.el-message--success').length >= 2)
    assert.deepEqual(datasetExports.map(item => item.expected_revision), [12, 13])
    await page.screenshot({ path: path.join(output, 'cot.png'), fullPage: true })

    await page.goto(base + '/data-publishing/archive')
    await page.locator('.candidate-title').getByText('batch-a', { exact: true }).waitFor()
    assert.equal(await page.getByText('old-run-not-business', { exact: false }).count(), 0)
    await page.screenshot({ path: path.join(output, 'publishing.png'), fullPage: true })
    await page.goto(base + '/quality?run=current-run')
    await page.getByText('TASK-C', { exact: true }).waitFor()
    await page.waitForURL('**/quality?batch_id=batch-b')
    await page.goto(base + '/correction/expert-action?tree_run_id=current-correction-run')
    await page.locator('.task-title').filter({ hasText: 'TASK-A' }).waitFor()
    await page.waitForURL('**/correction/expert-action?batch_id=batch-a')
    await page.goto(base + '/correction/cot-generation?session_id=internal-session')
    await page.locator('.step-item').first().waitFor()
    await page.waitForURL('**/correction/cot-generation?batch_id=batch-a')
    const invalidLinks = [
      ['/quality?run=expired-run', '该运行已失效，请查看批次当前结果'],
      ['/quality?run=unknown-run', '任务集不存在'],
      ['/quality?batch_id=missing', '指定批次不存在或当前不可用，请重新选择批次'],
      ['/quality?batch_id=', '指定批次不存在或当前不可用，请重新选择批次'],
      ['/correction/expert-action?tree_run_id=expired-run', '该运行已失效，请查看批次当前结果'],
      ['/correction/expert-action?tree_run_id=unknown-run', '任务集不存在'],
      ['/correction/expert-action?batch_id=missing', '指定批次不存在或当前不可用，请重新选择批次'],
      ['/correction/cot-generation?session_id=deleted-session', '指定批次或纠偏记录不存在或已失效，请重新选择批次'],
      ['/correction/cot-generation?batch_id=missing', '指定批次或纠偏记录不存在或已失效，请重新选择批次'],
    ]
    for (const [pathname, message] of invalidLinks) {
      const offset = requests.length
      await page.goto(base + pathname)
      await page.locator('.el-alert').getByText(message, { exact: true }).waitFor()
      assert.equal(new URL(page.url()).pathname + new URL(page.url()).search, pathname)
      assert.equal(await page.locator('.task-row,.task-title,.step-item').count(), 0)
      assert.equal(requests.slice(offset).some(request => (request.name.startsWith('/api/data-batches/') && !request.name.endsWith('/lifecycle')) || request.name === '/api/correction/sessions/internal-session' || request.name.endsWith('/cot')), false)
    }
    await page.screenshot({ path: path.join(output, 'invalid-link.png'), fullPage: true })
    assert.deepEqual(errors, [])
    assert.equal(requests.some(request => request.name === '/api/tree-runs'), false)
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ passed: true, reviews, cotSubmissions, errors }, null, 2))
    console.log(JSON.stringify({ passed: true, pages: 5, reviewDecisions: reviews.length, invalidLinks: 9, currentAliases: 3, output }))
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
