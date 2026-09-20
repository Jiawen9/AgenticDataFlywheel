// Production build versus unchanged DEV SFCs, with entirely local HTTP fixtures.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const vm = require('node:vm')
const ts = require('typescript')
const { buildReference } = require('./overview-dev-reference.cjs')
const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(process.env.ADF_BROWSER_ARTIFACTS || path.join(os.tmpdir(), 'adf-training-overview-review'))
const clone = value => JSON.parse(JSON.stringify(value))
const rows = [10,12,14,16,18,18,10,12,14,18].map((steps,index) => ({
  app: index < 6 ? '爱奇艺' : '淘宝', level1: index < 6 ? '影音娱乐' : '购物消费', level2: index < 6 ? '视频查找与播放' : '商品搜索',
  source: index < 8 ? '数据飞轮' : '专家采集', date: index < 6 ? '2026-09-19' : '2026-09-20', steps, manual: index < 4 ? 1 : 0,
}))
const count = items => ({ total_trajectories: items.length, subtask_trajectories: items.length, total_steps: items.reduce((sum,row) => sum+row.steps,0) })
function response(params,state) {
  const included = (row,omit=[]) => ['app','source','level1','level2'].every(key => omit.includes(key)||!params.get(key)||params.get(key)==='all'||row[key]===params.get(key))
    && (omit.includes('date')||((!params.get('start_date')||row.date>=params.get('start_date'))&&(!params.get('end_date')||row.date<=params.get('end_date'))))
  const available=state.noData?[]:rows, current=available.filter(row=>included(row)), total=count(current)
  const group = key => [...new Set(current.map(row=>row[key]))].map(name=>({name,...count(current.filter(row=>row[key]===name))}))
  return {
    schema_version:1,version:state.noData?null:'fixture-v1',updated_at:'2026-09-20T02:00:00Z',
    overview:{...total,manual_refine_steps:current.reduce((sum,row)=>sum+row.manual,0),show_manual_refine_steps:current.some(row=>row.source.includes('数据飞轮')),
      manual_known_steps:total.total_steps,manual_unknown_steps:0,level1_scenes:new Set(current.map(row=>row.level1)).size,
      level2_scenes:new Set(current.map(row=>row.level2)).size,total_apps:new Set(current.map(row=>row.app)).size,
      avg_steps_per_trajectory:current.length?total.total_steps/current.length:0},
    filters:{sources:[...new Set(available.map(row=>row.source))],
      scenes:[...new Set(available.filter(row=>included(row,['level1','level2','date'])).map(row=>row.level1))].map(name=>({name,level2_scenes:[...new Set(available.filter(row=>row.level1===name&&included(row,['level1','level2','date'])).map(row=>row.level2))]})),
      apps:[...new Set(available.filter(row=>included(row,['app','date'])).map(row=>row.app))],
      date_range:{min_date:state.noData?null:'2026-09-19',max_date:state.noData?null:'2026-09-20'}},
    trend:[...new Set(current.map(row=>row.date))].sort().map(date=>({date,...count(current.filter(row=>row.date<=date))})),
    app_stats:group('app'),scene_stats:group('level2'),
    action_stats:current.length?[{category:'click',count:total.total_steps-current.length},{category:'finish',count:current.length}]:[],
    step_stats:[...new Set(current.map(row=>row.steps))].sort((a,b)=>a-b).map(steps=>({steps,count:current.filter(row=>row.steps===steps).length})),
    conversions:clone(state.conversions),warnings:['不应出现在总览里的维护警告'],workbook_url:state.noData?null:'/api/training-data-overview/workbook',
  }
}
const release=(id,name)=>({release_id:id,name,batch_ids:['closed-'+id],created_at:'2026-09-20T09:00:00+08:00',
  excel_paths:[{path:'fixture/current.xlsx',filename:'纠偏全集.xlsx',rows:142,sha256:'a'.repeat(64),available:true}],
  trajectory_paths:[],source_count:1,task_count:10,trajectory_count:10,step_count:142,upload_status:'not_uploaded',local_available:true})
