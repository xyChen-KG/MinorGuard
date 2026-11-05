"""Task orchestration and configuration."""

import json
import time
from pathlib import Path

from collect_images.common import CollectorError, NeedUser, finite_number, now, validate_url
from .prompts import JUDGE, RECOVERY, SCENARIOS
from .report import write_report
from .stages import STAGES, prompt_for


TASK_ROOT = Path(__file__).resolve().parent


def load_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CollectorError("无法读取退出测试配置") from None
    if not isinstance(config, dict):
        raise CollectorError("退出配置必须是对象")
    for k, low, high in (("max_seconds", 1, 7200), ("max_steps", 1, 200), ("max_requests", 1, 300),
                         ("phase_steps", 1, 100), ("history_limit", 1, 100)):
        if type(config.get(k)) is not int:
            raise CollectorError(k + "必须为整数")
        finite_number(config[k], k, low, high)
    for k in ("settle_seconds", "wait_seconds"):
        config[k] = finite_number(config.get(k), k, 0, 10)
    for k in ("adb_path", "output_dir"):
        if not isinstance(config.get(k), str) or not config[k].strip():
            raise CollectorError(k + "路径无效")
        config[k] = str((path.parent / config[k]).resolve())
    for section in ("jev", "password", "judge"):
        if not isinstance(config.get(section), dict):
            raise CollectorError("缺少配置：" + section)
        finite_number(config[section].get("timeout"), section + ".timeout", 1, 600)
    recovery = config.get("recovery")
    if not isinstance(recovery, dict) or type(recovery.get("enabled")) is not bool:
        raise CollectorError("缺少脱困配置或enabled无效")
    for key, low, high in (("repeat_actions", 2, 6), ("max_repeated_actions", 2, 6), ("max_actions", 1, 10), ("max_recoveries", 1, 10)):
        if type(recovery.get(key)) is not int or not low <= recovery[key] <= high:
            raise CollectorError("recovery." + key + "无效")
    if recovery["max_repeated_actions"] < recovery["repeat_actions"]:
        raise CollectorError("连续动作上限不能低于无变化重复次数")
    finite_number(recovery.get("unchanged_threshold"), "recovery.unchanged_threshold", 0, 30)
    finite_number(recovery.get("timeout"), "recovery.timeout", 1, 300)
    validate_url(config["jev"].get("endpoint", ""))
    if not isinstance(config["jev"].get("model"), str) or not config["jev"]["model"].strip():
        raise CollectorError("Jev模型名为空")
    for k in ("allow_setup", "existing_pin_is_all_ones"):
        if type(config["password"].get(k)) is not bool:
            raise CollectorError("password." + k + "必须为布尔值")
    rounds = config["password"].get("max_rounds")
    if type(rounds) is not int or not 1 <= rounds <= 30:
        raise CollectorError("密码流程轮数无效")
    judge = config["judge"]
    if judge.get("thinking") not in ("enabled", "disabled") or judge.get("reasoning_effort") not in ("low", "high", "max"):
        raise CollectorError("最终模型思考配置无效")
    if type(judge.get("max_images")) is not int or not config["max_steps"] + 1 <= judge["max_images"] <= 250:
        raise CollectorError("最终图片预算必须至少为max_steps+1；不允许默默漏图")
    return config


