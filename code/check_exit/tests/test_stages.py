"""Checkpoint transitions, scoped requests, and real-operation evidence guards."""

import contextlib
import io
import os
from itertools import groupby
from unittest.mock import patch

from collect_images.common import CollectorError, NeedUser
from check_exit.judge import validate_result
from check_exit.navigator import JevNavigator
from check_exit.stages import STAGES, prompt_for
from check_exit.tests.test_exit_guard import BOX, ENV, TestBase, decision, final_result, frame, negative_path, pin, positive_path, prepared, restart_path, FakeJudge
from check_exit.workflow import Workflow


class StageTests(TestBase):
    def test_active_mode_pin_gate_is_not_mistaken_for_unfinished_setup(self):
        for allow_setup in (True, False):
            with self.subTest(allow_setup=allow_setup):
                self.cfg["password"]["allow_setup"] = allow_setup
                c = self.collector([decision("back", view="mode_password", mode="on")])
                with contextlib.redirect_stdout(io.StringIO()):
                    result = c.run_phase("prepare")
                self.assertEqual(result["status"], "need_user")
                self.assertEqual(c.password.calls, [])
                self.assertEqual(self.backend.actions, [])

    def test_ambiguous_pin_page_cannot_loop_return_forever(self):
        c = self.collector([decision("back", view="mode_password", mode="unknown")] * 3)
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(self.backend.actions, [("back",), ("back",)])
        self.assertEqual(c.password.calls, [])

    def test_mixed_mode_setup_form_can_pass_absent_agreement_without_skipping_pin_guard(self):
        # One screen can be both the opening form and a PIN page. Different
        # view answers must not strand the agreement checkpoint.
        nav = [decision(view="mode_intro", mode="off"), decision(view="mode_intro", mode="off"),
               decision("advance", view="mode_password", mode="off", agreement="absent"),
               decision("advance", view="mode_password", mode="off")]
        c = self.collector(nav, [NeedUser("测试表单还需要用户配置资料")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(c.stage, "enable_mode")
        self.assertEqual(len(c.password.calls), 1)
        self.assertEqual(self.backend.actions, [])
        self.assertFalse(self.device.known_pin)

    def test_password_model_coordinate_is_replaced_by_jev_grounding(self):
        self.device.known_pin = True
        wrong = pin("unlock", method="keypad")
        wrong["box"] = {"left": .7, "top": .7, "right": .9, "bottom": .9}
        c = self.collector(positive_path(), [wrong, {"action": "done"}])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("positive")
        self.assertEqual(result["status"], "collected")
        digits = [e for e in c.events if e["action"] == "pin_digit"]
        self.assertEqual(len(digits), 4)
        self.assertTrue(all(e["params"]["box"] == BOX for e in digits))
        grounded = [e for e in c.events if e["action"] == "ground_password_target"]
        self.assertEqual(grounded[0]["purpose"], "digit_key")
        self.assertEqual(grounded[0]["actor"], "jev")

    def test_pin_length_comes_from_visible_slots_not_password_model_guess(self):
        nav = [decision(view="settings", mode="unknown"), decision(view="mode_intro", mode="off"),
               decision("advance", view="mode_intro", mode="off", agreement="absent"),
               decision("tap", view="mode_intro", mode="off", tap_purpose="enable_mode"),
               decision("advance", view="mode_password", mode="unknown"), decision()]
        c = self.collector(nav, [pin("setup", method="keypad", length=6), pin("confirm", method="keypad", length=6), {"action": "done"}])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "collected")
        digits = [e for e in c.events if e["action"] == "pin_digit"]
        self.assertEqual(len(digits), 8)
        self.assertTrue(all(e["params"]["length"] == 4 for e in digits))
        self.assertEqual(self.device.pin_length, 4)
        self.assertEqual(len([e for e in c.events if e["action"] == "pin_length_corrected"]), 2)

    def test_pin_length_probe_is_narrow_and_has_no_confidence_gate(self):
        for selected, expected in (("four", 4), ("six", 6), ("unknown", None)):
            calls = []
            def transport(url, key, payload, *args):
                calls.append(payload)
                return {"answers": {"length": {"type": "choice", "choice": selected, "confidence": 0}}}
            with patch.dict(os.environ, ENV, clear=True):
                nav = JevNavigator(self.cfg["jev"], transport)
                if expected:
                    self.assertEqual(nav.inspect_pin_length(frame(), {}, 10), expected)
                else:
                    with self.assertRaises(CollectorError): nav.inspect_pin_length(frame(), {}, 10)
            self.assertEqual(set(calls[0]["questions"]), {"length"})

    def test_unknown_pin_in_negative_stage_still_returns_without_input(self):
        nav = negative_path()
        nav[1] = decision("stop", view="mode_password", mode="unknown", scope="requires_user", stop_reason="need_user")
        c = self.collector(nav)
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "collected")
        self.assertEqual(c.password.calls, [])
        self.assertFalse(self.device.attempted)
        self.assertTrue(any(e["action"] == "password_denied" for e in c.events))

    def test_agreement_checkbox_is_separate_from_activation_and_pin(self):
        nav = [decision(view="settings", mode="unknown"), decision(view="mode_intro", mode="off"),
               decision("tap", view="mode_intro", mode="off", agreement="unchecked"),
               decision("advance", view="mode_intro", mode="off", agreement="checked"),
               decision("tap", view="mode_intro", mode="off", tap_purpose="enable_mode"),
               decision("advance", view="mode_password", mode="unknown"), decision()]
        c = self.collector(nav, [pin("setup"), pin("confirm"), {"action": "done"}])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "collected")
        self.assertEqual([e["stage"] for e in c.events if e["action"] == "tap"], ["mode_agreement", "enable_mode"])
        self.assertTrue(all(e["stage"] == "enable_mode" for e in c.events if e["action"] == "enter_pin"))
        self.assertTrue(self.device.known_pin)

    def test_each_request_has_only_its_stage_goal_and_allowed_actions(self):
        for stage_id in ("find_settings", "mode_agreement", "negative_exit", "restart_verify", "positive_verify"):
            calls = []
            def transport(url, key, payload, *args):
                calls.append(payload)
                selected = {"action": "stop", "scope": "navigation", "view": "other", "mode_state": "unknown", "agreement": "unknown", "stop_reason": "insufficient"}
                return {"answers": {k: {"type": "choice", "choice": selected[k]} for k, q in payload["questions"].items() if q["type"] == "choice" and k in selected}}
            with patch.dict(os.environ, ENV, clear=True):
                JevNavigator(self.cfg["jev"], transport).decide(frame(), {"stage": stage_id, "phase": STAGES[stage_id]["phase"]}, 10)
            payload = calls[0]
            self.assertEqual(set(payload["questions"]["action"]["criteria"]), set(STAGES[stage_id]["actions"]))
            self.assertEqual(payload["state"]["rules"], prompt_for(stage_id))
            self.assertNotIn("stage_done", payload["questions"])
            self.assertNotIn("tap_purpose", payload["questions"])
            self.assertNotIn("handle_password", payload["questions"]["action"]["criteria"])
            self.assertEqual("agreement" in payload["questions"], stage_id == "mode_agreement")
            if stage_id.endswith("verify"):
                self.assertNotIn("target", payload["questions"])
                self.assertNotIn("direction", payload["questions"])
        self.assertNotEqual(prompt_for("negative_exit"), prompt_for("positive_exit"))

    def test_wrong_phase_or_missing_stage_never_requests_api(self):
        calls = []
        with patch.dict(os.environ, ENV, clear=True):
            nav = JevNavigator(self.cfg["jev"], lambda *args: calls.append(args))
            for ctx in ({}, {"phase": "restart", "stage": "enable_mode"}):
                with self.assertRaises(CollectorError): nav.decide(frame(), ctx, 10)
        self.assertEqual(calls, [])

    def test_success_uses_full_hierarchy_and_logs_every_transition(self):
        result, c, _ = self.run_success()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s["stage"] for s in c.stage_records], [
            "find_settings", "find_mode", "confirm_on", "negative_exit", "negative_return", "negative_find_settings", "negative_find_mode", "negative_verify",
            "restart_find_settings", "restart_find_mode", "restart_verify", "positive_exit", "positive_authorize", "positive_find_settings", "positive_find_mode", "positive_verify"])
        self.assertTrue(all(s["status"] == "collected" for s in c.stage_records))
        self.assertEqual([key for key, _ in groupby(call[1]["stage"] for call in c.navigator.calls)], [s["stage"] for s in c.stage_records])
        for e in c.events:
            self.assertIn(e["stage"], STAGES)

    def test_hiding_keyboard_does_not_finish_leaving_password_page(self):
        nav = negative_path()
        nav.insert(2, decision(view="mode_password", mode="unknown"))
        c = self.collector(nav)
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "collected")
        self.assertEqual(len([a for a in self.backend.actions if a[0] == "back"]), 2)
        self.assertEqual(c.password.calls, [])
        return_calls = [ctx for _, ctx in c.navigator.calls if ctx["stage"] == "negative_return"]
        self.assertEqual(len(return_calls), 3)
        self.assertEqual(len([e for e in c.events if e["action"] == "stage_complete" and e["stage"] == "negative_return"]), 1)

    def test_only_authorized_substages_can_change_mode_or_use_password(self):
        self.device.set_phase("prepare")
        c = self.collector([])
        c._enter_stage("find_mode"); c.capture(False)
        for d in (decision("tap", view="other", mode="unknown", tap_purpose="enable_mode"),
                  decision("handle_password", view="mode_password", mode="unknown", scope="mode_password")):
            with self.assertRaises(CollectorError): c._execute(d, "recovery_llm")
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(c.password.calls, [])

    def test_exit_checkpoint_cannot_advance_without_actual_exit_tap(self):
        c = self.collector([decision("advance")])
        result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual([s["stage"] for s in result["stages"]], ["negative_exit"])
        self.assertEqual(self.backend.actions, [])

    def test_off_intro_page_is_usable_state_evidence_after_pin(self):
        self.device.known_pin = True
        nav = prepared() + negative_path() + restart_path() + positive_path()[:2] + [decision(mode="off", view="mode_intro")] * 3
        c = self.collector(nav, [pin(), {"action": "done"}])
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge(), self.recorder).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["last_mode_observation"], "off")

    def test_lost_foreground_relaunch_does_not_count_as_exit_attempt(self):
        c = self.collector(negative_path())
        def info():
            launched = any(a[0] == "launch" for a in self.backend.actions)
            return {"package": self.device.package if launched else "com.android.launcher", "navigation_bars": []}
        with patch.object(self.device, "window_info", side_effect=info), contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "collected")
        exits = [e for e in c.events if e["action"] == "tap" and e.get("params", {}).get("purpose") == "exit_entry"]
        self.assertEqual(len(exits), 1)
        self.assertTrue(any(e["action"] == "launch_app" for e in c.events))
        self.assertLess(next(i for i, e in enumerate(c.events) if e["action"] == "launch_app"), next(i for i, e in enumerate(c.events) if e["action"] == "tap"))

    def test_os_navigation_bar_target_is_blocked_without_tap(self):
        c = self.collector([decision("tap", view="other", mode="unknown")])
        info = {"package": self.device.package, "navigation_bars": [(0, 100, 300, 220)]}
        with patch.object(self.device, "window_info", return_value=info), contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(self.backend.actions, [])
        self.assertTrue(any(e["action"] == "system_bar_tap" and e["status"] == "blocked" for e in c.events))

    def test_window_focus_and_vendor_insets_are_parsed_read_only(self):
        raw = b"mCurrentFocus=Window{com.android.systemui:6c0f03a u0 NotificationShade type=2040 }\nInsetsSource id=x type=navigationBars frame=[0,2653][1260,2800] visible=true"
        with patch.object(self.backend, "_run", return_value=raw):
            self.assertEqual(self.device.window_info(), {"package": "com.android.systemui", "navigation_bars": [(0, 2653, 1260, 2800)]})

    def test_recovery_grounding_uses_concrete_target_and_checks_presence(self):
        calls = []
        def transport(url, key, payload, *args):
            calls.append(payload)
            return {"answers": {"visible": {"type": "choice", "choice": "yes", "confidence": 0}, "target": {"type": "box", "box": BOX, "confidence": 0}}}
        with patch.dict(os.environ, ENV, clear=True):
            nav = JevNavigator(self.cfg["jev"], transport)
            self.assertEqual(nav.locate(frame(), "右上角设置齿轮", STAGES["find_settings"], 10), BOX)
            absent = lambda *args: {"answers": {"visible": {"type": "choice", "choice": "no"}, "target": {"type": "box", "box": BOX}}}
            with self.assertRaises(CollectorError): JevNavigator(self.cfg["jev"], absent).locate(frame(), "不可见目标", STAGES["find_settings"], 10)
        self.assertEqual(calls[0]["state"]["target"], "右上角设置齿轮")
        self.assertNotIn("action", calls[0]["questions"])

    def test_skipped_unknown_may_explain_preparation_but_never_claim_pass(self):
        c = self.collector([decision("stop", view="other", mode="unknown")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge("unknown"), self.recorder).run()
        bundle = result["collection"]
        value = final_result(bundle, "unknown")
        value["findings"][0]["image_ids"] = [bundle["preparation"]["before_image"]]
        self.assertEqual(validate_result(value, bundle)["verdict"], "unknown")
        value["findings"][0]["verdict"] = "not_observed"
        with self.assertRaises(CollectorError): validate_result(value, bundle)
