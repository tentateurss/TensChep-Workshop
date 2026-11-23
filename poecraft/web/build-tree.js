"use strict";
// Isolated renderer: all coordinates, connections and effects come from the selected PoB version.
const BuildTree = (() => {
 const cache=new Map();let token=0;
 function connection(a,b){const start=`M${a.x} ${a.y}`;
  if(a.group===b.group&&a.orbit===b.orbit&&a.radius>0){const delta=Math.atan2(Math.sin(b.angle-a.angle),Math.cos(b.angle-a.angle)),norm=x=>(x+Math.PI*2)%(Math.PI*2),sweep=Math.abs(Math.abs(delta)-Math.PI)<1e-9?norm(a.angle)>norm(b.angle):delta>0;return `${start}A${a.radius} ${a.radius} 0 0 ${sweep?1:0} ${b.x} ${b.y}`;}
  return `${start}L${b.x} ${b.y}`;
 }
 function masteries(spec){return Object.fromEntries([...(spec?.mastery_effects||'').matchAll(/\{(\d+),(\d+)\}/g)].map(m=>[m[1],Number(m[2])]));}
 function delta(current,target){if(!target||typeof current.version!=='string'||typeof target.version!=='string'||current.version.replaceAll('.','_')!==target.version.replaceAll('.','_')||!current.nodes||!target.nodes)return null;
  const a=new Set(current.nodes.map(String)),b=new Set(target.nodes.map(String)),am=masteries(current),bm=masteries(target);
  return {added:[...b].filter(id=>!a.has(id)),removed:[...a].filter(id=>!b.has(id)),masteries:[...b].filter(id=>a.has(id)&&am[id]&&bm[id]&&am[id]!==bm[id])};}
 async function mount(host,spec,target,t,esc){const mine=++token;if(!host||!spec)return;
  host.innerHTML=`<p role="status">${esc(t('build.map.loading'))}</p>`;
  try{const version=spec.version?.replaceAll('.','_');let data=cache.get(version);
   if(!data){const response=await fetch('/api/build/tree',{method:'POST',headers:{'Content-Type':'application/json','X-Workshop-Build':'1'},body:JSON.stringify({version})});data=await response.json();if(!response.ok)throw Error(data.error);cache.set(version,data);}
   if(mine!==token||!host.isConnected)return;
   if(data.version!==version)throw Error('build.map.error.version');
   draw(host,data,spec,target,t,esc);
  }catch(error){if(mine===token&&host.isConnected){const key=String(error.message).startsWith('build.map.error.')?error.message:'build.map.error.network';host.innerHTML=`<p class="build-warning" role="status">${esc(t(key))}</p><button type="button" data-tree-retry>${esc(t('build.map.retry'))}</button>`;host.querySelector('button').onclick=()=>mount(host,spec,target,t,esc);}}
 }
 function draw(host,data,spec,target,t,esc){const nodes=data.nodes,chosen=new Set((spec.nodes||[]).map(String)),diff=delta(spec,target),added=new Set(diff?.added),removed=new Set(diff?.removed),changed=new Set(diff?.masteries),effects=masteries(spec),future=masteries(target);
  const unknown=[...chosen,...(diff?.added||[])].filter(id=>!nodes[id]);const override=new Set((spec.overrides||[]).map(x=>x.nodeId));
  const ascendancyIds=[...new Set([...chosen,...added])].filter(id=>nodes[id]?.ascendancy);
  const state=id=>added.has(id)?'added':removed.has(id)?'removed':changed.has(id)?'changed':chosen.has(id)?'allocated':'';
  function list(ids,label){return `<details class="map-node-list"><summary>${esc(t(label))} (${ids.length})</summary>${ids.map(id=>`<button type="button" data-focus-node="${id}" ${nodes[id]?'':'disabled'}>${esc(nodes[id]?.name||t('build.map.unmappedNode'))}</button>`).join('')||esc(t('build.map.none'))}</details>`;}
  host.innerHTML=`<div class="map-toolbar"><div class="row wrap"><button type="button" data-map="in" aria-label="${esc(t('build.map.zoomIn'))}">+</button><button type="button" data-map="out" aria-label="${esc(t('build.map.zoomOut'))}">−</button><button type="button" data-map="fit">${esc(t('build.map.fit'))}</button><button type="button" data-map="all">${esc(t('build.map.all'))}</button><button type="button" data-map="ascendancy" ${ascendancyIds.length?'':'disabled'}>${esc(t('build.map.ascendancy'))}</button></div><label>${esc(t('build.map.find'))}<input type="search" data-node-search autocomplete="off"></label><div class="map-search-results"></div></div>
   ${spec.nodes===null?`<p class="build-warning">${esc(t('build.map.allocationsUnknown'))}</p>`:''}<p class="map-legend"><span class="allocated">● ${esc(t('build.map.allocated'))}</span><span class="added">● ${esc(t('build.map.added'))}</span><span class="removed">● ${esc(t('build.map.removed'))}</span><span>◆ ${esc(t('build.map.mastery'))}</span></p>
   <div class="passive-map"><svg tabindex="0" role="img" aria-label="${esc(t('build.map.controls'))}"><g class="map-links">${data.links.map(([a,b])=>`<path class="${chosen.has(a)&&chosen.has(b)?'allocated':''}" d="${connection(nodes[a],nodes[b])}"/>`).join('')}</g><g>${Object.values(nodes).map(n=>{const radius=n.kind==='keystone'?48:n.kind==='notable'?32:n.kind==='mastery'?32:20;return `<g data-node="${n.id}" class="map-node ${n.kind} ${n.ascendancy?'ascendancy':''} ${state(n.id)}" transform="translate(${n.x} ${n.y})"><title>${esc(n.name)}</title>${n.kind==='mastery'?`<path d="M0 -32L32 0 0 32 -32 0Z"/>`:`<circle r="${radius}"/>`}</g>`;}).join('')}</g></svg></div>
   <div class="map-inspector" role="status">${esc(t('build.map.select'))}</div><p class="muted">${esc(t('build.map.controls'))}</p>
   ${unknown.length?`<p class="build-warning">${esc(t('build.map.unmapped').replace('{n}',String(new Set(unknown).size)))}</p>`:''}
   ${target&&!diff?`<p class="build-warning">${esc(t('build.map.noCompare'))}</p>`:''}
   ${diff?list(diff.added,'build.map.added')+list(diff.removed,'build.map.removed')+list(diff.masteries,'build.map.changedMasteries'):''}${list([...chosen],'build.map.allocated')}
   <p class="muted">${esc(t('build.map.noOrder'))}</p><p class="build-warning">${esc(t('build.map.baseEffects'))}</p>
   <details><summary>${esc(t('build.provenance'))}</summary><p class="build-hash">${esc(data.version)} · ${esc(data.revision)}<br>SHA-256: ${esc(data.sha256)}</p><a target="_blank" rel="noopener noreferrer" href="${esc(data.source)}">Path of Building Community</a></details>`;
  const svg=host.querySelector('svg');let view,drag=null,moved=false;const pointers=new Map();
  function apply(){svg.setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`);}
  function fit(ids){let points=ids.map(id=>nodes[id]).filter(Boolean);if(!points.length)points=Object.values(nodes);const xs=points.map(n=>n.x),ys=points.map(n=>n.y),w=Math.max(1800,Math.max(...xs)-Math.min(...xs)+700),h=Math.max(1800,Math.max(...ys)-Math.min(...ys)+700),ratio=svg.clientWidth/Math.max(1,svg.clientHeight);const width=Math.max(w,h*ratio),height=width/ratio;view={x:(Math.max(...xs)+Math.min(...xs)-width)/2,y:(Math.max(...ys)+Math.min(...ys)-height)/2,w:width,h:height};apply();}
  const focusIds=[...new Set([...chosen,...added])].filter(id=>nodes[id]&&!nodes[id].ascendancy);fit(focusIds);
  function point(x,y){return new DOMPoint(x,y).matrixTransform(svg.getScreenCTM().inverse());}
  function zoom(factor,x,y){const p=x===undefined?{x:view.x+view.w/2,y:view.y+view.h/2}:point(x,y),w=Math.min(50000,Math.max(500,view.w*factor)),scale=w/view.w;view={x:p.x-(p.x-view.x)*scale,y:p.y-(p.y-view.y)*scale,w,h:view.h*scale};apply();}
  function inspect(id,center=false){const n=nodes[id];if(!n)return;host.querySelectorAll('.map-node.selected').forEach(el=>el.classList.remove('selected'));host.querySelector(`[data-node="${id}"]`)?.classList.add('selected');
   const effect=(added.has(id)?future:effects)[id],selected=(n.mastery_effects||[]).find(e=>e.effect===effect);let stats=n.kind==='mastery'?(selected?.stats||[]):n.stats;
   if(override.has(id))stats=[];
   host.querySelector('.map-inspector').innerHTML=`<h3>${esc(n.name)}</h3><span class="muted">${esc(n.ascendancy||t('build.map.'+(n.kind==='mastery'?'mastery':'baseNode')))}</span>${override.has(id)?`<p class="build-warning">${esc(t('build.map.override'))}</p>`:n.kind==='mastery'&&!selected?`<p>${esc(t('build.map.masteryUnknown'))}</p>`:''}<ul>${stats.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`;
   if(center){view.x=n.x-view.w/2;view.y=n.y-view.h/2;apply();}
  }
  svg.addEventListener('wheel',e=>{e.preventDefault();zoom(Math.exp(Math.max(-1,Math.min(1,e.deltaY*.002))),e.clientX,e.clientY);},{passive:false});
  svg.addEventListener('pointerdown',e=>{pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});svg.setPointerCapture(e.pointerId);drag={x:e.clientX,y:e.clientY};moved=false;});
  svg.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId))return;const old=[...pointers.values()];pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});const next=[...pointers.values()];
   if(next.length===2){const distance=p=>Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y);if(distance(next)>5)zoom(distance(old)/distance(next),(next[0].x+next[1].x)/2,(next[0].y+next[1].y)/2);moved=true;}
   else if(drag){const a=point(drag.x,drag.y),b=point(e.clientX,e.clientY);if(Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y)>2)moved=true;view.x+=a.x-b.x;view.y+=a.y-b.y;apply();}drag={x:e.clientX,y:e.clientY};});
  for(const event of ['pointerup','pointercancel'])svg.addEventListener(event,e=>{if(event==='pointerup'&&!moved){const p=point(e.clientX,e.clientY);let best=null,distance=Infinity;for(const n of Object.values(nodes)){const d=Math.hypot(n.x-p.x,n.y-p.y);if(d<distance){best=n;distance=d;}}if(distance<view.w/svg.clientWidth*18)inspect(best.id);}pointers.delete(e.pointerId);drag=null;});
  svg.addEventListener('keydown',e=>{if(['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))e.preventDefault();if(['+','='].includes(e.key))zoom(.7);if(e.key==='-')zoom(1.4);if(e.key==='0')fit(focusIds);if(e.key.startsWith('Arrow')){view.x+=({'ArrowLeft':-1,'ArrowRight':1}[e.key]||0)*view.w*.1;view.y+=({'ArrowUp':-1,'ArrowDown':1}[e.key]||0)*view.h*.1;apply();}});
  host.querySelectorAll('[data-map]').forEach(button=>button.onclick=()=>{const action=button.dataset.map;if(action==='in'||action==='out')zoom(action==='in'?.7:1.4);else fit(action==='all'?Object.keys(nodes):action==='ascendancy'?ascendancyIds:focusIds);});
  host.querySelectorAll('[data-focus-node]').forEach(b=>b.onclick=()=>inspect(b.dataset.focusNode,true));
  host.querySelector('[data-node-search]').oninput=e=>{const q=e.target.value.trim().toLowerCase(),matches=q?Object.values(nodes).filter(n=>n.name.toLowerCase().includes(q)).slice(0,12):[];const out=host.querySelector('.map-search-results');out.innerHTML=matches.map(n=>`<button type="button" data-search-node="${n.id}">${esc(n.name)}</button>`).join('');out.querySelectorAll('button').forEach(b=>b.onclick=()=>inspect(b.dataset.searchNode,true));};
 }
 return {mount,delta,masteries,connection};
})();
