// Run after `npm run build`. Uses only generated fixtures and the local dist folder.
// Every API and external request is intercepted; no model, backend, or phone is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')

const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(__dirname, '../../backend_workspace/augmentation-review-browser')
const clone = value => JSON.parse(JSON.stringify(value))
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const node = (id, label, kind, children = []) => ({ id, label, kind, children })
const tree = {
  version: 'frozen-test-tree', leaf_count: 4, execution_unit_count: 4, warnings: [],
  scenes: [
    node('scene-1', '视频场景', 'scene', [
      node('cap-1', '搜索能力', 'capability', [node('type-1', '搜索任务', 'sub_capability', [node('app-1', '测试视频', 'app')])]),
      node('cap-unused', '未关联能力', 'capability', [node('type-unused', '未关联任务', 'sub_capability', [node('app-unused', '测试视频', 'app')])]),
    ]),
    node('scene-2', '生活场景', 'scene', [node('cap-2', '搜索能力', 'capability', [node('type-2', '搜索任务', 'sub_capability', [node('app-2', '测试生活', 'app')])])]),
    node('scene-unused', '未关联场景', 'scene', [node('cap-other', '其他能力', 'capability', [node('type-other', '其他任务', 'sub_capability', [node('app-other', '其他 App', 'app')])])]),
  ],
}
const seed = index => ({
  seed_id: `seed-${index}`, source_row: index + 2,
  task: index < 2 ? '相同源失败任务，来自不同输入行' : `源失败任务 ${index + 1}`,
  app: index === 1 ? '测试生活' : '测试视频', scene: index === 1 ? '生活场景' : '视频场景',
  capability: '搜索能力', sub_capability: '搜索任务', classification_source: 'excel', classification_status: 'classified',
  mapping_status: 'matched', node_path_ids: index === 1 ? ['scene-2', 'cap-2', 'type-2', 'app-2'] : ['scene-1', 'cap-1', 'type-1', 'app-1'],
  generation_status: 'succeeded', result_count: index < 2 ? 10 : 1,
})
const variant = (source, index, extra = {}) => ({
  result_id: `${source.seed_id}-result-${index}`, seed_id: source.seed_id, source_row: source.source_row,
  source_task: source.task, '源失败任务': source.task, task: `${source.seed_id} 变体任务 ${index + 1}`,
  app: source.app, scene: source.scene, capability: source.capability, sub_capability: source.sub_capability,
  '用例编号': `${source.seed_id}-case-${index + 1}`, '审核状态': '待人工Review', deleted: false, ...extra,
})
const job = (id, count) => ({
  job_id: id, kind: 'augmentation', status: 'succeeded', stage: 'succeeded', created_at: '2026-09-11T09:30:00',
  started_at: '2026-09-11T09:30:01', completed_at: '2026-09-11T09:31:00', current_item: null,
  completed_items: count, total_items: count, percent: 100, generate_n: 10, input_filename: `${id}.xlsx`,
  result_count: count, errors: [], warnings: [], error: null, knowledge_base_version: tree.version,
})
const preview = (id, seeds, available = true) => ({
  job_id: id, available, ...(available ? { tree } : {}), seeds,
  stats: { total: seeds.length, matched: seeds.filter(value => value.mapping_status === 'matched').length,
    unmatched: seeds.filter(value => ['unclassified', 'not_found'].includes(value.mapping_status)).length,
    classification_failed: seeds.filter(value => value.mapping_status === 'classification_failed').length,
    eligible: seeds.filter(value => value.mapping_status !== 'classification_failed').length },
})
function fixtures() {
  const sources = Array.from({ length: 22 }, (_, index) => seed(index))
  const modern = sources.flatMap(source => Array.from({ length: source.result_count }, (_, index) => variant(source, index)))
  const historicSource = { ...seed(90), task: '历史源失败任务，共二十五个变体' }
  const legacy = Array.from({ length: 25 }, (_, index) => variant(historicSource, index, { seed_id: undefined, source_row: 92 }))
  legacy.push(variant(seed(91), 0, { seed_id: undefined, source_row: undefined, source_task: '无行号同文案', '源失败任务': '无行号同文案' }))
  legacy.push(variant(seed(92), 0, { seed_id: undefined, source_row: undefined, source_task: '无行号同文案', '源失败任务': '无行号同文案' }))
  const unmatched = { ...seed(93), seed_id: 'unmatched', task: '没有匹配分支的源任务', mapping_status: 'unclassified', node_path_ids: [], scene: 'Unclassified' }
  const emptySeed = { ...seed(50), task: '已分类但没有生成变体的源任务', result_count: 0, generation_status: 'failed' }
  const positioning = [...clone(modern), variant(seed(94), 0, { seed_id: undefined, source_row: 999, source_task: '同作业中的历史无种子结果', '源失败任务': '同作业中的历史无种子结果' })]
  const detailSeed = { ...seed(95), task: '请在测试视频中查找指定影片并核对详情。'.repeat(18) + '【完整任务末尾标记】', classification_source: 'model', generation_status: 'partial', result_count: 1, reason: '完整场景匹配说明：保留源分类路径', error: '模拟生成错误：单条变体超时，仅用于隔离测试' }
  const failedSeed = { ...seed(96), task: '分类失败时仍然完整展示这个失败源任务', classification_source: 'model', classification_status: 'failed', mapping_status: 'classification_failed', node_path_ids: [], generation_status: 'skipped', result_count: 0, error: '模拟分类错误：分类服务不可用，仅用于隔离测试' }
  return {
    modern: { job: job('modern', modern.length), results: modern, preview: preview('modern', sources) },
    legacy: { job: job('legacy', legacy.length), results: legacy, preview: preview('legacy', [], false) },
    unmatched: { job: job('unmatched', 1), results: [variant(unmatched, 0)], preview: preview('unmatched', [unmatched]) },
    delayed: { job: job('delayed', 1), results: [variant({ ...seed(99), seed_id: 'delayed', task: '迟到作业源任务' }, 0)], preview: preview('delayed', [seed(99)]) },
    positioning: { job: job('positioning', positioning.length), results: positioning, preview: preview('positioning', [...sources, emptySeed]) },
    details: { job: { ...job('details', 1), status: 'partial' }, results: [variant(detailSeed, 0)], preview: preview('details', [detailSeed, failedSeed]) },
  }
}

