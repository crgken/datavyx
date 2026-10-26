// Read-only browser acceptance: never clicks confirmation or alters a record.
const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const root=path.resolve(__dirname,'..');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8096/');
    const request=JSON.parse(fs.readFileSync(path.join(root,'artifacts/live-bulk-preview-request.json'),'utf8').replace(/^\uFEFF/,''));
    let job;
    for(let i=0;i<120;i++){
      job=await page.evaluate(async id=>(await(await fetch('/api/import?id='+id)).json()).data,request.import_job.id);
      if(job.status==='failed'||job.status==='needs_input')throw new Error(job.error||JSON.stringify(job.questions));
      if(job.status==='ready')break;
      await page.waitForTimeout(1500);
    }
    if(job.status!=='ready')throw new Error('Preview not ready');
    await page.reload();
    await page.locator('.bulk-summary').waitFor({state:'visible',timeout:15000});
    const info=job.draft.bulk,draft=job.draft;
    const graph=await page.evaluate(async()=>(await(await fetch('/api/graph')).json()).data);
    const report={rows:info.row_count,objects:draft.objects.length,new_objects:draft.objects.filter(o=>o.new).length,
      relations:info.relation_count,values:info.value_count,unresolved:draft.objects.filter(o=>!o.resolved).length,
      coverage_rows:await page.locator('.bulk-summary tbody tr').count(),
      summary:await page.locator('.bulk-summary').innerText(),
      desktop_overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),
      canonical_objects_before_confirmation:graph.node_count,canonical_edges_before_confirmation:graph.edge_count,errors};
    await page.screenshot({path:path.join(root,'artifacts/bulk-live-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    report.mobile_overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    await page.screenshot({path:path.join(root,'artifacts/bulk-live-mobile.png'),fullPage:true});
    const compact={...report};delete compact.summary;
    fs.writeFileSync(path.join(root,'artifacts/bulk-live-ui-verification.json'),JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(compact));
    if(report.rows!==45||report.objects!==95||report.new_objects!==55||report.relations!==90||report.values!==45||report.unresolved||report.coverage_rows!==45||report.errors.length||report.desktop_overflow||report.mobile_overflow)throw new Error('UI acceptance failed');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
