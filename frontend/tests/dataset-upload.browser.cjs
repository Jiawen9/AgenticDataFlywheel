// Production UI with isolated HTTP fixtures. Never contacts an internal website.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')

const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(__dirname, '../../backend_workspace/internal-upload-review')
const clone = value => JSON.parse(JSON.stringify(value))
const release = {
  release_id: 'rel_browser_fixture', name: '轨迹发布验收数据集', created_at: '2026-09-10T09:00:00+08:00',
  excel_paths: [0, 1].map(index => ({ path: 'fixture/' + index + '.xlsx', filename: '轨迹表格' + (index + 1) + '.xlsx', sha256: 'a'.repeat(64), rows: 10, available: true })),
  trajectory_paths: ['fixture/missing-raw-directory'], source_count: 2, task_count: 2, trajectory_count: 2, step_count: 20,
  upload_status: 'succeeded', upload_job_id: 'mock-old', upload_error: null,
  s3_uri: 's3://mock-only/fixture', uploaded_at: null, uploaded_files: 30, uploaded_bytes: 100, local_available: false,
}
function makeJob(status = 'uploading', completed = 1, id = 'job-first') {
  return {
    job_id: id, release_id: release.release_id, mode: 'internal', status, stage: status,
    created_at: '2026-09-10T10:00:00+08:00', started_at: '2026-09-10T10:00:00+08:00', completed_at: status === 'uploading' ? null : '2026-09-10T10:00:10+08:00',
    current_file: status === 'succeeded' ? null : '轨迹表格2.xlsx', completed_files: completed, total_files: 2, completed_bytes: completed * 100, total_bytes: 200, percent: completed * 50,
    error: status === 'failed' ? '测试网站拒绝接收第二份表格' : null, s3_uri: null,
    file_results: [0, 1].map(index => ({
      index, filename: '轨迹表格' + (index + 1) + '.xlsx', sha256: 'a'.repeat(64), idempotency_key: 'fixture:' + index,
      status: index < completed ? 'succeeded' : status === 'failed' ? 'failed' : 'uploading',
      remote_id: index < completed ? 'record-' + index : null,
      url: index < completed ? 'https://internal.example.test/records/' + index : null,
      error: index >= completed && status === 'failed' ? '测试网站拒绝接收第二份表格' : null,
    })),
  }
}
async function main() {
  await fs.mkdir(output, { recursive: true })
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
      let filename = path.resolve(dist, '.' + pathname)
      if (!filename.startsWith(dist + path.sep)) filename = path.join(dist, 'index.html')
      if (!path.extname(filename)) filename = path.join(dist, 'index.html')
      const body = await fs.readFile(filename)
      res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(filename)] || 'application/octet-stream')
      res.end(body)
    } catch { res.statusCode = 404; res.end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, serviceWorkers: 'block' })
    const errors = []
    const requests = []
    page.on('pageerror', error => errors.push(error.message))
    let configured = false
    let current = null
    let posts = 0
    let postGate = null
    let rejectPost = false
    let pollFailure = false
    await page.route('**/*', async route => {
      const req = route.request()
      if (!req.url().startsWith(base + '/')) return route.abort()
      const url = new URL(req.url()).pathname
      if (!url.startsWith('/api/')) return route.continue()
      requests.push({ url, method: req.method(), body: req.postData() })
      // An older backend response must render the new name without mutation.
      if (url === '/api/dataset-upload-capabilities') return route.fulfill({ json: { internal: { configured, reason: configured ? null : '内部网站上传尚未配置' } } })
      if (url === '/api/dataset-releases/candidates') return route.fulfill({ json: { candidates: [] } })
      if (url === '/api/dataset-releases') return route.fulfill({ json: { releases: [{ ...clone(release), ...(current ? { internal_upload: clone(current) } : {}) }] } })
      if (url.endsWith('/upload')) {
        posts++
        assert.deepEqual(req.postDataJSON(), { target: 'internal' })
        if (postGate) await postGate
        if (rejectPost) return route.fulfill({ status: 409, json: { detail: '发布文件 SHA256 不一致' } })
        current = makeJob('uploading', 1, 'job-' + posts)
        return route.fulfill({ status: 202, json: { job: clone(current) } })
      }
      if (url.startsWith('/api/dataset-upload-jobs/')) {
        if (pollFailure) { pollFailure = false; return route.fulfill({ status: 503, json: { detail: '临时网络错误' } }) }
        return route.fulfill({ json: { job: clone(current) } })
      }
      if (url === '/api/dataset-releases/' + release.release_id) return route.fulfill({ json: { release: { ...clone(release), ...(current ? { internal_upload: clone(current) } : {}) } } })
      return route.fulfill({ status: 501, json: { detail: 'Unexpected API blocked' } })
    })
    const go = () => page.goto(base + '/data-publishing/archive')
    const count = () => page.locator('.metric').nth(2).locator('b').innerText()
    await go()
    await page.getByText('云道S3上传尚未配置', { exact: true }).waitFor()
    const button = () => page.getByRole('button', { name: '云道S3上传', exact: true })
    assert.equal(await button().isDisabled(), true)
    assert.equal(await count(), '0', 'Mock successes must not count as real uploads')
    await page.getByRole('button', { name: '详情', exact: true }).click()
    await page.getByText('历史模拟上传', { exact: true }).waitFor()
    await page.getByText('模拟 S3 地址', { exact: true }).waitFor()
    await page.keyboard.press('Escape')
    await page.locator('.el-dialog').waitFor({ state: 'hidden' })
    await page.screenshot({ path: path.join(output, 'unconfigured.png'), fullPage: true })

    configured = true
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent.includes('云道S3上传') && !button.disabled))
    let releasePost
    postGate = new Promise(resolve => { releasePost = resolve })
    await button().click()
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent.includes('云道S3上传') && button.disabled))
    await button().evaluate(element => { element.click(); element.click() })
    assert.equal(posts, 1)
    releasePost()
    postGate = null
    await page.getByText('已确认 1/2 个 Excel', { exact: true }).waitFor()
    await page.getByText('远端记录编号：record-0', { exact: true }).waitFor()
    assert.equal(requests.filter(item => item.url.includes('/excels/')).length, 0)
    await page.screenshot({ path: path.join(output, 'uploading.png'), fullPage: true })

    // Refresh recovers from the release summary even without browser storage.
    await page.evaluate(() => localStorage.clear())
    await page.reload()
    await page.getByText('已确认 1/2 个 Excel', { exact: true }).waitFor()
    assert.equal(posts, 1)
    pollFailure = true
    await page.getByText('读取上传进度失败：临时网络错误', { exact: true }).waitFor()
    await page.waitForFunction(() => !document.body.textContent.includes('读取上传进度失败'), { timeout: 10000 })
    current = makeJob('failed', 1, current.job_id)
    current.error = '内部网站上传异常，请联系维护人员后重试'
    current.file_results[1].error = '内部网站拒绝接收：请调整表格字段'
    current.file_results[0].filename = '内部网站上传尚未配置.xlsx'
    current.file_results[0].remote_id = '内部网站记录-0'
    await page.getByRole('button', { name: '重试云道S3上传', exact: true }).waitFor()
    await page.locator('.upload-progress .el-alert').getByText('云道S3上传异常，请联系维护人员后重试', { exact: true }).waitFor()
    await page.getByText('内部网站拒绝接收：请调整表格字段', { exact: true }).waitFor()
    await page.getByText('内部网站上传尚未配置.xlsx', { exact: true }).waitFor()
    await page.getByText('远端记录编号：内部网站记录-0', { exact: true }).waitFor()
    assert.equal(await page.getByRole('link', { name: '查看云道S3记录' }).getAttribute('href'), 'https://internal.example.test/records/0')
    assert.equal(current.error, '内部网站上传异常，请联系维护人员后重试')
    await page.getByText('重试将跳过已确认成功且校验值一致的文件，继续上传剩余表格。', { exact: true }).waitFor()
    assert.equal(await count(), '0')
    await page.screenshot({ path: path.join(output, 'failed.png'), fullPage: true })
    await page.getByRole('button', { name: '重试云道S3上传', exact: true }).click()
    await page.getByText('已确认 1/2 个 Excel', { exact: true }).waitFor()
    assert.equal(posts, 2)
    current = makeJob('succeeded', 2, current.job_id)
    await page.getByRole('button', { name: '已上传云道S3', exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '已上传云道S3', exact: true }).isDisabled(), true)
    assert.equal(await count(), '1')
    assert.equal(await page.getByRole('link', { name: '查看云道S3记录' }).count(), 2)
    await page.screenshot({ path: path.join(output, 'succeeded.png'), fullPage: true })

    await page.reload()
    await page.getByRole('button', { name: '详情', exact: true }).click()
    await page.getByText('云道S3上传结果 · 2/2 个 Excel', { exact: true }).waitFor()
    assert.equal(posts, 2)
    await page.keyboard.press('Escape')
    await page.locator('.el-dialog').waitFor({ state: 'hidden' })

    // A 409 must surface the reason and must not create a progress success.
    current = null
    rejectPost = true
    await page.reload()
    await button().click()
    await page.getByText('发布文件 SHA256 不一致', { exact: true }).waitFor()
    assert.equal(await page.locator('.upload-progress').count(), 0)
    assert.equal(await count(), '0')
    assert.equal(await button().isDisabled(), false)
    for (const width of [1024, 390]) {
      await page.setViewportSize({ width, height: 950 })
      const contentWidth = await page.evaluate(() => document.documentElement.scrollWidth)
      assert.ok(contentWidth <= Math.max(width, 620), 'Keep wide Excel history inside its scrollable table')
      await page.screenshot({ path: path.join(output, 'width-' + width + '.png'), fullPage: true })
    }
    assert.equal(requests.filter(item => item.url.includes('/excels/')).length, 0, 'Upload never downloads a workbook through the browser')
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, posts, workbookRequests: 0, screenshots: output }))
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
