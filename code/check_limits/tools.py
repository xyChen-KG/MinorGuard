"""Small stdlib MCP/SSH adapter. Only the explicit read-only allowlist reaches JADX."""

import hashlib
import json
import shutil
import tempfile
import re
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from collect_images.common import CollectorError


READ_ONLY = {
    "search_classes_by_keyword", "search_method_by_name", "get_class_source",
    "get_method_by_name", "get_methods_of_class", "get_fields_of_class",
    "get_xrefs_to_class", "get_xrefs_to_method", "get_xrefs_to_field",
    "get_resource_file", "get_all_resource_file_names", "get_strings",
    "get_package_tree", "get_smali_of_class", "get_all_classes",
}
JAVA_SOURCE_TOOLS = {"get_class_source", "get_method_by_name"}
SOURCE_TOOLS = JAVA_SOURCE_TOOLS | {"get_smali_of_class", "independent_class_source"}
BROKEN_SOURCE = ("JADX ERROR", "Method dump skipped", "Method not decompiled")


def java_source_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return next((value[k] for k in ("code", "response", "content") if isinstance(value.get(k), str)), "")
    return ""


def source_text(value):
    if isinstance(value, dict) and not any(isinstance(value.get(k), str) for k in ("code", "response", "content")):
        return value.get("smali", "") if isinstance(value.get("smali"), str) else ""
    return java_source_text(value)


def bad_source(value):
    text = java_source_text(value)
    return not text.strip() or any(marker in text for marker in BROKEN_SOURCE)


def is_smali(value):
    return bool(re.search(r"(?m)^\s*\.class\s", source_text(value)))
ANDROID = "{http://schemas.android.com/apk/res/android}"
UI_RESOURCE_SCHEMA = {
    "type": "object", "properties": {
        "query": {"type": "string", "description": "截图原文中的短语，或已找到的资源名；按字面匹配，忽略空白和大小写，不是正则"},
        "offset": {"type": "integer", "description": "匹配结果起点，默认 0"},
        "count": {"type": "integer", "description": "返回匹配数量，1—200，默认 20"}},
    "required": ["query"], "additionalProperties": False,
}
EVIDENCE_SEARCH_SCHEMA = {
    "type": "object", "properties": {
        "query": {"type": "string", "description": "字面短语，不是正则；忽略大小写"},
        "id": {"type": "string", "description": "可选，只搜一个已保存证据；不写则搜本轮全部证据"},
        "offset": {"type": "integer"}, "count": {"type": "integer"}},
    "required": ["query"], "additionalProperties": False,
}
CLASS_NAMES_SCHEMA = {
    "type": "object", "properties": {
        "query": {"type": "string", "description": "按完整类名的字面子串过滤，只读取类名元数据，不反编译全 APK"},
        "offset": {"type": "integer"}, "count": {"type": "integer"}},
    "required": ["query"], "additionalProperties": False,
}
LEAD_SCHEMA = {
    "type": "object", "properties": {
        "id": {"type": "string", "description": "稳定编号，如 L1；更新时沿用"},
        "target": {"type": "string", "description": "证据中的重要类、方法、路由或消息线索"},
        "source_id": {"type": "string", "description": "线索最初出处的真实证据编号"},
        "status": {"type": "string", "enum": ["pending", "checked", "blocked"]},
        "note": {"type": "string", "description": "查到什么或具体阻碍；不能把尚未查当作 blocked"},
        "result_id": {"type": "string", "description": "checked 必填，实际查过的结果证据编号"}},
    "required": ["id", "target", "source_id", "status", "note"], "additionalProperties": False,
}


class MCPToolError(CollectorError):
    """Retain failure kind instead of turning every 404 into empty Java."""
    def __init__(self, detail):
        detail = str(detail)[:600]
        folded = detail.casefold()
        self.category = next((category for category, pattern in (
            ("class_not_found", r"class[^\n]{0,180}not found|classnotfound"),
            ("method_not_found", r"method[^\n]{0,180}not found"),
            ("analyzer_not_ready", r"not initialized|not ready|\b503\b"),
            ("timeout", r"timeout|timed out"),
            ("decompile_error", r"decompil|jadx error"),
        ) if re.search(pattern, folded)), "tool_error")
        super().__init__("JADX %s：%s；不是 App 未实现功能的证据" % (self.category, detail))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apk_metadata(path):
    """Read only the root manifest attributes; no Java/Android SDK dependency."""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("AndroidManifest.xml")
            if info.file_size > 8 * 1024 * 1024:
                raise ValueError("manifest too large")
            raw = archive.read(info)
        if raw.lstrip().startswith(b"<"):
            root = ET.fromstring(raw)
            attrs = {"package": root.get("package"), "version_name": root.get(ANDROID + "versionName"),
                     "version_code": root.get(ANDROID + "versionCode")}
        else:
            attrs = _binary_manifest(raw)
        if not attrs.get("package") or not re.fullmatch(r"[\w]+(?:\.[\w]+)+", attrs["package"]):
            raise ValueError("no package")
        return dict(attrs, sha256=sha256_file(path), path=str(Path(path).resolve()))
    except (OSError, ValueError, KeyError, IndexError, struct.error, UnicodeError, zipfile.BadZipFile, ET.ParseError):
        raise CollectorError("无法读取 APK 根 Manifest；仅支持完整 base APK，不将缺失元数据当作已匹配") from None


