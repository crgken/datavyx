"use strict";
let importTimer=null, importWatch=0;
const importPanel=node("section",undefined,"panel import-panel");
importPanel.hidden=true;importPanel.setAttribute("aria-label","整表导入进度");
$(".content").prepend(importPanel);
async function watchImport(id){
  const version=++importWatch;clearTimeout(importTimer);
  const poll=async()=>{
    try{
      const job=await api(url("/api/import",{id}));if(version!==importWatch)return;
      importPanel.hidden=false;clear(importPanel);
      importPanel.append(node("h2","整表导入"),node("p",`${job.processed_rows} / ${job.total_rows} 行已处理 · 每批最多 ${job.batch_size} 行`));
      const progress=node("progress");progress.max=job.total_rows;progress.value=job.processed_rows;progress.setAttribute("aria-label","表格处理进度");importPanel.append(progress);
      if(job.status==="ready"){
        importPanel.hidden=true;preview(job.draft);return;
      }
      if(job.status==='saved_pending'){importPanel.hidden=true;message('assistant',job.pending_item_count===0?'整表已核验并复用已有记录，无新增待确认项。':`整表 ${job.total_rows} 行已保存为待确认。可在待确认记录中核对，确认前不参与精准统计。`);await refreshBatches();await afterReviewSave();return;}
      if(job.status==="committed"||job.status==="cancelled"){importPanel.hidden=true;return;}
      if(job.status==="answered"){importPanel.hidden=true;message("assistant",job.response.message);if(job.response.result)statistics(job.response.result);return;}
      if(job.status==="running")importPanel.append(node("p","正在解释表头并检查所有行；尚未保存业务记录。","scope"));
      if(job.error)error(new Error(job.error));
      if(job.questions?.length)importPanel.append(node("p",job.questions.join("\n")));
      const controls=node("div",undefined,"review-actions");
      if(["failed","interrupted"].includes(job.status))controls.append(button("从检查点重试",async()=>{await api("/api/import",{job_id:id,action:"retry"});watchImport(id);}));
      controls.append(button("取消导入",async()=>{await api("/api/import",{job_id:id,action:"cancel"});++importWatch;clearTimeout(importTimer);importPanel.hidden=true;if(job.source_text)$("#message").value=job.source_text;}));
      importPanel.append(controls);
      if(job.status==="needs_input")importPanel.append(node("p","请取消导入后，在原表格中补充上述信息并重新提交。","scope"));
      if(job.status==="running")importTimer=setTimeout(poll,1500);
    }catch(exc){if(version!==importWatch)return;error(exc);importPanel.hidden=false;clear(importPanel);importPanel.append(node("p","导入进度暂时不可用；业务记录尚未由本次预览确认保存。"),button("重新连接",()=>watchImport(id)));}
  };
  await poll();
}
const previewBeforeBulk=preview;
preview=function(draft){
  previewBeforeBulk(draft);if(!draft.bulk)return;
  const info=draft.bulk,el=$("#preview"),actions=el.querySelector(".preview-actions"),details=node("details",undefined,"bulk-details");
  details.append(node("summary","查看对象、属性、关系及来源详情"));
  for(const child of [...el.children])if(child!==actions)details.append(child);
  el.insertBefore(details,actions);
  const summary=node("section",undefined,"bulk-summary"),newCount=draft.objects.filter(o=>o.new).length;
  summary.append(node("h3",`整表 ${info.processed_rows} / ${info.row_count} 行 · 遗漏 ${info.missing.length} 项`),
    node("p",Object.entries(info.object_counts).map(([kind,count])=>`${kind} ${count} 个`).join(" · ")),
    node("p",`${newCount} 个新对象 · ${draft.objects.length-newCount} 个已有对象 · ${info.relation_count} 条关系 · ${info.value_count} 条属性`),
    node("p","下表逐行核对原始值；全部批次合并后统一确认，已有对象身份有歧义时须先选择。","scope"));
  const rows=node("details");rows.open=true;rows.append(node("summary","逐行覆盖清单"),table([...info.headers,"覆盖状态"],info.rows.map((cells,index)=>[...cells,`${info.coverage[index].covered_columns.length} 列已覆盖`])));summary.append(rows);el.prepend(summary);
};
(async()=>{const conversation=await api("/api/conversation");if(conversation.pending?.import_id)await watchImport(conversation.pending.import_id);else if(conversation.pending?.draft_id){const draft=await api(url("/api/draft",{id:conversation.pending.draft_id}));if(draft.bulk)preview(draft);}})().catch(error);
