# lark-cli、阿斯加德与双身份接入

此项目包含调用代码、授权续期、凭据互斥锁、DPAPI 加密副本和首次安装操作入口。官方 CLI 二进制按组织批准渠道安装，不把二进制、Secret、token、加密凭据副本或真实群/消息记录放入 Git。已核对的 CLI 为 **1.0.97**；使用其他版本需重新验收下面的命令和 JSON 契约。

## 谁在发送、接收与回传

| 项目 | 对阿斯加德的操作 | 最终报告通道 |
| --- | --- | --- |
| 时延分析 | 固定开发者 User 在来源消息线程内 @ 阿斯加德，发送车辆版本查询和两条独立 `fsdlog`；同一 User 轮询接收卡片、读取结果 Sheets | 独立 Bot 创建文档、管理权限、转所有权，User 设置 L3 并撤销临时权限，Bot 回复原话题 |
| 走查测试 | 固定 User 在配置的可信查询群 @ 阿斯加德，只查询车辆版本/制品四位码；日志由车云下载后本地完整分析器处理 | 独立 Bot 发布主文档和两份明细表，回复原始来源消息 |
| Uroad | 标准链路不向阿斯加德派发任务；车云 Uroad/CAN 取数 | CC Connect 原生附件发送；不能用 lark-cli 消息代替原生附件回执 |

用户 profile 与报告 Bot profile 分开，均使用同一项目专属 `lark_home`、同一获准 App ID 和 `project_id-` 命名空间。`expected_dispatch_open_id` 是固定执行用户，不取入站请求人的 ID 来覆盖。Bot profile 不得存在可用用户登录态。

## 应用和资源权限

管理员先准备企业自建应用、应用发布/安装及获准使用范围。User 和报告 Bot 均需能访问来源群，User 还需能访问阿斯加德查询群及结果表；机器人必须在对应群内并能接收 @。名称相同不等于同一机器人：用**执行应用看到的** `open_bot_id` 核对，走查另核对可选 `app_id`。不照搬另一应用的 open ID。

| 身份 | 必须验收的能力 | 源码调用 |
| --- | --- | --- |
| User | 用户发送消息、读取消息/话题、按需核验成员；Sheets 元数据与单元格读取；文档密级查询和写入 | `im +messages-send/+messages-reply/+messages-mget/+chat-messages-list/+threads-messages-list`、`sheets` 读取、`drive +secure-label-list/+secure-label-update` |
| Bot | 创建/复制/编辑文档和表格；协作者、来源群查看权限、所有权转移；原消息回复 | `docs +create`、Sheets 写入/模板复制、`drive` 权限和所有权操作、`im +messages-reply` |
| User OAuth | 持续授权、上述业务能力和两个密级 scope | `offline_access`、`docs:secure_label:readonly`、`docs:secure_label:write_only`，其余按当前 CLI schema 和组织批准的操作 scope 填写 |

应用后台开通 scope 和 User 实际 OAuth 同意是两层要求；资源分享又是第三层。样例 `required_user_scopes` 只列续期器强制校验的三个基础项，部署者须补齐业务所需 scope。用 `lark-cli <相应命令> --help` 和真实 `missing_scopes` 核对，不申请 `all`，不使用 `--recommend`，不盲目授权导出云文档。


CLI 1.0.97 随附说明明确列出的 IM scope：User 发消息/回复需要 `im:message.send_as_user` 和 `im:message`；消息读取需要 `im:message:readonly` 与 `im:chat:read`；只在部署时核验群成员才另需 `im:chat.members:read`。Bot 发消息/回复需要 `im:message:send_as_bot`。读取代码显式 `--no-reactions`，无需为这条链路额外申请 reaction scope。以上加进对应 App 权限和 User OAuth scope，不能只填三个续期基础项。

当前原生 CLI schema 同时列出细粒度 `docs:permission.setting:write_only`（协作者设置）与 `docs:permission.member:transfer`（所有权转移）等可接受权限；这些是 Bot 报告治理的配置参考，非所有操作的完整集合，也不是要求申请 schema 列出的全部广域替代 scope。Sheets 读取/写入、文档创建、复制和协作者增删仍须按本项目实际启用的 CLI 命令/当前组织审批核验；资源访问权和 API scope 都验收成功才可启用。

