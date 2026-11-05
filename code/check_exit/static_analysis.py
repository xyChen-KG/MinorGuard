"""APK-backed bypass candidates and explicitly selected on-device probes."""

from contextlib import ExitStack
import json
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from check_limits.__main__ import verify_identity
from check_limits.agent import BudgetEnded, ChatClient, LimitsAgent, clean_text, parse_limitations, validate_code_references
from check_limits.server import ensure_server
from check_limits.tools import ANDROID, EvidenceTools, MCPClient, SSHConnection, apk_metadata, manifest_identity
from collect_images.common import CollectorError, Frame, now
from collect_images.recorder import Recorder
from run_check import prepare_apk
from .static_prompts import FINAL_SCHEMA, PROBE_JUDGE, SYSTEM


VIEW = "android.intent.action.VIEW"
BROWSABLE = "android.intent.category.BROWSABLE"
DEFAULT = "android.intent.category.DEFAULT"
ENTRY_LIMIT = 40


def class_name(package, name):
    if not name:
        return ""
    if name.startswith("."):
        return package + name
    return name if "." in name else package + "." + name


def manifest_entries(manifest):
    xml = manifest.get("content", manifest.get("response", "")) if isinstance(manifest, dict) else manifest
    try:
        root = ET.fromstring(xml)
        package = root.attrib["package"]
        app = root.find("application")
        if root.tag != "manifest" or app is None:
            raise ValueError()
    except (TypeError, KeyError, ValueError, ET.ParseError):
        raise CollectorError("Manifest 缺少可核对的组件声明") from None

    def attr(node, key, default=""):
        return node.get(ANDROID + key, default)

    activities = {class_name(package, attr(node, "name")): node for node in app.findall("activity")}
    entries = []
    for node in app:
        if node.tag not in ("activity", "activity-alias") or not attr(node, "name"):
            continue
        name = class_name(package, attr(node, "name"))
        target = class_name(package, attr(node, "targetActivity")) if node.tag == "activity-alias" else name
        target_node = activities.get(target)
        filters = [{
            "actions": [attr(child, "name") for child in f.findall("action")],
            "categories": [attr(child, "name") for child in f.findall("category")],
            "data": [{key.removeprefix(ANDROID): value for key, value in child.attrib.items()} for child in f.findall("data")],
            "extra_constraints": any(child.tag not in ("action", "category", "data") for child in f),
        } for f in node.findall("intent-filter")]
        exported = attr(node, "exported", "unspecified")
        if exported == "false" or exported == "unspecified" and not filters:
            continue
        # Keep ambiguous declarations as leads, but only replay a known enabled,
        # permission-free Activity or alias (including its target).
        enabled = all(attr(element, "enabled", "true") == "true" for element in (app, node, target_node) if element is not None)
        permissions = sorted({attr(element, "permission") for element in (app, node, target_node)
                              if element is not None and attr(element, "permission")})
        entries.append({"id": "A%03d" % (len(entries) + 1), "name": name, "target": target, "exported": exported,
                        "enabled": enabled and target_node is not None, "permissions": permissions, "filters": filters})
    return entries


def plain_uri(uri):
    # Narrow grammar also excludes Android-shell metacharacters, percent escapes,
    # fragments, credentials, extras, query parameters and port overrides.
    if not isinstance(uri, str) or not re.fullmatch(r"[a-z][a-z0-9+.-]{0,31}://[a-z0-9.-]+(?:/[a-zA-Z0-9._~/-]*)?", uri):
        return False
    if len(uri) > 500:
        return False
    parsed = urlsplit(uri)
    return (parsed.scheme not in {"file", "content", "intent", "android-app", "javascript", "data", "tel", "sms", "mailto"}
            and not {".", ".."} & set(parsed.path.split("/")))


