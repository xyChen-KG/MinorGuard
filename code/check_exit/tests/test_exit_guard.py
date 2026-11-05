import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from collect_images.common import CollectorError, Frame, NeedUser
from collect_images.recorder import Recorder
from check_exit.__main__ import main
from check_exit.collector import CollectionLimit, Collector, JevNavigator
from check_exit.device import Device
from check_exit.judge import Judge, validate_result
from check_exit.workflow import TASK_ROOT, Workflow, load_config


BOX = {"left": .1, "top": .2, "right": .5, "bottom": .35}
ENV = {"TYPESAFE_API_KEY": "test-jev-key", "LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "test-model-key", "LLM_MODEL": "test-vision"}


def frame(number=0):
    im = Image.new("RGB", (300, 600), (240, 250 - number % 40, 240))
    ImageDraw.Draw(im).text((30, 40), "SYNTHETIC OFFLINE TEST %d" % number, fill="black")
    stream = io.BytesIO()
    im.save(stream, format="PNG")
    return Frame.from_png(stream.getvalue())


def decision(action="stop", mode="on", view="mode_status", **extra):
    return {"action": action, "scope": "navigation", "tap_purpose": "navigation", "mode_state": mode,
            "view": view, "stage_done": "yes", "stop_reason": "completed", "box": BOX,
            "direction": "up", "length": "short", "agreement": "absent", **extra}


class Scripted:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def decide(self, image, context, timeout):
        self.calls.append((image.png, copy.deepcopy(context)))
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value

    def inspect_pin_length(self, image, context, timeout):
        return 4

    def locate(self, image, target, context, timeout):
        return BOX


class FakeADB:
    serial = "OFFLINE-FAKE-DEVICE"

    def __init__(self):
        self.actions = []
        self.fail_restart = False
        self.fail_pin = False

    def select_device(self):
        return self.serial

    def app_version(self, package):
        return "offline-test-only"

    def wake(self):
        self.actions.append(("wake",))

    def screenshot(self):
        return frame(len(self.actions))

    def tap(self, x, y):
        self.actions.append(("tap", x, y))

    def back(self):
        self.actions.append(("back",))

    def swipe(self, width, height, direction, length):
        self.actions.append(("swipe", direction, length))

    def _run(self, args):
        if args == ["shell", "dumpsys", "window", "displays"]:
            return b"mCurrentFocus=Window{offline u0 com.example.test/.TestActivity}"
        if args == ["shell", "dumpsys", "window", "windows"]:
            return b""
        self.actions.append(tuple(args))
        if self.fail_restart:
            raise NeedUser("offline restart failure")

    def enter_test_pin(self, length):
        self.actions.append(("pin", length))
        if self.fail_pin:
            raise NeedUser("offline partial input failure")

    def launch(self, package):
        self.actions.append(("launch", package))


def pin(stage="unlock", method="text", length=4):
    return {"action": "enter_pin", "stage": stage, "method": method, "length": length, "box": BOX}


def prepared():
    return [decision(), decision(), decision()]


def negative_path():
    return [decision("tap", tap_purpose="exit_entry"), decision(view="mode_password", mode="unknown"), decision(), decision(), decision(), decision()]


def restart_path():
    return [decision(), decision(), decision()]


def positive_path():
    return [decision("tap", tap_purpose="exit_entry"), decision(view="mode_password", mode="unknown"),
            decision(mode="off"), decision(mode="off"), decision(mode="off")]


def final_result(bundle, overall="not_observed"):
    findings = []
    for scene in bundle["scenarios"]:
        ids = [e for e in bundle["events"] if e["id"] in scene["event_ids"]]
        if scene["status"] != "collected":
            verdict = "unknown"
        else:
            verdict = "control_passed" if scene["id"] == "S3" else "not_observed"
            if scene["id"] == "S1" and overall == "bypass":
                verdict = "bypass"
        actual = [e["id"] for e in ids if e["status"] == "executed"]
        findings.append({"scenario_id": scene["id"], "verdict": verdict, "reason": "Synthetic offline result, not a real App verdict.",
                         "image_ids": list(dict.fromkeys(i for i in (scene.get("before_image"), scene.get("after_image")) if i)),
                         "event_ids": actual[:12]})
    return {"verdict": overall, "summary": "Synthetic offline test only", "findings": findings, "limitations": ["Not a real phone/App test"]}


class FakeJudge:
    stats = {"calls": 1, "model": "offline-fake"}

    def __init__(self, overall="not_observed", error=None):
        self.overall, self.error, self.calls = overall, error, []

    def judge(self, bundle, root):
        self.calls.append(copy.deepcopy(bundle))
        if self.error:
            raise self.error
        return validate_result(final_result(bundle, self.overall), bundle)


class TestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load_config(TASK_ROOT / "config.json")
        self.cfg.update(settle_seconds=0, wait_seconds=0)
        self.recorder = Recorder(Path(self.temp.name))
        self.backend = FakeADB()
        self.device = Device(self.backend, "com.example.test", self.cfg["password"])

    def collector(self, nav, password=()):
        return Collector(self.cfg, self.device, Scripted(nav), Scripted(password), self.recorder, sleeper=lambda _: None)

    def run_success(self, pin_method="text"):
        self.device.known_pin = True
        nav = prepared() + negative_path() + restart_path() + positive_path()
        collector = self.collector(nav, [pin(method=pin_method), {"action": "done"}])
        judge = FakeJudge()
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, collector, judge, self.recorder).run()
        return result, collector, judge


