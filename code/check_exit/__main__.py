"""Run from code/: .venv/bin/python -m check_exit --package com.dragon.read"""

import argparse
import json
import os
from pathlib import Path

from collect_images.adb import ADB
from collect_images.common import CODE_ROOT, CollectorError, load_env, validate_url
from collect_images.password import PasswordClient
from collect_images.recorder import Recorder
from .collector import Collector, JevNavigator
from .device import Device
from .judge import Judge
from .recovery import RecoveryNavigator
from .report import render_static
from .workflow import TASK_ROOT, Workflow, load_config


def model_preflight():
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise CollectorError("请在现有code/.env配置TYPESAFE_API_KEY")
    for prefix, fallback in (("LLM_", "LLM_"), ("CHECK_LLM_", "LLM_")):
        base = os.environ.get(prefix + "BASE_URL", os.environ.get(fallback + "BASE_URL", ""))
        key = os.environ.get(prefix + "API_KEY", os.environ.get(fallback + "API_KEY", ""))
        model = os.environ.get(prefix + "MODEL", os.environ.get(fallback + "MODEL", ""))
        if not all((base, key, model)):
            raise CollectorError("缺少密码/最终判断模型接口配置；未操作手机")
        if not key.strip() or "\r" in key or "\n" in key:
            raise CollectorError("模型密钥格式无效；未操作手机")
        validate_url(base)


def main(argv=None):
    parser = argparse.ArgumentParser(description="未成年人模式退出测试，可选 APK 静态旁路扫描")
    parser.add_argument("--package", help="被测 App 包名；仅分析现有 APK 时可从文件读取")
    parser.add_argument("--serial", default="", help="已经连接的ADB设备，未指定时须只有一个在线设备")
    parser.add_argument("--config", type=Path, default=TASK_ROOT / "config.json")
    parser.add_argument("--env-file", type=Path, default=CODE_ROOT / ".env")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--existing-pin-all-ones", action="store_true", help="仅在你明确确认已有独立模式密码全为1时使用；不授权负向阶段输入")
    parser.add_argument("--no-setup", action="store_true", help="不设置新密码；需已开启模式或由用户处理")
    parser.add_argument("--check", action="store_true", help="只读检查配置、密钥是否存在、设备和App版本，不截图/不操作/不请求模型")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--static", action="store_true", help="先做 APK 静态扫描，再做真机退出测试")
    modes.add_argument("--static-only", action="store_true", help="只扫描候选；同时指定 --apk 时无需连接手机")
    parser.add_argument("--apk", type=Path, help="已有 APK；与 --static 一起使用时须和手机安装文件哈希一致")
    parser.add_argument("--analysis-config", type=Path, default=CODE_ROOT / "check_config.json", help="复用现有 JADX/模型配置")
    parser.add_argument("--replay-uri", action="append", default=[], help="--static 下允许真机重放的完整候选深链接，最多 3 个")
    args = parser.parse_args(argv)
    use_static = args.static or args.static_only
    apk_only = args.static_only and args.apk is not None
    if args.check and use_static or args.apk is not None and not use_static:
        parser.error("--check 只检查动态配置，--apk 只与静态扫描选项一起使用")
    if not apk_only and not args.package:
        parser.error("除 --static-only --apk 外，需要指定 --package")
    if apk_only and args.serial:
        parser.error("仅分析现有 APK 时无需指定 --serial")
    if args.replay_uri and (not args.static or len(args.replay_uri) > 3):
        parser.error("--replay-uri 只用于 --static，最多 3 个")
    try:
        config = load_config(args.config.resolve())
        load_env(args.env_file.resolve())
        if args.existing_pin_all_ones:
            config["password"]["existing_pin_is_all_ones"] = True
        if args.no_setup:
            config["password"]["allow_setup"] = False
        if args.output_dir:
            config["output_dir"] = str(args.output_dir.resolve())
        if not args.static_only:
            model_preflight()
        if use_static:
            from check_limits.__main__ import load_settings
            from .static_analysis import StaticAnalysis, plain_uri
            if any(not plain_uri(uri) for uri in args.replay_uri):
                parser.error("--replay-uri 需是无参数和片段的完整深链接")
            settings = load_settings(args.analysis_config.resolve())
        backend, version = None, None
        if not apk_only:
            backend = ADB(config["adb_path"], args.serial)
            device = Device(backend, args.package, config["password"])
            backend.select_device()
            version = backend.app_version(args.package)
            if not version:
                raise CollectorError("未确认被测App已安装及版本；未操作设备")
        if args.check:
            print(json.dumps({"device": backend.serial, "package": device.package, "app_version": version,
                              "model_keys_present": True, "output_dir": config["output_dir"],
                              "note": "未截图、未操作手机、未调用模型、未连接分析服务器"}, ensure_ascii=False, indent=2))
            return 0
        recorder = Recorder(Path(config["output_dir"]))
        print("退出测试输出：" + str(recorder.root), flush=True)
        analysis = None
        if use_static:
            analysis = StaticAnalysis(settings, backend, args.package if apk_only else device.package, version,
                                      recorder.root, args.apk, args.replay_uri)
            static_result = analysis.scan()
            if args.static_only or static_result["status"] == "interrupted":
                recorder.save_json("result.json", {"status": static_result["status"], "static_analysis": static_result})
                report = render_static(static_result, heading="# 未成年人模式静态旁路分析")
                recorder._write(recorder.root / "report.md", "\n".join(report).encode("utf-8"))
                print("报告：" + str(recorder.root / "report.md"), flush=True)
                return {"completed": 0, "incomplete": 2, "interrupted": 130}.get(static_result["status"], 1)
        recovery = RecoveryNavigator(config["recovery"]) if config["recovery"]["enabled"] else None
        collector = Collector(config, device, JevNavigator(config["jev"]), PasswordClient(config["password"]), recorder, recovery=recovery)
        result = Workflow(config, collector, Judge(config["judge"]), recorder, static_analysis=analysis).run()
        print("执行状态：" + result["status"], flush=True)
        print("报告：" + str(recorder.root / "report.md"), flush=True)
        return {"completed": 0, "partial": 2, "interrupted": 130}.get(result["status"], 1)
    except CollectorError as error:
        print("无法启动：" + str(error))
        return 1
    except OSError:
        print("无法启动：配置文件或设备进程不可访问；未输出密钥")
        return 1
    except KeyboardInterrupt:
        print("已中断；未继续启动检测")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
