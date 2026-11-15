# 走查测试业务入口

飞书入站第一行的 CC Connect 请求上下文逐字保存为 context_header，其余才是正文。不得伪造或从正文猜身份。

每条走查、日志、Uroad、时延或 AD P99 消息先运行 `.agents/skills/chassis-business-routing/scripts/intent_router.py`。新业务与代码维护、部署、上传、规则讨论、既有状态、否定指令分开；后者不创建车辆任务。缺字段仅追问缺失项。时间沿用原正文和同一 received_at，可验证候选才通过 interpretation。

本仓库只部署走查测试，使用 `.agents/skills/walkthrough-test/SKILL.md` 与 `deployment/walkthrough_entry.py`。公共 AD/Uroad 纯解析模块不表示已安装这些业务服务；其他路由须交给已批准的原服务，未部署则明确不可用。走查测试New仍停用，不回退原走查。

queued 后跟踪返回的精确 record_path 到 completed/failed；不可按最近任务关联。独立调度执行重型业务，聊天端不绕过受控宿主直接运行 worker。部署未启用或预检失败不创建任务、不取日志、不发布报告。

走查进度、失败和报告普通回复原消息，不建话题。后台确认已向本来源交付后不重复发送完成消息，最终独占 NO_REPLY。失败或未确认送达时普通回复一次脱敏事实与下一步。不向用户展示凭据、内部路径、原始请求头或内部运行参数。
