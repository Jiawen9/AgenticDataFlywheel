// Built UI smoke test; every API request is mocked. No phone/backend/model calls.
const { chromium } = require('playwright')
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), http = require('node:http')
const dist = path.resolve(__dirname, '../dist'), output = path.resolve(__dirname, '../.tmp/page-loading-browser')
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const runs = Array.from({length: 100}, (_, i) => ({run_id: `cr-${i}`, collection_run_id: `cr-${i}`, batch_id: `b-${i}`, status: 'completed', collection_status: 'completed', transfer_status: 'completed', created_at: '2026-11-12T00:00:00Z', agent_type: 'pipelineagent', execution_mode: 'generate'}))
const pipes = runs.map((run, i) => ({pipeline_id: `p-${i}`, name: `流水线 ${i}`, batch_id: run.batch_id, status: 'succeeded', mode: 'manual', current_step: 'overview', storage_revision: 1, created_at: run.created_at, updated_at: run.created_at, collection_run_ids: [run.run_id], task_ids: [], steps: [{id: 'overview', label: '汇总', status: 'succeeded', job_ids: []}]}))
function page(rows, url) { const offset = +(url.searchParams.get('offset') || 0), limit = +(url.searchParams.get('limit') || 10), search = url.searchParams.get('search') || ''; const filtered = rows.filter(row => JSON.stringify(row).includes(search)); return {items: filtered.slice(offset, offset + limit), latest: rows.slice(0, 3), total: filtered.length, filtered_total: filtered.length, scope_counts: {total: rows.length, active: 0, abnormal: 7}, offset, limit, has_more: offset + limit < filtered.length, index_state: 'ready', counts_complete: true, updated_at: '2026-11-12T00:00:00Z'} }
async function main() {
  await fs.mkdir(output, {recursive: true})
  const server = http.createServer(async (req, res) => { try { const url = new URL(req.url, 'http://localhost'); let file = path.resolve(dist, '.' + decodeURIComponent(url.pathname)); if (!file.startsWith(dist + path.sep) || !path.extname(file)) file = path.join(dist, 'index.html'); const data = await fs.readFile(file); res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'); res.end(data) } catch { res.writeHead(404); res.end() } })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const base = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({headless: true, channel: process.env.ADF_BROWSER_CHANNEL || 'msedge'}).catch(error => { server.close(); throw error })
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}}), tab = await context.newPage(), calls = [], errors = []
  let delayPipeline = 0
  tab.on('pageerror', error => errors.push(String(error)))
  await context.route('**/api/**', async route => {
    const url = new URL(route.request().url()), p = url.pathname; calls.push(p + url.search)
    const send = json => route.fulfill({json})
    if (p === '/api/display-identifiers/resolve') return send({items: (route.request().postDataJSON().items || []).map(item => ({...item, display_id: item.id, mapped: false}))})
    if (p === '/api/pipelines') { if (delayPipeline) await sleep(delayPipeline); return send(page(url.searchParams.get('active_only') === 'true' ? [] : pipes, url)) }
    if (p.startsWith('/api/pipelines/')) return send({pipeline: pipes.find(row => row.pipeline_id === p.split('/').pop())})
    if (p.endsWith('/batches')) return send({batches: []})
    if (p.endsWith('/state')) return send({phones: [], apps: [], phoneApps: [], tasks: [], vla: []})
    if (p.endsWith('/config')) return send({sampling_enabled: false, temperature: 1, top_p: 1, use_experience_lib: false, capture_logprobs: false, repeat_times: 1})
    if (p.endsWith('/agent-capabilities')) return send({ok: true, agent_types: {}})
    if (p.endsWith('/remote/status')) return send({ok: true, statuses: []})
    if (p.endsWith('/remote/adb-devices')) return send({ok: true, devices: []})
    if (p.endsWith('/evaluation-config')) return send({checker: {base_url: '', model: ''}, supervisor: {base_url: '', model: ''}})
    if (p.endsWith('/evaluation-capabilities')) return send({ok: true, supported: false, message: 'offline fixture'})
    if (p.endsWith('/collection-runs') || p === '/api/model-iter/runs') return send(page(runs.map(row => p.startsWith('/api/model-iter') ? {...row, execution_mode: 'evaluate_only'} : row), url))
    if (p.endsWith('/reports')) return send(page(Array.from({length: 30}, (_, i) => ({key: `report-${i}`, folder: {index: i + 1, dir_name: `legacy-${i}`, files: []}, file: {name: `report-${i}.json`, size: 10}})), url))
    if (p.endsWith('/evaluations')) { if (url.searchParams.get('view') === 'cases') return send(page(Array.from({length: 500}, (_, i) => ({case_key: `case-${i}`, case_id: `case-${i}`, phone_id: 'phone', collection_status: 'completed', evaluation_status: 'completed', errors: []})), url)); return send({run_id: 'cr-0', status: 'completed', execution_mode: 'evaluate_only', summary: {total: 500, evaluation_completed: 500, evaluation_pending: 0, evaluation_running: 0, evaluation_failed: 0, success_rate: 1}, cases: []}) }
    if (p.endsWith('/diagnostics')) return send({run_id: 'cr-0', supported: false, phones: [], summary: {total: 0, healthy: 0, warning: 0, error: 0}, message: 'fixture'})
    if (/\/(collection-runs|runs)\/cr-\d+$/.test(p)) return send({...runs.find(row => row.run_id === p.split('/').pop()), ...(p.startsWith('/api/model-iter') ? {execution_mode: 'evaluate_only'} : {})})
    return route.fulfill({status: 404, json: {error: 'unneeded fixture route'}})
  })
  try {
    await tab.goto(base + '/pipeline'); await tab.getByText('流水线 0', {exact: true}).first().waitFor()
    assert.equal(await tab.locator('.history-panel .el-table__body .el-table__row').count(), 10)
    await tab.locator('.history-panel .btn-next').click(); await tab.getByText('流水线 10', {exact: true}).waitFor()
    assert.ok(new URL(tab.url()).searchParams.get('pipeline_id') === 'p-0', 'history paging must not steal selected detail')
    assert.ok(await tab.evaluate(() => Object.keys(sessionStorage).some(key => key.startsWith('adf.summary.v1:'))), 'summary snapshot persisted'); delayPipeline = 5000; await tab.reload({waitUntil: 'domcontentloaded'}); await tab.getByText('显示缓存摘要，正在后台更新；操作以最新详情为准。').waitFor(); delayPipeline = 0
    await tab.goto(base + '/collection/phone-factory'); await tab.getByTestId('run-overview').getByText('100', {exact: true}).waitFor()
    await tab.getByRole('button', {name: '查看全部', exact: true}).click(); await tab.getByTestId('factory-runs').locator('.el-table__row').first().waitFor()
    assert.equal(await tab.getByTestId('factory-runs').locator('.el-table__body .el-table__row').count(), 10)
    await tab.getByTestId('run-details-tab').locator('.btn-next').click(); await tab.getByTestId('factory-runs').getByText('cr-10', {exact: true}).waitFor()
    await tab.setViewportSize({width: 390, height: 844}); await sleep(600)
    const drawer = await tab.locator('.factory-details-drawer').boundingBox(); assert.ok(drawer.width <= 391 && drawer.width >= 385, JSON.stringify(drawer))
    await tab.screenshot({path: path.join(output, 'factory-narrow.png')})
    await tab.setViewportSize({width: 1440, height: 1000}); await tab.goto(base + '/model-iteration-evaluation')
    await tab.getByTestId('run-overview').getByText('100', {exact: true}).waitFor()
    assert.equal(calls.filter(url => url.startsWith('/api/model-iter/reports')).length, 0, 'closed reports must not load')
    await tab.getByRole('button', {name: '查看报告', exact: true}).click(); await tab.getByText('report-0.json', {exact: true}).waitFor()
    await tab.getByRole('tab', {name: '用例明细', exact: true}).click(); await tab.getByTestId('evaluation-cases').getByText('case-0', {exact: true}).waitFor()
    assert.equal(await tab.getByTestId('evaluation-cases').locator('.el-table__body .el-table__row').count(), 10)
    await tab.screenshot({path: path.join(output, 'evaluation-desktop.png')})
    assert.deepEqual(errors, [])
    const result = {passed: true, checks: ['pipeline SQL pagination preserves selected ID', 'session snapshot paints before slow refresh', '100 runs paginated at 10', '390px fullscreen drawer', '30 report files lazy loaded', '500 evaluation cases paginated at 10'], api_requests: calls.length, page_errors: errors}
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result))
  } catch (error) { await tab.screenshot({path: path.join(output, 'failure.png')}); await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({calls, errors, url: tab.url(), text: await tab.locator('body').innerText()}, null, 2)); throw error } finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