async function eventually(check, message) {
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    if (await check()) return
    await pause(40)
  }
  assert.fail(message)
}
async function mockPage(browser, base, width = 1440) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, hasTouch: width === 390, reducedMotion: 'reduce', serviceWorkers: 'block' })
  const state = { data: fixtures(), requests: [], blocked: [], errors: [], batches: {}, delayPreview: false, lateStarted: false, lateFinished: false }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (!url.pathname.startsWith('/api/')) {
      if (url.origin === base) return route.continue()
      state.blocked.push(request.url()); return route.abort('blockedbyclient')
    }
    const record = { path: url.pathname, method: request.method(), body: request.postData(), query: url.search }
    state.requests.push(record)
    const send = json => route.fulfill({ json: clone(json) })
    if (url.pathname === '/api/task-generation/jobs') return send({ jobs: Object.values(state.data).map(value => value.job) })
    if (url.pathname === '/api/task-generation/collection-batches') {
      const selected = state.batches[url.searchParams.get('job_id')]
      return send({ batches: selected ? [selected] : [] })
    }
    const match = url.pathname.match(/^\/api\/task-generation\/jobs\/([^/]+)(?:\/(.*))?$/)
    if (match && state.data[match[1]]) {
      const id = match[1], suffix = match[2] || '', entry = state.data[id]
      if (!suffix) return send(entry.job)
      if (suffix === 'augmentation-preview') {
        if (state.delayPreview && id === 'delayed') { state.lateStarted = true; await pause(900); state.lateFinished = true }
        const value = clone(entry.preview)
        if (url.searchParams.get('include_tree') === 'false') delete value.tree
        return send(value)
      }
      if (suffix === 'results') return send({ results: entry.results, errors: [] })
      if (suffix.startsWith('results/') && request.method() === 'PATCH') {
        const row = entry.results.find(value => value.result_id === decodeURIComponent(suffix.slice(8)))
        assert.ok(row, 'Patch must address a fixture result')
        Object.assign(row, request.postDataJSON())
        return send({ result: row })
      }
      if (suffix === 'export' && request.method() === 'POST') {
        record.taskCount = entry.results.filter(value => !value.deleted).length
        return send({ filename: `${id}.xlsx`, download_url: `/api/task-generation/jobs/${id}/exports/${id}.xlsx`, created_at: '2026-09-11T11:00:00', row_count: record.taskCount })
      }
      if (suffix.startsWith('exports/')) return route.fulfill({ body: Buffer.from('isolated workbook download'), contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers: { 'content-disposition': 'attachment; filename="fixture.xlsx"' } })
      if (suffix === 'collection-batch' && request.method() === 'POST') {
        record.taskCount = entry.results.filter(value => !value.deleted).length
        const batch = { schema_version: 1, batch_id: id, source_job_id: id, kind: 'augmentation', job_status: 'succeeded',
          knowledge_base_version: tree.version, created_at: '2026-09-11T11:10:00', task_count: record.taskCount,
          apps: [...new Set(entry.results.map(value => value.app))], filename: `${id}.xlsx`, download_url: `/api/task-generation/collection-batches/${id}/workbook` }
        state.batches[id] = batch
        return send(batch)
      }
    }
    state.blocked.push(`${request.method()} ${url.pathname}`)
    return route.fulfill({ status: 501, json: { detail: 'Unmocked API blocked by isolated browser review' } })
  })
  await page.goto(`${base}/task-generation/augmentation`)
  await page.locator('.variant-group').first().waitFor()
  return { page, state }
}
const groups = page => page.locator('.variant-group')
const toggle = group => group.locator('.variant-group-toggle')
const bodyRows = group => group.locator('.result-table .el-table__body-wrapper tbody tr')
async function setSeedPanel(page, expanded) {
  const button = page.locator('.seed-panel-toggle')
  if (await button.getAttribute('aria-expanded') !== String(expanded)) await button.click()
  await eventually(async () => await button.getAttribute('aria-expanded') === String(expanded), 'Source panel toggles explicitly')
}
async function chooseJob(page, id) {
  await page.locator('.job-tabs button').filter({ hasText: `${id}.xlsx` }).click()
  await eventually(async () => (await page.locator('.job-meta').innerText()).includes(`${id}.xlsx`), `Job ${id} should become selected`)
  await page.locator('.variant-group').first().waitFor()
}
async function screenshot(page, name, preserveHorizontalScroll = false) {
  if (!preserveHorizontalScroll) await page.evaluate(() => window.scrollTo({ left: 0 }))
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  await page.screenshot({ path: path.join(output, `${name}.png`), animations: 'disabled' })
}
async function settlePopover(popover) {
  await eventually(async () => await popover.evaluate(element => getComputedStyle(element).opacity === '1' && !/(?:enter-active|enter-from)/.test(element.className)), 'Detail popover finishes its opening animation')
}

