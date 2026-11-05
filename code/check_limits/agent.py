"""The same model reads screenshots, chooses tools, and makes the final judgment."""

import copy
import json
import os
import re
import time

from collect_images.common import CollectorError, now, post_json, validate_url
from .prompts import ANALYZE, EXTRACT, FINAL_SCHEMA, SYSTEM
from .tools import SOURCE_TOOLS


class BudgetEnded(CollectorError):
    pass


def clean_text(value, field, maximum=4000, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise CollectorError("模型输出字段不正确：" + field)
    return value.strip()


def parse_rules(value, image_ids):
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list) or len(value["rules"]) > 8:
        raise CollectorError("读图模型未返回有效规则列表（最多 8 条）")
    state = clean_text(value.get("screen_state"), "screen_state", 2000)
    rules, identifiers = [], set()
    for item in value["rules"]:
        if not isinstance(item, dict):
            raise CollectorError("规则必须为对象")
        rule = {key: clean_text(item.get(key), key) for key in
                ("id", "statement", "condition", "source_image", "source_text")}
        if not re.fullmatch(r"R[1-9][0-9]*", rule["id"]) or rule["id"] in identifiers:
            raise CollectorError("规则编号无效或重复")
        if rule["source_image"] not in image_ids:
            raise CollectorError("规则引用了不存在的图片")
        identifiers.add(rule["id"])
        rules.append(rule)
    limitations = parse_limitations(value.get("limitations", []))
    return {"screen_state": state, "rules": rules, "limitations": limitations}


def parse_limitations(value):
    if not isinstance(value, list) or len(value) > 8:
        raise CollectorError("limitations 应为简短列表")
    return [clean_text(text, "limitation", 2000) for text in value]


def validate_code_references(references, evidence):
    if not isinstance(references, list) or len(references) > 12:
        raise CollectorError("code_evidence 必须为证据列表")
    citations = []
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
            raise CollectorError("代码引用格式错误")
        record = evidence.get(ref["id"])
        quote = clean_text(ref.get("quote"), "quote", 4000)
        if not record or record["tool"] not in SOURCE_TOOLS:
            raise CollectorError("结论只能引用已经读取的源码证据，而非搜索结果")
        normalized = " ".join(quote.split())
        if normalized not in " ".join(record["text"].split()) or not any(normalized in " ".join(page.split()) for page in record["seen"]):
            raise CollectorError("代码引文不匹配 %s；请 read_evidence 重新读取该编号并逐字引用，不改变量名或加省略号；无法修正可写 unknown" % ref["id"])
        citations.append({"id": ref["id"], "quote": quote, "file": record["file"],
                          "tool": record["tool"], "location": record["arguments"]})
    return citations


def validate_findings(value, rules, evidence):
    """Integrity checks only, not a second semantic judge or score threshold."""
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        raise CollectorError("最终结果缺少 findings")
    expected = {r["id"] for r in rules}
    if len(value["findings"]) != len(expected):
        raise CollectorError("最终结果应覆盖全部规则，每条恰好一次")
    findings, seen = [], set()
    for item in value["findings"]:
        if not isinstance(item, dict):
            raise CollectorError("finding 必须为对象")
        identity = item.get("rule_id")
        if identity not in expected or identity in seen:
            raise CollectorError("结论引用未知或重复规则")
        seen.add(identity)
        verdict = item.get("verdict")
        if verdict not in ("supported", "contradicted", "unknown"):
            raise CollectorError("结论类型无效")
        citations = validate_code_references(item.get("code_evidence"), evidence)
        if verdict != "unknown" and not citations:
            raise CollectorError("代码支持/发现矛盾必须引用已读取的源码")
        findings.append({"rule_id": identity, "verdict": verdict,
                         "reason": clean_text(item.get("reason"), "reason", 3000), "code_evidence": citations})
    review = value.get("search_review")
    if not isinstance(review, dict) or set(review) != {"forward", "reverse", "missing", "stop_reason"}:
        raise CollectorError("提交前须填写 search_review：正向、反向、缺失环节和停止原因")
    review = {key: clean_text(review[key], "search_review." + key, 2000) for key in review}
    return {"summary": clean_text(value.get("summary"), "summary", 2000), "search_review": review,
            "findings": findings, "limitations": parse_limitations(value.get("limitations", []))}


