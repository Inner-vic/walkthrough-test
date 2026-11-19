---
name: walkthrough-test
description: 独立走查测试：同 VIN 两个时段并行从车云取日志，以 Windows 原生完整 201 项规则统计；固定群查询可信制品四位码，交付一份主报告和两份原生飞书明细。
---

# 走查测试

仅用户正文明确包含“走查测试”时启动本独立业务。原“时延分析”和 Uroad 保持各自入口、身份、状态与话题策略。部署或排查上下文不是业务测试数据，不创建任务或在线报告。

业务执行前读取 [对外会话交互](../../../.agents/skills/chassis-business-routing/references/conversation-interaction.md)。用户无需输入命令或 Skill 名称。仅确认一次收到，随后使用简短业务进度；成功送达原来源后不再重复完成通知，最终独占输出 `NO_REPLY`。失败或交付未确认则普通回复一次脱敏事实与下一步。

先运行公共 `chassis-business-routing/scripts/intent_router.py` 解析正文。自然语言时间按 [语义输入规则](../../../.agents/skills/chassis-business-routing/references/semantic-input.md) 处理，原正文和原始 CC Connect `context_header` 均不改写；同一次固定 `received_at` 配合可校验的引用候选传入受控入口。缺 VIN、两个时间窗或确有歧义时仅追问缺失项，不启动下载或查询。未显式指定角色时第一组为 Base，第二组为 PCM更新，不按时间先后重排。

本机运行入口始终为工作区 `deployment/walkthrough_entry.py`，由当前安装清单选择独立解释器、代码、配置与任务 runtime。不要直接调用 Skill 中的 worker 绕过本机入口，不从聊天猜身份或配置路径。可在本机读取 `deployment/walkthrough-test.installation.json`，但不得向用户输出其中的真实配置、身份或会话路径。

新任务采用[独立调度与交互](references/native-lifecycle.md)：受理时先核验独立任务服务，持久化入队后唤起执行，不在聊天进程下启动重型工作。全局最多一项完整取数/解析，同时最多两项任务分别处理解析与报告发布。两个时窗的环境查询并行，但每窗仍按 Prod→Test 顺序；下载与原统计并行预算不增加。任务进度由后台按阶段合并为普通来源回复，当前会话继续跟踪精确记录时不要再重复发同一阶段进度；终态报告仍只发一次。

```powershell
$Install = Get-Content -LiteralPath 'deployment/walkthrough-test.installation.json' -Raw | ConvertFrom-Json
& $Install.host_python -B -X utf8 'deployment/walkthrough_entry.py' start --text $OriginalText --ccconnect-context $TransportContext
```

新业务直接调用 `start`，它自带全部运行前检查。不要先要求独立只读 `preflight` 通过，导致明确过期但可续期的既有凭据无法进入受控维护。仅在固定 App/User 明确匹配且凭据明确过期时，`start` 才调用原有授权维护，并在完整预检再次通过后创建任务；独立 `preflight` 仍只读。各类预检失败保留有限阶段与原因，详见[运行前检查与启动](references/preflight-readiness.md)。

`TransportContext` 必须逐字来自入站首行，不能从正文制造。可选 `--received-at` 与 `--interpretation <JSON>` 必须沿用已验证的固定时钟与候选。同来源重投使用原记录时间锚点并去重，禁止按“最近任务”关联。`execution_disabled`、本地预检失败或配置不完整时不创建任务、不取日志、不发布报告，也不回退旧业务。

`start` 返回 `queued` 后跟踪返回的精确 `record_path`：

```powershell
& $Install.host_python -B -X utf8 'deployment/walkthrough_entry.py' wait --record $RecordPath --wait-seconds 45
```

未达 `completed/failed` 时继续同一记录的有界 wait。`status --record` 只读状态，`resume --record` 恢复精确任务。旧 0.5.1 runtime 的显式记录由受控入口分流至冻结旧解释器、源码与配置；旧任务已有最终结果只恢复交付，无最终结果不自动重新下载、计算或查询。旧任务不迁移进新 runtime，不覆盖 checkpoint。

新任务两条链路并行：

1. 车云按同一完整流程并行处理 Base 与 PCM更新。两个时段分别独立选择获准环境，默认每窗先完整查询 Prod 的五类来源，均查询成功且全部为空时再查询 Test；查到可用来源就使用该环境，两窗可以分别来自生产和测试。同一窗口的 AD/PCM/CPU 保持同环境。已验证的 Prod 首屏明确成功空信封按 [成功空响应契约](references/prod-list-response-compat.md) 兼容并保存原始 null 类型证据；其它权限、网络、响应或后续分页异常均不当作空、不触发回退。该窗两个环境均成功查询但都无来源时，明确反馈对应时段未查到日志。完整逐窗选择、查询顺序及来源绑定证据在任何下载前持久化，新版报告只接受经过校验的逐窗环境证据。旧 final 不迁移、不重算；旧同环境 final 仅按原配置恢复交付，旧混环境 final 不补造新证据。完整线上规则只读一次并冻结完整任务快照，两窗共用指纹；以已验 201 项完整口径为基准，保留原七子表映射，线上规则按完整快照处理、不按 201 项截断，不使用简化指标替代。
2. 独立固定 User 在已批准的性能报告沟通群向已核实的阿斯加德 Bot/App 发送制品查询，严格绑定本任务 VIN、请求及派发边界，核对来源身份后提取“相关制品信息”可信四位码。不得使用其他群、最近卡片、旧附件或占位 `XXXX` 冒充。本版取得可信四位码是完整报告发布门槛，查询失败或码不可信时停止发布并保留统计结果供恢复，不能创建完整成功报告。

