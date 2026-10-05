"""Synchronize maintained shared modules into self-contained plugin skills."""
from pathlib import Path
import argparse
import sys

ROOT=Path(__file__).resolve().parent.parent

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    stale=[]
    for scripts in ROOT.glob("plugins/*/skills/*/scripts"):
        for name in ("config_core.py","jobflow_cli.py","launch_playwright.py"):
            source=ROOT/"tools"/name
            target=scripts/name
            if not target.exists() or target.read_bytes()!=source.read_bytes():
                stale.append(str(target.relative_to(ROOT)))
                if not args.check:target.write_bytes(source.read_bytes())
        policy=scripts/"playwright"/"policy.js"
        if policy.exists() and policy.read_bytes()!=(ROOT/"tools"/"policy.js").read_bytes():
            stale.append(str(policy.relative_to(ROOT)))
            if not args.check:policy.write_bytes((ROOT/"tools"/"policy.js").read_bytes())
    if args.check and stale:
        print("shared_modules_stale: "+", ".join(stale))
        return 1
    print("shared_modules_current" if args.check else "shared_modules_synchronized")
    return 0

if __name__=="__main__":sys.exit(main())
