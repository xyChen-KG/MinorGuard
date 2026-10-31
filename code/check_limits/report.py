"""A concise report, not another model/agent call."""

LABELS = {"supported": "代码支持", "contradicted": "发现矛盾", "unknown": "无法判断"}


def cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def brief(value, limit):
    value = cell(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def render_report(result):
    apk = result.get("apk", {})
    stats = result.get("stats", {})
    lines = ["# 未成年人模式时长核对", "",
             "%s · %s · %.1f 秒 · 模型 %d 次 / 工具 %d 次" % (
                 cell(apk.get("package", "未知 App")), cell(apk.get("version_name", "版本未知")),
                 result.get("duration_seconds", 0), stats.get("model_calls", 0), stats.get("tool_calls", 0)), "",
             brief(result.get("summary", "未完成核对。"), 220), ""]
    if result.get("status") not in ("completed", "insufficient"):
        lines += ["运行状态：**%s**。已有记录保留，未完成部分不作结论。" % cell(result.get("status")), ""]
    rules = result.get("rules", {}).get("rules", [])
    findings = {f["rule_id"]: f for f in result.get("findings", [])}
    images = {i["id"]: i for i in result.get("images", [])}
    if rules:
        lines += ["| 页面规则 | 静态结论 | 关键依据 |", "| --- | --- | --- |"]
        for rule in rules:
            finding = findings.get(rule["id"])
            image = images.get(rule["source_image"], {})
            image_link = " [图](%s)" % image["file"] if image.get("file") else ""
            label = LABELS[finding["verdict"]] if finding else "未完成"
            basis = brief(finding["reason"], 160).rstrip("。；; ") if finding else "未取得完整分析结果"
            citations = []
            cited_files = set()
            if finding:
                for ref in finding["code_evidence"]:
                    if ref["file"] in cited_files:
                        continue
                    cited_files.add(ref["file"])
                    location = ref.get("location", {})
                    name = location.get("class_name", ref["id"]).split(".")[-1]
                    if location.get("method_name"):
                        name += "." + location["method_name"]
                    citations.append("[%s](%s)" % (cell(name), ref["file"]))
                    if len(citations) == 3:
                        break
            if citations:
                basis += "；" + "、".join(citations)
            lines.append("| %s%s | %s | %s |" % (brief(rule["statement"], 100), image_link, label, basis))
        lines.append("")
    caveats = list(result.get("limitations", []))
    caveats += result.get("rules", {}).get("limitations", [])
    if not result.get("screenshot_identity_verified"):
        caveats.insert(0, "截图没有完整版本记录，未确认截图与 APK 的版本对应关系。")
    caveats = list(dict.fromkeys(caveats))
    lines += ["局限：" + "；".join(brief(c, 120).rstrip("。；; ") for c in caveats[:3]) + ("。" if caveats else "无额外说明。"), "",
              "**仅核对页面声明与已读取的客户端代码，不证明运行时有效、不可绕过或法律合规。**", "",
              "详细规则、引文和调用轨迹见同目录 `result.json`、`rules.json`、`trace.jsonl`。", ""]
    return "\n".join(lines)


def write_report(recorder, result):
    recorder.save_json("result.json", result)
    recorder._write(recorder.root / "report.md", render_report(result).encode("utf-8"))
