"use strict";
const starState={graph:null,snapshot:null,request:0,selectionRequest:0,selected:null,loading:false,layoutBusy:false};
const graphColors=["#4775ce","#268a84","#a47a35","#8561ad","#b3687f","#477d99"];
const graphStatus={effective:"当前有效",pending:"内容待确认",blocked:"暂不生效"};
function graphColor(kind){let hash=0;for(const char of kind)hash=(hash+char.codePointAt(0))%graphColors.length;return graphColors[hash];}
function graphLabel(text){const letters=Array.from(text);return letters.length>10?letters.slice(0,10).join("")+"…":text;}
function graphControls(){for(const id of ["star-fit","star-relayout","star-zoom-in","star-zoom-out"])$("#"+id).disabled=!starState.graph||starState.loading||starState.layoutBusy;}
function destroyStar(){if(starState.graph){starState.graph.destroy();starState.graph=null;}clear($("#star-canvas"));}
function invalidateStar(){
  ++starState.request;++starState.selectionRequest;
  destroyStar();starState.snapshot=null;starState.selected=null;starState.loading=false;starState.layoutBusy=false;
  for(const id of ["star-selection","star-relations","star-legend"])clear($("#"+id));
  $("#star-selection").hidden=true;$("#star-accessible").hidden=true;
  $("#star-canvas").setAttribute("aria-busy","false");
  $("#star-status").textContent="数据已更新，打开星状图加载当前全部对象。";graphControls();
}
function graphFailure(exc){
  ++starState.selectionRequest;clear($("#star-selection"));$("#star-selection").hidden=true;
  destroyStar();starState.snapshot=null;starState.loading=false;starState.layoutBusy=false;
  clear($("#star-relations"));clear($("#star-legend"));$("#star-accessible").hidden=true;
  $("#star-canvas").setAttribute("aria-busy","false");
  const box=node("div",undefined,"empty graph-message");box.append(node("p","关系图未能完整加载。"),button("重新加载",enterStar));$("#star-canvas").append(box);
  $("#star-status").textContent=exc.code==="data_changed"?"数据已变化，请重新加载全图。":"本次未取得完整关系图，请重试。";graphControls();error(exc);
}
function graphLegend(snapshot){
  const el=$("#star-legend");clear(el);const counts=new Map();
  for(const obj of snapshot.nodes)counts.set(obj.kind,(counts.get(obj.kind)||0)+1);
  for(const [kind,count] of counts){const item=node("span",undefined,"graph-key"),dot=node("span","●","graph-dot");dot.style.color=graphColor(kind);item.append(dot,document.createTextNode(`${kind} ${count}`));el.append(item);}
  el.append(node("span","实线：当前有效关系","graph-key"),node("span","虚线：待确认对象或暂不生效关系","graph-key"));
}
function graphLists(snapshot){
  const el=$("#star-relations");clear(el);const names=new Map(snapshot.nodes.map(o=>[o.id,entityLabel(o)]));
  const connected=new Set(snapshot.edges.flatMap(e=>[e.source,e.target]));
  el.append(node("h3",`全部对象 · ${snapshot.node_count}`),table(["对象","类型","状态"],snapshot.nodes.map(o=>{
    const status=node("div");status.append(badge(o.confirmed));if(!connected.has(o.id))status.append(node("small","暂无当前关系"));
    return [button(entityLabel(o),()=>selectGraphNode(o.id),"text-link"),o.kind,status];
  })));
  if(snapshot.edges.length)el.append(node("h3",`全部关系 · ${snapshot.edge_count}`),table(["关系","方向","状态"],snapshot.edges.map(e=>[
    button(e.predicate,()=>selectGraphEdge(e.id),"text-link"),`${names.get(e.source)} → ${names.get(e.target)}`,graphStatus[e.effective_status]
  ])));
  $("#star-accessible").hidden=false;
}
async function markGraphSelection(id){
  const graph=starState.graph;if(!graph)return;
  const states={};if(starState.selected)states[starState.selected]=[];states[id]=["selected"];starState.selected=id;
  await graph.setElementState(states,false);
}
function detailFailure(exc,retry){
  const el=$("#star-selection");clear(el);el.hidden=false;el.append(node("p","详情未能加载，请重试。"),button("重试",retry));error(exc);
}
async function graphSources(id,offset,selection){
  const data=await api(url("/api/sources",{id,offset,limit:10}));
  if(selection!==starState.selectionRequest)return;
  const parent=$("#star-selection");parent.querySelector(".source-area")?.remove();const el=node("div",undefined,"source-area audit-panel");
  el.append(node("h3","来源原文"));for(const source of data.rows){const item=node("div",source.message,"source-item");item.append(node("small","支持摘录："+(source.quotes||[source.quote]).join("\n")));el.append(item);}
  el.append(paging(data,next=>graphSources(id,next,selection)));parent.append(el);
}
async function selectGraphNode(id,offset=0){
  const snapshot=starState.snapshot;if(!snapshot)return;
  const selection=++starState.selectionRequest,el=$("#star-selection");el.hidden=false;clear(el);el.append(node("p","正在读取对象详情…","scope"));
  try{
    await markGraphSelection(id);
    const detail=await api(url("/api/detail",{id,offset,limit:10,revision:snapshot.revision}));
    if(selection!==starState.selectionRequest)return;
    clear(el);const heading=node("div",undefined,"graph-detail-heading"),title=node("div");
    title.append(node("h3",entityLabel(detail.object)),node("p",detail.object.kind,"scope"),badge(detail.object.confirmed));
    heading.append(title,button("在对象浏览中打开",()=>openObject(detail.object,0)));el.append(heading);
    if(!detail.object.confirmed&&detail.object.reason)el.append(node("p",detail.object.reason,"scope"));
    if(detail.rows.length)el.append(table(["属性 / 关系","值","状态","来源"],detail.rows.map(f=>[
      f.predicate,valueText(f.value),f.effective_status==="effective"?"当前有效":f.blocked_reasons.join("；"),
      button(`${f.source_count} 条来源`,()=>graphSources(f.id,0,selection),"text-link")
    ])));
    else el.append(node("p","此对象暂无当前属性或关系。","scope"));
    el.append(paging(detail,next=>selectGraphNode(id,next)));
  }catch(exc){if(selection===starState.selectionRequest)detailFailure(exc,()=>selectGraphNode(id,offset));}
}
async function selectGraphEdge(id){
  const snapshot=starState.snapshot,edge=snapshot?.edges.find(e=>e.id===id);if(!edge)return;
  const selection=++starState.selectionRequest,el=$("#star-selection");
  try{
    await markGraphSelection(id);if(selection!==starState.selectionRequest)return;
    const subject=snapshot.nodes.find(o=>o.id===edge.source),target=snapshot.nodes.find(o=>o.id===edge.target);
    clear(el);el.hidden=false;el.append(node("h3",`${entityLabel(subject)} → ${edge.predicate} → ${entityLabel(target)}`));
    el.append(node("p",`内容${edge.review_status==="confirmed"?"已确认":"待确认"} · ${graphStatus[edge.effective_status]}`,"scope"));
    for(const reason of edge.blocked_reasons)el.append(node("p",reason,"scope"));
    const actions=node("div",undefined,"graph-toolbar");actions.append(button("查看起点对象",()=>selectGraphNode(edge.source)),button("查看终点对象",()=>selectGraphNode(edge.target)),button(`查看 ${edge.source_count} 条来源`,()=>graphSources(id,0,selection)));el.append(actions);
  }catch(exc){if(selection===starState.selectionRequest)detailFailure(exc,()=>selectGraphEdge(id));}
}
async function enterStar(){
  tabs("star");const request=++starState.request;++starState.selectionRequest;destroyStar();
  starState.snapshot=null;starState.selected=null;starState.loading=true;starState.layoutBusy=false;
  clear($("#star-selection"));$("#star-selection").hidden=true;clear($("#star-legend"));clear($("#star-relations"));$("#star-accessible").hidden=true;
  const canvas=$("#star-canvas");canvas.setAttribute("aria-busy","true");canvas.append(node("div","正在加载全部对象与关系…","empty graph-message"));
  $("#star-status").textContent="正在读取完整关系图…";graphControls();
  try{
    const snapshot=await api("/api/graph");if(request!==starState.request)return;
    if(!snapshot.complete||snapshot.nodes.length!==snapshot.node_count||snapshot.edges.length!==snapshot.edge_count)throw new Error("关系图数据不完整，请重新加载。");
    if(!window.G6?.Graph)throw new Error("关系图组件未能加载，请刷新页面重试。");
    clear(canvas);starState.snapshot=snapshot;graphLegend(snapshot);graphLists(snapshot);
    if(!snapshot.nodes.length){canvas.append(node("div","还没有对象。在左侧记录信息并确认后，这里会展示完整关系图。","empty graph-message"));$("#star-status").textContent=`全部 0 个对象 · 0 条关系 · 版本 ${snapshot.revision}`;starState.loading=false;canvas.setAttribute("aria-busy","false");graphControls();return;}
    const width=canvas.clientWidth||800,height=canvas.clientHeight||560,reduced=matchMedia("(prefers-reduced-motion: reduce)").matches;
    const nodes=snapshot.nodes.map((o,i)=>({id:o.id,data:o,style:{x:width/2+180*Math.cos(i*2*Math.PI/snapshot.node_count),y:height/2+180*Math.sin(i*2*Math.PI/snapshot.node_count)}}));
    const graph=new G6.Graph({container:canvas,width,height,autoFit:"view",padding:45,animation:false,zoomRange:[0.08,4],
      data:{nodes,edges:snapshot.edges.map(e=>({id:e.id,source:e.source,target:e.target,data:e}))},
      layout:{type:"d3-force",animation:!reduced,alphaDecay:0.08,velocityDecay:0.4,
        link:{distance:220,strength:0.25},manyBody:{strength:-800},collide:{radius:96,strength:1,iterations:3},
        x:{x:width/2,strength:0.015},y:{y:height/2,strength:0.015}},
      behaviors:["drag-canvas","zoom-canvas",{type:"drag-element-force",fixed:false}],
      transforms:[{type:"process-parallel-edges",mode:"bundle",distance:24}],
      node:{type:"circle",style:{size:54,fill:d=>graphColor(d.data.kind)+"20",stroke:d=>graphColor(d.data.kind),lineWidth:2,
        lineDash:d=>d.data.has_pending?[5,3]:[],labelText:d=>`${graphLabel(entityLabel(d.data))}\n${graphLabel(d.data.kind)}${d.data.confirmed?(d.data.has_pending?' · 含待确认':''):" · 待确认"}`,
        labelPlacement:"bottom",labelOffsetY:9,labelFontSize:12,labelLineHeight:17,labelFill:"#314965",labelBackground:true,labelBackgroundFill:"#fbfcff",labelPadding:2,cursor:"pointer"},
        state:{selected:{stroke:"#174fbb",lineWidth:4,halo:true,haloStroke:"#a8c7ff"}}},
      edge:{style:{stroke:d=>d.data.effective_status==="effective"?"#8c9eb6":"#b38b4b",lineWidth:1.5,endArrow:true,
        lineDash:d=>d.data.effective_status==="effective"?[]:[6,4],labelText:d=>graphLabel(d.data.predicate),labelAutoRotate:false,labelFontSize:11,labelFill:"#52657e",
        labelBackground:true,labelBackgroundFill:"#fff",labelPadding:3,cursor:"pointer"},state:{selected:{stroke:"#174fbb",lineWidth:3}}}
    });
    starState.graph=graph;
    graph.on(G6.NodeEvent.CLICK,event=>selectGraphNode(event.target.id));
    graph.on(G6.EdgeEvent.CLICK,event=>selectGraphEdge(event.target.id));
    await graph.render();if(request!==starState.request)return;
    await graph.fitView({},false);if(request!==starState.request)return;
    const pending=snapshot.edges.filter(e=>e.effective_status!=="effective").length;
    $("#star-status").textContent=`全部 ${snapshot.node_count} 个对象 · ${snapshot.edge_count} 条当前关系${pending?`（${pending} 条待确认或暂不生效）`:""} · 版本 ${snapshot.revision}`;
    canvas.setAttribute("aria-busy","false");starState.loading=false;graphControls();
  }catch(exc){if(request===starState.request)graphFailure(exc);}
}
async function relayoutStar(){
  const graph=starState.graph,request=starState.request;if(!graph)return;
  starState.layoutBusy=true;graphControls();
  try{await graph.layout();if(request===starState.request)await graph.fitView({},false);}
  catch(exc){if(request===starState.request)graphFailure(exc);}
  finally{if(request===starState.request){starState.layoutBusy=false;graphControls();}}
}
$("#star-tab").addEventListener("click",enterStar);
$("#star-refresh").addEventListener("click",enterStar);
$("#star-fit").addEventListener("click",()=>starState.graph?.fitView({},false).catch(error));
$("#star-relayout").addEventListener("click",relayoutStar);
$("#star-zoom-in").addEventListener("click",()=>starState.graph?.zoomBy(1.25,false).catch(error));
$("#star-zoom-out").addEventListener("click",()=>starState.graph?.zoomBy(0.8,false).catch(error));
let graphWidth=0;
new ResizeObserver(()=>{
  const canvas=$("#star-canvas"),width=canvas.clientWidth;if(!width){graphWidth=0;return;}if(width===graphWidth)return;graphWidth=width;
  if(starState.graph&&!starState.loading){starState.graph.resize(width,canvas.clientHeight);starState.graph.fitView({},false).catch(error);}
}).observe($("#star-canvas"));