def _binary_manifest(raw):
    if len(raw) < 8 or struct.unpack_from("<H", raw)[0] != 3:
        raise ValueError("not binary XML")
    strings = []
    offset = struct.unpack_from("<H", raw, 2)[0]
    total = struct.unpack_from("<I", raw, 4)[0]
    if total != len(raw) or offset < 8:
        raise ValueError("bad XML header")

    def string(index):
        if index >= len(strings):
            raise ValueError("bad string index")
        return strings[index]

    while offset + 8 <= total:
        kind, header, size = struct.unpack_from("<HHI", raw, offset)
        end = offset + size
        if header < 8 or size < header or end > total:
            raise ValueError("bad XML chunk")
        if kind == 1:
            if header < 28:
                raise ValueError("bad string pool")
            count, _, flags, start, _ = struct.unpack_from("<IIIII", raw, offset + 8)
            if count > 100000 or header + count * 4 > size or start >= size:
                raise ValueError("bad string pool offsets")
            utf8 = bool(flags & 0x100)
            for i in range(count):
                cursor = offset + start + struct.unpack_from("<I", raw, offset + header + i * 4)[0]
                def length(pos):
                    if utf8:
                        first = raw[pos]
                        return (((first & 0x7f) << 8) | raw[pos + 1], pos + 2) if first & 0x80 else (first, pos + 1)
                    first = struct.unpack_from("<H", raw, pos)[0]
                    return (((first & 0x7fff) << 16) | struct.unpack_from("<H", raw, pos + 2)[0], pos + 4) if first & 0x8000 else (first, pos + 2)
                if not offset + start <= cursor < end:
                    raise ValueError("bad string")
                n, cursor = length(cursor)
                if utf8:
                    n, cursor = length(cursor)
                nbytes = n if utf8 else 2 * n
                if cursor + nbytes > end:
                    raise ValueError("truncated string")
                strings.append(raw[cursor:cursor + nbytes].decode("utf-8" if utf8 else "utf-16-le"))
        elif kind == 0x102:
            if header < 16 or offset + header + 20 > end:
                raise ValueError("bad element")
            ext = offset + header
            _, name, attr_start, attr_size, count = struct.unpack_from("<IIHHH", raw, ext)
            if string(name) == "manifest":
                attrs = {}
                if attr_start < 20 or attr_size < 20 or ext + attr_start + attr_size * count > end:
                    raise ValueError("bad attributes")
                for i in range(count):
                    pos = ext + attr_start + i * attr_size
                    _, key, literal, _, _, value_type, value = struct.unpack_from("<IIIHBBI", raw, pos)
                    key = string(key)
                    if key in ("package", "versionName", "versionCode"):
                        text = string(literal) if literal != 0xffffffff else (string(value) if value_type == 3 else str(value))
                        attrs[{"versionName": "version_name", "versionCode": "version_code"}.get(key, key)] = text
                return attrs
        offset = end
    raise ValueError("no manifest element")


