// Real browser + local HTTP app. No network interception or mocked responses.
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const root=path.resolve(__dirname,'..');fs.mkdirSync(path.join(root,'artifacts'),{recursive:true});
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:1440,height:1050},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const checks=[];function check(name,ok){checks.push({name,passed:!!ok});if(!ok)throw new Error(name);}
 try{
  await page.goto('http://127.0.0.1:8096/');
  await page.getByRole('heading',{name:'从一句话开始'}).waitFor();
  check('initial_page',await page.title()==='Atlas Knowledge · 自然语言知识管理');
  await page.getByRole('button',{name:'合同',exact:true}).click();
  await page.getByRole('button',{name:'合同 A',exact:true}).waitFor();
  check('object_listing',await page.locator('#browser tbody tr').count()===3);
  await page.getByRole('button',{name:'合同 A',exact:true}).click();
  await page.locator('#detail').getByText('金额',{exact:true}).waitFor();
  check('detail_facts',await page.locator('#detail tbody tr').count()===3);
  await page.locator('#detail').getByRole('button',{name:'1 条来源',exact:true}).first().click();
  await page.locator('.source-area').getByRole('heading',{name:'来源原文'}).waitFor();
  check('source_visible',await page.locator('.source-item').count()===1);
  // Exercise real natural-language routing through the page's text input.
  await page.locator('#message').fill('目前记录了多少份合同？');
  await page.getByRole('button',{name:'发送'}).click();
  await Promise.race([page.locator('.stat-number').getByText('3',{exact:true}).waitFor({timeout:240000}),page.locator('#error').waitFor({state:'visible',timeout:240000}).then(async()=>{throw new Error(await page.locator('#error-text').textContent());})]);
  check('natural_query_rendered',await page.locator('.stat-number').textContent()==='3');
  await page.getByRole('button',{name:'查看参与 / 排除对象与来源 →'}).click();
  await page.locator('#audit tbody tr').first().waitFor();
  check('audit_participants',await page.locator('#audit tbody tr').count()===3);
  await page.locator('#message').fill('甲公司的合同 A 金额更正为 25 万元人民币。');
  await page.getByRole('button',{name:'发送'}).click();
  await Promise.race([page.getByRole('heading',{name:'待确认预览'}).waitFor({timeout:240000}),page.locator('#error').waitFor({state:'visible',timeout:240000}).then(async()=>{throw new Error(await page.locator('#error-text').textContent());})]);
  check('write_requires_preview',await page.getByRole('button',{name:'确认保存'}).isVisible());
  await page.getByRole('button',{name:'确认保存'}).click();
  await page.locator('#preview-panel').waitFor({state:'hidden'});
  await page.getByRole('button',{name:'合同',exact:true}).waitFor();
  check('confirmation_saved',await page.getByRole('button',{name:'合同',exact:true}).isVisible());
  const sum=await page.evaluate(async()=>{const r=await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'SUM',kind:'合同',predicate:'金额',unit:'CNY'})});return r.json();});
  check('confirmed_correction_sum',sum.data.sum==='650000');
  // A server failure must appear in the global region, never as a chat answer.
  const before=await page.locator('.message.assistant').count();
  await page.evaluate(()=>api('/api/objects?limit=0').catch(error));
  check('system_error_in_global_region',await page.locator('#error').isVisible());
  check('error_not_chat_message',await page.locator('.message.assistant').count()===before);
  await page.getByRole('button',{name:'关闭错误提示'}).click();
  await page.getByRole('tab',{name:'统计与复核'}).click();
  await page.screenshot({path:path.join(root,'artifacts','desktop-statistics.png'),fullPage:true});
  await page.getByRole('tab',{name:'对象浏览'}).click();
  await page.screenshot({path:path.join(root,'artifacts','desktop-browser.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(root,'artifacts','mobile.png'),fullPage:true});
  check('mobile_no_horizontal_overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  check('no_javascript_errors',errors.length===0);
 }catch(e){checks.push({name:'execution_completed',passed:false});throw e;}finally{
  fs.writeFileSync(path.join(root,'artifacts','ui-report.json'),JSON.stringify({checks,errors},null,2));
  await browser.close();
 }
 console.log(JSON.stringify(checks));
})().catch(e=>{console.error(e.message);process.exitCode=1;});
