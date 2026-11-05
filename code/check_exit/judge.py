"""One final DeepSeek judgement over raw images and real device events."""

import base64
import hashlib
import io
import json
import os
import re

from PIL import Image

from collect_images.common import CollectorError, post_json, validate_url
from .prompts import JUDGE, SCENARIOS


def text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CollectorError("判定模型文本字段为空或格式错误")
    return value.strip()


def validate_result(value, bundle):
    """Check integrity/coverage, not image semantics; no second judging agent."""
    if not isinstance(value, dict) or value.get("verdict") not in ("bypass", "not_observed", "unknown"):
        raise CollectorError("判定结果类别无效")
    rows = value.get("findings")
    if not isinstance(rows, list) or len(rows) != len(SCENARIOS):
        raise CollectorError("判定结果必须包含三个场景，未执行场景也要明确unknown")
    scenes = {s["id"]: s for s in bundle["scenarios"]}
    images = {i["id"]: i for i in bundle["images"]}
    events = {e["id"]: e for e in bundle["events"]}
    seen, findings = set(), []
    for row in rows:
        if not isinstance(row, dict) or row.get("scenario_id") not in scenes or row["scenario_id"] in seen:
            raise CollectorError("未知或重复场景编号")
        identity = row["scenario_id"]
        seen.add(identity)
        scene = scenes[identity]
        verdict = row.get("verdict")
        allowed = ("control_passed", "control_failed", "unknown") if identity == "S3" else ("bypass", "not_observed", "unknown")
        if verdict not in allowed:
            raise CollectorError("场景结论类别不匹配")
        allowed_events = set(scene.get("event_ids", []))
        allowed_images = {scene.get("before_image"), scene.get("after_image")}
        for eid in allowed_events:
            if eid in events:
                allowed_images.update((events[eid].get("before_image"), events[eid].get("after_image")))
        # A skipped scene has no own execution. An unknown explanation may cite
        # preparation evidence for why it could not start, never as a pass proof.
        if verdict == "unknown" and scene["status"] == "skipped":
            preparation = bundle["preparation"]
            allowed_images.update((preparation.get("before_image"), preparation.get("after_image")))
            for eid in preparation.get("event_ids", []):
                if eid in events:
                    allowed_images.update((events[eid].get("before_image"), events[eid].get("after_image")))
                    allowed_events.add(eid)
        refs = {}
        for key, registry, scope in (("image_ids", images, allowed_images), ("event_ids", events, allowed_events)):
            ids = row.get(key)
            if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or i not in registry or i not in scope for i in ids):
                raise CollectorError("证据引用不存在或不属于本场景：" + key)
            refs[key] = list(dict.fromkeys(ids))
        if verdict != "unknown":
            if bundle["preparation"]["status"] != "collected" or scene["status"] != "collected":
                raise CollectorError("未完成的场景不能给出确定结论")
            actual = [events[e] for e in allowed_events if e in events and events[e]["status"] == "executed"]
            needed = "restart_app" if identity == "S2" else "tap"
            if not any(e["action"] == needed and (needed != "tap" or e.get("params", {}).get("purpose") == "exit_entry") for e in actual):
                raise CollectorError("缺少本场景必须实际执行的动作")
            if len(refs["image_ids"]) < 2 or not {scene.get("before_image"), scene.get("after_image")} <= set(refs["image_ids"]) or not refs["event_ids"]:
                raise CollectorError("确定结论须引用前后图片和操作证据")
            if not any(events[e]["status"] == "executed" and events[e]["action"] == needed and
                       (needed != "tap" or events[e].get("params", {}).get("purpose") == "exit_entry") for e in refs["event_ids"]):
                raise CollectorError("必须引用实际退出尝试或重启事件，不能仅为观察/返回记录")
            if identity == "S3" and not any(e["action"] in ("enter_pin", "pin_digit") for e in actual):
                raise CollectorError("正确密码对照未实际输入密码")
            if identity == "S3" and not any(events[e]["action"] in ("enter_pin", "pin_digit") and events[e]["status"] == "executed" for e in refs["event_ids"]):
                raise CollectorError("正确密码对照必须引用实际输入记录")
            if identity != "S3" and any(e["action"] in ("enter_pin", "pin_digit", "password_tap") for e in actual):
                raise CollectorError("负向场景发生密码动作，不能视为无验证测试")
        findings.append({"scenario_id": identity, "verdict": verdict, "reason": text(row.get("reason")), **refs})
    by_id = {r["scenario_id"]: r["verdict"] for r in findings}
    if value["verdict"] == "bypass" and not any(by_id[s] == "bypass" for s in ("S1", "S2")):
        raise CollectorError("总体发现绕过却没有对应负向场景证据")
    if value["verdict"] == "not_observed" and (by_id["S1"] != "not_observed" or by_id["S2"] != "not_observed" or by_id["S3"] != "control_passed"):
        raise CollectorError("证据不完整或对照未成功，不能汇总为未发现绕过")
    limitations = value.get("limitations")
    if not isinstance(limitations, list) or len(limitations) > 8:
        raise CollectorError("局限列表格式错误")
    return {"verdict": value["verdict"], "summary": text(value.get("summary")),
            "findings": sorted(findings, key=lambda f: f["scenario_id"]),
            "limitations": [text(s, 1200) for s in limitations]}