async function checkReview(browser, base) {
  const { page, state } = await mockPage(browser, base)
  assert.equal(await groups(page).count(), 20, 'Parent pagination displays 20 source cases')
  assert.equal(await page.locator('.variant-group .result-table').count(), 0, 'Collapsed groups must not render variant tables')
  assert.equal(await page.locator('.variant-group-toggle[aria-expanded="true"]').count(), 0)
  assert.equal(await page.locator('.variant-group-source').filter({ hasText: '相同源失败任务，来自不同输入行' }).count(), 2, 'Equal source text with different seed IDs must stay separate')
  const first = groups(page).nth(0), second = groups(page).nth(1)
  await toggle(first).click()
  await eventually(async () => await bodyRows(first).count() === 10, 'One seed expands all ten variants')
  await toggle(second).click()
  assert.equal(await groups(page).count(), 20, 'Expanding a source does not filter the other sources')
  assert.equal(await bodyRows(second).count(), 10)
  assert.equal(await toggle(first).getAttribute('aria-expanded'), 'true', 'Groups expand independently')

  await first.getByRole('button', { name: '编辑', exact: true }).first().click()
  await first.locator('textarea').fill('取消折叠应保留此未保存文本')
  await toggle(first).click()
  const dialog = page.locator('.el-message-box')
  await dialog.waitFor({ state: 'visible' })
  await dialog.locator('.el-message-box__headerbtn').click()
  await dialog.waitFor({ state: 'hidden' })
  assert.equal(await toggle(first).getAttribute('aria-expanded'), 'true')
  assert.equal(await first.locator('textarea').inputValue(), '取消折叠应保留此未保存文本')
  await toggle(first).click()
  await dialog.getByRole('button', { name: '放弃修改', exact: true }).click()
  await eventually(async () => await toggle(first).getAttribute('aria-expanded') === 'false', 'Discard allows collapse')
  assert.equal(state.requests.filter(value => value.method === 'PATCH').length, 0, 'Discard must not persist edits')
  await toggle(first).click()
  await first.getByRole('button', { name: '编辑', exact: true }).first().click()
  await first.locator('textarea').fill('保存并折叠的人工变体')
  await toggle(first).click()
  await dialog.getByRole('button', { name: '保存并继续', exact: true }).click()
  await eventually(async () => await toggle(first).getAttribute('aria-expanded') === 'false', 'Save allows collapse')
  assert.equal(state.data.modern.results[0].task, '保存并折叠的人工变体')
  await toggle(first).click()
  assert.match(await first.innerText(), /保存并折叠的人工变体/)
  await first.getByRole('button', { name: '删除', exact: true }).first().click()
  await dialog.getByRole('button', { name: /^(确定|OK)$/ }).click()
  await first.getByRole('button', { name: '恢复', exact: true }).first().waitFor()
  assert.equal(state.data.modern.results[0].deleted, true)
  await first.getByRole('button', { name: '恢复', exact: true }).first().click()
  await dialog.getByRole('button', { name: /^(确定|OK)$/ }).click()
  await eventually(() => state.data.modern.results[0].deleted === false, 'Restore persists original result identity')
  await page.locator('.section-title').getByRole('button', { name: '刷新', exact: true }).click()
  await eventually(async () => await page.locator('.section-title button').isEnabled(), 'Refresh finishes')
  assert.equal(await toggle(first).getAttribute('aria-expanded'), 'true', 'Refresh keeps expanded source')
  assert.equal(await toggle(second).getAttribute('aria-expanded'), 'true')

  await first.getByRole('button', { name: '编辑', exact: true }).first().click()
  await first.locator('textarea').fill('父分页切换也需要保护')
  await page.locator('.group-pagination .btn-next').click()
  await dialog.waitFor({ state: 'visible' })
  await dialog.locator('.el-message-box__headerbtn').click()
  await dialog.waitFor({ state: 'hidden' })
  assert.equal(await groups(page).count(), 20)
  assert.equal(await first.locator('textarea').inputValue(), '父分页切换也需要保护')
  await page.locator('.group-pagination .btn-next').click()
  await dialog.getByRole('button', { name: '放弃修改', exact: true }).click()
  await eventually(async () => await groups(page).count() === 2, 'Parent page 2 contains remaining source groups')
  assert.equal(await page.locator('.variant-group .result-table').count(), 0)
  await page.locator('.group-pagination .btn-prev').click()
  await eventually(async () => await groups(page).count() === 20, 'Return to parent page 1')
  await setSeedPanel(page, true)
  await page.locator('.seed-table').getByRole('button', { name: '定位场景与变体', exact: true }).first().click()
  assert.equal(await groups(page).count(), 20, 'Source location must not filter the source groups')
  assert.equal(await toggle(groups(page).first()).getAttribute('aria-expanded'), 'true')
  await page.locator('[data-node-id="scene-2"]').click()
  await eventually(async () => await groups(page).count() === 1, 'Only the explicit scene selection filters results')
  await page.getByRole('button', { name: '导出全部未删除结果', exact: true }).click()
  await eventually(() => state.requests.some(value => value.path.endsWith('/modern/export')), 'Export requested')
  await page.locator('.collection-submission').getByRole('button', { name: '提交轨迹采集', exact: true }).click()
  await eventually(() => Boolean(state.batches.modern), 'Collection submission requested')
  for (const record of state.requests.filter(value => value.path.endsWith('/modern/export') || value.path.endsWith('/modern/collection-batch'))) {
    assert.equal(record.taskCount, 40, 'Filtered/collapsed display does not limit export or collection scope')
    assert.equal(record.body, null, 'Submit only the job identity in the URL')
  }
  await page.getByRole('button', { name: '清除筛选', exact: true }).click()
  await eventually(async () => await groups(page).count() === 20, 'Clearing filter restores all sources')

  await chooseJob(page, 'legacy')
  assert.equal(await groups(page).count(), 3, 'Known legacy source row groups together; missing source rows stay separate')
  assert.equal(await page.locator('.variant-group .result-table').count(), 0)
  await toggle(groups(page).first()).click()
  assert.equal(await bodyRows(groups(page).first()).count(), 20)
  await groups(page).first().locator('.variant-pagination .btn-next').click()
  await eventually(async () => await bodyRows(groups(page).first()).count() === 5, 'Historical large groups use child pagination')
  assert.match(await groups(page).first().innerText(), /变体任务 25/)
  const unknown = groups(page).nth(1)
  await toggle(unknown).click()
  await unknown.getByRole('button', { name: '删除', exact: true }).click()
  await dialog.getByRole('button', { name: /^(确定|OK)$/ }).click()
  await unknown.getByRole('button', { name: '恢复', exact: true }).waitFor()
  assert.equal(await groups(page).count(), 3, 'Entirely deleted source groups remain available for recovery')
  await unknown.getByRole('button', { name: '恢复', exact: true }).click()
  await dialog.getByRole('button', { name: /^(确定|OK)$/ }).click()
  await unknown.getByRole('button', { name: '删除', exact: true }).waitFor()
  await chooseJob(page, 'modern')
  assert.equal(await page.locator('.variant-group .result-table').count(), 0, 'Changing jobs resets expansion')
  state.delayPreview = true
  await page.locator('.job-tabs button').filter({ hasText: 'delayed.xlsx' }).click()
  await eventually(() => state.lateStarted, 'Delayed old job request began')
  await chooseJob(page, 'modern')
  await eventually(() => state.lateFinished, 'Delayed old response finished')
  assert.equal(await groups(page).count(), 20)
  assert.doesNotMatch(await page.locator('.jobs-card').innerText(), /迟到作业源任务/)
  assert.deepEqual(state.blocked, [])
  assert.deepEqual(state.errors, [])
  await page.close()
  return { requests: state.requests.length, patches: state.requests.filter(value => value.method === 'PATCH').length, exportAndCollectionScope: 40 }
}

