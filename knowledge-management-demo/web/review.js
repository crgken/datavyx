"use strict";
const reviewLabels = {confirm:"确认",archive:"归档",restore:"恢复对象",retract:"撤回",restate:"重新登记",reclassify:"更正类型"};
const effectiveLabels = {effective:"当前有效",pending:"内容待确认",blocked:"暂不生效",retracted:"已撤回",superseded:"已被更正",absent:"尚未登记"};
let selectedRef = null;
const archiveLabel=node("label"), archiveCheck=node("input");
archiveCheck.type="checkbox"; archiveCheck.id="include-archived";
archiveLabel.append(archiveCheck,document.createTextNode(" 包含归档对象"));
$(".browse-controls").prepend(archiveLabel);
archiveCheck.addEventListener("change",()=>{selectedRef=null;types().catch(error);});

const originalApi=api;
api=async function(path,body){
  if(body===undefined && (/^\/api\/(types|objects)(\?|$)/).test(path) && !$("#browse-view").hidden){
    selectedRef=null;
    path+=(path.includes("?")?"&":"?")+"include_archived="+archiveCheck.checked;
  }
  if(path==="/api/chat" && body && selectedRef && !$("#browse-view").hidden) body={...body,context_ref:selectedRef};
  return originalApi(path,body);
};

async function afterReviewSave(){
  const previous=state.detail?.id;
  state.result=null;clear($("#statistics"));clear($("#audit"));
  $("#statistics").append(node("p","数据已更新，请重新查询以获得当前统计。","scope"));
  selectedRef=null;
  await types();
  if(previous)await openObject({id:previous});
  invalidateStar();
  await health();
}

function reviewDialog(data, target, action){
  if(state.busy){error(new Error("当前对话正在处理，请完成后再发起操作。"));return;}
  const dialog=node("dialog",undefined,"review-dialog"), form=node("form");
  const isEntity=!target.subject_id, label=isEntity?entityLabel(target):target.predicate+"："+valueText(target.value);
  form.append(node("h2",reviewLabels[action]+" · "+label),node("p",isEntity?`当前类型：${target.kind}；身份${target.confirmed?"已确认":"待确认"}`:"只操作这一条信息，保留原文来源和历史。","scope"));
  const summary={confirm:isEntity?"仅确认对象当前身份和类型，其他待确认内容保持原状态。":"仅确认本条内容；关联对象未确认时仍暂不生效。",archive:"归档后默认浏览、统计和星状图不再使用该对象及受影响的关系；历史保留。",restore:"恢复对象原来的确认状态；已撤回或已被更正的信息保持失效。",retract:"撤回本条信息，其他属性和关系保持原状态。",restate:"生成新的信息版本并保留原确认状态，不覆盖现有单值。",reclassify:"更正现有分类并保留对象 ID 和历史。请核实这确实是分类错误。"};
  form.append(node("p",summary[action]));
  let kindInput,classification,confirmIdentity;
  if(action==="reclassify"){
    const kindLabel=node("label","正确类型" );kindInput=node("input");kindInput.required=true;kindInput.maxLength=100;kindInput.setAttribute("aria-label","正确类型");kindLabel.append(kindInput);form.append(kindLabel);
    const checkLabel=node("label");classification=node("input");classification.type="checkbox";classification.required=true;checkLabel.append(classification,document.createTextNode(" 我核实了当前分类存在错误"));form.append(checkLabel);
    if(!target.confirmed){const identityLabel=node("label");confirmIdentity=node("input");confirmIdentity.type="checkbox";identityLabel.append(confirmIdentity,document.createTextNode(" 同时确认该对象身份（可选）"));form.append(identityLabel);}
  }
  const additional=[];
  if(!isEntity && action==="confirm"){
    const candidates=[data.object,...(data.dependencies||[])].filter((o,i,all)=>!o.confirmed&&!o.archived&&all.findIndex(x=>x.id===o.id)===i);
    if(candidates.length){form.append(node("p","也可单独选择已核实的关联对象；默认不选。","scope"));for(const obj of candidates){const lab=node("label"),box=node("input");box.type="checkbox";lab.append(box,document.createTextNode(` 同时确认 ${entityLabel(obj)}（当前类型：${obj.kind}）`));additional.push({box,obj});form.append(lab);}}
  }
  const reasonLabel=node("label","操作说明"), reason=node("textarea");reason.maxLength=1000;reason.required=true;reason.value=action==="confirm"?"用户核实当前记录":action==="retract"?"用户撤回此条信息":"用户发起"+reviewLabels[action];reason.setAttribute("aria-label","操作说明");reasonLabel.append(reason);form.append(reasonLabel);
  const controls=node("div",undefined,"review-actions"),submit=node("button","生成预览","primary");submit.type="submit";controls.append(button("取消",()=>dialog.close()),submit);form.append(controls);
  form.addEventListener("submit",async event=>{event.preventDefault();submit.disabled=true;try{
    const op={target_type:isEntity?"entity":"fact",target_id:target.id,action,reason:reason.value.trim()};
    if(action==="reclassify"){op.new_kind=kindInput.value.trim();op.classification_error=classification.checked;}
    const operations=[op];
    for(const {box,obj} of additional)if(box.checked)operations.push({target_type:"entity",target_id:obj.id,action:"confirm",reason:reason.value.trim()});
    if(confirmIdentity?.checked)operations.push({target_type:"entity",target_id:target.id,action:"confirm",reason:reason.value.trim()});
    let replace="";
    if(state.draft){if(!window.confirm("当前有待确认预览。是否用本次操作替换？原预览尚未保存。"))return;replace=state.draft.draft_id;}
    const draft=await api("/api/review-preview",{operations,revision:data.revision,replace_draft_id:replace});
    dialog.close();preview(draft);$("#preview-panel").scrollIntoView({behavior:"smooth",block:"start"});
  }catch(exc){error(exc);}finally{submit.disabled=false;}});
  dialog.append(form);document.body.append(dialog);dialog.addEventListener("close",()=>dialog.remove(),{once:true});dialog.showModal();
}

