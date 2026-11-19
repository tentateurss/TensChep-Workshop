const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(require('path').join(__dirname, '../../poecraft/web/evaluator.js'), 'utf8');
const prefix = source.slice(0, source.indexOf('\nasync function loadTree'));
const {moneyHtml, equivalentsHtml, state, L} = vm.runInNewContext(prefix + '\n;({moneyHtml, equivalentsHtml, state, L})');
const r = {league:'Covens', display_rate:{source:'poe.ninja',league:'Covens',chaos_per_divine:200,expires_at:Date.now()/1000+60}};
for (const language of ['en', 'ru', 'de']) {
  L.code = language;
  state.currency = 'auto';
  assert(moneyHtml({amount:700,currency:'chaos'},700,r).includes('div'));
  assert(moneyHtml({amount:1,currency:'chaos'},1,r).startsWith('1 c'));
  state.currency = 'chaos';
  assert(moneyHtml({amount:3.5,currency:'divine'},700,r).startsWith('700 c'));
  state.currency = 'divine';
  assert(moneyHtml({amount:0.00001,currency:'chaos'},0.00001,r).includes('&lt;0.001'));
  assert(!moneyHtml({amount:0.00001,currency:'chaos'},0.00001,r).includes('0 div'));
  assert.equal(moneyHtml({amount:1,currency:'alch'},0.2,r,true),'1 alch');
  assert.equal(moneyHtml({amount:700,currency:'chaos'},700,{league:'Standard',display_rate:r.display_rate}),'700 chaos');
  assert.equal(moneyHtml({amount:700,currency:'chaos'},700,{league:'Covens'}),'700 chaos');
  assert.equal(moneyHtml({amount:700,currency:'chaos'},700,{league:'Covens',display_rate:{...r.display_rate,expires_at:0}}),'700 chaos');
  const equivalent = {checked:1,exact:1,seeds:[{seed:123,matched:1,of:1,url:'https://example.invalid',listing:{price:{amount:700,currency:'chaos'},chaos:700,online:true}}]};
  assert(equivalentsHtml(equivalent,r).includes('div'));
}
console.log('Evaluator currency formatting PASS (EN/RU/DE)');
