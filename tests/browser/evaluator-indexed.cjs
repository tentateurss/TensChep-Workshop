const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'poecraft/web/evaluator.js'), 'utf8');
const api = vm.runInNewContext(source.slice(0, source.indexOf('\nasync function evaluate(')) +
  '\n;({L,indexedDate,indexedHtml,marketMeta})', {document:{querySelector:()=>({})}, fetch:()=>{throw Error('No requests');}});
api.L.en = JSON.parse(fs.readFileSync(path.join(root, 'poecraft/locales/en.json'), 'utf8'));
for (const language of ['en','ru','de']) {
  api.L.code = language;
  api.L.strings = JSON.parse(fs.readFileSync(path.join(root, `poecraft/locales/${language}.json`), 'utf8'));
  for (const value of [null, undefined, '', '0', 'bad', '2026-02-30T12:00:00Z', '2999-01-01T00:00:00Z']) {
    assert.equal(api.indexedDate(value), null);
    assert(api.indexedHtml(value).includes(api.L.strings['eval.indexed.unknown']));
  }
  assert(api.indexedDate('2025-10-01T12:00:00+02:00'));
  const html = api.marketMeta({league:'Covens',listings:[{indexed:'2025-10-01T12:00:00Z'},{indexed:'bad'}]});
  for (const key of ['eval.indexed.oldest','eval.indexed.someUnknown','eval.indexed.note'])
    assert(html.includes(api.L.strings[key]));
  assert(html.includes('2025-10-01T12:00:00.000Z'));
  assert(!api.marketMeta({league:'Standard',quote:{fetched_at:null}}).includes('Covens'));
  assert(api.marketMeta({quote:{fetched_at:null}}).includes(api.L.strings['eval.freshness.unknown']));
}
console.log('PASS indexed dates and honest unknown states EN/RU/DE; rendering makes no requests');