renderDetail=function(data){
  const el=$("#detail");clear(el);
  if(state.detail)state.detail.archived=data.object.archived;
  selectedRef={target_type:"entity",target_id:data.object.id,revision:data.revision};
  const heading=node("div",undefined,"object-title"),title=node("div"),actions=node("div",undefined,"review-actions");
  title.append(node("h3",entityLabel(data.object)),node("small",data.object.kind+" · "+data.object.id),badge(data.object.confirmed));
  if(data.object.archived)title.append(node("p","已归档 · 保留历史，默认查询不计入","archive-notice"));
  else if(!data.object.confirmed)title.append(node("small",data.object.reason));
  if(data.object.archived)actions.append(button("恢复对象",()=>reviewDialog(data,data.object,"restore")));
  else{
    if(!data.object.confirmed)actions.append(button("确认对象",()=>reviewDialog(data,data.object,"confirm"),"primary"));
    const more=node("details"),summary=node("summary","更多操作");more.append(summary,button("更正类型",()=>reviewDialog(data,data.object,"reclassify")),button("归档对象",()=>reviewDialog(data,data.object,"archive")));actions.append(more);
  }
  const toggle=node("label"),check=node("input");check.type="checkbox";check.checked=data.history;check.addEventListener("change",()=>detailPage(0,check.checked).catch(error));toggle.append(check,document.createTextNode(" 查看历史"));actions.append(toggle);heading.append(title,actions);el.append(heading);
  if(!data.rows.length)el.append(node("p","此对象尚无属性记录。","scope"));
  else el.append(table(["属性 / 关系","值","内容确认 / 生效状态","操作与来源"],data.rows.map(f=>{
    const status=node("div");status.append(badge(f.review_status==="confirmed"),node("small",effectiveLabels[f.effective_status]));
    for(const reason of f.blocked_reasons||[])status.append(node("small",reason));
    const controls=node("div",undefined,"review-actions");
    if(f.active){if(f.review_status!=="confirmed")controls.append(button("确认此条",()=>reviewDialog(data,f,"confirm")));controls.append(button("撤回此条",()=>reviewDialog(data,f,"retract")));}
    else if(f.retracted_by&&!f.replaced_by)controls.append(button("重新登记",()=>reviewDialog(data,f,"restate")));
    controls.append(button("选中此条",()=>{selectedRef={target_type:"fact",target_id:f.id,revision:data.revision};$("#chat-status").textContent="已选中："+f.predicate+" · 可在对话中说明操作";}),button(`${f.source_count} 条来源`,()=>sources(f.id,0,el),"text-link"));
    const value=f.object_id?button(valueText(f.value),()=>openObject({id:f.object_id},state.detail.depth+1,data.revision),"text-link"):valueText(f.value);
    return [f.predicate,value,status,controls];
  })));
  if(data.history){
    el.append(node("h3","操作历史"));
    for(const event of data.mutations||[]){
      const change=JSON.parse(event.change);
      for(const op of change.reviews||[]){
        const entry=node("div",`${reviewLabels[op.action]}${op.target_type==="entity"?"对象":"信息"} · ${op.reason}${op.action==="reclassify"?` · ${op.before.kind} → ${op.after.kind}`:""}`,"source-block");
        entry.append(node("small",`${change.action_event_id?"页面操作":"对话操作"} · ${change.actor||"用户"} · ${new Date(event.created_at*1000).toLocaleString()}`));
        for(const source of op.sources||[])entry.append(node("small","用户原话："+source.quote));
        el.append(entry);
      }
      for(const item of change.entity_updates||[])el.append(node("p",`${fieldLabel(item.field)}：${item.previous} → ${item.value} · ${item.reason}`,"source-block"));
      for(const op of change.retractions||[])el.append(node("p",`撤回 ${op.previous.predicate} · ${op.reason}`,"source-block"));
    }
  }
  el.append(paging(data,next=>detailPage(next,data.history)),node("p",`当前对象属性 · ${data.history?"包含历史":"含待确认标记"} · 版本 ${data.revision}`,"scope"));
};

