// Complete production import flow with isolated API fixtures; no backend/cloud work.
const workbook = Buffer.from('UEsDBBQAAAAIAK6FNF0cdPw42AAAAPYBAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbK2RvU4DMQzHX+WUtWpcGDqgXhfKCgy8gJvz9aLLl2K3XN++uVAYUIGFyUr+Hz9L3rydE3EzeRe4VYNIegBgM5BH1jFRKEofs0cpz3yAhGbEA8H9arUGE4NQkKXMHWq72VGPRyfN01S+2cbQqkyOVfP4YZxZrcKUnDUoRYdT6L5RlleCLsnq4cEmXhSDgpuEWfkZcM29nChn21Hzilme0RcXTA7eYx73MY7695IbW8a+t4a6aI6+RDSnTNjxQCTe6Tq1RxsWf/OrmaGOu39e5Kv/cw+o595eAFBLAwQUAAAACACuhTRd/luGcooAAADwAAAACwAAAF9yZWxzLy5yZWxzjc8xDsIwDAXQq1Q+QF0YGFDaiaUr4gImddqqTRw5QZTbk7EgBkbrf70vmyuvlGcJaZpjqja/htTClHM8IyY7sadUS+RQEifqKZdTR4xkFxoZj01zQt0b0Jm9WfVDC9oPB6hur8j/2OLcbPki9uE55B8TX40ik46cW9hWfIoud5GlLihgZ/Djwe4NUEsDBBQAAAAIAK6FNF1m5Z0ypAAAAOoAAAAPAAAAeGwvd29ya2Jvb2sueG1sjY89DoJAEEavstkDOGBhQYDKhmOsMLgb2J/MrNFLWHoHK2NrYbwN4RgSkN5qZvLyvcmXnz11B+87cbG940LqGEMGwLVGq3jjA7qJtJ6sitNJR+BAqBrWiNH2sE2SHVhlnFwMGf3j8G1ratz7+mTRxUVC2KtovGNtAssynz/wbwqnLBZyfN/Hz2u4PYfrQ4qZVE0hUykoM9NCVZNKKHNYw7D2K79QSwMEFAAAAAgAroU0XbZUqs+OAAAA8QAAABoAAAB4bC9fcmVscy93b3JrYm9vay54bWwucmVsc43PPQ7CMAwF4KtUOUDdMjCgJhNLV8QFotRtojY/so2A2xMxoCIxMFl+lr4nDxfcrISc2IfCzSNuibXyIuUEwM5jtNzmgqle5kzRSl1pgWLdaheEQ9cdgfaGMsPebMZJKxqnXjXXZ8F/7DzPweE5u1vEJD8q4J5pZY8oFbW0oGj1iRjeo2+rqsAM8PWheQFQSwMEFAAAAAgAroU0XV050QgWAQAAsgQAABgAAAB4bC93b3Jrc2hlZXRzL3NoZWV0MS54bWyV1GFugyAYBuCrGA7QT9H6o0GSbr3BTkAYU1JBAyRtsuwCu8XO0ets95i6hWwJX6P/8PN94flBYJfBnX2nVMiupre+IV0I4wHAy04Z4XfDqOz052VwRoTp07XgR6fE81IyPdA8r8EIbQlny+wkguDMDZfMNaSYpnJeHAuShYZo22urnoKb5tpzFrg2olUMAmcwD0D+Fh6wgg9qTOQfsbyQQQ/W/6/A5ItIGpEU2ePzdvt6/yhgPjvfjbZNgbFykdJi4Vfy4yUHInstz+TtDryM8HIFvMDgWJmm4Fh4E7yK8GoFnGJwrFym4Fh4E3wf4fu7cLrASwyOlZNXBQtvgtcRXq+AVxgcKyevChZeB4c/rwnEZ4p/A1BLAQIUABQAAAAIAK6FNF0cdPw42AAAAPYBAAATAAAAAAAAAAAAAACAAQAAAABbQ29udGVudF9UeXBlc10ueG1sUEsBAhQAFAAAAAgAroU0Xf5bhnKKAAAA8AAAAAsAAAAAAAAAAAAAAIABCQEAAF9yZWxzLy5yZWxzUEsBAhQAFAAAAAgAroU0XWblnTKkAAAA6gAAAA8AAAAAAAAAAAAAAIABvAEAAHhsL3dvcmtib29rLnhtbFBLAQIUABQAAAAIAK6FNF22VKrPjgAAAPEAAAAaAAAAAAAAAAAAAACAAY0CAAB4bC9fcmVscy93b3JrYm9vay54bWwucmVsc1BLAQIUABQAAAAIAK6FNF1dOdEIFgEAALIEAAAYAAAAAAAAAAAAAACAAVMDAAB4bC93b3Jrc2hlZXRzL3NoZWV0MS54bWxQSwUGAAAAAAUABQBFAQAAnwQAAAAA', 'base64')
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(process.env.ADF_BROWSER_ARTIFACTS || path.join(os.tmpdir(), 'adf-external-import-review'))
const clone = value => JSON.parse(JSON.stringify(value))
const candidate = { session_id:'internal-draft',batch_id:'internal-active',tree_run_id:'internal-active',ready:true,reason:'',updated_at:'2026-09-20',
  latest_excel:{filename:'内部当前.xlsx',path:'fixture/internal.xlsx',rows:10,created_at:'2026-09-20'},task_count:2,trajectory_count:2,step_count:10 }
