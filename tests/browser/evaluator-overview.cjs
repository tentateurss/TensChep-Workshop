const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'poecraft/web/evaluator.js'), 'utf8');
const panel = {innerHTML: ''};
const {state, L, renderResult, marketMeta} = vm.runInNewContext(
  source.slice(0, source.indexOf('\nasync function evaluate(')) + '\n;({state,L,renderResult,marketMeta})',
  {document: {querySelector: () => panel}, fetch: () => {throw Error('Rendering must not request prices');}});
L.en = JSON.parse(fs.readFileSync(path.join(root, 'poecraft/locales/en.json'), 'utf8'));
for (const language of ['en', 'ru', 'de']) {
  L.code = language;
  L.strings = JSON.parse(fs.readFileSync(path.join(root, `poecraft/locales/${language}.json`), 'utf8'));
  state.filterDraft = [{enabled: true, min: 70, max: 90}];
  state.result = {kind:'rare', league:'Covens', item:{type:'Long name '.repeat(20)},
    passes:[{status:'online', total:1, dropped:[]}], listings:[],
    notes:[{text:'Visible accuracy warning', level:'info'}, {text:'HTTP 429', level:'error'}],
    filters:[{label:'Life <script>unsafe</script>',editable:true,numeric:true,min:70,max:90,reason:'Included'}],
    considered_properties:['Life'], unmatched_properties:['Unmatched property']};
  renderResult();
  const html = panel.innerHTML;
  assert(html.includes('evaluation-overview'));
  assert(html.includes('assessment-warnings'));
  assert(html.indexOf('Visible accuracy warning') < html.indexOf('<details'));
  assert(html.includes('role="alert">HTTP 429'));
  assert(!/<details[^>]*\sopen[\s>]/.test(html));
  assert(html.includes('Life &lt;script&gt;unsafe&lt;/script&gt;'));
  const metadata = marketMeta({league:'Covens',passes:[{}],economy:{fetched_at:'2001-01-01T00:00:00Z'}});
  assert(metadata.includes(L.strings['eval.market.unknownAge']));
  assert(!metadata.includes('2001'));
  state.result = {kind:'unique',league:'Covens',item:{name:'Unique',type:'Ring'},notes:[{text:L.strings['m.eval.unique_scope'],level:'info'}]};
  renderResult();
  assert(panel.innerHTML.includes('assessment-warnings')); // no summary: its scope warning must stay visible
}
console.log('Evaluator overview PASS: visible warnings, collapsed details, safe text and honest freshness (EN/RU/DE)');
