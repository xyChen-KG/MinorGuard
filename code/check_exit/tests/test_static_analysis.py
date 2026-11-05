import contextlib
import io
import json
import zipfile
from unittest.mock import Mock, patch

from check_exit.__main__ import main
from check_exit.static_analysis import ExitStaticAgent, StaticAnalysis, manifest_entries, matches_replay_filter
from check_exit.workflow import Workflow
from check_limits.tools import EvidenceTools
from collect_images.common import CollectorError
from check_exit.tests.test_exit_guard import (
    FakeJudge, TestBase, decision, negative_path, pin, positive_path, prepared, restart_path,
)


PACKAGE = "com.example.test"
URI = "testapp://guardian/restricted"
ENTRY = PACKAGE + ".Entry"
MANIFEST = f"""<manifest package="{PACKAGE}" xmlns:android="http://schemas.android.com/apk/res/android">
<application>
<activity android:name=".Entry" android:exported="true">
  <intent-filter>
    <action android:name="android.intent.action.VIEW"/>
    <category android:name="android.intent.category.DEFAULT"/>
    <category android:name="android.intent.category.BROWSABLE"/>
    <data android:scheme="testapp" android:host="guardian" android:path="/restricted"/>
  </intent-filter>
</activity>
<activity android:name=".Protected" android:exported="false" android:permission="test.permission.PRIVATE"/>
<activity-alias android:name=".ProtectedAlias" android:targetActivity=".Protected" android:exported="true"/>
</application></manifest>"""


