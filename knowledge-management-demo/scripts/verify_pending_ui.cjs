const fs=require('fs');const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage({viewport:{width:1440,height:1000}}),checks=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const fixture=JSON.parse(fs.readFileSync('artifacts/pending-ui-server.json','utf8'));
 function check(n,v){if(!v)throw Error(n);checks.push(n);}
 const api=async path=>(await (await page.request.get('http://127.0.0.1:8097'+path)).json()).data;
 await page.goto('http://127.0.0.1:8097/');await page.locator('.pending-batch').first().waitFor();
 check('two durable batches visible',await page.locator('.pending-batch').count()===2);
 await page.reload();await page.locator('.pending-batch').first().waitFor();check('pending inbox survives reload',await page.locator('.pending-batch').count()===2);
 const a=page.locator(`[data-batch-id="${fixture.batch_a}"]`);await a.locator('summary').first().click();await a.getByRole('button',{name:'确认所选',exact:true}).click();await page.locator('dialog').waitFor();
 check('explicit frozen selection lists 3 records',(await page.locator('dialog').innerText()).includes('3 项'));
 await page.locator('dialog').getByRole('button',{name:'取消',exact:true}).click();check('cancel review does not activate records',(await api('/api/batches')).total===2);
 await a.getByRole('button',{name:'确认所选',exact:true}).click();await page.locator('dialog').getByRole('button',{name:'确认所选信息',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.pending-batch').length===1);
 check('confirm selected batch leaves unrelated pending batch',(await api('/api/batches')).total===1);
 await page.getByRole('tab',{name:'星状图'}).click();await page.waitForFunction(()=>typeof starState!=='undefined'&&starState.graph&&!starState.loading);
 check('all objects visible without choosing center',await page.evaluate(()=>starState.snapshot.node_count===3&&starState.snapshot.edge_count===1));
 await page.selectOption('#star-record-status','pending');await page.waitForFunction(()=>!starState.loading&&starState.snapshot?.node_count===1);check('pending graph filter',await page.evaluate(()=>starState.snapshot.node_count===1));
 await page.selectOption('#star-record-status','confirmed');await page.waitForFunction(()=>!starState.loading&&starState.snapshot?.node_count===2);check('confirmed graph filter',await page.evaluate(()=>starState.snapshot.edge_count===1));
 await page.screenshot({path:'artifacts/pending-ui-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'artifacts/pending-ui-mobile.png',fullPage:true});
 check('mobile has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 const b=page.locator(`[data-batch-id="${fixture.batch_b}"]`);await b.locator('summary').first().click();await b.getByRole('button',{name:'撤回所选',exact:true}).click();await page.locator('dialog').getByRole('button',{name:'撤回所选信息',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.pending-batch').length===0);
 check('withdraw pending object hides it from graph',(await api('/api/graph')).node_count===2);
 check('no browser runtime errors',errors.length===0);
 const report={passed:checks.length,checks,errors,dataset:fixture.dataset};fs.writeFileSync('artifacts/pending-ui-acceptance.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
