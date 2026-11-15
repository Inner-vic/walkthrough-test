# 部署补充：阿斯加德固定群版本查询

本说明沿用r2已经核验的固定群、身份、授权、回复接收与首单标准，作为本版Windows包的身份诊断参考。Windows运行兼容层已有明确变更，不再宣称所有脚本与r2逐字相同；本版部署以根目录DEPLOY-WINDOWS.md和LOCAL-VALIDATION.json为准。不要用示例settings重建真实配置或授权。本群ID是用户指定的内部部署信息，不包含访问凭据。

## 发到哪个群、由谁发

固定查询群：`REPLACE_APPROVED_ID`。

部署Agent须核对**现有走查测试真实settings**中的`product_version.query_chat_id`等于上述值；不按群名猜测，不使用发起走查请求的来源群代替。字段正确时无需改动。若不符或缺失，先暂停新走查任务，盘点旧任务并备份真实settings；新群配置只用于新任务。旧任务的查询checkpoint和最终结果都绑定原群/身份，须用备份的原配置结束或恢复，不能改快照或checkpoint强配新群。其他身份与配置沿用现有值，不以settings.json.example覆盖真实配置。

发送者为现有`lark_profile`绑定的**固定授权User**，身份须通过`expected_operator_open_id`、`expected_lark_app_id`及独立`lark_home`校验。不是报告Bot，也不是每次发起走查测试的工程师。固定User应已加入查询群，并有现有CLI所需的发消息、读取消息和话题权限；阿斯加德也应已在群中。授权失效须按原环境恢复同一账号，不切换默认账号绕过。

代码发送的内容是：

```text
@阿斯加德 查询车辆版本 <本次VIN> CCUFSD
```

实际@对象来自`product_version.asgard_bot_id`。这里只查制品信息，不发送`fsdlog`，日志分析继续在本地执行。一个走查任务只发一次查询，与两个时段的本地日志处理并行。

## 配置字段对照

| 现有settings键 | 用途与部署要求 |
| --- | --- |
| `product_version.query_chat_id` | 使用上面指定的固定群ID。 |
| `product_version.asgard_bot_id` | 阿斯加德机器人的`ou_...`身份，用于@及回复来源校验；沿用已验证配置，不填发送者User的open_id。 |
| `product_version.asgard_app_id` | 可选的阿斯加德`cli_...`应用身份，配置后严格校验；与发送方的`expected_lark_app_id`用途不同，不能互相替代。 |
| `lark_profile` / `lark_home` | 现有隔离User身份及授权目录，发送和收回复均使用它。 |
| `expected_operator_open_id` / `expected_lark_app_id` | 固定发送者User及其调用应用身份，沿用原环境。 |
| `product_version.poll_interval_seconds` | 未指定默认2秒；是两次读取之间的等待间隔，接口调用耗时另计。 |
| `product_version.timeout_seconds` | 未指定默认120秒的回复等待预算；发送和接口调用也会耗时，不是整个任务的严格总耗时。 |
| `report_lark_profile` | 独立报告Bot，只负责主报告、两份明细及来源回复，不用于向阿斯加德查询。 |

群ID不能替代User授权或机器人身份。部署时必须沿用与当前调用应用配套的已验证ID；更换应用或身份后不能盲目复用旧open_id。缺失的必填项会被配置校验拒绝，不能用示例占位符上线。

执行方的两个profile必须不同，但都属于顶层`expected_lark_app_id`指定的同一项目应用。`report_lark_profile`必须bot-only：Bot可用、User不可用。目标阿斯加德应用是`product_version.asgard_app_id`，它与执行方项目App的用途不同；目标机器人的`asgard_bot_id`和`asgard_app_id`必须指向同一个阿斯加德机器人。不要为通过校验把两种App填成同一个。

## 可执行的配置与身份核验

从现有宿主工作区运行，使用当前宿主相同的Windows登录账号/服务账号、独立Python和身份目录。另一个Windows账号不能假定能解密原DPAPI授权。以下变量取当前环境，不使用示例配置重建环境：

```powershell
$Inspect = "$UpgradeRoot/upgrade/pinned_lark.py"
# $HostPython / $UpgradeRoot / $ExistingWalkthroughSettings 沿用UPGRADE.md中的实际值。
& $HostPython -X utf8 -B $Inspect --help

# 帮助读取不执行业务。先确认目标CLI支持下面使用的命令与flags；失败时停止，沿用环境维护流程适配，不盲目更新CLI。
$Cli = (Get-Content -LiteralPath $ExistingWalkthroughSettings -Raw | ConvertFrom-Json).lark_cli
& $Cli --version
& $Cli auth status --help
& $Cli auth login --help
& $Cli im +chat-members-list --help
& $Cli im +messages-mget --help
& $Cli im +chat-messages-list --help
& $Cli im +threads-messages-list --help

# 先读本地授权元数据；不加--verify时不验证服务器。
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth status --json
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind report-bot -- auth status --json
```

