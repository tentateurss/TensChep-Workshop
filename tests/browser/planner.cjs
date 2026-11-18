const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const os=require('os');
const artifacts=fs.mkdtempSync(path.join(os.tmpdir(),'tenschep-browser-'));
// Run against an isolated local server on port 18777; browser storage is temporary.
(async()=>{const b=await chromium.launch({headless:true});try{
const p=await b.newPage({viewport:{width:1440,height:900}});const errors=[];p.on('pageerror',e=>errors.push(String(e)));
await p.goto('http://127.0.0.1:18777/craft');
await p.locator('#tradeLeague').selectOption('Covens');
await p.locator('#baseResults li[data-id]').first().waitFor();
await p.route('**/api/bases?**',async route=>{
 if(new URL(route.request().url()).searchParams.get('q')==='Vaal Regalia')await new Promise(r=>setTimeout(r,500));
 await route.continue();
});
await p.locator('#baseSearch').fill('Vaal Regalia');
if(await p.locator('#baseResults li[data-id]').count())throw Error('stale bases remain selectable');
const vaalId='Metadata/Items/Armours/BodyArmours/BodyInt17';
await p.locator('#baseResults li[data-id="'+vaalId+'"]').click();
await p.waitForFunction(()=>document.querySelector('#baseSelected').textContent.includes('Vaal Regalia'));
await p.locator('#modFilter').fill('maximum life');
await p.waitForTimeout(600);
await p.locator('#families button[data-sig="prefix|base_maximum_life"]').locator('..').locator('summary').click();
await p.locator('#families button[data-sig="prefix|base_maximum_life"]').click();
await p.locator('#targets input[data-i]').fill('100');
await p.locator('#sims').selectOption('0');
const request=p.waitForRequest(r=>r.url().includes('/api/plan')&&r.method()==='POST');
await p.locator('#planBtn').click();
const body=(await request).postDataJSON();
if(body.league!=='Covens')throw Error('league not forwarded');
if(body.base!==vaalId)throw Error('wrong base submitted: '+body.base);
await p.locator('#result .summary .big').waitFor({state:'visible',timeout:60000});
if(!(await p.locator('#result > .card > .meta').first().innerText()).includes('Vaal Regalia'))throw Error('wrong result base');
const warnings=await p.locator('#result > .card > .warn').allTextContents();
if(new Set(warnings).size!==warnings.length)throw Error('duplicate result warnings');
if((await p.locator('#result').innerText()).includes('m.act.'))throw Error('untranslated result');
await p.locator('#result .comparison > summary').click();
await p.locator('#result .strategy-reason summary').first().click();
if(!await p.locator('#result .strategy-reason').first().evaluate(e=>e.open))throw Error('reason did not open');
await p.locator('#result tr[data-s]').last().focus();await p.keyboard.press('Enter');
await p.locator('#result .comparison > summary').click();
const scenes=[];
for(const lang of ['en','ru','de']){
 await p.locator('#langSelect [data-lang="'+lang+'"]').click();await p.waitForTimeout(700);
 const label=await p.locator('#result .summary .label').first().innerText();
 const costLabels={en:'Indicative cost',ru:'Ориентировочная стоимость',de:'Richtwert'};
 if(!label.includes(costLabels[lang]))throw Error('estimated total is not labelled: '+label);
 const resultText=await p.locator('#result').innerText();
 if(/config\/prices\.json|default_essence_by_level|MovementVelocity2Royale|item_level_restriction|remove_crafted_mods|added_mods|forced_mods/.test(resultText))throw Error('technical detail leaked into warnings');
 for(const [width,height] of [[1920,1080],[1440,900],[1280,720],[390,844]]){
  await p.setViewportSize({width,height});await p.screenshot({path:path.join(artifacts,'quality-'+lang+'-'+width+'.png'),fullPage:true});
  const size=await p.evaluate(()=>({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));scenes.push({lang,...size});
 }
}
await p.locator('#pasteBox').fill('Item Class: Boots\nRarity: Rare\nDoom Stride\nCrusader Boots\n--------\nItem Level: 86\n--------\n+82 to maximum Life\n25% increased Movement Speed\n+40% to Fire Resistance');
await p.locator('#importTargets').click();
await p.waitForFunction(()=>document.querySelectorAll('#targets input[data-i]').length===3);
await p.locator('#targets input[data-i]').first().fill('120');
await p.locator('#importHave').click();await p.locator('#clearHave').waitFor({state:'visible'});
const recovery=p.waitForRequest(r=>r.url().endsWith('/api/recover'));
await p.locator('#planBtn').click();
if((await recovery).postDataJSON().league!=='Covens')throw Error('wrong recovery league');
await p.locator('#result .summary .big').waitFor({state:'visible',timeout:60000});
if((await p.locator('#dataInfo').innerText()).includes('planner.'))throw Error('header not localized');
await p.route('**/api/localize',async route=>{await new Promise(r=>setTimeout(r,800));await route.continue();});
const pending=p.waitForRequest(r=>r.url().includes('/api/localize'));
await p.locator('#langSelect [data-lang="en"]').click();await pending;
await p.locator('#tradeLeague').selectOption('Standard');await p.waitForTimeout(1200);
if(await p.locator('#result').innerText())throw Error('old league result retained');
await p.reload();await p.waitForFunction(()=>document.querySelector('#tradeLeague').value==='Standard');
fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify({scenes,errors},null,2));
console.log(JSON.stringify({journey:'PASS',errors,overflow:scenes.filter(r=>r.scroll>r.width+2)}));if(errors.length||scenes.some(r=>r.scroll>r.width+2))throw Error('browser regression');
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
