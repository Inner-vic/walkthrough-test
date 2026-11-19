# 原生分析契约

在排查依赖、来源选择、统计差异或缺失时阅读。原生分析仍使用完整规则，不把六指标提取替代完整统计。Windows执行候选增加受控宿主/工具兼容边界，部署与状态说明见[WINDOWS-NATIVE.md](../WINDOWS-NATIVE.md)。

## 执行与输入

默认保持Windows经现有WSL、Linux本地调用的配置。Windows原生必须显式`execution="windows"`，使用本地CPython3.11 x86-64、明确的Git工具目录和实际离线工具/时区探针；不根据宿主OS悄悄切换后端。所有模式都只接受已批准的12模块源码契约和完整extract_logs方法；未知源码变化停止适配，不自动学习新摘要，不拿研究副本覆盖最新版。完整源码与规则使用独立依赖目录，其他最新版部署目录保留。规则在任务内一次快照，两窗共享。

`source-boundary-v2` 从受信外层类别保存basename→来源映射，各来源按原前缀分别保留结束后首个边界文件。原时间谓词、排序、行序、状态清理、阈值与公式保留；同来源同秒边界沿用实际glob及原生稳定顺序，不把全部候选加入统计。可能参与统计的同名跨源冲突拒绝；仅已证明各源都不会消费的冲突可审计排除。

结果及缓存保留 `analyzer_contract`、`runtime_execution`、`execution_compat` 和 `execution_compat_runtime`。实际INI/RecordPerformance窗口、候选成员、实际提取顺序/选集及SHA均须匹配。无最终快照的旧中间缓存不能认领成新版结果；恢复规则见EXECUTION.md。

Windows另保留`windows_host_runtime`和`windows_execution`，绑定实际Python、gawk/grep、兼容层与进程隔离helper的SHA。Windows新执行profile为`m100-full-native-audited-v10-windows-native`，原Linux/WSL仍为v8；只改profile文字不能通过真实工具/窗口审计。旧completed不动；已有可识别的有效final只恢复交付，不能因后端变更重新查询、下载或统计。

## 兼容执行

`portable-brake-hex-spawn-v2` 只将两个完整十六进制字段显式转换后，沿用原位运算和分类；异常字串保持原运行器行为，不强行给业务gate出值。批准AST与替换片段须匹配。

原分析器使用spawn ProcessPool。通过initializer传播受控策略，核验child实际调用、原五参数窗口、实际mawk -f程序SHA及退出码；父进程静态安装记录不代表子进程生效。没有真实MCU输入时不能伪造分类样本；CSV及统计正确性由对应真实/合成回归另证。未签名审计JSON不构成加密认证。

Windows把原逻辑mawk/grep调用映射到明确配置的Git gawk/grep，不伪造mawk可执行文件。兼容层仅做已验、可逆的AWK标识符/动态正则输入适配及所需Windows执行适配，保存原程序与实际程序摘要、子进程和退出码；统计公式、阈值与原规则不在该层改写。固定子进程`TZ=CST-8`、`LC_ALL=C`和UTF-8；Windows CRT不能用`TZ=Asia/Shanghai`替代已验+08:00行为。Job Object先隔离再放行分析，超时只终止本次树；隔离失败拒绝启动，不降级到仅kill父进程。

两个真实时段的Windows私有原型完整201项统计已与基准一致；最终integration目录、逐格发布/主表组合及目标首单验收仍由主流程收据确认，不据此宣称上线。日志缺失/partial继续保留，不因执行平台更换声明已恢复数据。

## 缺失与归整

`native-missing-input-shapes-v1` 只解释六项已知空指标的实际原始形态：跟踪、告警或类型注册中的其他数字不能替代目标字段。只有原字段无数字且形态均已识别时才解释；未知格式、有数值但原统计为空、无法严格解码时保留未归因，并保留已完成原生统计。来源、窗口、路径及SHA硬校验不放宽。

XVM Zstd仅在EOF而压缩帧未结束时保留已解码完整行，舍弃最后未结束行，逐文件保存哈希和恢复审计，来源/结果仍partial。discarded_tail_bytes仅为已解码但舍弃的尾行字节，不代表总丢失量。checksum错误、二进制/NUL、无完整行、超限或不安全归档仍拒绝。

AD FPS、PCM FPS独立取数。缺少一侧保留另一侧，不补零、不生成不成立的Δ或颜色。源日志首末时间、下载包数、全规则目录或模板格数，都不能单独证明连续采集、全部有效样本或已恢复数据。