def matches_replay_filter(entry, uri):
    if not plain_uri(uri) or entry["exported"] != "true" or not entry["enabled"] or entry["permissions"]:
        return False
    parsed = urlsplit(uri)
    for f in entry["filters"]:
        if VIEW not in f["actions"] or not {BROWSABLE, DEFAULT} <= set(f["categories"]) or f["extra_constraints"] or len(f["data"]) != 1:
            continue
        data = f["data"][0]
        if set(data) - {"scheme", "host", "path", "pathPrefix"} or data.get("scheme") != parsed.scheme or data.get("host") != parsed.hostname:
            continue
        if ("path" not in data or data["path"] == parsed.path) and ("pathPrefix" not in data or parsed.path.startswith(data["pathPrefix"])):
            return True
    return False


def select_entries(entries, replay_uris=()):
    related = re.compile(r"teen|youth|minor|juvenile|guardian|parent|teenager|未成年人|青少年|家长", re.I)

    def rank(entry):
        requested = any(matches_replay_filter(entry, uri) for uri in replay_uris)
        relevant = bool(related.search(json.dumps(entry, ensure_ascii=False)))
        browsable = any(BROWSABLE in f["categories"] for f in entry["filters"])
        return not requested, not relevant, not browsable

    return sorted(entries, key=rank)[:ENTRY_LIMIT]


class ExitStaticAgent(LimitsAgent):
    def __init__(self, config, client, tools, recorder, metadata, entries):
        super().__init__(config, client, tools, recorder, metadata, images=[])
        self.entries = {entry["id"]: entry for entry in entries}

    def run(self):
        compact = [{**{key: e[key] for key in ("id", "name", "target", "exported", "enabled", "permissions")},
                    "filter_preview": json.dumps(e["filters"], ensure_ascii=False)[:700]} for e in self.entries.values()]
        self.messages = [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps({"apk": self.metadata, "entries": compact}, ensure_ascii=False)}]
        return self._run_tools(FINAL_SCHEMA, "提交覆盖说明及有真实引文的旁路候选。", result_key="candidates")

    def _finish(self, value, budget_forced=False):
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), list) or len(value["candidates"]) > 8:
            raise CollectorError("候选结果格式无效（最多 8 条）")
        candidates, seen = [], set()
        for item in value["candidates"]:
            if not isinstance(item, dict) or not isinstance(item.get("entry_id"), str) or item["entry_id"] not in self.entries:
                raise CollectorError("候选引用了未提供的 Manifest 入口")
            if item.get("kind") not in ("mode_exit", "restricted_page"):
                raise CollectorError("候选类型无效")
            refs = validate_code_references(item.get("code_evidence"), self.tools.evidence)
            if not refs:
                raise CollectorError("候选需要已读取的源码引文")
            uri = clean_text(item.get("uri"), "uri", 1000, allow_empty=True)
            if uri and not any('"' + uri + '"' in ref["quote"] for ref in refs):
                raise CollectorError("URI 须在候选源码引文中逐字出现，无法确认时请留空")
            target = clean_text(item.get("target"), "target", 400)
            key = (item["entry_id"], item["kind"], target, uri)
            if key in seen:
                raise CollectorError("候选重复")
            seen.add(key)
            entry = self.entries[item["entry_id"]]
            eligible = matches_replay_filter(entry, uri)
            if not uri:
                reason = "未从源码确认可直接重放的完整深链接"
            elif not eligible:
                reason = "入口声明或链接格式超出自动重放范围"
            else:
                reason = "需要指定该链接并确认模式开启后才能复测"
            candidates.append({"id": "C%02d" % (len(candidates) + 1), "entry_id": entry["id"], "entry": entry["name"],
                               "kind": item["kind"], "target": target, "uri": uri,
                               "reason": clean_text(item.get("reason"), "reason", 1600), "code_evidence": refs,
                               "replay_eligible": eligible, "validation": {"status": "pending", "reason": reason}})
        return {"status": "completed", "summary": clean_text(value.get("summary"), "summary", 2000),
                "coverage": clean_text(value.get("coverage"), "coverage", 2000),
                "limitations": parse_limitations(value.get("limitations")), "candidates": candidates}


