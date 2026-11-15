# 外部依赖

本仓库包含业务适配源码、测试、主表 XML 和接口契约，不打包本机运行时、线上数据或第三方安装制品。

| 依赖 | 准备方式与核验 |
| --- | --- |
| Windows x64 / CPython 3.11 | 独立解释器和 venv；不复用全局环境，不启用 WSL |
| Python 包 | `requirements-windows-native.lock` 是完整 Windows 环境；使用 `--require-hashes` 安装；`requirements-host.txt` 仅宿主子集 |
| 完整 AD 分析器 | 本仓库 `workspace/runtime/approved-full-analyzer` 已包含经脱敏的完整源码，候选安装自动复制；包含 `common/`、`general_performance/` 及 `general_performance/o.ini`；`analyzer_contract.py` 固定校验完整模块与 AST 契约，原始版本及明确脱敏分发变体均固定 AST，未知代码拒绝 |
| Git 原生工具族 | 固定 Git for Windows 归档的 `usr` 工具及 DLL，gawk 5.3.2 / grep 3.0；不只拷贝 exe、不修改全局 PATH |
| 飞书 CLI | 目标组织获准版本，与隔离清单中的版本一致；User 和 Bot 各自独立 profile |
| CC Connect | 提供真实来源消息、发送者和群上下文，接入本仓库 AGENTS 与 Skill |
| 车云 | 获准用户名、Prod/Test 原点与下载主机白名单；值只写入私有配置 |
| 飞书资源 | 获准完整规则、原七子表模板、真实 ready 桥接记录、可信制品查询群及阿斯加德身份 |

`EXTERNAL-DEPENDENCIES.json` 保留固定工具来源 URL / SHA 信息；`WHEELS-PROVENANCE.json` 保留原受验 Windows wheels 的来源和散列，不表示 wheel 已提交到仓库。优先使用组织制品库，下载后核对完整依赖锁；锁定版本不可用时按受控依赖更新流程处理，不临时换最新版。

`tools/prepare_windows_tools.py --help` 可查看 SHA 固定归档的解包参数。它只准备新目录，不运行安装器、不修改系统 Git。分析器的已批准运行适配及说明保存在 `skill/analyzer-execution-fix.patch`、`ANALYZER-FIX.md` 和 `scripts/native_execution_compat.py` 中；不可跳过契约校验来接受任意外部版本。
