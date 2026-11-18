// Real parser/API; only the latency scenario delays an actual response.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),zlib=require('zlib'),assert=require('assert');
const base=process.env.BUILD_URL||'http://127.0.0.1:18781';
const fixture=process.env.BUILD_XML||path.join(__dirname,'../fixtures/pob/OccVortex.xml');
const xml=fs.readFileSync(fixture,'utf8');
const code=zlib.deflateSync(Buffer.from(xml)).toString('base64url');
const artifacts=process.env.BUILD_ARTIFACTS||fs.mkdtempSync(path.join(os.tmpdir(),'Workshop build UI '));fs.mkdirSync(artifacts,{recursive:true});
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(base+'/');await page.locator('a[href="/build"]').click();
 async function openImport(){if(!await page.locator('#buildImportPanel').evaluate(e=>e.open))await page.locator('#buildImportPanel>summary').click();}
 async function enter(source){await openImport();await page.locator('#buildSource').fill(source);const response=page.waitForResponse(r=>r.url().endsWith('/api/build/import'));await page.locator('#buildImport').click();return(await response).json();}
 const result=await enter(code);assert.equal(result.game,'poe1');await page.locator('.build-identity').waitFor();
 if(process.env.BUILD_LINK){const live=await enter(process.env.BUILD_LINK);assert.equal(live.game,'poe1');assert.equal(live.source,process.env.BUILD_LINK);}
 for(const lang of ['en','ru','de']){
  await page.locator('[data-lang="'+lang+'"]').click();await page.waitForFunction(l=>document.documentElement.lang===l,lang);
  for(const set of ['tree','skills','items']){const select=page.locator('[data-set="'+set+'"]');if(await select.count()){const values=await select.locator('option').evaluateAll(o=>o.map(x=>x.value));if(values.length>1)await select.selectOption(values[0]);}}
  const linked=page.locator('#buildStage option');if(await linked.count()>1)await page.locator('#buildStage').selectOption(await linked.nth(1).getAttribute('value'));
  for(const [width,height] of [[1440,900],[390,844]]){
   await page.setViewportSize({width,height});await page.waitForFunction(w=>document.querySelector('#buildImportPanel').open===(w>680),width);await page.screenshot({path:path.join(artifacts,'build-'+lang+'-'+width+'.png'),fullPage:true});
   if(width<680){assert.equal(await page.locator('[data-gem-group][open]').count(),0);assert.equal(await page.locator('[data-item-group][open]').count(),0);}
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+2),'overflow '+lang+width);
   assert(!/\bbuild\.[a-zA-Z_]/.test(await page.locator('#buildResult').innerText()),'missing translation');
  }
 }
 // Local file input uses the same parser, without accepting a server filesystem path.
 const imported=page.waitForResponse(r=>r.url().endsWith('/api/build/import'));await page.locator('#buildFile').setInputFiles(fixture);assert.equal((await(await imported).json()).game,'poe1');
 let rejected=await enter('corrupt!!!');assert.equal(rejected.error,'build.error.format');await page.locator('#buildStatus.error').waitFor();assert.equal(await page.locator('.build-identity').count(),0);
 rejected=await enter('<PathOfBuilding2><Build targetVersion="0_1"/></PathOfBuilding2>');assert.equal(rejected.error,'build.error.poe2');
 // Hostile item text stays text; a minimal build never invents missing statistics.
 await enter('<PathOfBuilding><Build/><Items><Item id="1">Rarity: UNIQUE\n&lt;img src=x onerror=alert(1)&gt;\nTest</Item></Items></PathOfBuilding>');
 assert.equal(await page.locator('#buildResult img').count(),0);assert((await page.locator('#buildResult').innerText()).includes('Nicht angegeben'));
 let release;const delayed=new Promise(resolve=>release=resolve);
 await page.route('**/api/build/import',async route=>{const response=await route.fetch();await delayed;await route.fulfill({response});});
 await openImport();await page.locator('#buildSource').fill(code);const started=page.waitForRequest(r=>r.url().endsWith('/api/build/import'));await page.locator('#buildImport').click();await started;await page.locator('#buildSource').fill('changed while loading');release();await page.waitForFunction(()=>!document.querySelector('#buildImport').disabled);assert.equal(await page.locator('.build-identity').count(),0);await page.unroute('**/api/build/import');
 assert.deepEqual(errors,[]);const report={status:'PASS',screens:6,trees:result.trees.length,skillSets:result.skill_sets.length,itemSets:result.item_sets.length,file:true,code:true,staleResponse:true,poe2Rejected:true,errors,artifacts};fs.writeFileSync(path.join(artifacts,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
