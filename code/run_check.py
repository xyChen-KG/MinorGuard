"""One command: verify/export APK -> collect -> prepare server -> analyze -> report."""

import argparse
import re
import time
from pathlib import Path

from collect_images.adb import ADB
from collect_images.common import CODE_ROOT, CollectorError, NeedUser, load_config, load_env, now
from collect_images.jev import JevClient
from collect_images.password import PasswordClient
from collect_images.recorder import Recorder
from collect_images.recovery import RecoveryClient
from collect_images.workflow import Workflow
from check_limits.__main__ import build_parser as analysis_parser, execute as analyze, load_settings
from check_limits.report import render_report
from check_limits.server import ensure_server
from check_limits.tools import apk_metadata


def prepare_apk(adb, package, supplied, root):
    paths = adb.apk_paths(package)
    expected = adb.apk_hash(paths[0])
    destination = supplied.resolve() if supplied else root / "input.apk"
    if supplied is None:
        print("[APK] 从当前手机提取已安装版本…", flush=True)
        adb.pull_apk(paths[0], destination)
    metadata = apk_metadata(destination)
    if metadata["sha256"] != expected or metadata["package"] != package:
        raise CollectorError("本地 APK 与手机当前安装文件不一致，未继续检测")
    return destination, metadata


def save_summary(recorder, value):
    recorder.save_json("result.json", value)
    collection = value.get("collection", {})
    analysis = value.get("analysis")
    if analysis and value.get("analysis_dir"):
        text = render_report(analysis)
        prefix = value["analysis_dir"]
        text = text.replace("](images/", "](" + prefix + "/images/")
        text = text.replace("](code_evidence/", "](" + prefix + "/code_evidence/")
        text = text.replace("详细规则、引文和调用轨迹见同目录 `result.json`、`rules.json`、`trace.jsonl`。",
                            "[详细分析与证据](" + prefix + "/result.json)")
    else:
        text = "# 未成年人模式时长核对\n\n" + value.get("summary", "尚未完成") + "\n\n未得到完整的静态核对结果，不作合规或运行效果结论。\n"
    recovery = collection.get("recovery", {})
    line = "全流程 %.1f 秒；采集 %s（%d 张图）；大模型脱困 %d 次，确认交回 Jev %d 次。" % (
        value["duration_seconds"], collection.get("status", "未开始"), collection.get("image_count", 0),
        recovery.get("delegations", 0), recovery.get("confirmed_handbacks", 0))
    text = text.replace("\n\n", "\n\n" + line + "\n\n", 1)
    if collection.get("status") and collection["status"] != "completed":
        text += "\n采集未完整完成：%s。本报告最多覆盖已采集页面，不能扩展为全部目标通过。\n" % collection.get("reason", "")
    if value.get("collection_dir"):
        text += "\n[采集过程与恢复记录](%s/result.json)\n" % value["collection_dir"]
    recorder._write(recorder.root / "report.md", text.encode("utf-8"))


def run(args):
    started, started_at = time.monotonic(), now()
    recorder = Recorder(args.output_dir.resolve())
    print("全流程输出：" + str(recorder.root), flush=True)
    value = {"status": "error", "started_at": started_at, "package": args.package, "collection": {}, "analysis": None}
    try:
        if not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", args.package):
            raise CollectorError("App 包名无效")
        load_env(args.env_file.resolve())
        cfg = load_config(args.collect_config.resolve())
        settings = load_settings(args.analysis_config.resolve())
        cfg.update(package=args.package, launch_app=args.launch)
        if args.max_steps is not None:
            if not 1 <= args.max_steps <= 60:
                raise CollectorError("全流程采集步数限 1—60")
            cfg["max_steps"] = args.max_steps
        if cfg["max_steps"] + 1 > settings["max_images"]:
            raise CollectorError("分析图片预算须至少为采集 max_steps+1，避免采完后遗漏页面")
        adb = ADB(cfg["adb_path"], args.serial or cfg.get("serial", ""))
        adb.select_device()
        apk, metadata = prepare_apk(adb, args.package, args.apk, recorder.root)
        value["apk"] = metadata
        recorder.save_json("apk.json", metadata)
        print("[采集] 启动 Jev；重复无进展时由大模型脱困…", flush=True)
        adb.wake()
        collection_recorder = Recorder(recorder.root / "collection")
        value["collection_dir"] = str(collection_recorder.root.relative_to(recorder.root))
        recovery = RecoveryClient(cfg["recovery"]) if cfg["recovery"]["enabled"] else None
        password = PasswordClient(cfg["password"]) if cfg["password"]["enabled"] else None
        collection = Workflow(cfg, adb, JevClient(cfg["jev"]), password, collection_recorder, recovery=recovery).run()
        value["collection"] = collection
        if collection["status"] in ("need_user", "error", "interrupted") or not collection["image_count"]:
            value.update(status=collection["status"], summary="采集停止：" + collection["reason"])
        else:
            print("[服务器] 自动核对或准备同一 APK 的 JADX 工程…", flush=True)
            value["server"] = ensure_server(apk, metadata, settings["jadx"])
            print("[核对] 自动读图、查代码并生成报告…", flush=True)
            analysis_args = analysis_parser().parse_args([
                "--apk", str(apk), "--images", str(collection_recorder.root),
                "--config", str(args.analysis_config.resolve()), "--env-file", str(args.env_file.resolve()),
                "--output-dir", str(recorder.root / "analysis")])
            result, analysis_root = analyze(analysis_args)
            value["analysis"] = result
            if analysis_root:
                value["analysis_dir"] = str(analysis_root.relative_to(recorder.root))
            value.update(status=result["status"], summary=result["summary"])
            if result["status"] == "completed" and collection["status"] != "completed":
                value["status"] = "partial"
    except KeyboardInterrupt:
        value.update(status="interrupted", summary="全流程被中断，已有证据保留。")
    except (CollectorError, OSError) as exc:
        value.update(status="need_user" if isinstance(exc, NeedUser) else "error",
                     summary=str(exc) if isinstance(exc, CollectorError) else "文件或进程操作失败；已有证据保留。")
    except Exception as exc:
        value.update(status="error", summary="内部错误（%s），已有证据保留。" % type(exc).__name__)
    value.update(finished_at=now(), duration_seconds=round(time.monotonic() - started, 3))
    save_summary(recorder, value)
    print("全流程结果：%s；%s" % (value["status"], value.get("summary", "")), flush=True)
    print("最终报告：" + str(recorder.root / "report.md"), flush=True)
    return value, recorder.root


def main(argv=None):
    parser = argparse.ArgumentParser(description="全自动：APK 校验/提取 → Jev 采集与大模型脱困 → JADX 核对 → 简短报告")
    parser.add_argument("--package", required=True)
    parser.add_argument("--serial")
    parser.add_argument("--apk", type=Path, help="可选：已有 APK；会与手机已安装文件校验，不提供则自动提取")
    parser.add_argument("--collect-config", type=Path, default=CODE_ROOT / "config.json")
    parser.add_argument("--analysis-config", type=Path, default=CODE_ROOT / "check_config.json")
    parser.add_argument("--env-file", type=Path, default=CODE_ROOT / ".env")
    parser.add_argument("--output-dir", type=Path, default=CODE_ROOT.parent / "results/runs")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--launch", action="store_true")
    value, _ = run(parser.parse_args(argv))
    return {"completed": 0, "partial": 2, "insufficient": 2, "incomplete": 2, "interrupted": 130}.get(value["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