class DeviceTests(TestBase):
    def test_negative_pin_and_password_taps_denied_even_with_known_password(self):
        self.device.known_pin = True
        for phase in ("no_auth", "restart"):
            self.device.set_phase(phase)
            for op in (lambda: self.device.authorize_password("unlock"), lambda: self.device.reserve_pin("unlock", 4),
                       lambda: self.device.pin_text("unlock", 4), lambda: self.device.password_tap("unlock", 20, 30)):
                with self.assertRaises(CollectorError):
                    op()
        self.assertEqual(self.backend.actions, [])

    def test_setup_confirm_unlock_capabilities(self):
        self.device.set_phase("prepare")
        with self.assertRaises(NeedUser):
            self.device.authorize_password("unlock")
        with self.assertRaises(NeedUser):
            self.device.authorize_password("confirm")
        self.device.reserve_pin("setup", 4)
        self.device.pin_text("setup", 4)
        self.device.entered.add("setup")
        self.device.reserve_pin("confirm", 4)
        self.device.entered.add("confirm")
        self.assertFalse(self.device.known_pin)
        self.device.note_mode_enabled()
        self.assertTrue(self.device.known_pin)
        self.device.set_phase("positive")
        self.device.authorize_password("unlock")

    def test_no_setup_and_unknown_password_stop(self):
        self.cfg["password"]["allow_setup"] = False
        self.device.set_phase("prepare")
        with self.assertRaises(NeedUser):
            self.device.reserve_pin("setup", 4)
        self.device.set_phase("positive")
        with self.assertRaises(NeedUser):
            self.device.reserve_pin("unlock", 4)

    def test_single_step_setup_promotes_pin_only_after_explicit_enabled_state(self):
        self.device.set_phase("prepare")
        self.device.reserve_pin("setup", 4)
        self.device.pin_text("setup", 4)
        self.device.entered.add("setup")
        self.assertFalse(self.device.known_pin)
        self.device.note_mode_enabled()
        self.assertTrue(self.device.known_pin)

    def test_incomplete_confirmation_does_not_promote_setup_pin(self):
        self.device.set_phase("prepare")
        self.device.reserve_pin("setup", 4)
        self.device.entered.add("setup")
        self.device.reserve_pin("confirm", 4)
        self.device.note_mode_enabled()
        self.assertFalse(self.device.known_pin)

    def test_wrong_length_and_duplicate_reservation_denied(self):
        self.device.set_phase("prepare")
        for length in (True, 5, "4", -1):
            with self.assertRaises(CollectorError):
                self.device.reserve_pin("setup", length)
        self.device.reserve_pin("setup", 4)
        with self.assertRaises(NeedUser):
            self.device.reserve_pin("setup", 4)
        self.device.entered.add("setup")
        with self.assertRaises(NeedUser):
            self.device.reserve_pin("confirm", 6)

    def test_partial_pin_failure_cannot_retry(self):
        self.device.known_pin = True
        self.device.set_phase("positive")
        self.backend.fail_pin = True
        self.device.reserve_pin("unlock", 4)
        with self.assertRaises(NeedUser):
            self.device.pin_text("unlock", 4)
        with self.assertRaises(CollectorError):
            self.device.pin_text("unlock", 4)
        with self.assertRaises(NeedUser):
            self.device.reserve_pin("unlock", 4)

    def test_restart_only_current_package_in_restart_phase(self):
        with self.assertRaises(CollectorError):
            self.device.restart()
        self.device.set_phase("restart")
        self.device.restart()
        self.assertEqual(self.backend.actions, [("shell", "am", "force-stop", "com.example.test"), ("launch", "com.example.test")])
        for package in ("com.test;echo bad", "", "../x", "com.test\nother"):
            with self.assertRaises(CollectorError):
                Device(self.backend, package, self.cfg["password"])