启动器从原settings读取`lark_cli`、`lark_home`、profile和宿主工作区，使用原锁及隔离环境，显式附加profile，不切换全局默认身份、不输出settings或token。CLI的auth status输出是授权元数据；只向交接记录摘录`appId`、User的`openId/available/tokenStatus/expiresAt/scope`及Bot的`available`，不粘贴整个配置或凭据目录。

User的`appId`须等于`expected_lark_app_id`，`identities.user.openId`须等于`expected_operator_open_id`，且User可用、tokenStatus为valid、expiresAt尚未到期。报告profile须同App且bot-only。调用失败、字段缺失或身份不符即暂停新的走查，不根据当前登录用户反写固定身份来绕过。读取元数据成功不等于群可访问。

需要确认服务器有效性时，再明确执行以下验证；它可能刷新同一账号现有token，不发起新OAuth：

```powershell
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth status --verify --json
```

固定身份字段缺失时，先从目标当前已批准配置、部署记录及同一隔离profile的auth status核实。核实操作者本人及执行方App后才补齐固定值；没有证据则暂停交由环境维护者提供，不能猜值或使用本机另一个应用的ID。

启动器要求原固定身份字段齐备，不用于自动挑选账号。若因缺`expected_operator_open_id`或`expected_lark_app_id`连本地状态都无法检查，先由目标已有批准部署记录补齐；没有该证据就停止并请环境维护者确认，不能临时填随机ID启动工具后再反写。

## User授权过期或缺权限

1. `--verify`可刷新现有授权时，重新核对同一User/App后继续；普通网络错误不等于需要换账号或重新授权。
2. 明确提示授权撤销、refresh token无效或需要本人授权时，由**同一固定User**重新授权。若真实settings含`auth_credential_replica_path`，必须沿用现有宿主的受控OAuth/加密副本维护入口；本启动器会拒绝直接auth login。未知原入口时暂停并联系该环境维护者，不复制其他电脑/账号的加密凭据、不删副本或checkpoint。
3. 未启用加密副本且原环境使用官方CLI直接授权时，可通过同一启动器走split-flow。`$NeededScopes`取当前错误明确报告的缺失scope及当前业务所需scope，不能填示例字符串、盲目申请all或缩减原已交付链路权限：

```powershell
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth login --scope $NeededScopes --no-wait --json
# 将本次返回的verification_url原样展示给固定User，并生成二维码；PNG路径相对宿主工作区。
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth qrcode $VerificationUrl --output "walkthrough-auth.png"
# Agent把链接和二维码交给该User，结束当前等待；收到本人确认完成后，Agent执行：
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth login --device-code $DeviceCode
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- auth status --verify --json
```

`$VerificationUrl`、`$DeviceCode`必须来自本次授权，不放入部署包、不复用旧流程值。身份不符立即停止，不修改固定User来迎合扫码者。Bot缺scope由执行方应用管理员按错误中的后台地址处理，不对报告Bot执行auth login。

IM业务需要User发送、消息读取及话题读取能力；成员核验还可能额外需要`im:chat.members:read`。应用管理员开通与User实际授权两层都应满足，以目标CLI schema和真实`missing_scopes`为准，不把本地preflight当scope验收。业务代码使用`--no-reactions`读取，不为诊断额外要求reaction权限。

## 阿斯加德ID缺失时如何核实

使用**同一隔离User profile**读取指定群成员；这是只读诊断，不发送消息：

```powershell
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- im +chat-members-list --as user --chat-id REPLACE_APPROVED_ID --member-types user,bot --member-id-type open_id --page-all --page-limit 10 --format json
```

在`users[]`核对固定User；在`bots[]`核对阿斯加德的名称、`member_id`和`app_id`。名称只能辅助选候选，不能单独定身份。`has_more=true`或`truncations`非空表示不完整，不能因列表未见某成员就判定不在群；继续按CLI分页能力核对或由群管理员确认。

用该群里**已知且经过人工确认的阿斯加德回复消息ID**再独立读取：

```powershell
# $KnownAsgardReplyId来自该群现有已确认回复，形如om_...，不是任意临近卡片。
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- im +messages-mget --as user --message-ids $KnownAsgardReplyId --no-reactions --format json
```

核对`chat_id`为固定群，`sender.open_bot_id`为配置的`asgard_bot_id`；若配置目标App，还须`sender.id_type=app_id`且`sender.id`等于`asgard_app_id`。这两项是当前执行应用实际看到的身份，不直接采用另一应用或另一profile的open_id。拿不到可靠候选、消息不可读或映射矛盾时停止补值，交由群/应用维护者确认；本地格式校验无法证明这些ID真实正确。已知历史回复只用于核验机器人身份，不能把历史四位码当作新任务的查询结果。

