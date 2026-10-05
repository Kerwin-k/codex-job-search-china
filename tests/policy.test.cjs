const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const policySource=fs.readFileSync(path.join(root,'tools/policy.js'),'utf8');
function policy(overrides={}){
  const config={cities:['合成城市'],keywords:['产品设计'],required_keywords:[],excluded_keywords:[],excluded_employer_types:[],salary:{monthly_floor:10000,comparison:'lower_bound',unknown:'review'},...overrides};
  const sandbox={__jobflowConfig:config};vm.createContext(sandbox);vm.runInContext(policySource,sandbox);return sandbox.__codexJobPolicy;
}
const card=['synthetic-job','高级设计师','合成公司','合成城市','15-20K','立即投递','','https://example.invalid/synthetic'];
test('different career and senior title are not inherited exclusions',()=>assert.equal(policy().triageCard(card,{}).decision,'pass'));
test('user excluded terms are respected',()=>assert.equal(policy({excluded_keywords:['高级']}).triageCard(card,{}).reason,'excluded_keyword'));
test('lower bound rejects crossing salary',()=>assert.equal(policy().triageCard([...card.slice(0,4),'8-12K',...card.slice(5)],{}).reason,'salary_below_floor'));
test('upper bound allows crossing salary',()=>assert.equal(policy({salary:{monthly_floor:10000,comparison:'upper_bound'}}).triageCard([...card.slice(0,4),'8-12K',...card.slice(5)],{}).decision,'pass'));
test('unknown salaries are reviewed',()=>assert.equal(policy().triageCard([...card.slice(0,4),'面议',...card.slice(5)],{}).decision,'review'));
test('daily and foreign pay stay unknown',()=>{for(const text of ['500元/天','100元/小时','USD 20000'])assert.equal(policy().salaryBand(text),null);});
test('annual pay normalizes to month',()=>{const b=policy().salaryBand('12-24万/年');assert.equal(b.lower,10000);assert.equal(b.upper,20000);});
test('exact job ID mismatch blocks detail binding',()=>assert.equal(policy().bindDetail(card,{jobId:'other',title:card[1],company:card[2],salary:card[4]}).ok,false));
test('company mismatch blocks detail binding',()=>assert.equal(policy().bindDetail(card,{jobId:card[0],title:card[1],company:'另一合成公司',salary:card[4]}).ok,false));
test('salary disagreement blocks detail binding',()=>assert.equal(policy().bindDetail(card,{jobId:card[0],title:card[1],company:card[2],salary:'20-30K'}).ok,false));
test('missing required keywords reviews rather than rejects',()=>assert.equal(policy({required_keywords:['合成证据词']}).triageCard(card,{}).decision,'review'));

function submitGuard(platform){
  const plugin=platform==='51job'?'job51':'zhaopin';const skill=platform==='51job'?'search-51job-jobs':'search-zhaopin-jobs';
  const source=fs.readFileSync(path.join(root,'plugins',plugin,'skills',skill,'scripts/playwright/install-template.js'),'utf8');
  const begin=source.indexOf('  const jobflowRawSubmit');const end=source.indexOf('  return { ok: true, version:',begin);
  let triggers=0;const context={__codexJobSubmit:async()=>{triggers++;return {ok:true};},__codexActiveDetail:{candidate:['synthetic-job']},__codexLane:{salaryFloor:10000,salaryComparison:'lower_bound'}};
  new Function('context',source.slice(begin,end))(context);
  const permit={platform,permit_id:'synthetic-permit',target:1,expires_at:Date.now()/1000+300};
  const attempt={job_id:'synthetic-job',permit_id:'synthetic-permit',expires_at:Date.now()/1000+300};
  const page={evaluate:async(fn,args)=>typeof args==='string'?{salary:'15-20K'}:true};
  return {context,permit,attempt,page,triggers:()=>triggers};
}
for(const platform of ['51job','zhaopin']){
  test(`${platform}: no authorization triggers no action`,async()=>{const s=submitGuard(platform);const r=await s.context.__codexJobSubmit(s.page,platform);assert.equal(r.error,'apply_authorization_required');assert.equal(s.triggers(),0);});
  test(`${platform}: no durable attempt triggers no action`,async()=>{const s=submitGuard(platform);s.context.__jobflowPermit=s.permit;const r=await s.context.__codexJobSubmit(s.page,platform);assert.equal(r.error,'prepared_send_attempt_required');assert.equal(s.triggers(),0);});
  test(`${platform}: expired attempt triggers no action`,async()=>{const s=submitGuard(platform);s.context.__jobflowPermit=s.permit;s.context.__jobflowAttempt={...s.attempt,expires_at:0};const r=await s.context.__codexJobSubmit(s.page,platform);assert.equal(r.error,'prepared_send_attempt_required');assert.equal(s.triggers(),0);});
  test(`${platform}: uncertain second attempt cannot trigger twice`,async()=>{const s=submitGuard(platform);s.context.__jobflowPermit=s.permit;s.context.__jobflowAttempt=s.attempt;assert.equal((await s.context.__codexJobSubmit(s.page,platform)).ok,true);const r=await s.context.__codexJobSubmit(s.page,platform);assert.equal(r.error,'prior_send_attempt_requires_reconciliation');assert.equal(s.triggers(),1);});
  test(`${platform}: unknown detail salary triggers no action`,async()=>{const s=submitGuard(platform);s.context.__jobflowPermit=s.permit;s.context.__jobflowAttempt=s.attempt;s.page.evaluate=async()=>null;const r=await s.context.__codexJobSubmit(s.page,platform);assert.equal(r.error,'detail_salary_unverified');assert.equal(s.triggers(),0);});
}
