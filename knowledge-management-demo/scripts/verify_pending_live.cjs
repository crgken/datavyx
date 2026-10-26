// Read-only verification of the actual local service after the upgrade.
const fs=require('fs');const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));
const get=async path=>(await (await page.request.get('http://127.0.0.1:8096'+path)).json()).data;
const check=(name,ok)=>{if(!ok)throw Error(name);checks.push(name);};
await page.goto('http://127.0.0.1:8096/');await page.getByRole('heading',{name:/待确认记录/}).waitFor();
const graph=await get('/api/graph'),health=await get('/api/health'),batches=await get('/api/batches');
check('live model configured',health.model_configured);check('inbox available',Number.isInteger(batches.total));
await page.getByRole('tab',{name:'星状图'}).click();await page.waitForFunction(()=>starState.graph&&!starState.loading,{},{timeout:60000});
check('full live graph rendered',await page.evaluate(expected=>starState.graph.getNodeData().length===expected&&starState.snapshot.complete,graph.node_count));
check('no browser exceptions',errors.length===0);
await page.screenshot({path:'artifacts/live-pending-desktop.png',fullPage:true});
await page.setViewportSize({width:390,height:844});await page.screenshot({path:'artifacts/live-pending-mobile.png',fullPage:true});
check('live mobile no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
const report={passed:checks.length,checks,errors,node_count:graph.node_count,edge_count:graph.edge_count,revision:graph.revision,pending_batches:batches.total,health};
fs.writeFileSync('docs/live-pending-ui-verification.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));await browser.close();})().catch(e=>{console.error(e);process.exit(1)});
