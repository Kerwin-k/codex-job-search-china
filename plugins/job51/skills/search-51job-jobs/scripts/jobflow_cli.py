"""Command line onboarding and environment checks for every platform plugin."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

from config_core import (PLATFORMS, ConfigError, authorize, config_path, data_root,
    effective, load_config, platform_state, private_file, require_apply, template,
    validate_config, validate_profile, write_json, configured_lanes, begin_attempt, dispatch_attempt, resolve_attempt)

def output(value: dict) -> None:
    print(json.dumps(value,ensure_ascii=False,indent=2))

def csv_input(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in value.replace("，",",").split(",") if x.strip()))

def init(args) -> dict:
    if config_path().exists():
        raise ConfigError("config_exists: use_your_editor_to_update")
    cfg=template()
    if args.non_interactive:
        cfg["search"]["cities"]=csv_input(args.cities)
        cfg["search"]["keywords"]=csv_input(args.keywords)
        cfg["search"]["salary"]["monthly_floor"]=args.salary_floor
        cfg["search"]["salary"]["comparison"]=args.salary_comparison
        if args.platforms:cfg["platforms"]=csv_input(args.platforms)
    else:
        print("Codex 国内求职工作流 · 首次配置\n配置与资料保存在源码目录之外。默认只筛选。")
        cfg["platforms"]=csv_input(input("平台 [51job,zhaopin,boss]： ").strip() or "51job,zhaopin,boss")
        cfg["search"]["cities"]=csv_input(input("城市（逗号分隔）： "))
        cfg["search"]["keywords"]=csv_input(input("岗位关键词（逗号分隔）： "))
        cfg["search"]["salary"]["monthly_floor"]=int(input("人民币月薪门槛 [0]： ").strip() or "0")
        answer=input("薪资 8–12K、门槛 10K：下限不满足，上限满足。比较下限还是上限 [lower/upper]： ").strip() or "lower"
        if answer not in ("lower","upper"):raise ConfigError("salary_comparison: choose_lower_or_upper")
        cfg["search"]["salary"]["comparison"]=answer+"_bound"
        cfg["search"]["excluded_keywords"]=csv_input(input("排除关键词（可留空）： "))
    validate_config(cfg)
    source_root=Path(__file__).resolve()
    if data_root()==source_root.parent or source_root.is_relative_to(data_root()):
        raise ConfigError("private_storage_must_be_separate_from_source")
    write_json(config_path(),cfg)
    profile=private_file(cfg["candidate"]["profile_file"])
    if not profile.exists():write_json(profile,{"confirmed":False,"facts":[]})
    greeting=private_file(cfg["candidate"]["greeting_file"])
    if not greeting.exists():greeting.write_text("",encoding="utf-8")
    return {"ok":True,"mode":"review","config_file":str(config_path()),"profile_file":str(profile),"next":"编辑本地事实资料，确认平台默认简历名称，再运行 validate。"}

def source_directory(platform: str) -> Path:
    root=Path(__file__).resolve().parent
    if (root/"boss_bridge.py").exists() or (root/"runtime_config.py").exists():return root
    repo=root.parent
    names={"51job":"job51","zhaopin":"zhaopin","boss":"boss"}
    skills={"51job":"search-51job-jobs","zhaopin":"search-zhaopin-jobs","boss":"search-boss-jobs"}
    return repo/"plugins"/names[platform]/"skills"/skills[platform]/"scripts"

def prepare_boss(port: int) -> dict:
    effective("boss",True)
    if not 1024<=port<=65535:raise ConfigError("bridge_port_out_of_range")
    session_path=platform_state("boss")/"bridge-session.json"
    if session_path.exists():
        session=json.loads(session_path.read_text(encoding="utf-8"))
        if session["port"]!=port:raise ConfigError("bridge_port_changed: stop_bridge_before_reconfiguration")
    else:
        session={"port":port,"token":secrets.token_urlsafe(32)}
        write_json(session_path,session)
    source=source_directory("boss")/"extension"
    target=data_root()/"extensions"/"boss"
    target.mkdir(parents=True,exist_ok=True)
    for name in ("manifest.json","background.js","content.js"):
        text=(source/name).read_text(encoding="utf-8")
        text=text.replace('"__JOBFLOW_BRIDGE_URL__"',json.dumps(f"http://127.0.0.1:{port}"))
        text=text.replace('"__JOBFLOW_BRIDGE_TOKEN__"',json.dumps(session["token"]))
        if name=="manifest.json":
            manifest=json.loads(text)
            manifest["host_permissions"]=["https://*.zhipin.com/*",f"http://127.0.0.1:{port}/*"]
            text=json.dumps(manifest,ensure_ascii=False,indent=2)
        (target/name).write_text(text,encoding="utf-8")
    return {"ok":True,"extension_directory":str(target),"bridge_port":port,"next":"在 Chrome 加载此本地扩展；用 boss_bridge.py serve 启动桥接，随后自行登录 BOSS。"}

def materialize(platform: str) -> dict:
    cfg=effective(platform,True)
    if platform=="boss":return prepare_boss(17897)
    source=source_directory(platform)/"playwright"
    target=data_root()/"runtime"/platform
    target.mkdir(parents=True,exist_ok=True)
    files=[]
    for file in source.glob("*.js"):
        if file.name.endswith("-template.js"):continue
        shutil.copyfile(file,target/file.name)
        files.append(file.name)
    policy=target/"policy.js"
    policy.write_text("globalThis.__jobflowConfig="+json.dumps(cfg["search"],ensure_ascii=False)+";\n"+(source/"policy.js").read_text(encoding="utf-8"),encoding="utf-8")
    install=(source/"install-template.js").read_text(encoding="utf-8")
    for marker,file in (("__RUNTIME_PATH__","browser-runtime.js"),("__POLICY_PATH__","policy.js")):
        install=install.replace(marker,str(target/file).replace("\\","\\\\"))
    fingerprint=hashlib.sha256(install.encode()).hexdigest()
    install=install.replace("__SOURCE_FINGERPRINT__",fingerprint)
    permit=None
    try:permit=require_apply(platform)
    except ConfigError:pass
    install=install.replace("const context = page.context();","const context = page.context();\n  context.__jobflowPermit = "+json.dumps(permit)+";",1)
    (target/"install.js").write_text(install,encoding="utf-8")
    files.append("install.js")
    write_json(target/"manifest.json",{"platform":platform,"sha256":{n:hashlib.sha256((target/n).read_bytes()).hexdigest() for n in files}})
    return {"ok":True,"runtime_directory":str(target),"apply_authorized":permit is not None,"next":"用该平台专属 Playwright MCP 的 browser_run_code 执行 install.js 的函数内容。"}

def doctor(platform: str|None) -> dict:
    checks=[{"name":"python","ok":sys.version_info>=(3,10),"fix":"安装 Python 3.10 或更高版本"}]
    try:cfg=load_config(); checks.append({"name":"config","ok":True})
    except ConfigError as e:cfg=template();checks.append({"name":"config","ok":False,"error":str(e),"fix":"运行 init 或修正本地配置"})
    if any(p in ("51job","zhaopin") for p in ([platform] if platform else cfg["platforms"])):
        checks.append({"name":"node","ok":shutil.which("node") is not None,"fix":"51job 与智联运行时需要 Node.js 18+"})
    try:validate_profile();checks.append({"name":"candidate_facts","ok":True})
    except ConfigError as e:checks.append({"name":"candidate_facts","ok":False,"error":str(e),"fix":"编辑本地事实资料并确认，不要修改源码"})
    for p in ([platform] if platform else cfg["platforms"]):
        if p not in cfg["platforms"]:continue
        checks.append({"name":p+"_resume_label","ok":bool(cfg["candidate"]["resume_labels"].get(p)),"fix":"在 candidate.resume_labels 配置平台实际使用的附件名称"})
    return {"ok":all(c["ok"] for c in checks),"checks":checks,"browser_state":"not_checked","note":"此检查不启动浏览器，不确认登录，不进行投递。"}

def render_submit(platform: str, job_id: str) -> dict:
    if platform=="boss":raise ConfigError("boss: use_send_verified")
    permit=require_apply(platform)
    attempt=begin_attempt(platform,job_id)
    # Reserve before rendering: a tool timeout or disconnect can never silently
    # create another submit script for the same unresolved job.
    dispatch_attempt(platform,job_id)
    source=source_directory(platform)/"playwright"/(platform+"-submit.js")
    function=source.read_text(encoding="utf-8")
    function=function.replace("const context = page.context();","const context = page.context();\n  context.__jobflowPermit="+json.dumps(permit)+";\n  context.__jobflowAttempt="+json.dumps(attempt)+";",1)
    name="submit-"+hashlib.sha256(job_id.encode()).hexdigest()[:16]+".js"
    target=data_root()/"runtime"/platform/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(function,encoding="utf-8")
    return {"ok":True,"submit_script":str(target),"job_id":job_id,"expires_in_seconds":300,"next":"仅执行一次；随后用直接证据 resolve-attempt。任何不确定结果先核对，不能重新生成或重发。"}

def duplicate_summary(company: str, title: str) -> dict:
    def normalized(value):
        return "".join(c for c in str(value or "").casefold() if c.isalnum())
    expected=(normalized(company),normalized(title))
    if not all(expected):raise ConfigError("duplicate_check_identity_required")
    matches=[]
    for platform in PLATFORMS:
        records=[]
        if platform=="boss":
            file=platform_state(platform)/"receipts.jsonl"
            if file.exists():
                try:records=[json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
                except ValueError:raise ConfigError("duplicate_ledger_unreadable") from None
        else:
            file=platform_state(platform)/("confirmed-"+platform+".tsv")
            if file.exists():
                with file.open(encoding="utf-8-sig",newline="") as handle:records=list(csv.DictReader(handle,delimiter="\t"))
        for record in records:
            job=record.get("job",record.get("receipt",record))
            if not isinstance(job,dict):raise ConfigError("duplicate_ledger_unreadable")
            actual=(normalized(job.get("company")),normalized(job.get("title",job.get("job_title"))))
            if actual==expected:
                matches.append(platform);break
    return {"ok":True,"duplicate":bool(matches),"matched_platforms":matches,"comparison":"normalized_company_and_role","read_only":True}

def main() -> int:
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="Codex 国内求职工作流 / 51job · 智联 · BOSS")
    sub=parser.add_subparsers(dest="command",required=True)
    start=sub.add_parser("init",help="首次配置；不会覆盖已有配置")
    start.add_argument("--non-interactive",action="store_true")
    start.add_argument("--cities",default="")
    start.add_argument("--keywords",default="")
    start.add_argument("--salary-floor",type=int,default=0)
    start.add_argument("--salary-comparison",choices=("lower_bound","upper_bound"),default="lower_bound")
    start.add_argument("--platforms",default="")
    sub.add_parser("validate",help="检查有效配置和已确认的事实资料")
    check=sub.add_parser("doctor",help="只读环境检查")
    check.add_argument("--platform",choices=PLATFORMS)
    show=sub.add_parser("config",help="展示本地配置位置或生效配置")
    show.add_argument("--platform",choices=PLATFORMS)
    lane=sub.add_parser("lanes",help="列出配置的搜索组合")
    lane.add_argument("--platform",choices=PLATFORMS,required=True)
    prep=sub.add_parser("prepare",help="生成私有运行时，不操作浏览器")
    prep.add_argument("--platform",choices=PLATFORMS,required=True)
    prep.add_argument("--port",type=int,default=17897)
    auth=sub.add_parser("authorize",help="仅在用户明确授权本次投递后使用")
    auth.add_argument("--platform",choices=PLATFORMS,required=True)
    auth.add_argument("--target",type=int,required=True)
    stop=sub.add_parser("revoke",help="撤销本地投递许可；重建运行时以撤销浏览器内许可")
    stop.add_argument("--platform",choices=PLATFORMS,required=True)
    send=sub.add_parser("render-submit",help="最终投递前保留持久尝试记录并生成单次脚本")
    send.add_argument("--platform",choices=("51job","zhaopin"),required=True)
    send.add_argument("--job-id",required=True)
    resolve=sub.add_parser("resolve-attempt",help="仅依据直接证据确认发送或确实未发送")
    resolve.add_argument("--platform",choices=PLATFORMS,required=True)
    resolve.add_argument("--job-id",required=True)
    resolve.add_argument("--outcome",choices=("sent","not_sent"),required=True)
    resolve.add_argument("--evidence",required=True)
    bind=sub.add_parser("bind-browser",help="绑定独立浏览器配置，不启动浏览器")
    bind.add_argument("--platform",choices=("51job","zhaopin"),required=True)
    bind.add_argument("--browser",choices=("msedge","chrome"),default="msedge")
    bind.add_argument("--profile",required=True,help="浏览器内部配置目录名，可在版本页面查看")
    connect=sub.add_parser("connect",help="显式注册平台专属 MCP，保留其他 Codex 配置")
    connect.add_argument("--platform",choices=("51job","zhaopin"),required=True)
    dedupe=sub.add_parser("dedupe",help="只读跨平台最少字段核对，不改变其他台账")
    dedupe.add_argument("--company",required=True)
    dedupe.add_argument("--title",required=True)
    args=parser.parse_args()
    try:
        if args.command=="init":result=init(args)
        elif args.command=="validate":
            cfg=load_config();profile=validate_profile()
            result={"ok":True,"platforms":cfg["platforms"],"confirmed_fact_count":len(profile["facts"])}
        elif args.command=="doctor":result=doctor(args.platform)
        elif args.command=="config":result={"ok":True,"config_file":str(config_path()),"effective":effective(args.platform,True) if args.platform else None}
        elif args.command=="lanes":
            effective(args.platform,True);result={"ok":True,"lanes":configured_lanes(args.platform)}
        elif args.command=="prepare":result=prepare_boss(args.port) if args.platform=="boss" else materialize(args.platform)
        elif args.command=="authorize":
            permit=authorize(args.platform,args.target);result={"ok":True,"platform":args.platform,"target":permit["target"],"next":"重新 prepare 以加载新许可；到期或配置变化后需重新授权。"}
        elif args.command=="render-submit":result=render_submit(args.platform,args.job_id)
        elif args.command=="resolve-attempt":result=resolve_attempt(args.platform,args.job_id,args.outcome,args.evidence)
        elif args.command=="dedupe":result=duplicate_summary(args.company,args.title)
        elif args.command=="bind-browser":
            if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,80}",args.profile) or args.profile in (".",".."):
                raise ConfigError("browser_profile_directory_name_required")
            effective(args.platform,True)
            binding={"browser":args.browser,"profile":args.profile}
            other="zhaopin" if args.platform=="51job" else "51job"
            other_path=platform_state(other)/"browser-binding.json"
            if other_path.exists() and json.loads(other_path.read_text(encoding="utf-8"))==binding:
                raise ConfigError("platform_browser_profiles_must_differ")
            write_json(platform_state(args.platform)/"browser-binding.json",binding)
            result={"ok":True,"platform":args.platform,"next":"在该配置中安装上游 Playwright 扩展并自行登录；随后运行 connect。"}
        elif args.command=="connect":
            effective(args.platform,True)
            if not (platform_state(args.platform)/"browser-binding.json").exists():raise ConfigError("browser_binding_required")
            codex=shutil.which("codex.exe") or shutil.which("codex") or shutil.which("codex.cmd")
            if not codex:raise ConfigError("codex_cli_missing: register_mcp_manually_using_install_guide")
            executable=[codex]
            if os.name=="nt" and Path(codex).suffix.lower()==".cmd":
                entry=Path(codex).parent/"node_modules"/"@openai"/"codex"/"bin"/"codex.js"
                node=shutil.which("node")
                if not entry.is_file() or not node:raise ConfigError("codex_native_launcher_required: register_mcp_manually")
                executable=[node,str(entry)]
            name="jobflow-"+args.platform
            existing=subprocess.run([*executable,"mcp","get",name],capture_output=True)
            if existing.returncode==0:raise ConfigError("mcp_name_exists: review_existing_entry_before_update")
            launcher=source_directory(args.platform)/"launch_playwright.py"
            completed=subprocess.run([*executable,"mcp","add",name,"--",sys.executable,str(launcher),"--platform",args.platform],capture_output=True)
            if completed.returncode:raise ConfigError("mcp_registration_failed")
            result={"ok":True,"mcp_name":name,"next":"重启会话，使新工具可用；只连接本平台配置。"}
        elif args.command=="revoke":
            (platform_state(args.platform)/"authorization.json").unlink(missing_ok=True)
            result={"ok":True,"platform":args.platform,"next":"51job/智联立即重新 prepare 并运行 install.js；BOSS 每次发送均读取本地许可。"}
        output(result)
        return 0 if result.get("ok") else 1
    except (ConfigError,ValueError,OSError) as e:
        output({"ok":False,"error":str(e) if isinstance(e,(ConfigError,ValueError)) else "local_file_operation_failed"})
        return 1

if __name__=="__main__":raise SystemExit(main())
