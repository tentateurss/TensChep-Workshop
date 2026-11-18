// Exercise real import/association; metadata responses cover legacy and version mismatch states.
const {chromium}=require('playwright');const fs=require('fs'),path=require('path'),assert=require('assert');
const source=fs.readFileSync(process.env.BUILD_STAGES_XML,'utf8'),base=process.env.BUILD_URL||'http://127.0.0.1:18781';
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}});let gameVersion='3.30.0.2.2';const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/api/meta',r=>r.fulfill({json:{data:{game_version:gameVersion}}}));
 async function load(){await page.goto(base+'/build');const response=page.waitForResponse(r=>r.url().endsWith('/api/build/import'));await page.locator('#buildSource').fill(source);await page.locator('#buildImport').click();const result=await(await response).json();assert.equal(result.loadouts.length,2);await page.locator('#buildStage').waitFor();}
 await load();
 for(const lang of ['en','ru','de']){
  await page.locator('[data-lang="'+lang+'"]').click();await page.waitForFunction(l=>document.documentElement.lang===l,lang);
  await page.locator('#buildStage').selectOption('identifier:early');
  for(const key of ['tree','skills','items'])assert.equal(await page.locator('[data-set="'+key+'"]').inputValue(),'1');
  await page.locator('[data-version-state="mismatch"]').waitFor();assert((await page.locator('.build-version').innerText()).includes('3.29'));
  await page.locator('#buildStage').selectOption('identifier:maps');
  for(const key of ['tree','skills','items'])assert.equal(await page.locator('[data-set="'+key+'"]').inputValue(),'2');
  await page.locator('[data-version-state="match"]').waitFor();
  assert.equal(await page.locator('[data-gem-group][open]').count(),0);assert.equal(await page.locator('[data-item-group][open]').count(),0);
  await page.locator('[data-gem-group]>summary').first().focus();await page.keyboard.press('Enter');assert.equal(await page.locator('[data-gem-group][open]').count(),1);
  await page.locator('[data-item-group]>summary').first().click();assert.equal(await page.locator('[data-item-group][open]').count(),1);
  await page.locator('[data-set="skills"]').selectOption('3');assert.equal(await page.locator('#buildStage').inputValue(),'');
  assert.equal(await page.locator('[data-set="tree"]').inputValue(),'2');assert.equal(await page.locator('[data-set="items"]').inputValue(),'2');
  await page.locator('[data-set="tree"]').selectOption('1');assert.equal(await page.locator('[data-set="skills"]').inputValue(),'3');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+2));
 }
 for(const version of ['3.30.9',null,'garbage']){
  gameVersion=version;await load();await page.locator('#buildStage').selectOption('identifier:maps');await page.locator('[data-version-state="'+(version==='3.30.9'?'match':'unknown')+'"]').waitFor();
 }
 assert.deepEqual(errors,[]);console.log(JSON.stringify({status:'PASS',linkedStages:2,manualSelection:true,versions:['mismatch','match','unknown'],mobileAccordions:true,keyboard:true,languages:3,errors}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