def tool_call(name, arguments, call_id):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class StaticAnalysisTests(TestBase):
    def test_local_apk_cli_checks_jadx_identity_without_constructing_adb(self):
        root = self.recorder.root
        apk = root / "sample.apk"
        manifest = MANIFEST.replace(f'package="{PACKAGE}"', f'package="{PACKAGE}" android:versionName="1" android:versionCode="5"')
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", manifest)
        args = ["--static-only", "--apk", str(apk), "--output-dir", str(root / "runs")]
        with patch("check_exit.__main__.load_env"), patch("check_exit.__main__.ADB") as adb, \
             patch("check_exit.static_analysis.ensure_server") as server, \
             patch("check_exit.static_analysis.SSHConnection") as ssh, \
             patch("check_exit.static_analysis.MCPClient") as mcp, \
             patch("check_exit.static_analysis.ChatClient"), \
             patch("check_exit.static_analysis.ExitStaticAgent") as agent, contextlib.redirect_stdout(io.StringIO()):
            connection = ssh.return_value.__enter__.return_value
            connection.identity.side_effect = lambda: {"sha256": server.call_args.args[1]["sha256"]}
            mcp.return_value.call.return_value = {"content": manifest}
            agent.return_value.run.return_value = {"status": "completed", "candidates": []}
            agent.return_value.model_calls = agent.return_value.tool_calls = 0
            self.assertEqual(main(args), 0)
            path, metadata, _ = server.call_args.args
            self.assertEqual(path, apk.resolve())
            self.assertEqual(metadata["package"], PACKAGE)
            server.reset_mock()
            self.assertEqual(main(args + ["--package", "com.example.other"]), 1)
            adb.assert_not_called()
            server.assert_not_called()
        report = next(p for p in (root / "runs").glob("*/report.md") if "扫描结束" in p.read_text())
        self.assertIn(PACKAGE + " · 1 · 只分析输入 APK", report.read_text())

    def test_replay_filter_rejects_wrong_paths_parameters_and_inherited_permissions(self):
        entry, alias = manifest_entries({"content": MANIFEST})
        self.assertEqual(entry["name"], ENTRY)
        self.assertEqual(alias["target"], PACKAGE + ".Protected")
        self.assertIn("test.permission.PRIVATE", alias["permissions"])
        self.assertTrue(matches_replay_filter(entry, URI))
        for uri in (URI + "?action=reset", URI + "#anchor", URI + ";id", "testapp://guardian/elsewhere"):
            self.assertFalse(matches_replay_filter(entry, uri))
        with_permission = manifest_entries(MANIFEST.replace("<application>", '<application android:permission="test.permission.APP">'))[0]
        self.assertFalse(matches_replay_filter(with_permission, URI))

    def test_shared_tool_loop_requires_read_source_and_exact_uri(self):
        (self.recorder.root / "code_evidence").mkdir()
        quote = f'void open() {{ route("{URI}"); }}'
        mcp = Mock()
        mcp.call.return_value = {"code": "class Entry { " + quote + " }"}
        schemas = {"get_class_source": {"inputSchema": {"type": "object", "properties": {"class_name": {"type": "string"}},
                                                        "required": ["class_name"]}}}
        tools = EvidenceTools(mcp, schemas, self.recorder)
        candidate = {"entry_id": "A001", "kind": "restricted_page", "uri": URI, "target": "受限页面",
                     "reason": "测试用调用链", "code_evidence": [{"id": "E001", "quote": quote}]}
        final = {"summary": "测试候选", "coverage": "扫描一个入口", "limitations": [], "candidates": [candidate]}
        client = Mock()
        client.chat.side_effect = [
            (tool_call("submit_result", final, "before-evidence"), {}, "offline"),
            (tool_call("get_class_source", {"class_name": ENTRY}, "read-source"), {}, "offline"),
            (tool_call("submit_result", {**final, "candidates": [{**candidate, "uri": URI + "-guessed"}]}, "wrong-uri"), {}, "offline"),
            (tool_call("submit_result", final, "final"), {}, "offline"),
        ]
        config = {"max_seconds": 60, "max_model_calls": 7, "max_tool_calls": 3, "max_context_chars": 20000, "jadx": {"timeout": 10}}
        agent = ExitStaticAgent(config, client, tools, self.recorder, {}, manifest_entries(MANIFEST))
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent.run()
        self.assertEqual(agent.model_calls, 4)
        saved = result["candidates"][0]
        self.assertEqual(saved["uri"], URI)
        self.assertEqual(saved["validation"]["status"], "pending")
        self.assertTrue((self.recorder.root / saved["code_evidence"][0]["file"]).is_file())

    def analysis(self, replay_uris=(URI,)):
        analysis = StaticAnalysis({}, self.backend, PACKAGE, "1", self.recorder.root, replay_uris=replay_uris)
        analysis.result.update(status="completed", apk={"sha256": "same-hash"}, candidates=[{
            "id": "C01", "entry": ENTRY, "kind": "mode_exit", "target": "模式状态", "uri": URI,
            "reason": "候选说明", "code_evidence": [], "replay_eligible": True, "validation": {"status": "pending", "reason": "待验证"}}])
        self.backend.apk_paths = Mock(return_value=["/data/app/base.apk"])
        self.backend.apk_hash = Mock(return_value="same-hash")
        return analysis

    def test_resolution_mismatch_keeps_candidate_pending_and_preserves_control(self):
        analysis = self.analysis()
        with patch.object(self.backend, "_run", return_value=b"other.app/.Activity") as adb:
            self.assertTrue(analysis.probe(self.collector([])))
        self.assertEqual(adb.call_count, 1)
        self.assertIn("resolve-activity", adb.call_args.args[0])
        self.assertEqual(analysis.result["candidates"][0]["validation"]["status"], "pending")

    def test_candidate_without_operator_selected_uri_never_launches(self):
        analysis = self.analysis(replay_uris=())
        with patch.object(self.backend, "_run") as adb:
            self.assertTrue(analysis.probe(self.collector([])))
        adb.assert_not_called()

    def test_probe_cannot_start_from_mode_off(self):
        collector = self.collector([])
        self.device.set_phase("prepare")
        collector.capture(settle=False)
        collector.images[-1]["navigation_observation"] = {"view": "mode_status", "mode_state": "off"}
        trigger = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            result = collector.run_phase("probe", trigger=trigger)
        trigger.assert_not_called()
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["landing_image"])

    def test_missing_candidate_keeps_requested_replay_incomplete(self):
        analysis = self.analysis(replay_uris=(URI + "-unmatched",))
        self.device.known_pin = True
        collector = self.collector(prepared() + negative_path() + restart_path() + positive_path(), [pin(), {"action": "done"}])
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, collector, FakeJudge(), self.recorder, static_analysis=analysis).run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["judgement"]["verdict"], "not_observed")
        self.assertIn("本轮未重放", (self.recorder.root / "report.md").read_text())

    def test_probe_judge_cannot_confirm_mode_exit_when_status_is_on(self):
        analysis = self.analysis()
        collector = self.collector([])
        self.device.set_phase("probe")
        for _ in range(3):
            collector.capture(settle=False)
        observation = {"status": "collected", "before_image": "I001", "landing_image": "I002",
                       "after_image": "I003", "mode_observation": "on"}
        model_response = {"verdict": "bypass", "reason": "测试错误判定", "image_ids": ["I001", "I002", "I003"]}
        analysis.config = {"api_timeout": 10}
        with patch("check_exit.static_analysis.ChatClient") as chat:
            chat.return_value.chat.return_value = ({"content": json.dumps(model_response)}, {}, "offline")
            with self.assertRaisesRegex(CollectorError, "截图证据"):
                analysis._judge_probe(analysis.result["candidates"][0], observation, collector.images, self.recorder.root)

    def test_real_workflow_probes_after_restart_and_skips_control_if_mode_closes(self):
        analysis = self.analysis()
        collector = self.collector(prepared() + negative_path() + restart_path() + [decision(mode="off")] * 3 + positive_path(),
                                   [pin(), {"action": "done"}])
        self.device.known_pin = True
        original_run = self.backend._run

        def run(args):
            if args[:4] == ["shell", "cmd", "package", "resolve-activity"]:
                return (PACKAGE + "/.Entry\n").encode()
            if args[:3] == ["shell", "am", "start"]:
                self.backend.actions.append(tuple(args))
                return b"Status: ok\n"
            return original_run(args)

        with patch.object(self.backend, "_run", side_effect=run), patch.object(analysis, "_judge_probe", return_value={"status": "bypass", "reason": "测试桩"}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, collector, FakeJudge("unknown"), self.recorder, static_analysis=analysis).run()
        self.assertEqual([s["status"] for s in result["collection"]["scenarios"]], ["collected", "collected", "skipped"])
        events = result["collection"]["events"]
        actions = [e["action"] for e in events]
        self.assertLess(actions.index("restart_app"), actions.index("probe_link"))
        self.assertNotIn("enter_pin", actions)
        probe = next(e for e in events if e["action"] == "probe_link")
        finding = result["static_analysis"]["candidates"][0]["validation"]
        self.assertEqual(probe["params"]["candidate"], "C01")
        self.assertEqual(finding["landing_image"], probe["after_image"])
        self.assertEqual(finding["status"], "bypass")