class ChatClient:
    def __init__(self, config, transport=post_json):
        self.config, self.transport = config, transport
        self.base = os.environ.get("CHECK_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.key = os.environ.get("CHECK_LLM_API_KEY", os.environ.get("LLM_API_KEY", ""))
        self.model = os.environ.get("CHECK_LLM_MODEL", os.environ.get("LLM_MODEL", ""))
        if not all((self.base, self.key, self.model)):
            raise CollectorError("请在现有 .env 配置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL；分析模型须支持图片和工具调用")
        validate_url(self.base)

    def chat(self, messages, timeout, tools=None, json_mode=False, final_only=False):
        payload = {"model": self.model, "messages": messages}
        thinking_enabled = self.config.get("thinking") != "disabled"
        if self.config.get("thinking"):
            payload["thinking"] = {"type": self.config["thinking"]}
        if thinking_enabled and self.config.get("reasoning_effort"):
            payload["reasoning_effort"] = self.config["reasoning_effort"]
        if tools:
            # Thinking mode rejects named/required choices. At the budget boundary
            # expose only the finishing tool and retain auto; never disable thinking.
            payload["tools"] = [t for t in tools if t.get("function", {}).get("name") == "submit_result"] if final_only and thinking_enabled else tools
            if not payload["tools"]:
                raise CollectorError("结束阶段缺少 submit_result 工具；没有自动关闭 thinking")
            payload["tool_choice"] = {"type": "function", "function": {"name": "submit_result"}} if final_only and not thinking_enabled else "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 44 * 1024 * 1024:
            raise CollectorError("模型请求过大；没有静默丢弃图片或代码")
        response = self.transport(self.base + "/chat/completions", self.key, payload,
                                  min(timeout, self.config["api_timeout"]))
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict) or choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ValueError()
            if message.get("role") != "assistant":
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise CollectorError("模型返回不完整或格式错误；未执行工具") from None
        raw_usage = response.get("usage", {})
        usage = {k: v for k, v in raw_usage.items() if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                 and type(v) is int and v >= 0} if isinstance(raw_usage, dict) else {}
        details = raw_usage.get("completion_tokens_details", {}) if isinstance(raw_usage, dict) else {}
        if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int and details["reasoning_tokens"] >= 0:
            usage["reasoning_tokens"] = details["reasoning_tokens"]
        return message, usage, response.get("model", self.model)


