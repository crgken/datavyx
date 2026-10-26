// Actual browser, HTTP and disposable Neo4j. No request interception.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
 const root=path.resolve(__dirname,'..'),info=JSON.parse(fs.readFileSync(path.join(root,'artifacts/review-ui-server.json'))),checks=[],errors=[];
 const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage({viewport:{width:1440,height:1080}});
 page.on('pageerror',e=>errors.push(e.message));
 function check(name,value){checks.push({name,passed:!!value});console.log((value?'PASS ':'FAIL ')+name);if(!value)throw new Error(name);}
 async function get(url){return page.evaluate(async u=>(await(await fetch(u)).json()).data,url);}
 async function open(id){await page.locator('#object-id').fill(id);await page.locator('#open-object button').click();await page.locator('#detail').getByText(id,{exact:false}).first().waitFor();}
 async function previewAction(label){await page.getByRole('button',{name:label,exact:true}).click();await page.locator('dialog').waitFor();}
 async function freeze(){await page.locator('dialog').getByRole('button',{name:'生成预览',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'visible'});await page.locator('dialog').waitFor({state:'detached'});}
 async function save(){await page.getByRole('button',{name:'确认保存',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'hidden'});await page.waitForFunction(()=>document.querySelector('#detail h3')!==null);}
 try{
  await page.goto('http://127.0.0.1:8097/');await page.getByRole('tab',{name:'对象浏览'}).waitFor();
  const health=await get('/api/health');check('buttons_without_model_configuration',health.model_configured===false);
  await open(info.ids.p);await previewAction('确认对象');check('confirm_dialog_explains_scope',(await page.locator('dialog').textContent()).includes('其他待确认内容保持原状态'));
  await freeze();check('preview_has_action_and_impact',(await page.locator('#preview').textContent()).includes('本次操作'));
  const prior=await get('/api/detail?id='+info.ids.p);check('preview_does_not_confirm',prior.object.confirmed===false);
  await page.reload();await page.getByRole('button',{name:'确认保存',exact:true}).waitFor();check('review_preview_survives_reload',await page.locator('#preview-panel').isVisible());
  // There is no selected object after reload; save then explicitly open it.
  await page.getByRole('button',{name:'确认保存',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'hidden'});await open(info.ids.p);
  let detail=await get('/api/detail?id='+info.ids.p);check('object_only_does_not_confirm_relation',detail.object.confirmed&&!detail.rows[0].confirmed&&detail.rows[0].review_status==='pending');
  await previewAction('确认此条');check('optional_endpoint_confirmation_unselected',await page.locator('dialog input[type=checkbox]').count()===1&&!await page.locator('dialog input[type=checkbox]').isChecked());
  await freeze();await save();detail=await get('/api/detail?id='+info.ids.p);check('fact_confirmation_shows_dependency_block',detail.rows[0].review_status==='confirmed'&&detail.rows[0].effective_status==='blocked');
  check('block_reason_visible',(await page.locator('#detail').textContent()).includes('北岸站身份或类型待确认'));
  await open(info.ids.s);await previewAction('确认对象');await freeze();await save();
  await open(info.ids.p);detail=await get('/api/detail?id='+info.ids.p);check('dependency_resolution_activates_relation',detail.rows[0].effective_status==='effective');
  await page.getByRole('tab',{name:'星状图'}).click();await page.locator('#star-canvas svg').waitFor();check('effective_relation_in_star',(await page.locator('#star-status').textContent()).includes('关系总量 1'));
  await page.getByRole('tab',{name:'对象浏览'}).click();await previewAction('撤回此条');await freeze();await save();
  check('retraction_keeps_object',(await get('/api/detail?id='+info.ids.p)).object.archived===false);
  await page.locator('#detail').getByRole('checkbox',{name:'查看历史'}).check();await page.getByRole('button',{name:'重新登记',exact:true}).waitFor();
  check('retracted_history_visible',(await page.locator('#detail').textContent()).includes('已撤回'));
  await previewAction('重新登记');await freeze();await save();check('restated_fact_visible',(await get('/api/detail?id='+info.ids.p)).total===1);
  await page.locator('#detail summary').click();await previewAction('归档对象');await freeze();check('archive_preview_states_exclusion',(await page.locator('#preview').textContent()).includes('已归档'));await save();
  check('archived_banner_and_restore',await page.getByRole('button',{name:'恢复对象',exact:true}).isVisible());
  check('archive_hides_default_list',!(await get('/api/objects?limit=100')).rows.some(o=>o.id===info.ids.p));
  await page.getByRole('tab',{name:'星状图'}).click();await page.locator('#star-canvas svg').waitFor();
  check('archived_object_not_star_center',!(await page.locator('#star-status').textContent()).includes('中心：周工'));
  check('archived_object_not_star_picker',!(await page.locator('#star-object option').allTextContents()).some(t=>t.includes('周工')));
  await page.getByRole('tab',{name:'对象浏览'}).click();
  await page.locator('#include-archived').check();await page.getByRole('button',{name:'人员',exact:true}).click();await page.getByRole('button',{name:'周工',exact:true}).waitFor();check('archive_filter_finds_object',await page.getByRole('button',{name:'周工',exact:true}).isVisible());
  await page.getByRole('button',{name:'周工',exact:true}).click();await previewAction('恢复对象');await freeze();await save();
  detail=await get('/api/detail?id='+info.ids.p+'&history=true');check('restore_preserves_withdrawn_version',detail.rows.some(f=>!f.active&&f.effective_status==='retracted')&&detail.rows.some(f=>f.active));
  await open(info.ids.d);await page.locator('#detail summary').click();await previewAction('更正类型');
  check('type_confirmation_optional_unselected',!await page.locator('dialog input[type=checkbox]').last().isChecked());
  await page.locator('dialog').getByRole('textbox',{name:'正确类型'}).fill('设备');await page.locator('dialog input[type=checkbox]').first().check();await freeze();
  check('type_preview_before_after',(await page.locator('#preview').textContent()).includes('机构 → 设备'));await save();
  detail=await get('/api/detail?id='+info.ids.d);check('type_correction_preserves_pending_identity',detail.object.kind==='设备'&&!detail.object.confirmed&&detail.object.id===info.ids.d);
  await previewAction('确认对象');await freeze();await page.getByRole('button',{name:'取消本次变更',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'hidden'});check('cancel_has_no_effect',!(await get('/api/detail?id='+info.ids.d)).object.confirmed);
  check('no_fabricated_user_chat',(await get('/api/messages')).filter(m=>m.role==='user').length===1);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(root,'artifacts/review-mobile.png'),fullPage:true});
  check('mobile_no_document_overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await previewAction('确认对象');check('mobile_dialog_fits',await page.locator('dialog').evaluate(e=>e.getBoundingClientRect().width<=innerWidth));await page.locator('dialog').getByRole('button',{name:'取消',exact:true}).click();
  await page.setViewportSize({width:1440,height:1080});await page.screenshot({path:path.join(root,'artifacts/review-desktop.png'),fullPage:true});
  check('no_javascript_errors',errors.length===0);check('no_global_error',!await page.locator('#error').isVisible());
 }catch(e){checks.push({name:'execution_complete',passed:false,error:e.message});console.error(e.message);await page.screenshot({path:path.join(root,'artifacts/review-ui-failure.png'),fullPage:true});}
 finally{fs.writeFileSync(path.join(root,'artifacts/review-ui-acceptance.json'),JSON.stringify({passed:checks.filter(c=>c.passed).length,failed:checks.filter(c=>!c.passed).length,checks,errors},null,2));await browser.close();}
 process.exitCode=checks.some(c=>!c.passed)?1:0;
})();
