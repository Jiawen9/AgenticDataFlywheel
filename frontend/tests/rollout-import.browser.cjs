// Real rendered forms with every API mocked; no phone, model or upload service.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises'), path = require('node:path'), http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = process.env.ADF_BROWSER_ARTIFACTS || path.resolve(__dirname, '../../backend_workspace/tmp/rollout-import-validation/browser')
const clone = value => JSON.parse(JSON.stringify(value))
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const now = new Date().toISOString()
const source = 'D:\\offline-fixtures\\raw\\rollout_trajectories'
const task = { task_id: 'stable-case-A', collection_case_id: 'case-A', task: '打开设置', goal: '打开设置', app: '源文件 App', scene: '工具', capability: '设置', trajectory_count: 2, step_count: 4, annotated: false, warning: '' }
const batch = id => ({ batch_id: id, label: '导入 Rollout', kind: 'rollout_import', task_count: 1, ready_trajectory_count: 2, ready_step_count: 4, collection_status: 'ready', preprocessing_status: 'not_started', latest_job: null, annotation_version: null, artifacts: [], can_start: true, reason: null })
const detail = id => ({ schema_version: 1, batch_id: id, name: id, source_job_id: null, kind: 'rollout_import', job_status: 'succeeded', created_at: now, task_count: 1, apps: ['源文件 App'], filename: id + '.xlsx', snapshot: { tasks: [task] } })
const steps = ['collection', 'preprocessing', 'tree', 'quality', 'correction', 'cot', 'publication', 'overview']
async function main() {
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://local').pathname)
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
  const calls = [], errors = [], unexpected = [], cases = [], imported = [], pipelines = [], previews = new Map(), commits = new Map(), jobs = new Map()
  let previewNumber = 0, invalidNextPreview = false, holdPreview = false, releasePreview, failCommitOnce = false
  const writes = () => calls.filter(call => call.method !== 'GET')
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), name = url.pathname, method = request.method(), body = request.postDataJSON()
      if (url.origin !== base) { unexpected.push('external ' + request.url()); return route.abort() }
      if (!name.startsWith('/api/')) return route.continue()
      calls.push({ name, method, body, query: Object.fromEntries(url.searchParams) })
      const send = async value => { try { await route.fulfill({ json: clone(value) }) } catch (error) { if (!/already handled|closed|Invalid InterceptionId/.test(String(error))) throw error } }
      if (name === '/api/rollout-imports/options') return send({ default_source_path: source, allowed_roots: ['D:\\offline-fixtures\\raw'] })
      if (name === '/api/rollout-imports/preview') {
        const valid = !invalidNextPreview; invalidNextPreview = false
        const value = { import_id: 'preview-' + ++previewNumber, batch_id: body.batch_id, source_path: body.source_path, valid,
          task_count: 1, trajectory_count: 2, step_count: 4, tasks: [{ ...task, task: body.task_overrides?.[0]?.task || task.task }],
          warnings: ['目录为原始来源；将复制冻结文件后登记新批次'], errors: valid ? [] : ['case-A：缺少任务目标，请补充后重新校验'], expires_at: new Date(Date.now() + 3600000).toISOString() }
        previews.set(value.import_id, value)
        if (holdPreview) { holdPreview = false; await new Promise(resolve => { releasePreview = resolve }) }
        return send(value)
      }
      if (name === '/api/rollout-imports' && method === 'POST') {
        const preview = previews.get(body.import_id); assert.ok(preview?.valid)
        let result = commits.get(body.request_id)
        if (!result) { result = { batch_id: preview.batch_id, collection_run_id: 'import-run-' + preview.batch_id, task_count: 1, trajectory_count: 2, step_count: 4, reused: false }; commits.set(body.request_id, result); imported.push(batch(preview.batch_id)) }
        if (failCommitOnce) { failCommitOnce = false; return route.fulfill({ status: 502, json: { detail: '模拟导入响应丢失' } }) }
        return send({ ...result, reused: true })
      }
      if (name === '/api/pipelines' && method === 'POST') {
        const pipeline = { ...body, pipeline_id: 'import-pipeline', task_ids: [task.task_id], status: 'running', current_step: 'collection', storage_revision: 1, created_at: now, updated_at: now,
          collection_run_ids: [], steps: steps.map(id => ({ id, label: id, status: 'pending', percent: 0, jobs: [], job_ids: [] })) }
        pipelines.push(pipeline); return send({ pipeline })
      }
      if (name === '/api/pipelines') return send({ pipelines: pipelines.filter(value => !url.searchParams.get('batch_id') || value.batch_id === url.searchParams.get('batch_id')) })
      if (name === '/api/pipelines/import-pipeline') return send({ pipeline: pipelines[0] })
      if (name.endsWith('/lifecycle')) return send({ batch_id: name.split('/')[3], status: 'active', published_at: null, release_id: null })
      if (name === '/api/trajectory-preprocessing/batches') return send({ batches: [batch('previous-active-batch'), ...imported] })
      if (name === '/api/phone-factory/batches') return send({ batches: [detail('previous-active-batch'), ...imported.map(value => detail(value.batch_id))] })
      if (name.startsWith('/api/phone-factory/batches/')) return send(detail(name.split('/').at(-1)))
      if (name.startsWith('/api/rollout-imports/batches/')) return send(detail(name.split('/').at(-1)))
      if (name === '/api/phone-factory/state') return send({ phones: [], apps: [], phoneApps: [], vla: [], tasks: [] })
      if (name === '/api/phone-factory/config') return send({ sampling_enabled: false, temperature: .7, top_p: .9, use_experience_lib: false })
      if (name === '/api/phone-factory/collection-runs') return send({ runs: [{ collection_run_id: 'import-run-' + url.searchParams.get('batch_id'), batch_id: url.searchParams.get('batch_id'), source_kind: 'rollout_import', status: 'completed', created_at: now, completed_at: now, batch_tasks: { 'case-A': task }, trajectories: [], errors: [] }] })
      if (name === '/api/trajectory-preprocessing/jobs' && method === 'POST') {
        const value = { job_id: 'pre-' + body.batch_id, batch_id: body.batch_id, status: 'running', stage: 'annotating', percent: 25, total_steps: 4, completed_steps: 1, current_task: task.task_id, current_trajectory: 'same-run', current_step: 1, created_at: now, artifacts: [], error: null }
        jobs.set(value.job_id, value); return send(value)
      }
      if (name.startsWith('/api/trajectory-preprocessing/jobs/')) return send(jobs.get(name.split('/').at(-1)))
      if (name === '/api/tasks') return send({ tasks: [task] })
      if (name === '/api/tree-builds') return send({ jobs: [] })
      unexpected.push(method + ' ' + name); return route.fulfill({ status: 501, json: { detail: 'Unmocked API blocked: ' + name } })
    })
    const dialog = () => page.getByRole('dialog', { name: '导入已有 Rollout', exact: true })
    const field = (id, element = 'input') => dialog().locator(`${element}[data-testid="rollout-import-${id}"], [data-testid="rollout-import-${id}"] ${element}`)
    const button = id => dialog().getByTestId('rollout-import-' + id)
    async function openImport(id) {
      await page.getByTestId('open-rollout-import').click(); await dialog().waitFor()
      await field('source-path').waitFor()
      await page.waitForFunction(() => !document.querySelector('input[data-testid="rollout-import-source-path"], [data-testid="rollout-import-source-path"] input')?.disabled)
      assert.equal(await field('source-path').inputValue(), source)
      await field('batch-id').fill(id)
    }
    await page.goto(base + '/pipeline')
    await page.getByRole('button', { name: '新建 Pipeline', exact: true }).click()
    const createDialog = page.getByRole('dialog', { name: '新建 Pipeline', exact: true })
    await createDialog.getByPlaceholder('例如：手机采集批次验收').fill('保留我的 Pipeline 配置')
    await createDialog.locator('.el-form-item').filter({ hasText: '业务批次' }).locator('.el-select__wrapper').click()
    await page.getByRole('option', { name: /previous-active-batch/ }).click()
    await openImport('raw-pipeline-batch')
    assert.equal(await button('commit').isDisabled(), true)
    invalidNextPreview = true
    await button('preview').click(); await dialog().getByText('校验未通过', { exact: true }).waitFor()
    assert.equal(await button('commit').isDisabled(), true)
    assert.match(await dialog().getByTestId('rollout-import-summary').innerText(), /1 个任务 · 2 条轨迹 · 4 步/)
    await field('task-case-A', 'textarea').fill('人工补充的真实任务目标')
    await dialog().getByText('信息已修改，请重新校验', { exact: true }).waitFor()
    await button('preview').click(); await dialog().getByText('校验通过', { exact: true }).waitFor()
    assert.equal(await button('commit').isEnabled(), true)
    assert.equal(calls.filter(call => call.name === '/api/rollout-imports/preview').at(-1).body.task_overrides[0].task, '人工补充的真实任务目标')
    cases.push('任务缺失时预览定位错误并禁止导入；补充真实任务后重新校验')

    await field('app').fill('表单补缺 App')
    assert.equal(await button('commit').isDisabled(), true)
    await field('scene').fill('测试一级'); await field('capability').fill('测试二级')
    await button('preview').click(); await dialog().getByText('校验通过', { exact: true }).waitFor()
    assert.match(await dialog().getByTestId('rollout-import-summary').innerText(), /源文件 App \/ 工具 \/ 设置/)
    await page.screenshot({ path: path.join(output, 'rollout-import-preview.png'), fullPage: true })
    assert.equal(writes().filter(call => !call.name.endsWith('/preview')).length, 0)
    cases.push('分类修改使预览失效，源文件分类优先展示；预览不启动预处理或 Pipeline')

    failCommitOnce = true
    await button('commit').click(); await dialog().getByText(/模拟导入响应丢失/).waitFor()
    await button('commit').click(); await dialog().waitFor({ state: 'hidden' })
    const importCalls = calls.filter(call => call.name === '/api/rollout-imports' && call.method === 'POST')
    assert.equal(importCalls.length, 2); assert.deepEqual(importCalls[0].body, importCalls[1].body); assert.equal(imported.length, 1)
    assert.equal(await createDialog.getByPlaceholder('例如：手机采集批次验收').inputValue(), '保留我的 Pipeline 配置')
    assert.match(await createDialog.locator('.el-form-item').filter({ hasText: '业务批次' }).innerText(), /raw-pipeline-batch · 导入 Rollout/)
    assert.equal(calls.filter(call => call.name === '/api/pipelines' && call.method === 'POST').length, 0)
    await page.screenshot({ path: path.join(output, 'pipeline-import-selected.png'), fullPage: true })
    await createDialog.getByRole('button', { name: '创建并开始', exact: true }).click(); await page.waitForURL('**pipeline_id=import-pipeline')
    assert.equal(pipelines.length, 1); assert.equal(pipelines[0].start_mode, 'existing'); assert.equal(pipelines[0].batch_id, 'raw-pipeline-batch')
    cases.push('导入丢失响应重试复用请求编号；新批次选中且配置保留，点击创建才开始 existing Pipeline')

    await page.goto(base + '/collection/tree-building')
    await page.getByRole('button', { name: '选择批次 previous-active-batch', exact: true }).click()
    await openImport('cancelled-batch')
    const beforeClose = writes().length
    holdPreview = true
    await button('preview').click()
    while (!releasePreview) await pause(20)
    await dialog().getByRole('button', { name: '取消', exact: true }).click(); await dialog().waitFor({ state: 'hidden' })
    await openImport('raw-manual-batch')
    await button('preview').click(); await dialog().getByText('校验通过', { exact: true }).waitFor()
    releasePreview(); releasePreview = null; await pause(150)
    assert.equal(await field('batch-id').inputValue(), 'raw-manual-batch')
    assert.equal(await button('commit').isEnabled(), true)
    assert.equal(writes().slice(beforeClose).filter(call => call.name === '/api/rollout-imports').length, 0)
    await button('commit').click(); await dialog().waitFor({ state: 'hidden' })
    const lastCommit = calls.filter(call => call.name === '/api/rollout-imports' && call.method === 'POST').at(-1)
    assert.equal(previews.get(lastCommit.body.import_id).batch_id, 'raw-manual-batch')
    cases.push('关闭后重新打开隔离预览；迟到响应不能覆盖新草稿或恢复旧批次')

    await page.getByRole('button', { name: '选择批次 raw-manual-batch', exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '选择批次 raw-manual-batch', exact: true }).getAttribute('aria-expanded'), 'true')
    assert.equal(calls.filter(call => call.name === '/api/trajectory-preprocessing/jobs' && call.method === 'POST').length, 0)
    await page.getByRole('button', { name: '开始预处理', exact: true }).click(); await page.getByLabel('预处理进度').waitFor()
    const preprocessCalls = calls.filter(call => call.name === '/api/trajectory-preprocessing/jobs' && call.method === 'POST')
    assert.equal(preprocessCalls.length, 1); assert.equal(preprocessCalls[0].body.batch_id, 'raw-manual-batch')
    assert.match(await page.getByLabel('预处理进度').innerText(), /1.*4/)
    await page.screenshot({ path: path.join(output, 'imported-raw-preprocessing.png'), fullPage: true })
    cases.push('预处理页导入后选择新批次，用户提交前不运行；提交后显示同一批次真实作业进度')

    assert.deepEqual(errors, []); assert.deepEqual(unexpected, [])
    assert.equal(calls.some(call => call.name.includes('/remote/')), false)
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ cases, calls, errors, unexpected }, null, 2))
    console.log(JSON.stringify({ passed: cases.length, cases, output }, null, 2))
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), calls, errors, unexpected }, null, 2))
    throw error
  } finally {
    if (releasePreview) releasePreview()
    await browser.close(); await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
