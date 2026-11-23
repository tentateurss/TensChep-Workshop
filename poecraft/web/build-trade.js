"use strict";
// Build equipment reviews reuse server-side mappings; editing never sends a request.
const BuildTradeUI=(()=>{
 const reviews=new Map();
 const key=item=>`${result?.sha256}|${item.id}`;
 const settings=s=>s.settings||s.preview.filters.map(f=>({id:f.id,enabled:f.enabled,choice:f.choice,min:f.min,max:f.max}));
 function query(s){const q=structuredClone(s.preview.query),stats=[];q.query.status.option=s.status||'online';
  delete q.query.filters.armour_filters;delete q.query.filters.socket_filters;
  for(const setting of settings(s)){if(!setting.enabled)continue;const row=s.preview.filters.find(f=>f.id===setting.id),bounds={};
   for(const k of ['min','max'])if(setting[k]!==null)bounds[k]=setting[k];
   if(['defence','socket'].includes(row.kind)){const group=row.kind==='defence'?'armour_filters':'socket_filters';q.query.filters[group]??={filters:{}};q.query.filters[group].filters[row.field]=bounds;}
   else stats.push({id:setting.choice,...(Object.keys(bounds).length?{value:bounds}:{})});
  }q.query.stats=[{type:'and',filters:stats}];return q;
 }
 function content(item){const s=reviews.get(key(item))||{};
  let html=`<button type="button" data-build-trade="${esc(item.id)}" ${s.busy?'disabled':''}>${esc(t('build.trade.review'))}</button>`;
  if(s.preview){const p=s.preview,choices=settings(s);html+=`<form class="build-trade-form" data-trade-form="${esc(item.id)}"><fieldset ${s.busy?'disabled':''}><p>${esc(p.base)} · ${esc(t('build.trade.rarity.'+p.rarity))}</p><p class="muted">${esc(t('build.trade.thresholds'))}</p>`;
   html+=p.filters.map((f,i)=>{const v=choices[i];return `<div class="build-trade-filter"><label class="trade-enabled"><input type="checkbox" data-filter="${f.id}" data-field="enabled" ${v.enabled?'checked':''}>${esc(t(f.label))}${f.reason?'<small class="build-warning">'+esc(t(f.reason))+'</small>':''}</label><label>${esc(t('build.trade.match'))}<select data-filter="${f.id}" data-field="choice">${f.options.map(o=>`<option value="${esc(o.id)}" ${o.id===v.choice?'selected':''}>${esc(o.label.endsWith(' (Local)')?t('build.trade.local'):f.reason==='build.trade.ambiguous'&&o.id===f.options[0].id?t('build.trade.global'):['explicit','implicit','crafted','fractured'].includes(o.label)?t('build.trade.stat.'+o.label):t(o.label))}</option>`).join('')}</select></label><label>${esc(t('build.trade.min'))}<input type="number" step="any" data-filter="${f.id}" data-field="min" value="${v.min??''}" ${f.value===null?'disabled':''}></label><label>${esc(t('build.trade.max'))}<input type="number" step="any" data-filter="${f.id}" data-field="max" value="${v.max??''}" ${f.value===null?'disabled':''}></label></div>`;}).join('');
   html+=`<label>${esc(t('build.trade.sellers'))}<select data-seller-status><option value="online" ${s.status!=='any'?'selected':''}>${esc(t('build.trade.online'))}</option><option value="any" ${s.status==='any'?'selected':''}>${esc(t('build.trade.any'))}</option></select></label>`;
   if(p.skipped.length)html+=`<div class="build-warning"><strong>${esc(t('build.trade.skipped'))}</strong><ul>${p.skipped.map(f=>`<li>${esc(f.line)} — ${esc(t(f.reason))}</li>`).join('')}</ul></div>`;
   html+=`<details class="build-query"><summary>${esc(t('build.trade.actual'))}</summary><pre>${esc(JSON.stringify(query(s),null,2))}</pre></details><button type="submit" ${s.busy?'disabled':''}>${esc(t('build.trade.button'))}</button></fieldset></form>`;
  }
  if(s.busy)html+=`<p role="status">${esc(t('build.trade.loading'))}</p>`;
  if(s.error)html+=`<p class="build-warning" role="alert">${esc(t(s.error))}</p>`;
  if(s.last){html+=`<p role="status">${esc(t('build.trade.'+s.last.state).replace('{n}',String(s.last.total??'?')))}</p>`;
   if(s.last.inferred)html+=`<p class="build-warning">${esc(t('build.trade.cached'))}</p>`;
   if(s.last.url)html+=`<a href="${esc(s.last.url)}" target="_blank" rel="noopener noreferrer">${esc(t('build.trade.open'))}</a>`;
  }return html;
 }
 function html(item){return `<div class="build-trade-review" data-trade-card="${esc(item.id)}">${content(item)}</div>`;}
 function paint(item){document.querySelectorAll(`[data-trade-card="${CSS.escape(item.id)}"]`).forEach(el=>el.innerHTML=content(item));}
 async function post(body){const r=await fetch('/api/build/trade',{method:'POST',headers:{'Content-Type':'application/json','X-Workshop-Build':'1'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'build.trade.error.unavailable');return data;}
 async function prepare(item){const id=key(item),s=reviews.get(id)||{};if(s.busy)return;s.busy=true;s.error=null;reviews.set(id,s);paint(item);
  try{const data=await post({item,preview:true,refresh:true});if(id!==key(item))return;s.preview=data;s.settings=null;s.last=null;}
  catch(e){s.error=String(e.message).startsWith('build.trade.error.')?e.message:'build.trade.error.unavailable';}
  finally{s.busy=false;if(id===key(item))paint(item);}
 }
 function init(){const host=$('#buildResult');
  host.addEventListener('click',e=>{const b=e.target.closest('[data-build-trade]');if(b&&!b.disabled){const item=result?.inventory.find(i=>i.id===b.dataset.buildTrade);if(item)prepare(item);}});
  function edit(e){const form=e.target.closest('[data-trade-form]');if(!form)return;const item=result?.inventory.find(i=>i.id===form.dataset.tradeForm),s=item&&reviews.get(key(item));if(!s?.preview||s.busy)return;
   s.settings=s.preview.filters.map(f=>{const fields=[...form.querySelectorAll('[data-filter]')].filter(el=>el.dataset.filter===f.id),row={id:f.id};for(const el of fields)row[el.dataset.field]=el.type==='checkbox'?el.checked:el.dataset.field==='choice'?el.value:el.value===''?null:Number(el.value);return row;});
   s.status=form.querySelector('[data-seller-status]').value;s.last=null;s.error=null;form.querySelector('.build-query pre').textContent=JSON.stringify(query(s),null,2);
   form.parentElement.querySelectorAll('a,[role="status"],[role="alert"]').forEach(el=>el.remove());
  }host.addEventListener('input',edit);host.addEventListener('change',edit);
  host.addEventListener('submit',async e=>{const form=e.target.closest('[data-trade-form]');if(!form)return;e.preventDefault();if(!form.reportValidity())return;
   const item=result?.inventory.find(i=>i.id===form.dataset.tradeForm),id=item&&key(item),s=id&&reviews.get(id);if(!s?.preview||s.busy)return;
   const league=$('#buildTradeLeague').value,expected=query(s),rows=settings(s);const popup=window.open('about:blank','_blank');if(popup)popup.opener=null;s.busy=true;s.error=null;s.last=null;paint(item);
   try{const data=await post({item,league,lang:L.code,filters:rows,status:s.status||'online',confirm:true,expected_query:expected});if(id!==key(item)||league!==$('#buildTradeLeague').value){popup?.close();return;}
    if(data.url){const u=new URL(data.url);if(u.protocol!=='https:'||!['www.pathofexile.com','ru.pathofexile.com','de.pathofexile.com'].includes(u.hostname)||!u.pathname.startsWith('/trade/search/'))throw Error('build.trade.error.unavailable');if(popup&&!popup.closed)popup.location.replace(u.href);}else popup?.close();s.last=data;
   }catch(e){popup?.close();s.error=String(e.message).startsWith('build.trade.error.')?e.message:'build.trade.error.unavailable';}
   finally{s.busy=false;if(id===key(item))paint(item);}
  });
  $('#buildTradeLeague').addEventListener('change',()=>{for(const s of reviews.values())s.last=null;document.querySelectorAll('[data-trade-card]').forEach(el=>{const item=result?.inventory.find(i=>i.id===el.dataset.tradeCard);if(item)paint(item);});});
 }
 return {html,init,query,clear:()=>reviews.clear()};
})();
BuildTradeUI.init();
