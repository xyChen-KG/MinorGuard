"""Usage: python -m check_limits --apk APP.apk --images COLLECTION_DIRECTORY"""

import argparse
import base64
from contextlib import ExitStack
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from collect_images.common import CODE_ROOT, CollectorError, finite_number, load_env, now
from collect_images.recorder import Recorder
from .agent import BudgetEnded, ChatClient, LimitsAgent
from .report import write_report
from .prompts import SYSTEM, EXTRACT, ANALYZE, FINAL_SCHEMA
from .tools import EvidenceTools, MCPClient, SSHConnection, apk_metadata, manifest_identity


def load_settings(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CollectorError("无法读取 check_config.json") from None
    if not isinstance(config, dict) or not isinstance(config.get("goal"), str) or not config["goal"].strip():
        raise CollectorError("检查配置必须包含非空 goal")
    ranges = {"api_timeout": (1, 600), "max_seconds": (1, 7200), "max_model_calls": (2, 200),
              "max_tool_calls": (1, 400), "max_images": (1, 100), "max_tool_chars": (1000, 50000),
              "max_context_chars": (10000, 3000000)}
    config.setdefault("max_search_classes", 5000)
    ranges["max_search_classes"] = (1, 20000)
    for key, (low, high) in ranges.items():
        if type(config.get(key)) is not int:
            raise CollectorError("配置 " + key + " 必须为整数")
        finite_number(config[key], key, low, high)
    if config.get("thinking") not in (None, "enabled", "disabled"):
        raise CollectorError("thinking 应为 enabled、disabled 或 null")
    if config.get("reasoning_effort") not in (None, "low", "high", "max"):
        raise CollectorError("reasoning_effort 应为 low、high、max 或 null")
    if config.get("thinking") == "disabled" and config.get("reasoning_effort") is not None:
        raise CollectorError("关闭 thinking 时不要同时设置 reasoning_effort，避免矛盾配置")
    jadx = config.get("jadx")
    if not isinstance(jadx, dict) or not all(isinstance(jadx.get(k), str) and jadx[k] for k in ("ssh_host", "remote_apk_state_file")):
        raise CollectorError("缺少 JADX SSH 配置")
    if type(jadx.get("remote_mcp_port")) is not int:
        raise CollectorError("MCP 端口必须为整数")
    finite_number(jadx["remote_mcp_port"], "remote_mcp_port", 1, 65535)
    jadx["timeout"] = finite_number(jadx.get("timeout", 180), "jadx.timeout", 1, 600)
    for key, default, high in (("ready_timeout", 600, 1800), ("reparse_timeout", 600, 1800), ("heap_gb", 32, 48)):
        jadx[key] = finite_number(jadx.get(key, default), "jadx." + key, 1, high)
    output = config.get("output_dir")
    if not isinstance(output, str) or not output:
        raise CollectorError("缺少 output_dir")
    config["output_dir"] = str((path.parent / output).resolve())
    return config


def prepare_images(directory, maximum, recorder=None):
    directory = directory.resolve()
    if not directory.is_dir():
        raise CollectorError("图片目录不存在")
    # Accept either the collection root or its images subdirectory. Ignore unrelated folders.
    image_dir = directory / "images" if (directory / "images").is_dir() else directory
    paths = [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    paths.sort(key=lambda p: [(1, int(s)) if s.isdigit() else (0, s.lower()) for s in re.split(r"(\d+)", p.name)])
    if not paths or len(paths) > maximum:
        raise CollectorError("需要 1—%d 张有顺序的截图；不会静默抽掉图片" % maximum)
    prepared, total_bytes = [], 0
    for index, path in enumerate(paths, 1):
        if path.stat().st_size > 20 * 1024 * 1024:
            raise CollectorError("单张截图超过 20 MiB")
        raw = path.read_bytes()
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 20000000 or min(image.size) < 100:
                    raise CollectorError("图片尺寸过小或超过像素上限")
                image.load()
                width, height = image.size
                upload = image.convert("RGB")
                upload.thumbnail((2800, 2800), Image.Resampling.LANCZOS)
                encoded = io.BytesIO()
                upload.save(encoded, format="JPEG", quality=92)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise CollectorError("存在无法读取的截图：" + path.name) from None
        total_bytes += len(encoded.getvalue())
        if total_bytes > 24 * 1024 * 1024:
            raise CollectorError("本轮图片编码前总量超过 24 MiB；请缩小明确的输入范围")
        identity = "I%03d" % index
        relative = "images/" + identity + path.suffix.lower()
        if recorder:
            recorder._write(recorder.root / relative, raw)
        prepared.append({"id": identity, "file": relative, "source": str(path), "width": width, "height": height,
                         "sha256": hashlib.sha256(raw).hexdigest(),
                         "data_url": "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")})
    metadata_file = directory / "result.json"
    if image_dir == directory:
        metadata_file = directory.parent / "result.json"
    identity = {}
    if metadata_file.is_file() and metadata_file.stat().st_size < 1024 * 1024:
        try:
            value = json.loads(metadata_file.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                identity = {k: value[k] for k in ("package", "app_version") if isinstance(value.get(k), str)}
        except (OSError, ValueError):
            pass
    return prepared, identity


def verify_identity(local, remote, manifest, screenshot):
    if local["sha256"] != remote.get("sha256"):
        raise CollectorError("本地 APK 与服务器当前加载记录的 APK 哈希不同；没有自动切换/覆盖服务器项目")
    for key in ("package", "version_name", "version_code"):
        if local.get(key) is None or str(local[key]) != str(manifest.get(key)):
            raise CollectorError("JADX 当前项目的包名/版本与输入 APK 不一致；停止以防串用其他 App 的证据")
    if screenshot.get("package") and screenshot["package"] != local["package"]:
        raise CollectorError("截图采集记录的 App 包名与 APK 不一致")
    if screenshot.get("app_version") and screenshot["app_version"] != local["version_name"]:
        raise CollectorError("截图采集记录的 App 版本与 APK 不一致")
    return bool(screenshot.get("package") and screenshot.get("app_version"))


def build_parser():
    parser = argparse.ArgumentParser(description="一个视觉大模型读取截图、调用只读 JADX 工具并生成简短时长核对报告")
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True, help="按操作顺序命名的截图目录或采集结果目录")
    parser.add_argument("--config", type=Path, default=CODE_ROOT / "check_config.json")
    parser.add_argument("--env-file", type=Path, default=CODE_ROOT / ".env")
    parser.add_argument("--goal", help="覆盖检查目标，不写死时长/时段数值")
    parser.add_argument("--output-dir", type=Path, help="本轮输出父目录，供端到端入口组织证据")
    parser.add_argument("--check", action="store_true", help="只核对输入、SSH/MCP、APK 身份和密钥是否配置；不请求模型")
    return parser


def execute(args):
    """Return the actual result and report directory; also used by run_check."""
    recorder, agent, result = None, None, {}
    started, started_at = time.monotonic(), now()
    try:
        load_env(args.env_file.resolve())
        config = load_settings(args.config.resolve())
        if args.output_dir is not None:
            config["output_dir"] = str(args.output_dir.resolve())
        if args.goal is not None:
            if not args.goal.strip():
                raise CollectorError("检查目标不能为空")
            config["goal"] = args.goal
        local = apk_metadata(args.apk.resolve())
        if not args.check:
            recorder = Recorder(Path(config["output_dir"]))
            (recorder.root / "code_evidence").mkdir(mode=0o700)
            print("输出目录：" + str(recorder.root), flush=True)
        images, screenshot = prepare_images(args.images, config["max_images"], recorder)
        result.update(apk=local, images=[{k: v for k, v in i.items() if k != "data_url"} for i in images], goal=config["goal"])
        if recorder:
            recorder.save_json("run_config.json", config)
            recorder.save_json("inputs.json", result)
            recorder.save_json("prompt_snapshot.json", {"system": SYSTEM, "extract": EXTRACT, "analyze": ANALYZE,
                "final_schema": FINAL_SCHEMA, "source_sha256": {name: hashlib.sha256(
                    (Path(__file__).parent / name).read_bytes()).hexdigest() for name in ("prompts.py", "tools.py", "agent.py")}})
        print("[准备] 核对服务器 APK 和只读 MCP 工具…", flush=True)
        with SSHConnection(config["jadx"]) as connection, ExitStack() as cleanup:
            connection.expected_sha256 = local["sha256"]
            result["analyzer_readiness"] = connection.wait_ready()
            remote = connection.identity()
            mcp = MCPClient(connection.url, config["jadx"]["timeout"])
            cleanup.callback(mcp.close)
            schemas = mcp.initialize()
            manifest = manifest_identity(mcp.call("get_android_manifest", {}))
            matched = verify_identity(local, remote, manifest, screenshot)
            result.update(screenshot_identity_verified=matched, server_apk=remote)
            if args.check:
                print(json.dumps({"input": local, "images": len(images), "server_apk_matches": True,
                    "screenshot_version_matches_record": matched, "read_only_tools": sorted(schemas),
                    "model_configured": all(os.environ.get("CHECK_" + k, os.environ.get(k)) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")),
                    "note": "未调用模型、未操作手机、未改动服务器项目"}, ensure_ascii=False, indent=2))
                return {"status": "checked"}, None
            tools = EvidenceTools(mcp, schemas, recorder, config["max_tool_chars"], config["max_search_classes"],
                                  recovery=connection.recover_source)
            client = ChatClient(config)
            metadata = dict(local, screenshot_identity=screenshot, screenshot_version_verified=matched)
            agent = LimitsAgent(config, client, tools, recorder, metadata, images)
            result.update(agent.run())
    except KeyboardInterrupt:
        result.update(status="interrupted", summary="分析被中断，已保存现有规则和证据。", findings=[], limitations=[])
    except BudgetEnded as exc:
        result.update(status="incomplete", summary=str(exc), findings=[], limitations=[])
    except CollectorError as exc:
        result.update(status="error", summary=str(exc), findings=[], limitations=[])
    except Exception as exc:
        result.update(status="error", summary="内部错误（%s）；已保留现有记录。" % type(exc).__name__, findings=[], limitations=[])
    if recorder:
        result.update(started_at=started_at, finished_at=now(), duration_seconds=round(time.monotonic() - started, 3),
                      rules=agent.rules if agent and agent.rules else {"rules": [], "limitations": []},
                      stats={"model_calls": agent.model_calls if agent else 0, "tool_calls": agent.tool_calls if agent else 0,
                             "models": list(dict.fromkeys(agent.models)) if agent else [], "usage": agent.usage if agent else {}})
        write_report(recorder, result)
        print("结果：%s；%s" % (result["status"], result["summary"]), flush=True)
        print("报告：" + str(recorder.root / "report.md"), flush=True)
    else:
        print("无法运行：" + result.get("summary", "未知错误"), flush=True)
    return result, recorder.root if recorder else None


def main(argv=None):
    result, _ = execute(build_parser().parse_args(argv))
    return {"checked": 0, "completed": 0, "insufficient": 2, "incomplete": 2, "interrupted": 130}.get(result.get("status"), 1)


if __name__ == "__main__":
    raise SystemExit(main())
