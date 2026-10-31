"""Small shared types, configuration and HTTP transport (no model SDK required)."""

import base64
import hashlib
import io
import json
import math
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageChops, ImageStat


CODE_ROOT = Path(__file__).resolve().parents[1]


class CollectorError(Exception):
    """An error whose message is safe to show without leaking API bodies or keys."""


class NeedUser(CollectorError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def finite_number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectorError("%s 必须是数字" % name)
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise CollectorError("%s 超出允许范围" % name)
    return number


def box_pixels(box: Dict[str, Any], width: int, height: int) -> Tuple[int, int, int, int]:
    if not isinstance(box, dict):
        raise CollectorError("缺少目标框")
    values = {k: finite_number(box.get(k), "box." + k, 0, 1)
              for k in ("left", "top", "right", "bottom")}
    if values["left"] >= values["right"] or values["top"] >= values["bottom"]:
        raise CollectorError("目标框为空或边界顺序错误")
    left = min(width - 1, int(values["left"] * width))
    top = min(height - 1, int(values["top"] * height))
    right = min(width - 1, max(left + 1, round(values["right"] * width) - 1))
    bottom = min(height - 1, max(top + 1, round(values["bottom"] * height) - 1))
    return left, top, right, bottom


@dataclass(frozen=True)
class Frame:
    png: bytes
    width: int
    height: int
    captured_at: str
    stable: bool = True

    @classmethod
    def from_png(cls, data: bytes, stable: bool = True) -> "Frame":
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or min(image.size) < 2:
                    raise ValueError()
                image.load()
                width, height = image.size
        except Exception as exc:
            raise CollectorError("截图不是有效的 PNG，可能未连接设备或页面禁止截图") from exc
        return cls(data, width, height, now(), stable)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def image(self) -> Image.Image:
        with Image.open(io.BytesIO(self.png)) as image:
            return image.convert("RGB")

    def asset(self) -> Dict[str, str]:
        """Unannotated Jev upload copy: each side 280..1568, same aspect ratio.

        Keep self.png intact for the recorder and physical phone coordinates.
        Do not pad/crop silently: that would invalidate normalized Box mapping.
        """
        minimum, maximum = 280, 1568
        ratio = max(self.width, self.height) / min(self.width, self.height)
        if ratio > maximum / minimum:
            raise CollectorError("截图过于狭长，无法等比满足 Jev 尺寸限制；未裁剪或扭曲坐标")
        scale = min(1.0, maximum / max(self.width, self.height))
        scale = max(scale, minimum / min(self.width, self.height))
        size = (round(self.width * scale), round(self.height * scale))
        image = self.image()
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {"type": "image", "format": "png",
                "data_base64": base64.b64encode(buffer.getvalue()).decode("ascii")}


def frame_difference(a: Frame, b: Frame) -> float:
    """Mean RGB difference, ignoring status/navigation bars; not a semantic comparator."""
    if (a.width, a.height) != (b.width, b.height):
        return 255.0
    def thumb(frame: Frame) -> Image.Image:
        return frame.image().crop((0, int(frame.height * .06), frame.width,
                                   int(frame.height * .94))).resize((96, 160))
    return sum(ImageStat.Stat(ImageChops.difference(thumb(a), thumb(b))).mean) / 3


def load_env(path: Path) -> None:
    """Read KEY=value without executing shell, expanding variables, or overriding env."""
    if not path.exists():
        return
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise CollectorError(".env 第 %d 行格式错误" % line_no)
        try:
            parts = shlex.split(value, comments=True)
        except ValueError as exc:
            raise CollectorError(".env 第 %d 行引号不匹配" % line_no) from exc
        if len(parts) > 1:
            raise CollectorError(".env 第 %d 行含空格的值需要引号" % line_no)
        os.environ.setdefault(key.strip(), parts[0] if parts else "")


def load_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectorError("无法读取配置 JSON") from exc
    if not isinstance(config, dict):
        raise CollectorError("配置必须是 JSON 对象")
    for key in ("package", "goal", "evidence", "max_steps", "max_seconds", "jev", "password"):
        if key not in config:
            raise CollectorError("配置缺少 %s" % key)
    if not isinstance(config["package"], str) or not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", config["package"]):
        raise CollectorError("App 包名格式不正确")
    if not isinstance(config["goal"], str) or not config["goal"].strip():
        raise CollectorError("采集目标不能为空")
    if not isinstance(config["evidence"], list) or not all(isinstance(x, str) and x.strip() for x in config["evidence"]):
        raise CollectorError("evidence 应是非空文字组成的列表，也可为空列表")
    if len(config["evidence"]) > 20:
        raise CollectorError("轻量采集流程最多支持 20 项采集要求")
    for key, low, high in (("max_steps", 1, 500), ("max_seconds", 1, 7200),
                           ("history_limit", 1, 100),
                           ("stability_checks", 0, 5)):
        value = config.get(key, {"history_limit": 10, "stability_checks": 2}.get(key))
        finite_number(value, key, low, high)
        if not isinstance(value, int):
            raise CollectorError(key + " 必须是整数")
        config[key] = value
    for key, default in (("settle_seconds", .8), ("wait_seconds", 2),
                         ("stability_threshold", 2)):
        config[key] = finite_number(config.get(key, default), key, 0, 30)
    for key in ("jev", "password"):
        if not isinstance(config[key], dict):
            raise CollectorError(key + " 必须是对象")
    # Retired scores/settings must not silently reactivate execution gates in old configs.
    for key in ("min_choice_confidence", "min_box_confidence", "min_yes", "refine_ambiguous_tap"):
        config["jev"].pop(key, None)
    for key in ("verify_before_tap", "stale_threshold", "no_progress_limit"):
        config.pop(key, None)
    for key in ("enabled", "allow_setup", "existing_pin_is_all_ones"):
        value = config["password"].get(key, key != "existing_pin_is_all_ones")
        if not isinstance(value, bool):
            raise CollectorError("password." + key + " 必须是布尔值")
        config["password"][key] = value
    rounds = config["password"].get("max_rounds", 8)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 30:
        raise CollectorError("password.max_rounds 应在 1 到 30 之间")
    config["password"]["max_rounds"] = rounds
    for section in ("jev", "password"):
        config[section]["timeout"] = finite_number(config[section].get("timeout", 45), "timeout", 1, 60)
    recovery = config.setdefault("recovery", {})
    if not isinstance(recovery, dict):
        raise CollectorError("recovery 必须是对象")
    recovery.setdefault("enabled", True)
    if type(recovery["enabled"]) is not bool:
        raise CollectorError("recovery.enabled 必须为布尔值")
    for key, default, low, high in (("repeat_actions", 3, 2, 10), ("max_actions", 3, 1, 10), ("max_recoveries", 3, 1, 10)):
        value = recovery.setdefault(key, default)
        if type(value) is not int:
            raise CollectorError("recovery." + key + " 必须为整数")
        finite_number(value, "recovery." + key, low, high)
    recovery["timeout"] = finite_number(recovery.get("timeout", 45), "recovery.timeout", 1, 60)
    for key, default in (("adb_path", "../tools/platform-tools/adb"),
                         ("output_dir", "../results/collections")):
        value = config.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise CollectorError(key + " 必须是路径")
        config[key] = str((path.parent / value).resolve())
    return config


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CollectorError("API 地址不能携带密码、查询参数或片段")
    if not parsed.hostname or (parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"))):
        raise CollectorError("API 必须使用 HTTPS；仅本机测试地址允许 HTTP")


def http_error_detail(raw: bytes, key: str) -> str:
    """Expose only a short diagnostic field, never arbitrary response/request bodies."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(body, dict):
        return ""
    value = body.get("error", body.get("detail", body.get("message")))
    if isinstance(value, dict):
        value = value.get("message", value.get("detail", value.get("code")))
    if not isinstance(value, str):
        return ""
    for secret in (key, os.environ.get("TYPESAFE_API_KEY"), os.environ.get("LLM_API_KEY")):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    if any(word in value for word in ("data_base64", "image_url", "data:image/")):
        return "错误详情包含图片请求字段，已省略"
    value = re.sub(r"[A-Za-z0-9+/=_-]{48,}", "[LONG_VALUE_REDACTED]", value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return value[:600]


def post_json(url: str, key: str, payload: Dict[str, Any], timeout: float,
              extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    validate_url(url)
    if not key or "\n" in key or "\r" in key:
        raise CollectorError("API Key 未配置或格式错误")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
    # Never follow a redirect with a credential-bearing POST. No automatic paid retries.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, new_url):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise CollectorError("模型响应过大")
        result = json.loads(data)
    except urllib.error.HTTPError as exc:
        # Do not log arbitrary bodies. Only retain short, explicitly redacted diagnostics.
        status = exc.code
        try:
            detail = http_error_detail(exc.read(8192), key)
        except OSError:
            detail = ""
        exc.close()
        message = "模型接口 HTTP %d；请检查模型名、额度和接口配置" % status
        raise CollectorError(message + ("；" + detail if detail else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CollectorError("模型接口连接失败或超时；未自动重试") from None
    except (ValueError, UnicodeError):
        raise CollectorError("模型接口未返回有效 JSON") from None
    if not isinstance(result, dict):
        raise CollectorError("模型响应必须是 JSON 对象")
    return result
