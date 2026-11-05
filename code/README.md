# 轻量 MinorGuard：未成年人模式检测

`collect_images` 用 Jev 操作 Android App 并按顺序保存截图；`check_limits` 读图并调用只读 JADX 工具，核对时长限制的客户端实现。另有独立的[退出保护测试](check_exit/README.md)：可执行真机测试、扫描 APK 的潜在旁路，或只分析现有 APK（无需手机）。

## 一次运行整个流程（推荐）

两个独立入口仍可单用；新增 `run_check` 将它们自动串起来，过程中不需要手动再启动第二条命令。

```bash
cd /Users/xingyuchen/Desktop/MinorGuard/code

# 手机已解锁并打开知乎，从当前页面继续；不传 --apk 时自动提取已安装 APK
.venv/bin/python -m run_check --package com.zhihu.android --serial IP:端口

# 复用已有 APK，但仍会与手机原文件做哈希校验
.venv/bin/python -m run_check --package com.zhihu.android --serial IP:端口 \
  --apk ../apks/zhihu/zhihu.apk
```

顺序：校验/提取 APK → Jev 采集（卡住时大模型脱困）→ 自动准备对应服务器 JADX 工程 → 同一个分析模型读图、查代码、判断 → 最终简短报告。报告位于 `results/runs/运行编号/report.md`，旁边保留完整采集和分析记录；包含全流程耗时、采集状态、恢复次数、逐条结论和局限。失败、中断或需要用户验证时也输出报告，不把“流程结束”写成“App 合规”。采集未全完成但有页面时可继续做有限范围分析，最终标记 `partial`，不会把缺失部分算通过。

`run_check` 和退出保护的静态扫描会自动准备服务器：`check_config.json` 的 `jadx.managed_root` 指向已部署的 MinorGuard 目录。按需同步本地 `jadx_runtime/` 源码并编译插件/独立解析器，备份原部署；只有源码、JVM 配置和 APK 都一致且真正就绪才复用，否则重载标记属于本项目的 JADX/MCP 会话。不动其他项目/用户进程。APK 按 SHA-256 命名上传，不覆盖旧版本。共享同一手机/JADX 时请勿并发运行多个 App。单用 `check_limits` 不部署或切换项目，但同样强制检查真正就绪。

需要事先配置 `.env`、SSH `safe`、Android 调试授权和测试账号。只唤醒屏幕，不自动解锁设备，不修改屏幕超时设置。split APK 当前明确报不支持，不漏掉分包后作完整结论。默认最多 60 步采集、80 张分析输入，仍受请求总大小限制；不静默丢弃截图。不会自动退出未成年人模式或恢复账号设置。

### 重复卡住时的大模型脱困

`config.json` 的 `recovery` 默认启用。连续 3 次相同方向滑动/近似位置点击/返回，且页面主体差异很小，触发接管；页面持续变化的正常连续滚动不触发。这个像素变化统计只是停滞启发式，不是语义正确性保证，也不是置信度门槛。

大模型接收最近 3 张全分辨率内存原图、动作和目标，直接给出安全导航动作与 Box，不使用 Jev 的缩小副本。操作后再次请求大模型看新截图；只有确认预期页面发生有效变化，才带着提示交回 Jev。脱困不等于完成采集，例如确认已进入可继续探索的设置页就可以交回。不能在首次接管时直接交还，不能仅凭 ADB 返回成功交还。每次最多 3 个恢复动作，最后仍有一次确认机会；全程最多 3 次接管。未确认就达到预算则保留证据并报告未完成，不强行交还。密码继续交给原有全 1、4/6 位的受限分支；未知密码、账号/设备/支付密码和验证流程仍需用户。

轨迹区分 `jev`、`recovery_llm`、`password_llm`，记录委派、动作及 `return_to_jev/confirmed`。`result.json` 的 `recovery` 保存接管数、模型调用数和确认交还数。所有分数仍仅记录，不用于阻止合法动作。

## 时长规则核对（新）

输入 APK 和操作截图；不需要连接手机。程序不预设 40 分钟、22:00 等规则值，不用 OCR，不让 Jev 二次审核。复用 `.env` 中的 DeepSeek 配置，要求模型同时支持图片和工具调用；也可用 `CHECK_LLM_*` 三个环境变量单独覆盖。

