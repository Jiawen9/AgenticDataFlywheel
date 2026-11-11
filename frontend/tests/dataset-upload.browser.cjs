// Real production UI with isolated fixtures. All non-local requests are blocked.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(process.env.ADF_BROWSER_ARTIFACTS || path.join(os.tmpdir(), 'adf-yundao-upload-review'))
const clone = value => JSON.parse(JSON.stringify(value))
const release = {
  release_id: 'rel_browser_fixture', name: '轨迹发布验收数据集', created_at: '2026-11-10T09:00:00+08:00', batch_ids: ['batch_001'],
  excel_paths: [{ path: 'releases/fixture/001/数据表.xlsx', filename: '数据表.xlsx', sha256: 'a'.repeat(64), rows: 220, available: true }],
  trajectory_paths: ['fixture/shared-root-never-uploaded'], source_count: 1, task_count: 11, trajectory_count: 22, step_count: 220,
  upload_status: 'succeeded', upload_job_id: 'mock-old', upload_error: null,
  s3_uri: 's3://mock-only/fixture', uploaded_at: null, uploaded_files: 30, uploaded_bytes: 100, local_available: true,
}
function makeJob(status, completed, id, prefix = 'data/验收目录') {
  return {
    job_id: id, release_id: release.release_id, mode: 'internal', status, stage: status, folder_upload: true, include_trajectories: true,
    destination: { prefix, bucket: 'fixture-bucket', region: 'fixture-region' },
    source_summary: { case_count: 11, trajectory_count: 22, excel_count: 1 },
    created_at: '2026-11-10T10:00:00+08:00', started_at: '2026-11-10T10:00:00+08:00', completed_at: status === 'uploading' ? null : '2026-11-10T10:00:10+08:00',
    current_item: status === 'succeeded' ? null : '用例01 / 轨迹02', completed_items: completed, total_items: 23,
    current_file: null, completed_files: 0, total_files: 0, completed_bytes: 0, total_bytes: 0, percent: 0,
    progress: { speed_text: '12.5 MiB/s', message: '正在上传完整目录（脚本速度输出）' },
    recent_output: ['正在上传完整目录', '当前传输速度 12.5 MiB/s'],
    error: status === 'failed' ? 'fixture 轨迹目录传输失败，请重试' : null,
    item_results: Array.from({ length: 23 }, (_, index) => ({
      kind: index ? 'trajectory' : 'excel', filename: index ? `轨迹${String(index).padStart(2, '0')}` : '数据表.xlsx',
      idempotency_key: `fixture:${index}`, source_dir: `fixture/${index}`,
      bucket_path: `${prefix}/batch_001/${index ? `用例01/轨迹${String(index).padStart(2, '0')}/` : '发布表格/001/'}`,
      status: index < completed ? 'succeeded' : status === 'failed' && index === completed ? 'failed' : 'pending',
      error: index === completed && status === 'failed' ? 'fixture 目录读取失败' : null,
    })),
  }
}
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
  let browser
  try {
    browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, serviceWorkers: 'block' })
    page.setDefaultTimeout(15000)
    const errors = [], requests = [], posts = [], unexpected = [], layouts = []
    page.on('pageerror', error => errors.push(error.message))
    let configured = false, current = null, pollFailure = false
    await page.route('**/*', async route => {
      const req = route.request()
      if (!req.url().startsWith(base + '/')) return route.abort()
      const url = new URL(req.url()).pathname
      if (!url.startsWith('/api/')) return route.continue()
      requests.push({ url, method: req.method() })
      if (url === '/api/pipelines') return route.fulfill({ json: { pipelines: [] } })
      if (url === '/api/display-identifiers/resolve') return route.fulfill({ json: { items: req.postDataJSON().items.map(item => ({ ...item, mapped: false, display_id: item.id })) } })
      if (url === '/api/training-data-overview') return route.fulfill({ json: { conversions: [], workbook_url: null } })
      if (url === '/api/dataset-upload-capabilities') return route.fulfill({ json: { internal: { configured, reason: configured ? null : '内部网站上传尚未配置', directory_upload: true, default_prefix: 'data/默认/', bucket: 'fixture-bucket', region: 'fixture-region' } } })
      if (url === '/api/dataset-releases/candidates') return route.fulfill({ json: { candidates: [] } })
      if (url === '/api/dataset-releases') return route.fulfill({ json: { releases: [{ ...clone(release), ...(current ? { internal_upload: clone(current) } : {}) }] } })
      if (url === `/api/dataset-releases/${release.release_id}/upload`) {
        posts.push(req.postDataJSON())
        const prefix = posts.at(-1).destination_prefix.replace(/^\/+|\/+$/g, '')
        current = makeJob('uploading', 1, 'job-' + posts.length, prefix)
        return route.fulfill({ status: 202, json: { job: clone(current) } })
      }
      if (url.startsWith('/api/dataset-upload-jobs/')) {
        if (pollFailure) { pollFailure = false; return route.fulfill({ status: 503, json: { detail: '临时网络错误' } }) }
        return route.fulfill({ json: { job: clone(current) } })
      }
      if (url === '/api/dataset-releases/' + release.release_id) return route.fulfill({ json: { release: { ...clone(release), ...(current ? { internal_upload: clone(current) } : {}) } } })
      unexpected.push(url)
      return route.fulfill({ status: 501, json: { detail: 'Unexpected API blocked' } })
    })
    const count = () => page.locator('.metric').nth(2).locator('b').innerText()
    const uploadButton = name => page.getByRole('button', { name, exact: true })
    const confirm = () => page.getByTestId('confirm-yundao-upload')
    const scope = () => page.locator('.upload-progress .folder-progress')
    await page.goto(base + '/data-publishing/archive')
    await page.getByText('云道S3上传尚未配置', { exact: true }).waitFor()
    assert.equal(await uploadButton('云道S3上传').isDisabled(), true)
    assert.equal(await count(), '0', 'Mock historical successes are not real uploads')
    await page.screenshot({ path: path.join(output, 'unconfigured.png'), fullPage: true })
    configured = true
    await uploadButton('刷新').click()
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent.trim() === '云道S3上传' && !button.disabled))
    await uploadButton('云道S3上传').click()
    assert.equal(posts.length, 0, 'Opening target dialog does not upload')
    assert.equal(await page.getByTestId('upload-destination-prefix').inputValue(), 'data/默认/')
    await page.getByTestId('upload-destination-prefix').fill('data/验收目录/')
    await page.getByRole('dialog', { name: '上传到云道S3' }).waitFor()
    await page.waitForTimeout(350)
    await page.screenshot({ path: path.join(output, 'destination-desktop.png'), fullPage: true, animations: 'disabled' })
    await confirm().click()
    await scope().getByText('当前上传速度：12.5 MiB/s', { exact: true }).waitFor()
    assert.deepEqual(posts[0], { target: 'internal', include_trajectories: true, destination_prefix: 'data/验收目录/' })
    await page.getByRole('dialog', { name: '上传到云道S3' }).waitFor({ state: 'hidden' })
    assert.equal(await scope().getByText(/当前脚本字节进度/).count(), 0, 'Speed alone is not byte percentage')
    assert.equal(await scope().locator('li').count(), 0, 'Receipts initially collapsed')
    await scope().getByRole('button', { name: '查看目录上传结果（23）' }).click()
    assert.equal(await scope().locator('li').count(), 10)
    await scope().getByRole('button', { name: '下一页' }).click()
    await scope().getByText('2 / 3 · 每页 10 个目录', { exact: true }).waitFor()
    const pollCount = requests.filter(item => item.url.includes('/dataset-upload-jobs/')).length
    await page.waitForTimeout(1000)
    assert.ok(requests.filter(item => item.url.includes('/dataset-upload-jobs/')).length > pollCount, 'Polling continues after dialog closed')
    await scope().getByText('2 / 3 · 每页 10 个目录', { exact: true }).waitFor()
    await page.screenshot({ path: path.join(output, 'uploading-desktop.png'), fullPage: true })
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.waitForTimeout(100)
      await page.screenshot({ path: path.join(output, `uploading-${width}.png`), fullPage: true })
      const layout = await page.evaluate(() => ({ viewport: innerWidth, pageWidth: document.documentElement.scrollWidth, inheritedBodyMinWidth: parseFloat(getComputedStyle(document.body).minWidth), uploadWidth: document.querySelector('.upload-progress').getBoundingClientRect().width }))
      layouts.push(layout)
      // Existing global styles enforce a 620px minimum page. Check this feature
      // adds no overflow beyond it; record the limitation rather than hiding it.
      assert.ok(layout.pageWidth <= Math.max(width, layout.inheritedBodyMinWidth) + 1, `No added overflow at ${width}px`)
      assert.ok(layout.uploadWidth <= layout.pageWidth, 'Upload content stays inside its existing page container')
    }
    await page.evaluate(() => localStorage.clear())
    await page.reload()
    await scope().getByText('当前上传速度：12.5 MiB/s', { exact: true }).waitFor()
    assert.equal(posts.length, 1, 'Refresh resumes polling without duplicate upload')
    pollFailure = true
    await page.getByText('读取上传进度失败：临时网络错误', { exact: true }).waitFor()
    assert.equal(await scope().count(), 1, 'Polling error preserves prior progress')
    await page.getByText('读取上传进度失败：临时网络错误', { exact: true }).waitFor({ state: 'hidden' })
    current = makeJob('failed', 1, current.job_id)
    await uploadButton('重试云道S3上传').waitFor()
    await uploadButton('重试云道S3上传').click()
    assert.equal(await page.getByTestId('upload-destination-prefix').inputValue(), 'data/验收目录')
    await confirm().click()
    await page.waitForFunction(() => !document.querySelector('[data-testid="confirm-yundao-upload"]'))
    assert.deepEqual(posts[1], { target: 'internal', include_trajectories: true, destination_prefix: 'data/验收目录', retry_job_id: 'job-1' })
    current = makeJob('succeeded', 23, current.job_id)
    await uploadButton('云道S3上传／更换目标').waitFor()
    assert.equal(await count(), '1')
    await uploadButton('云道S3上传／更换目标').click()
    await page.getByTestId('upload-destination-prefix').fill('data/另一个目标/')
    await page.getByRole('dialog', { name: '上传到云道S3' }).waitFor()
    await page.waitForTimeout(350)
    const dialogBox = await page.getByRole('dialog', { name: '上传到云道S3' }).boundingBox()
    assert.ok(dialogBox.x >= 0 && dialogBox.x + dialogBox.width <= 391, 'Target dialog fits the 390px viewport')
    await page.screenshot({ path: path.join(output, 'destination-390.png'), fullPage: true, animations: 'disabled' })
    await confirm().click()
    await page.waitForFunction(() => !document.querySelector('[data-testid="confirm-yundao-upload"]'))
    assert.deepEqual(posts[2], { target: 'internal', include_trajectories: true, destination_prefix: 'data/另一个目标/' })
    assert.equal(requests.filter(item => item.url.includes('/excels/')).length, 0, 'No workbook downloads through browser')
    assert.deepEqual(unexpected, [])
    assert.deepEqual(errors, [])
    const result = { passed: true, posts, workbookRequests: 0, pageErrors: errors, screenshots: output, layouts,
      limitations: ['Existing global styles enforce body min-width:620px at narrow viewports; this feature adds no extra page overflow.'] }
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2))
    console.log(JSON.stringify(result))
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
