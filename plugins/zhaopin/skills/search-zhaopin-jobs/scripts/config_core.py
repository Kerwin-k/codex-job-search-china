"""User configuration and private storage. Python standard library only."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote
from contextlib import contextmanager

PLATFORMS = ("51job", "zhaopin", "boss")
# Public platform geography, not a user's preferred cities. Users can add codes.
CITY_CODES = {
    "51job": {"北京":"010000", "上海":"020000", "广州":"030200", "深圳":"040000", "天津":"050000", "重庆":"060000", "南京":"070200", "苏州":"070300", "无锡":"070400", "杭州":"080200", "成都":"090200", "武汉":"180200", "西安":"200200"},
    "zhaopin": {"北京":"530", "上海":"538", "广州":"763", "深圳":"765", "天津":"531", "重庆":"551", "南京":"635", "苏州":"639", "无锡":"636", "杭州":"653", "成都":"801", "武汉":"736", "西安":"854"},
    "boss": {"北京":"101010100", "上海":"101020100", "广州":"101280100", "深圳":"101280600", "天津":"101030100", "重庆":"101040100", "南京":"101190100", "苏州":"101190400", "无锡":"101190200", "杭州":"101210100", "成都":"101270100", "武汉":"101200100", "西安":"101110100"},
}
DEFAULT_SEARCH = {"cities":[], "keywords":[], "expectation_pools":[], "salary":{"monthly_floor":0,"comparison":"lower_bound","unknown":"review"}, "excluded_keywords":[], "excluded_employer_types":[], "required_keywords":[], "page_budget":4, "city_codes":{}}
DEFAULT_EXECUTION = {"mode":"review", "target_count":5, "model":"inherit", "reasoning_effort":"inherit"}

class ConfigError(ValueError):
    pass

def data_root() -> Path:
    custom = os.environ.get("JOBFLOW_HOME")
    base = Path(custom).expanduser() if custom else Path(os.environ.get("LOCALAPPDATA", Path.home()/".local"/"share"))/"codex-job-search-china"
    resolved=base.resolve()
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor/"plugin.json").is_file() or (ancestor/"SKILL.md").is_file() or ((ancestor/"jobflow.py").is_file() and (ancestor/"tools").is_dir()):
            if resolved.is_relative_to(ancestor):
                raise ConfigError("private_storage_must_be_separate_from_source")
    return resolved

def platform_state(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ConfigError("unknown_platform")
    return data_root()/"state"/platform

def config_path() -> Path:
    return data_root()/"config.json"

def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+"."+secrets.token_hex(6)+".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def template() -> dict:
    return {"schema_version":1, "platforms":list(PLATFORMS), "search":copy.deepcopy(DEFAULT_SEARCH), "execution":copy.deepcopy(DEFAULT_EXECUTION), "candidate":{"profile_file":"candidate.local.json", "greeting_file":"greeting.local.txt", "resume_labels":{}}, "platform_overrides":{}}

def keys(value: object, allowed: set[str], name: str) -> dict:
    if not isinstance(value,dict):
        raise ConfigError(name+": object_required")
    if set(value)-allowed:
        raise ConfigError(name+": unknown_fields")
    return value

def string_list(value: object, name: str, required: bool = False) -> list[str]:
    if not isinstance(value,list) or any(not isinstance(x,str) or not x.strip() or len(x)>160 for x in value):
        raise ConfigError(name+": nonempty_strings_required")
    if len(set(value))!=len(value):
        raise ConfigError(name+": duplicates")
    if required and not value:
        raise ConfigError(name+": configure_at_least_one")
    return value

def positive(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(name+": integer_out_of_range")

def merge(base: dict, override: dict) -> dict:
    out=copy.deepcopy(base)
    for k,v in override.items():
        out[k]=merge(out[k],v) if isinstance(v,dict) and isinstance(out.get(k),dict) else copy.deepcopy(v)
    return out

def validate_search(raw: dict, ready: bool) -> dict:
    keys(raw,set(DEFAULT_SEARCH),"search")
    search=merge(DEFAULT_SEARCH,raw)
    for field in ("cities","keywords","expectation_pools","excluded_keywords","required_keywords","excluded_employer_types"):
        string_list(search[field],"search."+field, ready and field=="cities")
    if ready and not (search["keywords"] or search["expectation_pools"]):
        raise ConfigError("search: keyword_or_expectation_pool_required")
    if set(search["excluded_employer_types"])-{"public","headhunter","outsourcing"}:
        raise ConfigError("excluded_employer_types: unsupported_value")
    salary=keys(search["salary"],{"monthly_floor","comparison","unknown"},"salary")
    positive(salary["monthly_floor"],"salary.monthly_floor",0,1000000)
    if salary["comparison"] not in ("lower_bound","upper_bound") or salary["unknown"]!="review":
        raise ConfigError("salary: unsupported_policy")
    positive(search["page_budget"],"page_budget",1,20)
    codes=search["city_codes"]
    if not isinstance(codes,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r"\d{3,12}",v) for k,v in codes.items()):
        raise ConfigError("city_codes: numeric_string_mapping_required")
    return search

def validate_execution(raw: dict) -> dict:
    keys(raw,set(DEFAULT_EXECUTION),"execution")
    exe=merge(DEFAULT_EXECUTION,raw)
    if exe["mode"] not in ("review","draft","apply"):
        raise ConfigError("execution.mode: unsupported_value")
    positive(exe["target_count"],"target_count",1,1000)
    for key in ("model","reasoning_effort"):
        if not isinstance(exe[key],str) or not exe[key].strip():
            raise ConfigError("execution."+key+": string_required")
    return exe

def validate_config(raw: dict, ready: bool=True) -> dict:
    keys(raw,set(template()),"config")
    if raw.get("schema_version")!=1:
        raise ConfigError("config: unsupported_schema_version")
    platforms=string_list(raw.get("platforms"),"platforms",True)
    if set(platforms)-set(PLATFORMS):
        raise ConfigError("platforms: unsupported_value")
    out=merge(template(),raw)
    out["search"]=validate_search(out["search"],ready)
    out["execution"]=validate_execution(out["execution"])
    cand=keys(out["candidate"],{"profile_file","greeting_file","resume_labels"},"candidate")
    for key in ("profile_file","greeting_file"):
        if not isinstance(cand[key],str) or not cand[key].strip():
            raise ConfigError("candidate."+key+": path_required")
    if not isinstance(cand["resume_labels"],dict) or any(k not in PLATFORMS or not isinstance(v,str) or not v.strip() for k,v in cand["resume_labels"].items()):
        raise ConfigError("resume_labels: platform_string_mapping_required")
    overrides=keys(out["platform_overrides"],set(PLATFORMS),"platform_overrides")
    for p,override in overrides.items():
        keys(override,{"search","execution"},"platform_override")
        if "search" in override:
            keys(override["search"],set(DEFAULT_SEARCH),"override.search")
        if "execution" in override:
            keys(override["execution"],set(DEFAULT_EXECUTION),"override.execution")
        validate_search(merge(out["search"],override.get("search",{})),ready)
        validate_execution(merge(out["execution"],override.get("execution",{})))
    for p in platforms:
        search=merge(out["search"],overrides.get(p,{}).get("search",{}))
        if ready:
            for city in search["cities"]:
                if city not in search["city_codes"] and city not in CITY_CODES[p]:
                    raise ConfigError(p+": city_code_required")
    return out

def load_config(ready: bool=True) -> dict:
    if not config_path().is_file():
        if ready:
            raise ConfigError("config_missing: run_init")
        return template()
    try:
        raw=json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError,json.JSONDecodeError):
        raise ConfigError("config_invalid_json") from None
    return validate_config(raw,ready)

def effective(platform: str, ready: bool=False) -> dict:
    if platform not in PLATFORMS:
        raise ConfigError("unknown_platform")
    cfg=load_config(ready)
    if ready and platform not in cfg["platforms"]:
        raise ConfigError("platform_disabled")
    return merge(cfg,cfg["platform_overrides"].get(platform,{}))

def private_file(name: str) -> Path:
    path=Path(name).expanduser()
    return (path if path.is_absolute() else data_root()/path).resolve()

def validate_profile() -> dict:
    cfg=load_config()
    try:
        raw=json.loads(private_file(cfg["candidate"]["profile_file"]).read_text(encoding="utf-8-sig"))
    except (OSError,json.JSONDecodeError):
        raise ConfigError("profile_missing_or_invalid") from None
    keys(raw,{"confirmed","facts"},"profile")
    if raw.get("confirmed") is not True or not isinstance(raw.get("facts"),list) or not raw["facts"]:
        raise ConfigError("profile: confirmed_facts_required")
    ids=set()
    for fact in raw["facts"]:
        keys(fact,{"id","text","source"},"fact")
        if any(not isinstance(fact.get(k),str) or not fact[k].strip() for k in ("id","text","source")) or fact["id"] in ids:
            raise ConfigError("profile: unique_ids_and_evidence_required")
        ids.add(fact["id"])
    return raw

def config_digest(platform: str) -> str:
    cfg=effective(platform,True)
    profile=validate_profile()
    greeting=private_file(cfg["candidate"]["greeting_file"])
    text=greeting.read_text(encoding="utf-8") if greeting.is_file() else ""
    payload=json.dumps({"config":cfg,"profile":profile,"greeting":text},sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()

def authorize(platform: str, target: int, duration: int=7200) -> dict:
    positive(target,"target",1,1000)
    cfg=effective(platform,True)
    validate_profile()
    if not cfg["candidate"]["resume_labels"].get(platform):
        raise ConfigError("resume_label_required: confirm_platform_default_attachment")
    permit={"schema":1,"platform":platform,"target":target,"expires_at":time.time()+duration,"config_digest":config_digest(platform),"permit_id":secrets.token_hex(16)}
    write_json(platform_state(platform)/"authorization.json",permit)
    return permit

def require_apply(platform: str) -> dict:
    effective(platform,True)
    try:
        permit=json.loads((platform_state(platform)/"authorization.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):
        raise ConfigError("apply_authorization_required") from None
    if permit.get("platform")!=platform or permit.get("expires_at",0)<=time.time():
        raise ConfigError("apply_authorization_expired")
    if permit.get("config_digest")!=config_digest(platform):
        raise ConfigError("configuration_changed: reauthorize")
    return permit

@contextmanager
def attempt_store(platform: str):
    directory=platform_state(platform)
    directory.mkdir(parents=True,exist_ok=True)
    lock=directory/"send-attempts.lock"
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:raise ConfigError("attempt_store_locked: reconcile_before_retry") from None
    try:
        os.close(fd)
        path=directory/"send-attempts.json"
        try:records=json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError,json.JSONDecodeError):raise ConfigError("attempt_store_corrupt") from None
        if not isinstance(records,list):raise ConfigError("attempt_store_corrupt")
        yield records
        write_json(path,records)
    finally:lock.unlink(missing_ok=True)

def begin_attempt(platform: str, job_id: str) -> dict:
    permit=require_apply(platform)
    if not isinstance(job_id,str) or not job_id.strip() or len(job_id)>160:
        raise ConfigError("job_id_required")
    with attempt_store(platform) as records:
        if any(r["job_id"]==job_id and r["status"]!="not_sent" for r in records):
            raise ConfigError("prior_send_attempt_requires_reconciliation")
        if sum(r["permit_id"]==permit["permit_id"] and r["status"]!="not_sent" for r in records)>=permit["target"]:
            raise ConfigError("authorized_target_reached")
        record={"attempt_id":secrets.token_hex(16),"permit_id":permit["permit_id"],"job_id":job_id,"status":"prepared","created_at":time.time(),"expires_at":min(permit["expires_at"],time.time()+300)}
        records.append(record)
    return record

def dispatch_attempt(platform: str, job_id: str) -> dict:
    permit=require_apply(platform)
    with attempt_store(platform) as records:
        selected=next((r for r in reversed(records) if r["job_id"]==job_id),None)
        if not selected or selected["permit_id"]!=permit["permit_id"] or selected["status"]!="prepared" or selected["expires_at"]<=time.time():
            raise ConfigError("prepared_send_attempt_required")
        selected["status"]="dispatched"
        selected["dispatched_at"]=time.time()
        result=copy.deepcopy(selected)
    return result

def resolve_attempt(platform: str, job_id: str, outcome: str, evidence: str) -> dict:
    if outcome not in ("sent","not_sent") or not evidence.strip():
        raise ConfigError("direct_evidence_required")
    with attempt_store(platform) as records:
        selected=next((r for r in reversed(records) if r["job_id"]==job_id),None)
        if not selected:raise ConfigError("send_attempt_missing")
        if selected["status"] in ("sent","not_sent") and selected["status"]!=outcome:
            raise ConfigError("terminal_attempt_cannot_be_reclassified")
        selected.update(status=outcome,evidence=evidence[:500],resolved_at=time.time())
    return {"ok":True,"job_id":job_id,"outcome":outcome}

def configured_lanes(platform: str) -> list[dict]:
    cfg=effective(platform)
    search=cfg["search"]
    result=[]
    groups=[("keyword",k) for k in search["keywords"]]
    if platform=="zhaopin":
        groups=[("expectation_pool",k) for k in search["expectation_pools"]]+groups
    for source,keyword in groups:
        for city in search["cities"]:
            code=search["city_codes"].get(city,CITY_CODES[platform].get(city))
            if not code:
                raise ConfigError("city_code_required")
            url=(f"https://we.51job.com/pc/search?jobArea={code}&keyword={quote(keyword)}" if platform=="51job" else f"https://sou.zhaopin.com/?jl={code}&kw={quote(keyword)}&et=2" if source=="keyword" else "https://www.zhaopin.com/jobs/?pageMode=recommend")
            item={"id":f"r1-t1-{city}-{source}-{keyword.replace('/','_')}","platform":platform,"round":1,"tier":1,"city":city,"cityCode":code,"keyword":keyword,"source":source,"salaryFloor":search["salary"]["monthly_floor"],"salaryComparison":search["salary"]["comparison"],"pageBudget":search["page_budget"],"searchUrl":url}
            if source=="expectation_pool":item["poolLabel"]=keyword
            result.append(item)
    return result

def salary_band(value: object) -> tuple[int,int] | None:
    text=str(value or "").translate(str.maketrans({chr(0xE030+i):str(i) for i in range(10)})).replace(",","").replace("／","/")
    if re.search(r"面议|面谈|/天|/日|/时|/小时|日薪|时薪|每天|每小时|USD|美元|港币|HKD",text,re.I):return None
    annual=bool(re.search(r"/年|每年|年薪|年度",text))
    match=re.search(r"(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)?\s*[-–—~至到]\s*(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)?",text)
    if match:
        if not (match[2] or match[4]):return None
        values=[(float(match[1]),match[2] or match[4]),(float(match[3]),match[4] or match[2])]
    else:
        match=re.search(r"(\d+(?:\.\d+)?)\s*(万|千|[kKwW]|元)",text)
        if not match:return None
        values=[(float(match[1]),match[2])]
    out=[]
    for number,unit in values:
        amount=number*(10000 if unit.lower() in ("万","w") else 1000 if unit.lower() in ("千","k") else 1)
        out.append(round(amount/12 if annual else amount))
    return min(out),max(out)

def salary_pass(value: object, platform: str) -> bool | None:
    band=salary_band(value)
    if band is None:return None
    rule=effective(platform)["search"]["salary"]
    return band[0 if rule["comparison"]=="lower_bound" else 1]>=rule["monthly_floor"]

def card_reason(job: dict, platform: str) -> str:
    search=effective(platform)["search"]
    text=" ".join(str(job.get(k) or "") for k in ("title","company","summary","signals","tags","company_type"))
    if job.get("city") and search["cities"] and not any(c in str(job["city"]) for c in search["cities"]):return "city_mismatch"
    if any(word.casefold() in text.casefold() for word in search["excluded_keywords"]):return "excluded_keyword"
    patterns={"public":r"国企|国有企业|央企|事业单位|政府机关", "headhunter":r"猎头|代招|人力资源服务", "outsourcing":r"外包|驻场|常驻客户"}
    for kind in search["excluded_employer_types"]:
        if re.search(patterns[kind],text,re.I):return "excluded_employer_type"
    if salary_pass(job.get("salary"),platform) is False:return "salary_below_floor"
    return ""

def priority(job: dict, platform: str) -> int:
    text=" ".join(str(job.get(k) or "") for k in ("title","summary","signals"))
    return sum(10 if word.casefold() in str(job.get("title","")).casefold() else 2 for word in effective(platform)["search"]["keywords"] if word.casefold() in text.casefold())