class Workflow:
    def __init__(self, config, collector, judge, recorder, clock=time.monotonic, static_analysis=None):
        self.config, self.collector, self.judge, self.recorder, self.clock = config, collector, judge, recorder, clock
        self.static_analysis = static_analysis

    def run(self):
        started = self.clock()
        device = self.collector.device
        bundle = {"package": device.package, "app_version": None,
                  "goal": "检查未成年人模式能否未经有效验证退出，仅覆盖规定的动态测试路径。",
                  "preparation": {"status": "skipped", "reason": "尚未开始"},
                  "scenarios": [{"id": i, "phase": p, "name": n, "status": "skipped", "reason": "前置阶段未完成",
                                 "before_image": None, "after_image": None, "event_ids": []} for i, p, n in SCENARIOS],
                  "images": self.collector.images, "events": self.collector.events}
        result = {"status": "error", "started_at": now(), "collection": bundle, "judgement": None}
        if self.static_analysis is not None:
            result["static_analysis"] = self.static_analysis.result
        self.recorder.save_json("run_config.json", self.config)
        self.recorder.save_json("prompt_snapshot.json", {"stages": STAGES, "stage_prompts": {key: prompt_for(key) for key in STAGES}, "recovery": RECOVERY, "judge": JUDGE})
        try:
            device.backend.select_device()
            bundle["device"] = device.backend.serial
            bundle["app_version"] = device.backend.app_version(device.package)
            device.backend.wake()  # Not unlock; not changing display timeout.
            device.set_phase("prepare")
            device.backend.launch(device.package)
            print("[退出测试] 准备：确认模式已开启…", flush=True)
            bundle["preparation"] = self.collector.run_phase("prepare")
            proceed = bundle["preparation"]["status"] == "collected"
            for scene in bundle["scenarios"]:
                if not proceed:
                    break
                if scene["phase"] == "positive" and self.static_analysis is not None:
                    if not self.static_analysis.probe(self.collector):
                        scene["reason"] = "候选复测后未确认模式仍开启，保留现场"
                        break
                print("[退出测试] " + scene["name"], flush=True)
                value = self.collector.run_phase(scene["phase"])
                scene.update(value)
                self.recorder.save_json("scenarios.json", {"preparation": bundle["preparation"], "scenarios": bundle["scenarios"]})
                # These are navigation observations, NOT bypass decisions. Do not
                # re-enable a mode observed off just to finish later tests.
                proceed = value["status"] == "collected" and value["mode_observation"] == "on"
            if bundle["preparation"]["status"] == "interrupted" or any(s["status"] == "interrupted" for s in bundle["scenarios"]):
                result.update(status="interrupted", error="采集已中断，不继续请求最终模型")
            else:
                result["status"] = "collected" if all(s["status"] == "collected" for s in bundle["scenarios"]) else "partial"
        except KeyboardInterrupt:
            result.update(status="interrupted", error="用户中断；已保存证据，不继续操作或请求模型")
        except Exception as error:
            result.update(status="need_user" if isinstance(error, NeedUser) else "error",
                          error=str(error) if isinstance(error, CollectorError) else "采集内部错误（%s），已有记录保留" % type(error).__name__)
        finally:
            result["collection_stats"] = {"actions": self.collector.steps, "requests": self.collector.requests,
                                          "seconds": round(self.clock() - started, 3),
                                          "recovery": {"delegations": self.collector.recovery_count, "model_calls": self.collector.recovery_calls,
                                                       "confirmed_handbacks": self.collector.handbacks}}
            bundle["pin_metadata"] = {"policy": "all_ones_test_only", "length": device.pin_length,
                                      "attempted_stages": sorted(device.attempted), "entered_stages": sorted(device.entered)}
            self.recorder.save_json("collection.json", bundle)
            self.recorder.save_json("scenarios.json", {"preparation": bundle["preparation"], "scenarios": bundle["scenarios"]})
        if bundle["images"] and result["status"] != "interrupted":
            try:
                print("[退出测试] DeepSeek根据截图和完整操作记录做最终判断…", flush=True)
                result["judgement"] = self.judge.judge(bundle, self.recorder.root)
                result["judge_stats"] = self.judge.stats
                result["status"] = "completed" if all(s["status"] == "collected" for s in bundle["scenarios"]) else "partial"
                if self.static_analysis is not None and not self.static_analysis.completed():
                    result["status"] = "partial"
            except KeyboardInterrupt:
                result.update(status="interrupted", error="最终判断被中断；已有记录保留")
            except Exception as error:
                result.update(status="error", error=str(error) if isinstance(error, CollectorError) else "最终判定失败（%s）" % type(error).__name__)
        last = bundle["images"][-1].get("navigation_observation", {}) if bundle["images"] else {}
        explicit = last.get("view") == "mode_status" or last.get("view") == "mode_intro" and last.get("mode_state") == "off"
        result["last_mode_observation"] = last.get("mode_state", "unknown") if explicit else "unknown"
        result.update(finished_at=now(), duration_seconds=round(self.clock() - started, 3))
        write_report(self.recorder, result)
        return result