```bash
cd /Users/xingyuchen/Desktop/MinorGuard/code

# 无模型费用：确认 APK/截图、服务器当前项目、只读 MCP 和配置
.venv/bin/python -m check_limits \
  --apk ../apks/bilibili/bilibili.apk \
  --images ../results/collections/20261029-213057-12851b5a --check

# 实际运行：会向模型 API 发送截图及按需读取的代码片段
.venv/bin/python -m check_limits \
  --apk ../apks/bilibili/bilibili.apk \
  --images ../results/collections/20261029-213057-12851b5a
```

`--images` 接收采集运行目录或其 `images/` 子目录，也可使用自行准备的图片目录，按文件名自然排序；无采集元数据时明确标注截图版本未核实。带点击框的截图会告知模型标注不是 App 自身界面。当前配置最多 80 张，另有总请求大小限制，不会偷偷筛掉部分截图。

流程：一次读图提取规则 JSON → 先搜截图文字资源 → 正向从入口追到限制动作，同时反向从限制弹窗/密码页追调用者和触发条件 → 跟进实际的混淆类、回调、消息与生命周期 → 模型填写 `search_review` 并提交结论 → Python 核对格式、证据和待查线索，并排版简短报告。不增加规划/审核 Agent，不写死某个 App 类名，没有置信度门槛。

三个轻量辅助工具：`search_evidence(query, id?, offset?, count?)` 在本轮已保存的全文中搜片段，不发远程请求，返回原证据编号及行号；`find_class_names(query, offset?, count?)` 自动缓存完整分页类名元数据后过滤，不反编译全 APK；`record_lead(id, target, source_id, status, note, result_id?)` 记录少量重要线索，checked 必须附实际查询结果证据，blocked 须说明具体障碍。存在 pending 时正常提交会被要求继续查；达到调用预算的最后提交可以保留 pending，并明确记录未追完。代码不判断“已查足够”的语义，也不能防止模型遗漏线索或错误标记状态。

`search_review` 只记录正向查到哪里、反向查到哪里、缺什么和停止原因，保存在 JSON，不增加报告篇幅。每次还保存提示词快照和源文件哈希，便于对照不同版本。

源码页会附带实际 `new` 出来的、尚未读取的限定类名目标和反向引用查询提示；只作为导航候选，不自动认定与任务有关。若要以 unknown 结束而仍有此类候选，正常预算下只提醒一次补查，模型可解释无关/不可访问后结束，不强制全读或用满预算。MCP 添加的行号栏只从可引用文本中移除，原始编号响应仍留在证据 JSON。

轻量只读工具 `search_ui_resources(query, offset=0, count=20)`：按截图短语或资源名检索，忽略空格和大小写；资源表由 MCP 读取并在本次分析中缓存，只把命中结果发给模型。每页 200 份资源文件，自动继续分页，不再限制为 5 页；仍受总时限约束并检测分页不前进。未完整读取、XML 解析失败等情况都会明确标记，零命中不代表没有功能。原始资源表保存在 `code_evidence/ui_resources.json`。工具不猜测资源 ID，页面声明也不能作为实现证据。普通搜索的分页 count 上限也提高到 200，按返回分页继续读取，不保证一次查全。

找不到页面入口时允许用页面语义对应的类名/方法名辅助；找到入口后优先沿调用关系扩大范围，不反复猜类名或全文阅读整个 APK。合理路径查过仍缺关键链路就提前提交“无法判断”，附部分证据，**不补动态实验**。

源码读取链：GUI 源码 → 区分类/方法不存在、未就绪、Java 失败 → Java 失败优先读现有 Smali → 无有效指令时再独立 JVM 同步加载相同完整 APK、禁用内联读取整类及指令 → 仍无证据则 unknown。类/方法不存在不盲目重解析整个 APK，应核对真实全名、外层或承载类。原始坏结果和恢复结果分别落盘；只缓存正常源码或实际非空指令，不无限重试。GUI 插件的 class-source 绕过旧源码 LRU；独立 JVM 不复用 GUI 的错误状态。方法恢复返回整类，须核对实际方法与重载。独立解析不加载资源表；Smali 不是 Java 还原成功，更不是运行验证。代码/指令可用 `search_evidence` 定位，再 `read_evidence` 补上下文。

