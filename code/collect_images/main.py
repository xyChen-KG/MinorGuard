"""Run with: python -m collect_images --help (working directory: code)."""

import argparse
import json
import os
from pathlib import Path

from .adb import ADB
from .common import CODE_ROOT, CollectorError, finite_number, load_config, load_env
from .jev import JevClient
from .password import PasswordClient
from .recovery import RecoveryClient
from .recorder import Recorder
from .workflow import Workflow


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Jev 驱动的轻量 App 图片采集器；不执行合规判定")
    parser.add_argument("--config", type=Path, default=CODE_ROOT / "config.json")
    parser.add_argument("--env-file", type=Path, default=CODE_ROOT / ".env")
    parser.add_argument("--serial", help="ADB 设备序列号，或已经连接的 IP:端口")
    parser.add_argument("--package", help="被测 App 包名")
    parser.add_argument("--goal", help="本轮采集目标；覆盖时清空配置中的默认采集清单")
    parser.add_argument("--need", action="append", help="需要采集的页面说明，可重复；覆盖默认清单")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--launch", action="store_true", help="先启动被测 App；默认从当前页面继续")
    parser.add_argument("--check", action="store_true", help="只检查配置与设备，不截图、不调用模型、不操作页面")
    args = parser.parse_args(argv)
    try:
        load_env(args.env_file.resolve())
        config = load_config(args.config.resolve())
        if args.package:
            import re
            if not re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", args.package):
                raise CollectorError("App 包名格式不正确")
            config["package"] = args.package
        if args.goal is not None:
            if not args.goal.strip():
                raise CollectorError("采集目标不能为空")
            config.update(goal=args.goal, evidence=[])
        if args.need is not None:
            if len(args.need) > 20 or any(not text.strip() for text in args.need):
                raise CollectorError("--need 不得为空且最多 20 项")
            config["evidence"] = args.need
        for name, high in (("max_steps", 500), ("max_seconds", 7200)):
            override = getattr(args, name)
            if override is not None:
                finite_number(override, name, 1, high)
                config[name] = override
        config["launch_app"] = args.launch
        adb = ADB(config["adb_path"], args.serial or config.get("serial", ""))
        if args.check:
            serial = adb.select_device()
            print(json.dumps({"device": serial, "package": config["package"],
                "typesafe_key_configured": bool(os.environ.get("TYPESAFE_API_KEY")),
                "password_api_configured": all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")),
                "output_dir": config["output_dir"], "note": "未截图、未调用模型、未操作页面"}, ensure_ascii=False, indent=2))
            return 0
        jev = JevClient(config["jev"])
        password = PasswordClient(config["password"]) if config["password"]["enabled"] else None
        recorder = Recorder(Path(config["output_dir"]))
        print("输出目录：%s" % recorder.root, flush=True)
        recovery = RecoveryClient(config["recovery"]) if config["recovery"]["enabled"] else None
        result = Workflow(config, adb, jev, password, recorder, recovery=recovery).run()
        print("采集结果：%s；%s；用时 %.1f 秒" % (result["status"], result["reason"], result["duration_seconds"]))
        print("结果文件：%s" % (recorder.root / "result.json"))
        return {"completed": 0, "insufficient": 2, "need_user": 2, "interrupted": 130}.get(result["status"], 1)
    except CollectorError as exc:
        print("无法启动：%s" % exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