const originalObjectTable=objectTable;
objectTable=function(rows){
  const el=originalObjectTable(rows),trs=el.querySelectorAll("tbody tr");
  rows.forEach((obj,index)=>{if(obj.archived)trs[index].children[2].append(node("span","已归档","badge pending"));});
  return el;
};

const originalPreview=preview;
preview=function(draft){
  originalPreview(draft);
  const el=$("#preview"), actions=el.querySelector(".preview-actions");
  if(!draft.objects.length)el.querySelector("h3")?.remove();
  if(draft.reviews?.length){
    const section=node("section",undefined,"review-preview");section.append(node("h3","本次操作"));
    section.append(table(["目标","操作","变化与依据"],draft.reviews.map(op=>{
      const b=op.before,a=op.after,objects=draft.review_objects||[];
      const subject=objects.find(o=>o.id===b.subject_id),related=objects.find(o=>o.id===b.object_id);
      const value=op.target_type==="fact"?(b.object_id?(related?entityLabel(related):"关联对象"):valueText(JSON.parse(b.value_json))):"";
      const target=op.target_type==="entity"?entityLabel(b):`${subject?entityLabel(subject):"当前对象"} · ${b.predicate}：${value}`;
      const detail=op.action==="reclassify"?`${b.kind} → ${a.kind}`:op.action==="confirm"?"待确认 → 内容已确认；依赖对象仍分别核验":op.action==="retract"?"当前 → 已撤回（保留历史）":op.action==="restate"?"重新登记为新版本":op.action==="archive"?"当前 → 已归档":"已归档 → 恢复原确认状态";
      return [target,reviewLabels[op.action],`${detail}\n${op.reason}`];
    })));
    if(draft.origin==="button")section.append(node("p","操作依据：页面操作事件。原记录来源保留，不新增伪造的用户原话。","scope"));
    const impact=node("div");section.append(impact);el.insertBefore(section,actions);
    const load=async offset=>{const page=await api(url("/api/review-impact",{id:draft.draft_id,offset,limit:25}));clear(impact);impact.append(node("h3",`生效状态变化 · 共 ${page.total} 条`));if(page.rows.length)impact.append(table(["对象 / 信息","原状态","操作后"],page.rows.map(r=>[`${r.subject} · ${r.predicate} · ${r.value}`,effectiveLabels[r.before],`${effectiveLabels[r.after]}${r.reasons.length?" · "+r.reasons.join("；"):""}`])));else impact.append(node("p","其他信息生效状态不变。","scope"));impact.append(paging(page,load));};load(0).catch(error);
  }
  actions.append(button("取消本次变更",async()=>{await api("/api/cancel-preview",{draft_id:draft.draft_id});state.draft=null;localStorage.removeItem("kd-draft");$("#preview-panel").hidden=true;}));
};
