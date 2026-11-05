"""Temporary visual navigation; shares the collector's phase and device guards."""

import base64
import json
import os

from collect_images.common import CollectorError, box_pixels, post_json, validate_url
from .prompts import RECOVERY


class RecoveryNavigator:
    def __init__(self, config, transport=post_json):
        self.config, self.transport = config, transport

    def decide(self, frame, context, timeout):
        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        key, model = os.environ.get("LLM_API_KEY", ""), os.environ.get("LLM_MODEL", "")
        if not all((base, key, model)):
            raise CollectorError("脱困需要已有LLM_*接口配置")
        validate_url(base)
        state = {k: v for k, v in context.items() if k != "recent_frames"}
        content = [{"type": "text", "text": json.dumps(state, ensure_ascii=False)}]
        frames = list(context.get("recent_frames", []))[-3:]
        if not frames or frames[-1].sha256 != frame.sha256:
            frames = (frames + [frame])[-3:]
        for index, image in enumerate(frames, 1):
            content += [{"type": "text", "text": "最近原始截图%d%s" % (index, "（当前）" if index == len(frames) else "")},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image.png).decode("ascii"), "detail": "high"}}]
        payload = {"model": model, "messages": [{"role": "system", "content": RECOVERY}, {"role": "user", "content": content}],
                   "response_format": {"type": "json_object"}, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        response = self.transport(base + "/chat/completions", key, payload, min(timeout, self.config["timeout"]))
        try:
            item = response["choices"][0]
            if item.get("finish_reason") != "stop" or item["message"].get("role") != "assistant":
                raise ValueError()
            value = json.loads(item["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise CollectorError("脱困模型未返回完整JSON；未执行动作") from None
        result = self.parse(value, frame, context.get("after_action") is True)
        result["model"] = str(response.get("model", model))[:120]
        usage = response.get("usage", {})
        result["usage"] = {k: v for k, v in usage.items() if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                           and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
        return result

    @staticmethod
    def parse(value, frame, after_action):
        if not isinstance(value, dict):
            raise CollectorError("脱困决策必须为对象")
        fields = {"action": ("tap", "swipe", "back", "wait", "handle_password", "hand_back", "stop"),
                  "scope": ("navigation", "mode_password", "requires_user"),
                  "view": ("mode_status", "mode_intro", "settings", "mode_password", "pin_error", "identity", "locked", "other"),
                  "mode_state": ("on", "off", "unknown")}
        result = {}
        for name, allowed in fields.items():
            if value.get(name) not in allowed:
                raise CollectorError("脱困字段无效：" + name)
            result[name] = value[name]
        for name in ("reason", "hint"):
            text = value.get(name, "")
            if not isinstance(text, str) or len(text) > 1200:
                raise CollectorError("脱困说明无效")
            result[name] = text.strip()
        action = result["action"]
        if action == "tap":
            if value.get("tap_purpose") not in ("navigation", "enable_mode", "exit_entry"):
                raise CollectorError("脱困点击缺少作用说明")
            box_pixels(value.get("box"), frame.width, frame.height)
            result.update(box={k: value["box"][k] for k in ("left", "top", "right", "bottom")}, tap_purpose=value["tap_purpose"])
            target = value.get("target", "")
            if not isinstance(target, str) or len(target) > 1200:
                raise CollectorError("脱困点击目标描述无效")
            result["target"] = target.strip()
        elif action == "swipe":
            if value.get("direction") not in ("up", "down", "left", "right") or value.get("length") not in ("short", "long"):
                raise CollectorError("脱困滑动参数无效")
            result.update(direction=value["direction"], length=value["length"])
        elif action == "hand_back":
            if not after_action or value.get("progress_confirmed") is not True or not result["reason"] or not result["hint"]:
                raise CollectorError("缺少操作后的有效进展确认，不能交回Jev")
            if result["scope"] != "navigation" or result["view"] not in ("mode_status", "mode_intro", "settings", "other"):
                raise CollectorError("验证页不能作为脱困成功交回")
            result["progress_confirmed"] = True
        elif action == "stop":
            if value.get("stop_reason") not in ("insufficient", "need_user"):
                raise CollectorError("脱困停止原因无效")
            result["stop_reason"] = value["stop_reason"]
        return result
