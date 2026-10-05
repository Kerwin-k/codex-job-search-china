"""Fail-closed file allowlist, content checks and reproducible release ZIP."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parent.parent
MANIFEST=ROOT/"release-files.json"
IGNORED={".git","__pycache__",".venv","node_modules","dist"}
ALLOWED_SUFFIXES={".py",".js",".cjs",".ps1",".md",".json",".yaml",".yml",".svg"}
ALLOWED_NAMES={"LICENSE",".gitignore",".gitattributes"}
EMAIL=re.compile(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ABS_PATH=re.compile(r"(?:[A-Za-z]:[\\/](?:Users|WorkSpace|Documents|Program Files)|/Users/[A-Za-z0-9_. -]{2,}/|/home/[A-Za-z0-9_.-]{2,}/)",re.I)
SECRET=re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
PRIVATE_FILES=re.compile(r"(?:^|/)(?:state|runtime|logs|backups|private|workflow-backups)(?:/|$)|\.local\.|\.(?:pdf|docx|jsonl|tsv|log|lock|tmp|bak)$",re.I)

def scan_text(text: str, identifiers: list[str]|None=None) -> list[str]:
    categories=[]
    emails=[x for x in EMAIL.findall(text) if not x.endswith("@users.noreply.github.com")]
    if emails:categories.append("email")
    if PHONE.search(text):categories.append("phone")
    if ABS_PATH.search(text):categories.append("machine_path")
    if SECRET.search(text):categories.append("credential")
    if identifiers and any(x and x.casefold() in text.casefold() for x in identifiers):categories.append("private_identifier")
    return categories

def public_files(root: Path=ROOT) -> list[Path]:
    return sorted((p for p in root.rglob("*") if p.is_file() and not any(x in IGNORED for x in p.relative_to(root).parts) and p.name!="release-files.json"),key=lambda p:p.relative_to(root).as_posix())

def check_files(files: list[Path], identifiers: list[str]) -> list[dict]:
    issues=[]
    for file in files:
        relative=file.relative_to(ROOT).as_posix()
        if file.is_symlink() or file.suffix not in ALLOWED_SUFFIXES and file.name not in ALLOWED_NAMES:
            issues.append({"file":relative,"category":"file_type"});continue
        # extension/manifest and execution wrappers are source. A directory
        # literally called runtime/state always belongs outside the package.
        if PRIVATE_FILES.search(relative):issues.append({"file":relative,"category":"private_file"});continue
        try:text=file.read_text(encoding="utf-8")
        except (UnicodeError,OSError):issues.append({"file":relative,"category":"non_text_file"});continue
        for kind in scan_text(text,identifiers):issues.append({"file":relative,"category":kind})
        if file.suffix==".py":
            try:ast.parse(text)
            except SyntaxError:issues.append({"file":relative,"category":"python_syntax"})
        if file.suffix==".json":
            try:json.loads(text)
            except ValueError:issues.append({"file":relative,"category":"json_syntax"})
        if file.suffix==".md":
            for link in re.findall(r"\]\(([^)]+)\)",text):
                if link.startswith(("https://","http://","#","mailto:")):continue
                target=(file.parent/link.split("#",1)[0]).resolve()
                if not target.is_relative_to(ROOT) or not target.exists() and target!=MANIFEST:issues.append({"file":relative,"category":"broken_relative_link"})
    return issues

def git_history_check(identifiers: list[str]) -> list[dict]:
    if not (ROOT/".git").exists():return []
    run=subprocess.run(["git","rev-list","--all"],cwd=ROOT,capture_output=True,text=True)
    if run.returncode:return [{"category":"git_history_unreadable"}]
    issues=[]
    for commit in run.stdout.splitlines():
        meta=subprocess.run(["git","show","-s","--format=%an%n%ae%n%cn%n%ce%n%B",commit],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
        if meta.returncode or scan_text(meta.stdout,identifiers):issues.append({"category":"git_metadata"})
        tree=subprocess.run(["git","ls-tree","-r","--name-only",commit],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
        for name in tree.stdout.splitlines():
            if PRIVATE_FILES.search(name):issues.append({"category":"git_private_file"});continue
            blob=subprocess.run(["git","show",commit+":"+name],cwd=ROOT,capture_output=True)
            try:text=blob.stdout.decode("utf-8")
            except UnicodeError:issues.append({"category":"git_binary"});continue
            if scan_text(text,identifiers):issues.append({"category":"git_content"})
    return issues

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--refresh",action="store_true",help="Only after reviewing new public source files")
    parser.add_argument("--private-identifiers",type=Path,help="Private JSON list outside this repository; values are never printed")
    parser.add_argument("--history",action="store_true")
    parser.add_argument("--zip",type=Path,help="Create a checked allowlist-only ZIP outside the source tree")
    args=parser.parse_args()
    identifiers=[]
    if args.private_identifiers:
        if args.private_identifiers.resolve().is_relative_to(ROOT):
            print(json.dumps({"ok":False,"error":"private_scan_dictionary_must_be_outside_repo"}));return 1
        identifiers=json.loads(args.private_identifiers.read_text(encoding="utf-8"))
        if not isinstance(identifiers,list) or any(not isinstance(x,str) for x in identifiers):return 1
    files=public_files()
    issues=check_files(files,identifiers)
    payload={"schema_version":1,"files":[p.relative_to(ROOT).as_posix() for p in files]}
    if issues:
        print(json.dumps({"ok":False,"issue_count":len(issues),"categories":sorted({x["category"] for x in issues})}));return 1
    if args.refresh:
        MANIFEST.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if not MANIFEST.exists() or json.loads(MANIFEST.read_text(encoding="utf-8"))!=payload:
        print(json.dumps({"ok":False,"error":"release_allowlist_changed: review_before_refresh"}));return 1
    if scan_text(MANIFEST.read_text(encoding="utf-8"),identifiers):return 1
    if args.history:
        history=git_history_check(identifiers)
        if history:
            print(json.dumps({"ok":False,"issue_count":len(history),"categories":sorted({x["category"] for x in history})}));return 1
    zip_sha=None
    if args.zip:
        if args.zip.resolve().is_relative_to(ROOT):
            print(json.dumps({"ok":False,"error":"release_archive_must_be_outside_source"}));return 1
        args.zip.parent.mkdir(parents=True,exist_ok=True)
        selected=files+[MANIFEST]
        with zipfile.ZipFile(args.zip,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            for file in selected:
                info=zipfile.ZipInfo("codex-job-search-china/"+file.relative_to(ROOT).as_posix(),date_time=(2026,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=0o644<<16
                archive.writestr(info,file.read_bytes())
        with zipfile.ZipFile(args.zip) as archive:
            if len(archive.namelist())!=len(selected):return 1
            for name in archive.namelist():
                if scan_text(archive.read(name).decode("utf-8"),identifiers):return 1
        zip_sha=hashlib.sha256(args.zip.read_bytes()).hexdigest()
    print(json.dumps({"ok":True,"public_files":len(files)+1,"content_checks":"passed","history_checked":args.history,"archive_sha256":zip_sha}))
    return 0

if __name__=="__main__":raise SystemExit(main())
