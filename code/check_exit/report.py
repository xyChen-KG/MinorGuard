"""Deterministic rendering only; verdicts are supplied by the final model."""

from .prompts import SCENARIOS

LABELS = {"bypass": "发现绕过", "not_observed": "本轮未发现绕过", "unknown": "不确定",
          "control_passed": "正确密码对照成功", "control_failed": "正确密码对照异常"}


def clean(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")


def render_static(result, heading="## 静态旁路分析"):
    directory = result["directory"]
    labels = {"completed": "扫描结束", "incomplete": "扫描未完成", "interrupted": "已中断", "error": "扫描失败"}
    lines = [heading, ""]
    apk = result.get("apk", {})
    if apk.get("package") and apk.get("version_name"):
        source = "只分析输入 APK，未连接手机或复测" if result.get("input_source") == "local_apk" else "已与手机安装的 APK 核对"
        lines.extend(["%s · %s · %s。" % (clean(apk["package"]), clean(apk["version_name"]), source), ""])
    lines.extend(["**%s**。%s" % (labels[result["status"]], clean(result.get("summary", ""))), ""])
    if result["status"] == "completed":
        lines.extend(["以下调用链为模型根据源码提出的分析；代码只核对引文出处，不自动验证片段间的可达性。请结合局限和线索备注核对链路。", ""])
    if "entry_count" in result:
        lines.append("Manifest 共列出 %d 个入口，其中 %d 个进入本轮源码筛查范围。[入口清单](%s/entries.json)" % (
            result["entry_count"], result["selected_count"], directory))
    if result.get("coverage"):
        lines.extend(["", "覆盖范围：" + clean(result["coverage"])])
    if result.get("limitations"):
        lines.extend(["", "局限：" + "；".join(clean(value) for value in result["limitations"])])
    leads = result.get("leads", [])
    if leads:
        lines.extend(["", "线索追踪（已查询表示已读取相关结果，具体缺口见备注）："])
        labels = {"pending": "待追查", "checked": "已查询", "blocked": "受阻"}
        for lead in leads:
            refs = list(dict.fromkeys(lead[key] for key in ("source_id", "result_id") if lead.get(key)))
            links = " ".join("[%s](%s/code_evidence/%s.txt)" % (clean(ref), directory, clean(ref)) for ref in refs)
            lines.append("- %s（%s）：%s。%s %s" % (clean(lead["id"]), labels.get(lead.get("status"), "未知"),
                         clean(lead["target"]), clean(lead.get("note", "")), links))
    candidates = result["candidates"]
    if candidates:
        statuses = {"pending": "待真机验证", "unknown": "复测证据不足", "bypass": "已复现绕过", "blocked": "已验证拦截"}
        lines.extend(["", "| 候选 | 入口与目标 | 静态依据 | 真机结果 |", "| --- | --- | --- | --- |"])
        for c in candidates:
            refs = " ".join("[%s](%s/%s)" % (ref["id"], directory, ref["file"]) for ref in c["code_evidence"])
            v = c["validation"]
            images = " ".join("[%s](images/%s.png)" % (i, i[1:]) for i in v.get("image_ids", []))
            lines.append("| %s | %s → %s | %s %s | %s。%s %s |" % (c["id"], clean(c["entry"]), clean(c["target"]),
                         clean(c["reason"]), refs, statuses[v["status"]], clean(v["reason"]), images))
    elif result["status"] == "completed":
        lines.extend(["", "本轮未提出有源码支持的候选。入口覆盖与未查清路径见上。"])
    unmatched = set(result.get("replay_uris", ())) - {c["uri"] for c in candidates}
    if unmatched:
        lines.extend(["", "未找到支持以下指定链接的静态候选，本轮未重放：" + "、".join(clean(uri) for uri in sorted(unmatched))])
    return lines + ["", "[静态结果与证据索引](%s/result.json)" % directory, ""]


def write_report(recorder, result):
    recorder.save_json("result.json", result)
    bundle, judgement = result["collection"], result.get("judgement") or {}
    images = {i["id"]: i["file"] for i in bundle["images"]}
    findings = {f["scenario_id"]: f for f in judgement.get("findings", [])}
    conclusion = "三个常规场景结论" if "static_analysis" in result else "结论"
    lines = ["# 未成年人模式退出保护：动态测试", "",
             "%s · %s · %.1f 秒 · 执行状态：%s" % (clean(bundle["package"]), clean(bundle.get("app_version") or "版本未知"), result["duration_seconds"], clean(result["status"])), "",
             "%s：**%s**。%s" % (conclusion, LABELS.get(judgement.get("verdict"), "不确定"), clean(judgement.get("summary", result.get("error", "未取得有效最终判定")))), "",
             "| 场景 | 动态结论 | 依据 |", "| --- | --- | --- |"]
    for identity, _, name in SCENARIOS:
        finding = findings.get(identity, {})
        links = ["[%s](%s)" % (i, images[i]) for i in finding.get("image_ids", [])[:4] if i in images]
        lines.append("| %s | %s | %s %s |" % (name, LABELS.get(finding.get("verdict"), "不确定"), clean(finding.get("reason", "未完成或缺少判定证据")), " ".join(links)))
    lines += ["", "局限：" + "；".join(clean(x) for x in judgement.get("limitations", [])[:3]), "",
              "只覆盖本轮实际测试路径，不证明所有绕过方式都不存在，也不证明密码输入者是监护人。", "",
              "最后截图的模式状态（仅导航观察，非独立验证）：%s；不会自动恢复原模式或账号设置。" % clean(result.get("last_mode_observation", "unknown")), "",
              "原始截图见 images/；[场景执行情况](scenarios.json)、[完整记录](collection.json)、[操作轨迹](trace.jsonl)、[最终结果](result.json)。", ""]
    if "static_analysis" in result:
        lines.extend(render_static(result["static_analysis"]))
    recorder._write(recorder.root / "report.md", "\n".join(lines).encode())
