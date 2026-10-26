"use strict";
const inbox=node('section',undefined,'panel pending-inbox');
inbox.setAttribute('aria-label','待确认记录');$('.content').prepend(inbox);
const recordStatus=node('select');recordStatus.id='record-status';recordStatus.setAttribute('aria-label','记录状态');
for(const [v,t] of [['all','全部记录'],['confirmed','已确认记录'],['pending','包含待确认信息']]){const o=node('option',t);o.value=v;recordStatus.append(o);}
const statusLabel=node('label','记录状态 ');statusLabel.append(recordStatus);$('.browse-controls').prepend(statusLabel);
recordStatus.addEventListener('change',()=>{const kind=state.browser?.kind;if(kind)objects(kind).catch(error);else objects('').catch(error);});
const starFilter=node('select');starFilter.setAttribute('aria-label','星状图记录状态');starFilter.id='star-record-status';
for(const [v,t] of [['all','全部对象与关系'],['confirmed','已确认且有效'],['pending','待确认及关联上下文']]){const o=node('option',t);o.value=v;starFilter.append(o);}
$('.graph-toolbar').prepend(starFilter);starFilter.addEventListener('change',()=>enterStar().catch(error));

const pendingApi=api;
api=async function(path,body){
  if(body===undefined&&/^\/api\/objects(\?|$)/.test(path))path+='&status='+recordStatus.value;
  const result=await pendingApi(path,body);
  if(result.warning)error(new Error(result.warning));
  if(path==='/api/graph'&&starFilter.value!=='all'){
    let nodes,edges;
    if(starFilter.value==='confirmed'){
      nodes=result.nodes.filter(n=>n.confirmed);const ids=new Set(nodes.map(n=>n.id));edges=result.edges.filter(e=>e.effective_status==='effective'&&ids.has(e.source)&&ids.has(e.target));
    }else{
      edges=result.edges.filter(e=>e.review_status!=='confirmed');const ids=new Set(edges.flatMap(e=>[e.source,e.target]));nodes=result.nodes.filter(n=>n.has_pending||ids.has(n.id));
    }
    return {...result,nodes,edges,node_count:nodes.length,edge_count:edges.length,scope:starFilter.options[starFilter.selectedIndex].text};
  }
  if(result.saved_pending){
    state.draft=null;$('#preview-panel').hidden=true;localStorage.removeItem('kd-draft');
    setTimeout(()=>Promise.all([refreshBatches(),afterReviewSave()]).catch(error),0);
  }
  return result;
};

function itemText(item,names){
  const d=item.data,name=item.target_label||names[item.target_id]||d.name||d.identifier||'对象';
  if(item.type==='entity')return `${d.kind} · ${name}`;
  if(item.type==='update')return `${name} · ${fieldLabel(d.field)}：${d.previous||'空'} → ${d.value}`;
  if(item.type==='operation')return `${name}${item.current_record?.predicate?' · '+item.current_record.predicate:''} · ${reviewLabels[d.action]||d.action}${d.new_kind?'：'+(item.current_record?.kind||'当前类型')+' → '+d.new_kind:''}`;
  return `${item.subject_label||names[d.subject_id]||'对象'} · ${d.predicate}：${d.object_id?(item.object_label||names[d.object_id]||'关联对象'):valueText(JSON.parse(d.value_json))}`;
}

async function reviewSelection(batch,ids,action){
  const frozen=await api('/api/batch-review',{batch_id:batch.id,item_ids:ids,action});
  const dialog=node('dialog',undefined,'review-dialog'),form=node('form');
  const names=Object.fromEntries(batch.items.filter(i=>i.type==='entity').map(i=>[i.target_id,i.data.name||i.data.identifier]));
  form.append(node('h2',`${action==='confirm'?'确认':'撤回'}所选 ${frozen.items.length} 项`),
    node('p',action==='confirm'?'只处理下面列出的信息。确认不会把约数变为精确数值；端点尚未确认的关系仍暂不生效。':'撤回待确认信息并保留历史。更正提议撤回后，原已确认值保持有效。','scope'));
  const list=node('div',undefined,'review-selection-list');
  for(const i of frozen.items)list.append(node('p',itemText(i,names)));
  form.append(list);
  const controls=node('div',undefined,'review-actions'),submit=node('button',action==='confirm'?'确认所选信息':'撤回所选信息','primary');submit.type='submit';
  controls.append(button('取消',()=>dialog.close()),submit);form.append(controls);dialog.append(form);document.body.append(dialog);
  dialog.addEventListener('close',()=>dialog.remove());
  form.addEventListener('submit',async e=>{e.preventDefault();submit.disabled=true;try{const result=await api('/api/batch-apply',{token:frozen.token});dialog.close();message('assistant',result.message);await refreshBatches();await afterReviewSave();}catch(exc){error(exc);}finally{submit.disabled=false;}});
  dialog.showModal();
}