## 怎么接收信息

当前实现是**同一User通过lark-cli主动轮询读取**，由`scripts/product_version.py`执行；这条查询分支不要求另建消息接收Bot、Webhook或事件回调服务。

1. 发送前保存发送意图与幂等键；收到回执后保存本条查询的`message_id`。
2. 先用`im +messages-mget --as user`读回原查询，核对群、VIN、发送者和查询文本。
3. 若有话题ID，使用`im +threads-messages-list --as user`读取对应话题；普通群回复则在固定群有界查询候选，再独立读回候选消息。
4. 只接受配置的阿斯加德身份、且精确关联本条查询的回复。普通回复要求`reply_to`指向查询消息；话题回复核对话题和查询唯一性。不根据卡片相邻、同VIN或出现四位码推断归属，有歧义的回复不认领。
5. 从回复解析“相关制品信息”的四位十六进制编码，连同查询及回复来源证据写入最终结果。

返回值是**查询时平台提供的制品信息**，不冒充两个历史测试时段各自的安装版本。

## 状态、失败与恢复

主任务记录为`${runtime_state_dir}/${job_key}.json`；制品分支状态为`${runtime_state_dir}/${job_key}.work/product-version/product-version.json`，保存查询消息ID、群、身份绑定、阶段和结果；主任务记录会更新`product_version_stage`。变量取现有任务实际值，不从示例路径猜测。不要把包含VIN和消息正文的运行记录提交到公开仓库。

- 超时或读取失败：保留本地日志统计，在报告中标明制品信息未获取；这不是日志解析失败。
- 发送结果未知：保留原状态人工核对，不自动重发。
- 回复来源或关联校验失败：拒绝取值，不抓取群里最近一个四位码补上。
- 同版本未完成分析任务恢复时，已发送的查询只继续读取原消息；已有最终快照的任务只恢复交付。最终快照中的制品信息缺失，不因补发报告重新查询或改写快照。
- 普通群回复的发现限定在原查询发送后的24小时有界读取范围，超出时停止认领，不自动另发查询；话题回复按原话题读取。

| `product-version.json`中的phase | 实际处理 |
| --- | --- |
| `prepared` | 发送意图已保存，尚未发；正常worker可执行一次发送。 |
| `sent`、`timeout`、`poll_failed` | 同版本无最终快照恢复时只读取原查询；不重新发。已有最终快照时只恢复交付。 |
| `ready` | 复用已验证结果，不重复查询。 |
| `send_pending`、`unknown` | 发送可能已发生但回执未知，人工核对，不自动重发。 |
| `failed` | 复用已存不可用结果；修复权限后resume也不会抛弃该终态重新抓值。需新查询时通过新来源消息创建新任务，不删checkpoint。 |

配置缺失、固定User/App不符、群/身份/checkpoint或最终快照绑定不符会抛错，须停止整项走查执行并排查，不能以“仅缺制品”绕过。正常超时、读取失败或回复来源/关联/编码校验未通过会保留相应不可用状态，本地统计可保存并按partial说明。群里存在四位码不等于该任务已成功认领。

排错优先读取原查询和checkpoint。可用上面的`+messages-mget`读取查询ID；有`thread_id`时用以下只读命令核对话题，仍不能通过附近卡片替代原关联：

```powershell
& $HostPython -X utf8 -B $Inspect --settings $ExistingWalkthroughSettings --skill-root "$UpgradeRoot/walkthrough-test" --profile-kind user -- im +threads-messages-list --as user --thread $ThreadId --order asc --page-all --page-limit 10 --no-reactions --format json
```

## 部署验收

继续按顶层`UPGRADE.md`执行升级前后受管文件检查和本地preflight。本地preflight不会发送查询，也不证明查询群或远端读取权限已通过。

当前preflight只核对本地配置格式、固定User/App、token到期、依赖和源码契约等；不证明IM scopes、报告Bot-only、真实群/成员/机器人映射、所有CLI子命令或远端发布权限。本节的授权状态和只读群/回复检查用于补齐这些部署核验；真实发送与自动关联仍由首单验收确认。启动器只转交本节白名单命令，不能用它发送额外测试查询。

部署后首单核对：消息由固定User发到上述群；阿斯加德回复被本条查询认领；任务记录和报告中的四位码与回复一致；本地日志两时段仍独立并行；主报告及两份明细可打开。若超时，先看状态和对应回复，不手工在群里重复发送以掩盖问题。本次补充仅核对代码和说明，未向群发送测试消息。
