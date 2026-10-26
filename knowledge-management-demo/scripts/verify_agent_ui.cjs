// Real browser against isolated port 8097. No interception or mock responses.
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const root=path.resolve(__dirname,'..'),checks=[],errors=[];
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 page.on('pageerror',e=>errors.push(e.message));
 function check(name,value){checks.push({name,passed:!!value});console.log((value?'PASS ':'FAIL ')+name);if(!value)throw new Error(name);}
 async function response(){
  await Promise.race([
   page.locator('#send').waitFor({state:'attached'}).then(()=>page.waitForFunction(()=>!document.querySelector('#send').disabled,{},{timeout:240000})),
   page.locator('#error').waitFor({state:'visible',timeout:240000}).then(async()=>{throw new Error(await page.locator('#error-text').textContent());})]);
 }
 async function chat(text){await page.locator('#message').fill(text);await page.locator('#send').click();await response();}
 try{
  await page.goto('http://127.0.0.1:8097/');
  await page.getByRole('heading',{name:'从一句话开始'}).waitFor();
  check('sibling_tabs',await page.getByRole('tab').count()===3);
  await chat('登记设备，编号 UI-991，只提供编号，不提供名称。');
  await page.locator('#preview-panel').waitFor({state:'visible'});
  check('identifier_preview_label',(await page.locator('#preview').textContent()).includes('UI-991'));
  check('new_topic_has_no_historical_correction',!(await page.locator('#preview').textContent()).includes('对象字段更正'));
  await page.reload();
  await page.locator('#preview-panel').waitFor({state:'visible'});
  check('server_restores_preview_after_reload',await page.getByRole('button',{name:'确认保存',exact:true}).isVisible());
  await page.getByRole('button',{name:'确认保存',exact:true}).click();
  await page.locator('#preview-panel').waitFor({state:'hidden',timeout:240000});
  check('confirmation_has_actual_receipt',(await page.locator('#messages').textContent()).includes('保存'));
  const objects=await page.evaluate(async()=> (await (await fetch('/api/objects?limit=100')).json()).data.rows);
  const equipment=objects.find(o=>o.identifier==='UI-991');
  check('canonical_name_remains_empty',equipment&&equipment.name==='');
  await page.getByRole('button',{name:'设备',exact:true}).click();
  await page.getByRole('button',{name:'UI-991',exact:true}).waitFor();
  check('identifier_only_browsable',await page.getByRole('button',{name:'UI-991',exact:true}).isVisible());
  await chat('望远镜之前误归到人员，实际上是设备，安装于观测站。');
  await page.locator('#preview-panel').waitFor({state:'visible'});
  check('type_before_after_preview',/对象字段更正|更正类型/.test(await page.locator('#preview').textContent())&&(await page.locator('#preview').textContent()).includes('人员'));
  await page.getByRole('button',{name:'确认保存',exact:true}).click();
  await page.locator('#preview-panel').waitFor({state:'hidden',timeout:240000});
  const telescope=objects.find(o=>o.name==='望远镜');
  await page.locator('#object-id').fill(telescope.id);
  await page.locator('#open-object').getByRole('button').click();
  await page.locator('#detail').getByRole('checkbox').check();
  await page.locator('#detail').getByText('操作历史',{exact:true}).waitFor();
  check('type_history_visible',(await page.locator('#detail').textContent()).includes('人员 → 设备'));
  await page.getByRole('tab',{name:'星状图'}).click();
  await page.locator('.star-node').first().waitFor();
  check('star_view_works',await page.locator('.star-node').count()>0);
  const count=await page.locator('.message.assistant').count();
  await page.evaluate(()=>api('/api/confirm',{draft_id:'invalid'}).catch(error));
  check('server_guard_in_global_region',await page.locator('#error').isVisible());
  check('system_error_not_chat',await page.locator('.message.assistant').count()===count);
  await page.screenshot({path:path.join(root,'artifacts','agent-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(root,'artifacts','agent-mobile.png'),fullPage:true});
  check('mobile_no_overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  check('no_javascript_errors',errors.length===0);
 }catch(e){await page.screenshot({path:path.join(root,'artifacts','agent-ui-failure.png'),fullPage:true});checks.push({name:'execution_complete',passed:false,error:e.message});throw e;}
 finally{fs.writeFileSync(path.join(root,'artifacts','agent-ui-report.json'),JSON.stringify({checks,errors},null,2));await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
