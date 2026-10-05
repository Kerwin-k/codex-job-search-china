#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
from urllib.parse import quote

import config_core as jobflow

DEFAULT_WORKBENCH_STATE = jobflow.platform_state("51job") / "51job-workbench.json"

CITIES = [(c, jobflow.CITY_CODES["51job"].get(c, "")) for c in jobflow.effective("51job")["search"]["cities"]]
CITY_TIERS = [CITIES]
ROUNDS = [(1, jobflow.effective("51job")["search"]["page_budget"], jobflow.effective("51job")["search"]["keywords"])]

def lanes(platform="51job"):
    if platform != "51job": raise ValueError("platform_not_supported")
    return jobflow.configured_lanes(platform)

def write_template(template,output,marker,value):
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(template.read_text(encoding="utf-8").replace(marker,json.dumps(value,ensure_ascii=False,separators=(",",":"))),encoding="utf-8")

def recovery_lane_from_workbench(state_path: Path):
    try:
        state=json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("platform")!="51job" or not isinstance(state.get("lane"),dict):
        return None
    saved=state["lane"]
    matches=[item for item in lanes() if item["id"]==saved.get("id") and item["city"]==saved.get("city") and item["keyword"]==saved.get("keyword")]
    if len(matches)!=1:
        return None
    selected=matches[0]
    page=int(saved.get("page") or 1)
    if page<1 or page>selected["pageBudget"]:
        return None
    return {**selected,"startPage":page,"recoverySource":"workbench"}

def write_candidate_template(template: Path, output: Path, candidate_value, recovery_lane):
    output.parent.mkdir(parents=True,exist_ok=True)
    source=template.read_text(encoding="utf-8")
    source=source.replace("__CANDIDATE_JSON__",json.dumps(candidate_value,ensure_ascii=False,separators=(",",":")))
    source=source.replace("__RECOVERY_LANE_JSON__",json.dumps(recovery_lane,ensure_ascii=False,separators=(",",":")))
    output.write_text(source,encoding="utf-8")

def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
    lane=sub.add_parser("lane"); lane.add_argument("--platform",choices=["51job"],required=True); lane.add_argument("--city",required=True); lane.add_argument("--keyword",required=True); lane.add_argument("--page",type=int,default=1); lane.add_argument("--output",type=Path,required=True)
    candidate=sub.add_parser("candidate"); candidate.add_argument("--candidate-env",action="store_true",required=True); candidate.add_argument("--workbench-state",type=Path,default=DEFAULT_WORKBENCH_STATE); candidate.add_argument("--output",type=Path,required=True)
    cleanup=sub.add_parser("cleanup"); cleanup.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); jobflow.effective("51job", True); root=Path(__file__).resolve().parent/"playwright"
    if args.command=="lane":
        matches=[item for item in lanes() if item["city"]==args.city and item["keyword"]==args.keyword]
        if len(matches)!=1: parser.error("named_lane_not_found")
        selected=matches[0]
        if args.page<1 or args.page>selected["pageBudget"]: parser.error("lane_page_out_of_range")
        selected={**selected,"startPage":args.page}; write_template(root/"lane-template.js",args.output,"__LANE_JSON__",selected); print(json.dumps(selected,ensure_ascii=False,separators=(",",":")))
    elif args.command=="candidate":
        raw=os.environ.get("CODEX_ENTERPRISE_ACTIVE_CANDIDATE_JSON","")
        if not raw.strip(): parser.error("CODEX_ENTERPRISE_ACTIVE_CANDIDATE_JSON_missing")
        value=json.loads(raw)
        if not isinstance(value,list) or len(value)<5: parser.error("candidate_must_be_positional_card")
        recovery_lane=recovery_lane_from_workbench(args.workbench_state)
        write_candidate_template(root/"inspect-template.js",args.output,value,recovery_lane)
        print(json.dumps({"ok":True,"job_id":str(value[0]),"recovery_lane":recovery_lane.get("id") if recovery_lane else "","recovery_page":recovery_lane.get("startPage") if recovery_lane else 0,"output":str(args.output)},ensure_ascii=False,separators=(",",":")))
    else:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text((root/"cleanup-template.js").read_text(encoding="utf-8"),encoding="utf-8")
        print(json.dumps({"ok":True,"action":"cleanup","output":str(args.output)},ensure_ascii=False,separators=(",",":")))
if __name__=="__main__": main()
