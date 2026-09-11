// Serves only the built frontend on an ephemeral port; all API calls are intercepted.
// Build before each comparison; capture before editing with `node tests/scenario-home.browser.cjs baseline`,
// then run `node tests/scenario-home.browser.cjs final`. Output goes to ignored backend_workspace/.
// NODE_PATH may point to an existing Playwright installation; no model calls are made.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')

let base
const phase = process.argv[2] || 'final'
const output = path.resolve(__dirname, '../../backend_workspace/scenario-editorial-review')
const dist = path.resolve(__dirname, '../dist')

async function reviewPage(browser, options = {}) {
  const page = await browser.newPage({ serviceWorkers: 'block', ...options })
  await page.route('**/api/**', route => route.fulfill({ status: 501, json: { detail: 'Unmocked API blocked by read-only review' } }))
  return page
}

async function shot(page, name) {
  await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true, animations: 'disabled' })
}

async function settled(page) {
  await page.waitForFunction(() => !document.querySelector('.studio-mode-enter-active, .studio-mode-leave-active'))
}

const THEME = {
  ink: 'rgb(15, 23, 42)',
  muted: 'rgb(100, 116, 139)',
  line: 'rgb(220, 229, 235)',
  accent: 'rgb(20, 184, 166)',
  accentDeep: 'rgb(15, 118, 110)',
  surface: 'rgb(255, 255, 255)',
  accentSoft: 'rgb(240, 253, 250)',
  transparent: 'rgba(0, 0, 0, 0)',
}

