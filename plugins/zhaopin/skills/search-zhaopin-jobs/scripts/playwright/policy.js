(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;if(root)root.__codexJobPolicy=api;})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  const clean=v=>String(v??'').replace(/\s+/g,' ').trim();
  const config=()=>globalThis.__jobflowConfig||{cities:[],keywords:[],required_keywords:[],excluded_keywords:[],excluded_employer_types:[],salary:{monthly_floor:0,comparison:'lower_bound',unknown:'review'}};
  const salaryBand=value=>{
    let text=clean(value).replace(/[,，]/g,'').replace(/／/g,'/').replace(/[\uE030-\uE039]/g,c=>String(c.charCodeAt(0)-0xE030));
    if(!text||/面议|面谈|\/天|\/日|\/时|\/小时|日薪|时薪|每天|每小时|USD|美元|港币|HKD/i.test(text))return null;
    const annual=/\/年|每年|年薪|年度/.test(text);
    const band=text.match(/(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)?\s*[-–—~至到]\s*(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)?/);
    const single=text.match(/(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)/);
    if(!band&&!single)return null;
    if(band&&!band[2]&&!band[4])return null;
    const amount=(n,u)=>{u=String(u||'').toLowerCase();const factor=u==='万'||u==='w'?10000:u==='千'||u==='k'?1000:1;return Math.round(Number(n)*factor/(annual?12:1));};
    const values=band?[amount(band[1],band[2]||band[4]),amount(band[3],band[4]||band[2])]:[amount(single[1],single[2])];
    return {lower:Math.min(...values),upper:Math.max(...values),period:'month',sourcePeriod:annual?'year':'month'};
  };
  const salaryReason=(salary,lane)=>{
    const band=salaryBand(salary);if(!band)return 'salary_unknown';
    const rule=config().salary;
    const floor=Number(lane?.salaryFloor??rule.monthly_floor);
    const comparison=lane?.salaryComparison??rule.comparison;
    return band[comparison==='upper_bound'?'upper':'lower']<floor?'salary_below_floor':'';
  };
  const employerPatterns={public:/国企|国有企业|央企|事业单位|政府机关/i,headhunter:/猎头|代招|人力资源服务/i,outsourcing:/外包|驻场|常驻客户/i};
  const excluded=text=>{
    const cfg=config();
    if(cfg.excluded_keywords.some(w=>text.toLocaleLowerCase().includes(w.toLocaleLowerCase())))return 'excluded_keyword';
    for(const type of cfg.excluded_employer_types)if(employerPatterns[type]?.test(text))return 'excluded_employer_type';
    return '';
  };
  const triageCard=(card,lane={})=>{
    if(!card?.[0]||!card?.[1]||!card?.[2])return {decision:'review',reason:'identity_missing'};
    if(/已投递|已申请/.test(clean(card[5])))return {decision:'hard_reject',reason:'already_applied'};
    const city=clean(card[3]);const text=card.slice(1,7).map(clean).join(' ');
    if(lane.city&&city&&!city.includes(lane.city))return {decision:'hard_reject',reason:'city_mismatch'};
    const reject=excluded(text);if(reject)return {decision:'hard_reject',reason:reject};
    const salary=salaryReason(card[4],lane);
    if(salary==='salary_below_floor')return {decision:'hard_reject',reason:salary};
    if(salary)return {decision:'review',reason:salary};
    if(config().required_keywords.length&&!config().required_keywords.some(w=>text.toLocaleLowerCase().includes(w.toLocaleLowerCase())))return {decision:'review',reason:'required_keyword_review'};
    return {decision:'pass',reason:''};
  };
  const gateCards=(cards,lane)=>{
    const result={rawCount:cards.length,count:0,eligibleCount:0,reviewCount:0,cards:[],reviewCards:[],skips:{},reviews:{}};
    for(const card of cards){const gate=triageCard(card,lane);if(gate.decision==='hard_reject')result.skips[gate.reason]=(result.skips[gate.reason]||0)+1;else{result.cards.push(card);if(gate.decision==='review'){result.reviewCards.push({jobId:String(card[0]),reason:gate.reason});result.reviews[gate.reason]=(result.reviews[gate.reason]||0)+1;}}}
    result.count=result.cards.length;result.reviewCount=result.reviewCards.length;result.eligibleCount=result.count-result.reviewCount;return result;
  };
  const deterministicDetailReject=detail=>excluded([detail.title,detail.company,detail.companyNature,detail.body,detail.description,detail.jd,detail.jdText,detail.requirements,detail.duties,detail.hard].flat().map(clean).join(' '));
  const bindDetail=(card,detail)=>{
    const normalized=v=>clean(v).toLocaleLowerCase().replace(/[\s（）()【】\[\]·•,，。.-]/g,'');
    const compatible=(a,b)=>{a=normalized(a);b=normalized(b);return !!a&&!!b&&a===b;};
    const errors=[];
    if(String(card?.[0])!==String(detail?.jobId))errors.push('job_id');
    if(!compatible(card?.[1],detail?.title))errors.push('title');
    if(!compatible(card?.[2],detail?.company))errors.push('company');
    const left=salaryBand(card?.[4]),right=salaryBand(detail?.salary);
    if(left&&right&&(Math.abs(left.lower-right.lower)>1||Math.abs(left.upper-right.upper)>1))errors.push('salary');
    return {ok:errors.length===0,error:errors.length?'detail_binding_mismatch':'',mismatches:errors,salaryComparable:!!left&&!!right,salaryAuthority:right?'detail':left?'card':'unknown',effectiveSalary:clean(detail?.salary||card?.[4])};
  };
  const validateFilters=(actual,expected)=>{const errors=[];if(clean(actual?.city)!==clean(expected.city))errors.push('city');if(actual?.fullTime!==true)errors.push('full_time');return {ok:!errors.length,errors};};
  const budgetDecision=state=>Number(state.cityJdsSinceSubmit??state.cityJds??0)>=20?{action:'switch_city',terminal:false,reason:'city_review_budget'}:Number(state.laneJdsSinceSubmit??state.laneJds??0)>=8?{action:'switch_lane',terminal:false,reason:'lane_review_budget'}:{action:'continue',terminal:false};
  return {version:'0.1.0',clean,salaryBand,salaryLower:value=>salaryBand(value)?.lower??null,triageCard,gateCards,deterministicDetailReject,validateFilters,bindDetail,budgetDecision};
});
