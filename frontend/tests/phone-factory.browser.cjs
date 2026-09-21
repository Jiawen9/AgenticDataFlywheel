// All API requests are fulfilled locally. No device, model, collector or upload service is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const copy = value => JSON.parse(JSON.stringify(value))
const batch = id => ({schema_version:1,batch_id:id,source_job_id:id==='manual-one'?null:id,kind:id==='manual-one'?'manual_collection':'task_generation',job_status:'succeeded',knowledge_base_version:null,created_at:'2026-09-21T10:00:00+08:00',task_count:1,apps:['示例App'],filename:`collection-batch-${id}.xlsx`,download_url:'',warnings:id==='manual-one'?[{sheet:'Sheet1',row:2,field:'一级场景、二级场景',message:'场景缺失，后续汇总显示为未分类'}]:[],snapshot:{tasks:[]}})
async function main() {
 const output = await fs.mkdtemp(path.join(os.tmpdir(),'adf-phone-factory-browser-'))
 const server=http.createServer(async(req,res)=>{try{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);let file=path.resolve(dist,'.'+pathname);if(!file.startsWith(dist+path.sep)||!path.extname(file))file=path.join(dist,'index.html');res.setHeader('Content-Type',({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');res.end(await fs.readFile(file))}catch{res.statusCode=404;res.end('missing')}})
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
 const base='http://127.0.0.1:'+server.address().port
 const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true})
 const page=await browser.newPage({viewport:{width:1560,height:1080},serviceWorkers:'block'})
 const errors=[],calls=[],batches=[batch('generated-one')]
 let state={phones:['phone-1','phone-2'],apps:['示例App','另一个App'],phoneApps:[{phone_id:'phone-1',app:'示例App',status:'空闲'},{phone_id:'phone-1',app:'另一个App',status:'空闲'},{phone_id:'phone-2',app:'示例App',status:'空闲'}],vla:['http://mock-vla:8000/v1'],tasks:[]}
 let uploadAttempts=0,generateAttempts=0,deleteAttempts=0,runs=[],evalRuns=[]
 page.on('pageerror',error=>errors.push(String(error)))
 try {
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url())
  if(url.origin!==base)return route.abort()
  if(!url.pathname.startsWith('/api/'))return route.continue()
  const method=route.request().method(),body=route.request().postDataJSON();calls.push({path:url.pathname,method,body,query:Object.fromEntries(url.searchParams)})
  const send=json=>route.fulfill({json})
  if(url.pathname.endsWith('/lifecycle'))return send({status:'active',batch_id:url.pathname.split('/')[3]})
  if(url.pathname==='/api/phone-factory/batches')return send({batches})
  const batchMatch=url.pathname.match(/^\/api\/phone-factory\/batches\/([^/]+)(\/workbook)?$/)
  if(batchMatch){if(batchMatch[2])return route.fulfill({body:'frozen workbook'});return send(batches.find(item=>item.batch_id===batchMatch[1]))}
  const api=url.pathname.match(/^\/api\/(phone-factory|model-iter)(\/.*)$/)
  if(!api)return send({})
  const evaluation=api[1]==='model-iter', action=api[2]
  const modeState=()=>evaluation?{...copy(state),tasks:state.tasks.map(task=>({...task,status:evalRuns.length?'运行中':'未运行'}))}:copy(state)
  if(action==='/state')return send(modeState())
  if(action==='/config')return send({sampling_enabled:false,temperature:0.7,top_p:0.85,use_experience_lib:false})
  if(action==='/tasks'&&method==='POST'){
   if(!body.source_batch_id&&++uploadAttempts===1)return route.fulfill({status:502,json:{error:'模拟上传响应丢失，请重试'}})
   const id=body.source_batch_id||'manual-one',filename=batch(id).filename
   if(!state.tasks.some(item=>item.filename===filename)){
    if(!batches.some(item=>item.batch_id===id))batches.push(batch(id))
    state.tasks.push({filename,description:body.description,status:'未运行',source_batch_id:id})
   }
   return send({...modeState(),imported_task:{filename,source_batch_id:id,warnings:batch(id).warnings}})
  }
  if(action==='/remote/status')return send({ok:true,statuses:body.phones.map(id=>({phone_id:id,status:id==='phone-1'?'运行中':'空闲'}))})
  if(action==='/remote/adb-devices')return send({ok:true,devices:state.phones.concat('phone-3').map((id,i)=>({serial:id,model:`模拟手机 ${id}`,battery:75-i}))})
  if(action==='/remote/monitor'){
   await new Promise(resolve=>setTimeout(resolve,80))
   return send({ok:true,screenshot:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwZkAAAAASUVORK5CYII=',log:`${body.phone_id} 的模拟采集日志`,running:false,device_size:{width:1080,height:2400}}).catch(()=>{})
  }
  if(action==='/remote/start-run'){
   if(!evaluation&&++generateAttempts===1)return route.fulfill({status:502,json:{error:'模拟网络响应丢失，请重试'}})
   if(evaluation)evalRuns=[{run_id:'eval-1',collection_run_id:'eval-1',run_mode:'modeliter',status:'running',filename:body.filename,vla:body.vla,created_at:'2026-09-21T10:00:00Z',errors:[]}]
   else {runs=[{collection_run_id:'cr-one',batch_id:'manual-one',status:'running',transfer_status:'waiting'}];state.tasks.find(item=>item.filename===body.filename).status='运行中'}
   return send({ok:true,message:evaluation?'评估已下发':'采集已下发',collection_run_id:evaluation?'eval-1':'cr-one'})
  }
  if(action==='/collection-runs')return send({runs})
  if(action==='/runs')return send({runs:evalRuns})
  if(action==='/collection-runs/cr-one/sync'){runs=[{collection_run_id:'cr-one',batch_id:'manual-one',status:'completed',trajectory_count:2}];return send(runs[0])}
  if(action==='/remote/del-phone'){
   if(++deleteAttempts===1)return route.fulfill({status:502,json:{error:'模拟停止设备失败'}})
   state.phones=state.phones.filter(id=>id!==body.phone_id);state.phoneApps=state.phoneApps.filter(item=>item.phone_id!==body.phone_id)
   return send({ok:true,state:modeState()})
  }
  if(action==='/reports')return send({ok:true,folders:[{index:1,dir_name:'old-folder',run_id:'eval-report-1',files:[{name:'评估结果.xlsx',size:1024,file_id:'report-1'}]}]})
  if(action==='/report-download')return route.fulfill({body:'frozen evaluation report'})
  return route.fulfill({status:404,json:{error:'unexpected mock endpoint '+action}})
 })
 await page.goto(base+'/collection/phone-factory')
 await page.getByRole('heading',{name:'手机工厂采集',exact:true}).waitFor()
 await page.getByPlaceholder('任务描述').fill('手工采集任务')
 await page.locator('input[type=file]').setInputFiles({name:'manual.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:Buffer.from('fixture workbook bytes')})
 await page.locator('.traj-panel .row-form').filter({has:page.getByPlaceholder('任务描述')}).getByRole('button',{name:'新增',exact:true}).click()
 await page.getByText('模拟上传响应丢失，请重试',{exact:true}).waitFor()
 await page.locator('.traj-panel .row-form').filter({has:page.getByPlaceholder('任务描述')}).getByRole('button',{name:'新增',exact:true}).click()
 await page.locator('.batch-summary').waitFor()
 const uploads=calls.filter(item=>item.path==='/api/phone-factory/tasks'&&!item.body.source_batch_id)
 assert.equal(uploads.length,2);assert.ok(uploads[0].body.request_id);assert.deepEqual(uploads[0].body,uploads[1].body)
 assert.match(await page.locator('.batch-summary').innerText(),/来源：手动上传/)
 assert.match(await page.getByTestId('collection-classification-warning').innerText(),/1 条分类提示.*第 2 行.*未分类/)
 assert.equal(await page.locator('.batch-summary').getByRole('link',{name:'前往预处理'}).getAttribute('href'),'/collection/tree-building?batch_id=manual-one')
 await page.locator('.batch-summary').getByRole('button',{name:'定制运行',exact:true}).click()
 const dialog=page.getByRole('dialog',{name:'选择手机运行'})
 await dialog.locator('.el-select').nth(1).click()
 await page.getByRole('option',{name:'示例App',exact:true}).click()
 await dialog.locator('.el-switch').first().click()
 await dialog.getByRole('spinbutton').first().fill('0.35')
 await dialog.getByRole('button',{name:'确定',exact:true}).click()
 await page.getByText('模拟网络响应丢失，请重试',{exact:true}).waitFor()
 await dialog.getByRole('button',{name:'确定',exact:true}).click()
 await dialog.waitFor({state:'hidden'})
 const submissions=calls.filter(item=>item.path==='/api/phone-factory/remote/start-run')
 assert.equal(submissions.length,2);assert.deepEqual(submissions[0].body,submissions[1].body)
 assert.equal(submissions[1].body.config.temperature,0.35);assert.equal(submissions[1].body.config.sampling_enabled,true);assert.equal(submissions[1].body.vla,'http://mock-vla:8000/v1')
 assert.equal(calls.filter(item=>item.path.endsWith('/tasks/start')).length,0)
 await page.getByRole('button',{name:'重试回传',exact:true}).waitFor({timeout:15000})
 await page.getByRole('button',{name:'重试回传',exact:true}).click()
 await page.getByText('2 条轨迹',{exact:true}).waitFor()
 await page.screenshot({path:path.join(output,'collection.png'),fullPage:true})
 const phoneTable=page.locator('.panel').first().locator('.el-table')
 await phoneTable.getByRole('button',{name:'删除手机',exact:true}).first().click()
 await page.getByRole('button',{name:'停止并删除',exact:true}).click()
 await page.getByText('模拟停止设备失败',{exact:true}).waitFor()
 assert.equal(await phoneTable.locator('.el-table__row').filter({hasText:'phone-1'}).count(),2)
 await phoneTable.getByRole('button',{name:'删除手机',exact:true}).first().click()
 await page.getByRole('button',{name:'停止并删除',exact:true}).click()
 await page.waitForFunction(()=>!document.querySelector('.panel .el-table')?.textContent.includes('phone-1'))
 assert.equal(state.phoneApps.some(row=>row.phone_id==='phone-1'),false)
 await page.goto(base+'/phone-factory')
 await page.getByRole('button').filter({hasText:'模拟手机 phone-2'}).click()
 await page.getByTestId('phone-live-log').filter({hasText:'phone-2 的模拟采集日志'}).waitFor()
 await page.getByTestId('phone-live-screen').waitFor()
 await page.screenshot({path:path.join(output,'monitor.png'),fullPage:true})
 await page.getByRole('dialog').getByLabel('Close this dialog').click()
 await page.getByRole('button').filter({hasText:'模拟手机 phone-3'}).click()
 await page.getByTestId('phone-live-log').filter({hasText:'phone-3 的模拟采集日志'}).waitFor()
 assert.equal((await page.getByTestId('phone-live-log').innerText()).includes('phone-2'),false)
 await page.goto(base+'/model-iteration-evaluation')
 await page.getByRole('heading',{name:'模型迭代评估',exact:true}).waitFor()
 assert.equal(await page.getByText('采集任务批次',{exact:true}).count(),0)
 assert.equal(await page.getByRole('link',{name:'前往预处理'}).count(),0)
 await page.getByRole('button',{name:'开始运行',exact:true}).click()
 await page.getByText('评估已下发',{exact:true}).waitFor()
 assert.equal(calls.filter(item=>item.path==='/api/model-iter/remote/start-run').at(-1).body.run_mode,'modeliter')
 await page.getByRole('button',{name:'下载报告',exact:true}).waitFor()
 const downloadPromise=page.waitForEvent('download')
 await page.getByRole('button',{name:'下载报告',exact:true}).click()
 const download=await downloadPromise
 assert.equal(download.suggestedFilename(),'评估结果.xlsx')
 assert.deepEqual(calls.filter(item=>item.path==='/api/model-iter/report-download').at(-1).query,{run_id:'eval-report-1',file_id:'report-1'})
 await page.screenshot({path:path.join(output,'evaluation.png'),fullPage:true})
 await page.setViewportSize({width:390,height:900})
 await page.screenshot({path:path.join(output,'evaluation-mobile.png'),fullPage:true})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),true)
 assert.deepEqual(errors,[])
 const result={passed:true,cases:['manual upload retry identity','manual batch + preprocessing link','per-device VLA/config','network retry retains request_id','no late tasks/start mutation','result sync','delete failure preserves associations','delete success removes all associations','real monitor fixture and device switch','evaluation isolated from production','stable report download'],apiCalls:calls.length,errors,output}
 await fs.writeFile(path.join(output,'result.json'),JSON.stringify(result,null,2))
 console.log(JSON.stringify(result))
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
}
main().catch(error=>{console.error(error);process.exitCode=1})