class StaticAnalysis:
    def __init__(self, config, backend, package, version, root, apk=None, replay_uris=()):
        self.config, self.backend, self.package, self.version = config, backend, package, version
        self.recorder = Recorder(root / "static")
        self.apk, self.replay_uris = apk, tuple(dict.fromkeys(replay_uris))
        self.result = {"status": "error", "candidates": [], "directory": str(self.recorder.root.relative_to(root)),
                       "replay_uris": self.replay_uris}

    def save(self):
        self.recorder.save_json("result.json", self.result)

    def scan(self):
        self.result["started_at"] = now()
        try:
            if self.backend is None:
                if self.apk is None:
                    raise CollectorError("没有手机连接时须指定待分析的 APK")
                apk = self.apk.resolve()
                metadata = apk_metadata(apk)
                if self.package and metadata["package"] != self.package:
                    raise CollectorError("输入 APK 包名与 --package 不一致，未继续检测")
                self.result["input_source"] = "local_apk"
            else:
                apk, metadata = prepare_apk(self.backend, self.package, self.apk, self.recorder.root)
                if metadata["version_name"] != self.version:
                    raise CollectorError("App 版本在准备阶段发生变化，请重新启动检查")
                self.result["input_source"] = "device_verified"
            client = ChatClient(self.config)
            self.result["apk"] = metadata
            print("[静态旁路] 核对或准备 JADX 工程…", flush=True)
            jadx = self.config["jadx"]
            ensure_server(apk, metadata, jadx)
            with SSHConnection(jadx) as connection, ExitStack() as cleanup:
                connection.expected_sha256 = metadata["sha256"]
                connection.wait_ready()
                mcp = MCPClient(connection.url, jadx["timeout"])
                cleanup.callback(mcp.close)
                schemas = mcp.initialize()
                manifest = mcp.call("get_android_manifest", {})
                verify_identity(metadata, connection.identity(), manifest_identity(manifest), {})
                entries = manifest_entries(manifest)
                selected = select_entries(entries, self.replay_uris)
                self.recorder.save_json("entries.json", {"entries": entries, "selected_ids": [e["id"] for e in selected]})
                self.result.update(entry_count=len(entries), selected_count=len(selected))
                (self.recorder.root / "code_evidence").mkdir(mode=0o700)
                tools = EvidenceTools(mcp, schemas, self.recorder, self.config["max_tool_chars"],
                                      self.config["max_search_classes"], recovery=connection.recover_source)
                agent = ExitStaticAgent(self.config, client, tools, self.recorder,
                                        {**metadata, "replay_focus": self.replay_uris}, selected)
                try:
                    findings = agent.run()
                    verify_identity(metadata, connection.identity(), manifest_identity(mcp.call("get_android_manifest", {})), {})
                    self.result.update(findings)
                finally:
                    self.result["stats"] = {"model_calls": agent.model_calls, "tool_calls": agent.tool_calls}
        except KeyboardInterrupt:
            self.result.update(status="interrupted", summary="静态分析已中断")
        except BudgetEnded as exc:
            self.result.update(status="incomplete", summary=str(exc))
        except (CollectorError, OSError) as exc:
            self.result.update(status="error", summary=str(exc) if isinstance(exc, CollectorError) else "静态分析的文件或连接操作失败")
        except Exception as exc:
            self.result.update(status="error", summary="静态分析内部错误（%s）" % type(exc).__name__)
        finally:
            self.result["finished_at"] = now()
            self.save()
        return self.result

    def _resolve_intent(self, candidate):
        package = self.package
        uri = candidate["uri"]
        if uri not in self.replay_uris or not candidate["replay_eligible"] or not plain_uri(uri):
            raise CollectorError("深链接未通过重放限制")
        intent = ["--user", "current", "-a", VIEW, "-c", BROWSABLE, "-d", uri, "-p", package]
        resolved = self.backend._run(["shell", "cmd", "package", "resolve-activity", "--brief", *intent]).decode(errors="replace")
        component = resolved.strip().splitlines()[-1] if resolved.strip() else ""
        actual_package, _, actual_class = component.partition("/")
        if actual_package != package or class_name(package, actual_class) != candidate["entry"]:
            raise CollectorError("设备实际解析入口与候选不一致，未启动")
        return intent

    def _launch(self, intent):
        output = self.backend._run(["shell", "am", "start", "-W", *intent]).decode(errors="replace")
        if not re.search(r"^Status: ok\s*$", output, re.M):
            raise CollectorError("设备未确认深链接启动成功")

    def _judge_probe(self, candidate, observation, images, root):
        image_ids = [observation["before_image"], observation["landing_image"], observation["after_image"]]
        if observation["status"] != "collected" or not all(image_ids):
            return {"status": "unknown", "reason": observation["reason"], "image_ids": list(dict.fromkeys(i for i in image_ids if i))}
        catalog = {i["id"]: i for i in images}
        content = [{"type": "text", "text": json.dumps({"candidate": candidate, "mode_after": observation["mode_observation"]}, ensure_ascii=False)}]
        for label, identity in zip(("触发前", "触发后首张", "状态核对"), image_ids):
            item = catalog[identity]
            frame = Frame.from_png((root / item["file"]).read_bytes())
            if frame.sha256 != item["sha256"]:
                raise CollectorError("候选截图哈希不匹配")
            content.extend([{"type": "text", "text": label + " " + identity},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + frame.asset()["data_base64"]}}])
        client = ChatClient(self.config)
        message, _, _ = client.chat([{"role": "system", "content": PROBE_JUDGE}, {"role": "user", "content": content}],
                                     min(60, self.config["api_timeout"]), json_mode=True)
        try:
            value = json.loads(message.get("content", ""))
            verdict = value["verdict"]
            refs = value["image_ids"]
            if verdict not in ("bypass", "blocked", "unknown") or not isinstance(refs, list) or any(i not in image_ids for i in refs):
                raise ValueError()
            if verdict != "unknown" and not set(image_ids) <= set(refs):
                raise ValueError()
            if verdict == "blocked" and observation["mode_observation"] != "on":
                raise ValueError()
            if verdict == "bypass" and candidate["kind"] == "mode_exit" and observation["mode_observation"] != "off":
                raise ValueError()
            return {"status": verdict, "reason": clean_text(value["reason"], "reason", 1000), "image_ids": refs}
        except (TypeError, KeyError, ValueError):
            raise CollectorError("候选复测判定缺少一致的截图证据") from None

    def probe(self, collector):
        """Run after S2 verified on; return whether the positive control can start."""
        attempted = set()
        for candidate in self.result["candidates"]:
            uri = candidate["uri"]
            if uri in attempted or uri not in self.replay_uris or not candidate["replay_eligible"]:
                continue
            try:
                installed = self.backend.apk_paths(self.package)[0]
                if self.backend.apk_hash(installed) != self.result["apk"]["sha256"]:
                    raise CollectorError("手机 APK 在扫描后发生变化，未重放候选")
            except CollectorError as exc:
                candidate["validation"] = {"status": "pending", "reason": str(exc)}
                self.save()
                return True
            try:
                intent = self._resolve_intent(candidate)
            except CollectorError as exc:
                candidate["validation"] = {"status": "pending", "reason": str(exc)}
                self.save()
                continue
            print("[静态旁路] 真机复测 " + candidate["id"], flush=True)
            attempted.add(uri)
            observation = collector.run_phase("probe", trigger=lambda: self._launch(intent),
                                              trigger_params={"candidate": candidate["id"], "uri": uri})
            candidate["validation"] = {**observation, "collection_status": observation["status"], "status": "unknown"}
            self.save()
            if observation["status"] == "interrupted":
                raise KeyboardInterrupt
            try:
                candidate["validation"].update(self._judge_probe(candidate, observation, collector.images, collector.recorder.root))
            except CollectorError as exc:
                candidate["validation"]["reason"] = str(exc)
            self.save()
            if observation["status"] != "collected" or observation["mode_observation"] != "on":
                return False
        return True

    def completed(self):
        resolved = {c["uri"] for c in self.result["candidates"] if c["validation"]["status"] in ("bypass", "blocked")}
        return self.result["status"] == "completed" and set(self.replay_uris) <= resolved