async function assertHomeBackground(page) {
  const actual = await page.evaluate(() => ({
    bodyColor: getComputedStyle(document.body).backgroundColor,
    bodyImage: getComputedStyle(document.body).backgroundImage,
    layers: [
      '.scenario-studio-page', '.studio-shell', '.studio-shell-header',
      '.tree-home', '.home-intro', '.home-metrics', '.tree-frame', '.home-values',
    ].map(selector => {
      const style = getComputedStyle(document.querySelector(selector))
      return { selector, color: style.backgroundColor, image: style.backgroundImage }
    }),
  }))
  assert.equal(actual.bodyColor, 'rgb(238, 243, 246)', 'The overview must reveal the site background')
  assert.match(actual.bodyImage, /^radial-gradient\(/, 'Keep the existing site gradient')
  for (const layer of actual.layers) {
    assert.equal(layer.color, THEME.transparent, `${layer.selector} must not cover the site background`)
    assert.equal(layer.image, 'none', `${layer.selector} must not add another gradient`)
  }
}

async function assertHomeTheme(page) {
  await assertHomeBackground(page)
  const actual = await page.evaluate(() => {
    const style = selector => getComputedStyle(document.querySelector(selector))
    return {
      title: style('.studio-shell-title h1').color,
      eyebrow: style('.studio-shell-title .eyebrow').color,
      frameBackground: style('.tree-frame').backgroundColor,
      frameBorder: style('.tree-frame').borderTopColor,
      nodeFill: style('.tree-node-base').fill,
      nodeStroke: style('.tree-node-base').stroke,
      branchStroke: style('.tree-branch').stroke,
      nodeIcon: style('.node-icon').color,
      rootFill: style('.tree-root > rect').fill,
      searchBackground: style('.studio-search .el-input__wrapper').backgroundColor,
    }
  })
  assert.deepEqual(actual, {
    title: THEME.ink, eyebrow: THEME.accentDeep, frameBackground: THEME.transparent, frameBorder: THEME.line,
    nodeFill: THEME.surface, nodeStroke: THEME.line, branchStroke: THEME.line, nodeIcon: THEME.accentDeep,
    rootFill: THEME.surface, searchBackground: THEME.surface,
  })
}

async function assertActiveTheme(page) {
  const actual = await page.evaluate(() => ({
    nodeFill: getComputedStyle(document.querySelector('.capability-tree-node.is-active .tree-node-base')).fill,
    nodeStroke: getComputedStyle(document.querySelector('.capability-tree-node.is-active .tree-node-base')).stroke,
    branchStroke: getComputedStyle(document.querySelector('.tree-branch.is-active')).stroke,
  }))
  assert.deepEqual(actual, { nodeFill: THEME.accentSoft, nodeStroke: THEME.accent, branchStroke: THEME.accent })
}

async function assertEditorTheme(page) {
  const actual = await page.evaluate(() => ({
    title: getComputedStyle(document.querySelector('.studio-shell-title h1')).color,
    shellBackground: getComputedStyle(document.querySelector('.studio-shell')).backgroundColor,
    editorBackground: getComputedStyle(document.querySelector('.scenario-editor')).backgroundColor,
    editorBorder: getComputedStyle(document.querySelector('.scenario-editor')).borderTopColor,
    browserBackground: getComputedStyle(document.querySelector('.column-browser')).backgroundColor,
    activeTab: getComputedStyle(document.querySelector('.scene-tab.active')).borderBottomColor,
  }))
  assert.deepEqual(actual, {
    title: THEME.ink, shellBackground: THEME.surface, editorBackground: THEME.surface, editorBorder: THEME.line,
    browserBackground: 'rgb(248, 250, 252)', activeTab: THEME.accent,
  })
}

async function assertSelectedEditorItemTheme(page) {
  const actual = await page.locator('.column-item.selected').first().evaluate(element => {
    const style = getComputedStyle(element)
    return { border: style.borderTopColor, background: style.backgroundColor }
  })
  assert.deepEqual(actual, { border: THEME.accent, background: THEME.accentSoft })
}

async function assertCanvas(page, count) {
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.capability-tree')
    const svg = document.querySelector('.tree-connections')
    return canvas && svg && Math.abs(canvas.clientWidth - svg.viewBox.baseVal.width) < 1
  })
  assert.equal(await page.locator('.capability-tree-node').count(), count)
  const dimensions = await page.evaluate(() => ({ width: window.innerWidth, content: document.documentElement.scrollWidth }))
  assert.ok(dimensions.content <= dimensions.width, `Horizontal overflow: ${JSON.stringify(dimensions)}`)
  // Sample one layout frame, so scroll anchoring after loading cannot mix coordinates.
  const { bounds, boxes } = await page.evaluate(() => ({
    bounds: document.querySelector('.capability-tree').getBoundingClientRect().toJSON(),
    boxes: [...document.querySelectorAll('.capability-tree-node')].map(node => node.getBoundingClientRect().toJSON()),
  }))
  for (const box of boxes) {
    assert.ok(box.left >= bounds.left - 1 && box.right <= bounds.right + 1, `Node must stay inside canvas horizontally: ${JSON.stringify({ box, bounds })}`)
    assert.ok(box.top >= bounds.top - 1 && box.bottom <= bounds.bottom + 1, `Node must stay inside canvas vertically: ${JSON.stringify({ box, bounds })}`)
  }
  boxes.forEach((box, i) => {
    for (const other of boxes.slice(i + 1)) assert.ok(box.right <= other.left + 1 || box.left >= other.right - 1 || box.bottom <= other.top + 1 || box.top >= other.bottom - 1, 'Expanded card must not overlap another node')
  })
}

async function assertIdle(page) {
  await page.mouse.move(0, 0)
  try {
    await page.waitForFunction(() => !document.querySelector('.capability-tree-node.is-active'), null, { timeout: 3000 })
  } catch (error) {
    console.log('hover diagnostic', await page.evaluate(() => ({
      active: [...document.querySelectorAll('.capability-tree-node.is-active')].map(e => ({ label: e.getAttribute('aria-label'), hover: e.matches(':hover'), focus: e.matches(':focus'), rect: e.getBoundingClientRect().toJSON() })),
      hover: [...document.querySelectorAll(':hover')].map(e => e.className?.baseVal || e.className),
    })))
    throw error
  }
  assert.equal(await page.locator('.tree-branch.is-active, .capability-tree-node.is-dimmed').count(), 0)
}