const sourceRows = page => page.locator('.seed-table .el-table__body-wrapper tbody tr')
const seedPager = page => page.locator('.seed-pagination')
const activePage = async pagination => Number(await pagination.locator('.el-pager .is-active').innerText())
const sourceGroup = (page, seedId) => page.locator(`.variant-group[data-group-id="seed:${seedId}"]`)
async function checkModernGroups(page, expectedPage) {
  assert.match(await page.locator('.results-heading').innerText(), /当前\s*22\s*个源用例/, 'Location must preserve all 22 source groups')
  assert.equal(await groups(page).count(), expectedPage === 1 ? 20 : 2)
  assert.equal(await activePage(page.locator('.group-pagination')), expectedPage, 'Location must retain the target group page')
}
async function locationState(page) {
  return page.evaluate(() => ({
    groupPage: document.querySelector('.group-pagination .el-pager .is-active')?.textContent,
    seedPage: document.querySelector('.seed-pagination .el-pager .is-active')?.textContent,
    sourcePanelExpanded: document.querySelector('.seed-panel-toggle')?.getAttribute('aria-expanded'),
    expanded: [...document.querySelectorAll('.variant-group-toggle[aria-expanded="true"]')].map(element => element.closest('.variant-group').dataset.groupId),
    focusedSources: [...document.querySelectorAll('.seed-table .focused-row')].map(element => element.textContent),
    focusedGroups: [...document.querySelectorAll('.variant-group.focused')].map(element => element.dataset.groupId),
    focusedResults: [...document.querySelectorAll('.result-table .focused-row')].map(element => element.textContent),
    focusedTree: [...document.querySelectorAll('.association-card.focused [data-node-id]')].map(element => element.dataset.nodeId),
  }))
}
async function chooseSourceStatus(page, label) {
  await setSeedPanel(page, true)
  await page.locator('.seed-toolbar .el-select').click()
  await page.locator('.el-select-dropdown:visible').getByText(label, { exact: true }).click()
}

