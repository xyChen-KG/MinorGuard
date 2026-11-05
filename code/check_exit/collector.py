"""Task-local Jev navigation and evidence capture, without final verdicts."""

import io
import time
from collections import deque

from PIL import ImageDraw

from collect_images.common import CollectorError, NeedUser, box_pixels, frame_difference, now
from .navigator import JevNavigator
from .stages import INITIAL, STAGES


class CollectionLimit(CollectorError):
    pass


class Collector:
    def __init__(self, config, device, navigator, password, recorder, clock=time.monotonic, sleeper=time.sleep, recovery=None):
        self.config, self.device, self.navigator, self.password, self.recorder = config, device, navigator, password, recorder
        self.clock, self.sleep = clock, sleeper
        self.started = clock()
        self.steps = self.requests = 0
        self.current = None
        self.current_id = None
        self.images, self.events = [], []
        self.recovery = recovery
        self.recent_frames = deque(maxlen=3)
        self.nav_history = deque(maxlen=6)
        self.recovery_count = self.recovery_calls = self.handbacks = 0
        self.recovery_hint = ""
        self.phase_step_start = 0
        self.stage = None
        self.stage_records = []
        self.pending_recovery = None
        self.reopens = 0
        self.keyboard_hides = 0

    def remaining(self):
        return self.config["max_seconds"] - (self.clock() - self.started)

    def guard(self, request=False):
        if self.remaining() <= 0 or self.steps >= self.config["max_steps"]:
            raise CollectionLimit("达到采集总时间或动作预算；未判断App不合规")
        if self.steps - self.phase_step_start >= self.config["phase_steps"]:
            raise CollectionLimit("达到本阶段动作预算")
        if request:
            if self.requests >= self.config["max_requests"]:
                raise CollectionLimit("达到导航/密码/脱困请求预算")
            self.requests += 1

    def event(self, action, status, before=None, after=None, **extra):
        entry = {"id": "T%03d" % (len(self.events) + 1), "phase": self.device.phase,
                 "stage": self.stage,
                 "action": action, "status": status, "time": now(), "before_image": before, "after_image": after, **extra}
        self.events.append(entry)
        self.recorder.append(entry)
        self.recorder.save_json("images.json", {"images": self.images})
        return entry

    def capture(self, settle=True):
        if settle:
            self.sleep(max(0, min(self.config["settle_seconds"], self.remaining())))
        # Only retry the read. Repeating the preceding tap/PIN/restart could
        # change the experiment or submit a password twice.
        for attempt in range(2):
            try:
                frame = self.device.backend.screenshot()
                break
            except CollectorError:
                if attempt or self.remaining() <= 0:
                    raise NeedUser("截图传输或解码连续失败；未重复前一步操作，请检查设备连接及页面截图能力") from None
                self.event("capture_retry", "retrying", self.current_id, None,
                           reason="截图读取失败，仅重试截图，不重试点击、密码输入或重启")
                self.sleep(max(0, min(1, self.remaining())))
        if frame.image().getbbox() is None:
            raise NeedUser("截图全黑；可能锁屏或禁止截图，不能用作证据")
        number = len(self.images) + 1
        relative = self.recorder.save_frame(number, frame)
        self.current, self.current_id = frame, "I%03d" % number
        self.recent_frames.append(frame)
        self.images.append({"id": self.current_id, "file": relative, "phase": self.device.phase,
                            "stage": self.stage,
                            "captured_at": frame.captured_at, "sha256": frame.sha256,
                            "width": frame.width, "height": frame.height})

    def _mark(self, box):
        # Keep the raw image immutable for final judgement; annotate a separate file.
        picture = self.current.image()
        draw = ImageDraw.Draw(picture)
        edges = box_pixels(box, self.current.width, self.current.height)
        draw.rectangle(edges, outline="#ff253a", width=max(2, self.current.width // 300))
        x, y = (edges[0] + edges[2]) // 2, (edges[1] + edges[3]) // 2
        draw.line((x - 6, y, x + 6, y), fill="#ff253a", width=3)
        draw.line((x, y - 6, x, y + 6), fill="#ff253a", width=3)
        raw = io.BytesIO()
        picture.save(raw, format="PNG")
        path = "images/%03d-tap.png" % len(self.images)
        self.recorder._write(self.recorder.root / path, raw.getvalue())
        return path

    def _uncover_secure_keyboard(self):
        """Dismiss a confirmed visible secure IME if it obscures the capture.

        This is an ordinary Back, not a security-setting change or a blind PIN
        action. Keep both the black frame and the resulting frame as evidence.
        """
        body = self.current.image().crop((0, int(self.current.height * .06),
                                          self.current.width, int(self.current.height * .94)))
        if body.getbbox() is not None or not self.device.visible_secure_keyboard():
            return False
        if self.device.window_info()["package"] != self.device.package:
            raise NeedUser("黑屏时未确认被测App前台，不自动收起键盘")
        if self.keyboard_hides >= 8:
            raise NeedUser("多次收起安全键盘后仍无法稳定采集；停止操作")
        self.keyboard_hides += 1
        self.perform("hide_secure_keyboard", self.device.backend.back,
                     {"reason": "内容区全黑且系统确认安全输入法窗口可见；收起键盘恢复截图"})
        body = self.current.image().crop((0, int(self.current.height * .06),
                                          self.current.width, int(self.current.height * .94)))
        if body.getbbox() is None:
            raise NeedUser("收起安全键盘后内容仍全黑，无法读取页面；未输入密码")
        self.nav_history.clear()
        return True

    def perform(self, action, operation, params=None, box=None, actor="controller"):
        self.guard()
        self.steps += 1
        before, after, before_frame = self.current_id, None, self.current
        marked = self._mark(box) if box else None
        status = "failed_or_unknown"
        try:
            operation()
            status = "executed_after_capture_missing"
            self.capture()
            after, status = self.current_id, "executed"
            if actor in ("jev", "recovery_llm") and action in ("tap", "swipe", "back"):
                key = (action,)
                if action == "swipe":
                    key += (params["direction"],)
                elif action == "tap":
                    key += (round((box["left"] + box["right"]) * 10), round((box["top"] + box["bottom"]) * 10))
                self.nav_history.append({"key": key, "difference": frame_difference(before_frame, self.current), "before": before_frame})
        finally:
            self.event(action, status, before, after, actor=actor, params=dict(params or {}, **({"box": box} if box else {})), annotated_image=marked)
            print("[%02d] %s / %s → %s" % (self.steps, actor, action, status), flush=True)

    def _context(self, phase, observation, stage_events):
        spec = STAGES[self.stage]
        return {"package": self.device.package, "phase": phase, "stage": self.stage,
                "phase_goal": spec["goal"], "current_stage": spec,
                "history": [e for e in stage_events if e.get("stage") == self.stage][-self.config["history_limit"]:],
                "stage_actions": [e["action"] for e in stage_events if e["status"] == "executed"],
                "exit_attempted": any(e["action"] == "tap" and e.get("params", {}).get("purpose") == "exit_entry"
                                      and e["status"] == "executed" for e in stage_events),
                "password_allowed": phase == "prepare" and self.config["password"]["allow_setup"] or phase == "positive" and self.device.known_pin,
                "allow_setup": self.config["password"]["allow_setup"],
                "known_test_pin": self.device.known_pin, "last_observation": observation,
                "recovery_hint": self.recovery_hint,
                "navigation_progress": [{"action": list(h["key"]), "image_difference": round(h["difference"], 3)} for h in self.nav_history]}

    def _enter_stage(self, stage_id):
        if stage_id not in STAGES or STAGES[stage_id]["phase"] != self.device.phase:
            raise CollectorError("子阶段与测试场景不匹配")
        self.stage = stage_id
        self.nav_history.clear()
        self.recovery_hint = ""
        self.pending_recovery = None
        self.stage_records.append({"stage": stage_id, "phase": self.device.phase, "name": STAGES[stage_id]["name"],
                                   "status": "running", "before_image": self.current_id, "after_image": None})
        self.event("stage_enter", "recorded", self.current_id, self.current_id, goal=STAGES[stage_id]["goal"])
        print("[阶段] " + STAGES[stage_id]["name"], flush=True)

    def _next_stage(self, stage_id, reason):
        self.stage_records[-1].update(status="collected", after_image=self.current_id, reason=reason)
        self.event("stage_complete", "recorded", self.current_id, self.current_id, reason=reason)
        if stage_id:
            self._enter_stage(stage_id)

    def _route(self, decision):
        """Advance only from this checkpoint's visible evidence or actual events."""
        stage, view = self.stage, decision["view"]
        if view == "mode_password" and decision["scope"] == "requires_user" and stage in ("enable_mode", "positive_authorize"):
            raise NeedUser("当前密码页需要用户处理，未交密码工具")
        if view == "mode_password" and stage in ("find_settings", "find_mode"):
            if decision["mode_state"] == "on" or not self.config["password"]["allow_setup"]:
                raise NeedUser("寻找模式入口时已处于模式验证/使用限制密码页；本阶段不解锁，无法到达退出测试起点")
            backs = sum(e["action"] == "leave_unfinished_setup" and e["phase"] == "prepare" for e in self.events)
            if backs >= 2:
                raise NeedUser("两次返回仍停留在模式密码页，未确认是未完成设置；停止循环")
            self.perform("back", self.device.backend.back)
            self.event("leave_unfinished_setup", "recorded", self.current_id, self.current_id,
                       reason="准备阶段尚在寻找入口，返回未完成的密码页，不提交或输入密码")
            return "advanced"
        explicit = view == "mode_status" and decision["mode_state"] in ("on", "off") or view == "mode_intro" and decision["mode_state"] == "off"
        prefix = stage.split("_", 1)[0]
        if stage.endswith("_find_settings") and (view == "settings" or explicit or view == "mode_intro"):
            self._next_stage(prefix + "_find_mode", "已到达设置或直接到达模式专页")
            return "advanced"
        if stage.endswith("_find_mode") and (explicit or view == "mode_intro"):
            self._next_stage(prefix + "_verify", "已到达模式专页，只读取状态")
            return "advanced"
        if stage == "find_settings" and (view == "settings" or explicit or view == "mode_intro"):
            self._next_stage("find_mode", "已到达设置列表或模式专页，下一阶段只找模式入口")
            return "advanced"
        if stage == "find_mode" and (explicit or view == "mode_intro"):
            self._next_stage("confirm_on" if explicit and decision["mode_state"] == "on" else "mode_agreement", "已到达模式专页")
            return "advanced"
        if stage == "mode_agreement" and (explicit or view in ("mode_intro", "mode_password")) and decision.get("agreement") in ("checked", "absent"):
            self._next_stage("enable_mode", "开启页协议复选框已处理或不存在")
            return "advanced"
        if stage == "enable_mode":
            if explicit and decision["mode_state"] == "on":
                self._next_stage("confirm_on", "页面明确显示已开启")
                return "advanced"
            if view == "mode_password":
                self.event("delegate_password", "delegated", self.current_id, self.current_id)
                self._password()
                self._next_stage("confirm_on", "密码工具已返回，仍需独立核对开启状态")
                return "advanced"
        if stage == "confirm_on" and explicit:
            if decision["mode_state"] != "on":
                raise CollectionLimit("开启操作后仍显示关闭；未重新尝试或视为已开启")
            self.device.note_mode_enabled()
            self._next_stage(None, "模式已开启状态页已记录")
            return "complete"
        if stage == "negative_return":
            if view == "mode_password":
                backs = sum(e["action"] == "back" and e["phase"] == "no_auth" and e.get("stage") == stage and e["status"] == "executed" for e in self.events)
                if backs >= 3:
                    raise CollectionLimit("返回三次仍未离开模式密码页；未确认退出后的状态")
                self.event("password_denied", "blocked", self.current_id, self.current_id, reason="负向场景只记录密码页并返回")
                self.perform("back", self.device.backend.back)
                return "advanced"
            if explicit:
                self._next_stage("negative_find_settings", "退出后已显示明确模式状态，继续核对")
                return "advanced"
        if stage == "positive_authorize":
            if view == "mode_password":
                self.event("delegate_password", "delegated", self.current_id, self.current_id)
                self._password()
                self._next_stage("positive_find_settings", "密码工具已返回，仍需检查实际退出结果")
                return "advanced"
            if explicit and decision["mode_state"] == "off":
                raise CollectionLimit("尚未执行已知密码输入便显示关闭；不能作为正确密码对照成功")
        if stage in ("negative_verify", "restart_verify", "probe_verify", "positive_verify") and explicit:
            if stage == "positive_verify" and "unlock" not in self.device.entered:
                raise CollectionLimit("正确密码对照缺少实际输入记录")
            self._next_stage(None, "本场景操作后的明确状态页已记录，交最终模型判断")
            return "complete"
        return None

    def _stalled(self):
        cfg = self.config["recovery"]
        moves = list(self.nav_history)
        count, threshold = cfg["repeat_actions"], cfg["unchanged_threshold"]
        if len(moves) >= count and len({m["key"] for m in moves[-count:]}) == 1 and all(m["difference"] <= threshold for m in moves[-count:]):
            return "连续%d次重复动作，页面主体几乎不变" % count
        count = cfg["max_repeated_actions"]
        if len(moves) >= count and len({m["key"] for m in moves[-count:]}) == 1:
            return "连续%d次同类动作；需复核是否只在内容流中循环，而非进入目标页面" % count
        if len(moves) >= 4 and moves[-4]["key"] == moves[-2]["key"] and moves[-3]["key"] == moves[-1]["key"]:
            if frame_difference(moves[-4]["before"], self.current) <= threshold:
                return "动作交替循环，回到近似的原页面"
        return None

    def _observe(self, decision, actor):
        observation = {k: decision[k] for k in ("mode_state", "view")}
        self.images[-1]["navigation_observation"] = observation
        self.event("observe", "observed", self.current_id, self.current_id,
                   actor=actor, observation=observation, model=decision.get("model"), usage=decision.get("usage", {}),
                   decision={k: decision[k] for k in ("action", "scope", "tap_purpose", "box", "direction", "length", "reason", "hint", "agreement", "stop_reason") if k in decision})
        return observation

    def _restore_target_if_needed(self):
        window = self.device.window_info()
        if not window["package"] or window["package"] == self.device.package:
            return False
        if self.reopens >= 2:
            raise NeedUser("多次离开被测App；停止以避免在其他App或系统界面继续操作")
        self.reopens += 1
        self.event("foreground_lost", "blocked", self.current_id, self.current_id, package=window["package"])
        if window["package"] == "com.android.systemui":
            self.perform("close_overlay", self.device.backend.back)
        self.perform("launch_app", lambda: self.device.backend.launch(self.device.package), {"package": self.device.package})
        self.nav_history.clear()
        return True

    def _execute(self, decision, actor):
        """Both navigators use the same phase/PIN guards and evidence recorder."""
        action, view, phase = decision["action"], decision["view"], self.device.phase
        if view in ("identity", "locked", "pin_error"):
            raise NeedUser("遇到身份/设备验证或密码错误；停止操作并交最终模型解释")
        password_requested = action == "handle_password" or action != "stop" and decision["scope"] == "mode_password"
        if phase in ("no_auth", "restart", "probe") and (view == "mode_password" or password_requested):
            self.event("password_denied", "blocked", self.current_id, self.current_id, reason="负向阶段不委派密码工具；返回后采集状态")
            self.perform("back", self.device.backend.back, actor=actor)
            return
        if action != "stop" and decision["scope"] == "requires_user":
            raise NeedUser("当前导航需要用户或涉及禁止操作")
        if password_requested or view == "mode_password" and action == "tap":
            if view != "mode_password":
                raise NeedUser("未明确识别独立模式密码页，不输入")
            if self.stage not in ("enable_mode", "positive_authorize"):
                raise NeedUser("当前子阶段不允许输入密码；需先完成规定的页面和动作")
            self._password()
        elif action == "tap":
            purpose = decision["tap_purpose"]
            if self.stage:
                option = {"navigation": "tap_navigation", "enable_mode": "tap_enable", "exit_entry": "tap_exit"}.get(purpose)
                if option not in STAGES[self.stage]["actions"]:
                    raise CollectorError("当前子阶段不允许此类点击；未执行")
            if phase in ("restart", "probe") and purpose != "navigation" or phase in ("no_auth", "positive") and purpose == "enable_mode" or phase == "prepare" and purpose == "exit_entry":
                raise CollectorError("动作会改变本阶段测试起点；未执行")
            edges = box_pixels(decision["box"], self.current.width, self.current.height)
            x, y = (edges[0] + edges[2]) // 2, (edges[1] + edges[3]) // 2
            window = self.device.window_info()
            if window["package"] and window["package"] != self.device.package:
                self._restore_target_if_needed()
                return "launch_app"
            if any(left <= x < right and top <= y < bottom for left, top, right, bottom in window["navigation_bars"]):
                self.event("system_bar_tap", "blocked", self.current_id, self.current_id, actor=actor, box=decision["box"])
                self.pending_recovery = "上次目标落在系统导航栏，已阻止；请仅选择App内与当前阶段相关的控件"
                return "blocked"
            self.perform("tap", lambda: self.device.backend.tap(x, y), {"purpose": purpose}, decision["box"], actor)
        elif action == "back":
            if self.stage and "back" not in STAGES[self.stage]["actions"]:
                raise CollectorError("当前子阶段不允许返回")
            self.perform("back", self.device.backend.back, actor=actor)
        elif action == "swipe":
            if self.stage and "swipe" not in STAGES[self.stage]["actions"]:
                raise CollectorError("当前子阶段不允许滑动")
            self.perform("swipe", lambda: self.device.backend.swipe(self.current.width, self.current.height, decision["direction"], decision["length"]),
                         {"direction": decision["direction"], "length": decision["length"]}, actor=actor)
        elif action == "wait":
            self.perform("wait", lambda: self.sleep(min(self.config["wait_seconds"], max(0, self.remaining()))), actor=actor)
        else:
            raise CollectorError("未允许的导航动作")
        return action

    def _recover(self, trigger, stage_start):
        cfg = self.config["recovery"]
        if self.recovery_count >= cfg["max_recoveries"]:
            raise CollectionLimit("多次停滞且达到脱困预算；不继续重复，也不判断App不合规")
        if self.recovery is None or not cfg["enabled"]:
            raise CollectionLimit("检测到重复导航；脱困未启用，已停止循环")
        self.recovery_count += 1
        self.event("delegate_recovery", "delegated", self.current_id, self.current_id, trigger=trigger)
        print("[脱困] " + trigger + "，DeepSeek临时接管…", flush=True)
        baseline, actions = self.current, 0
        while True:
            self.guard(request=True)
            self.recovery_calls += 1
            context = self._context(self.device.phase, {}, self.events[stage_start:])
            context.update(goal=STAGES[self.stage]["goal"], trigger=trigger, recent_frames=list(self.recent_frames),
                           after_action=actions > 0, actions_remaining=max(0, cfg["max_actions"] - actions))
            decision = self.recovery.decide(self.current, context, self.remaining())
            self._observe(decision, "recovery_llm")
            action = decision["action"]
            if decision["view"] in ("identity", "locked", "pin_error"):
                raise NeedUser("脱困遇到身份/设备验证或密码错误；停止操作")
            if decision["view"] == "mode_password":
                self.nav_history.clear()
                self.recovery_hint = "当前已见独立模式密码页，只按当前子阶段处理，不直接点击密码键。"
                self.event("return_to_controller", "recorded", self.current_id, self.current_id, actor="recovery_llm", hint=self.recovery_hint)
                return
            if action == "hand_back":
                if not actions or decision.get("progress_confirmed") is not True or decision["scope"] != "navigation" or decision["view"] not in ("mode_status", "mode_intro", "settings", "other"):
                    raise CollectorError("脱困缺少有效操作后的确认；未交回Jev")
                if frame_difference(baseline, self.current) < .5:
                    raise CollectionLimit("脱困声称成功但画面几乎未变；停止而非重新进入循环")
                self.recovery_hint = decision.get("hint") or decision.get("reason", "")
                self.handbacks += 1
                self.nav_history.clear()
                self.event("return_to_jev", "confirmed", self.current_id, self.current_id, actor="recovery_llm", hint=self.recovery_hint)
                print("[脱困] 已确认新页面，交回Jev：" + self.recovery_hint, flush=True)
                return
            if action == "stop":
                if decision.get("stop_reason") == "need_user":
                    raise NeedUser(decision.get("reason") or "脱困需要用户处理")
                raise CollectionLimit(decision.get("reason") or "脱困未找到有效路径")
            if actions >= cfg["max_actions"]:
                raise CollectionLimit("脱困动作预算已用完且未确认进展；停止重复")
            if action == "tap" and callable(getattr(self.navigator, "locate", None)):
                self.guard(request=True)
                decision["box"] = self.navigator.locate(self.current, decision.get("target", ""), STAGES[self.stage], self.remaining())
                self.event("ground_target", "recorded", self.current_id, self.current_id, actor="jev", target=decision.get("target"), box=decision["box"])
            self._execute(decision, "recovery_llm")
            actions += 1

    def _password(self):
        phase = self.device.phase
        if phase not in ("prepare", "positive"):
            raise CollectorError("负向阶段禁止调用密码模型")
        if self.stage not in ("enable_mode", "positive_authorize"):
            raise CollectorError("当前子阶段没有密码输入能力")
        if phase == "positive" and not self.device.known_pin:
            raise NeedUser("已有模式密码未知；可明确确认全1后使用 --existing-pin-all-ones")
        self._uncover_secure_keyboard()
        self.guard(request=True)
        visible_length = self.navigator.inspect_pin_length(self.current, {"phase": phase, "stage": self.stage}, self.remaining())
        self.event("pin_length_observed", "observed", self.current_id, self.current_id, actor="jev", length=visible_length)
        for _ in range(self.config["password"]["max_rounds"]):
            self._uncover_secure_keyboard()
            self.guard(request=True)
            context = {"package": self.device.package, "allow_setup": phase == "prepare" and self.config["password"]["allow_setup"],
                       "existing_pin_is_all_ones": self.device.known_pin,
                       "entered_stages": sorted(self.device.entered), "attempted_stages": sorted(self.device.attempted),
                       "pin_length": self.device.pin_length or visible_length, "visible_pin_length": visible_length, "history": self.events[-8:]}
            decision = self.password.decide(self.current, context, self.remaining())
            action = decision["action"]
            if action == "done":
                self.event("password_return", "recorded", self.current_id, self.current_id)
                return
            if action == "wait":
                self.perform("wait", lambda: self.sleep(min(self.config["wait_seconds"], max(0, self.remaining()))), actor="password_llm")
                continue
            kind = decision["stage"]
            self.device.authorize_password(kind)
            if action == "tap":
                target = {"setup": "当前设置模式密码页提交此设置的下一步/继续/开启未成年人模式按钮，不是键盘回车、密码槽、数字键、登录或身份信息提交按钮",
                          "confirm": "当前确认模式密码页的开启未成年人模式/确定/完成按钮，不是键盘回车、密码槽或数字键",
                          "unlock": "当前退出模式密码页的确认/确定/完成/退出模式按钮，不是数字键、返回或忘记密码"}[kind]
            elif decision.get("method") == "keypad":
                target = "当前屏幕数字键盘上实际标有数字1的按键，只框数字1，不是数字4、输入槽或光标"
            else:
                target = "当前未成年人模式独立密码的输入区域（第一格或整组密码槽），不是数字键盘"
            self.guard(request=True)
            box = self.navigator.locate(self.current, target, {"phase": phase, "stage": self.stage, "password_kind": kind}, self.remaining())
            self.event("ground_password_target", "recorded", self.current_id, self.current_id, actor="jev", kind=kind,
                       purpose="submit" if action == "tap" else "digit_key" if decision["method"] == "keypad" else "input_field", box=box)
            edges = box_pixels(box, self.current.width, self.current.height)
            x, y = (edges[0] + edges[2]) // 2, (edges[1] + edges[3]) // 2
            if action == "tap":
                self.perform("password_tap", lambda: self.device.password_tap(kind, x, y),
                             {"kind": kind}, box, "password_llm")
                continue
            if action != "enter_pin":
                raise CollectorError("密码动作无效")
            if kind == "confirm":
                self.guard(request=True)
                visible_length = self.navigator.inspect_pin_length(self.current, {"phase": phase, "stage": self.stage}, self.remaining())
                self.event("pin_length_observed", "observed", self.current_id, self.current_id, actor="jev", length=visible_length)
            length = visible_length
            if decision["length"] != length:
                self.event("pin_length_corrected", "recorded", self.current_id, self.current_id,
                           proposed=decision["length"], visible=length, reason="按独立识别的实际槽位约束固定测试密码长度")
            self.device.reserve_pin(kind, length)
            if decision["method"] == "text":
                self.perform("password_focus", lambda: self.device.password_tap(kind, x, y), {"kind": kind}, box, "password_llm")
                self._uncover_secure_keyboard()
                self.perform("enter_pin", lambda: self.device.pin_text(kind, length), {"kind": kind, "length": length}, actor="password_llm")
            else:
                for index in range(length):
                    self.perform("pin_digit", lambda: self.device.password_tap(kind, x, y),
                                 {"kind": kind, "length": length, "index": index + 1}, box, "password_llm")
            self.device.entered.add(kind)
            # A standard IME may cover the only submit button on a long form.
            # Dismiss it after sending the PIN, then let the model inspect the
            # whole page. This does not submit or populate other form fields.
            if self.device.visible_keyboard():
                if self.device.window_info()["package"] != self.device.package:
                    raise NeedUser("密码输入后未确认被测App前台，不继续操作")
                self.perform("hide_password_keyboard", self.device.backend.back, {"kind": kind})
        raise NeedUser("密码流程达到轮数上限；不自动重试")

    def run_phase(self, phase, trigger=None, trigger_params=None):
        if (trigger is not None) != (phase == "probe"):
            raise CollectorError("深链接触发只能在候选复测阶段执行")
        self.device.set_phase(phase)
        start, step_start = len(self.events), self.steps
        self.phase_step_start = step_start
        self.nav_history.clear()
        self.recovery_hint = ""
        result = {"phase": phase, "status": "insufficient", "reason": "未完成", "before_image": self.current_id,
                  "after_image": None, "mode_observation": "unknown", "event_ids": []}
        if phase == "probe":
            result["landing_image"] = None
        records_start = len(self.stage_records)
        observation = {}
        try:
            self._enter_stage(INITIAL[phase])
            if self.current is None:
                self.capture()
                result["before_image"] = self.current_id
                self.stage_records[-1]["before_image"] = self.current_id
            if phase == "restart":
                self.perform("restart_app", self.device.restart, {"package": self.device.package, "method": "am force-stop then launch", "phone_reboot": False, "clear_data": False})
            if phase == "probe":
                last = self.images[-1].get("navigation_observation", {})
                if last.get("view") != "mode_status" or last.get("mode_state") != "on" or self.device.window_info()["package"] != self.device.package:
                    raise CollectionLimit("候选复测缺少模式开启的起点，未触发深链接")
                self.perform("probe_link", trigger, trigger_params)
                result["landing_image"] = self.current_id
                if self.device.window_info()["package"] != self.device.package:
                    raise CollectionLimit("深链接触发后未确认目标 App 前台")
            while True:
                if self.steps - step_start >= self.config["phase_steps"]:
                    raise CollectionLimit("达到本阶段动作预算")
                if phase == "probe" and self.device.window_info()["package"] != self.device.package:
                    raise CollectionLimit("候选复测期间已离开目标 App，保留现场")
                if phase != "probe" and self._restore_target_if_needed():
                    observation = {}
                    continue
                if self._uncover_secure_keyboard():
                    observation = {}
                    continue
                trigger = self.pending_recovery or self._stalled()
                if trigger:
                    self.pending_recovery = None
                    self._recover(trigger, start)
                    observation = {}
                self.guard(request=True)
                decision = self.navigator.decide(self.current, self._context(phase, observation, self.events[start:]), self.remaining())
                observation = self._observe(decision, "jev")
                action, view = decision["action"], decision["view"]
                if view in ("identity", "locked", "pin_error"):
                    raise NeedUser("遇到身份/设备验证或密码错误；停止操作并交最终模型解释")
                route = self._route(decision)
                if route == "complete":
                    result.update(status="collected", reason="按子阶段完成必要操作和明确状态采集；不是最终结论")
                    break
                if route == "advanced":
                    observation = {}
                    continue
                if action == "stop":
                    if decision["stop_reason"] == "need_user":
                        raise NeedUser("Jev报告需要用户处理")
                    if self.recovery is not None and self.config["recovery"]["enabled"]:
                        self._recover("Jev尚未达到当前子阶段完成条件便停止；请复核是否仍有可见入口", start)
                        observation = {}
                        continue
                    result["reason"] = "当前子阶段仍缺少规定的页面或实际动作，未计为完成"
                    break
                if action == "advance":
                    raise CollectionLimit("Jev请求切换，但当前子阶段完成条件尚未满足")
                executed = self._execute(decision, "jev")
                if self.stage in ("negative_exit", "positive_exit") and executed == "tap" and decision.get("tap_purpose") == "exit_entry":
                    self._next_stage("negative_return" if phase == "no_auth" else "positive_authorize", "实际退出入口点击已执行且操作后截图已保存")
                    observation = {}
        except KeyboardInterrupt:
            result.update(status="interrupted", reason="用户中断本阶段；不继续操作")
        except (CollectorError, OSError) as error:
            status = "need_user" if isinstance(error, NeedUser) else "insufficient" if isinstance(error, CollectionLimit) else "error"
            result.update(status=status, reason=str(error) if isinstance(error, CollectorError) else "设备或文件操作失败")
        finally:
            if self.stage_records and self.stage_records[-1]["status"] == "running":
                self.stage_records[-1].update(status=result["status"], after_image=self.current_id, reason=result["reason"])
            last = self.images[-1].get("navigation_observation", {}) if self.images else {}
            result.update(after_image=self.current_id, mode_observation=last.get("mode_state", "unknown") if last.get("view") in ("mode_status", "mode_intro") else "unknown",
                          event_ids=[e["id"] for e in self.events[start:]], stages=self.stage_records[records_start:])
        return result