class SSHConnection:
    """Own one foreground SSH tunnel; never change/restart the existing server."""
    def __init__(self, config):
        self.config, self.process, self.url = config, None, None

    def remote(self, script, timeout=20):
        host = self.config["ssh_host"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
            raise CollectorError("ssh_host 不是有效的 SSH 别名/主机")
        try:
            result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                                     "python3 -"], input=script, text=True, capture_output=True, timeout=timeout)
            if result.returncode:
                raise CollectorError("服务器只读检查失败；请检查 safe 的 SSH 连接和 JADX 运行状态")
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise CollectorError("服务器只读检查失败或超时") from None

    def identity(self):
        # A local config path, never an argument chosen by the model. No shell interpolation.
        state_file = self.config["remote_apk_state_file"]
        script = "import hashlib,json\nfrom pathlib import Path\np=Path(Path(%r).read_text().strip())\nh=hashlib.sha256()\nwith p.open('rb') as f:\n for b in iter(lambda:f.read(1048576),b''):h.update(b)\nprint(json.dumps({'path':str(p),'sha256':h.hexdigest()}))\n" % state_file
        return self.remote(script)

    def wait_ready(self):
        """Fail closed: the patched plugin must prove load returned and passes initialized."""
        request = {"state": self.config["remote_apk_state_file"],
                   "timeout": self.config.get("ready_timeout", 600), "heap_gb": self.config.get("heap_gb", 32)}
        script = '''import json,time,urllib.request,urllib.error
from pathlib import Path
d=%r
deadline=time.monotonic()+d['timeout']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
status={}
try:
 while time.monotonic()<deadline:
  try:
   with opener.open('http://127.0.0.1:8650/minorguard-ready',timeout=3) as response:status=json.load(response)
   if status.get('protocol')!='minorguard-jadx-ready-v1':raise RuntimeError('Missing real readiness protocol; deploy runtime source first')
   if status.get('ready') is True and status.get('load_returned') is True and status.get('passes_initialized') is True:
    expected=Path(Path(d['state']).read_text().strip()).resolve()
    if [Path(p).resolve() for p in status.get('inputs',[])]!=[expected]:raise RuntimeError('Ready analyzer belongs to another APK')
    if status.get('heap_max_bytes',0)<d['heap_gb']*1024**3*.90:raise RuntimeError('JVM still uses old memory settings; reload managed analyzer')
    print(json.dumps(status));break
  except urllib.error.HTTPError as error:
   if error.code==404:raise RuntimeError('Missing real readiness endpoint; deploy runtime source first')
  except (OSError,ValueError):pass
  time.sleep(.5)
 else:raise RuntimeError('Analyzer initialization timed out: '+str(status.get('reason','unreachable')))
except Exception as error:print(json.dumps({'error':str(error)}))
''' % request
        value = self.remote(script, timeout=request["timeout"] + 15)
        if value.get("error") or value.get("ready") is not True:
            raise CollectorError("JADX 尚未真正初始化完成：" + value.get("error", "未得到有效就绪状态"))
        return value

    def recover_source(self, arguments, timeout):
        """Run the versioned isolated reader; timeout covers load AND code generation."""
        identity = self.identity()
        if getattr(self, "expected_sha256", identity["sha256"]) != identity["sha256"]:
            raise CollectorError("服务器 APK 在本轮分析中已变化；未对另一版本进行独立解析")
        root = self.config.get("managed_root", "")
        if not root or self.config["remote_apk_state_file"] != root + "/runtime/run/current_apk.txt":
            raise CollectorError("独立解析仅支持本项目托管目录")
        request = {"root": root, "sha256": identity["sha256"], "class_name": arguments["class_name"],
                   "heap_gb": int(self.config.get("heap_gb", 32)),
                   "timeout": min(timeout, self.config.get("reparse_timeout", 600))}
        script = '''import json,runpy
try:
 reader=runpy.run_path(%r)
 print(json.dumps(reader['read_source'](%r)))
except Exception as error:print(json.dumps({'error':type(error).__name__+': '+str(error)[:400]}))
''' % (root + "/tools/minorguard-runtime/read_source.py", request)
        result = self.remote(script, timeout=request["timeout"] + 15)
        if result.get("error"):
            raise CollectorError("独立解析失败：" + result["error"])
        return result

    def __enter__(self):
        # Validate host even before spawning the tunnel.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", self.config["ssh_host"]):
            raise CollectorError("ssh_host 格式不正确")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        remote_port = self.config["remote_mcp_port"]
        self.process = subprocess.Popen([
            "ssh", "-T", "-N", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-L", "127.0.0.1:%d:127.0.0.1:%d" % (port, remote_port), self.config["ssh_host"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        self.url = "http://127.0.0.1:%d/mcp" % port
                        return self
                except OSError:
                    time.sleep(.1)
            raise CollectorError("SSH 隧道未能建立；没有改动服务器已有服务")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None

    def __exit__(self, *args):
        self.close()


class MCPClient:
    def __init__(self, url, timeout=180):
        # The adapter is intentionally restricted to a local SSH tunnel.
        if not re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url):
            raise CollectorError("MCP 只能连接本机 SSH 转发端口")
        self.url, self.timeout, self.session, self.counter = url, timeout, None, 0
        self.protocol = "2025-03-26"
        self.wire_directory = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def rpc(self, method, params=None, notification=False):
        self.counter += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = self.counter
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers, method="POST")
        deadline = time.monotonic() + self.timeout
        # FastMCP may repeat a large package index in text and structuredContent.
        # It stays local; only bounded evidence pages go to the model.
        limit = (64 if method == "tools/call" and payload["params"].get("name") == "get_package_tree" else 32) * 1024 * 1024
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification or response.status == 202:
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    size = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise CollectorError("JADX 工具达到本次时间预算；可缩小搜索范围或写无法判断")
                        transport_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                        if transport_socket is not None:
                            transport_socket.settimeout(max(.01, deadline - time.monotonic()))
                        line = response.readline(256 * 1024 * 1024 + 1)
                        if time.monotonic() >= deadline:
                            raise CollectorError("JADX 工具达到本次时间预算；可缩小搜索范围或写无法判断")
                        size += len(line)
                        if size > 256 * 1024 * 1024:
                            raise CollectorError("MCP 响应超过 256 MiB 硬安全上限；未静默截断")
                        if not line:
                            raise CollectorError("MCP 事件流结束但没有结果")
                        if line.startswith(b"data:"):
                            if len(line) > limit:
                                self._save_large_wire(line[5:].strip())
                            data = json.loads(line[5:].strip())
                            if isinstance(data, dict) and data.get("id") == payload["id"]:
                                break
                else:
                    with tempfile.SpooledTemporaryFile(max_size=limit) as spool:
                        size = 0
                        while True:
                            if time.monotonic() >= deadline:
                                raise CollectorError("MCP 响应读取达到时间预算")
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            spool.write(chunk)
                            if size > 256 * 1024 * 1024:
                                raise CollectorError("MCP 响应超过 256 MiB 硬安全上限；未静默截断")
                        spool.seek(0)
                        if size > limit and self.wire_directory:
                            with (self.wire_directory / ("mcp-%d.wire.json" % self.counter)).open("wb") as saved:
                                shutil.copyfileobj(spool, saved)
                            (self.wire_directory / ("mcp-%d.wire.json" % self.counter)).chmod(0o600)
                            spool.seek(0)
                        data = json.load(spool)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise CollectorError("MCP 接口 HTTP %d；未重试" % status) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise CollectorError("MCP 连接/响应失败；未重试") from None
        if not isinstance(data, dict) or data.get("id") != payload["id"] or "error" in data:
            raise CollectorError("MCP 返回协议错误")
        return data.get("result", {})

    def _save_large_wire(self, raw):
        if self.wire_directory:
            path = self.wire_directory / ("mcp-%d.wire.json" % self.counter)
            with path.open("wb") as stream:
                stream.write(raw)
            path.chmod(0o600)

    def initialize(self):
        info = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "MinorGuard-check-limits", "version": "0.1"}})
        self.protocol = info.get("protocolVersion", self.protocol)
        self.rpc("notifications/initialized", notification=True)
        tools, cursor = {}, None
        for _ in range(10):
            page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            for item in page.get("tools", []):
                if isinstance(item, dict) and item.get("name") in READ_ONLY:
                    tools[item["name"]] = item
            cursor = page.get("nextCursor")
            if not cursor:
                break
        if not {"get_class_source", "search_classes_by_keyword", "get_method_by_name"} <= set(tools):
            raise CollectorError("MCP 缺少必需的只读工具")
        return tools

    def close(self):
        """Release only this client's stateful MCP session, before closing its tunnel."""
        if not self.session:
            return
        request = urllib.request.Request(self.url, method="DELETE", headers={
            "Mcp-Session-Id": self.session, "MCP-Protocol-Version": self.protocol})
        try:
            with self.opener.open(request, timeout=2):
                pass
        except urllib.error.HTTPError as exc:
            exc.close()
        except OSError:
            pass
        finally:
            self.session = None

    def call(self, name, arguments):
        if name not in READ_ONLY and name != "get_android_manifest":
            raise CollectorError("不允许调用该 JADX 工具")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        # Prefer structured output; FastMCP often also repeats it in text.
        if isinstance(result.get("structuredContent"), dict):
            value = result["structuredContent"]
            if result.get("isError") or value.get("error") or value.get("success") is False:
                raise MCPToolError(value.get("error", value))
            return value
        blocks = result.get("content", [])
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        if result.get("isError") or isinstance(value, dict) and (value.get("error") or value.get("success") is False):
            raise MCPToolError(value.get("error", value) if isinstance(value, dict) else value)
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def manifest_identity(value):
    if isinstance(value, dict):
        value = value.get("content", value.get("response", ""))
    try:
        root = ET.fromstring(value)
        return {"package": root.get("package"), "version_name": root.get(ANDROID + "versionName"),
                "version_code": root.get(ANDROID + "versionCode")}
    except (TypeError, ValueError, ET.ParseError):
        raise CollectorError("JADX 未返回可核对的 Manifest") from None