async function checkLocation(browser, base) {
  const { page, state } = await mockPage(browser, base)
  await setSeedPanel(page, true)
  await sourceRows(page).nth(0).getByRole('button', { name: '定位场景与变体', exact: true }).click()
  await checkModernGroups(page, 1)
  assert.equal(await toggle(sourceGroup(page, 'seed-0')).getAttribute('aria-expanded'), 'true')
  assert.match(await page.locator('.filter-context').innerText(), /当前定位/)
  assert.equal(await page.getByRole('button', { name: '清除筛选', exact: true }).count(), 0, 'Location alone must not become a filter')
  await sourceGroup(page, 'seed-1').getByRole('button', { name: '定位场景', exact: true }).click()
  await checkModernGroups(page, 1)
  assert.equal(await toggle(sourceGroup(page, 'seed-0')).getAttribute('aria-expanded'), 'true')
  assert.equal(await toggle(sourceGroup(page, 'seed-1')).getAttribute('aria-expanded'), 'true')
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位源用例', exact: true }).nth(3).click()
  await checkModernGroups(page, 1)
  await sourceRows(page).nth(2).locator('td').nth(1).click()
  await checkModernGroups(page, 1)
  assert.equal(await toggle(sourceGroup(page, 'seed-2')).getAttribute('aria-expanded'), 'true', 'Clicking a source row locates and expands without filtering')
  await bodyRows(sourceGroup(page, 'seed-1')).nth(4).locator('td').nth(2).click()
  await checkModernGroups(page, 1)
  assert.ok((await sourceGroup(page, 'seed-1').getAttribute('class')).split(' ').includes('focused'))
  assert.equal(await sourceGroup(page, 'seed-1').locator('.result-table .focused-row').count(), 1)
  await toggle(sourceGroup(page, 'seed-0')).click()
  await toggle(sourceGroup(page, 'seed-1')).click()
  await page.locator('.results-heading').scrollIntoViewIfNeeded()
  await screenshot(page, 'location-preserves-source-groups')

  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位场景', exact: true }).click()
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位源用例', exact: true }).first().click()
  assert.equal(await sourceGroup(page, 'seed-0').locator('.result-table .focused-row').count(), 1)
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '编辑', exact: true }).first().click()
  await sourceGroup(page, 'seed-0').locator('textarea').fill('取消定位要保留此草稿和所有定位状态')
  const before = await locationState(page), dialog = page.locator('.el-message-box')
  await bodyRows(sourceGroup(page, 'seed-0')).nth(1).locator('td').nth(2).click()
  await dialog.waitFor({ state: 'visible' })
  assert.deepEqual(await locationState(page), before, 'A result row must not move its highlight before unsaved-edit protection resolves')
  await dialog.locator('.el-message-box__headerbtn').click()
  await dialog.waitFor({ state: 'hidden' })
  assert.deepEqual(await locationState(page), before, 'Cancelling result-row location keeps the original controlled highlight')
  await sourceGroup(page, 'seed-4').getByRole('button', { name: '定位场景', exact: true }).click()
  await dialog.waitFor({ state: 'visible' })
  await dialog.locator('.el-message-box__headerbtn').click()
  await dialog.waitFor({ state: 'hidden' })
  assert.deepEqual(await locationState(page), before, 'Cancelling an unsaved-edit prompt must not change focus, paging, or expansion')
  assert.equal(await sourceGroup(page, 'seed-0').locator('textarea').inputValue(), '取消定位要保留此草稿和所有定位状态')
  await sourceGroup(page, 'seed-4').getByRole('button', { name: '定位场景', exact: true }).click()
  await dialog.getByRole('button', { name: '放弃修改', exact: true }).click()
  await eventually(async () => await sourceGroup(page, 'seed-0').locator('textarea').count() === 0, 'Discard finishes before subsequent location')
  await checkModernGroups(page, 1)

  await page.locator('.group-pagination .btn-next').click()
  await checkModernGroups(page, 2)
  await sourceGroup(page, 'seed-20').getByRole('button', { name: '定位场景', exact: true }).click()
  await checkModernGroups(page, 2)
  assert.equal(await activePage(seedPager(page)), 2, 'Locating a parent on page 2 reveals its source row on seed page 2')
  await sourceGroup(page, 'seed-21').getByRole('button', { name: '定位场景', exact: true }).click()
  await checkModernGroups(page, 2)
  await sourceRows(page).nth(0).getByRole('button', { name: '定位场景与变体', exact: true }).click()
  await checkModernGroups(page, 2)
  await sourceRows(page).nth(1).locator('td').nth(1).click()
  await checkModernGroups(page, 2)
  await sourceGroup(page, 'seed-20').getByRole('button', { name: '定位源用例', exact: true }).click()
  await checkModernGroups(page, 2)
  await bodyRows(sourceGroup(page, 'seed-21')).first().locator('td').nth(2).click()
  await checkModernGroups(page, 2)
  assert.equal(await activePage(seedPager(page)), 2)
  assert.equal(await sourceGroup(page, 'seed-21').locator('.result-table .focused-row').count(), 1)
  await page.locator('.results-heading').scrollIntoViewIfNeeded()
  await screenshot(page, 'location-preserves-second-page')
  await page.getByRole('button', { name: '清除定位', exact: true }).click()
  await checkModernGroups(page, 2)
  assert.equal(await activePage(seedPager(page)), 2, 'Clearing location must not reset either pagination')
  assert.equal(await page.locator('.variant-group.focused,.seed-table .focused-row,.result-table .focused-row,.association-card.focused').count(), 0)
  await page.locator('.group-pagination .btn-prev').click()
  await checkModernGroups(page, 1)

  await chooseSourceStatus(page, '未关联')
  assert.equal(await sourceRows(page).count(), 0)
  await checkModernGroups(page, 1)
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位场景', exact: true }).click()
  await checkModernGroups(page, 1)
  assert.match(await page.locator('.seed-toolbar').innerText(), /未关联/)
  await page.locator('.el-message').filter({ hasText: '源用例不在当前筛选范围内，可清除筛选查看' }).first().waitFor({ state: 'visible' })
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位源用例', exact: true }).first().click()
  await checkModernGroups(page, 1)
  assert.match(await page.locator('.seed-toolbar').innerText(), /未关联/)
  assert.equal(await sourceRows(page).count(), 0, 'Locating a hidden source must not clear the explicit status filter')
  await page.locator('[data-node-id="scene-1"]').click()
  assert.match(await page.locator('.results-heading').innerText(), /当前\s*21\s*个源用例/)
  assert.match(await page.locator('.seed-toolbar').innerText(), /未关联/)
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位场景', exact: true }).click()
  await bodyRows(sourceGroup(page, 'seed-0')).nth(1).locator('td').nth(2).click()
  assert.equal(await page.locator('[data-node-id="scene-1"]').getAttribute('aria-pressed'), 'true', 'Row location must preserve explicit scene filters')
  assert.match(await page.locator('.results-heading').innerText(), /当前\s*21\s*个源用例/)
  assert.equal(await sourceRows(page).count(), 0)
  await page.getByRole('button', { name: '清除定位', exact: true }).click()
  assert.equal(await page.locator('[data-node-id="scene-1"]').getAttribute('aria-pressed'), 'true')
  assert.match(await page.locator('.seed-toolbar').innerText(), /未关联/)
  await page.getByRole('button', { name: '清除筛选', exact: true }).click()
  await checkModernGroups(page, 1)

  await chooseJob(page, 'legacy')
  await toggle(groups(page).first()).click()
  await groups(page).first().locator('.variant-pagination .btn-next').click()
  await eventually(async () => await bodyRows(groups(page).first()).count() === 5, 'Legacy child page 2 is ready')
  await bodyRows(groups(page).first()).nth(3).locator('td').nth(2).click()
  assert.equal(await activePage(groups(page).first().locator('.variant-pagination')), 2, 'Locating a child on page 2 must not jump back to child page 1')
  assert.equal(await bodyRows(groups(page).first()).count(), 5)
  assert.equal(await groups(page).count(), 3)

  await chooseJob(page, 'positioning')
  await setSeedPanel(page, true)
  await page.locator('.group-pagination .btn-next').click()
  await sourceGroup(page, 'seed-20').getByRole('button', { name: '定位场景', exact: true }).click()
  assert.equal(await activePage(seedPager(page)), 2)
  const emptySource = sourceRows(page).filter({ hasText: '已分类但没有生成变体的源任务' })
  await emptySource.getByRole('button', { name: '定位场景与变体', exact: true }).click()
  assert.equal(await activePage(page.locator('.group-pagination')), 2, 'A seed without variants must not reset result pagination')
  assert.equal(await groups(page).count(), 3)
  await sourceGroup(page, 'seed-20').getByRole('button', { name: '定位场景', exact: true }).click()
  const historical = page.locator('.variant-group[data-group-id="row:999"]')
  await toggle(historical).click()
  await bodyRows(historical).first().locator('td').nth(2).click()
  assert.equal(await page.locator('.seed-table .focused-row,.association-card.focused').count(), 0, 'A historical result without a seed clears the previous source/tree focus')
  assert.equal(await activePage(page.locator('.group-pagination')), 2)
  assert.equal(await groups(page).count(), 3)
  assert.deepEqual(state.blocked, [])
  assert.deepEqual(state.errors, [])
  assert.equal(state.requests.filter(value => value.method === 'PATCH').length, 0, 'Location alone must not write results')
  await screenshot(page, 'location-with-historical-result')
  await page.close()
  return { sourceGroups: 22, locationEntryPoints: 5, requests: state.requests.length, writes: 0 }
}