class CollectorTests(TestBase):
    def test_unused_scope_answer_does_not_block_valid_stop(self):
        for scope in ("mode_password", "requires_user"):
            with self.subTest(scope=scope):
                c = self.collector([decision("tap", tap_purpose="exit_entry"), decision(view="mode_password", mode="unknown"), decision(), decision(), decision(), decision(scope=scope)])
                result = c.run_phase("no_auth")
                self.assertEqual(result["status"], "collected")
                self.assertEqual(c.password.calls, [])

    def test_tap_on_positive_password_screen_must_use_password_tool(self):
        self.device.known_pin = True
        c = self.collector([decision("tap", tap_purpose="exit_entry"), decision("tap", mode="unknown", view="mode_password"), decision(mode="off"), decision(mode="off"), decision(mode="off")],
                           [pin(), {"action": "done"}])
        result = c.run_phase("positive")
        self.assertEqual(result["status"], "collected")
        self.assertEqual(len(c.password.calls), 2)
        # Ordinary Jev tap applies only to the exit entry, not to a password key.
        self.assertEqual(len([e for e in c.events if e["action"] == "tap"]), 1)
        self.assertEqual(len([e for e in c.events if e["action"] == "enter_pin"]), 1)

    def test_first_setup_confirm_then_positive_unlock(self):
        nav = [decision(view="settings", mode="unknown"), decision(mode="off", view="mode_intro"), decision(mode="off", view="mode_intro"), decision("tap", mode="off", tap_purpose="enable_mode"),
               decision("handle_password", mode="unknown", view="mode_password", scope="mode_password"), decision()] + negative_path() + restart_path() + positive_path()
        passwords = [pin("setup"), pin("confirm"), {"action": "done"}, pin("unlock"), {"action": "done"}]
        c = self.collector(nav, passwords)
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge(), self.recorder).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.device.entered, {"setup", "confirm", "unlock"})
        self.assertTrue(self.device.known_pin)
        self.assertEqual([(e["phase"], e["params"]["kind"]) for e in c.events if e["action"] == "enter_pin"],
                         [("prepare", "setup"), ("prepare", "confirm"), ("positive", "unlock")])

    def test_end_to_end_with_raw_images_and_separate_annotations(self):
        result, collector, judge = self.run_success()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["judgement"]["verdict"], "not_observed")
        self.assertEqual([s["status"] for s in result["collection"]["scenarios"]], ["collected"] * 3)
        self.assertEqual(len(judge.calls), 1)
        for item in collector.images:
            import hashlib
            self.assertEqual(hashlib.sha256((self.recorder.root / item["file"]).read_bytes()).hexdigest(), item["sha256"])
        self.assertTrue(list((self.recorder.root / "images").glob("*-tap.png")))
        self.assertTrue((self.recorder.root / "report.md").is_file())
        self.assertEqual([e["phase"] for e in collector.events if e["action"] == "enter_pin"], ["positive"])

    def test_keypad_records_each_digit_without_plaintext_pin(self):
        result, collector, _ = self.run_success("keypad")
        self.assertEqual(result["status"], "completed")
        digits = [e for e in collector.events if e["action"] == "pin_digit"]
        self.assertEqual(len(digits), 4)
        self.assertEqual([e["params"]["index"] for e in digits], [1, 2, 3, 4])
        self.assertNotIn('"value":', self.recorder.trace_path.read_text())

    def test_early_stop_not_completion(self):
        c = self.collector([decision()])
        self.device.set_phase("prepare")
        c.capture(False)
        result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(self.backend.actions, [])

    def test_intro_or_home_not_explicit_mode_state(self):
        c = self.collector([decision(view="other")])
        result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")

    def test_negative_password_request_never_calls_password_client(self):
        c = self.collector(negative_path())
        result = c.run_phase("no_auth")
        self.assertEqual(result["status"], "collected")
        self.assertEqual(c.password.calls, [])
        self.assertEqual(self.backend.actions[-1], ("back",))
        self.assertTrue(any(e["action"] == "password_denied" for e in c.events))

    def test_negative_tap_on_recognized_password_page_forced_back(self):
        c = self.collector([decision("tap", view="mode_password", mode="unknown"), decision()])
        c.run_phase("no_auth")
        self.assertEqual(self.backend.actions, [("back",)])

    def test_restart_failure_does_not_claim_restart_done(self):
        self.backend.fail_restart = True
        c = self.collector([])
        self.device.set_phase("prepare")
        c.capture(False)
        result = c.run_phase("restart")
        self.assertEqual(result["status"], "need_user")
        e = next(e for e in c.events if e["action"] == "restart_app")
        self.assertEqual(e["status"], "failed_or_unknown")
        self.assertFalse(any(a[0] == "launch" for a in self.backend.actions))

    def test_restarting_cannot_turn_mode_back_on(self):
        c = self.collector([decision("tap", tap_purpose="enable_mode", view="other", mode="unknown")])
        result = c.run_phase("restart")
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(a[0] == "tap" for a in self.backend.actions))

    def test_off_state_stops_dependent_tests_without_reenabling(self):
        c = self.collector(prepared() + [decision("tap", tap_purpose="exit_entry")] + [decision(mode="off")] * 4)
        judge = FakeJudge("bypass")
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, judge, self.recorder).run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["judgement"]["verdict"], "bypass")
        self.assertEqual([s["status"] for s in result["collection"]["scenarios"]], ["collected", "skipped", "skipped"])

    def test_unknown_password_collects_negatives_but_no_guess(self):
        nav = prepared() + negative_path() + restart_path() + positive_path()[:2]
        c = self.collector(nav)
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge("unknown"), self.recorder).run()
        self.assertEqual(result["collection"]["scenarios"][2]["status"], "need_user")
        self.assertEqual(c.password.calls, [])
        self.assertEqual(result["judgement"]["verdict"], "unknown")

    def test_user_interrupt_does_not_call_final_model(self):
        c = self.collector([decision(), decision(), decision(), decision("tap", tap_purpose="exit_entry"), KeyboardInterrupt()])
        judge = FakeJudge()
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, judge, self.recorder).run()
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(judge.calls, [])
        self.assertEqual(result["collection"]["scenarios"][0]["status"], "interrupted")
        self.assertTrue(result["collection"]["scenarios"][0]["event_ids"])

    def test_api_failure_and_black_screen_are_not_app_failures(self):
        for failure in (CollectorError("offline API failure"), NeedUser("offline black screen")):
            c = self.collector([failure])
            result = c.run_phase("prepare")
            self.assertNotEqual(result["status"], "collected")
        c = self.collector([])
        black = Image.new("RGB", (300, 600), "black")
        output = io.BytesIO(); black.save(output, format="PNG")
        self.backend.screenshot = lambda: Frame.from_png(output.getvalue())
        self.assertEqual(c.run_phase("prepare")["status"], "need_user")

    def test_transient_capture_failure_retries_read_without_repeating_pin(self):
        c = self.collector([])
        self.device.set_phase("positive")
        self.device.known_pin = True
        c.capture(False)
        self.device.reserve_pin("unlock", 4)
        images = iter([CollectorError("offline invalid PNG"), frame(1)])

        def screenshot():
            value = next(images)
            if isinstance(value, Exception):
                raise value
            return value

        self.backend.screenshot = screenshot
        with contextlib.redirect_stdout(io.StringIO()):
            c.perform("enter_pin", lambda: self.device.pin_text("unlock", 4))
        self.assertEqual(self.backend.actions, [("pin", 4)])
        self.assertEqual(c.events[-1]["status"], "executed")
        self.assertEqual(c.events[-1]["after_image"], "I002")
        self.assertEqual([e["action"] for e in c.events], ["capture_retry", "enter_pin"])

    def test_persistent_capture_failure_keeps_action_uncertainty_and_stops(self):
        c = self.collector([])
        self.device.set_phase("prepare")
        c.capture(False)
        attempts = []

        def unavailable():
            attempts.append(1)
            raise NeedUser("offline disconnected")

        self.backend.screenshot = unavailable
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(NeedUser):
            c.perform("back", self.backend.back)
        self.assertEqual(self.backend.actions, [("back",)])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(c.events[-1]["status"], "executed_after_capture_missing")
        self.assertIsNone(c.events[-1]["after_image"])

    def test_actual_black_frame_is_not_a_transport_retry(self):
        c = self.collector([])
        self.device.set_phase("prepare")
        output = io.BytesIO()
        Image.new("RGB", (300, 600), "black").save(output, format="PNG")
        self.backend.screenshot = lambda: Frame.from_png(output.getvalue())
        with self.assertRaises(NeedUser):
            c.capture(False)
        self.assertFalse(any(e["action"] == "capture_retry" for e in c.events))

    def test_action_and_request_budgets(self):
        self.cfg["max_requests"] = 1
        c = self.collector([decision("wait", mode="unknown", view="other")])
        result = c.run_phase("prepare")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(c.requests, 1)
        self.cfg["max_requests"] = 80
        self.cfg["max_steps"] = 1
        c = self.collector([decision("wait", mode="unknown", view="other")])
        self.assertEqual(c.run_phase("prepare")["status"], "insufficient")
        self.assertEqual(c.steps, 1)

    def test_observed_biometrics_and_password_error_stop(self):
        for view in ("identity", "locked", "pin_error"):
            with self.subTest(view=view):
                c = self.collector([decision("tap", mode="unknown", view=view)])
                result = c.run_phase("no_auth")
                self.assertEqual(result["status"], "need_user")
                self.assertEqual(c.password.calls, [])
                self.assertEqual(self.backend.actions, [])

    def test_sensitive_scope_stops_without_click(self):
        c = self.collector([decision("tap", scope="requires_user", view="other", mode="unknown")])
        self.assertEqual(c.run_phase("prepare")["status"], "need_user")
        self.assertEqual(self.backend.actions, [])

    def test_final_observation_not_stale_after_partial_input(self):
        self.device.known_pin = True
        self.backend.fail_pin = True
        nav = prepared() + negative_path() + restart_path() + positive_path()[:2]
        c = self.collector(nav, [pin()])
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge("unknown"), self.recorder).run()
        self.assertEqual(result["last_mode_observation"], "unknown")
        self.assertNotIn("unlock", self.device.entered)


