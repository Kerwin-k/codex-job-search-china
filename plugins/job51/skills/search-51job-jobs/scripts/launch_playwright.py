"""Launch upstream MCP against an explicitly configured browser profile."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from config_core import ConfigError, platform_state

MCP_VERSION = "0.0.83"

def ensure_mcp(platform):
    """Install without package hooks; launch Node directly, avoiding cmd path parsing."""
    node=shutil.which("node")
    npm=shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:raise ConfigError("node_and_npm_required")
    base=platform_state(platform)/"dependencies"/"playwright-mcp"
    package=base/"node_modules"/"@playwright"/"mcp"
    manifest=package/"package.json"
    current=False
    if manifest.is_file():
        current=json.loads(manifest.read_text(encoding="utf-8")).get("version")==MCP_VERSION
    if not current or not (package/"cli.js").is_file():
        executable=[npm]
        if os.name=="nt":
            candidates=[Path(npm).parent/"node_modules"/"npm"/"bin"/"npm-cli.js",
                        Path(node).parent/"node_modules"/"npm"/"bin"/"npm-cli.js"]
            entry=next((p for p in candidates if p.is_file()),None)
            if not entry:raise ConfigError("native_npm_launcher_missing")
            executable=[node,str(entry)]
        base.mkdir(parents=True,exist_ok=True)
        subprocess.run([*executable,"install","--ignore-scripts","--no-audit","--no-fund",
                        "--prefix",str(base),"@playwright/mcp@"+MCP_VERSION],
                       stdout=sys.stderr,stderr=sys.stderr,check=True)
    if json.loads(manifest.read_text(encoding="utf-8")).get("version")!=MCP_VERSION:
        raise ConfigError("dependency_version_mismatch")
    return [node,str(package/"cli.js")]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--platform",choices=("51job","zhaopin"),required=True)
    args=parser.parse_args()
    try:
        path=platform_state(args.platform)/"browser-binding.json"
        if not path.exists():raise ConfigError("browser_binding_required: run_bind_browser")
        binding=json.loads(path.read_text(encoding="utf-8"))
        if not binding.get("profile") or binding.get("browser") not in ("msedge","chrome"):
            raise ConfigError("browser_binding_invalid")
        other="zhaopin" if args.platform=="51job" else "51job"
        other_path=platform_state(other)/"browser-binding.json"
        if other_path.exists():
            alternate=json.loads(other_path.read_text(encoding="utf-8"))
            if alternate==binding:raise ConfigError("platform_browser_profiles_must_differ")
        executable=ensure_mcp(args.platform)
        command=[*executable,"--extension","--browser",binding["browser"],"--profile-dir-name",binding["profile"]]
        return subprocess.call(command)
    except (ConfigError,OSError,ValueError,subprocess.CalledProcessError):
        print("jobflow_browser_connection_failed: verify_private_binding_and_node",file=sys.stderr)
        return 1

if __name__=="__main__":raise SystemExit(main())