async function refreshBatches(offset=0){
  const data=await api(url('/api/batches',{offset,limit:10}));clear(inbox);
  const heading=node('div',undefined,'panel-heading');heading.append(node('h2',`待确认记录 · ${data.total} 批`),button('刷新',()=>refreshBatches(offset),'quiet'));inbox.append(heading);
  inbox.append(node('p','信息已保存，可查询和查看星图。待确认内容不参与精准统计。','scope'));
  if(!data.rows.length)inbox.append(node('p','当前没有待确认记录。','scope'));
  for(const batch of data.rows){
    const details=node('details',undefined,'pending-batch');details.dataset.batchId=batch.id;
    const label=batch.metadata.bulk?.row_count?`整表 ${batch.metadata.bulk.row_count} 行`:batch.metadata.migration?'已有待确认信息':'记录批次';
    details.append(node('summary',`${label} · ${batch.pending_count} 项待确认 · ${new Date(batch.created_at*1000).toLocaleString('zh-CN')}`));
    let rendered=false;
    details.addEventListener('toggle',()=>{if(!details.open||rendered)return;rendered=true;
      const area=node('div');details.append(area);
      const pending=batch.items.filter(i=>i.status==='pending'),selected=new Set(pending.map(i=>i.id));
      const names=Object.fromEntries(batch.items.filter(i=>i.type==='entity').map(i=>[i.target_id,i.data.name||i.data.identifier]));
      const allLabel=node('label'),all=node('input');all.type='checkbox';all.checked=true;allLabel.append(all,document.createTextNode(` 选择本批全部 ${pending.length} 项（含其他页）`));area.append(allLabel);
      const body=node('div'),actions=node('div',undefined,'review-actions');area.append(body,actions);
      const count=node('span');actions.append(count,
        button('确认所选',()=>{if(!selected.size)throw new Error('请先选择记录。');return reviewSelection(batch,[...selected],'confirm');},'primary'),
        button('撤回所选',()=>{if(!selected.size)throw new Error('请先选择记录。');return reviewSelection(batch,[...selected],'withdraw');}));
      const update=()=>{count.textContent=`已选 ${selected.size} / ${pending.length} 项`;all.checked=selected.size===pending.length;all.indeterminate=selected.size>0&&selected.size<pending.length;};
      const render=(start=0)=>{clear(body);const end=Math.min(start+25,pending.length);
        body.append(table(['选择','信息与来源','操作'],pending.slice(start,end).map(i=>{
          const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=selected.has(i.id);checkbox.setAttribute('aria-label','选择'+itemText(i,names));checkbox.addEventListener('change',()=>{checkbox.checked?selected.add(i.id):selected.delete(i.id);update();});
          const desc=node('div',itemText(i,names));desc.append(node('small','待确认'));
          if(i.type==='fact'&&i.data.pending_supersedes?.length)desc.append(node('small','更正提议；原已确认值仍有效'));
          const view=button('查看 / 修改',async()=>{const oid=i.type==='fact'?i.data.subject_id:i.type==='operation'&&i.data.target_type==='fact'?i.current_record.subject_id:i.target_id;await openObject({id:oid});$('#message').value=`请修改当前对象的${i.type==='fact'?'“'+i.data.predicate+'”待确认信息':'待确认信息'}：`;$('#message').focus();},'text-link');
          return [checkbox,desc,view];
        })));
        body.append(paging({offset:start,limit:25,total:pending.length,returned_count:end-start,has_more:end<pending.length},render));update();
      };
      all.addEventListener('change',()=>{selected.clear();if(all.checked)pending.forEach(i=>selected.add(i.id));render();});render();
      if(batch.metadata.source){const source=node('details');source.append(node('summary','查看原始输入'),node('pre',batch.metadata.source,'pending-source'));area.append(source);}
    });
    inbox.append(details);
  }
  if(data.total>10)inbox.append(paging(data,next=>refreshBatches(next)));
}

const statsBeforePending=statistics;
statistics=function(data){
  if(!data.record_query)return statsBeforePending(data);
  tabs('browse');const el=$('#browser');clear(el);clear($('#detail'));clear($('#neighbors'));
  el.append(node('h3','查询结果 · 包含待确认信息'),node('p',data.scope,'scope'));
  for(const row of data.rows){el.append(objectTable([row.object]));if(row.facts.length)el.append(table(['属性 / 关系','值','确认状态'],row.facts.map(f=>[f.predicate,valueText(f.value),badge(f.review_status==='confirmed')])));for(const p of row.pending_proposals||[])el.append(node('p','待确认提议：'+itemText(p,{[row.object.id]:entityLabel(row.object)})));}
  el.append(paging(data,async offset=>statistics(await api('/api/records',{...data.record_query,offset,revision:data.revision}))));
};

const renderBeforePending=renderDetail;
renderDetail=function(data){renderBeforePending(data);if(data.pending_proposals?.length){const area=node('div',undefined,'audit-panel');area.append(node('h3','待确认的更正与操作'),node('p','以下提议尚未改变当前正式值。请在上方待确认批次核对。','scope'));for(const p of data.pending_proposals)area.append(node('p',itemText(p,{[data.object.id]:entityLabel(data.object)})));$('#detail').append(area);}};

const previewBeforePending=preview;
preview=function(data){if(data.saved_pending){refreshBatches().catch(error);return;}previewBeforePending(data);};

const healthBeforePending=health;
let indexWarningShown=false;
health=async function(){await healthBeforePending();const data=await api('/api/health');if(data.projection_failed&&!indexWarningShown){indexWarningShown=true;error(new Error('部分语义索引更新失败，信息已安全保存。可继续浏览和精准统计，或点击下方按钮重试。'));}let retry=$('#index-retry');if(data.projection_failed&&!retry){retry=button('重试语义索引',async()=>{await api('/api/index-retry',{});indexWarningShown=false;retry.remove();});retry.id='index-retry';inbox.append(retry);}};
refreshBatches().catch(error);
