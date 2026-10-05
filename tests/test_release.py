from pathlib import Path
import sys
import unittest
import json
import os
import tempfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import release_check as release
import launch_playwright as launcher

class ReleaseTests(unittest.TestCase):
    def test_email_and_phone_are_detected_without_echo(self):
        private="synthetic"+"@"+"example.invalid"
        self.assertEqual(release.scan_text(private),["email"])
        self.assertEqual(release.scan_text("138"+"1234"+"5678"),["phone"])

    def test_secret_is_detected(self):
        self.assertEqual(release.scan_text("sk-"+"X"*30),["credential"])

    def test_private_dictionary_is_case_insensitive(self):
        self.assertEqual(release.scan_text("SyntheticIdentifier",["syntheticidentifier"]),["private_identifier"])

    def test_runtime_and_resume_files_are_blocked(self):
        for file in ("state/batch.json","scripts/runtime/token.json","candidate.local.json","backup.pdf","receipts.jsonl"):
            self.assertIsNotNone(release.PRIVATE_FILES.search(file))

    def test_machine_paths_detected(self):
        self.assertEqual(release.scan_text("D:"+"/WorkSpace/synthetic"),["machine_path"])

    def test_launcher_installs_and_runs_without_shell_in_special_path(self):
        with tempfile.TemporaryDirectory(prefix="jobflow & ") as directory:
            root=Path(directory)
            npm_entry=root/"node_modules/npm/bin/npm-cli.js"
            npm_entry.parent.mkdir(parents=True)
            npm_entry.write_text("",encoding="utf-8")
            def which(name):
                return str(root/("node.exe" if name=="node" else "npm.cmd"))
            def install(command,**kwargs):
                self.assertIn("--ignore-scripts",command)
                self.assertNotIn("shell",kwargs)
                if os.name=="nt":self.assertEqual(command[:2],[str(root/"node.exe"),str(npm_entry)])
                package=Path(command[command.index("--prefix")+1])/"node_modules/@playwright/mcp"
                package.mkdir(parents=True)
                (package/"package.json").write_text(json.dumps({"version":launcher.MCP_VERSION}),encoding="utf-8")
                (package/"cli.js").write_text("",encoding="utf-8")
            with patch.dict(os.environ,{"JOBFLOW_HOME":str(root/"private & data")}),patch.object(launcher.shutil,"which",side_effect=which),patch.object(launcher.subprocess,"run",side_effect=install) as run:
                command=launcher.ensure_mcp("51job")
                self.assertEqual(command[0],str(root/"node.exe"))
                self.assertTrue(command[1].endswith("cli.js"))
                self.assertEqual(launcher.ensure_mcp("51job"),command)
                self.assertEqual(run.call_count,1)

if __name__=="__main__":unittest.main()