async function assertSourcePanelHeight(page) {
  const bounds = await page.locator('.seed-table').boundingBox()
  assert.ok(bounds && bounds.height <= 280.5, `Expanded source table must be at most 280px high: ${JSON.stringify(bounds)}`)
  return bounds.height
}
async function checkSeedPanel(browser, base) {
  const { page, state } = await mockPage(browser, base, 1400)
  const panelToggle = page.locator('.seed-panel-toggle')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false', 'Source list starts collapsed')
  assert.equal(await page.locator('.seed-table').isVisible(), false)
  assert.equal(await page.evaluate(() => {
    const treePanel = document.querySelector('.augmentation-scene-tree')
    const review = document.querySelector('.results-heading')
    const pagination = document.querySelector('.group-pagination')
    const seeds = document.querySelector('.seed-panel')
    return Boolean((treePanel.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING)
      && (review.compareDocumentPosition(pagination) & Node.DOCUMENT_POSITION_FOLLOWING)
      && (pagination.compareDocumentPosition(seeds) & Node.DOCUMENT_POSITION_FOLLOWING))
  }), true, 'Order is scene tree, variant review, variant pagination, collapsed source list')

  await sourceGroup(page, 'seed-19').getByRole('button', { name: '定位场景', exact: true }).click()
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false', 'Parent scene location does not open the source list')
  await bodyRows(sourceGroup(page, 'seed-19')).first().locator('td').nth(2).click()
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false', 'Ordinary variant-row selection does not open the source list')
  await sourceGroup(page, 'seed-19').getByRole('button', { name: '定位源用例', exact: true }).click()
  await eventually(async () => await panelToggle.getAttribute('aria-expanded') === 'true', 'Explicit source location opens the bottom list')
  await checkModernGroups(page, 1)
  assert.equal(await sourceRows(page).count(), 20)
  const sourceHeight = await assertSourcePanelHeight(page)
  const target = page.locator('.seed-table .focused-row')
  try {
    await eventually(async () => {
      const row = await target.boundingBox(), table = await page.locator('.seed-table').boundingBox()
      return row && table && row.y >= Math.max(table.y, 0) - 1 && row.y + row.height <= Math.min(table.y + table.height, 1000) + 1
    }, 'The located source at the bottom of its page must scroll into the bounded table and viewport')
  } catch (error) {
    console.error(JSON.stringify(await page.evaluate(() => ({ window: { scrollY, innerHeight, documentHeight: document.documentElement.scrollHeight }, elements: [...document.querySelectorAll('.seed-panel,.seed-table,.seed-table .el-table__header-wrapper,.seed-table .el-table__body-wrapper,.seed-table .el-scrollbar__wrap,.seed-table .focused-row')].map(element => { const rect = element.getBoundingClientRect(); return { class: element.className, y: rect.y, bottom: rect.bottom, height: rect.height, scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight } }) }))))
    throw error
  }
  assert.match(await target.innerText(), /源失败任务 20/)
  await screenshot(page, 'bottom-source-list-located-1400')
  await setSeedPanel(page, false)
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位场景', exact: true }).click()
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false')
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '编辑', exact: true }).first().click()
  await sourceGroup(page, 'seed-0').locator('textarea').fill('取消明确定位也不能打开底部列表')
  const before = await locationState(page), dialog = page.locator('.el-message-box')
  await sourceGroup(page, 'seed-0').getByRole('button', { name: '定位源用例', exact: true }).nth(1).click()
  await dialog.waitFor({ state: 'visible' })
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false', 'Protection must run before source-panel expansion')
  await dialog.locator('.el-message-box__headerbtn').click()
  await dialog.waitFor({ state: 'hidden' })
  assert.deepEqual(await locationState(page), before)
  await sourceGroup(page, 'seed-4').getByRole('button', { name: '定位场景', exact: true }).click()
  await dialog.getByRole('button', { name: '放弃修改', exact: true }).click()
  await eventually(async () => await sourceGroup(page, 'seed-0').locator('textarea').count() === 0, 'Discard finishes')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false')

  await setSeedPanel(page, true)
  await page.locator('.section-title').getByRole('button', { name: '刷新', exact: true }).click()
  await eventually(async () => await page.locator('.section-title button').isEnabled(), 'Same-job refresh finishes')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'true', 'Same-job refresh retains expanded source list')
  await setSeedPanel(page, false)
  await page.locator('.section-title').getByRole('button', { name: '刷新', exact: true }).click()
  await eventually(async () => await page.locator('.section-title button').isEnabled(), 'Collapsed refresh finishes')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false')
  await setSeedPanel(page, true)
  await chooseJob(page, 'details')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false', 'Changing jobs resets the source list to collapsed')
  await setSeedPanel(page, true)
  const task = sourceRows(page).first().locator('.seed-task')
  assert.equal(await task.evaluate(element => getComputedStyle(element).webkitLineClamp), '2', 'Compact source tasks are limited to two lines')
  const firstDetail = state.data.details.preview.seeds[0]
  await sourceRows(page).first().getByRole('button', { name: '详情', exact: true }).click()
  const details = page.locator('.seed-detail-popover:visible')
  await details.waitFor({ state: 'visible' })
  await settlePopover(details)
  const detailText = await details.innerText()
  assert.ok(detailText.includes(firstDetail.task), 'The popover contains the complete source task including its clipped tail')
  assert.ok(detailText.includes(firstDetail.reason))
  assert.ok(detailText.includes(firstDetail.error))
  assert.match(detailText, /模型分类/)
  assert.match(detailText, /部分完成/)
  assert.match(detailText, /视频场景.*搜索能力.*搜索任务.*测试视频/s)
  await screenshot(page, 'bottom-source-full-details-1400', true)
  await page.locator('.results-heading').click()
  await eventually(async () => await details.count() === 0, 'Previous detail popover finishes closing')
  await sourceRows(page).nth(1).getByRole('button', { name: '详情', exact: true }).click()
  const failedDetails = details.filter({ hasText: state.data.details.preview.seeds[1].task })
  await failedDetails.waitFor({ state: 'visible' })
  await settlePopover(failedDetails)
  assert.match(await failedDetails.innerText(), /分类失败/)
  assert.match(await failedDetails.innerText(), /已跳过/)
  assert.ok((await failedDetails.innerText()).includes(state.data.details.preview.seeds[1].error))
  await page.locator('.results-heading').click()
  await chooseJob(page, 'modern')
  assert.equal(await panelToggle.getAttribute('aria-expanded'), 'false')
  assert.deepEqual(state.blocked, [])
  assert.deepEqual(state.errors, [])
  assert.equal(state.requests.filter(value => value.method === 'PATCH').length, 0)
  await page.close()
  return { sourceHeight, defaultCollapsed: true, explicitLocationOnly: true, completeDetails: true, requests: state.requests.length }
}

