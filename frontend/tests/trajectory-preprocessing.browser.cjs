// Built frontend only. Every API and image request is mocked; no backend, model or phone is contacted.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const http = require('node:http')
const dist = path.resolve(__dirname, '../dist')
const artifact = (id, stage, version) => ({ batch_id:id, stage, version, created_at:'2026-09-15T20:00:00', files:[
  {name:'result.json',kind:'json'}, {name:'result.xlsx',kind:'excel'}] })
const job = (id, status = 'running') => ({job_id:'pre-'+id,batch_id:id,status,stage:status==='running'?'annotating':status,completed_steps:status==='succeeded'?2:1,total_steps:2,current_task:'same',current_trajectory:'same-1',current_step:1,percent:status==='succeeded'?100:50,error:status==='failed'?'模拟模型超时，可重试':null,artifacts:status==='succeeded'?[artifact(id,'01_conversion','conversion-v1'),artifact(id,'02_annotation','v1')]:[],annotation_version:status==='succeeded'?'v1':null})
const batch = (id, version='v1') => ({batch_id:id,kind:id==='a'?'existing_trajectories':'task_generation',label:id==='a'?'已有轨迹验证批次':'采集任务批次 '+id,task_count:1,ready_trajectory_count:version?1:0,ready_step_count:version?2:0,collection_status:version?'ready':'waiting',preprocessing_status:version?'succeeded':'not_started',latest_job:null,annotation_version:version,artifacts:version?[artifact(id,'01_conversion','conversion-v1'),artifact(id,'02_annotation',version)]:[],can_start:false,reason:version?'当前版本已完成，可检查动作框后建树':'尚无完整原始轨迹，请等待采集文件就绪'})
const task = id => ({task_id:'same',goal:'批次 '+id+' 的独立任务内容',warning:'',first_trajectory:'same-1',trajectory_count:1,step_count:2,annotated:true})
const record = id => ({trajectory_id:'same-1',step_count:2,steps:[1,2].map(step=>({step,excel_row:step+1,image:'same/same-1/'+step+'.png',image_url:'',xml:'',action_text:'点击 '+id,action:{action:'click',coordinate:[500,500]},action_summary:'批次 '+id+' 步骤 '+step,actions_box:'<bbox>[40,40,180,180]</bbox>'}))})
async function main() {
  const output = await fs.mkdtemp(path.join(os.tmpdir(),'adf-preprocessing-browser-'))
  const server = http.createServer(async (req,res)=>{
    try {
      const requested = decodeURIComponent(new URL(req.url,'http://localhost').pathname)
      let file = path.join(dist, requested)
      if (!path.resolve(file).startsWith(dist)) throw new Error('outside dist')
      if (!path.extname(requested)) file=path.join(dist,'index.html')
      const body = await fs.readFile(file)
      res.setHeader('Content-Type',({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream')
      res.end(body)
    } catch { res.statusCode=404;res.end('missing') }
  })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const base = 'http://127.0.0.1:'+server.address().port
  const browser = await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'msedge',headless:true})
  const results=[]
  try {
    for (const width of [1440,1024,390]) {
      const page = await browser.newPage({viewport:{width,height:950},serviceWorkers:'block',hasTouch:width===390})
      const errors=[],calls=[]
      let rows=[batch('a'),batch('b'),batch('waiting',null),{...batch('ready',null),collection_status:'ready',ready_trajectory_count:1,ready_step_count:2,can_start:true,reason:null},{...batch('failed',null),collection_status:'ready',preprocessing_status:'failed',latest_job:job('failed','failed')}]
      let build=null, activeJobs={}, hold=false, releaseOld, signalOld
      page.on('pageerror',error=>errors.push(String(error)))
      await page.route('**/*',async route=>{
        const url=new URL(route.request().url())
        if (url.origin!==base) return route.abort()
        if (!url.pathname.startsWith('/api/')) return route.continue()
        const method=route.request().method(),body=route.request().postDataJSON()
        calls.push({path:url.pathname,query:Object.fromEntries(url.searchParams),method,body})
        const send=json=>route.fulfill({json})
        if (/^\/api\/data-batches\/[^/]+\/lifecycle$/.test(url.pathname)) return send({ batch_id:url.pathname.split('/')[3],status:'active',published_at:null,release_id:null })
        if (url.pathname==='/api/trajectory-preprocessing/batches') return send({batches:rows})
        if (url.pathname==='/api/trajectory-preprocessing/jobs'&&method==='POST') {
          activeJobs[body.batch_id]=job(body.batch_id)
          if(body.batch_id==='ready') activeJobs.ready={...activeJobs.ready,stage:'scanning',total_steps:0,completed_steps:0,percent:0}
          rows=rows.map(row=>row.batch_id===body.batch_id?{...row,latest_job:activeJobs[body.batch_id],preprocessing_status:'running',can_start:false}:row)
          return send(activeJobs[body.batch_id])
        }
        if (/^\/api\/trajectory-preprocessing\/jobs\/pre-[^/]+\/retry$/.test(url.pathname)) {
          activeJobs.failed=job('failed');rows=rows.map(row=>row.batch_id==='failed'?{...row,latest_job:activeJobs.failed,preprocessing_status:'running'}:row)
          return send(activeJobs.failed)
        }
        if (url.pathname.startsWith('/api/trajectory-preprocessing/jobs/pre-')) {
          const id=url.pathname.split('pre-')[1]
          return send(activeJobs[id]||job(id,'succeeded'))
        }
        if (url.pathname==='/api/tree-builds'&&method==='GET') return send({jobs:build?[build]:[]})
        if (url.pathname==='/api/tree-builds'&&method==='POST') {
          build={job_id:'tree-a',batch_id:body.batch_id,annotation_version:rows.find(row=>row.batch_id===body.batch_id)?.annotation_version,status:'running',stage:'classifying',task_ids:body.task_ids,created_at:'2026-09-15T20:00:00',classified_steps:1,total_steps:2,percent:50,error:null,run_id:null}
          return send(build)
        }
        if (url.pathname==='/api/tree-builds/tree-a') return send(build)
        if (url.pathname==='/api/tasks') {
          assert.ok(url.searchParams.get('annotation_version'));assert.ok(url.searchParams.get('batch_id'))
          return send({tasks:[task(url.searchParams.get('batch_id'))]})
        }
        if (url.pathname==='/api/tasks/same/trajectories') return send({task:task(url.searchParams.get('batch_id')),trajectories:[{trajectory_id:'same-1',source_trajectory_id:'原始轨迹-'+url.searchParams.get('batch_id'),collected_at:'2026-09-15T18:20:30+08:00',step_count:2}]})
        if (url.pathname==='/api/tasks/same/trajectories/same-1') {
          if (hold&&url.searchParams.get('batch_id')==='a') await new Promise(resolve=>{releaseOld=resolve;signalOld()})
          return send({trajectory:record(url.searchParams.get('batch_id'))})
        }
        if (url.pathname.endsWith('/bbox')&&method==='PATCH') {
          assert.equal(body.batch_id,'a');assert.equal(body.annotation_version,'v1')
          rows=rows.map(row=>row.batch_id==='a'?{...row,annotation_version:'v2',artifacts:[artifact('a','01_conversion','conversion-v1'),artifact('a','02_annotation','v2')]}:row)
          return send({actions_box:'<bbox>'+JSON.stringify(body.bbox)+'</bbox>',annotation_version:'v2'})
        }
        if (url.pathname.startsWith('/api/assets/')) {
          assert.ok(url.searchParams.get('batch_id'));assert.ok(url.searchParams.get('annotation_version'))
          return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="480"><rect width="320" height="480" fill="#f0fdfa"/><rect x="40" y="40" width="140" height="140" fill="#99f6e4"/><text x="45" y="115" font-size="24">Mock screen</text></svg>'})
        }
        if (url.pathname.includes('/api/data-batches/')) return route.fulfill({contentType:'application/octet-stream',headers:{'Content-Disposition':'attachment; filename="result.xlsx"'},body:'mock frozen bytes'})
        if (url.pathname==='/api/phone-factory/state') return send({phones:['phone'],apps:['App'],phoneApps:[],vla:[],tasks:[]})
        if (url.pathname==='/api/phone-factory/remote/status') return send({ok:true,statuses:[]})
        if (url.pathname==='/api/phone-factory/remote/adb-devices') return send({ok:true,devices:[]})
        if (url.pathname==='/api/phone-factory/config') return send({sampling_enabled:false,temperature:0.5,top_p:0.9,use_experience_lib:false})
        if (url.pathname==='/api/task-generation/collection-batches'||url.pathname==='/api/phone-factory/batches') return send({batches:[{schema_version:1,batch_id:'b',source_job_id:'b',kind:'task_generation',job_status:'succeeded',knowledge_base_version:null,created_at:'2026-09-15T20:00:00',task_count:1,apps:['App'],filename:'collection-batch-b.xlsx'}]})
        if (url.pathname.startsWith('/api/task-generation/collection-batches/')||url.pathname.startsWith('/api/phone-factory/batches/')) {
          const id=url.pathname.split('/').at(-1)
          return send({schema_version:1,batch_id:id,source_job_id:id,kind:'task_generation',job_status:'succeeded',knowledge_base_version:null,created_at:'2026-09-15T20:00:00',task_count:1,apps:['App'],filename:'collection-batch-'+id+'.xlsx',snapshot:{tasks:[{task_id:'same',collection_case_id:'CASE-1',task:'采集源任务 '+id,app:'App'}]}})
        }
        if (url.pathname==='/api/phone-factory/collection-runs') {
          const id=url.searchParams.get('batch_id')
          return send({runs:id==='waiting'?[]:[{collection_run_id:'cr-'+id,batch_id:id,status:'completed',created_at:'2026-09-15T18:00:00+08:00',completed_at:'2026-09-15T18:20:30+08:00',batch_tasks:{'CASE-1':{task_id:'same',collection_case_id:'CASE-1',task:'采集源任务 '+id,app:'App'}},trajectories:[{task_id:'same',collection_case_id:'CASE-1',collection_run_id:'cr-'+id,source_trajectory_id:'原始轨迹-'+id,relative_dir:'same/same-1',collected_at:'2026-09-15T18:20:30+08:00'}],errors:[],dispatch_error:null}]})
        }
        errors.push('unmocked '+method+' '+url.pathname)
        return route.fulfill({status:501,json:{detail:'Unmocked API blocked'}})
      })
      const select=id=>page.getByRole('button',{name:'选择批次 '+id,exact:true}).click()
      const openTask=()=>page.locator('.task-title').click()
      const openTrajectory=()=>page.locator('.trajectory-title').click()
      await page.goto(base+'/collection/tree-building')
      await page.getByRole('button',{name:'选择批次 a',exact:true}).waitFor()
      assert.equal(await page.locator('.batch-item.selected').count(),0)
      assert.equal(calls.filter(call=>call.path==='/api/tasks').length,0)
      await select('waiting')
      assert.equal(await page.getByRole('button',{name:'开始预处理',exact:true}).isDisabled(),true)
      await page.locator('.source-task>summary').click()
      await page.getByText('暂无已就绪原始轨迹',{exact:true}).waitFor()
      await select('a');await openTask();await openTrajectory()
      await page.locator('.action-image img').waitFor()
      assert.ok((await page.locator('.trajectory-title').innerText()).includes('原始轨迹-a'))
      assert.ok((await page.locator('.trajectory-title').innerText()).includes('2026-09-15 18:20:30'))
      await page.getByRole('button',{name:'修改 bbox',exact:true}).click()
      await openTask();await page.getByRole('dialog').waitFor();await page.keyboard.press('Escape')
      assert.equal(await page.locator('.action-image__toolbar').isVisible(),true)
      await openTrajectory();await page.getByRole('dialog').waitFor();await page.keyboard.press('Escape')
      assert.equal(await page.locator('.action-image__toolbar').isVisible(),true)
      await select('b')
      await page.getByRole('dialog').waitFor()
      await page.keyboard.press('Escape')
      assert.equal(await page.getByRole('button',{name:'选择批次 a',exact:true}).getAttribute('aria-expanded'),'true')
      assert.equal(await page.locator('.action-image__toolbar').isVisible(),true)
      await select('b');await page.getByRole('button',{name:'放弃修改',exact:true}).click()
      assert.equal(await page.getByRole('button',{name:'选择批次 b',exact:true}).getAttribute('aria-expanded'),'true')
      assert.equal(await page.locator('.trajectory-explorer').count(),0)
      await select('a');await openTask();await openTrajectory();await page.getByRole('button',{name:'修改 bbox',exact:true}).click()
      await page.getByRole('button',{name:'全选可用任务',exact:true}).click()
      await page.getByRole('button',{name:'提交轨迹树构建',exact:true}).click()
      await page.getByRole('button',{name:'保存并继续',exact:true}).click()
      await page.getByLabel('建树进度').waitFor()
      const buildCall=calls.find(call=>call.path==='/api/tree-builds'&&call.method==='POST')
      assert.deepEqual(buildCall.body,{task_ids:['same'],batch_id:'a'})
      assert.ok(calls.some(call=>call.path.startsWith('/api/assets/')&&call.query.annotation_version==='v2'))
      const artifactLink=page.getByLabel('阶段文件').locator('a').filter({hasText:'Excel'}).last()
      assert.ok((await artifactLink.getAttribute('href')).includes('/02_annotation/v2/'))
      const downloadPromise=page.waitForEvent('download');await artifactLink.click();await downloadPromise
      await page.reload();await page.getByLabel('建树进度').waitFor()
      assert.equal(calls.filter(call=>call.path==='/api/tree-builds'&&call.method==='POST').length,1)
      await page.screenshot({path:path.join(output,width+'-ready.png'),fullPage:true,animations:'disabled'})
      await select('ready');await page.getByRole('button',{name:'开始预处理',exact:true}).click()
      await page.getByLabel('预处理进度').waitFor()
      await page.getByText('正在统计步骤',{exact:true}).waitFor()
      assert.equal((await page.getByLabel('预处理进度').innerText()).includes('0 / 0'),false)
      const starts=calls.filter(call=>call.path==='/api/trajectory-preprocessing/jobs'&&call.method==='POST').length
      await page.reload();await page.getByLabel('预处理进度').waitFor()
      assert.equal(calls.filter(call=>call.path==='/api/trajectory-preprocessing/jobs'&&call.method==='POST').length,starts)
      activeJobs.ready=job('ready','succeeded')
      rows=rows.map(row=>row.batch_id==='ready'?{...batch('ready'),latest_job:activeJobs.ready}:row)
      await page.getByRole('button',{name:'全选可用任务',exact:true}).waitFor()
      await select('failed');await page.getByText('模拟模型超时，可重试',{exact:true}).waitFor()
      await page.getByRole('button',{name:'重试预处理',exact:true}).click()
      await page.getByRole('button',{name:'预处理中',exact:true}).waitFor()
      assert.equal(await page.locator('.batch-item.selected .batch-status').innerText(),'处理中')
      assert.equal(calls.filter(call=>call.path.endsWith('/pre-failed/retry')).length,1)
      rows=rows.map(row=>row.batch_id==='failed'?{...row,latest_job:job('failed','failed'),preprocessing_status:'failed',can_start:true}:row)
      await page.getByRole('button',{name:'刷新批次',exact:true}).click()
      await page.getByRole('button',{name:'开始新预处理',exact:true}).click()
      assert.ok(calls.some(call=>call.path==='/api/trajectory-preprocessing/jobs'&&call.body?.batch_id==='failed'))
      await page.screenshot({path:path.join(output,width+'-progress.png'),fullPage:true,animations:'disabled'})
      await page.goto(base+'/collection/phone-factory?collection_batch_id=b')
      await page.getByRole('link',{name:'前往预处理',exact:true}).waitFor()
      await page.getByRole('link',{name:'前往预处理',exact:true}).click()
      await page.waitForURL('**/collection/tree-building?batch_id=b')
      await page.getByRole('button',{name:'选择批次 b',exact:true}).waitFor()
      assert.equal(calls.filter(call=>call.path.includes('/remote/start-run')).length,0)
      await select('a');await openTask()
      const oldStarted=new Promise(resolve=>{signalOld=resolve});hold=true
      await openTrajectory();await oldStarted
      await select('b');hold=false;releaseOld()
      await openTask();await openTrajectory()
      await page.getByText('批次 b 步骤 1',{exact:true}).waitFor()
      assert.equal(await page.getByText('批次 a 步骤 1',{exact:true}).count(),0)
      await page.screenshot({path:path.join(output,width+'-batch.png'),fullPage:true,animations:'disabled'})
      const overflow=await page.evaluate(()=>({width:innerWidth,body:document.documentElement.scrollWidth}))
      assert.ok(overflow.body<=overflow.width+1,JSON.stringify(overflow))
      assert.deepEqual(errors,[])
      results.push({width,passed:true,api_calls:calls.length})
      await page.close()
    }
    await fs.writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,results},null,2))
    console.log(JSON.stringify({output,results},null,2))
  } finally { await browser.close();await new Promise(resolve=>server.close(resolve)) }
}
main().catch(error=>{console.error(error);process.exitCode=1})
