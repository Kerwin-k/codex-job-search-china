#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
from urllib.parse import quote

import config_core as jobflow

CITIES = [(c, jobflow.CITY_CODES["zhaopin"].get(c, "")) for c in jobflow.effective("zhaopin")["search"]["cities"]]
CITY_TIERS = [CITIES]
POOLS = [(p, True) for p in jobflow.effective("zhaopin")["search"]["expectation_pools"]]
SUPPLEMENTS = jobflow.effective("zhaopin")["search"]["keywords"]

def lanes(platform="zhaopin"):
    if platform != "zhaopin": raise ValueError("platform_not_supported")
    return jobflow.configured_lanes(platform)

def write_template(template,output,marker,value):
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(template.read_text(encoding="utf-8").replace(marker,json.dumps(value,ensure_ascii=False,separators=(",",":"))),encoding="utf-8")

def main():
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
    lane=sub.add_parser("lane"); lane.add_argument("--platform",choices=["zhaopin"],required=True); selector=lane.add_mutually_exclusive_group(required=True); selector.add_argument("--pool",choices=[x[0] for x in POOLS]); selector.add_argument("--keyword",choices=SUPPLEMENTS); lane.add_argument("--city",required=True); lane.add_argument("--page",type=int,default=1); lane.add_argument("--output",type=Path,required=True)
    candidate=sub.add_parser("candidate"); candidate.add_argument("--candidate-env",action="store_true",required=True); candidate.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); jobflow.effective("zhaopin", True); root=Path(__file__).resolve().parent/"playwright"
    if args.command=="lane":
        primary=args.pool or args.keyword; source="expectation_pool" if args.pool else "keyword"
        matches=[item for item in lanes() if item["city"]==args.city and item["source"]==source and (item.get("poolLabel") or item["keyword"])==primary]
        if len(matches)!=1: parser.error("named_lane_not_found")
        selected=matches[0]
        if args.page<1 or args.page>selected["pageBudget"]: parser.error("lane_page_out_of_range")
        selected={**selected,"startPage":args.page}; write_template(root/"lane-template.js",args.output,"__LANE_JSON__",selected); print(json.dumps(selected,ensure_ascii=False,separators=(",",":")))
    else:
        raw=os.environ.get("CODEX_ENTERPRISE_ACTIVE_CANDIDATE_JSON","")
        if not raw.strip(): parser.error("CODEX_ENTERPRISE_ACTIVE_CANDIDATE_JSON_missing")
        value=json.loads(raw)
        if not isinstance(value,list) or len(value)<5: parser.error("candidate_must_be_positional_card")
        write_template(root/"inspect-template.js",args.output,"__CANDIDATE_JSON__",value); print(json.dumps({"ok":True,"job_id":str(value[0]),"output":str(args.output)},ensure_ascii=False,separators=(",",":")))
if __name__=="__main__": main()