class JevProtocolTests(TestBase):
    def response(self, **updates):
        selected = decision(**updates)
        answer = {k: {"type": "choice", "choice": selected[k], "confidence": 0} for k in
                  ("action", "scope", "tap_purpose", "mode_state", "view", "stage_done", "stop_reason", "direction", "length")}
        answer["target"] = {"type": "box", "box": dict(BOX), "confidence": 0}
        if selected["action"] == "tap":
            answer["action"]["choice"] = {"navigation": "tap_navigation", "exit_entry": "tap_exit", "enable_mode": "tap_enable"}[selected["tap_purpose"]]
        if selected["action"] == "stop":
            answer["stop_reason"]["choice"] = "insufficient"
        return {"model": "offline-jev", "answers": answer, "usage": {"input_tokens": 12, "output_tokens": 3}}

    def test_choice_box_request_and_no_confidence_gate(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return self.response(action="tap", tap_purpose="exit_entry")
        with patch.dict(os.environ, ENV, clear=True):
            result = JevNavigator(self.cfg["jev"], transport).decide(frame(), {"phase": "no_auth", "stage": "negative_exit"}, 10)
        self.assertEqual(result["action"], "tap")
        self.assertEqual(result["box"], BOX)
        payload = calls[0][2]
        self.assertEqual(calls[0][4], {"TypeSafe-Danger-Zone-Images": "true"})
        self.assertIn(payload["state"]["screen"], payload["assets"])
        self.assertEqual(payload["questions"]["target"]["type"], "box")
        self.assertNotIn("verdict", payload["questions"])
        self.assertNotIn("api_key", payload["state"])

    def test_unused_bad_box_does_not_block_stop(self):
        response = self.response()
        response["answers"].pop("target")
        with patch.dict(os.environ, ENV, clear=True):
            result = JevNavigator(self.cfg["jev"], lambda *args: response).decide(frame(), {"phase": "prepare", "stage": "find_mode"}, 10)
        self.assertEqual(result["action"], "stop")

    def test_bad_action_box_or_observation_rejected(self):
        for field in ("action", "target", "mode_state"):
            with self.subTest(field=field):
                response = self.response(action="tap")
                if field == "target": response["answers"][field]["box"]["left"] = 1.5
                else: response["answers"][field]["choice"] = "arbitrary_shell"
                with patch.dict(os.environ, ENV, clear=True):
                    with self.assertRaises(CollectorError):
                        JevNavigator(self.cfg["jev"], lambda *args: response).decide(frame(), {"phase": "prepare", "stage": "find_mode"}, 10)

    def test_swipe_parameters_are_explicit(self):
        with patch.dict(os.environ, ENV, clear=True):
            result = JevNavigator(self.cfg["jev"], lambda *args: self.response(action="swipe")).decide(frame(), {"phase": "prepare", "stage": "find_mode"}, 10)
        self.assertEqual((result["direction"], result["length"]), ("up", "short"))

    def test_missing_key_never_calls_transport(self):
        calls = []
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CollectorError):
                JevNavigator(self.cfg["jev"], lambda *args: calls.append(args)).decide(frame(), {}, 10)
        self.assertEqual(calls, [])