## 首次安装（目标机管理员操作，不是业务启动）

1. 准备独立 settings 和 isolation manifest，两者的 App、User、home、CLI、profile、加密副本路径必须一致。manifest 的 `expected_cli_version` 填实际受验版本，`auth_maintenance.check_interval_minutes` 建议 60。同 Windows 用户运行 CLI、业务和维护服务；DPAPI 不能跨账户/跨机复制。
2. 在专属 home 初始化两个**新** profile。此时没有任何业务 worker；初始化工具拒绝已有 CLI 配置。它使用隐藏输入读取 Secret，经 stdin 分别调用 `config init --app-id ... --app-secret-stdin --name ...`，再设置用户 `strict-mode user` 和报告 `strict-mode bot`。HOME/USERPROFILE 仅传给子进程，不修改系统或全局默认 profile。

```powershell
# 在仓库根运行；PrivateSettings 是目标部署的真实私有配置。
python tools/initialize_lark_profiles.py --settings $PrivateSettings
```

初始化不 OAuth、不发消息。`tools/lark_project.py` 后续通过 isolation manifest 对工作区、App 和 profile 做绑定校验。已有安装用受控维护入口，不重复初始化。

3. 仅 User 进行首次 split-flow。将本次 `verification_url` 原样交给固定 User，并用同一 home/profile 执行 `auth qrcode <URL> --output <相对PNG路径>` 展示二维码。本人完成后再提交本次 device code；Agent 场景必须先交还控制权，再等本人确认。不要将 device code/token 写入聊天或仓库。

```powershell
python tools/lark_project.py --workspace $Workspace --manifest $Isolation --bootstrap --profile-kind user -- auth login --scope $ApprovedScopes --no-wait --json
# 本人确认完成后；DeviceCode 仅在目标机私有流程内保存。
python tools/lark_project.py --workspace $Workspace --manifest $Isolation --bootstrap --profile-kind user -- auth login --device-code $DeviceCode --json
python tools/lark_project.py --workspace $Workspace --manifest $Isolation --bootstrap --seed-replica
```

seed 会强制核对 App、固定 User、Bot-only 和密级 scope 后，仅保存官方 CLI 已加密 DPAPI 记录，不解密 token；已有副本拒绝覆盖。从此不再使用 bootstrap。CLI JSON 成功以 `ok==true` 与退出码判断，不以顶层 `code==0` 判断。

## 续期、撤销和重新授权

正常业务距 access token 到期超过 15 分钟只读本地状态；必要时才 `auth status --verify`，瞬时失败有限复核。所有业务/维护进程共用 home/App 凭据锁，防止 refresh token 轮换互相覆盖。周期维护执行 `deployment/refresh-feishu-user-auth.ps1 -ManifestPath <manifest>`；只读检查加 `-CheckOnly`。这些入口不派发车辆任务、不发布报告。

已有副本且 refresh token 确认不可用时，用本项目 `deployment/feishu_project_oauth.py --manifest <manifest> --initiate`，本人完成后再 `--complete`。不删除副本、pending journal 或旧健康记录来伪造恢复。`20140` 交应用管理员检查 OAuth/Device Flow、发布权限及安装状态。普通网络错误不等于授权撤销。Bot 缺权限由管理员配置，不对 Bot 执行用户登录。

## 消息可靠性和部署验收

发送前持久化意图与幂等键，只有 CLI 返回真实 `message_id` 才确认已派发；部分派发或发送结果未知时保留证据，不整单重发。接收靠同一 User 有界分页轮询，不要求额外消息接收 Bot/Webhook。校验阿斯加德身份、来源群、精确 VIN/时间及本次发送边界；冲突、截断和歧义必须停止，不能取最近卡片。

时延报告需完成权限收紧、原群查看、所有权转移、L3 和必要临时权限撤销后才交付。走查查询要求精确 reply/线程归属，返回的是查询时制品信息。最终成功以真实来源绑定的 Bot/CC Connect 回执为准。离线 mock 测试不证明新环境权限或真实群消息可用；首次线上验收必须使用明确获准请求，由接入人员核对完整送达证据。
