"""Jev navigation with bounded visual recovery and a restricted PIN branch."""

import time
from collections import deque
from typing import Any, Dict, Optional

from .common import CollectorError, Frame, NeedUser, box_pixels, frame_difference, now


class BudgetReached(CollectorError):
    pass


class Workflow:
    def __init__(self, config, adb, jev, password, recorder, sleeper=time.sleep, clock=time.monotonic, recovery=None):
        self.config, self.adb, self.jev = config, adb, jev
        self.password, self.recorder = password, recorder
        self.sleep, self.clock = sleeper, clock
        self.current = None  # type: Optional[Frame]
        self.frame_number = 0
        self.steps = 0
        self.history = []
        self.collected = {}  # index -> first supporting frame
        self.no_progress = 0
        self.started = 0.0
        self.password_stages = set()
        self.password_attempts = set()
        self.pin_length = None
        self.delegations = 0
        self.recovery = recovery
        self.recent_frames = deque(maxlen=3)
        self.repeated_action = None
        self.repeated_count = 0
        self.recovery_count = 0
        self.recovery_calls = 0
        self.handbacks = 0
        self.recovery_hint = ""

    def _budget(self) -> float:
        remaining = self.config["max_seconds"] - (self.clock() - self.started)
        if self.steps >= self.config["max_steps"] or remaining <= 0:
            raise BudgetReached("达到本轮步数或总时间上限")
        return remaining

    def _capture(self, settle: bool = True) -> None:
        if settle:
            remaining = self.config["max_seconds"] - (self.clock() - self.started)
            self.sleep(max(0, min(self.config["settle_seconds"], remaining)))
        frame = self.adb.screenshot()
        stable = False
        for _ in range(self.config["stability_checks"]):
            if self.clock() - self.started >= self.config["max_seconds"]:
                break
            self.sleep(.25)
            next_frame = self.adb.screenshot()
            stable = frame_difference(frame, next_frame) <= self.config["stability_threshold"]
            frame = next_frame
            if stable:
                break
        if not self.config["stability_checks"]:
            stable = True
        self.current = Frame(frame.png, frame.width, frame.height, frame.captured_at, stable)
        self.recent_frames.append(self.current)
        self.frame_number += 1
        self.recorder.save_frame(self.frame_number, self.current)

    def _context(self) -> Dict[str, Any]:
        return {
            "goal": self.config["goal"], "package": self.config["package"],
            "requirements": self.config["evidence"],
            "collected": [{"requirement": self.config["evidence"][i], "image": image}
                          for i, image in sorted(self.collected.items())],
            "missing": [text for i, text in enumerate(self.config["evidence"]) if i not in self.collected],
            "history": self.history[-self.config["history_limit"]:],
            "consecutive_unchanged": self.no_progress,
            "recovery_hint": self.recovery_hint,
            "steps_remaining": max(0, self.config["max_steps"] - self.steps),
            "password_policy": "仅模式独立密码，全1；账号/支付/验证码不得处理",
            "allow_setup": self.config["password"]["allow_setup"],
        }

    def _require_visible(self):
        if self.current is not None and self.current.image().getbbox() is None:
            raise NeedUser("截图为全黑：请检查休眠/锁屏或 App 禁止截图；未把不可见页面当作可测试证据")

    def _record(self, value: Dict[str, Any]) -> None:
        self.recorder.append(value)
        self.history.append({key: value[key] for key in
            ("step", "actor", "action", "params", "before_image", "after_image", "status", "changed") if key in value})
        print("[%02d] %s / %s → %s" % (value["step"], value["actor"], value["action"], value["status"]), flush=True)

    def _event(self, actor: str, action: str, params=None, status="recorded", decision=None) -> None:
        self._budget()
        self.steps += 1
        image = self.recorder.frame_path(self.frame_number) if self.current else None
        self._record({"step": self.steps, "actor": actor, "action": action,
                      "params": params or {}, "status": status, "time": now(),
                      "before_image": image, "after_image": image, "decision": decision or {}})

    def _perform(self, actor: str, action: str, params=None, decision=None, progress=True) -> bool:
        self._budget()
        params = params or {}
        before, before_number = self.current, self.frame_number
        if before is None:
            raise CollectorError("操作前缺少截图")
        # Execute the selected action. A banner animation is not a reason to cancel a tap.
        self.steps += 1
        started = self.clock()
        record = {"step": self.steps, "actor": actor, "action": action, "params": dict(params),
                  "observed_at": before.captured_at, "executed_at": now(),
                  "before_image": self.recorder.frame_path(before_number), "after_image": None,
                  "before_observation_sha256": before.sha256, "decision": decision or {},
                  "status": "failed_or_unknown"}
        try:
            if action == "tap":
                edges = box_pixels(params.get("box"), before.width, before.height)
                x, y = (edges[0] + edges[2]) // 2, (edges[1] + edges[3]) // 2
                record["params"]["point"] = [x, y]
                self.recorder.mark_tap(before_number, before, params["box"])
                self.adb.tap(x, y)
            elif action == "swipe":
                self.adb.swipe(before.width, before.height, params["direction"], params["length"])
            elif action == "back":
                self.adb.back()
            elif action == "wait":
                self.sleep(min(self.config["wait_seconds"], max(0, self.config["max_seconds"] - (self.clock() - self.started))))
            elif action == "enter_pin":
                record["params"]["value"] = "[REDACTED]"
                self.adb.enter_test_pin(params["length"])
            elif action == "launch":
                self.adb.launch(self.config["package"])
            else:
                raise CollectorError("不允许执行此动作")
            record["status"] = "executed"
            self._capture()
            record.update(after_image=self.recorder.frame_path(self.frame_number),
                          after_observation_sha256=self.current.sha256, after_stable=self.current.stable,
                          changed=frame_difference(before, self.current) > self.config["stability_threshold"])
            if progress:
                self.no_progress = 0 if record["changed"] else self.no_progress + 1
                key = None
                if action == "swipe":
                    key = (action, params["direction"])
                elif action == "tap":
                    b = params["box"]
                    key = (action, round((b["left"] + b["right"]) * 10), round((b["top"] + b["bottom"]) * 10))
                elif action == "back":
                    key = (action,)
                if key is not None and not record["changed"]:
                    self.repeated_count = self.repeated_count + 1 if key == self.repeated_action else 1
                    self.repeated_action = key
                else:
                    self.repeated_action, self.repeated_count = None, 0
            return True
        finally:
            record.update(finished_at=now(), duration_seconds=round(self.clock() - started, 3))
            if record["status"] == "executed" and record["after_image"] is None:
                record["status"] = "executed_but_after_capture_missing"
            self._record(record)

    def _check_password_stage(self, stage: str) -> None:
        cfg = self.config["password"]
        if stage == "setup" and not cfg["allow_setup"]:
            raise NeedUser("本轮没有授权设置模式独立密码")
        if stage == "confirm" and "setup" not in self.password_stages:
            raise NeedUser("未在本轮设置密码，不自动完成未知的确认流程")
        if stage == "unlock" and not (cfg["existing_pin_is_all_ones"] or "setup" in self.password_stages):
            raise NeedUser("已有模式密码未知；请先确认它是全 1，再启用相应配置")

    def _recover(self) -> None:
        cfg = self.config["recovery"]
        if self.recovery_count >= cfg["max_recoveries"]:
            raise BudgetReached("多次出现重复停滞且达到本轮脱困预算；未判断 App 不合规")
        self.recovery_count += 1
        self._event("controller", "delegate_recovery", {"repeat_count": self.repeated_count}, status="delegated")
        actions = 0
        while True:
            self._require_visible()
            context = dict(self._context(), recent_frames=list(self.recent_frames), after_action=actions > 0,
                           recovery_actions=actions, actions_remaining=max(0, cfg["max_actions"] - actions),
                           trigger="Jev 重复同一类动作，页面主体没有推进")
            self.recovery_calls += 1
            decision = self.recovery.decide(self.current, context, self._budget())
            action = decision["action"]
            if action == "hand_back":
                if actions == 0 or decision.get("progress_confirmed") is not True:
                    raise CollectorError("没有操作后确认，不交回 Jev")
                self.recovery_hint = decision.get("hint") or decision.get("reason", "")
                self.handbacks += 1
                self.no_progress, self.repeated_count, self.repeated_action = 0, 0, None
                self._event("recovery_llm", "return_to_jev", {"hint": self.recovery_hint}, status="confirmed", decision=decision)
                return
            if action == "stop":
                self._event("recovery_llm", "stop", status="stopped", decision=decision)
                if decision.get("stop_reason") == "need_user":
                    raise NeedUser(decision.get("reason") or "脱困需要用户处理登录/验证")
                raise BudgetReached(decision.get("reason") or "大模型仍未找到有效导航路径")
            if actions >= cfg["max_actions"]:
                raise BudgetReached("大模型脱困动作预算已用完，但尚未确认脱困；没有强行交回 Jev")
            if action == "handle_password":
                self._event("recovery_llm", action, status="delegated", decision=decision)
                self._handle_password(return_actor="recovery")
            else:
                params = {k: decision[k] for k in ("box", "direction", "length") if k in decision}
                self._perform("recovery_llm", action, params, decision, progress=False)
            actions += 1

    def _handle_password(self, return_actor="jev") -> None:
        if self.password is None or not self.config["password"]["enabled"]:
            raise NeedUser("密码大模型分支未启用")
        self.delegations += 1
        if self.delegations > 3:
            raise NeedUser("多次进入密码流程，停止以避免重复尝试")
        for _ in range(self.config["password"]["max_rounds"]):
            remaining = self._budget()
            context = {"package": self.config["package"], "allow_setup": self.config["password"]["allow_setup"],
                       "existing_pin_is_all_ones": self.config["password"]["existing_pin_is_all_ones"],
                       "entered_stages": sorted(self.password_stages),
                       "attempted_stages": sorted(self.password_attempts), "pin_length": self.pin_length,
                       "history": self.history[-8:]}
            decision = self.password.decide(self.current, context, remaining)
            action = decision["action"]
            if action == "done":
                self._event("password_llm", "return_to_" + return_actor, status="returned")
                self.no_progress = 0
                return
            if action == "wait":
                self._perform("password_llm", "wait", progress=False)
                continue
            stage = decision["stage"]
            self._check_password_stage(stage)
            if action == "tap":
                self._perform("password_llm", "tap", {"box": decision["box"], "stage": stage}, progress=False)
                continue
            if action != "enter_pin":
                raise CollectorError("密码分支只允许受限操作")
            if stage in self.password_attempts:
                raise NeedUser("同一密码阶段已经输入过，未再次尝试")
            length = decision["length"]
            if self.pin_length is not None and self.pin_length != length:
                raise NeedUser("前后密码长度不一致，未输入")
            if decision["method"] == "text":
                focused = self._perform("password_llm", "tap", {"box": decision["box"], "stage": stage,
                                         "purpose": "focus_pin_field"}, progress=False)
                if not focused:
                    continue
            self.password_attempts.add(stage)  # A partial failure must not trigger a retry.
            self.pin_length = length
            if decision["method"] == "keypad":
                for index in range(length):
                    executed = self._perform("password_llm", "tap", {"box": decision["box"],
                        "stage": stage, "purpose": "pin_digit", "digit_index": index + 1,
                        "length": length}, progress=False)
                    if not executed:
                        raise NeedUser("密码键盘在输入期间变化，停止，不自动补输或重试")
            else:
                self._perform("password_llm", "enter_pin", {"length": length, "stage": stage}, progress=False)
            self.password_stages.add(stage)  # All digits were sent; not proof the App accepted them.
        raise NeedUser("密码处理达到轮数上限，交回用户")

    def run(self) -> Dict[str, Any]:
        self.started = self.clock()
        started_at = now()
        status, reason, version = "error", "尚未开始", None
        self.recorder.save_json("run_config.json", self.config)
        try:
            self.adb.select_device()
            version = self.adb.app_version(self.config["package"])
            self._capture(settle=False)
            self._require_visible()
            if self.config.get("launch_app", False):
                self._perform("controller", "launch")
            while True:
                self._require_visible()
                if self.recovery is not None and self.config.get("recovery", {}).get("enabled") and self.repeated_count >= self.config["recovery"]["repeat_actions"]:
                    self._recover()
                remaining = self._budget()
                decision_started = self.clock()
                decision_started_at = now()
                decision = self.jev.decide(self.current, self._context(), remaining)
                decision.update(decision_started_at=decision_started_at,
                                decision_seconds=round(self.clock() - decision_started, 3))
                for index in decision.get("evidence", []):
                    if type(index) is int and 0 <= index < len(self.config["evidence"]):
                        self.collected.setdefault(index, self.recorder.frame_path(self.frame_number))
                action = decision["action"]
                if action == "stop":
                    reported_stop = decision["stop_reason"]
                    status = reported_stop
                    # Independent Jev answers can conflict. Completion is based on
                    # its explicit goal confirmation AND every required screenshot,
                    # never on the stop label alone. Do not override auth stops.
                    if (status == "insufficient" and self.config["evidence"] and decision.get("goal_done") is True
                            and len(self.collected) == len(self.config["evidence"])):
                        status = "completed"
                        decision["stop_label_normalized"] = True
                    if status == "completed" and (not decision.get("goal_done") or
                            len(self.collected) != len(self.config["evidence"])):
                        status = "insufficient"
                        reason = "Jev 请求结束，但缺少采集项或未确认目标完成，未计为成功"
                    else:
                        reason = {"completed": "Jev 确认采集目标完成，且采集清单已有截图支持",
                                  "insufficient": "Jev 判断暂时没有足够的目标页面证据",
                                  "need_user": "Jev 判断需要用户介入"}[status]
                    self._event("jev", "stop", {"reason": status, "reported_reason": reported_stop}, status="stopped", decision=decision)
                    break
                if action == "handle_password":
                    self._event("jev", action, status="delegated", decision=decision)
                    self._handle_password()
                else:
                    params = {key: decision[key] for key in ("box", "direction", "length") if key in decision}
                    self._perform("jev", action, params, decision)
        except BudgetReached as exc:
            status, reason = "insufficient", str(exc)
        except NeedUser as exc:
            status, reason = "need_user", str(exc)
        except CollectorError as exc:
            status, reason = "error", str(exc)
        except KeyboardInterrupt:
            status, reason = "interrupted", "用户中断；已保存现有图片和轨迹"
        except Exception as exc:
            status, reason = "error", "内部错误（%s）；已保存现有轨迹" % type(exc).__name__
        result = {
            "status": status, "reason": reason, "goal": self.config["goal"],
            "package": self.config["package"], "app_version": version, "device": self.adb.serial,
            "started_at": started_at, "finished_at": now(),
            "duration_seconds": round(self.clock() - self.started, 3),
            "steps": self.steps, "image_count": self.frame_number,
            "final_image": self.recorder.frame_path(self.frame_number) if self.current else None,
            "evidence": [{"requirement": text, "collected": i in self.collected,
                          "image": self.collected.get(i)} for i, text in enumerate(self.config["evidence"])],
            "mode_pin": {"policy": "all_ones_test_only", "length": self.pin_length,
                         "attempted_stages": sorted(self.password_attempts),
                         "entered_stages": sorted(self.password_stages)},
            "recovery": {"delegations": self.recovery_count, "model_calls": self.recovery_calls,
                         "confirmed_handbacks": self.handbacks, "last_hint": self.recovery_hint},
            "limitations": ["本结果只说明页面采集情况，不是合规判定。",
                            "模型判断和目标框可能有误；置信度仅记录，不作为执行门槛。",
                            "点击图带标注；模型使用内存中的原图，不会读取标注图。",
                            "未自动退出模式或恢复设置；请检查测试后的手机状态。"],
        }
        self.recorder.save_json("result.json", result)
        return result
