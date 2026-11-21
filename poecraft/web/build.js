"use strict";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const L={code:'en',strings:{},en:{},langs:[]};
let result=null,generation=0,busy=false,statusKey='',selection={},languageGeneration=0,gameVersion=null,metadataReady=false;
const mobile=matchMedia('(max-width:680px)');
const collections={tree:'trees',skills:'skill_sets',items:'item_sets'};
const t=k=>L.strings[k]??L.en[k]??k;
const clean=v=>String(v).replace(/\^(?:x[0-9a-fA-F]{6}|\d)/g,'');
const text=v=>v===null||v===undefined||v===''?esc(t('build.unspecified')):esc(clean(v));
function setStatus(key,error=false){statusKey=key;$('#buildStatus').textContent=key?t(key):'';$('#buildStatus').className=error?'error':'';}
function active(sets,key){return sets.find(x=>x.id===selection[key])||sets.find(x=>x.active)||sets[0];}
function selector(sets,key,label){return `<label>${esc(t(label))}<select data-set="${key}">${sets.map((s,i)=>`<option value="${esc(s.id)}" ${s===active(sets,key)?'selected':''}>${esc(clean(s.title||t('build.set')+' '+(i+1)))}${s.active?' · '+esc(t('build.authorSelected')):''}</option>`).join('')}</select></label>`;}
function series(value){return typeof value==='string'?value.match(/^(\d+)[._](\d+)(?:[._]\d+)*$/)?.slice(1,3).join('.'):null;}
function versionHtml(){
 const tree=active(result.trees,'tree'),pob=series(tree?.version),game=series(gameVersion);
 const status=!metadataReady?'checking':!pob||!game?'unknown':pob===game?'match':'mismatch';
 if(status==='match')return `<p class="muted build-version" data-version-state="match">${esc(t('build.gameVersion'))}: ${text(gameVersion)}</p>`;
 let message=t('build.version.'+status).replace('{tree}',tree?.version?.replaceAll('_','.')||t('build.unspecified')).replace('{game}',gameVersion||t('build.unspecified'));
 return `<p class="build-version build-warning" role="status" data-version-state="${status}">${esc(message)}</p>`;
}
function stagesHtml(){
 const stages=result.loadouts||[];
 if(!stages.length)return `<section class="card build-stages"><h2>${esc(t('build.stages'))}</h2><p>${esc(t('build.stage.none'))}</p></section>`;
 return `<section class="card build-stages"><label for="buildStage">${esc(t('build.stages'))}</label><select id="buildStage"><option value="">${esc(t('build.stage.independent'))}</option>${stages.map(stage=>`<option value="${esc(stage.id)}" ${selection.loadout===stage.id?'selected':''}>${text(stage.title)}</option>`).join('')}</select><p class="muted">${esc(t(selection.loadout?'build.stage.linked':'build.stage.manual'))}</p></section>`;
}
function treeHtml(){const sets=result.trees,s=active(sets,'tree');return `<section class="card build-tree"><div class="build-section-head"><h2>${esc(t('build.tree'))}</h2>${sets.length?selector(sets,'tree','build.treeSet'):''}</div>${!s?`<p class="muted">${text(null)}</p>`:`${versionHtml()}<label class="map-compare">${esc(t('build.map.compare'))}<select id="treeCompare"><option value="">${esc(t('build.map.noTarget'))}</option>${sets.filter(x=>x.id!==s.id).map(x=>`<option value="${esc(x.id)}" ${selection.compare===x.id?'selected':''}>${text(x.title||t('build.set')+' '+x.id)}</option>`).join('')}</select></label><div id="buildTreeMap"></div>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(t('build.openTree'))}</a>`:''}`}</section>`;}

function gemsHtml(){const sets=result.skill_sets,s=active(sets,'skills');return `<section class="card"><div class="build-section-head"><h2>${esc(t('build.gems'))}</h2>${sets.length?selector(sets,'skills','build.skillSet'):''}</div>${!s||!s.groups.length?`<p class="muted">${text(null)}</p>`:s.groups.map((g,i)=>`<details data-gem-group class="build-entry ${g.enabled===false?'build-disabled':''}" ${mobile.matches?'':'open'}><summary>${text(g.label||g.slot||t('build.group')+' '+(i+1))}${g.enabled===false?' · '+esc(t('build.disabled')):''}</summary><p class="muted">${esc(t('build.linked'))}</p><div class="build-gems">${g.gems.map(gem=>`<span class="build-gem ${gem.enabled===false?'build-disabled':''}">${text(gem.name)}<small>${esc(t('build.level'))}: ${text(gem.level)} · ${esc(t('build.quality'))}: ${text(gem.quality)}${gem.enabled===false?' · '+esc(t('build.disabled')):''}</small></span>`).join('')||text(null)}</div></details>`).join('')}</section>`;}
function itemsHtml(){const sets=result.item_sets,s=active(sets,'items');const inventory=new Map(result.inventory.map(x=>[x.id,x]));return `<section class="card"><div class="build-section-head"><h2>${esc(t('build.gear'))}</h2>${sets.length?selector(sets,'items','build.itemSet'):''}</div>${!s||!s.slots.length?`<p class="muted">${text(null)}</p>`:s.slots.filter(slot=>!['0','',null,undefined].includes(slot.item_id)).map(slot=>{const item=inventory.get(slot.item_id);return `<details data-item-group class="build-entry"><summary><span class="muted">${text(slot.name)}</span><strong>${text(item?.name)}</strong></summary>${item?`<pre>${esc(item.text)}</pre><button type="button" data-build-trade="${esc(item.id)}">${esc(t('build.trade.button'))}</button><div class="build-trade-status" role="status"></div>`:''}</details>`;}).join('')||text(null)}<details><summary>${esc(t('build.inventory'))} (${result.inventory.length})</summary>${result.inventory.map(item=>`<details class="build-entry"><summary>${text(item.name)}</summary><pre>${esc(item.text)}</pre></details>`).join('')||text(null)}</details></section>`;}
function render(){document.documentElement.lang=L.code;document.title=t('hub.tool.build.title')+' ? TensChep Workshop';document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));$('#langSelect').innerHTML=L.langs.map(l=>`<button type="button" class="lang ${l.code===L.code?'on':''}" data-lang="${esc(l.code)}">${esc(l.code.toUpperCase())}</button>`).join('');if(statusKey)$('#buildStatus').textContent=t(statusKey);
 $('#buildImportPanel').open=!mobile.matches||!result;
 if(!result){$('#buildResult').innerHTML='';return;}
 const currentTree=active(result.trees,'tree');if(selection.compare===undefined)selection.compare=result.trees[result.trees.indexOf(currentTree)+1]?.id||'';if(selection.compare===currentTree?.id)selection.compare='';
 $('#buildResult').innerHTML=`<section class="card build-identity"><p class="build-eyebrow">PATH OF BUILDING · POE 1</p><h2>${text(result.ascendancy||result.class)}</h2><div class="build-stats"><span><small>${esc(t('build.class'))}</small><b>${text(result.class)}</b></span><span><small>${esc(t('build.ascendancy'))}</small><b>${text(result.ascendancy)}</b></span><span><small>${esc(t('build.level'))}</small><b>${text(result.level)}</b></span></div><p class="build-import-badge">${esc(t('build.importBadge'))}</p><details class="build-caveats"><summary>${esc(t('build.caveats'))}</summary><div class="build-notices">${result.warnings.filter(w=>!['tree_view','ambiguous_sets','missing_items'].includes(w)).map(w=>`<p>${esc(t('build.'+w))}</p>`).join('')}</div></details>${result.warnings.filter(w=>['ambiguous_sets','missing_items'].includes(w)).map(w=>`<p class="build-warning">${esc(t('build.'+w))}</p>`).join('')}${result.source?`<a href="${esc(result.source)}" target="_blank" rel="noopener noreferrer">${esc(t('build.original'))} ↗</a>`:''}<details><summary>${esc(t('build.provenance'))}</summary><p class="build-hash">SHA-256: ${esc(result.sha256)}</p></details></section>${stagesHtml()}<div class="build-grid">${treeHtml()+gemsHtml()+itemsHtml()}</div>`;
 const spec=active(result.trees,'tree');if(spec)BuildTree.mount($('#buildTreeMap'),spec,result.trees.find(x=>x.id===selection.compare),t,esc);
}
async function setLang(code){const token=++languageGeneration;const loc=await(await fetch('/api/locale?lang='+encodeURIComponent(code))).json();if(token!==languageGeneration)return;Object.assign(L,{code:loc.code,strings:loc.strings,en:loc.en});localStorage.setItem('poecraft.lang',loc.code);render();}
function invalidate(){generation++;result=null;selection={};setStatus('');render();}
async function submit(source){if(busy)return;const token=++generation;result=null;selection={};busy=true;$('#buildImport').disabled=true;$('#buildFile').disabled=true;$('#buildOpen').disabled=true;setStatus('build.loading');render();try{if(new TextEncoder().encode(source).length>1024*1024)throw Error('build.error.too_large');const response=await fetch('/api/build/import',{method:'POST',headers:{'Content-Type':'application/json','X-Workshop-Build':'1'},body:JSON.stringify({source})});const data=await response.json();if(token!==generation)return;if(!response.ok)throw Error(data.error||'build.error.format');result=data;selection.loadout=(data.loadouts||[]).find(stage=>Object.entries(collections).every(([key,field])=>stage[key]===active(data[field],key)?.id))?.id||'';setStatus('build.loaded');render();}catch(error){if(token===generation)setStatus(String(error.message).startsWith('build.error.')?error.message:'build.error.network',true);}finally{busy=false;$('#buildImport').disabled=false;$('#buildFile').disabled=false;$('#buildOpen').disabled=false;}}
$('#buildForm').addEventListener('submit',e=>{e.preventDefault();submit($('#buildSource').value);});
$('#buildSource').addEventListener('input',invalidate);
$('#buildOpen').addEventListener('click',()=>$('#buildFile').click());
$('#buildFile').addEventListener('change',async e=>{invalidate();const token=generation,file=e.target.files[0];if(!file)return;if(file.size>1024*1024){setStatus('build.error.too_large',true);return;}try{const source=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());if(token!==generation)return;$('#buildSource').value='';await submit(source);}catch{setStatus('build.error.format',true);}});
$('#langSelect').addEventListener('click',e=>{const b=e.target.closest('[data-lang]');if(b)setLang(b.dataset.lang).catch(()=>setStatus('build.error.network',true));});
$('#buildResult').addEventListener('change',e=>{
 if(e.target.id==='treeCompare'){selection.compare=e.target.value;render();}
 else if(e.target.id==='buildStage'){
  selection.loadout=e.target.value;
  const stage=(result.loadouts||[]).find(s=>s.id===selection.loadout);
  if(stage)for(const key of Object.keys(collections))selection[key]=stage[key];
  render();
 }else if(e.target.dataset.set){selection[e.target.dataset.set]=e.target.value;selection.loadout='';render();}
});
mobile.addEventListener('change',render);
(async()=>{L.langs=await(await fetch('/api/locales')).json();await setLang(localStorage.getItem('poecraft.lang')||'en');})().catch(()=>setStatus('build.error.network',true));

fetch('/api/meta').then(r=>{if(!r.ok)throw Error('metadata');return r.json();}).then(meta=>{gameVersion=meta.data?.game_version||null;}).catch(()=>{}).finally(()=>{metadataReady=true;render();});

fetch('/api/trade/leagues').then(r=>r.json()).then(data=>{const select=$('#buildTradeLeague');select.innerHTML=[...new Set([data.league,...data.known])].map(league=>`<option value="${esc(league)}">${esc(league)}</option>`).join('');}).catch(()=>{});
$('#buildResult').addEventListener('click',async event=>{
 const button=event.target.closest('[data-build-trade]');if(!button||button.disabled)return;
 const item=result?.inventory.find(x=>x.id===button.dataset.buildTrade),league=$('#buildTradeLeague').value;
 const popup=window.open('about:blank','_blank');if(popup)popup.opener=null;
 const out=button.nextElementSibling;button.disabled=true;out.textContent=t('build.trade.loading');
 try{const response=await fetch('/api/build/trade',{method:'POST',headers:{'Content-Type':'application/json','X-Workshop-Build':'1'},body:JSON.stringify({item,league,lang:L.code})});const data=await response.json();if(!button.isConnected||league!==$('#buildTradeLeague').value){popup?.close();return;}
 if(!response.ok)throw Error(data.error||'build.trade.error.unavailable');
 out.innerHTML=`<p>${esc(t('build.trade.scope'))}</p>`+(data.dropped?.length?`<p class="build-warning">${esc(t('eval.relaxedMods').replace('{mods}',data.dropped.join('; ')))}</p>`:'')+(data.notes||[]).map(note=>`<p class="build-warning">${esc(note.text)}</p>`).join('');
 if(data.url){const url=new URL(data.url);if(url.protocol!=='https:'||!['www.pathofexile.com','ru.pathofexile.com','de.pathofexile.com'].includes(url.hostname)||!url.pathname.startsWith('/trade/search/'))throw Error('build.trade.error.unavailable');
 const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent=t('build.trade.open');out.append(link);if(popup)popup.location.replace(url.href);else link.focus();
 }else {popup?.close();if(!out.textContent)throw Error('build.trade.error.unavailable');}
 }catch(error){popup?.close();out.textContent=t(String(error.message).startsWith('build.trade.error.')?error.message:'build.trade.error.unavailable');}finally{button.disabled=false;}
});
$('#buildTradeLeague').addEventListener('change',()=>document.querySelectorAll('.build-trade-status').forEach(el=>el.textContent=''));