class JudgeTests(TestBase):
    def test_must_cite_actual_required_action_and_both_endpoints(self):
        result, _, _ = self.run_success()
        bundle = result["collection"]
        value = final_result(bundle)
        scene = bundle["scenarios"][0]
        only_back = next(e["id"] for e in bundle["events"] if e["id"] in scene["event_ids"] and e["action"] == "back")
        value["findings"][0]["event_ids"] = [only_back]
        with self.assertRaises(CollectorError): validate_result(value, bundle)
        value = final_result(bundle)
        intermediate = next(e["after_image"] for e in bundle["events"] if e["id"] in scene["event_ids"] and e["action"] == "tap")
        value["findings"][0]["image_ids"] = [scene["before_image"], intermediate]
        with self.assertRaises(CollectorError): validate_result(value, bundle)

    def test_symlink_image_directory_rejected_before_model_call(self):
        result, _, _ = self.run_success()
        # Move only test-owned evidence to a sibling; no destructive directory operations.
        images = self.recorder.root / "images"
        elsewhere = Path(self.temp.name) / "moved_images"
        images.rename(elsewhere)
        images.symlink_to(elsewhere, target_is_directory=True)
        calls = []
        with patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(CollectorError):
                Judge(self.cfg["judge"], lambda *args: calls.append(args)).judge(result["collection"], self.recorder.root)
        self.assertEqual(calls, [])
    def test_actual_http_payload_high_thinking_and_all_raw_images(self):
        result, c, _ = self.run_success()
        bundle = result["collection"]
        calls = []
        def transport(url, key, payload, timeout):
            calls.append((url, key, payload, timeout))
            return {"model": "fake-final", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(final_result(bundle)), "reasoning_content": "DO NOT PERSIST THIS"}}], "usage": {"prompt_tokens": 10}}
        with patch.dict(os.environ, ENV, clear=True):
            judge = Judge(self.cfg["judge"], transport)
            self.assertEqual(judge.judge(bundle, self.recorder.root)["verdict"], "not_observed")
        payload = calls[0][2]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        uploaded = [x for x in payload["messages"][1]["content"] if x["type"] == "image_url"]
        self.assertEqual(len(uploaded), len(c.images))
        self.assertNotIn("tools", payload)
        self.assertNotIn("DO NOT PERSIST", json.dumps(judge.stats))
        self.assertNotIn("test-model-key", self.recorder.trace_path.read_text())
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        anchors = context["reference_index"]
        by_id = {e["id"]: e for e in bundle["events"]}
        for scene in bundle["scenarios"]:
            entry_ids = anchors[scene["id"]]["must_cite_one_actual_entry_or_restart_event"]
            self.assertTrue(entry_ids)
            for eid in entry_ids:
                self.assertIn(eid, scene["event_ids"])
                self.assertEqual(by_id[eid]["status"], "executed")
                self.assertEqual(by_id[eid]["action"], "restart_app" if scene["id"] == "S2" else "tap")
        pin_ids = anchors["S3"]["must_cite_one_pin_input_event_for_S3"]
        self.assertTrue(pin_ids)
        self.assertTrue(set(pin_ids).isdisjoint(anchors["S3"]["must_cite_one_actual_entry_or_restart_event"]))

    def test_invalid_findings_rejected(self):
        result, _, _ = self.run_success()
        bundle = result["collection"]
        for change in ("nonexistent_image", "nonexistent_event", "duplicate", "missing", "bad_category", "no_proof", "wrong_overall", "cross_phase"):
            with self.subTest(change=change):
                value = final_result(bundle)
                if change == "nonexistent_image": value["findings"][0]["image_ids"] = ["I999"]
                if change == "nonexistent_event": value["findings"][0]["event_ids"] = ["T999"]
                if change == "duplicate": value["findings"][1] = copy.deepcopy(value["findings"][0])
                if change == "missing": value["findings"].pop()
                if change == "bad_category": value["findings"][2]["verdict"] = "bypass"
                if change == "no_proof": value["findings"][0]["image_ids"] = []
                if change == "wrong_overall": value["verdict"] = "bypass"
                if change == "cross_phase": value["findings"][0]["image_ids"] = [bundle["scenarios"][2]["after_image"]]
                with self.assertRaises(CollectorError): validate_result(value, bundle)

    def test_unperformed_scenario_cannot_pass(self):
        result, _, _ = self.run_success()
        bundle = copy.deepcopy(result["collection"])
        value = final_result(bundle)
        bundle["scenarios"][1]["status"] = "skipped"
        with self.assertRaises(CollectorError): validate_result(value, bundle)

    def test_tampered_or_outside_images_fail_before_request(self):
        result, _, _ = self.run_success()
        bundle = result["collection"]
        calls = []
        judge = Judge(self.cfg["judge"], lambda *args: calls.append(args))
        with patch.dict(os.environ, ENV, clear=True):
            changed = copy.deepcopy(bundle); changed["images"][0]["file"] = "../.env"
            with self.assertRaises(CollectorError): judge.judge(changed, self.recorder.root)
            changed = copy.deepcopy(bundle); changed["images"][0]["sha256"] = "0" * 64
            with self.assertRaises(CollectorError): judge.judge(changed, self.recorder.root)
            judge.config = dict(self.cfg["judge"], max_images=1)
            with self.assertRaises(CollectorError): judge.judge(bundle, self.recorder.root)
        self.assertEqual(calls, [])

    def test_truncated_model_response_no_retry(self):
        result, _, _ = self.run_success()
        calls = []
        def transport(*args):
            calls.append(args)
            return {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]}
        with patch.dict(os.environ, ENV, clear=True):
            with self.assertRaises(CollectorError): Judge(self.cfg["judge"], transport).judge(result["collection"], self.recorder.root)
        self.assertEqual(len(calls), 1)

    def test_final_failure_keeps_collection_and_report(self):
        c = self.collector(prepared() + [decision("tap", tap_purpose="exit_entry")] + [decision(mode="off")] * 4)
        with contextlib.redirect_stdout(io.StringIO()):
            result = Workflow(self.cfg, c, FakeJudge(error=CollectorError("offline final error")), self.recorder).run()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["judgement"])
        for filename in ("collection.json", "scenarios.json", "report.md", "result.json", "trace.jsonl"):
            self.assertTrue((self.recorder.root / filename).is_file())
        self.assertIn("不确定", (self.recorder.root / "report.md").read_text())