class EvidenceTools:
    """Save full results; the model sees bounded pages, never silently truncated code."""
    def __init__(self, mcp, schemas, recorder, max_chars=14000, max_search_classes=5000, recovery=None):
        self.mcp, self.schemas, self.recorder, self.max_chars = mcp, schemas, recorder, max_chars
        self.max_search_classes, self.scope_counts = max_search_classes, {}
        self.package_counts = None
        self.evidence = {}
        self.query_cache = {}
        self.ui_resources = None
        self.ui_resource_info = None
        self.recovery = recovery
        self.read_timeout = 600
        self.failed_source_queries = {}
        self.recovered_classes = {}
        self.class_names = None
        self.leads = {}
        self.mcp.wire_directory = self.recorder.root / "code_evidence"

    def definitions(self):
        definitions = [{"type": "function", "function": {
            "name": name, "description": item.get("description", "")[:2000],
            "parameters": item["inputSchema"]}} for name, item in sorted(self.schemas.items())]
        for entry in definitions:
            function = entry["function"]
            if function["name"] == "search_method_by_name":
                function["description"] += " 全局扫描，仅适用于类数在扫描预算内的小 APK；大 APK 请先按类名定位，再限定 package 使用 search_classes_by_keyword(search_in=method)。"
            elif function["name"] == "search_classes_by_keyword":
                function["description"] += " class 之外的 method/field/code/comment 搜索必须限定 package，并受类数预算限制。"
        definitions.append({"type": "function", "function": {
            "name": "read_evidence", "description": "读取已保存工具结果的另一行段；避免整类过长被截断。返回文本行号是本地证据行号。",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}},
                "required": ["id", "start_line"], "additionalProperties": False}}})
        for name, schema, description in (
            ("search_evidence", EVIDENCE_SEARCH_SCHEMA, "在本轮已保存的完整结果内搜索，返回命中行及上下文；保留原证据编号，返回的源码片段可引用。不触发远程反编译。"),
            ("record_lead", LEAD_SCHEMA, "记录或更新少量重要线索及出处；checked 要附实际结果证据，blocked 要具体说明访问障碍。返回剩余 pending。不是新 Agent。"),
        ):
            definitions.append({"type": "function", "function": {"name": name, "description": description, "parameters": schema}})
        if "get_all_classes" in self.schemas:
            definitions.append({"type": "function", "function": {"name": "find_class_names", "parameters": CLASS_NAMES_SCHEMA,
                "description": "检索完整类名和内部类别名；自动缓存分页元数据，不反编译全 APK，不要求只按简单类名查包。"}})
        if "get_strings" in self.schemas:
            definitions.append({"type": "function", "function": {
                "name": "search_ui_resources", "parameters": UI_RESOURCE_SCHEMA,
                "description": "优先用截图文字定位 APK 字符串资源。只传入截图短语或资源名；本地过滤并缓存资源表，不把整表上传模型。返回原文、资源名、文件和检索局限；资源声明不是实现证据。"}})
        return definitions

    def call(self, name, arguments):
        self.read_deadline = time.monotonic() + self.read_timeout
        if name in ("search_evidence", "record_lead", "find_class_names"):
            schema = {"search_evidence": EVIDENCE_SEARCH_SCHEMA, "record_lead": LEAD_SCHEMA,
                      "find_class_names": CLASS_NAMES_SCHEMA}[name]
            self._validate_arguments(schema, arguments)
            if name == "search_evidence":
                return self._search_evidence(arguments)
            if name == "record_lead":
                return self._record_lead(arguments)
            return self._find_class_names(arguments)
        if name == "read_evidence":
            if set(arguments) != {"id", "start_line"} or arguments["id"] not in self.evidence:
                raise CollectorError("证据编号不存在或参数不正确")
            return self.page(arguments["id"], arguments["start_line"])
        if name == "search_ui_resources":
            if "get_strings" not in self.schemas:
                raise CollectorError("当前 MCP 未提供字符串资源读取能力；可换现有资源工具或写无法判断")
            self._validate_arguments(UI_RESOURCE_SCHEMA, arguments)
            if not arguments["query"].strip():
                raise CollectorError("资源关键词不能为空")
            return self._save(name, arguments, self._search_ui_resources(arguments))
        if name not in self.schemas or name not in READ_ONLY:
            raise CollectorError("工具未在本轮只读清单中")
        self._validate_arguments(self.schemas[name]["inputSchema"], arguments)
        cache_key = json.dumps([name, arguments], ensure_ascii=False, sort_keys=True)
        if name in JAVA_SOURCE_TOOLS and arguments["class_name"] in self.recovered_classes:
            result = self.page(self.recovered_classes[arguments["class_name"]], 1)
            result.update(cached=True, requested={"tool": name, "arguments": arguments}, scope="whole_class")
            return result
        if cache_key in self.query_cache:
            result = self.page(self.query_cache[cache_key], 1)
            result["cached"] = True
            return result
        if cache_key in self.failed_source_queries:
            raise CollectorError("该源码/目标本轮已读取失败或恢复后仍不完整；请读取已保存证据 %s、核对类名或换路径，不复用为正常源码" % self.failed_source_queries[cache_key])
        if name == "search_method_by_name":
            total = self._scan_size()
            if total > self.max_search_classes:
                raise CollectorError("全局方法扫描涉及 %d 个类，超过扫描预算 %d；请读取已知类/方法，或限定 package 使用 search_classes_by_keyword(search_in=method)" % (total, self.max_search_classes))
        if name == "search_classes_by_keyword" and {"method", "field", "code", "comment"} & set(arguments.get("search_in", "code").split(",")):
            scope = arguments.get("package", "").strip().rstrip(".")
            if not scope:
                raise CollectorError("方法/字段/全文搜索请提供 package 范围；先用 search_in=class 找相关包，避免扫描整个大 APK")
            if scope == "defpackage" or re.fullmatch(r"p\d+", scope.split(".")[0]):
                raise CollectorError("JADX 会忽略这种纯 p数字 的包过滤；请直接读取已找到的类，而非全量全文搜索")
            if scope not in self.scope_counts:
                total = self._scan_size(scope)
                if total == 0:
                    raise CollectorError("包索引未找到此范围；零匹配不能用作小范围证明，请按实际类名读取")
                self.scope_counts[scope] = total
                self.recorder.append({"event": "search_scope_checked", "package": scope, "classes": total})
            total = self.scope_counts[scope]
            if total > self.max_search_classes:
                raise CollectorError("此范围有 %d 个类，超过单次全文扫描预算 %d；请缩小到具体功能包，或直接读取已找到的类/方法" % (total, self.max_search_classes))
        try:
            value = self.mcp.call(name, arguments)
        except CollectorError as error:
            if name not in JAVA_SOURCE_TOOLS:
                raise
            category = getattr(error, "category", "read_error")
            value = {"code": "", "read_error": str(error), "error_kind": category}
            if category in ("class_not_found", "method_not_found", "analyzer_not_ready"):
                original = self._save(name, arguments, value)
                self.failed_source_queries[cache_key] = original["evidence_id"]
                original["error_kind"] = category
                original["note"] += "；未重解析整个 APK。请 find_class_names 核对真实全名，或读取外层/承载类；未就绪则先修复就绪状态。"
                return original
        if name in JAVA_SOURCE_TOOLS and bad_source(value):
            return self._recover(name, arguments, value, cache_key)
        if name == "get_smali_of_class" and not is_smali(value):
            raise CollectorError("未返回有效 Smali 类指令；未缓存为空源码或正常结果")
        result = self._save(name, arguments, value)
        self.query_cache[cache_key] = result["evidence_id"]
        return result

    def _search_evidence(self, arguments):
        query = arguments["query"].strip().casefold()
        if not query:
            raise CollectorError("证据搜索词不能为空")
        identity = arguments.get("id")
        if identity is not None and identity not in self.evidence:
            raise CollectorError("证据编号不存在")
        records = [self.evidence[identity]] if identity else list(self.evidence.values())
        hits = []
        for entry in records:
            lines = entry["text"].splitlines()
            for at, line in enumerate(lines):
                if query in line.casefold():
                    hits.append((entry, lines, at))
        offset, count = arguments.get("offset", 0), arguments.get("count", 8)
        matches, size = [], 0
        for entry, lines, at in hits[offset:offset + count]:
            start, end = max(0, at - 3), min(len(lines), at + 4)
            clipped = any(len(line) > 1800 for line in lines[start:end])
            if clipped:
                # A giant JSON/code line must not hide a match far beyond its first page.
                start, end = at, at + 1
                line = lines[at]
                match_at = line.casefold().find(query)
                left = max(0, match_at - 400)
                text = line[left:max(left + 1800, match_at + len(query))]
            else:
                text = "\n".join(lines[start:end])
            item = {"evidence_id": entry["id"], "tool": entry["tool"], "arguments": entry["arguments"],
                    "start_line": start + 1, "end_line": end, "text": text, "clipped_line": clipped}
            if entry["tool"] == "get_smali_of_class":
                item["representation"] = "smali"
            cost = len(json.dumps(item, ensure_ascii=False))
            if matches and size + cost > self.max_chars:
                break
            size += cost
            matches.append(item)
            entry["seen"].append(text)
        next_offset = offset + len(matches)
        return {"query": arguments["query"], "matches": matches, "total_matches": len(hits),
                "next_offset": next_offset if next_offset < len(hits) else None,
                "note": "只搜本轮已保存结果；引用使用原 evidence_id。片段不代表整类已读；用 read_evidence 补上下文。"}

    def _record_lead(self, arguments):
        identity = arguments["id"]
        if not re.fullmatch(r"L[1-9][0-9]*", identity) or not arguments["target"].strip():
            raise CollectorError("线索编号须为 L1 等，target 不能为空")
        source = arguments["source_id"]
        if source not in self.evidence:
            raise CollectorError("线索出处证据不存在")
        status = arguments["status"]
        if status not in ("pending", "checked", "blocked"):
            raise CollectorError("线索状态无效")
        if status != "pending" and not arguments["note"].strip():
            raise CollectorError("已查或阻塞线索须说明结果/具体原因")
        if status == "checked" and arguments.get("result_id") not in self.evidence:
            raise CollectorError("checked 须引用实际查询结果的 result_id")
        if arguments.get("result_id") and arguments["result_id"] not in self.evidence:
            raise CollectorError("线索结果证据不存在")
        old = self.leads.get(identity)
        if old and any(old[key] != arguments[key] for key in ("target", "source_id")):
            raise CollectorError("更新须保留原 target/source_id；新线索请用新编号")
        self.leads[identity] = dict(arguments)
        self.recorder.save_json("leads.json", {"leads": list(self.leads.values())})
        self.recorder.append({"event": "lead_updated", "id": identity, "status": status,
                              "source_id": source, "result_id": arguments.get("result_id")})
        return {"updated": self.leads[identity], "pending": [v for v in self.leads.values() if v["status"] == "pending"]}

    def _find_class_names(self, arguments):
        if "get_all_classes" not in self.schemas:
            raise CollectorError("当前 MCP 未提供类名索引；请用已有类名搜索或实际引用")
        query = arguments["query"].strip().casefold()
        if not query:
            raise CollectorError("类名关键词不能为空")
        if self.class_names is None:
            names, offset, expected = [], 0, None
            while True:
                if time.monotonic() >= self.read_deadline:
                    raise CollectorError("类名索引读取达到预算；未把部分索引当完整结果")
                previous_timeout = self.mcp.timeout
                self.mcp.timeout = min(previous_timeout, max(.01, self.read_deadline - time.monotonic()))
                try:
                    value = self.mcp.call("get_all_classes", {"offset": offset, "count": 10000})
                finally:
                    self.mcp.timeout = previous_timeout
                if not isinstance(value, dict):
                    raise CollectorError("类名索引格式不可识别")
                page, pagination = value.get("items"), value.get("pagination", {})
                if not isinstance(page, list) or any(not isinstance(n, str) for n in page):
                    raise CollectorError("类名索引格式不可识别")
                total = pagination.get("total")
                if type(total) is not int or total < 0 or type(pagination.get("has_more")) is not bool:
                    raise CollectorError("类名索引分页不完整")
                if expected is not None and total != expected:
                    raise CollectorError("类名索引在分页过程中发生变化")
                expected = total
                names.extend(page)
                if not pagination["has_more"]:
                    # Distinct DEX classes can share a displayed alias in JADX.
                    # Completeness is about raw entries, not uniqueness of aliases.
                    if len(names) != expected:
                        raise CollectorError("类名索引缺失，未标记为完整")
                    break
                next_offset = pagination.get("next_offset")
                if not page or type(next_offset) is not int or next_offset != offset + len(page):
                    raise CollectorError("类名索引分页未连续前进")
                offset = next_offset
            self.class_names = names
            self.recorder.save_json("code_evidence/class_names.json", {"classes": names, "complete": True,
                "duplicate_aliases": len(names) - len(set(names))})
        matches = [name for name in self.class_names if query in name.casefold()]
        offset, count = arguments.get("offset", 0), arguments.get("count", 40)
        return self._save("find_class_names", arguments, {"items": matches[offset:offset + count],
            "total_matches": len(matches), "index_classes": len(self.class_names),
            "next_offset": offset + count if offset + count < len(matches) else None,
            "note": "元数据索引不是源码；内联或内部类可能须读取外层/承载类。"})

    def _recover(self, name, arguments, value, cache_key):
        """Record the bad read, try a fresh full-APK JVM, then actual instructions."""
        original = self._save(name, arguments, value)
        attempts = [{"step": "gui_source", "evidence_id": original["evidence_id"], "status": "incomplete"}]
        # Usually much cheaper than loading a second full APK. These are actual
        # instructions, not proof that Java decompilation succeeded.
        gui_smali_tried = "get_smali_of_class" in self.schemas
        if gui_smali_tried:
            try:
                instructions = self.mcp.call("get_smali_of_class", {"class_name": arguments["class_name"]})
                if is_smali(instructions):
                    result = self._save("get_smali_of_class", {"class_name": arguments["class_name"]}, {
                        "code": source_text(instructions), "backend": "gui_smali", "original_request": arguments,
                        "recovery_attempts": attempts})
                    result["recovery"] = {"attempts": attempts, "representation": "smali"}
                    self.query_cache[cache_key] = result["evidence_id"]
                    self.recovered_classes[arguments["class_name"]] = result["evidence_id"]
                    self.recorder.append({"event": "source_recovery_finished", "backend": "gui_smali", "evidence_id": result["evidence_id"]})
                    return result
                attempts.append({"step": "gui_smali", "error": "No valid instructions returned"})
            except CollectorError as error:
                attempts.append({"step": "gui_smali", "error": str(error)})
        recovered = None
        if self.recovery is not None:
            self.recorder.append({"event": "source_recovery_started", "arguments": arguments})
            try:
                remaining = self.read_deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectorError("没有剩余的独立解析时间预算")
                recovered = self.recovery(arguments, remaining)
                if not bad_source(recovered):
                    result = self._save("independent_class_source", {"class_name": arguments["class_name"]}, {
                        **recovered, "recovery_attempts": attempts + recovered.get("attempts", []),
                        "original_request": {"tool": name, "arguments": arguments}})
                    result["recovery"] = {"attempts": attempts + recovered.get("attempts", []), "scope": "whole_class"}
                    self.query_cache[cache_key] = result["evidence_id"]
                    self.recovered_classes[arguments["class_name"]] = result["evidence_id"]
                    self.recorder.append({"event": "source_recovery_finished", "backend": "independent_java", "evidence_id": result["evidence_id"]})
                    return result
                attempts += recovered.get("attempts", [])
                if java_source_text(recovered).strip():
                    partial = self._save("independent_class_source", arguments, recovered)
                    attempts.append({"step": "independent_source_incomplete", "evidence_id": partial["evidence_id"]})
            except CollectorError as error:
                attempts.append({"step": "independent_parse", "error": str(error)})
        instructions = recovered.get("smali", "") if isinstance(recovered, dict) else ""
        if is_smali(instructions):
            result = self._save("get_smali_of_class", {"class_name": arguments["class_name"]}, {
                "code": instructions, "backend": "independent_smali" if recovered else "gui_smali",
                "recovery_attempts": attempts, "original_request": arguments,
                "metadata": recovered.get("metadata", {}) if isinstance(recovered, dict) else {}})
            result["recovery"] = {"attempts": attempts, "representation": "smali"}
            # Cache only real, nonempty instructions, NOT the broken Java response.
            self.query_cache[cache_key] = result["evidence_id"]
            self.recovered_classes[arguments["class_name"]] = result["evidence_id"]
            self.recorder.append({"event": "source_recovery_finished", "backend": "smali", "evidence_id": result["evidence_id"]})
            return result
        self.failed_source_queries[cache_key] = original["evidence_id"]
        self.recorder.append({"event": "source_recovery_finished", "backend": "failed", "evidence_id": original["evidence_id"]})
        original["recovery"] = {"attempts": attempts, "status": "failed"}
        self.recorder.save_json("code_evidence/" + original["evidence_id"] + "-recovery.json", original["recovery"])
        return original

    def _save(self, name, arguments, value):
        identity = "E%03d" % (len(self.evidence) + 1)
        if isinstance(value, dict) and name in SOURCE_TOOLS:
            # Method responses use 'code'; keep real Java lines readable/quotable.
            text = source_text(value)
        elif isinstance(value, str):
            text = value
        elif isinstance(value, dict) and isinstance(value.get("response"), str):
            text = value["response"]
        elif isinstance(value, dict) and isinstance(value.get("content"), str):
            text = value["content"]
        elif isinstance(value, dict) and isinstance(value.get("file"), dict) and isinstance(value["file"].get("content"), str):
            text = value["file"]["content"]
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        # Some MCP versions add "12| " to every source line. Preserve the raw
        # response JSON, but keep the source evidence/quotes free of UI gutters.
        lines = text.splitlines()
        if name in JAVA_SOURCE_TOOLS and lines and all(re.match(r"^\s*\d+\|", line) for line in lines):
            text = "\n".join(re.sub(r"^\s*\d+\| ?", "", line) for line in lines)
        entry = {"id": identity, "tool": name, "arguments": arguments,
                 "file": "code_evidence/" + identity + ".txt", "text": text, "seen": []}
        self.evidence[identity] = entry
        self.recorder._write(self.recorder.root / entry["file"], text.encode("utf-8"))
        self.recorder.save_json("code_evidence/" + identity + ".json", {"tool": name, "arguments": arguments, "result": value})
        return self.page(identity, 1)

    def unread_source_targets(self, identity=None):
        """Navigation hints from literal constructors, not a semantic call graph.

        Never scan old runs or infer a target from class-name guesses. Keep
        candidates separate from leads until the model judges their relevance.
        """
        read = {v["arguments"].get("class_name") for v in self.evidence.values()
                if v["tool"] in SOURCE_TOOLS}
        entries = [self.evidence[identity]] if identity else self.evidence.values()
        found = {}
        frameworks = ("java.", "javax.", "android.", "androidx.", "kotlin.", "kotlinx.",
                      "io.reactivex.", "com.google.", "org.json.")
        for entry in entries:
            if entry["tool"] not in JAVA_SOURCE_TOOLS | {"independent_class_source"}:
                continue
            # Only actual qualified constructor tokens; no assumed package for
            # simple/inner names and no all-APK full-text decompilation.
            for target in re.findall(r"\bnew\s+([\w$]+(?:\.[\w$]+)+)\s*\(", entry["text"]):
                if target in read or target.startswith(frameworks) or not target.split(".")[0][0].islower():
                    continue
                found.setdefault(target, {"target": target, "source_id": entry["id"],
                    "query": {"tool": "get_class_source", "arguments": {"class_name": target}}})
        return list(found.values())

    def _search_ui_resources(self, arguments):
        if self.ui_resources is None:
            entries, pages, limitations, offset, complete = [], [], [], 0, False
            files_read = 0
            timeout = self.mcp.timeout
            deadline = time.monotonic() + timeout
            # Usually one merged strings.xml. Bound unusual/malformed responses.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    limitations.append("达到本次资源读取时间预算")
                    break
                try:
                    self.mcp.timeout = min(timeout, remaining)
                    page = self.mcp.call("get_strings", {"offset": offset, "count": 200})
                except CollectorError as exc:
                    limitations.append("资源读取失败：" + str(exc))
                    break
                finally:
                    self.mcp.timeout = timeout
                pages.append(page)
                if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                    limitations.append("资源列表格式不可识别，未将空结果当作没有相关资源")
                    break
                for item in page["items"]:
                    if not isinstance(item, dict) or not isinstance(item.get("content"), str) or not isinstance(item.get("file"), str):
                        limitations.append("部分资源条目格式不可识别")
                        continue
                    files_read += 1
                    try:
                        xml = item["content"]
                        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                            raise ValueError("不解析资源中的 DTD/实体声明")
                        root = ET.fromstring(xml)
                        if root.tag != "resources":
                            raise ValueError("不是 resources 根节点")
                    except (ValueError, ET.ParseError):
                        limitations.append("部分字符串 XML 无法解析")
                        continue
                    for node in root:
                        if node.get("name"):
                            entries.append({"resource_file": item["file"], "name": node.get("name"),
                                            "type": node.tag, "text": "".join(node.itertext()).strip()})
                pagination = page.get("pagination", {})
                if not isinstance(pagination, dict) or type(pagination.get("has_more")) is not bool:
                    limitations.append("资源接口缺少有效分页信息，不能确认覆盖完整")
                    break
                if not pagination["has_more"]:
                    complete = True
                    break
                next_offset = pagination.get("next_offset", offset + len(page["items"]))
                if type(next_offset) is not int or next_offset <= offset or not page["items"]:
                    limitations.append("资源分页没有前进，已停止重复读取")
                    break
                offset = next_offset
            if not complete:
                limitations.append("资源表未完整读取；零命中不代表没有实现")
            self.ui_resources = entries
            self.ui_resource_info = {"complete": complete and not limitations, "files_read": files_read,
                                     "resources_read": len(entries), "limitations": list(dict.fromkeys(limitations))[:8]}
            self.recorder.save_json("code_evidence/ui_resources.json", {"pages": pages, **self.ui_resource_info})
        fold = lambda text: "".join(text.split()).casefold()
        query = fold(arguments["query"])
        matches = [item for item in self.ui_resources if query in fold(item["text"]) or query in fold(item["name"])]
        offset, count = arguments.get("offset", 0), arguments.get("count", 20)
        end = offset + count
        return {"query": arguments["query"], "matches": matches[offset:end],
                "pagination": {"total": len(matches), "offset": offset, "count": len(matches[offset:end]),
                               "has_more": end < len(matches), "next_offset": end if end < len(matches) else None},
                "resource_scan": self.ui_resource_info,
                "note": "仅定位资源，不证明功能有效；资源 ID 未返回时不要猜测。原始资源表保存在 code_evidence/ui_resources.json。"}

    def _scan_size(self, scope=None):
        # Simple class-name search cannot establish package size. Method/field
        # scans also become expensive on large APKs, even without code search.
        if self.package_counts is None:
            probe = self.mcp.call("get_package_tree", {})
            packages = probe.get("packages") if isinstance(probe, dict) else None
            if not isinstance(packages, list) or any(not isinstance(p, dict) or not isinstance(p.get("name"), str)
                    or type(p.get("class_count")) is not int or p["class_count"] < 0 for p in packages):
                raise CollectorError("包索引不可用，无法估算扫描范围；请直接读取已找到的类或方法")
            if sum(p["class_count"] for p in packages) != probe.get("total_classes"):
                raise CollectorError("包索引不完整；请直接读取类或方法，不启动扫描")
            self.package_counts = packages
        return sum(p["class_count"] for p in self.package_counts
                   if scope is None or p["name"] == scope or p["name"].startswith(scope + "."))

    def page(self, identity, start):
        entry = self.evidence[identity]
        if type(start) is not int or start < 1:
            raise CollectorError("start_line 应为正整数")
        lines = entry["text"].splitlines() or [""]
        if start > len(lines):
            raise CollectorError("起始行超出证据范围")
        selected, count = [], 0
        for line in lines[start - 1:]:
            # Split an exceptional single huge JSON line without hiding truncation.
            if count + len(line) + 1 > self.max_chars:
                if not selected:
                    selected.append(line[:self.max_chars])
                break
            selected.append(line)
            count += len(line) + 1
        text = "\n".join(selected)
        entry["seen"].append(text)
        end = start + len(selected) - 1
        truncated_line = len(selected) == 1 and len(lines[start - 1]) > self.max_chars
        result = {"evidence_id": identity, "tool": entry["tool"], "arguments": entry["arguments"],
                "start_line": start, "end_line": end, "total_lines": len(lines), "text": text,
                "truncated": end < len(lines) or truncated_line,
                "next_start_line": end + 1 if end < len(lines) else None,
                "note": "完整结果已保存；超长单行请改用 get_method_by_name 定位所需方法" if truncated_line else "本地证据行号，不一定等于 JADX GUI 行号"}
        if entry["tool"] in SOURCE_TOOLS:
            if not entry["text"].strip():
                result["warning"] = "工具返回空源码，不代表功能不存在；可读外层类/具体方法或沿引用换路径。"
            elif any(marker in entry["text"] for marker in ("JADX ERROR", "Method dump skipped", "Method not decompiled")):
                result["warning"] = "源码含反编译错误或省略，不能把缺失片段当作没有实现；核对具体方法/外层类，仍不完整则 unknown。"
            if entry["tool"] == "get_smali_of_class":
                result["representation"] = "smali"
                result["note"] += "；这是 DEX 底层指令，不是恢复成功的 Java；需按寄存器、分支和实际调用判断。"
            if entry["tool"] == "independent_class_source":
                result["note"] += "；独立完整 APK 解析，禁用内联、不加载资源表；返回整类而非指定重载，必须核对实际声明。"
            targets = self.unread_source_targets(identity)
            if targets:
                result["unread_direct_targets"] = targets[:12]
                result["navigation_note"] = "这些是源码实际 new 的直接目标，可能是回调；不是已确认的调用链。优先选模式入口/限制页相关目标查读，并对限制动作查调用者。"
            if "get_xrefs_to_class" in self.schemas and entry["arguments"].get("class_name"):
                result["reverse_query"] = {"tool": "get_xrefs_to_class", "arguments": {
                    "class_name": entry["arguments"]["class_name"]}}
        return result

    @staticmethod
    def _validate_arguments(schema, arguments):
        if not isinstance(arguments, dict):
            raise CollectorError("工具参数必须是对象")
        props = schema.get("properties", {})
        if set(arguments) - set(props) or set(schema.get("required", [])) - set(arguments):
            raise CollectorError("工具参数缺失或包含未允许的字段")
        for key, value in arguments.items():
            field = props[key]
            kinds = [f.get("type") for f in field.get("anyOf", [])] or [field.get("type")]
            valid = any((t == "string" and isinstance(value, str)) or (t == "integer" and type(value) is int)
                        or (t == "boolean" and type(value) is bool) or (t == "null" and value is None) for t in kinds)
            if not valid:
                raise CollectorError("工具参数类型不正确：" + key)
            if isinstance(value, str) and len(value) > 1000:
                raise CollectorError("工具参数过长")
            if key == "count" and (value < 1 or value > 200):
                raise CollectorError("分页 count 限 1—200")
            if key == "offset" and value < 0:
                raise CollectorError("offset 不能为负")
            if key == "search_in" and not set(value.split(",")) <= {"class", "method", "field", "code", "comment"}:
                raise CollectorError("search_in 无效")
