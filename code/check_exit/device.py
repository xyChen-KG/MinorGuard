"""Fixed device primitives, with phase-local PIN capabilities; no model shell."""

import re

from collect_images.common import CollectorError, NeedUser


class Device:
    def __init__(self, backend, package, password_config):
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", package):
            raise CollectorError("被测App包名无效")
        self.backend, self.package, self.password_config = backend, package, password_config
        self.phase = None
        self.known_pin = password_config["existing_pin_is_all_ones"]
        self.attempted = set()
        self.entered = set()
        self.pin_length = None
        self.text_sent = set()

    def set_phase(self, phase):
        if phase not in ("prepare", "no_auth", "restart", "probe", "positive"):
            raise CollectorError("未知测试阶段")
        self.phase = phase

    def authorize_password(self, kind):
        if self.phase not in ("prepare", "positive"):
            raise CollectorError("负向阶段禁止密码工具和密码输入")
        if self.phase == "prepare":
            if kind not in ("setup", "confirm") or not self.password_config["allow_setup"]:
                raise NeedUser("准备阶段仅允许授权设置新模式密码；不自动解锁已有密码")
            if kind == "confirm" and "setup" not in self.entered:
                raise NeedUser("本轮未设置密码，不自动确认")
        elif kind != "unlock" or not self.known_pin:
            raise NeedUser("退出对照需要已知全1模式密码；不猜测已有密码")

    def reserve_pin(self, kind, length):
        self.authorize_password(kind)
        if type(length) is not int or length not in (4, 6):
            raise CollectorError("模式测试密码仅支持4或6位数字1")
        if self.pin_length is not None and self.pin_length != length:
            raise NeedUser("密码长度前后不一致，未输入")
        if kind in self.attempted:
            raise NeedUser("同一密码阶段已经尝试，禁止自动重试")
        self.attempted.add(kind)  # Reserve BEFORE touching the device, even on partial failure.
        self.pin_length = length

    def pin_text(self, kind, length):
        self.authorize_password(kind)
        if kind not in self.attempted or length != self.pin_length or kind in self.text_sent:
            raise CollectorError("密码输入未预先授权")
        self.text_sent.add(kind)
        self.backend.enter_test_pin(length)

    def password_tap(self, kind, x, y):
        self.authorize_password(kind)
        self.backend.tap(x, y)

    def note_mode_enabled(self):
        # Sent input is not acceptance: only promote after collecting an explicit on page.
        # Some Apps have one setup field and no confirmation screen.
        if "setup" in self.entered and ("confirm" not in self.attempted or "confirm" in self.entered):
            self.known_pin = True

    def restart(self):
        if self.phase != "restart":
            raise CollectorError("只允许程序在重启场景重启当前App")
        self.backend._run(["shell", "am", "force-stop", self.package])
        self.backend.launch(self.package)

    def window_info(self):
        """Read focus and OS navigation-bar bounds without changing the phone."""
        raw = self.backend._run(["shell", "dumpsys", "window", "displays"])
        data = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        focus = re.search(r"mCurrentFocus=Window\{([^\n}]*)", data)
        package = None
        if focus:
            match = re.search(r"(?:^|\s)([A-Za-z][\w]*(?:\.[\w]+)+)[/:]", focus[1])
            if match:
                package = match[1]
        bars = []
        for match in re.finditer(r"type=navigationBars frame=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", data):
            box = tuple(map(int, match.groups()))
            if box[2] > box[0] and box[3] > box[1] and box not in bars:
                bars.append(box)
        return {"package": package, "navigation_bars": bars}

    def visible_keyboard(self, secure_only=False):
        """Check the live IME window, without changing any keyboard settings."""
        raw = self.backend._run(["shell", "dumpsys", "window", "windows"])
        data = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        for block in re.split(r"\n\s*Window #\d+", data):
            header = block.splitlines()[0] if block.splitlines() else ""
            if not ("type=2011" in header or "ty=INPUT_METHOD " in block):
                continue
            if ((not secure_only or re.search(r"\bSECURE\b", block)) and "isOnScreen=true" in block
                    and "isVisible=true" in block and "mHasSurface=true" in block):
                return True
        return False

    def visible_secure_keyboard(self):
        return self.visible_keyboard(secure_only=True)
