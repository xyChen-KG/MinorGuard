"""Readability recovery must not disable security or duplicate PIN input."""

import contextlib
import io
from unittest.mock import patch

from PIL import Image, ImageDraw

from collect_images.common import Frame, NeedUser
from check_exit.tests.test_exit_guard import TestBase, frame, pin


def obscured_frame():
    image = Image.new("RGB", (300, 600), "black")
    ImageDraw.Draw(image).rectangle((0, 0, 299, 24), fill="white")
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    return Frame.from_png(raw.getvalue())


class CaptureRecoveryTests(TestBase):
    def test_normal_keyboard_is_closed_after_pin_to_reveal_submit_not_other_fields(self):
        c = self.collector([], [pin("setup"), {"action": "done"}])
        self.device.set_phase("prepare")
        c._enter_stage("enable_mode")
        c.capture(False)
        visible = lambda *args, **kwargs: bool(self.backend.actions and self.backend.actions[-1][0] == "pin")
        with patch.object(self.device, "visible_keyboard", side_effect=visible):
            with contextlib.redirect_stdout(io.StringIO()):
                c._password()
        self.assertEqual([a[0] for a in self.backend.actions], ["tap", "pin", "back"])
        self.assertEqual(c.events[-2]["action"], "hide_password_keyboard")
        self.assertEqual(c.events[-2]["status"], "executed")
        self.assertFalse(self.device.known_pin)

    def test_secure_keyboard_must_be_live_ime_not_an_app_or_hidden_window(self):
        window = ("\n  Window #1 Window{abc u0 SecureInputMethod type=2011 }:\n"
                  "    fl=NOT_FOCUSABLE SECURE\n"
                  "    mHasSurface=true isOnScreen=true isVisible=true\n")
        for content, expected in (
                (window, True),
                (window.replace("isVisible=true", "isVisible=false"), False),
                (window.replace("mHasSurface=true", "mHasSurface=false"), False),
                (window.replace("type=2011", "type=1"), False),
                (window.replace(" SECURE", ""), False),
                ("\n  Window #0 Window{app type=1}:\n fl=SECURE\n" + window.replace(" SECURE", ""), False)):
            with self.subTest(expected=expected, content=content):
                with patch.object(self.backend, "_run", return_value=content.encode()):
                    self.assertEqual(self.device.visible_secure_keyboard(), expected)

    def test_black_secure_keyboard_is_dismissed_with_both_frames_recorded(self):
        c = self.collector([])
        self.device.set_phase("no_auth")
        with patch.object(self.backend, "screenshot", return_value=obscured_frame()):
            c.capture(False)
        with patch.object(self.device, "visible_secure_keyboard", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(c._uncover_secure_keyboard())
        self.assertEqual(self.backend.actions, [("back",)])
        self.assertEqual(c.events[-1]["action"], "hide_secure_keyboard")
        self.assertEqual((c.events[-1]["before_image"], c.events[-1]["after_image"]), ("I001", "I002"))
        self.assertEqual(c.password.calls, [])
        self.assertFalse(self.device.attempted)

    def test_no_blind_back_for_other_black_content_or_another_foreground_app(self):
        c = self.collector([])
        self.device.set_phase("prepare")
        with patch.object(self.backend, "screenshot", return_value=obscured_frame()):
            c.capture(False)
        with patch.object(self.device, "visible_secure_keyboard", return_value=False):
            self.assertFalse(c._uncover_secure_keyboard())
        with patch.object(self.device, "visible_secure_keyboard", return_value=True), patch.object(self.device, "window_info", return_value={"package": "com.other.app"}):
            with self.assertRaises(NeedUser):
                c._uncover_secure_keyboard()
        self.assertEqual(self.backend.actions, [])

    def test_still_obscured_after_one_back_stops_before_input(self):
        c = self.collector([])
        self.device.set_phase("prepare")
        with patch.object(self.backend, "screenshot", return_value=obscured_frame()), patch.object(self.device, "visible_secure_keyboard", return_value=True):
            c.capture(False)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(NeedUser):
                c._uncover_secure_keyboard()
        self.assertEqual(self.backend.actions, [("back",)])
        self.assertEqual(c.events[-1]["status"], "executed")

    def test_text_pin_focus_can_hide_keyboard_then_send_only_one_pin(self):
        c = self.collector([], [pin("setup"), {"action": "done"}])
        self.device.set_phase("prepare")
        c._enter_stage("enable_mode")
        c.capture(False)

        def screenshot():
            return obscured_frame() if self.backend.actions and self.backend.actions[-1][0] == "tap" else frame(1)

        with patch.object(self.backend, "screenshot", side_effect=screenshot), patch.object(self.device, "visible_secure_keyboard", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                c._password()
        self.assertEqual([action[0] for action in self.backend.actions], ["tap", "back", "pin"])
        self.assertEqual([action for action in self.backend.actions if action[0] == "pin"], [("pin", 4)])
        self.assertEqual(self.device.entered, {"setup"})
        self.assertFalse(self.device.known_pin)
