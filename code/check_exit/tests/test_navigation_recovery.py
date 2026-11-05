"""Regression coverage for the real repeated-scroll failure and phase isolation."""

import contextlib
import io
import json
import os
from unittest.mock import patch

from collect_images.common import CollectorError
from check_exit.collector import Collector
from check_exit.recovery import RecoveryNavigator
from check_exit.stages import INITIAL
from check_exit.tests.test_exit_guard import BOX, ENV, Scripted, TestBase, decision, frame


def recovered(**updates):
    return decision("hand_back", mode="unknown", view="other", progress_confirmed=True,
                    reason="菜单已经实际展开", hint="从展开的菜单进入设置", **updates)


class NavigationRecoveryTests(TestBase):
    def make(self, nav, recovery):
        return Collector(self.cfg, self.device, Scripted(nav), Scripted([]), self.recorder,
                         sleeper=lambda _: None, recovery=Scripted(recovery))

    def test_three_ineffective_swipes_delegate_then_confirm_and_return(self):
        self.backend.screenshot = lambda: frame(30 if any(a[0] == "tap" for a in self.backend.actions) else 0)
        nav = [decision("swipe", mode="unknown", view="other")] * 3 + [decision(), decision(), decision()]
        c = self.make(nav, [decision("tap", mode="unknown", view="other"), recovered()])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "collected")
        self.assertEqual([a[0] for a in self.backend.actions], ["swipe"] * 3 + ["tap"])
        self.assertEqual((c.recovery_count, c.recovery_calls, c.handbacks), (1, 2, 1))
        self.assertEqual(c.requests, 9)  # Includes the separate Jev target-grounding request.
        self.assertEqual(c.navigator.calls[3][1]["recovery_hint"], "从展开的菜单进入设置")
        self.assertEqual([call[1]["after_action"] for call in c.recovery.calls], [False, True])
        self.assertEqual(len(c.recovery.calls[-1][1]["recent_frames"]), 3)
        self.assertTrue(any(e["action"] == "return_to_jev" for e in c.events))
        self.assertTrue(any(e["action"] == "tap" and e["actor"] == "recovery_llm" and e.get("annotated_image") for e in c.events))

    def test_six_changing_feed_swipes_still_trigger_semantic_review(self):
        from PIL import Image
        from collect_images.common import Frame
        def moving():
            stream = io.BytesIO()
            Image.new("RGB", (300, 600), "white" if len(self.backend.actions) % 2 else "black").save(stream, format="PNG")
            # Keep a visible pixel in the black synthetic page.
            if len(self.backend.actions) % 2 == 0:
                return frame(0)
            return Frame.from_png(stream.getvalue())
        self.backend.screenshot = moving
        c = self.make([decision("swipe", mode="unknown", view="other")] * 8, [CollectorError("offline deliberate API failure")])
        # Use a strongly changing image pair, as with an infinite recommendation feed.
        with patch("check_exit.collector.frame_difference", return_value=60), contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(len(c.navigator.calls), 6)
        self.assertEqual(c.recovery_calls, 1)
        self.assertEqual(result["status"], "error")

    def test_disabled_or_missing_recovery_stops_instead_of_spinning(self):
        c = self.make([decision("swipe", mode="unknown", view="other")] * 10, [])
        self.backend.screenshot = lambda: frame(0)
        c.recovery = None
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(len(c.navigator.calls), 3)
        self.assertEqual(c.steps, 3)

    def test_handoff_with_unchanged_screen_cannot_claim_recovered(self):
        self.backend.screenshot = lambda: frame(0)
        c = self.make([decision("swipe", mode="unknown", view="other")] * 3,
                      [decision("tap", mode="unknown", view="other"), recovered()])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(c.handbacks, 0)
        self.assertEqual(len(c.navigator.calls), 3)

    def test_recovery_cannot_type_negative_pin_or_reenable_mode(self):
        for phase in ("no_auth", "restart"):
            with self.subTest(phase=phase):
                c = self.make([], [decision("handle_password", scope="mode_password", view="mode_password", mode="unknown"),
                                   decision("stop", stop_reason="insufficient")])
                self.device.set_phase(phase)
                c._enter_stage(INITIAL[phase])
                c.capture(False)
                with contextlib.redirect_stdout(io.StringIO()):
                    c._recover("offline synthetic repetition", 0)
                self.assertEqual(c.password.calls, [])
                self.assertFalse(any(a[0] == "pin" for a in self.backend.actions))
                self.assertFalse(self.device.attempted)
                c.recovery = Scripted([decision("tap", tap_purpose="enable_mode")])
                before = len(self.backend.actions)
                with self.assertRaises(CollectorError), contextlib.redirect_stdout(io.StringIO()):
                    c._recover("offline synthetic repetition", 0)
                self.assertEqual(len(self.backend.actions), before)

    def test_recovery_budget_does_not_add_an_unbounded_retry_loop(self):
        self.cfg["recovery"]["max_actions"] = 1
        c = self.make([], [decision("back"), decision("back")])
        self.device.set_phase("prepare"); c._enter_stage("find_mode"); c.capture(False)
        with self.assertRaises(CollectorError), contextlib.redirect_stdout(io.StringIO()):
            c._recover("offline synthetic repetition", 0)
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(c.recovery_calls, 2)
        self.assertEqual(c.handbacks, 0)

    def test_recovery_actions_count_toward_phase_budget(self):
        self.cfg["phase_steps"] = 4
        self.backend.screenshot = lambda: frame(0)
        c = self.make([decision("swipe", mode="unknown", view="other")] * 3, [decision("back"), recovered()])
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(c.steps, 4)
        self.assertEqual(c.recovery_calls, 1)

    def test_recovery_sensitive_view_stops_before_any_action(self):
        c = self.make([], [decision("tap", view="identity")])
        self.device.set_phase("prepare"); c._enter_stage("find_mode"); c.capture(False)
        with self.assertRaises(CollectorError), contextlib.redirect_stdout(io.StringIO()):
            c._recover("offline synthetic repetition", 0)
        self.assertEqual(self.backend.actions, [])


