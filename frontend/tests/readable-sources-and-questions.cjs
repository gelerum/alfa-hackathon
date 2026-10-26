const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map(), timers = [];
function element() { return {dataset:{}, listeners:{}, classList:{add(){},remove(){}}, innerHTML:'', textContent:'', value:'', addEventListener(type, handler){this.listeners[type]=handler;}, setAttribute(){}}; }
const $ = key => { if (!nodes.has(key)) nodes.set(key,element()); return nodes.get(key); };
let posts = 0, jobStatus = 'running', failPoll = false;
const context = vm.createContext({
  document:{querySelector:$,querySelectorAll(){return [];},createElement:element}, FormData, console,
  crypto:require('node:crypto').webcrypto, URL, Date,
  setTimeout(callback){timers.push(callback);return timers.length;},clearTimeout(){},
  localStorage:{getItem(){return '';},setItem(){}},
  async fetch(url,options){
    let data;
    if(url.endsWith('/questions')) {posts++;data={job_id:'job-1'};}
    else if(url==='/api/jobs/job-1') {if(failPoll)throw new Error('offline');data={status:jobStatus,result:{answer:'Проверьте сроки поставок.',source_details:[]}};}
    else if(url.endsWith('/discussions')) data=[];
    else throw new Error('Unexpected '+url);
    return {ok:true,status:200,json:async()=>data};
  }
});
const app=fs.readFileSync(path.join(__dirname,'..','app.js'),'utf8').replace(/\ninitialize\(\);\s*$/,'');
vm.runInContext(app,context);
vm.runInContext("reportView={id:'report-1',version:'version-1'}",context);
context.sampleSource={source_type:'bank_import',source_date:null,excerpt:JSON.stringify({bank_record_id:537,company_id:'U_2222',source_snapshot_id:'private-snapshot',fts_country_name:'ОАЭ, КИТАЙ, ТУНИС',raw_attributes:{REVENUE_COMPANY:'16083708340'},revenue_value:'16083708340',currency:null})};
const rendered=vm.runInContext("sourceHTML(sampleSource,{field:'fts_country_name',value:'ОАЭ, КИТАЙ, ТУНИС'})",context);
assert.match(rendered,/Страны в сведениях ФТС/);
assert.match(rendered,/ОАЭ, Китай, Тунис/);
assert.match(rendered,/Дата исходных данных не указана/);
assert.doesNotMatch(rendered,/bank_record_id|fts_country_name|raw_attributes|source_snapshot_id|private-snapshot|U_2222|16083708340/);
assert.match(vm.runInContext("sourceHTML({source_url:'javascript:alert(1)',excerpt:'<img onerror=alert(1)>'})",context),/&lt;img/);
assert.doesNotMatch(vm.runInContext("sourceHTML({source_url:'javascript:alert(1)',excerpt:'x'})",context),/href=/);
assert.match(vm.runInContext("sourceHTML({source_type:'bank_analytics',document_id:'doc-1',excerpt:'fts_country_name is an exact quote'})",context),/Аналитика Альфа-Банка/);
assert.match(vm.runInContext("sourceHTML({excerpt:'fts_country_name is an exact quote'})",context),/fts_country_name is an exact quote/);
assert.equal(vm.runInContext("humanText('fts_country_name — ОАЭ')",context),'страны в сведениях ФТС — ОАЭ');
assert.equal(vm.runInContext("money(16000000000)",context),'16 млрд');
assert.equal(vm.runInContext("money(16000000000,'RUB')",context),'16 млрд ₽');
assert.match(vm.runInContext("sourceHTML({excerpt:'{incomplete metadata'})",context),/Структурированные сведения/);
assert.doesNotMatch(vm.runInContext("sourceHTML({excerpt:'{incomplete metadata'})",context),/incomplete metadata/);

context.qaClaims=[
  {kind:'verified_fact',certainty:'confirmed',statement:'Факт'},
  {kind:'bank_position',certainty:'qualified',statement:'Позиция'},
  {kind:'ai_inference',certainty:'qualified',statement:'Вывод'},
  {kind:'risk',certainty:'qualified',statement:'Риск'},
  {kind:'verified_fact',certainty:'unverified',statement:'Пока не проверено'}
];
const groups=vm.runInContext('evidenceGroups(qaClaims)',context);
assert.match(groups,/Факты из источников/);assert.match(groups,/Позиция Альфа-Банка/);
assert.match(groups,/Выводы модели/);assert.match(groups,/Гипотезы и непроверенные сведения/);
assert.match(groups,/не официальная позиция Альфа-Банка/);
assert.equal(vm.runInContext('claimGroup(qaClaims[4])',context),'hypotheses');
assert.equal(vm.runInContext("revenueText(16000000000,'RUB',2025)",context),'выручка 16 млрд ₽ за 2025 год');
assert.equal(vm.runInContext("revenueText(null,'RUB',2025)",context),'');
assert.equal(vm.runInContext("factValue({field:'revenue_value',value:'16000000000',currency:'RUB',revenue_year:2025})",context),'16 млрд ₽ · 2025 г.');
assert.match(vm.runInContext("factValue({field:'revenue_value',value:'16000000000'})",context),/валюта или период не указаны/);

const input=element(),button=element(),status=element(),answer=element(),form=element();
form.dataset.insight='card-1';input.value='Что проверить?';
form.querySelector=selector=>selector==='input'?input:button;
form.parentElement={querySelector:selector=>selector==='.answer'?answer:status};
const event={preventDefault(){},target:{closest(){return form;}}};
const flush=async()=>{for(let n=0;n<15;n++)await Promise.resolve();};
(async()=>{
  const first=$('#reportBody').listeners.submit(event);
  await $('#reportBody').listeners.submit(event);
  await flush();
  assert.equal(posts,1,'Double submit must not create a second question');
  assert.equal(button.disabled,true);
  assert.match(button.textContent,/Готовим ответ/);
  assert.match(status.textContent,/Готовим ответ по источникам/);
  jobStatus='completed';timers.shift()();await first;await flush();
  assert.equal(button.disabled,false);
  assert.match(answer.innerHTML,/Проверьте сроки поставок/);
  assert.match(status.textContent,/Ответ готов и сохранён/);

  input.value='Уточните сроки';failPoll=true;
  await $('#reportBody').listeners.submit(event);await flush();
  assert.equal(posts,2);
  assert.equal(button.textContent,'Проверить ответ');
  assert.equal(input.disabled,true);
  failPoll=false;
  await $('#reportBody').listeners.submit(event);await flush();
  assert.equal(posts,2,'Resuming after a network error must reuse the accepted job');
  assert.equal(input.disabled,false);
  console.log('Readable evidence, no invented currency, escaped content, question progress, duplicate suppression and resume: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