class LimitsAgent:
    def __init__(self, config, client, tools, recorder, metadata, images, clock=time.monotonic, progress=print):
        self.config, self.client, self.tools, self.recorder = config, client, tools, recorder
        self.metadata, self.images, self.clock, self.progress = metadata, images, clock, progress
        self.started = self.clock()
        self.model_calls, self.tool_calls = 0, 0
        self.rules = None
        self.models, self.usage = [], {}
        self.messages = []
        self.failed_queries = {}
        self.finish_recheck_sent = False

    def remaining(self):
        left = self.config["max_seconds"] - (self.clock() - self.started)
        if left <= 0:
            raise BudgetEnded("达到分析时间预算；未完成部分不作结论")
        return left

    def _ask(self, tools=None, json_mode=False, final_only=False):
        if self.model_calls >= self.config["max_model_calls"]:
            raise BudgetEnded("达到模型调用预算；未完成部分不作结论")
        timeout = self.remaining()
        self.model_calls += 1
        start = self.clock()
        self.recorder.append({"event": "model_started", "call": self.model_calls, "time": now()})
        message, usage, model = self.client.chat(self.messages, timeout, tools=tools, json_mode=json_mode, final_only=final_only)
        self.models.append(model)
        for name, count in usage.items():
            self.usage[name] = self.usage.get(name, 0) + count
        self.recorder.append({"event": "model_finished", "call": self.model_calls, "model": model,
                              "seconds": round(self.clock() - start, 3), "usage": usage,
                              "thinking": self.config.get("thinking"), "reasoning_effort": self.config.get("reasoning_effort"),
                              "reasoning_returned": bool(message.get("reasoning_content")),
                              "reasoning_chars": len(message["reasoning_content"]) if isinstance(message.get("reasoning_content"), str) else 0,
                              "tool_names": [c.get("function", {}).get("name") for c in message.get("tool_calls", [])]})
        # Preserve thinking messages in memory if enabled; never persist reasoning_content.
        assistant = {k: copy.deepcopy(message[k]) for k in ("role", "content", "tool_calls", "reasoning_content") if k in message}
        self.messages.append(assistant)
        return assistant

    def context_chars(self):
        # Images have their own byte cap. This is a local character budget, NOT
        # the provider's token limit; retain full history and reserve a final turn.
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in self.messages
                   if m.get("role") != "user" or isinstance(m.get("content"), str))

    def _finish(self, value, budget_forced=False):
        final = validate_findings(value, self.rules["rules"], self.tools.evidence)
        pending = [lead for lead in self.tools.leads.values() if lead["status"] == "pending"]
        if pending and not budget_forced:
            raise CollectorError("仍有重要 pending 线索：%s。请继续查并用 record_lead 更新；真实访问障碍才可 blocked，不要仅为提交而跳过。" %
                                 "、".join(lead["id"] + ":" + lead["target"][:80] for lead in pending))
        targets = self.tools.unread_source_targets()
        if not budget_forced and not self.finish_recheck_sent and targets and any(f["verdict"] == "unknown" for f in final["findings"]):
            self.finish_recheck_sent = True
            self.recorder.append({"event": "finish_recheck", "unread_direct_targets": targets[:12]})
            raise CollectorError("结束前复查一次：已读源码中仍有未读取的直接构造/回调目标 %s。请选择模式入口或限制动作相关的线索补查，并通过引用追踪调用者；无关目标不必都读。若确实无关或不可访问，请在 search_review 说明后提交，不要求用满预算。" %
                                 json.dumps(targets[:12], ensure_ascii=False))
        if pending:
            final["limitations"].append("分析预算到达结束阶段，仍有未追完线索：" + "、".join(v["id"] for v in pending))
        final["leads"] = list(self.tools.leads.values())
        self.recorder.save_json("search_review.json", {**final["search_review"], "pending_at_finish": [v["id"] for v in pending],
                                                       "budget_forced": budget_forced})
        return dict(final, status="completed")

    def run(self):
        self.messages = [{"role": "system", "content": SYSTEM}]
        content = [{"type": "text", "text": json.dumps({
            "goal": self.config["goal"], "apk": self.metadata, "instructions": EXTRACT}, ensure_ascii=False)}]
        for img in self.images:
            content += [{"type": "text", "text": img["id"] + "（按操作顺序，图片上的点击框是采集标注，不是 App 自带界面）"},
                        {"type": "image_url", "image_url": {"url": img["data_url"], "detail": "high"}}]
        self.messages.append({"role": "user", "content": content})
        self.progress("[读图] 大模型提取页面规则…", flush=True)
        answer = self._ask(json_mode=True)
        try:
            extracted = json.loads(answer.get("content", ""))
        except (TypeError, ValueError):
            raise CollectorError("读图模型未返回有效 JSON；没有自动补造规则") from None
        self.rules = parse_rules(extracted, {i["id"] for i in self.images})
        self.recorder.save_json("rules.json", self.rules)
        self.progress("[读图] 提取 %d 条规则" % len(self.rules["rules"]), flush=True)
        if not self.rules["rules"]:
            return {"status": "insufficient", "summary": "图片中未提取到明确的时长/时段规则。", "findings": [],
                    "limitations": self.rules["limitations"]}
        self.messages.append({"role": "user", "content": ANALYZE +
                              "\n本轮模型请求总上限 %d 次（含读图、查代码决策及最终提交），工具上限 %d 次。无需用满。" %
                              (self.config["max_model_calls"], self.config["max_tool_calls"]) +
                              "\n本轮规则：" + json.dumps(self.rules, ensure_ascii=False)})
        self.recorder.append({"event": "code_search_strategy", "strategy": "ui_anchor_then_expand",
                              "tracing": "bidirectional_with_leads", "model_call_limit": self.config["max_model_calls"], "dynamic_fallback": False})
        return self._run_tools(FINAL_SCHEMA, "完成全部规则的静态核对，提交简短结论及真实代码引用。")

    def _run_tools(self, schema, description, result_key="findings"):
        definitions = self.tools.definitions() + [{"type": "function", "function": {
            "name": "submit_result", "description": description, "parameters": schema}}]
        while True:
            context_ending = self.context_chars() >= self.config["max_context_chars"]
            final_only = context_ending or self.tool_calls >= self.config["max_tool_calls"] or self.model_calls >= self.config["max_model_calls"] - 1
            if final_only:
                if context_ending:
                    self.recorder.append({"event": "context_budget_final_turn", "characters": self.context_chars(),
                                          "limit": self.config["max_context_chars"]})
                self.messages.append({"role": "user", "content": "上下文或调用预算已进入收尾阶段。现在调用 submit_result，明确未查清的部分，不再读取新代码。"})
            message = self._ask(tools=definitions, final_only=final_only)
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise CollectorError("模型工具调用格式错误")
            if not calls:
                # Accept valid final JSON too; free-form discussion gets one explicit next instruction.
                try:
                    return self._finish(json.loads(message.get("content", "")), final_only)
                except (TypeError, ValueError, CollectorError):
                    self.messages.append({"role": "user", "content": "请使用所提供的工具继续分析，或调用 submit_result 提交结果，不要只给自由文本。"})
                    continue
            identifiers = [c.get("id") for c in calls if isinstance(c, dict)]
            if len(identifiers) != len(calls) or any(not isinstance(i, str) or not i for i in identifiers) or len(set(identifiers)) != len(identifiers):
                raise CollectorError("模型工具调用编号无效或重复")
            # No execution after a final result hidden in a mixed batch of calls.
            mixed_final = len(calls) > 1 and any(c.get("function", {}).get("name") == "submit_result" for c in calls)
            for call in calls:
                name = call.get("function", {}).get("name")
                started = self.clock()
                failed_key = None
                try:
                    self.remaining()
                    arguments = json.loads(call.get("function", {}).get("arguments", ""))
                    if not isinstance(arguments, dict):
                        raise CollectorError("工具参数应为 JSON 对象")
                    if name == "submit_result":
                        if mixed_final:
                            raise CollectorError("submit_result 应单独提交，先读取工具结果再下结论")
                        final = self._finish(arguments, final_only)
                        self.recorder.append({"event": "final_submitted", "time": now(), result_key: len(final[result_key])})
                        return final
                    if final_only or self.tool_calls >= self.config["max_tool_calls"]:
                        raise CollectorError("工具预算已用完，请提交结果")
                    self.tool_calls += 1
                    self.progress("[查代码 %d] %s" % (self.tool_calls, name), flush=True)
                    self.recorder.append({"event": "tool_started", "name": name, "arguments": arguments, "time": now()})
                    self.tools.mcp.timeout = min(self.config["jadx"]["timeout"], self.remaining())
                    self.tools.read_timeout = self.remaining()
                    failed_key = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
                    if failed_key in self.failed_queries:
                        raise CollectorError("相同只读查询本轮已失败，不重复执行：" + self.failed_queries[failed_key] + "；请换查询或写 unknown。")
                    output = self.tools.call(name, arguments)
                    event = {"event": "tool_finished", "name": name, "arguments": arguments,
                             "evidence_id": output.get("evidence_id"), "seconds": round(self.clock() - started, 3)}
                except BudgetEnded:
                    raise
                except (CollectorError, TypeError, ValueError) as exc:
                    message_text = str(exc) if isinstance(exc, CollectorError) else "工具调用格式错误；未执行"
                    if failed_key is not None:
                        self.failed_queries.setdefault(failed_key, message_text)
                    output = {"error": message_text, "instruction": "这不是 App 不实现规则的证据；修正参数、换查询方法或写 unknown。"}
                    event = {"event": "tool_error", "name": name, "error": message_text}
                self.recorder.append(event)
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": json.dumps(output, ensure_ascii=False)})