class ConfigAndCLI(TestBase):
    def test_relative_paths_and_image_budget(self):
        root = TASK_ROOT.parents[1]
        self.assertEqual(self.cfg["adb_path"], str(root / "tools/platform-tools/adb"))
        self.assertEqual(self.cfg["output_dir"], str(root / "results/check_exit"))
        raw = json.loads((TASK_ROOT / "config.json").read_text())
        for mutate in (lambda c: c.update(max_steps=True), lambda c: c["judge"].update(max_images=2), lambda c: c["password"].update(existing_pin_is_all_ones="true")):
            value = copy.deepcopy(raw); mutate(value)
            path = Path(self.temp.name) / "config.json"; path.write_text(json.dumps(value))
            with self.assertRaises(CollectorError): load_config(path)

    def test_check_is_read_only_no_models_no_ssh_no_screenshot(self):
        with patch.dict(os.environ, ENV, clear=True), patch("check_exit.__main__.ADB", return_value=self.backend), \
             patch("check_exit.__main__.JevNavigator") as jev, patch("check_exit.__main__.Judge") as judge, \
             patch("check_exit.__main__.Workflow") as workflow, contextlib.redirect_stdout(io.StringIO()) as output:
            status = main(["--package", "com.example.test", "--env-file", str(Path(self.temp.name) / "missing.env"), "--check"])
        self.assertEqual(status, 0)
        self.assertEqual(self.backend.actions, [])
        jev.assert_not_called(); judge.assert_not_called(); workflow.assert_not_called()
        self.assertNotIn("test-model-key", output.getvalue())

    def test_cli_missing_credentials_stops_before_device(self):
        with patch.dict(os.environ, {}, clear=True), patch("check_exit.__main__.ADB") as adb, contextlib.redirect_stdout(io.StringIO()):
            status = main(["--package", "com.example.test", "--env-file", str(Path(self.temp.name) / "none")])
        self.assertEqual(status, 1)
        adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