async function checkFinal(browser, page, snapshot) {
  const scenes = snapshot.scenes
  const back = async () => {
    await page.locator('.studio-context button').click()
    await page.locator('.capability-tree-node').first().waitFor()
    await settled(page)
    await assertIdle(page)
    await assertHomeBackground(page)
  }
  await back()
  await assertCanvas(page, scenes.length)
  await assertHomeTheme(page)
  assert.equal(await page.locator('.metric-item dd').first().innerText(), String(scenes.length))
  assert.ok(await page.locator('.metric-item dd').first().evaluate(el => parseFloat(getComputedStyle(el).fontSize) <= 32))
  const defaultBox = await page.locator('.capability-tree-node').first().boundingBox()
  await page.locator('.capability-tree-node').first().hover()
  await page.waitForFunction(() => document.querySelector('.capability-tree-node.is-active'))
  assert.equal(await page.locator('.capability-tree-node.is-active').count(), 1)
  await assertActiveTheme(page)
  assert.ok(await page.locator('.is-active .detail-item').count() <= 3)
  assert.equal(await page.locator('.tree-node-preview').count(), 0)
  const expandedBox = await page.locator('.capability-tree-node').first().boundingBox()
  assert.ok(expandedBox.height > defaultBox.height)
  await assertCanvas(page, scenes.length)
  await shot(page, 'final-hover')
  await assertIdle(page)
  await page.locator('.capability-tree-node').last().hover()
  await assertCanvas(page, scenes.length)
  await assertIdle(page)

  await page.keyboard.press('Tab')
  await page.locator('.capability-tree-node').last().focus()
  assert.equal(await page.locator('.capability-tree-node.is-active').count(), 1)
  await page.keyboard.press('Enter')
  await page.locator('.column-browser').waitFor()
  await settled(page)
  assert.equal(await page.locator('.scene-tab.active').innerText(), scenes.at(-1).label)
  assert.equal(new URL(page.url()).search, '', 'Internal switching must not change URL')
  await back()
  await page.locator('.home-primary').click()
  await page.locator('.column-browser').waitFor()
  await settled(page)
  assert.equal(await page.locator('.scene-tab.active').innerText(), scenes[0].label)
  await back()

  const capability = scenes[0].children[0]
  await page.locator('.studio-search input').fill(capability.label)
  await page.locator('.search-result').filter({ hasText: capability.label }).first().click()
  await page.locator('.column-browser').waitFor()
  await settled(page)
  assert.equal(await page.locator('.column-item.selected .item-label').first().innerText(), capability.label)
  await assertSelectedEditorItemTheme(page)
  await back()

  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await assertCanvas(page, scenes.length)
    await assertHomeTheme(page)
    await shot(page, `final-${width}`)
    await page.locator('.capability-tree-node').first().hover()
    await page.waitForFunction(() => document.querySelector('.capability-tree-node.is-active'))
    await assertActiveTheme(page)
    await assertCanvas(page, scenes.length)
    await shot(page, `final-hover-${width}`)
    await assertIdle(page)
    await page.locator('.home-primary').click()
    await page.locator('.column-browser').waitFor()
    await settled(page)
    await assertEditorTheme(page)
    await shot(page, `final-editor-${width}`)
    await back()
  }

  const touch = await reviewPage(browser, { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, reducedMotion: 'reduce' })
  await touch.route('**/api/task-generation/tree', route => route.fulfill({ json: snapshot }))
  await touch.goto(`${base}/scenario-studio`)
  await touch.locator('.capability-tree-node').first().waitFor()
  await assertCanvas(touch, scenes.length)
  await touch.locator('.capability-tree-node').first().tap()
  await shot(touch, 'final-touch-after-tap')
  await touch.locator('.column-browser').waitFor()
  await touch.close()

  // Synthetic edge cases stay in this browser only and never reach the knowledge base.
  const variants = [
    { name: 'single', scenes: [scenes[0]] },
    { name: 'many-long', scenes: Array.from({ length: 18 }, (_, i) => ({ ...scenes[i % scenes.length], id: `test-${i}`, label: `长名称场景验证-${i}-用于检查两行换行和节点边界的场景标题` })) },
    { name: 'no-capabilities', scenes: [{ id: 'empty-scene', kind: 'scene', label: '空能力场景', children: [] }] },
    { name: 'empty', scenes: [] },
  ]
  for (const variant of variants) {
    const test = await reviewPage(browser, { viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
    await test.route('**/api/task-generation/tree', route => route.fulfill({ json: { ...snapshot, scenes: variant.scenes } }))
    await test.goto(`${base}/scenario-studio`)
    if (variant.scenes.length) {
      await test.locator('.capability-tree-node').first().waitFor()
      await assertCanvas(test, variant.scenes.length)
      await test.locator('.capability-tree-node').first().hover()
      await assertCanvas(test, variant.scenes.length)
      if (variant.name === 'no-capabilities') assert.equal(await test.locator('.detail-empty').innerText(), '暂无能力')
      const contentFits = await test.locator('.is-active .node-content').evaluate(element => element.scrollHeight <= element.clientHeight)
      assert.ok(contentFits, 'Expanded content must not be clipped')
    } else {
      await test.getByText('能力树尚未建立').waitFor()
    }
    await shot(test, `final-${variant.name}`)
    if (!variant.scenes.length) {
      for (const width of [1440, 1024, 390]) {
        await test.setViewportSize({ width, height: 900 })
        await assertHomeBackground(test)
        await shot(test, `final-empty-${width}`)
      }
      await test.locator('.tree-state button').click()
      await test.locator('.column-browser').waitFor()
    }
    await test.close()
  }

  const failed = await reviewPage(browser, { viewport: { width: 390, height: 844 } })
  let attempts = 0
  await failed.route('**/api/task-generation/tree', route => {
    attempts++
    return route.fulfill(attempts === 1 ? { status: 503, json: { detail: '只读验收：模拟加载失败' } } : { json: snapshot })
  })
  await failed.goto(`${base}/scenario-studio`)
  await failed.locator('.tree-state--error').waitFor()
  assert.equal(await failed.locator('.tree-state--error strong').evaluate(element => getComputedStyle(element).color), 'rgb(180, 83, 60)')
  await shot(failed, 'final-error')
  for (const width of [1440, 1024, 390]) {
    await failed.setViewportSize({ width, height: 900 })
    await assertHomeBackground(failed)
    await shot(failed, `final-error-${width}`)
  }
  await failed.locator('.tree-state--error button').click()
  await failed.locator('.capability-tree-node').first().waitFor()
  assert.equal(attempts, 2)
  await assertCanvas(failed, scenes.length)
  await assertHomeTheme(failed)
  await failed.close()

  // Real animations: rapidly switch hover targets, then resize to a touch-width layout.
  const animated = await reviewPage(browser, { viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference' })
  await animated.route('**/api/task-generation/tree', route => route.fulfill({ json: snapshot }))
  await animated.goto(`${base}/scenario-studio`)
  await animated.locator('.capability-tree-node').first().waitFor()
  for (const i of [0, 2, scenes.length - 1, 1, 0]) {
    const node = animated.locator('.capability-tree-node').nth(i)
    await node.hover()
    await animated.waitForTimeout(240)
    assert.equal(await animated.locator('.capability-tree-node.is-active').getAttribute('data-scene-node'), scenes[i].id)
    await assertCanvas(animated, scenes.length)
  }
  await assertIdle(animated)
  await animated.setViewportSize({ width: 390, height: 844 })
  await assertCanvas(animated, scenes.length)
  await animated.close()

  const legacy = await reviewPage(browser)
  await legacy.route('**/api/task-generation/tree', route => route.fulfill({ json: snapshot }))
  await legacy.goto(`${base}/task-generation/scenario-tree?l1=${scenes.at(-1).id}`)
  await legacy.locator('.column-browser').waitFor()
  await settled(legacy)
  assert.equal(new URL(legacy.url()).pathname, '/scenario-studio')
  assert.equal(new URL(legacy.url()).searchParams.get('mode'), 'editor')
  assert.equal(await legacy.locator('.scene-tab.active').innerText(), scenes.at(-1).label)
  await legacy.close()
}

async function main() {
  await fs.mkdir(output, { recursive: true })
  // Replay the saved tree directly: re-importing Excel is not part of this UI review.
  const kb = path.resolve(__dirname, '../../backend_workspace/task_generation/KnowledgeBase')
  const { version } = JSON.parse(await fs.readFile(path.join(kb, 'current.json'), 'utf8'))
  const saved = JSON.parse(await fs.readFile(path.join(kb, 'versions', version, 'scene_tree.json'), 'utf8'))
  const snapshot = { ...saved, version }
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
  base = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  try {
    const page = await reviewPage(browser, { viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
    await page.route('**/api/task-generation/tree', route => route.fulfill({ json: snapshot }))
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    let treeRequests = 0
    let writes = 0
    page.on('request', request => {
      if (request.url().includes('/api/task-generation/tree')) treeRequests++
      if (request.url().includes('/api/') && request.method() !== 'GET') writes++
    })
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/task-generation/tree'))
    await page.goto(`${base}/scenario-studio`)
    const payload = await (await responsePromise).json()
    await page.locator('.capability-tree-node').first().waitFor()
    await page.mouse.move(0, 0)
    if (phase === 'final') await assertHomeTheme(page)
    await shot(page, `${phase}-home`)
    await page.locator('.capability-tree-node').first().click()
    await page.locator('.column-browser').waitFor()
    await page.waitForFunction(() => !document.querySelector('.studio-mode-enter-active, .studio-mode-leave-active'))
    if (phase === 'final') await assertEditorTheme(page)
    await page.mouse.move(0, 0)
    await page.evaluate(() => window.scrollTo(0, 0))
    await shot(page, `${phase}-editor`)
    const editorLayout = await page.locator('.studio-shell-header, .scenario-editor, .column-browser, .scenario-column').evaluateAll(elements => elements.map(element => {
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, borderWidth: style.borderWidth, radius: style.borderRadius }
    }))
    await fs.writeFile(path.join(output, `${phase}-editor-layout.json`), JSON.stringify(editorLayout, null, 2))
    if (phase === 'final') {
      const baselineLayout = JSON.parse(await fs.readFile(path.join(output, 'baseline-editor-layout.json'), 'utf8'))
      const baselineComparable = baselineLayout.map(item => {
        const comparable = { x: item.x, y: item.y, width: item.width, height: item.height, radius: item.radius }
        const legacyBorderWidth = String(item.border || '').trim().split(' ')[0]
        if (item.borderWidth || legacyBorderWidth) comparable.borderWidth = item.borderWidth || legacyBorderWidth
        return comparable
      })
      const editorComparable = editorLayout.map((item, index) => {
        const comparable = { x: item.x, y: item.y, width: item.width, height: item.height, radius: item.radius }
        if ('borderWidth' in baselineComparable[index]) comparable.borderWidth = item.borderWidth
        return comparable
      })
      assert.deepEqual(editorComparable, baselineComparable, 'Editor geometry, border widths, and radii must remain unchanged')
    }
    if (phase === 'final') await checkFinal(browser, page, snapshot)
    assert.equal(treeRequests, 1, 'Entering the editor must reuse the loaded tree')
    assert.equal(writes, 0, 'Read-only review must not save anything')
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ phase, scenes: payload.scenes.length, treeRequests, writes, screenshots: output }))
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