const internal = {release_id:'rel-internal',name:'已有内部发布',batch_ids:['internal-published'],created_at:'2026-09-20T09:00:00+08:00',
  source_kind:'correction',excel_paths:[{filename:'内部发布.xlsx',path:'fixture/internal.xlsx',rows:10,sha256:'a'.repeat(64),available:true}],
  trajectory_paths:[],source_count:1,task_count:2,trajectory_count:2,step_count:10,upload_status:'not_uploaded',local_available:true}
const overviewConversion = (release,status) => ({release_id:release.release_id,name:release.name,status,error:null,warnings:[],updated_at:'2026-09-20T09:00:00+08:00'})
function dashboard(releases,conversions,params) {
  const records=[{source:'数据飞轮',date:'2026-09-20',app:'爱奇艺',level1:'影音娱乐',level2:'视频播放',total_trajectories:2,subtask_trajectories:2,total_steps:10}]
  for(const release of releases.filter(item=>item.source_kind==='external_manual'))
    if(conversions.find(item=>item.release_id===release.release_id)?.status==='succeeded') records.push({
      source:release.external_import.data_source,date:release.external_import.data_date,app:release.external_import.app,
      level1:release.external_import.level1,level2:release.external_import.level2,total_trajectories:2,subtask_trajectories:2,total_steps:5})
  const selected=records.filter(row=>['source','app','level1','level2'].every(key=>!params.get(key)||params.get(key)==='all'||params.get(key)===row[key])
    &&(!params.get('start_date')||row.date>=params.get('start_date'))&&(!params.get('end_date')||row.date<=params.get('end_date')))
  const count=rows=>Object.fromEntries(['total_trajectories','subtask_trajectories','total_steps'].map(key=>[key,rows.reduce((sum,row)=>sum+row[key],0)]))
  const total=count(selected),dates=[...new Set(records.map(row=>row.date))].sort()
  return {schema_version:1,version:'mock-summary',updated_at:'2026-09-20',overview:{...total,show_manual_refine_steps:selected.some(row=>row.source==='数据飞轮'),manual_refine_steps:1,manual_known_steps:total.total_steps,manual_unknown_steps:0,
    level1_scenes:new Set(selected.map(row=>row.level1)).size,level2_scenes:new Set(selected.map(row=>row.level2)).size,total_apps:new Set(selected.map(row=>row.app)).size,avg_steps_per_trajectory:total.total_trajectories?total.total_steps/total.total_trajectories:0},
    filters:{sources:[...new Set(records.map(row=>row.source))],apps:[...new Set(records.map(row=>row.app))],
      scenes:records.map(row=>({name:row.level1,level2_scenes:[row.level2]})),date_range:{min_date:dates[0],max_date:dates.at(-1)}},
    trend:dates.map(date=>({date,...count(selected.filter(row=>row.date<=date))})),
    app_stats:selected.map(row=>({name:row.app,...count([row])})),scene_stats:selected.map(row=>({name:row.level1+'-'+row.level2,...count([row])})),
    action_stats:[{category:'click',count:total.total_steps}],step_stats:[{steps:5,count:total.total_trajectories}],
    conversions:clone(conversions),warnings:[],workbook_url:'/api/training-data-overview/workbook'}
}
function preview(fields,index,mode) {
  const conflict=mode==='conflict',invalid=mode==='invalid'||conflict
  return {import_id:invalid?null:'import-'+index,valid:!invalid,sheets:['轨迹数据','备份表'],sheet_name:fields.sheet_name||'轨迹数据',
    filename:'外部历史轨迹.xlsx',expires_at:'2026-09-21T09:00:00+08:00',
    summary:{trajectory_count:2,step_count:5,apps:[{name:fields.app,trajectory_count:2,step_count:5}],
      scenes:[{level1:fields.level1,level2:fields.level2,trajectory_count:2,step_count:5}]},
    errors:invalid?[{sheet:'轨迹数据',row:conflict?null:4,field:conflict?'data_date':'trajectory_id',message:conflict?'同一文件已使用不同的数据日期或分类发布，请查看原发布记录':'缺失轨迹标识 trajectory_id（模拟第4行）'}]:[],
    warnings:[{sheet:'轨迹数据',row:null,field:'app',message:'表格空缺分类使用本次填写值（模拟）'}],
    duplicate_release:(conflict||mode==='duplicate')?{release_id:'rel-external',name:'外部历史导入'}:null}
}
async function serve() {
  const server=http.createServer(async(req,res)=>{
    try{let file=path.resolve(dist,'.'+decodeURIComponent(new URL(req.url,'http://localhost').pathname))
      if(!file.startsWith(dist+path.sep)||!path.extname(file))file=path.join(dist,'index.html')
      const body=await fs.readFile(file);res.setHeader('Content-Type',({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');res.end(body)
    }catch{res.statusCode=404;res.end()}
  })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  return {base:'http://127.0.0.1:'+server.address().port,close:()=>new Promise(resolve=>server.close(resolve))}
}
function gate() {
  let resolve,seen
  const promise=new Promise(done=>{resolve=done}),started=new Promise(done=>{seen=done})
  return {promise,resolve,seen,wait:async()=>{
    let timer;try{await Promise.race([started,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Mock request did not start')),5000)})])}finally{clearTimeout(timer)}
  }}
}
async function main() {
  await fs.mkdir(output,{recursive:true})
  const server=await serve(),browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true})
  const requests=[],unknown=[],errors=[],previews=[],publishes=[],releases=[clone(internal)],conversions=[overviewConversion(internal,'succeeded')]
  const imports=new Map(),committed=new Map()
  const gates=[]
  let previewMode='invalid',previewGate=null,publishGate=null,historyGate=null,publishMode='lost-response',external=null,downloadCount=0
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1080},serviceWorkers:'block'})
    context.on('page',page=>page.on('pageerror',error=>errors.push(error.message)))
    await context.route('**/*',async route=>{
      const req=route.request(),url=new URL(req.url()),name=url.pathname
      if(url.origin!==server.base)return route.abort()
      if(!name.startsWith('/api/'))return route.continue()
      requests.push({name,method:req.method(),query:url.search})
      const json=body=>route.fulfill({json:clone(body)})
      if(name==='/api/dataset-release-imports/preview'){
        const form=await new Response(req.postDataBuffer(),{headers:{'content-type':req.headers()['content-type']}}).formData()
        const fields=Object.fromEntries([...form.entries()].filter(([name])=>name!=='file'))
        assert.equal(form.get('file').name,'外部历史轨迹.xlsx');assert.ok(form.get('file').size>500)
        previews.push(fields)
        const result=preview(fields,previews.length,previewMode)
        if(result.import_id)imports.set(result.import_id,clone(fields))
        if(previewGate){const wait=previewGate;previewGate=null;wait.seen();await wait.promise}
        return json(result)
      }
      if(name==='/api/dataset-releases/import'){
        const body=req.postDataJSON();publishes.push(body)
        assert.deepEqual(Object.keys(body).sort(),['import_id','name','request_id']);assert.ok(body.request_id)
        if(publishMode==='conflict')return route.fulfill({status:409,json:{detail:{code:'duplicate_metadata',message:'同一文件已使用不同元信息发布（模拟冲突）',release_id:'rel-external'}}})
        if(!external){
          const fields=imports.get(body.import_id);assert.ok(fields)
          external={...clone(internal),release_id:'rel-external',name:body.name,batch_ids:['external_fixture'],source_kind:'external_manual',
            created_at:'2026-09-20T10:00:00+08:00',trajectory_count:2,task_count:2,step_count:5,
            excel_paths:[{filename:'外部历史轨迹.xlsx',path:'fixture/external.xlsx',rows:5,sha256:'b'.repeat(64),available:true}],trajectory_paths:[],
            external_import:{...fields,import_id:body.import_id,parser_version:1,source_sha256:'b'.repeat(64),content_hash:'c'.repeat(64),
              canonical:{path:'fixture/canonical.json',sha256:'c'.repeat(64)},manifest:{path:'fixture/manifest.json',sha256:'d'.repeat(64)}}}
          releases.push(external);conversions.push(overviewConversion(external,'queued'));committed.set(body.request_id,external)
        }
        if(publishGate){const wait=publishGate;publishGate=null;wait.seen();await wait.promise}
        if(publishMode==='lost-response'){publishMode='normal';return route.abort('failed')}
        return json({release:committed.get(body.request_id)||external})
      }
      if(name==='/api/dataset-releases/candidates')return json({candidates:[candidate]})
      if(name==='/api/dataset-releases'){
        const snapshot=clone(releases)
        if(historyGate){const held=historyGate;historyGate=null;held.seen();await held.promise}
        return json({releases:snapshot})
      }
      if(name==='/api/dataset-upload-capabilities')return json({internal:{configured:true,reason:null}})
      if(name==='/api/training-data-overview')return json(dashboard(releases,conversions,url.searchParams))
      if(/\/api\/dataset-releases\/[^/]+\/excels\/0$/.test(name)){downloadCount++;return route.fulfill({contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers:{'Content-Disposition':'attachment; filename="original.xlsx"'},body:workbook})}
      if(name.startsWith('/api/dataset-releases/'))return json({release:releases.find(item=>item.release_id===name.split('/').at(-1))})
      unknown.push(name);return route.fulfill({status:501,json:{detail:'Unmocked API blocked: '+name}})
    })
    const page=await context.newPage()
    const id=part=>page.getByTestId('external-import-'+part)
    const fill=async(part,value)=>{const host=id(part),child=host.locator('input');await(await child.count()?child:host).fill(value)}
    const closeDetail=async()=>{await page.keyboard.press('Escape');await page.getByRole('dialog',{name:'数据集详情',exact:true}).waitFor({state:'hidden'})}
    const prepare=async()=>{await id('preview').click();await id('summary').waitFor()}
    const internalName=page.getByPlaceholder('输入数据集名称，例如：爱奇艺 GUI 轨迹数据集 v1')
    await page.goto(server.base+'/data-publishing/archive')
    await page.locator('.candidate').filter({hasText:'internal-active'}).locator('.el-checkbox').click()
    await internalName.fill('尚未提交的内部数据集名称')
    await id('open').click();await id('dialog').waitFor()
    await id('file').setInputFiles({name:'外部历史轨迹.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:workbook})
    await fill('name','外部历史导入');await fill('data-source','专家采集');await fill('data-date','2024-03-10')
    await fill('app','淘宝');await fill('level1','购物消费');await fill('level2','商品搜索')
    await prepare()
    await id('errors').getByText(/缺失轨迹标识 trajectory_id/).waitFor()
    assert.equal(await id('publish').isDisabled(),true)
    assert.match(await id('errors').innerText(),/4/)
    await page.screenshot({path:path.join(output,'invalid-preview.png'),fullPage:true})
    previewMode='valid';const delayed=gate();previewGate=delayed;gates.push(delayed)
    await id('preview').click();await delayed.wait()
    await fill('app','京东')
    delayed.resolve();await page.waitForTimeout(100)
    assert.equal(await id('summary').count(),0,'A late preview must not restore an obsolete result')
    assert.equal(await id('publish').isDisabled(),true)
    await prepare();await id('warnings').waitFor()
    assert.equal(previews.at(-1).app,'京东')
    await fill('data-date','2024-03-11')
    assert.equal(await id('summary').count(),0)
    assert.equal(await id('publish').isDisabled(),true)
    await id('sheet').selectOption('备份表');await prepare()
    assert.equal(previews.at(-1).sheet_name,'备份表')
    assert.equal(previews.at(-1).data_date,'2024-03-11')
    previewMode='conflict';await id('preview').click();await id('errors').getByText(/同一文件已/).waitFor()
    assert.equal(await id('publish').isDisabled(),true)
    await id('duplicate').waitFor()
    previewMode='valid';await prepare()
    await page.screenshot({path:path.join(output,'valid-preview.png'),fullPage:true})
    await id('publish').scrollIntoViewIfNeeded()
    await page.screenshot({path:path.join(output,'valid-preview-actions.png'),fullPage:true})
    // A server rejection is recoverable without clearing the draft or creating a release.
    publishMode='conflict';await id('publish').click()
    await id('error').getByText(/同一文件已使用不同元信息发布.*请重新预览/).waitFor()
    assert.equal(await id('summary').count(),0);assert.equal(await id('publish').isDisabled(),true);assert.equal(releases.length,1)
    await prepare()
    publishMode='lost-response';const delayedPublish=gate();publishGate=delayedPublish;gates.push(delayedPublish)
    await id('publish').click();await delayedPublish.wait()
    await id('publish').evaluate(button=>{button.click();button.click()})
    assert.equal(publishes.length,2,'Repeated clicks while publishing must not send additional requests')
    assert.equal(await id('cancel').isDisabled(),true)
    delayedPublish.resolve()
    await id('dialog').getByText(/Failed to fetch|发布失败|fetch/).waitFor()
    const lost=publishes.at(-1)
    assert.equal(releases.length,2)
    await id('cancel').click();await id('dialog').waitFor({state:'hidden'})
    await id('open').click();await id('summary').waitFor()
    const oldHistory=gate();historyGate=oldHistory;gates.push(oldHistory)
    await id('publish').click()
    await oldHistory.wait()
    await page.getByTestId('release-overview-state').getByText('等待转换',{exact:true}).waitFor()
    assert.equal(publishes.at(-1).request_id,lost.request_id,'Unknown outcome retry must keep the same idempotency key')
    assert.equal(publishes.at(-1).import_id,lost.import_id)
    assert.equal(releases.length,2)
    assert.equal(await page.getByText('当前批次的选择和页面缓存已清空',{exact:false}).count(),0)
    assert.equal(await internalName.inputValue(),'尚未提交的内部数据集名称')
    assert.equal(await page.locator('.candidate').filter({hasText:'internal-active'}).getByRole('checkbox').isChecked(),true)
    assert.equal(await page.locator('.candidate').filter({hasText:'external_fixture'}).count(),0)
    conversions.find(item=>item.release_id==='rel-external').status='succeeded'
    await page.getByTestId('release-overview-state').getByText('已汇总',{exact:true}).waitFor()
    await page.getByRole('dialog',{name:'数据集详情',exact:true}).getByText('外部历史轨迹.xlsx',{exact:true}).waitFor()
    await page.screenshot({path:path.join(output,'published-detail.png'),fullPage:true})
    const downloadLink=page.getByRole('dialog',{name:'数据集详情',exact:true}).getByRole('link',{name:'下载',exact:true})
    const href=await downloadLink.getAttribute('href');assert.match(href,/\/api\/dataset-releases\/rel-external\/excels\/0/)
    const downloaded=page.waitForEvent('download')
    await downloadLink.click()
    const file=await downloaded;await file.path();assert.equal(downloadCount,1)
    await closeDetail()
    const history=page.locator('.el-table__row').filter({hasText:'外部历史导入'})
    assert.equal(await history.getByRole('button',{name:'云道S3上传',exact:true}).isEnabled(),true)
    // A later explicit refresh must win over the older post-publication history request.
    external.internal_upload={job_id:'newer-upload-state',release_id:external.release_id,mode:'internal',status:'failed',stage:'failed',
      completed_files:0,total_files:1,completed_bytes:0,total_bytes:100,percent:0,current_file:null,
      created_at:'2026-09-20T10:00:01+08:00',started_at:'2026-09-20T10:00:01+08:00',completed_at:'2026-09-20T10:00:02+08:00',
      error:'刷新后的较新上传状态（模拟）',file_results:[]}
    await page.getByRole('button',{name:'刷新',exact:true}).click()
    await history.getByRole('button',{name:'重试云道S3上传',exact:true}).waitFor()
    await history.getByRole('button',{name:'详情',exact:true}).click()
    await page.getByRole('dialog',{name:'数据集详情',exact:true}).getByText('刷新后的较新上传状态（模拟）',{exact:true}).waitFor()
    const oldResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/dataset-releases')
    oldHistory.resolve();await oldResponse;await page.waitForTimeout(100)
    assert.equal(await history.getByRole('button',{name:'重试云道S3上传',exact:true}).count(),1,'Old history response cannot roll back newer S3 state')
    assert.equal(await page.getByRole('dialog',{name:'数据集详情',exact:true}).getByText('刷新后的较新上传状态（模拟）',{exact:true}).count(),1)
    await page.screenshot({path:path.join(output,'newer-history-preserved.png'),fullPage:true})
    await closeDetail()
    // Re-importing identical bytes/metadata points at the existing release and does not double-count.
    await id('open').click();await id('file').setInputFiles({name:'外部历史轨迹.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:workbook})
    await fill('name','外部历史导入');await fill('data-source','专家采集');await fill('data-date','2024-03-11')
    await fill('app','京东');await fill('level1','购物消费');await fill('level2','商品搜索')
    previewMode='duplicate';await prepare();await id('duplicate').waitFor()
    await id('publish').click();await page.getByTestId('release-overview-state').getByText('已汇总',{exact:true}).waitFor()
    assert.equal(releases.length,2);await closeDetail()
    assert.equal(await internalName.inputValue(),'尚未提交的内部数据集名称')
    assert.equal(await page.locator('.candidate').getByRole('checkbox').isChecked(),true)
    await page.goto(server.base+'/data-publishing/overview')
    await page.getByTestId('overview-metric-total_trajectories').getByText('4',{exact:true}).waitFor()
    assert.match(await page.getByTestId('overview-metric-total_steps').innerText(),/15/)
    assert.deepEqual(await page.getByTestId('overview-source').locator('option').allTextContents(),['所有数据','数据飞轮','专家采集'])
    assert.equal(await page.getByTestId('overview-start-date').inputValue(),'2024-03-11')
    await page.getByTestId('overview-source').selectOption('专家采集')
    await page.waitForTimeout(100);await page.waitForLoadState('networkidle')
    await page.getByTestId('overview-search').click()
    await page.getByTestId('overview-metric-total_trajectories').getByText('2',{exact:true}).waitFor()
    assert.equal(await page.getByTestId('overview-metric-manual_refine_steps').count(),0)
    await page.screenshot({path:path.join(output,'overview-external-source.png'),fullPage:true})
    assert.equal(requests.some(item=>/\/api\/(correction|tree|trajectory-preprocessing|task-generation)/.test(item.name)),false)
    assert.deepEqual(unknown,[]);assert.deepEqual(errors,[])
    const report={passed:true,previewRequests:previews.length,publishRequests:publishes.length,releases:releases.length,
      latePreviewIgnored:true,newerHistoryAndDetailPreserved:true,changedMetadataInvalidatesPreview:true,invalidTableAndConflictLocated:true,lostResponseRetryReusedRequestId:true,
      internalDraftPreserved:true,externalBatchNeverOfferedForProcessing:true,originalDownload:true,s3EntryPreserved:true,twoSourcesAccumulate:true,output}
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report))
  }finally{
    for(const held of gates)held.resolve()
    await fs.writeFile(path.join(output,'requests.json'),JSON.stringify({requests,previews,publishes},null,2))
    await browser.close();await server.close()
  }
}
main().catch(error=>{console.error(error);process.exitCode=1})