`check_config.json` 配置 SSH 别名（默认 `safe`）、MCP 端口、服务器已加载 APK 的记录文件，以及时间/调用预算。分析阶段默认 `thinking="enabled"`、`reasoning_effort="high"`；每轮 assistant 的 `reasoning_content`（包括未调用工具的轮次）均在内存中原样回传，不写入日志；日志只记录是否返回、字符数与 API 提供的 reasoning token 数。思考模式不使用强制工具选择，最后预算阶段只保留 `submit_result` 工具并使用 `auto`，不会为收尾偷偷关闭思考。单次模型请求超时改为 300 秒，仍受整轮 60 分钟约束；上限 100 次模型调用不变。所有模型请求均不自动重试。工具失败会反馈给模型换路径或写“无法判断”，不当作 App 未实现功能。

当前分析预算为 **100 次模型请求**（读图、代码决策及最终提交）、400 次工具调用、60 分钟、250 万文本上下文字符。字符数是本地预算，不等于模型 token 上限；达到字符预算时不丢弃历史证据、不再直接退出，而是只保留提交工具让模型收尾。请求仍受 44 MiB 和 API 自身上下文限制，程序不声称任意字符数都能容纳。模型请求上限没有提高。JVM 最大堆默认 **32 GiB**，可配置到 48 GiB；普通查询 180 秒，独立完整 APK 解析和初始化检查各 600 秒，恢复过程同时受分析剩余时间约束。证据足够或合理路径用尽时可提前结束，上限不要求用满。

MCP 普通响应的内存缓冲阈值 32 MiB，包索引 64 MiB；超过阈值不再直接拒收，会落盘继续解析（硬安全上限 256 MiB）。只把 14000 字符的证据页发给模型，完整结果保留在本地。FastMCP 重复的结构化/文本结果优先读结构化版本。大结果保存在 `code_evidence/*.wire.json`，而不是整包上传模型。

方法、字段、代码和注释搜索必须限定 `package`，模型先按类名查找相关包再检索。工具会读取包结构索引，累计该前缀及子包的类数；默认 `max_search_classes=5000`，可根据实际调用链手动配置到 20000，超过就要求缩小范围或直接读类/方法。全局 `search_method_by_name` 仅在整个 APK 不超过该预算时执行。包索引缺失或无匹配时不启动扫描；类名零匹配不能证明包很小。单次 MCP 查询有总时限，SSE 心跳不会延长它；同轮已失败的查询不重复执行。已有插件不提供搜索取消接口，客户端超时不保证 GUI 后台查询立即停止；独立解析超时则终止自己创建的子 JVM。

服务器仍只监听回环地址，通过仅本机可用的 SSH 隧道访问。`/minorguard-ready` 等待 GUI `JadxWrapper` 加载锁释放，并检查类表、类路径和 AttachMethodDetails 内部依赖确实初始化；HTTP 200/Manifest 可访问不算就绪。查询路由在未就绪时返回 503，不让过早读取污染缓存。还核对当前 APK 路径、哈希、Manifest 和实际 JVM 堆上限。此检查适用于已部署的 JADX 1.5.6；版本变化应重新验证接口，不默默退回端口检查。单独分析入口不重启/切换工程；全流程按上述托管规则准备。项目身份依赖可信记录，不防御别人并发手动切换 JADX 工程。

输出 `../results/checks/运行编号/`：

```text
report.md              简短结论表、关键截图/代码链接、局限
result.json            完整规则、判断理由、原始引文、模型及总耗时
rules.json             模型从图片提取的规则及原文
trace.jsonl            模型/工具调用、错误和耗时，不保存密钥/图片 base64/思维链
code_evidence/         实际读取的完整代码和查询结果，可分段继续读取
images/                本次输入截图副本
inputs.json            输入清单、哈希；run_config.json 为非密钥配置
leads.json             重要线索与待查/已查/访问受阻状态（使用该工具时）
search_review.json     结束前的正反向覆盖说明及未追完线索
prompt_snapshot.json   本轮实际提示词与分析源码哈希，不含密钥
```

“代码支持”只表示已读代码支持完整页面声明，不把“无法使用”缩成“有弹窗提示”；“发现矛盾”需要相同适用条件下的明确不一致；“无法判断”表示证据不足，可以附已找到的部分线索。语义判断由模型完成，仍可能判断错误，代码并不证明结论正确。仅搜索到常量/文案不足以支持规则，时间戳还需结合辅助方法和时区解释；报告不宣称运行时生效、不可绕过或法律合规。达到预算、断网或中断时保留已有记录，未完成规则标为“未完成”，不伪造成功报告。