def reference_index(bundle):
    """Actual operation anchors, not model verdicts or inferred UI semantics."""
    events = {e["id"]: e for e in bundle["events"]}
    result = {}
    for scene in bundle["scenarios"]:
        actual = [events[e] for e in scene.get("event_ids", []) if e in events and events[e]["status"] == "executed"]
        required = [e["id"] for e in actual if e["action"] == "restart_app"] if scene["id"] == "S2" else [
            e["id"] for e in actual if e["action"] == "tap" and e.get("params", {}).get("purpose") == "exit_entry"]
        result[scene["id"]] = {"status": scene["status"],
            "required_before_after_images": list(dict.fromkeys(i for i in (scene.get("before_image"), scene.get("after_image")) if i)),
            "must_cite_one_actual_entry_or_restart_event": required,
            "must_cite_one_pin_input_event_for_S3": [e["id"] for e in actual if e["action"] in ("enter_pin", "pin_digit")] if scene["id"] == "S3" else []}
    return result


class Judge:
    def __init__(self, config, transport=post_json):
        self.config, self.transport = config, transport
        self.stats = {}

    def judge(self, bundle, root):
        base = os.environ.get("CHECK_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        key = os.environ.get("CHECK_LLM_API_KEY", os.environ.get("LLM_API_KEY", ""))
        model = os.environ.get("CHECK_LLM_MODEL", os.environ.get("LLM_MODEL", ""))
        if not all((base, key, model)):
            raise CollectorError("请在现有.env配置DeepSeek图片模型接口")
        validate_url(base)
        images = bundle["images"]
        if not images or len(images) > self.config["max_images"]:
            raise CollectorError("最终图片为空或超过预算；未静默丢弃图片")
        index = reference_index(bundle)
        (root / "reference_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        content = [{"type": "text", "text": json.dumps(dict(bundle, reference_index=index), ensure_ascii=False)}]
        total = 0
        for item in images:
            if not re.fullmatch(r"images/\d{3}\.png", item["file"]):
                raise CollectorError("证据图片路径无效，不读取目录外文件或标注副本")
            path = root / item["file"]
            if path.is_symlink() or path.resolve().parent != root.resolve() / "images":
                raise CollectorError("证据图片路径越界")
            if path.stat().st_size > 20 * 1024 * 1024:
                raise CollectorError("单张证据图片过大")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise CollectorError("证据图片已被修改，停止判定")
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 20000000:
                    raise CollectorError("证据图片像素过多")
                image = image.convert("RGB")
                image.thumbnail((2800, 2800))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
            total += len(output.getvalue())
            if total > 24 * 1024 * 1024:
                raise CollectorError("最终图片总量超过24MiB；未静默截取部分场景")
            content.extend(({"type": "text", "text": item["id"] + "，阶段=" + item["phase"]},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode(), "detail": "high"}}))
        payload = {"model": model, "messages": [{"role": "system", "content": JUDGE}, {"role": "user", "content": content}],
                   "response_format": {"type": "json_object"}, "thinking": {"type": self.config["thinking"]}}
        if self.config["thinking"] == "enabled":
            payload["reasoning_effort"] = self.config["reasoning_effort"]
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 44 * 1024 * 1024:
            raise CollectorError("最终请求超过44MiB；未丢弃图片或历史")
        response = self.transport(base + "/chat/completions", key, payload, self.config["timeout"])
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") != "stop" or message.get("role") != "assistant":
                raise ValueError()
            value = json.loads(message["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise CollectorError("最终模型返回不完整或不是JSON；没有自动重试") from None
        usage = response.get("usage", {})
        self.stats = {"model": str(response.get("model", model))[:120], "calls": 1,
                      "thinking": self.config["thinking"], "reasoning_effort": self.config["reasoning_effort"],
                      "reasoning_returned": bool(message.get("reasoning_content")),
                      "usage": {k: v for k, v in usage.items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0} if isinstance(usage, dict) else {}}
        # Keep the concise answer for diagnosing rejected citations/coverage;
        # never save raw response, reasoning, request images, or credentials.
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
        if len(rendered.encode()) <= 65536:
            (root / "judge_response.json").write_text(rendered, encoding="utf-8")
        # Raw reasoning, API keys, and image base64 are never persisted.
        return validate_result(value, bundle)
