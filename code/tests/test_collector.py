"""Offline tests: synthetic screenshots, fake device/models, local-only HTTP server."""

import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from collect_images.adb import ADB
from collect_images.common import (CODE_ROOT, CollectorError, Frame, NeedUser,
                                  box_pixels, frame_difference, load_config, load_env,
                                  post_json, validate_url, http_error_detail)
from collect_images.jev import JevClient
from collect_images.password import PasswordClient
from collect_images.recorder import Recorder
from collect_images.workflow import Workflow


BOX = {"left": .1, "top": .2, "right": .7, "bottom": .4}


def frame(label="HOME", color="white"):
    image = Image.new("RGB", (200, 400), color)
    draw = ImageDraw.Draw(image)
    draw.text((25, 40), label, fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return Frame.from_png(stream.getvalue())


def config():
    value = load_config(CODE_ROOT / "config.json")
    value.update(goal="Collect test screens", evidence=["Mode", "Limits"],
                 settle_seconds=0, wait_seconds=0, stability_checks=0,
                 max_steps=50, max_seconds=60)
    return value


def stop(evidence=None, reason="completed", done=True):
    return {"action": "stop", "evidence": evidence or [], "stop_reason": reason, "goal_done": done}


class FakeADB:
    def __init__(self, frames=None):
        self.frames = frames or [frame()]
        self.index = 0
        self.serial = "offline-test-device"
        self.actions = []

    def select_device(self):
        return self.serial

    def app_version(self, package):
        return "offline-test"

    def screenshot(self):
        return self.frames[min(self.index, len(self.frames) - 1)]

    def advance(self, action):
        self.actions.append(action)
        self.index += 1

    def tap(self, x, y):
        self.advance(("tap", x, y))

    def swipe(self, width, height, direction, length):
        self.advance(("swipe", direction, length))

    def back(self):
        self.advance(("back",))

    def enter_test_pin(self, length):
        self.advance(("enter_pin", length))

    def launch(self, package):
        self.advance(("launch", package))


class Scripted:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.frames = []
        self.contexts = []

    def decide(self, image, context, timeout):
        self.frames.append(image.png)
        self.contexts.append(copy.deepcopy(context))
        value = next(self.decisions)
        if isinstance(value, BaseException):
            raise value
        return value


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = Recorder(Path(self.temp.name))
        self.cfg = config()

    def run_flow(self, decisions, adb=None, password=None):
        model = Scripted(decisions)
        driver = adb or FakeADB()
        workflow = Workflow(self.cfg, driver, model, password, self.recorder, sleeper=lambda _: None)
        with contextlib.redirect_stdout(io.StringIO()):
            result = workflow.run()
        return result, model, driver

    def trace(self):
        return [json.loads(line) for line in self.recorder.trace_path.read_text().splitlines()]

    def test_full_collection_and_original_images_never_replaced_in_model_input(self):
        home, settings, limits = frame(), frame("SETTINGS", "lightblue"), frame("LIMITS", "lightgreen")
        result, model, adb = self.run_flow([
            {"action": "tap", "box": BOX, "evidence": []},
            {"action": "swipe", "direction": "up", "length": "short", "evidence": [0]},
            stop([1]),
        ], FakeADB([home, settings, limits]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["image_count"], 3)
        self.assertEqual(model.frames, [home.png, settings.png, limits.png])
        root = self.recorder.root
        self.assertNotEqual((root / "images/001.png").read_bytes(), home.png)
        self.assertEqual((root / "images/002.png").read_bytes(), settings.png)
        self.assertEqual((root / "images/003.png").read_bytes(), limits.png)
        trace = self.trace()
        self.assertEqual(trace[0]["after_image"], trace[1]["before_image"])
        self.assertEqual(trace[1]["after_image"], trace[2]["before_image"])
        self.assertEqual(trace[0]["before_observation_sha256"], home.sha256)
        self.assertEqual(trace[0]["params"]["point"], list(adb.actions[0][1:]))
        self.assertTrue((root / "result.json").is_file())

    def test_completion_downgraded_when_evidence_missing(self):
        result, _, adb = self.run_flow([stop([0])])
        self.assertEqual(result["status"], "insufficient")
        self.assertFalse(result["evidence"][1]["collected"])
        self.assertEqual(adb.actions, [])

    def test_completion_needs_goal_confirmation(self):
        result, _, _ = self.run_flow([stop([0, 1], done=False)])
        self.assertEqual(result["status"], "insufficient")

    def test_conflicting_stop_label_uses_explicit_goal_and_all_required_evidence(self):
        result, _, _ = self.run_flow([stop([0, 1], reason="insufficient", done=True)])
        self.assertEqual(result["status"], "completed")
        event = self.trace()[-1]
        self.assertEqual(event["params"]["reported_reason"], "insufficient")
        self.assertTrue(event["decision"]["stop_label_normalized"])

    def test_auth_stop_is_never_overridden_by_completion_answers(self):
        result, _, _ = self.run_flow([stop([0, 1], reason="need_user", done=True)])
        self.assertEqual(result["status"], "need_user")

    def test_insufficient_without_goal_confirmation_or_full_evidence_is_not_promoted(self):
        for decision in (stop([0, 1], reason="insufficient", done=False),
                         stop([0], reason="insufficient", done=True)):
            result, _, _ = self.run_flow([decision])
            self.assertEqual(result["status"], "insufficient")

    def test_no_progress_is_a_hint_not_a_hard_stop(self):
        self.cfg["no_progress_limit"] = 2
        self.cfg["evidence"] = []
        result, _, _ = self.run_flow([{"action": "wait"}] * 6 + [stop()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"], 7)

    def test_step_budget_preserves_after_screenshot(self):
        self.cfg["max_steps"] = 1
        result, _, adb = self.run_flow([{"action": "tap", "box": BOX}])
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(len(adb.actions), 1)
        self.assertEqual(self.trace()[0]["after_image"], "images/002.png")

    def test_model_error_and_ctrl_c_both_keep_result(self):
        for exception, status in ((CollectorError("mock API unavailable"), "error"),
                                  (KeyboardInterrupt(), "interrupted")):
            with self.subTest(status=status):
                result, _, _ = self.run_flow([exception])
                self.assertEqual(result["status"], status)
                self.assertTrue((self.recorder.root / "result.json").is_file())
                self.assertTrue(self.recorder.trace_path.is_file())
                self.assertEqual(result["image_count"], 1)

    def test_disconnected_device_before_first_screenshot(self):
        adb = FakeADB()
        adb.select_device = lambda: (_ for _ in ()).throw(NeedUser("offline"))
        result, model, _ = self.run_flow([], adb)
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(model.frames, [])

    def test_screen_animation_does_not_cancel_selected_tap(self):
        old, new = frame(), frame("DIALOG", "lightblue")
        class ChangingADB(FakeADB):
            count = 0
            def screenshot(self):
                self.count += 1
                return old if self.count == 1 else new
        result, _, adb = self.run_flow([{"action": "tap", "box": BOX}, stop(reason="insufficient")], ChangingADB())
        self.assertEqual(len(adb.actions), 1)
        self.assertEqual(self.trace()[0]["status"], "executed")
        self.assertNotEqual((self.recorder.root / "images/001.png").read_bytes(), old.png)
        self.assertEqual((self.recorder.root / "images/002.png").read_bytes(), new.png)
        self.assertEqual(result["status"], "insufficient")

    def test_password_keypad_setup_and_confirm_then_returns_to_jev(self):
        self.cfg["evidence"] = ["Mode"]
        password = Scripted([
            {"action": "enter_pin", "stage": "setup", "length": 4, "method": "keypad", "box": BOX},
            {"action": "enter_pin", "stage": "confirm", "length": 4, "method": "keypad", "box": BOX},
            {"action": "done"},
        ])
        result, model, adb = self.run_flow([{"action": "handle_password"}, stop([0])], password=password)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(adb.actions), 8)
        self.assertTrue(all(x[0] == "tap" for x in adb.actions))
        self.assertEqual(result["mode_pin"]["length"], 4)
        self.assertEqual(result["mode_pin"]["entered_stages"], ["confirm", "setup"])
        self.assertEqual(len(model.frames), 2)

    def test_unknown_existing_pin_never_entered(self):
        password = Scripted([{"action": "enter_pin", "stage": "unlock", "length": 4, "method": "keypad", "box": BOX}])
        result, _, adb = self.run_flow([{"action": "handle_password"}], password=password)
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(adb.actions, [])

    def test_password_length_mismatch_stops_before_confirmation(self):
        password = Scripted([
            {"action": "enter_pin", "stage": "setup", "length": 4, "method": "keypad", "box": BOX},
            {"action": "enter_pin", "stage": "confirm", "length": 6, "method": "keypad", "box": BOX},
        ])
        result, _, adb = self.run_flow([{"action": "handle_password"}], password=password)
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(len(adb.actions), 4)

    def test_no_repeated_pin_attempt(self):
        self.cfg["password"]["existing_pin_is_all_ones"] = True
        decision = {"action": "enter_pin", "stage": "unlock", "length": 4, "method": "keypad", "box": BOX}
        result, _, adb = self.run_flow([{"action": "handle_password"}], password=Scripted([decision, decision]))
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(len(adb.actions), 4)

    def test_text_pin_is_masked_and_not_exposed_in_trace(self):
        self.cfg["evidence"] = []
        password = Scripted([
            {"action": "enter_pin", "stage": "setup", "length": 6, "method": "text", "box": BOX},
            {"action": "done"},
        ])
        result, _, adb = self.run_flow([{"action": "handle_password"}, stop()], password=password)
        self.assertEqual(result["status"], "completed")
        self.assertIn(("enter_pin", 6), adb.actions)
        text = self.recorder.trace_path.read_text()
        self.assertNotIn("111111", text)
        self.assertIn("[REDACTED]", text)

    def test_partial_pin_failure_not_reported_as_fully_entered(self):
        class FailingPINADB(FakeADB):
            def tap(self, x, y):
                super().tap(x, y)
                raise NeedUser("mock interrupted PIN input")
        password = Scripted([{"action": "enter_pin", "stage": "setup", "length": 4,
                              "method": "keypad", "box": BOX}])
        result, _, adb = self.run_flow([{"action": "handle_password"}], FailingPINADB(), password)
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(result["mode_pin"]["attempted_stages"], ["setup"])
        self.assertEqual(result["mode_pin"]["entered_stages"], [])
        self.assertEqual(len(adb.actions), 1)

    def test_setup_authorization_can_be_disabled(self):
        self.cfg["password"]["allow_setup"] = False
        password = Scripted([{"action": "enter_pin", "stage": "setup", "length": 4, "method": "keypad", "box": BOX}])
        result, _, adb = self.run_flow([{"action": "handle_password"}], password=password)
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(adb.actions, [])

    def test_action_after_model_time_budget_expired_is_not_executed(self):
        tick = [0.0]
        class SlowModel:
            def decide(self, *args):
                tick[0] += 100
                return {"action": "tap", "box": BOX}
        adb = FakeADB()
        workflow = Workflow(self.cfg, adb, SlowModel(), None, self.recorder,
                            sleeper=lambda _: None, clock=lambda: tick[0])
        with contextlib.redirect_stdout(io.StringIO()):
            result = workflow.run()
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(adb.actions, [])

    def test_time_budget_expiring_after_action_preserves_image_without_another_action(self):
        tick = [0.0]
        class SlowSnapshotADB(FakeADB):
            calls = 0
            def screenshot(self):
                self.calls += 1
                if self.calls == 2:
                    tick[0] += 100
                return super().screenshot()
        adb = SlowSnapshotADB()
        model = Scripted([{"action": "tap", "box": BOX}])
        workflow = Workflow(self.cfg, adb, model, None, self.recorder,
                            sleeper=lambda _: None, clock=lambda: tick[0])
        with contextlib.redirect_stdout(io.StringIO()):
            result = workflow.run()
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(len(adb.actions), 1)
        self.assertEqual(result["image_count"], 2)

    def test_after_capture_failure_retains_attempt_and_is_not_completed(self):
        class DisconnectingADB(FakeADB):
            def screenshot(self):
                if self.actions:
                    raise NeedUser("mock disconnect")
                return super().screenshot()
        result, _, adb = self.run_flow([{"action": "tap", "box": BOX}], DisconnectingADB())
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(len(adb.actions), 1)
        event = self.trace()[0]
        self.assertEqual(event["status"], "executed_but_after_capture_missing")
        self.assertIsNone(event["after_image"])

    def test_confirming_unknown_pin_and_disabled_branch_are_not_executed(self):
        for decision in ({"action": "enter_pin", "stage": "confirm", "length": 4, "method": "keypad", "box": BOX},):
            result, _, adb = self.run_flow([{"action": "handle_password"}], password=Scripted([decision]))
            self.assertEqual(result["status"], "need_user")
            self.assertEqual(adb.actions, [])
        self.cfg["password"]["enabled"] = False
        result, _, adb = self.run_flow([{"action": "handle_password"}])
        self.assertEqual(result["status"], "need_user")
        self.assertEqual(adb.actions, [])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.image = frame()
        self.env = patch.dict(os.environ, {"TYPESAFE_API_KEY": "unit-test-only", "LLM_API_KEY": "unit-test-only",
            "LLM_BASE_URL": "https://gateway.example/v1", "LLM_MODEL": "test-model"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, action="tap"):
        return {"model": "jev-test", "answers": {
            "action": {"type": "choice", "choice": action, "confidence": .99},
            "execution_scope": {"type": "choice", "choice": "navigation", "confidence": .1},
            "tap_target": {"type": "box", "box": BOX, "confidence": .99},
        }}

    def test_jev_request_uses_image_asset_and_experimental_header(self):
        captured = {}
        def transport(url, key, payload, timeout, headers):
            captured.update(url=url, key=key, payload=payload, headers=headers)
            return self.response()
        client = JevClient(self.cfg["jev"], transport)
        result = client.decide(self.image, {"goal": "test", "requirements": []}, 10)
        payload = captured["payload"]
        reference = payload["state"]["screen"]
        upload = Image.open(io.BytesIO(base64.b64decode(payload["assets"][reference]["data_base64"])))
        self.assertEqual(upload.size, (280, 560))
        self.assertEqual(self.image.png, frame().png)
        self.assertEqual(captured["headers"]["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(result["action"], "tap")
        self.assertEqual(payload["questions"]["action"]["type"], "choice")
        self.assertEqual(payload["questions"]["tap_target"]["type"], "box")

    def test_back_does_not_need_box(self):
        response = self.response("back")
        del response["answers"]["tap_target"]
        result = JevClient(self.cfg["jev"]).parse(response, self.image, 0)
        self.assertEqual(result["action"], "back")
        self.assertNotIn("box", result)

    def test_legacy_presence_score_does_not_gate_a_selected_valid_tap(self):
        response = self.response()
        response["answers"]["tap_available"] = {"type": "noul", "noul": .1}
        result = JevClient(self.cfg["jev"]).parse(response, self.image, 0)
        self.assertEqual(result["action"], "tap")

    def test_low_or_zero_action_and_box_confidence_do_not_block(self):
        for name in ("action", "tap_target"):
            for score in (0, .14, .2):
                response = self.response()
                response["answers"][name]["confidence"] = score
                result = JevClient(self.cfg["jev"]).parse(response, self.image, 0)
                self.assertEqual(result["action"], "tap")
                self.assertEqual(result["box"], BOX)

    def test_low_scores_do_not_trigger_extra_localization_api_calls(self):
        initial = self.response()
        initial["answers"]["action"]["confidence"] = .1
        initial["answers"]["tap_target"]["confidence"] = .14
        calls = []
        def transport(*args):
            calls.append(args[2])
            return initial
        result = JevClient(self.cfg["jev"], transport).decide(self.image, {"requirements": []}, 10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["api_calls"], 1)
        self.assertNotIn("tap_available", calls[0]["questions"])
        self.assertNotIn("tap_intent", calls[0]["questions"])

    def test_optional_missing_or_invalid_confidence_does_not_block(self):
        for value in (None, float("nan"), True):
            response = self.response()
            response["answers"]["action"]["confidence"] = value
            response["answers"]["tap_target"]["confidence"] = value
            result = JevClient(self.cfg["jev"]).parse(response, self.image, 0)
            self.assertEqual(result["action"], "tap")
            self.assertIsNone(result["confidence"])

    def test_unsafe_action_rejected(self):
        response = self.response()
        response["answers"]["execution_scope"]["choice"] = "requires_user"
        with self.assertRaises(NeedUser):
            JevClient(self.cfg["jev"]).parse(response, self.image, 0)

    def test_selected_password_branch_does_not_require_a_confidence_cutoff(self):
        response = self.response("handle_password")
        response["answers"]["execution_scope"]["choice"] = "mode_password"
        response["answers"]["execution_scope"]["confidence"] = 0
        response["answers"]["action"]["confidence"] = 0
        result = JevClient(self.cfg["jev"]).parse(response, self.image, 0)
        self.assertEqual(result["action"], "handle_password")

    def test_collection_evidence_uses_explicit_choices_not_score_thresholds(self):
        response = self.response("stop")
        response["answers"].update({
            "stop_reason": {"type": "choice", "choice": "completed", "confidence": 0},
            "goal_done": {"type": "choice", "choice": "complete", "confidence": 0},
            "evidence_0": {"type": "choice", "choice": "present", "confidence": 0},
        })
        result = JevClient(self.cfg["jev"]).parse(response, self.image, 1)
        self.assertTrue(result["goal_done"])
        self.assertEqual(result["evidence"], [0])

    def test_malformed_bounds_nan_and_boolean_rejected(self):
        for value in ({**BOX, "left": -.1}, {**BOX, "left": .9},
                      {**BOX, "top": float("nan")}, {**BOX, "top": True}, {}):
            with self.subTest(box=value), self.assertRaises(CollectorError):
                box_pixels(value, 200, 400)

    def test_password_api_receives_original_png_and_only_whitelisted_output_survives(self):
        captured = {}
        def transport(url, key, payload, timeout):
            captured.update(url=url, payload=payload)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "action": "enter_pin", "mode_pin": True, "stage": "setup", "length": 4,
                "method": "keypad", "box": BOX, "reason": "not persisted"})}}]}
        result = PasswordClient(self.cfg["password"], transport).decide(self.image, {}, 10)
        self.assertEqual(captured["url"], "https://gateway.example/v1/chat/completions")
        image_part = captured["payload"]["messages"][1]["content"][1]
        self.assertEqual(base64.b64decode(image_part["image_url"]["url"].split(",")[1]), self.image.png)
        self.assertNotIn("reason", result)

    def test_password_free_text_shell_bad_length_or_other_credentials_rejected(self):
        for value in ({"action": "shell", "command": "anything"},
                      {"action": "input_text", "text": "anything"},
                      {"action": "enter_pin", "mode_pin": False},
                      {"action": "enter_pin", "mode_pin": True, "stage": "setup", "length": 8,
                       "method": "keypad", "box": BOX}):
            with self.subTest(value=value), self.assertRaises(CollectorError):
                PasswordClient.parse(value, self.image)

    def test_model_truncated_json_not_executed(self):
        transport = lambda *args: {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with self.assertRaises(CollectorError):
            PasswordClient(self.cfg["password"], transport).decide(self.image, {}, 10)


class ConfigAndDriverTests(unittest.TestCase):
    def test_env_loading_never_evaluates_shell_and_preserves_existing_variable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"EXISTING": "kept"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("EXISTING=replaced\nTEST_VALUE='$(not-executed)'\nEMPTY=\n", encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["EXISTING"], "kept")
            self.assertEqual(os.environ["TEST_VALUE"], "$(not-executed)")
            self.assertEqual(os.environ["EMPTY"], "")

    def test_config_relative_paths_resolved_against_config_location(self):
        cfg = config()
        self.assertEqual(cfg["output_dir"], str(CODE_ROOT.parent / "results/collections"))
        self.assertTrue(Path(cfg["adb_path"]).is_file())

    def test_url_credentials_and_remote_http_forbidden(self):
        for url in ("http://remote.example/v1", "https://user:pass@api.example", "https://api.example?key=abc"):
            with self.subTest(url=url), self.assertRaises(CollectorError):
                validate_url(url)
        validate_url("http://127.0.0.1:12345/test")

    def test_device_selection_requires_unique_authorized_device(self):
        driver = ADB(config()["adb_path"])
        with patch.object(driver, "_run", return_value=b"List of devices attached\na device\nb device\n"):
            with self.assertRaises(NeedUser):
                driver.select_device()
        with patch.object(driver, "_run", return_value=b"List of devices attached\na unauthorized\n"):
            with self.assertRaises(NeedUser):
                driver.select_device()
        with patch.object(driver, "_run", return_value=b"List of devices attached\na device\n"):
            self.assertEqual(driver.select_device(), "a")

    def test_pin_tool_accepts_only_fixed_length_policy(self):
        driver = ADB(config()["adb_path"], "fake")
        with patch.object(driver, "_run") as run:
            driver.enter_test_pin(4)
            self.assertEqual(run.call_args.args[0], ["shell", "input", "text", "1111"])
            with self.assertRaises(CollectorError):
                driver.enter_test_pin(5)

    def test_png_validation_and_difference(self):
        with self.assertRaises(CollectorError):
            Frame.from_png(b"not an image")
        a = frame()
        self.assertEqual(frame_difference(a, a), 0)
        self.assertGreater(frame_difference(a, frame(color="lightblue")), 5)

    def test_full_phone_resolution_resized_only_for_jev_upload(self):
        original = Image.new("RGBA", (1260, 2800), "white")
        buffer = io.BytesIO()
        original.save(buffer, format="PNG")
        source = Frame.from_png(buffer.getvalue())
        asset = source.asset()
        with Image.open(io.BytesIO(base64.b64decode(asset["data_base64"]))) as upload:
            self.assertEqual(upload.size, (706, 1568))
            self.assertEqual(upload.mode, "RGB")
        self.assertEqual(source.png, buffer.getvalue())
        self.assertEqual((source.width, source.height), (1260, 2800))

    def test_extreme_aspect_ratio_not_silently_padded_or_cropped(self):
        image = Image.new("RGB", (100, 2000), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        with self.assertRaises(CollectorError):
            Frame.from_png(buffer.getvalue()).asset()


class HttpTests(unittest.TestCase):
    def test_diagnostic_message_redacts_credentials(self):
        raw = json.dumps({"detail": "Unknown model: test-model; supplied unit-secret"}).encode()
        detail = http_error_detail(raw, "unit-secret")
        self.assertIn("Unknown model", detail)
        self.assertNotIn("unit-secret", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertEqual(http_error_detail(b'{"value":"not an error field"}', "key"), "")
        self.assertNotIn("AAAA", http_error_detail(b'{"detail":"bad image_url data:image/png;base64,AAAA"}', "key"))

    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                cls.calls.append((self.path, json.loads(body), self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                self.send_response(401 if self.path == "/unauthorized" else 200)
                self.end_headers()
                self.wfile.write(b'not JSON' if self.path == "/invalid" else b'{"value":"mock-response-secret"}')
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_http_body_and_authorization(self):
        result = post_json(self.base + "/ok", "fake-key", {"test": "中文"}, 2)
        self.assertEqual(result["value"], "mock-response-secret")
        self.assertEqual(self.calls[-1][1], {"test": "中文"})
        self.assertEqual(self.calls[-1][2], "Bearer fake-key")

    def test_errors_do_not_echo_keys_or_response_bodies_and_do_not_retry(self):
        before = len(self.calls)
        with self.assertRaises(CollectorError) as caught:
            post_json(self.base + "/unauthorized", "fake-key", {}, 2)
        self.assertNotIn("mock-response-secret", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertEqual(len(self.calls) - before, 1)

    def test_authenticated_post_not_redirected(self):
        before = len(self.calls)
        with self.assertRaises(CollectorError):
            post_json(self.base + "/redirect", "fake-key", {}, 2)
        self.assertEqual(len(self.calls) - before, 1)

    def test_invalid_response_json(self):
        with self.assertRaises(CollectorError):
            post_json(self.base + "/invalid", "fake-key", {}, 2)


if __name__ == "__main__":
    unittest.main()
