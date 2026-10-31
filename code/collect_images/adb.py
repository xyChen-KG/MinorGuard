"""Only allow explicit ADB operations. No shell command supplied by a model."""

import re
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from .common import CollectorError, Frame, NeedUser


class ADB:
    def __init__(self, path: str, serial: str = "", timeout: float = 15):
        self.path = str(Path(path).resolve())
        self.serial = serial
        self.timeout = timeout
        if not Path(self.path).is_file():
            raise CollectorError("找不到 ADB，请检查 config.json 的 adb_path")

    def _run(self, args: List[str], device: bool = True, timeout=None) -> bytes:
        command = [self.path]
        if device:
            if not self.serial:
                raise NeedUser("尚未选择手机")
            command += ["-s", self.serial]
        try:
            result = subprocess.run(command + args, capture_output=True, timeout=self.timeout if timeout is None else timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise NeedUser("ADB 执行失败或超时，请检查设备连接") from None
        if result.returncode:
            # input text commands may echo a PIN; never include command/stdout/stderr.
            raise NeedUser("ADB 操作失败，请检查设备是否在线、已解锁并授权调试")
        return result.stdout

    def select_device(self) -> str:
        rows = self._run(["devices", "-l"], device=False).decode("utf-8", errors="replace").splitlines()
        devices = [row.split()[0] for row in rows if len(row.split()) >= 2 and row.split()[1] == "device"]
        if self.serial:
            if self.serial not in devices:
                raise NeedUser("指定设备不在线或未授权；不会自动改连其他设备")
        elif len(devices) == 1:
            self.serial = devices[0]
        else:
            raise NeedUser("没有唯一可用设备；请连接手机，或用 --serial 明确指定")
        return self.serial

    def screenshot(self) -> Frame:
        return Frame.from_png(self._run(["exec-out", "screencap", "-p"]))

    def wake(self) -> None:
        """Wake only; never unlock a device PIN or alter its screen timeout."""
        self._run(["shell", "input", "keyevent", "224"])

    def apk_paths(self, package):
        if not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", package):
            raise CollectorError("App 包名无效")
        data = self._run(["shell", "pm", "path", package]).decode("utf-8", errors="replace")
        paths = [line[8:].strip() for line in data.splitlines() if line.startswith("package:")]
        if not paths or any(not path.startswith("/") or not path.endswith(".apk") or "\n" in path for path in paths):
            raise NeedUser("没有读到该 App 的已安装 APK 路径")
        if len(paths) != 1:
            raise CollectorError("该 App 是 split APK；当前分析器只支持完整 base APK，不能漏掉其他代码分包后声称完整核对")
        return paths

    def apk_hash(self, path):
        data = self._run(["shell", "sha256sum", shlex.quote(path)], timeout=60).decode("utf-8", errors="replace")
        digest = data.split()[0] if data.split() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CollectorError("无法校验手机 APK 的哈希，不默认认为与本地文件相同")
        return digest

    def pull_apk(self, source, destination):
        if Path(destination).exists():
            raise CollectorError("提取目标已存在，未覆盖文件")
        self._run(["pull", source, str(destination)], timeout=180)

    def tap(self, x: int, y: int) -> None:
        if not isinstance(x, int) or not isinstance(y, int) or min(x, y) < 0:
            raise CollectorError("点击坐标无效")
        self._run(["shell", "input", "tap", str(x), str(y)])

    def swipe(self, width: int, height: int, direction: str, length: str) -> None:
        if direction not in ("up", "down", "left", "right") or length not in ("short", "long"):
            raise CollectorError("滑动参数不在允许列表内")
        distance = .25 if length == "short" else .5
        x1 = x2 = .5
        y1 = y2 = .5
        if direction in ("up", "down"):
            sign = 1 if direction == "up" else -1
            y1, y2 = .5 + sign * distance / 2, .5 - sign * distance / 2
        else:
            sign = 1 if direction == "left" else -1
            x1, x2 = .5 + sign * distance / 2, .5 - sign * distance / 2
        self._run(["shell", "input", "swipe", str(int(width * x1)), str(int(height * y1)),
                   str(int(width * x2)), str(int(height * y2)), "400"])

    def back(self) -> None:
        self._run(["shell", "input", "keyevent", "4"])

    def enter_test_pin(self, length: int) -> None:
        if type(length) is not int or length not in (4, 6):
            raise CollectorError("测试模式密码仅支持 4 或 6 位数字 1")
        self._run(["shell", "input", "text", "1" * length])

    def launch(self, package: str) -> None:
        if not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", package):
            raise CollectorError("App 包名无效")
        self._run(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])

    def app_version(self, package: str) -> Optional[str]:
        try:
            data = self._run(["shell", "dumpsys", "package", package]).decode("utf-8", errors="replace")
        except CollectorError:
            return None
        match = re.search(r"\bversionName=([^\s]+)", data)
        return match.group(1)[:100] if match else None