API 接入依据：[DeepSeek 图片输入](https://api-docs.deepseek.com/guides/vision/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[JADX MCP 只读工具](https://github.com/zinja-coder/jadx-mcp-server)。

## 图片采集：运行

Python 3.9+。建议使用独立虚拟环境，不修改系统 Python：

```bash
cd /Users/xingyuchen/Desktop/MinorGuard/code
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env  # 仅在不存在时创建，不覆盖已填写的 Key
```

已创建空密钥的本地 `.env`，默认使用官方 DeepSeek：`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash`。你只需填入 `TYPESAFE_API_KEY`（Jev）和 `LLM_API_KEY`（DeepSeek）。若已有 `.env`，不要再次复制示例覆盖它。

密码和脱困分支需要兼容 Chat Completions 图片输入及 JSON 模式的模型；DeepSeek 的 `deepseek-flash` 图片输入与 JSON 输出见[官方图片文档](https://api-docs.deepseek.com/guides/vision/)和[JSON 文档](https://api-docs.deepseek.com/guides/json_mode/)。真实视觉脱困和读图查代码的调用已经测试；这不保证每个 App 的密码页面都能识别。不要使用账单或控制台网页地址作为 API 地址，也不要把密钥写进源代码。

手机提前解锁、连接 ADB、登录测试账号并打开被测 App。不会自动连接未知网络设备；无线调试需先手动 `adb connect IP:端口`。

```bash
# 只检查设备和密钥是否配置，不调用模型或操作页面
.venv/bin/python -m collect_images --check

# 使用 config.json 的默认目标，从手机当前页面开始
.venv/bin/python -m collect_images

# 多设备时明确指定；只有 --launch 才会先启动 App
.venv/bin/python -m collect_images --serial IP:端口 --launch

# 自定义目标与采集要求。覆盖 goal 时会清空默认 evidence，避免混入旧目标
.venv/bin/python -m collect_images --goal '找到未成年人模式的使用时长设置页面' \
  --need '未成年人模式入口' --need '使用时长限制的规则或设置页面'

# 离线测试，不连接手机、不请求模型
.venv/bin/python -m unittest discover -s tests -v
```

`config.json` 的设备路径、输出路径相对于配置文件位置解析；命令从哪里运行不影响它们。可用 `--config`、`--env-file` 指定其他文件。

## 过程

1. ADB 获取原始截图；短暂等待并尝试确认画面稳定。
2. 同一次 Jev 请求携带当前未标注截图、采集目标、最近操作、已经收集/缺少的页面。上传副本转为 RGB 并等比缩放到 Jev 要求的每边 280～1568 像素；保存图片仍为原始分辨率，归一化 Box 映射回原屏幕，不自动裁剪/补边或扭曲比例。
3. Jev 独立回答动作 Choice、目标 Box、操作性质 Choice、采集项是否可见等问题。代码只读取当前动作需要的参数；不会假设 Box 已读取同次 Choice 的答案。
4. 普通导航直接执行选定动作和合法 Box；置信度只记录，没有执行分数门槛。没有目标存在度数值门槛、低分二次定位或整屏变化拦截；画面变化与连续无变化只作为下一轮提示。保留合法动作、坐标、时间/步数预算，以及禁止付款、发送、发布、删除和未知密码等边界。
5. 执行点击、滑动、返回或等待，保存操作后截图，进入下一轮。
6. Jev 选 `handle_password` 时进入受限密码分支；重复无进展时进入导航脱困分支。大模型完成动作、查看新图并确认脱困后，再交回 Jev，不常驻代替 Jev。
7. 模型请求停止或达到上限时保存结果。`completed` 需要模型确认目标完成，且每项 `evidence` 已有对应截图。

Jev 的并行答案偶尔会出现“目标 complete、全部采集项 present，但停止标签 insufficient”的自相矛盾。仅在有明确的非空采集清单、目标完成确认和全部对应图片时，将该停止标签规范化为 `completed`，原标签和调整标记保留在轨迹里；缺项、目标未确认或 `need_user` 均不会被提升为完成。它只解决采集状态字段的一致性，不把静态结论变成通过。

支持动作：`tap`、`swipe`、`back`、`wait`、`handle_password`、`stop`。模型不会生成任意 ADB/shell 指令。上/下/左/右指手指运动方向，滑动距离为短/长两种。

## 图片与记录

输出在项目根目录 `results/collections/运行编号/`：

```text
images/001.png ...    每个观察页面一张图，按时间编号
trace.jsonl           每步操作、执行者、目标框、点击点、前后图片、时间、结果
run_config.json       本轮非密钥配置
result.json           完成情况、已采集/缺少项、停止原因、总耗时和模式密码处理记录
```

- 点击：在**点击前页面**标出目标框及实际点击点。密码键盘的每次点击也单独保存。
- 滑动、返回、等待：保存原图，参数写入轨迹。
- 操作后截图也作为下一步操作前截图；若下一步点击，它会带下一步的框。轨迹通过 `before_image`/`after_image` 关联，避免保存重复文件。
- 正常结束时最后一张图不加框。中途断连、API 失败或 Ctrl+C 时，保留最近已保存的截图与已有轨迹；若操作后截图失败，最后一张可能是带框的操作前图片，不假装已经看到最终页面。
- 供模型使用的原图只在内存中，不会拿标注后的磁盘图片再次请求模型。`*_observation_sha256` 是原始截图的哈希，不是可能带框的磁盘图片哈希。
- 目录权限为仅当前用户访问。截图仍可能包含个人信息，请只用受控测试账号，不要直接公开运行目录。

## 密码边界

只处理**本次测试 App 的未成年人模式独立数字密码**，不是账号或支付密码。设置密码全为 1，根据页面明确要求输入 4/6 位，确认长度必须一致。密码值不会写入轨迹字段，但测试 PIN 的长度和阶段会记录；屏幕可能显示测试输入。

`password.allow_setup=true` 允许设置新的测试模式密码；设置操作会改变 App 状态。本轮设置的密码可用于确认或解锁。已有模式密码默认视为未知；只有你明确确认它已经是全 1，才把 `password.existing_pin_is_all_ones` 改为 `true`。每个阶段只输入一次，错误/重复阶段不重试。手机厂商的特殊键盘是否支持输入，需要真机验证。

模型对页面和按钮的判断不是硬安全保证，建议只在受控账号和可恢复的测试环境使用。不会自动退出未成年人模式或恢复设置。进程被强制杀死/断电时不能保证生成最终总结，已落盘图片和轨迹仍保留。

## 结果含义与限制

- `completed`：目标页面已采集，**不代表时长约束真实生效或 App 合规**。
- `insufficient`：模型探索后仍缺少证据、达到步数/时间预算；不代表 App 没有该功能。
- `need_user`：登录、验证码、未知密码、额外授权或禁止的付款等操作；不会因为置信度低而要求人工处理。
- `error` / `interrupted`：程序/接口错误或用户中断。

模型的置信度不是经验证的准确率保证，本版不将它用于阻止动作；页面稳定性与变化统计也不拦截点击。只在动作边界检查时间/步数预算；正在执行的单个 ADB 操作可能占用额外超时时间，不会因超预算继续发起新操作。

## 文件职责与接口依据

`run_check.py` 是全流程入口；`collect_images/main.py` 是单独采集入口，`workflow.py` 调度，`jev.py` 调用 Jev，`recovery.py` 处理导航脱困，`password.py` 处理受限密码；`adb.py` 操作手机，`recorder.py` 保存记录，`common.py` 放公共配置、截图和 HTTP 逻辑。`check_limits/` 负责读图与代码核对，`server.py` 只在全流程中自动准备本项目的 JADX 工程。

按官方格式实现：[Jev 图片](https://docs.typesafe.ai/images)、[Box](https://docs.typesafe.ai/primitives/box)、[并行问题](https://docs.typesafe.ai/patterns/fan-out)、[OpenAI 兼容的图片消息结构](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat)。Jev 图片/Box 仍是官方标注的实验能力，接口和准确性可能变化；发送必需的 `TypeSafe-Danger-Zone-Images: true` 头。模型名应使用账户 [GET /v1/models](https://docs.typesafe.ai/api/list-models) 返回的名称，默认 `jev-latest`，实际响应型号记录在轨迹中，不能自行拼接 `jev-1.14` 这样的未提供别名。
