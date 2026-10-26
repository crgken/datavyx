// Read-only checks against the running Demo, plus isolated API fixtures for
// pagination/empty/error states. No model calls or business records are written.
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const root=path.resolve(__dirname,'..');
  fs.mkdirSync(path.join(root,'artifacts'),{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  const checks=[],errors=[];
  const page=await browser.newPage({viewport:{width:1440,height:1050},deviceScaleFactor:1});
  page.on('pageerror',e=>errors.push(e.message));
  function check(name,value){checks.push({name,passed:!!value});if(!value)throw new Error(name);}
  async function ready(target){await target.locator('#star-canvas[aria-busy="false"] svg').waitFor();}
  try{
    await page.goto('http://127.0.0.1:8096/');
    await page.locator('#browser table').waitFor();
    check('three_sibling_tabs',(await page.locator('.tabs [role="tab"]').allTextContents()).join('|')==='对象浏览|星状图|统计与复核');
    const objects=await page.evaluate(async()=>{const r=await fetch('/api/objects?limit=100');return (await r.json()).data.rows;});
    const contract=objects.find(o=>o.name==='合同 A');
    if(!contract)throw new Error('Existing sample contract A is required for read-only integration checks.');
    await page.locator('#object-id').fill(contract.id);
    await page.locator('#open-object button').click();
    await page.locator('#detail .object-title').waitFor();
    await page.getByRole('tab',{name:'星状图',exact:true}).click();
    await ready(page);
    check('star_is_independent_panel',await page.locator('#star-view').isVisible()&&!await page.locator('#browse-view').isVisible()&&!await page.locator('#stats-view').isVisible());
    check('adopts_browser_selected_object',await page.locator('#star-canvas .star-center').getAttribute('data-id')===contract.id);
    check('real_edges_rendered',await page.locator('#star-canvas .star-edge').count()>0);
    check('outgoing_arrow',await page.locator('#star-canvas .star-edge[marker-end]').count()>0);
    const company=page.locator('#star-canvas .star-node:not(.star-center)').first();
    const companyId=await company.getAttribute('data-id');
    await company.click();
    check('node_opens_selection',await page.locator('#star-selection').isVisible()&&!await page.locator('#star-selection').getByRole('button',{name:'设为中心',exact:true}).isDisabled());
    await page.locator('#star-selection').getByRole('button',{name:'设为中心',exact:true}).click();
    await ready(page);
    check('recenter_on_neighbor',await page.locator('#star-canvas .star-center').getAttribute('data-id')===companyId);
    check('incoming_arrow',await page.locator('#star-canvas .star-edge[marker-start]').count()>0);
    await page.screenshot({path:path.join(root,'artifacts','star-desktop.png'),fullPage:true});
    await page.locator('#star-selection').getByRole('button',{name:'查看完整详情'}).click();
    await page.locator('#browse-view').waitFor({state:'visible'});
    check('details_use_object_browser',await page.locator('#detail .object-title').isVisible());
    await page.getByRole('tab',{name:'星状图',exact:true}).click();
    await ready(page);
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(root,'artifacts','star-mobile.png'),fullPage:true});
    check('mobile_no_page_overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    check('mobile_graph_scrolls_inside_panel',await page.locator('#star-canvas').evaluate(el=>el.scrollWidth>el.clientWidth));
    check('mobile_keeps_center_visible',await page.locator('#star-canvas').evaluate(el=>Math.abs(el.scrollLeft-(el.scrollWidth-el.clientWidth)/2)<2));

    // Isolated fixture page tests layout/controls beyond the live sample size.
    const fixture=await browser.newPage({viewport:{width:1440,height:1050}});
    fixture.on('pageerror',e=>errors.push(e.message));
    const center={id:'fixture-center',name:'分页中心',kind:'演示类型',confirmed:true};
    const children=Array.from({length:10},(_,i)=>({id:'fixture-'+i,name:'关联对象 '+i,kind:'开放类型',confirmed:true}));
    const roots=[center,...children,...Array.from({length:20},(_,i)=>({id:'other-'+i,name:'其他对象 '+i,kind:'其他类型',confirmed:true}))];
    let mode='normal',holdNeighbors=false,releaseNeighbors;
    await fixture.route('**/api/**',async route=>{
      const uri=new URL(route.request().url());
      const offset=Number(uri.searchParams.get('offset')||0),limit=Number(uri.searchParams.get('limit')||25);
      let data;
      if(uri.pathname==='/api/health')data={model_configured:true,projection_pending:0,projection_failed:0};
      else if(uri.pathname==='/api/messages')data=[];
      else if(uri.pathname==='/api/types')data={rows:mode==='no_objects'?[]:[{kind:'演示类型',confirmed:1,total:1}],total:mode==='no_objects'?0:1,offset:0,limit:25,returned_count:mode==='no_objects'?0:1,has_more:false,revision:1};
      else if(uri.pathname==='/api/objects'){const list=mode==='no_objects'?[]:roots;data={rows:list.slice(offset,offset+limit),total:list.length,offset,limit,returned_count:list.slice(offset,offset+limit).length,has_more:offset+limit<list.length,revision:1};}
      else if(uri.pathname==='/api/detail')data={object:roots.find(o=>o.id===uri.searchParams.get('id'))||center,rows:[],revision:1};
      else if(uri.pathname==='/api/neighbors'){
        if(holdNeighbors)await new Promise(resolve=>{releaseNeighbors=resolve;});
        if(mode==='error'){await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({ok:false,errors:[{code:'data_changed',message:'数据已变化，请刷新。'}]})});return;}
        const rows=mode==='empty'?[]:children.map((obj,i)=>({object:obj,predicate:'关联角色 '+i,direction:i%2?'in':'out',fact_id:'fact-'+i}));
        const selected=rows.slice(offset,offset+limit);
        data={rows:selected,total:rows.length,offset,limit,returned_count:selected.length,has_more:offset+limit<rows.length,revision:1};
      }else{await route.abort();return;}
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,data})});
    });
    await fixture.goto('http://127.0.0.1:8096/');
    await fixture.locator('#browser table').waitFor();
    holdNeighbors=true;
    await fixture.getByRole('tab',{name:'星状图',exact:true}).click();
    await fixture.locator('#star-canvas[aria-busy="true"]').waitFor();
    check('explicit_loading_state',await fixture.locator('#star-status').textContent().then(s=>s.includes('正在读取')));
    await fixture.waitForFunction(()=>document.querySelector('#star-canvas[aria-busy="true"]')!==null);
    // Wait for the held request to arrive without depending on fixed delays.
    for(let i=0;i<100&&!releaseNeighbors;i++)await new Promise(resolve=>setTimeout(resolve,20));
    if(!releaseNeighbors)throw new Error('Fixture relation request did not arrive.');
    holdNeighbors=false;releaseNeighbors();
    await ready(fixture);
    check('eight_relations_first_page',await fixture.locator('#star-relations tbody tr').count()===8);
    check('accurate_relation_total',(await fixture.locator('#star-status').textContent()).includes('关系总量 10 条'));
    await fixture.getByRole('button',{name:'下一页对象',exact:true}).click();
    await fixture.getByText('对象列表 26–31 / 共 31 个',{exact:true}).waitFor();
    check('picker_pages_beyond_first_25',await fixture.locator('#star-object option[value="other-19"]').count()===1);
    check('picker_page_preserves_center',await fixture.locator('#star-object').inputValue()==='fixture-center');
    await fixture.locator('#star-pagination').getByRole('button',{name:'下一页',exact:true}).click();
    await ready(fixture);
    check('remaining_relations_second_page',await fixture.locator('#star-relations tbody tr').count()===2);
    check('last_page_disables_next',await fixture.locator('#star-pagination').getByRole('button',{name:'下一页',exact:true}).isDisabled());
    mode='empty';
    await fixture.getByRole('button',{name:'刷新关系图 ↻'}).click();
    await ready(fixture);
    check('no_relation_keeps_center',await fixture.locator('.star-center').count()===1&&await fixture.locator('.star-no-relations').isVisible());
    mode='error';
    const chatCount=await fixture.locator('.message').count();
    await fixture.getByRole('button',{name:'刷新关系图 ↻'}).click();
    await fixture.locator('#error').waitFor({state:'visible'});
    check('error_clears_stale_graph',await fixture.locator('#star-canvas svg').count()===0);
    check('failed_query_not_empty_success',!(await fixture.locator('#star-status').textContent()).includes('关系总量 0'));
    check('error_not_inserted_in_chat',await fixture.locator('.message').count()===chatCount);
    mode='no_objects';
    await fixture.reload();
    await fixture.locator('#browser .empty').waitFor();
    await fixture.getByRole('tab',{name:'星状图',exact:true}).click();
    await fixture.getByText('当前没有可浏览的对象。',{exact:true}).waitFor();
    check('empty_database_prompt',await fixture.locator('#star-canvas svg').count()===0&&await fixture.locator('#star-object').isDisabled());
    await fixture.close();
    check('no_javascript_errors',errors.length===0);
  }catch(exc){checks.push({name:'completed',passed:false});throw exc;}
  finally{
    fs.writeFileSync(path.join(root,'artifacts','star-ui-report.json'),JSON.stringify({checks,errors},null,2));
    await browser.close();
  }
  console.log(JSON.stringify(checks));
})().catch(exc=>{console.error(exc.message);process.exitCode=1;});
