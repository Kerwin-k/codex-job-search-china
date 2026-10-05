import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import config_core as core
import jobflow_cli as cli

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.environment=patch.dict(os.environ,{"JOBFLOW_HOME":self.tmp.name})
        self.environment.start()
        self.cfg=core.template()
        self.cfg["search"].update(cities=["北京"],keywords=["产品设计"])
        self.cfg["search"]["salary"]["monthly_floor"]=10000
        self.cfg["candidate"]["resume_labels"]={p:"合成附件" for p in core.PLATFORMS}
        core.write_json(core.config_path(),self.cfg)
        core.write_json(core.data_root()/"candidate.local.json",{"confirmed":True,"facts":[{"id":"synthetic-fact","text":"合成：掌握原型工具。","source":"合成夹具"}]})

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def test_unknown_top_level_rejected(self):
        cfg=copy.deepcopy(self.cfg);cfg["unknown"]=1
        with self.assertRaises(core.ConfigError):core.validate_config(cfg)

    def test_unknown_override_rejected(self):
        cfg=copy.deepcopy(self.cfg);cfg["platform_overrides"]={"boss":{"search":{"salray":1}}}
        with self.assertRaises(core.ConfigError):core.validate_config(cfg)

    def test_override_is_merged_and_validated(self):
        cfg=copy.deepcopy(self.cfg);cfg["platform_overrides"]={"boss":{"search":{"salary":{"comparison":"upper_bound"}}}}
        core.write_json(core.config_path(),cfg)
        effective=core.effective("boss",True)
        self.assertEqual(effective["search"]["salary"],{"monthly_floor":10000,"comparison":"upper_bound","unknown":"review"})
        self.assertEqual(core.effective("51job")["search"]["salary"]["comparison"],"lower_bound")

    def test_boolean_is_not_integer_quota(self):
        cfg=copy.deepcopy(self.cfg);cfg["execution"]["target_count"]=True
        with self.assertRaises(core.ConfigError):core.validate_config(cfg)

    def test_custom_city_mapping(self):
        cfg=copy.deepcopy(self.cfg);cfg["search"]["cities"]=["合成城市"]
        with self.assertRaises(core.ConfigError):core.validate_config(cfg)
        cfg["search"]["city_codes"]={"合成城市":"123456"}
        core.write_json(core.config_path(),cfg)
        self.assertEqual(core.configured_lanes("51job")[0]["cityCode"],"123456")

    def test_arbitrary_profession_generates_lanes(self):
        for p in ("51job","zhaopin"):
            lanes=core.configured_lanes(p)
            self.assertEqual(len(lanes),1)
            self.assertEqual(lanes[0]["keyword"],"产品设计")
            self.assertEqual(lanes[0]["city"],"北京")

    def test_salary_period_and_bound(self):
        self.assertEqual(core.salary_band("12–24万元/年"),(10000,20000))
        self.assertEqual(core.salary_band("0.8-1.2万/月"),(8000,12000))
        self.assertFalse(core.salary_pass("8-12K","51job"))
        cfg=copy.deepcopy(self.cfg);cfg["search"]["salary"]["comparison"]="upper_bound"
        core.write_json(core.config_path(),cfg)
        self.assertTrue(core.salary_pass("8-12K","51job"))

    def test_daily_foreign_and_unknown_pay_not_guessed(self):
        for text in ("300元/天","100元/小时","USD 10000","面议","8000-12000"):
            self.assertIsNone(core.salary_band(text),text)

    def test_pua_salary_digits(self):
        self.assertEqual(core.salary_band(chr(0xE031)+chr(0xE030)+"-"+chr(0xE032)+chr(0xE030)+"K"),(10000,20000))

    def test_user_rules_not_author_rules(self):
        job={"title":"高级设计师","company":"合成公司","city":"北京","salary":"15-20K"}
        self.assertEqual(core.card_reason(job,"boss"),"")
        cfg=copy.deepcopy(self.cfg);cfg["search"]["excluded_keywords"]=["高级"]
        core.write_json(core.config_path(),cfg)
        self.assertEqual(core.card_reason(job,"boss"),"excluded_keyword")

    def test_unconfirmed_profile_blocks_apply(self):
        core.write_json(core.data_root()/"candidate.local.json",{"confirmed":False,"facts":[]})
        with self.assertRaises(core.ConfigError):core.authorize("boss",1)

    def test_attachment_name_required(self):
        cfg=copy.deepcopy(self.cfg);cfg["candidate"]["resume_labels"]={}
        core.write_json(core.config_path(),cfg)
        with self.assertRaises(core.ConfigError):core.authorize("boss",1)

    def test_config_change_invalidates_authorization(self):
        core.authorize("boss",1)
        cfg=copy.deepcopy(self.cfg);cfg["search"]["keywords"]=["视觉设计"]
        core.write_json(core.config_path(),cfg)
        with self.assertRaises(core.ConfigError):core.require_apply("boss")

    def test_profile_change_invalidates_authorization(self):
        core.authorize("boss",1)
        core.write_json(core.data_root()/"candidate.local.json",{"confirmed":True,"facts":[{"id":"changed","text":"合成修改","source":"合成夹具"}]})
        with self.assertRaises(core.ConfigError):core.require_apply("boss")

    def test_expired_authorization_blocks_apply(self):
        core.authorize("boss",1,duration=-1)
        with self.assertRaises(core.ConfigError):core.require_apply("boss")

    def test_platform_states_differ(self):
        self.assertEqual(len({core.platform_state(p) for p in core.PLATFORMS}),3)

    def test_private_storage_cannot_be_inside_source(self):
        with patch.dict(os.environ,{"JOBFLOW_HOME":str(ROOT/"private")}):
            with self.assertRaises(core.ConfigError):core.data_root()

    def test_attempt_survives_reauthorization(self):
        core.authorize("boss",2)
        core.begin_attempt("boss","synthetic-job")
        core.authorize("boss",2)
        with self.assertRaises(core.ConfigError):core.begin_attempt("boss","synthetic-job")

    def test_attempt_dispatch_is_single_use(self):
        core.authorize("boss",2)
        core.begin_attempt("boss","synthetic-job")
        self.assertEqual(core.dispatch_attempt("boss","synthetic-job")["status"],"dispatched")
        with self.assertRaises(core.ConfigError):core.dispatch_attempt("boss","synthetic-job")

    def test_attempt_lock_prevents_concurrent_writer(self):
        core.authorize("boss",2)
        with core.attempt_store("boss"):
            with self.assertRaises(core.ConfigError):core.begin_attempt("boss","synthetic-job")

    def test_uncertain_attempt_counts_against_target(self):
        core.authorize("boss",1)
        core.begin_attempt("boss","synthetic-job-1")
        with self.assertRaises(core.ConfigError):core.begin_attempt("boss","synthetic-job-2")

    def test_explicit_not_sent_resolution_allows_retry(self):
        core.authorize("boss",1)
        core.begin_attempt("boss","synthetic-job")
        core.resolve_attempt("boss","synthetic-job","not_sent","synthetic direct no-send evidence")
        self.assertEqual(core.begin_attempt("boss","synthetic-job")["status"],"prepared")

    def test_confirmed_send_cannot_be_reclassified(self):
        core.authorize("boss",1)
        core.begin_attempt("boss","synthetic-job")
        core.resolve_attempt("boss","synthetic-job","sent","synthetic direct sent evidence")
        with self.assertRaises(core.ConfigError):core.resolve_attempt("boss","synthetic-job","not_sent","inconsistent synthetic evidence")

    def test_render_submit_is_durable_and_private(self):
        core.authorize("51job",1)
        result=cli.render_submit("51job","synthetic-job")
        path=Path(result["submit_script"])
        self.assertTrue(path.is_relative_to(core.data_root()))
        self.assertIn("__jobflowAttempt",path.read_text(encoding="utf-8"))
        with self.assertRaises(core.ConfigError):cli.render_submit("51job","synthetic-job")

    def test_prepare_materializes_valid_js(self):
        for p in ("51job","zhaopin"):
            result=cli.materialize(p)
            self.assertFalse(result["apply_authorized"])
            for file in Path(result["runtime_directory"]).glob("*.js"):
                checked=subprocess.run(["node","--check",str(file)],capture_output=True)
                self.assertEqual(checked.returncode,0,file.name)

    def test_boss_token_is_random_and_never_in_result(self):
        result=cli.prepare_boss(17897)
        session=json.loads((core.platform_state("boss")/"bridge-session.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(session["token"]),40)
        self.assertNotIn(session["token"],json.dumps(result))
        self.assertIn(session["token"],(Path(result["extension_directory"])/"content.js").read_text(encoding="utf-8"))

    def test_standalone_skill_help(self):
        for p in ROOT.glob("plugins/*/skills/*/scripts/jobflow.py"):
            run=subprocess.run([sys.executable,str(p),"--help"],capture_output=True)
            self.assertEqual(run.returncode,0,p.parts[-4])

    def test_cross_platform_dedupe_is_read_only_and_minimal(self):
        path=core.platform_state("boss")/"receipts.jsonl"
        path.parent.mkdir(parents=True,exist_ok=True)
        original=json.dumps({"job":{"company":"合成公司","job_title":"产品设计"},"private_message":"合成私有内容"},ensure_ascii=False)+"\n"
        path.write_text(original,encoding="utf-8")
        result=cli.duplicate_summary("合成公司","产品设计")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["matched_platforms"],["boss"])
        self.assertNotIn("合成私有内容",json.dumps(result,ensure_ascii=False))
        self.assertEqual(path.read_text(encoding="utf-8"),original)

    def test_technical_failure_is_not_a_duplicate(self):
        path=core.platform_state("51job")/"technical-failures-51job.tsv"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("company\ttitle\n合成公司\t产品设计\n",encoding="utf-8")
        self.assertFalse(cli.duplicate_summary("合成公司","产品设计")["duplicate"])

    def test_customized_engines_have_no_fixed_career_lane(self):
        for name,engine in (("job51","job51_workbench.py"),("zhaopin","zhaopin_workbench.py"),("boss","boss_ledger.py")):
            folder=next((ROOT/"plugins"/name/"skills").iterdir())/"scripts"
            command="import json; import config_core as c; print(json.dumps(c.effective('"+('51job' if name=='job51' else name)+"')['search']['keywords']))"
            run=subprocess.run([sys.executable,"-c",command],cwd=folder,capture_output=True,encoding="utf-8",errors="replace")
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout),["产品设计"])
            run=subprocess.run([sys.executable,str(folder/engine),"--help"],capture_output=True)
            self.assertEqual(run.returncode,0,engine)

    def test_three_engines_reject_a_second_live_lease_owner(self):
        for plugin,engine in (("job51","job51_workbench.py"),("zhaopin","zhaopin_workbench.py"),("boss","boss_ledger.py")):
            folder=next((ROOT/"plugins"/plugin/"skills").iterdir())/"scripts"
            start=["batch-start","--target","2","--mode","search-city","--keyword","产品设计"] if plugin=="boss" else ["start","--target","2"]
            first=subprocess.run([sys.executable,str(folder/engine),*start],capture_output=True)
            self.assertEqual(first.returncode,0,plugin+": start")
            lease=subprocess.run([sys.executable,str(folder/engine),"lease-acquire","--owner","synthetic-owner-1"],capture_output=True)
            self.assertEqual(lease.returncode,0,plugin+": first lease")
            conflict=subprocess.run([sys.executable,str(folder/engine),"lease-acquire","--owner","synthetic-owner-2"],capture_output=True)
            result=json.loads(conflict.stdout.decode("utf-8"))
            self.assertFalse(result.get("ok"),plugin+": concurrent owner refused")

    def test_boss_bridge_authentication_without_browser(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        cli.prepare_boss(port)
        script=ROOT/"plugins/boss/skills/search-boss-jobs/scripts/boss_bridge.py"
        flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
        process=subprocess.Popen([sys.executable,str(script),"serve"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
        url=f"http://127.0.0.1:{port}"
        try:
            ready=False
            for _ in range(50):
                try:
                    with urlopen(url+"/health",timeout=.2) as response:ready=response.status==200
                    if ready:break
                except (URLError,OSError):time.sleep(.05)
            self.assertTrue(ready,"isolated bridge started")
            with self.assertRaises(HTTPError) as error:urlopen(url+"/api/status",timeout=1)
            self.assertEqual(error.exception.code,403)
            session=json.loads((core.platform_state("boss")/"bridge-session.json").read_text(encoding="utf-8"))
            request=Request(url+"/api/status",headers={"X-Boss-Bridge-Token":session["token"]})
            with urlopen(request,timeout=1) as response:result=json.load(response)
            self.assertTrue(result.get("ok"))
            self.assertNotIn(session["token"],json.dumps(result))
        finally:
            process.terminate()
            process.wait(timeout=5)

if __name__=="__main__":unittest.main()