class RecoveryProtocolTests(TestBase):
    def test_high_thinking_and_no_reasoning_or_secret_saved(self):
        calls = []
        def transport(url, key, payload, timeout):
            calls.append((url, key, payload, timeout))
            value = decision("tap", mode="unknown", view="other", reason="进入顶部菜单")
            return {"model": "offline-recovery", "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(value), "reasoning_content": "DO NOT SAVE"}}]}
        with patch.dict(os.environ, ENV, clear=True):
            result = RecoveryNavigator(self.cfg["recovery"], transport).decide(frame(),
                {"phase": "no_auth", "goal": "offline", "after_action": False, "recent_frames": [frame()]}, 90)
        payload = calls[0][2]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("recent_frames", payload["messages"][1]["content"][0]["text"])
        self.assertNotIn("DO NOT SAVE", json.dumps(result))
        self.assertNotIn(ENV["LLM_API_KEY"], json.dumps(payload))
        self.assertEqual(result["box"], BOX)

    def test_handoff_needs_actual_action_and_confirmation(self):
        value = recovered()
        with self.assertRaises(CollectorError): RecoveryNavigator.parse(value, frame(), False)
        for change in ({"progress_confirmed": False}, {"scope": "mode_password"}, {"view": "identity"}, {"hint": ""}):
            bad = dict(value, **change)
            with self.assertRaises(CollectorError): RecoveryNavigator.parse(bad, frame(), True)

    def test_invalid_action_and_missing_click_purpose_rejected(self):
        for value in (decision("shell"), dict(decision("tap"), tap_purpose=""), dict(decision("tap"), box={"left": -3})):
            with self.assertRaises(CollectorError): RecoveryNavigator.parse(value, frame(), False)
