// Read-only verification of the user's live page; never freezes or commits a draft.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path');
(async()=>{
 const root=path.resolve(__dirname,'..'),checks={},errors=[];
 const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage({viewport:{width:1440,height:1080}});
 page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto('http://127.0.0.1:8096/');await page.getByRole('tab',{name:'对象浏览'}).waitFor();
  const get=async url=>page.evaluate(async u=>(await(await fetch(u)).json()).data,url);
  const health=await get('/api/health');checks.model_configured=health.model_configured;
  const objects=await get('/api/objects?limit=100'),wang=objects.rows.find(o=>o.name==='王总');
  checks.objects_preserved=objects.total===8;checks.wang_still_pending=wang&&!wang.confirmed;
  await page.locator('#object-id').fill(wang.id);await page.locator('#open-object button').click();
  await page.locator('#detail').getByRole('heading',{name:'王总',exact:true}).waitFor();
  checks.confirm_object_available=await page.locator('#detail').getByRole('button',{name:'确认对象',exact:true}).isVisible();
  checks.confirm_fact_available=await page.locator('#detail').getByRole('button',{name:'确认此条',exact:true}).isVisible();
  checks.retract_fact_available=await page.locator('#detail').getByRole('button',{name:'撤回此条',exact:true}).isVisible();
  await page.locator('#detail summary').click();
  checks.reclassify_and_archive_available=await page.getByRole('button',{name:'更正类型',exact:true}).isVisible()&&await page.getByRole('button',{name:'归档对象',exact:true}).isVisible();
  await page.getByRole('button',{name:'确认对象',exact:true}).click();
  checks.current_type_visible_in_dialog=(await page.locator('dialog').textContent()).includes(wang.kind);
  await page.locator('dialog').getByRole('button',{name:'取消',exact:true}).click();
  await page.locator('#detail').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(root,'artifacts/live-review-desktop.png'),fullPage:true});
  checks.no_global_error=!await page.locator('#error').isVisible();checks.no_javascript_errors=errors.length===0;
  const current=await get('/api/detail?id='+wang.id);checks.read_only_no_confirmation=!current.object.confirmed;
  const report={url:'http://127.0.0.1:8096/',checks,errors,health};fs.writeFileSync(path.join(root,'docs/live-review-ui-verification.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));if(Object.values(checks).some(v=>!v))process.exitCode=1;
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