Windows 原生完整后端仅使用走查专用 CPython 3.11、离线依赖、完整分析器与独立工具族；不启用 WSL、不改系统 Git/PATH/TZ。`OPENBLAS_NUM_THREADS=1` 仅限走查子进程。来源、归档、规则、字段、时间覆盖及并行隔离守卫不可降级；PCM/CPU 缺失不填造成有效数值，VLA 不以 PCM 值补齐。

新请求使用[受限并行与性能记录](references/native-performance.md)：AD 下载结束后，PCM/CPU 准备及解析与完整 AD 解析重叠；同一时段不叠加两套下载池，两窗最多保持既有八路传输。Windows 完整解析按核数为每窗分配最多四个进程，双窗合计至多半数逻辑核（小核数机器保留一个进程的最低运行能力），实际请求和 spawn 数量须互相核验。固定 User 的单次原模板完整只读快照在同一凭据锁内前后核验身份，快照内各 API 仍验证身份、路径、命令范围和模板绑定。统计规则、完整七子表回读、复制前重查、权限和最终交付门槛不变；冻结旧版本的任务仍走其原版本。

新任务的完整 AD 下载支持[逐文件校验与恢复](references/download-recovery.md)。网络中断、HTTP 429/5xx 有限重试；HTTP 400 与权限错误明确失败，不跳过文件、不改写 URL、不切换环境。只复用当前任务计划内且有逐文件完成凭据、重新核验通过的文件；所有必需来源齐全后才生成完整下载记录。无凭据旧半成品、计划变化或缓存污染停止并保留现场，不补造凭据。下载失败在任务与逐窗诊断中保留有限环境、状态和尝试次数，普通回复明确尚未完成分析；不得把代码修改或局部验证称为完整走查通过。

完整交付为 **一份主 Docx＋Base/PCM更新各一份原生七子表飞书 Sheets**。无图、不转 Excel；主报告保持原48格统计表、差值和颜色规则，新请求另附一个“数据来源：Baseline 表格　PCM更新表格”段落，两个具名链接分别指向本任务真实 Base 和 PCM更新 Sheets，不复制或嵌入表格。新标题统一为 `四位制品码（YYYYMMDD）走查测试`，日期取明确 Base 角色，可信查询与历史版本的内部语义守卫保持。字段已解析但无有效数值填 ASCII `-`；字段未解析在主报告统计表内明确标注，来源完整性未确认的必要说明放入数据来源段落。已证明 incomplete_zstd_frame 且按完整解码行恢复时，仅说明压缩日志缺少完整结束帧、已保留完整解码行、日志完整性未确认；不能据此断言请求时段必然缺失记录。其它 partial 问题保留通用来源审计告警，未解析标记不变。禁止猜值、填零或丢弃缺失。

三份业务资产都必须由当前报告 Bot 创建、写数、回读、逐份治理，再将编辑/管理及所有权交给本次请求发起人。固定 User 用于规则与原模板读取、复制权核验、获准的制品查询及必要密级操作；业务报告仍由报告 Bot 创建。私聊仅给请求者授权；群聊仅按既有策略给精确来源群查看权，请求者仍为所有者；会话类型未知时停止发布。保留报告 Bot 管理、密级及临时授权清理等既有权限策略。

专用空白模板属于部署资源，不转给业务请求者、不加入来源群。`fixed_user` 模式只允许原固定 User 读取原 reference，Bot 从真实 ready manifest 绑定的等价专用空白模板创建业务结果。原共享模板保持只读。ready 必须具有同项目/User/App/profile/home绑定、官方唯一复制回执、实际 Bot 身份、密级保留、私有范围、Bot view/copy 和完整七子表等价证据；无 ready 或绑定变化立即失败。默认 Bot/v1 及旧任务恢复逻辑保留，新模式独立 v2。不得伪造 ready、放松模板等价或 Bot-only 守卫。

新任务使用独立呈现v2：最终一次普通纯文本回复关联原 `source_message_id`，不加 `--reply-in-thread`，不创建新话题，只含主报告裸URL、VIN、Base 和 PCM更新完整起止时间。两份在线明细由主报告内的真实链接进入；回执不再列出明细URL、差值/颜色解释或查询备注，未解析项仍在报告中保留。裸链接可由飞书客户端显示原生预览，不能仅凭API成功宣称已显示缩略图。新请求在record与final中显式绑定 detail_links_style=source_paragraph；已有呈现v2缺少该标记时仍逐字使用原两行入口表及原提示，不迁移旧结果；未知或混用样式拒绝。新请求另在record与final中绑定 metric_header_style=no_ad，仅将主报告两个显示表头改为 `P99 Infer Delay`、`Frame Rate`；内部分析键、统计口径和原始七子表保持不变。已有结果无此标记时保留原AD表头，不重渲染；未知、单边或混用标记拒绝。旧任务呈现v1及其发送断点保持原样，不重算已有final、不自动改发已完成结果。`completed` 仍要求三份资产治理及本来源明确Bot送达凭证。
