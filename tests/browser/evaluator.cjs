const {chromium}=require('playwright');const fs=require('fs');const path=require('path');const os=require('os');
const artifacts=fs.mkdtempSync(path.join(os.tmpdir(),'tenschep-evaluator-'));
// Controlled market responses from evaluator_fixtures.py; no live Trade searches.
const fixtures=JSON.parse(fs.readFileSync(process.env.EVAL_FIXTURES,'utf8'));
(async()=>{const b=await chromium.launch({headless:true});try{
 const p=await b.newPage();const errors=[];const scenes=[];let current='unique',delay=false,offline=false,compatibility=null;
 p.on('pageerror',e=>errors.push(String(e)));
 await p.route('**/api/trade/leagues',r=>r.fulfill({json:{league:'Allflame',known:['Allflame','Standard']}}));
 await p.route('**/api/evaluate',async route=>{const body=route.request().postDataJSON();
  if(offline)return route.fulfill({status:503,json:{error:'Controlled network failure'}});
  const data=JSON.parse(JSON.stringify(fixtures.results[body.lang||'en'][current]));
  data.league=body.league||'Allflame';
  if(compatibility&&data.timeless){data.timeless.provenance.compatibility=compatibility;data.timeless.provenance.reason=compatibility==='compatible'?'verified_versions':compatibility==='missing'?'missing_files':'version_mismatch';if(compatibility!=='compatible'){data.timeless.data=false;delete data.timeless.sockets;delete data.timeless.calc;delete data.timeless.equivalents;}}
  if(delay&&data.league==='Allflame')await new Promise(r=>setTimeout(r,500));
  if(data.league==='Standard'&&data.summary){data.summary.price.amount=222;data.summary.chaos=222;}
  await route.fulfill({json:data});});
 await p.goto('http://127.0.0.1:18777/evaluator');await p.locator('#league').selectOption('Allflame');
 for(const lang of ['en','ru','de']){
  await p.locator('#langSelect [data-lang="'+lang+'"]').click();await p.waitForFunction(l=>document.documentElement.lang===l,lang);
  for(const name of Object.keys(fixtures.texts)){
   current=name;await p.locator('#itemText').fill(fixtures.texts[name]);
   if(name==='unique')await p.locator('#itemText').dispatchEvent('paste');else await p.locator('#evalBtn').click();
   await p.waitForFunction(()=>state.result!==null);
   if(name==='relaxed'&&!await p.locator('.relaxed-mods').isVisible())throw Error('relaxation hidden');
   if(name==='timeless'&&!await p.locator('[data-compatibility="unverified"]').isVisible())throw Error('provenance hidden');
   for(const [width,height] of [[1920,1080],[1440,900],[1280,720],[390,844]]){
    await p.setViewportSize({width,height});const size=await p.evaluate(()=>({w:document.documentElement.clientWidth,s:document.documentElement.scrollWidth}));scenes.push({lang,name,...size});
    if(name==='relaxed'&&(width===1440||width===390))await p.screenshot({path:path.join(artifacts,'evaluator-'+lang+'-'+width+'.png'),fullPage:true});
   }
  }
 }
 for(const lang of ['en','ru','de']){await p.locator('#langSelect [data-lang="'+lang+'"]').click();await p.waitForFunction(l=>document.documentElement.lang===l,lang);for(const status of ['compatible','incompatible','missing']){compatibility=status;current='timeless';await p.locator('#itemText').fill(fixtures.texts.timeless);await p.locator('#evalBtn').click();await p.locator('[data-compatibility="'+status+'"]').waitFor({state:'visible'});if(status!=='compatible'&&await p.locator('#tjCalc').count())throw Error('blocked data calculable');}}compatibility=null;
 current='normal';delay=true;await p.locator('#itemText').fill(fixtures.texts.normal);
 const pending=p.waitForRequest(r=>r.url().includes('/api/evaluate'));await p.locator('#evalBtn').click();await pending;
 await p.locator('#league').selectOption('Standard');await p.waitForTimeout(700);
 if(await p.evaluate(()=>state.result.league)!=='Standard')throw Error('stale league accepted');
 offline=true;await p.locator('#evalBtn').click();await p.locator('#result .error').waitFor({state:'visible'});
 if(await p.locator('#evalBtn').isDisabled())throw Error('cannot retry');
 console.log(JSON.stringify({states:scenes.length,errors,overflow:scenes.filter(x=>x.s>x.w+2),paste:true,leagueRace:true,networkError:true}));
 fs.writeFileSync(path.join(artifacts,'results.json'),JSON.stringify({scenes,errors}));
 if(errors.length||scenes.some(x=>x.s>x.w+2))throw Error('browser regression');
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