async function checkTreeAndSizes(browser, base) {
  const results = []
  for (const width of [1440, 1024, 390]) {
    const { page, state } = await mockPage(browser, base, width)
    const panel = page.locator('.augmentation-scene-tree'), viewport = page.locator('.association-tree-viewport')
    assert.equal(await panel.locator('[data-node-id="scene-unused"]').count(), 0, 'Unrelated root is initially hidden')
    assert.equal(await panel.locator('[data-node-id="cap-unused"]').count(), 0, 'Unrelated descendants are initially hidden')
    assert.equal(await panel.locator('[data-node-id="scene-1"]').count(), 1)
    assert.equal(await panel.locator('[data-node-id="scene-2"]').count(), 1)
    const bounds = await viewport.boundingBox()
    assert.ok(bounds.height <= 300.5, `Tree height ${bounds.height} must be bounded at width ${width}`)
    await panel.scrollIntoViewIfNeeded()
    await screenshot(page, `tree-related-${width}`)
    await panel.getByRole('button', { name: '全部场景', exact: true }).click()
    await panel.locator('[data-node-id="scene-unused"]').waitFor()
    assert.equal(await panel.locator('[data-node-id="cap-unused"]').count(), 1)
    assert.ok((await viewport.boundingBox()).height <= 300.5)
    await screenshot(page, `tree-all-${width}`)
    await panel.locator('[data-node-id="scene-unused"]').click()
    await eventually(async () => await groups(page).count() === 0, 'Unrelated node remains selectable in full snapshot mode')
    await panel.getByRole('button', { name: '仅关联场景', exact: true }).click()
    await eventually(async () => await groups(page).count() === 20, 'Hiding unrelated selected node clears its invisible filter')
    assert.equal(await panel.locator('[data-node-id][aria-pressed="true"]').count(), 0)
    assert.equal(await panel.locator('[data-node-id="scene-unused"]').count(), 0)
    await page.locator('.results-heading').scrollIntoViewIfNeeded()
    await screenshot(page, `variants-collapsed-${width}`)
    await toggle(groups(page).first()).click()
    await groups(page).first().scrollIntoViewIfNeeded()
    // The existing global mobile layout has a 620px minimum; this feature must
    // not expand the document beyond that established site-wide baseline.
    const documentWidth = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, siteMinimum: parseFloat(getComputedStyle(document.body).minWidth) || 0 }))
    const expectedWidth = Math.max(documentWidth.viewport, documentWidth.siteMinimum)
    assert.ok(documentWidth.document <= expectedWidth + 1, `The local tree/table must not widen the existing page at ${width}px: ${JSON.stringify(documentWidth)}`)
    await screenshot(page, `variants-expanded-${width}`)
    assert.equal(await page.locator('.seed-panel-toggle').getAttribute('aria-expanded'), 'false')
    await setSeedPanel(page, true)
    const sourceHeight = await assertSourcePanelHeight(page)
    await page.locator('.seed-panel').scrollIntoViewIfNeeded()
    await screenshot(page, `bottom-source-list-expanded-${width}`)
    await setSeedPanel(page, false)
    await screenshot(page, `bottom-source-list-collapsed-${width}`)
    await chooseJob(page, 'unmatched')
    assert.equal(await page.locator('.seed-panel-toggle').getAttribute('aria-expanded'), 'false')
    assert.equal(await panel.locator('[data-node-id]').count(), 0, 'No match must not light guessed branches')
    assert.match(await panel.innerText(), /暂无|尚无|没有|未关联/)
    await panel.scrollIntoViewIfNeeded()
    await screenshot(page, `tree-unmatched-${width}`)
    await panel.getByRole('button', { name: '全部场景', exact: true }).click()
    await panel.locator('[data-node-id="scene-unused"]').waitFor()
    assert.equal(await panel.locator('.association-card.related').count(), 0)
    await chooseJob(page, 'details')
    await setSeedPanel(page, true)
    const detailButton = sourceRows(page).first().getByRole('button', { name: '详情', exact: true })
    if (width === 390) await detailButton.tap()
    else await detailButton.click()
    const popover = page.locator('.seed-detail-popover:visible')
    await popover.waitFor({ state: 'visible' })
    await settlePopover(popover)
    assert.ok((await popover.innerText()).includes(state.data.details.preview.seeds[0].task))
    const popoverBounds = await popover.boundingBox()
    if (popoverBounds.x < -1 || popoverBounds.x + popoverBounds.width > width + 1) console.error(JSON.stringify(await page.evaluate(() => ({ scrollX, innerWidth, visualViewport: window.visualViewport ? { width: visualViewport.width, offsetLeft: visualViewport.offsetLeft, pageLeft: visualViewport.pageLeft } : null }))))
    assert.ok(popoverBounds.width <= width - 24 + 1 && popoverBounds.x >= -1 && popoverBounds.x + popoverBounds.width <= width + 1,
      `The source detail popover must fit the actual ${width}px viewport: ${JSON.stringify(popoverBounds)}`)
    await screenshot(page, `bottom-source-details-${width}`, true)
    assert.deepEqual(state.blocked, [])
    assert.deepEqual(state.errors, [])
    results.push({ width, treeHeight: bounds.height, sourceHeight, documentWidth: documentWidth.document, siteMinimumWidth: documentWidth.siteMinimum })
    await page.close()
  }
  return results
}

async function main() {
  await fs.access(path.join(dist, 'index.html'))
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, 'http://local').pathname)
      if (pathname.startsWith('/api/')) { response.writeHead(501); response.end('APIs must be mocked'); return }
      let filename = path.resolve(dist, `.${pathname}`)
      if (filename !== dist && !filename.startsWith(`${dist}${path.sep}`)) { response.writeHead(403); response.end(); return }
      if (!path.extname(filename)) filename = path.join(dist, 'index.html')
      const body = await fs.readFile(filename)
      response.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(filename)] || 'application/octet-stream')
      response.end(body)
    } catch { response.writeHead(404); response.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  try {
    const review = await checkReview(browser, base)
    const location = await checkLocation(browser, base)
    const sourcePanel = await checkSeedPanel(browser, base)
    const viewports = await checkTreeAndSizes(browser, base)
    const report = { review, location, sourcePanel, viewports, screenshots: output, realExternalCalls: 0 }
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report))
  } catch (error) {
    for (const context of browser.contexts()) for (const page of context.pages()) await screenshot(page, 'failure').catch(() => {})
    throw error
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