const conversion=(id,status)=>({release_id:id,name:id,status,error:status==='failed'?'冻结 Excel 校验失败（模拟）':null,warnings:['缺少旧采集场景信息（模拟警告）'],updated_at:'2026-09-20'})
async function serve(directory) {
  const server=http.createServer(async(req,res)=>{
    try {
      const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname)
      let file=path.resolve(directory,'.'+pathname)
      if(!file.startsWith(directory+path.sep)||!path.extname(file)) file=path.join(directory,'index.html')
      const body=await fs.readFile(file)
      res.setHeader('Content-Type',({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream')
      res.end(body)
    }catch{res.statusCode=404;res.end()}
  })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  return {base:'http://127.0.0.1:'+server.address().port,close:()=>new Promise(resolve=>server.close(resolve))}
}
async function snapshot(page,selector) {
  return page.locator(selector).evaluate(root=>{
    const base=root.getBoundingClientRect()
    const describe=selector=>[...root.querySelectorAll(selector)].map(element=>{
      const rect=element.getBoundingClientRect(),style=getComputedStyle(element)
      return {text:element.matches('select')?[...element.options].map(option=>option.textContent):element.textContent.trim(),
        value:element.matches('select,input')?element.value:undefined,x:rect.x-base.x,y:rect.y-base.y,width:rect.width,height:rect.height,
        fontSize:style.fontSize,fontWeight:style.fontWeight,padding:style.padding,display:style.display}
    })
    return {heading:describe('h1'),tabs:describe('.view-link'),labels:describe('.filter-item > label'),controls:describe('.filter-item select,.filter-item input'),
      buttons:describe('.filter-item button'),cards:describe('.card'),chart:describe('.chart-container'),pies:describe('.pie-card'),pieTitles:describe('.pie-card h3')}
  })
}
function compareLayout(actual,expected,view) {
  for(const key of Object.keys(expected)) {
    assert.equal(actual[key].length,expected[key].length,view+' '+key+' count')
    actual[key].forEach((item,index)=>{
      const ref=expected[key][index]
      for(const field of ['text','value','fontSize','fontWeight','padding','display']) assert.deepEqual(item[field],ref[field],view+' '+key+'['+index+'].'+field)
      for(const field of ['x','y','width','height']) assert.ok(Math.abs(item[field]-ref[field])<1.1,view+' '+key+'['+index+'].'+field+': '+item[field]+' vs DEV '+ref[field])
    })
  }
}
function removeColors(value) {
  if(Array.isArray(value)) return value.map(removeColors)
  if(!value||typeof value!=='object') return value
  return Object.fromEntries(Object.entries(value).filter(([key])=>!/color/i.test(key)).map(([key,item])=>[key,removeColors(item)])
    .filter(([,item])=>!item||typeof item!=='object'||Array.isArray(item)||Object.keys(item).length))
}
async function main() {
  await fs.mkdir(output,{recursive:true})
  const reference=await buildReference(),appServer=await serve(dist),devServer=await serve(reference.directory)
  const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true})
  const errors=[],requests=[],unknown=[],layoutErrors=[],state={noData:false,conversions:[conversion('rel-one','failed'),conversion('rel-two','succeeded')]}
  let failRead=false,failDownload=false,retries=0,holdRead=null
  const releases=[release('rel-one','失败转换验收'),release('rel-two','成功转换验收')]
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1080},serviceWorkers:'block'})
    context.on('page',page=>page.on('pageerror',error=>errors.push(error.message)))
    await context.addInitScript(()=>{
      const NativeDate=Date,started=NativeDate.now(),base=new NativeDate('2026-09-20T04:00:00Z').getTime()
      window.Date=class extends NativeDate {constructor(...args){super(...(args.length?args:[base+NativeDate.now()-started]))} static now(){return base+NativeDate.now()-started}}
    })
    await context.route('**/*',async route=>{
      const req=route.request(),url=new URL(req.url()),name=url.pathname
      if(![appServer.base,devServer.base].includes(url.origin)) return route.abort()
      if(!name.startsWith('/api/')) return route.continue()
      requests.push({origin:url.origin,name,query:url.search,method:req.method()})
      const json=body=>route.fulfill({json:clone(body)}),result=response(url.searchParams,state)
      if(url.origin===devServer.base) {
        const devResult={...result,trend:result.trend.map(item=>({...item,total_steps_cumulative:item.total_steps})),
          app_stats:result.app_stats.map(item=>({...item,app:item.name}))}
        const values={sources:result.filters.sources,date_range:result.filters.date_range,scenes:result.filters.scenes,apps:result.filters.apps,
          data:devResult,scene_stats:result.scene_stats.map(item=>({...item,scene_name:item.name})),distributions:{action_stats:result.action_stats,step_stats:result.step_stats}}
        if(name.slice(5) in values) return json(values[name.slice(5)])
      }
      if(name==='/api/training-data-overview') {
        if(failRead){failRead=false;return route.fulfill({status:503,json:{detail:'总览模拟服务暂不可用'}})}
        if(holdRead){const gate=holdRead;holdRead=null;gate.seen();await gate.wait}
        return json(result)
      }
      if(name.endsWith('/retry')) {
        const item=state.conversions.find(item=>item.release_id===name.split('/').at(-2));retries++
        Object.assign(item,{status:'running',error:null});return json({conversion:item})
      }
      if(name==='/api/training-data-overview/workbook') {
        assert.equal(url.search,'','Global download must not include dashboard/release filters')
        if(failDownload){failDownload=false;return route.fulfill({status:409,json:{detail:'完整汇总文件校验失败（模拟）'}})}
        return route.fulfill({contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers:{'Content-Disposition':'attachment; filename="all_data.xlsx"'},body:'mock workbook only'})
      }
      if(name==='/api/dataset-releases/candidates') return json({candidates:[]})
      if(name==='/api/dataset-releases') return json({releases})
      if(name.startsWith('/api/dataset-releases/')) return json({release:releases.find(item=>item.release_id===name.split('/').at(-1))})
      if(name==='/api/dataset-upload-capabilities') return json({internal:{configured:false,reason:'云道S3上传尚未配置'}})
      unknown.push(name);return route.fulfill({status:501,json:{detail:'Unexpected API blocked: '+name}})
    })
    const page=await context.newPage(),dev=await context.newPage(),target='[data-testid="training-data-overview"]'
    const settled=async page=>{await page.getByRole('button',{name:'搜索',exact:true}).waitFor();await page.locator('.loading-overlay').waitFor({state:'hidden'});await page.waitForLoadState('networkidle')}
    const appReads=()=>requests.filter(item=>item.origin===appServer.base&&item.name==='/api/training-data-overview')
    const change=async(testid,value,requestCount=1)=>{
      const before=appReads().length
      await page.getByTestId(testid).selectOption(value)
      for(let tries=0;tries<100&&appReads().length<before+requestCount;tries++) await page.waitForTimeout(10)
      assert.ok(appReads().length>=before+requestCount,'Expected facet requests for '+testid)
      await page.waitForLoadState('networkidle')
    }
    const cards=page=>page.locator('.cards .card').allTextContents(),comparisons=[]
    for(const view of ['scene','app']) {
      await page.goto(appServer.base+'/data-publishing/overview'+(view==='app'?'?view=app':''));await settled(page)
      await page.getByRole('heading',{name:'VLA训练数据概览 - '+(view==='app'?'App视角':'场景视角'),exact:true}).waitFor()
      const parentWidth=await page.locator(target).evaluate(element=>element.parentElement.getBoundingClientRect().width)
      await dev.setViewportSize({width:Math.round(parentWidth+20),height:1080})
      await dev.goto(devServer.base+(view==='app'?'/app-view':'/'));await settled(dev)
      const actual=await snapshot(page,target),expected=await snapshot(dev,view==='app'?'.app-view':'.scene-view')
      await fs.writeFile(path.join(output,view+'-layout.json'),JSON.stringify({actual,expected},null,2))
      try{compareLayout(actual,expected,view)}catch(error){layoutErrors.push(error.message)}
      assert.equal(await page.locator('.cards .card').count(),8)
      assert.equal(await page.locator(target).getByText(/校验汇总|下载完整|重试转换|冻结 Excel|维护警告/).count(),0)
      assert.equal(await page.locator(target+' canvas').count(),4)
      await dev.waitForTimeout(1200)
      await page.locator(target).screenshot({path:path.join(output,view+'-production.png')})
      await dev.locator(view==='app'?'.app-view':'.scene-view').screenshot({path:path.join(output,view+'-dev.png')})
      comparisons.push({view,options:await dev.evaluate(()=>window.__devOptions)})
      const before=await cards(page)
      await change('overview-source','数据飞轮',3)
      assert.deepEqual(await cards(page),before)
      if(view==='scene') {
        await change('overview-level1','影音娱乐')
        await page.getByTestId('overview-app').locator('option[value="淘宝"]').waitFor({state:'detached'})
        assert.deepEqual(await page.getByTestId('overview-app').locator('option').allTextContents(),['全部App','爱奇艺'])
        await change('overview-level2','视频查找与播放')
      }else{
        await change('overview-app','淘宝')
        await page.getByTestId('overview-level1').locator('option[value="影音娱乐"]').waitFor({state:'detached'})
        assert.deepEqual(await page.getByTestId('overview-level1').locator('option').allTextContents(),['全部','购物消费'])
        await page.getByTestId('overview-level1').selectOption('购物消费')
        await page.getByTestId('overview-level2').selectOption('商品搜索')
      }
      await page.waitForLoadState('networkidle');assert.deepEqual(await cards(page),before)
      const beforeQuick=appReads().length
      await page.getByRole('button',{name:'最近一周',exact:true}).click()
      assert.equal(await page.getByTestId('overview-start-date').inputValue(),'2026-09-13')
      assert.equal(await page.getByTestId('overview-end-date').inputValue(),'2026-09-20')
      await page.getByRole('button',{name:'最近一个月',exact:true}).click()
      assert.equal(await page.getByTestId('overview-start-date').inputValue(),'2026-08-21')
      assert.equal(appReads().length,beforeQuick);assert.deepEqual(await cards(page),before)
      await page.getByTestId('overview-search').click();await settled(page)
      assert.equal((await page.getByTestId('overview-metric-total_trajectories').innerText()).replace(/\s+/g,''),'轨迹总数'+(view==='scene'?'6':'2'))
      await change('overview-source','专家采集',3)
      assert.equal(await page.locator('.cards .card').count(),8)
      await page.getByTestId('overview-search').click();await settled(page)
      assert.equal(await page.locator('.cards .card').count(),7,'Only non-flywheel rows hide the manual-refine card')
    }
    await page.getByRole('link',{name:'场景视角',exact:true}).click();await settled(page)
    await page.getByTestId('overview-metric-manual_refine_steps').waitFor()
    assert.equal(await page.getByTestId('overview-source').inputValue(),'all')
    assert.equal(await page.getByTestId('overview-level1').inputValue(),'')
    assert.equal(await page.locator('.cards .card').count(),8)
    failRead=true;await page.getByTestId('overview-search').click()
    await page.getByText('加载失败，请重试',{exact:true}).waitFor()
    assert.equal(await page.locator('.cards .card').count(),8)
    state.noData=true;await page.getByTestId('overview-search').click();await settled(page)
    assert.equal((await page.getByTestId('overview-metric-total_trajectories').innerText()).replace(/\s+/g,''),'轨迹总数0')
    assert.equal(await page.locator('.cards .card').count(),7);state.noData=false
    await page.goto(appServer.base+'/data-publishing/archive')
    await page.getByRole('button',{name:'详情',exact:true}).first().click()
    await page.getByTestId('release-overview-state').getByText('转换失败',{exact:true}).waitFor()
    await page.getByText('冻结 Excel 校验失败（模拟）',{exact:true}).waitFor()
    await page.getByText('缺少旧采集场景信息（模拟警告）',{exact:true}).waitFor()
    assert.equal(await page.getByRole('button',{name:'云道S3上传',exact:true}).first().isDisabled(),true)
    await page.getByTestId('release-overview-retry').click()
    await page.getByTestId('release-overview-state').getByText('转换中',{exact:true}).waitFor()
    assert.equal(await page.getByTestId('release-overview-retry').isDisabled(),true)
    const currentReads=appReads().length
    await page.keyboard.press('Escape');await page.locator('.el-dialog').waitFor({state:'hidden'})
    await page.waitForTimeout(3300)
    assert.equal(appReads().length,currentReads,'Closing detail cancels its conversion polling')
    await page.getByRole('button',{name:'详情',exact:true}).nth(1).click()
    await page.getByTestId('release-overview-state').getByText('已汇总',{exact:true}).waitFor()
    assert.equal(await page.getByTestId('release-overview-status').getAttribute('data-release-id'),'rel-two')
    assert.equal(await page.getByTestId('release-overview-error').count(),0)
    assert.equal(await page.getByTestId('release-overview-retry').innerText(),'校验汇总')
    failDownload=true;await page.getByTestId('release-overview-download').click()
    await page.getByTestId('release-overview-download-error').waitFor()
    const downloadEvent=page.waitForEvent('download');await page.getByTestId('release-overview-download').click()
    assert.equal((await downloadEvent).suggestedFilename(),'all_data.xlsx')
    await page.getByTestId('release-overview-retry').click()
    await page.getByTestId('release-overview-state').getByText('转换中',{exact:true}).waitFor()
    Object.assign(state.conversions[1],{status:'succeeded',error:null})
    await page.getByTestId('release-overview-state').getByText('已汇总',{exact:true}).waitFor()
    await page.screenshot({path:path.join(output,'release-maintenance.png'),fullPage:true})
    // A delayed response from an old detail must not change a newly opened detail.
    await page.keyboard.press('Escape');await page.locator('.el-dialog').waitFor({state:'hidden'})
    let releaseHeld,markHeld
    const seenHeld=new Promise(resolve=>{markHeld=resolve}),held=new Promise(resolve=>{releaseHeld=resolve})
    holdRead={wait:held,seen:markHeld}
    state.conversions[0]=conversion('rel-one','failed')
    await page.getByRole('button',{name:'详情',exact:true}).first().click()
    let heldTimeout
    try { await Promise.race([seenHeld,new Promise((_,reject)=>{heldTimeout=setTimeout(()=>reject(new Error('Expected delayed release detail request')),5000)})]) }
    finally { clearTimeout(heldTimeout) }
    await page.keyboard.press('Escape');await page.locator('.el-dialog').waitFor({state:'hidden'})
    await page.getByRole('button',{name:'详情',exact:true}).nth(1).click()
    await page.getByTestId('release-overview-state').getByText('已汇总',{exact:true}).waitFor()
    releaseHeld();await page.waitForTimeout(150)
    assert.equal(await page.getByTestId('release-overview-status').getAttribute('data-release-id'),'rel-two')
    assert.equal(await page.getByTestId('release-overview-error').count(),0)
    assert.equal(retries,2)
    assert.equal(requests.some(item=>item.method!=='GET'&&!item.name.endsWith('/retry')),false)
    assert.deepEqual(unknown,[]);assert.deepEqual(errors,[])
    const code=ts.transpileModule(await fs.readFile(path.resolve(__dirname,'../src/utils/trainingOverviewCharts.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
    const factories={};vm.runInNewContext(code,{exports:factories},{filename:'trainingOverviewCharts.ts'})
    const fixture=response(new URLSearchParams(),state)
    for(const {view,options} of comparisons) {
      const actual=[factories.trendOption(fixture.trend),factories.comparisonOption(view==='scene'?fixture.app_stats:fixture.scene_stats,view),
        factories.distributionOption(fixture.action_stats.map(item=>({name:item.category,value:item.count})),'action'),
        factories.distributionOption(fixture.step_stats.map(item=>({name:item.steps+' 步',value:item.count})),'steps')]
      assert.deepEqual(removeColors(clone(actual)),removeColors(options),view+' chart settings match DEV except colors')
    }
    assert.deepEqual(layoutErrors,[],'All DEV geometry comparisons must match')
    const report={passed:true,lateDetailReplyIgnored:true,devViewsCompared:2,geometryMatched:true,chartOptionsMatchedExceptColors:true,cascadeAndDatesDeferred:true,independentAppEntry:true,maintenanceRetries:retries,globalDownload:true,detailPollingStopped:true,screenshots:output}
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2))
    console.log(JSON.stringify(report))
  }finally{await fs.writeFile(path.join(output,'requests.json'),JSON.stringify(requests,null,2));await browser.close();await appServer.close();await devServer.close();await reference.cleanup()}
}
main().catch(error=>{console.error(error);process.exitCode=1})
