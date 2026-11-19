[返回项目介绍](../README.md)

以下命令均在仓库根目录执行；配置示例不包含本人账号、授权或业务数据。

# 走查测试

同一车辆两个时段的智能底盘日志走查与对比服务。通过 CC Connect 接收飞书自然语言请求，独立从车云获取日志，在 Windows 原生完整分析器中统计，交付一份主报告和两份原生飞书明细表。

当前源码基线：`0.8.1-windows-integration-target.16`，执行标记 `walkthrough-target16-hardening`。本仓库按当前生效实现整理；新环境默认关闭业务执行，完成配置和验收后再启用。

## 功能

- 同 VIN 两窗并行处理；默认第一段为 Base、第二段为 PCM 更新，显式角色优先，不按时间先后重排。
- 未标注时区时按北京时间解析；支持中文日期、全角标点和同日简写。完整时区时间保留语义，无日期且无法验证时询问，不猜测日期。
- 每个时窗独立按 Prod → Test 查询，五类来源全部查询成功且为空才回退。同窗 AD、PCM、CPU 保持同环境；查询异常明确失败。
- 冻结完整规则快照供双窗共用。当前已验口径为 201 项，未来按完整快照执行，不按 201 项截断。
- 按精确来源消息去重、排队、恢复；支持逐文件下载校验、有限重试、检查点和失败诊断。
- 完整交付：主 Docx 的 48 格统计表，以及 Base / PCM 更新各一份七子表 Sheets。主报告链接到真实明细；缺失或未解析数据明确显示。
- 固定 User 读取规则/原模板、查询可信制品码并处理必要密级；独立 Bot 创建报告、治理权限并回复原消息。报告所有权交给本次请求者。

本仓库对应“走查测试”入口。“走查测试New”仍停用；AD P99、Uroad 仅包含公共路由所需的纯解析模块，不包含它们的业务执行服务。

## 请求示例

以下 VIN 为合成测试值；实际请求需替换为获准车辆。

```text
走查测试 DEM0VEH1CLE000001
Base：2026-11-03 13:10 至 13:17
PCM 更新：2026-11-03 09:53 至 10:10
```

用户无需输入命令。CC Connect 必须另行传入真实入站上下文；不能从正文推断或伪造发送者、群、来源消息。部署、代码上传、README 编写及历史状态追问不创建走查任务。

## 仓库结构

```text
README.md                         功能、快速开始和文档索引
docs/                             架构、配置、部署、运维、测试与变更说明
examples/                         无真实身份及端点的配置样例
tools/                            源码检查、候选安装、测试和调度服务安装工具
workspace/
  AGENTS.md                       CC Connect 接入约束
  .agents/skills/                  技能入口、公共意图与时间解析
  deployment/                     宿主、调度器、授权维护与 OAuth 工具
  runtime/wtr096-hardening/skill/  当前走查完整适配源码、测试、模板和契约说明
SOURCE-PROVENANCE.json             导出来源及与运行版本的差异说明
REPOSITORY-MANIFEST.json            提交文件的 SHA-256 清单
.gitlab-ci.yml                     静态检查和纯输入解析回归
```

`runtime` 中提交的是版本化源码；Python 环境、分析器依赖、业务运行数据均在部署时准备。

## 快速开始

1. 阅读 [部署说明](../docs/deployment.md) 和 [依赖清单](../docs/dependencies.md)，准备 Windows x64、独立 CPython 3.11、仓库内完整分析器源码及 Git 原生工具、飞书 CLI。
2. 校验仓库内容：

   ```powershell
   python -B -X utf8 tools/check_repository.py
   ```

3. 用 CPython 3.11 在本地下载锁定 wheel，再生成**全新目录**的候选部署：

   ```powershell
   & $Python311 -I -m pip download --only-binary=:all: --require-hashes `
     -r workspace/runtime/wtr096-hardening/skill/requirements-windows-native.lock `
     -d $Wheelhouse
   & $Python311 -B -X utf8 tools/prepare_workspace.py `
     --workspace $NewWorkspace --base-python $Python311 --wheelhouse $Wheelhouse
   ```

   依赖锁针对 Windows CPython 3.11 x64；下载命令也在该平台运行。候选安装不覆盖已有部署、不初始化授权、不创建车辆任务。

4. 按 [配置说明](../docs/configuration.md) 填写候选目录的私有样例，配置隔离身份、完整规则、模板桥接及可信制品查询。安装调度任务，运行宿主只读预检和规则核验。全部通过后再启用。
5. 使用独立解释器运行离线回归：

   ```powershell
   & $HostPython -B -X utf8 tools/run_tests.py
   ```

## 运行与恢复

以下命令在**已部署工作区**运行。`$OriginalText`、`$ContextHeader` 和 `$ReceivedAt` 必须来自同一入站请求。

```powershell
$Install = Get-Content deployment/walkthrough-test.installation.json -Raw | ConvertFrom-Json
& $Install.host_python -B -X utf8 deployment/walkthrough_entry.py start `
  --text $OriginalText --ccconnect-context $ContextHeader --received-at $ReceivedAt

# queued 后仅跟踪返回的精确 record_path
& $Install.host_python -B -X utf8 deployment/walkthrough_entry.py wait `
  --record $RecordPath --wait-seconds 45
```

`status --record` 读取精确任务，`resume --record` 请求恢复原任务。未知版本、来源、身份或最终结果绑定不匹配时停止；不以“最近任务”关联，也不通过重建报告掩盖未知发送结果。详见 [运维与故障处理](../docs/operations.md)。

## 性能与验证

双窗查询并行、单窗四路下载、全局单项重型分析与最多两个发布线程；PCM/CPU 准备和 AD 分析重叠。七子表快照仅在同任务、同角色、同 token 且 revision 未变化时复用，写入后仍完整回读。

只读测量中，重复模板读取从 17 次调用约 21 秒降至 1 次约 1.3 秒；这是该读取阶段的收益，不能当作整单耗时。测试结果、环境跳过项和线上验收边界见 [验证说明](../docs/validation.md)。

## 维护与交付资料

- [架构和数据流](../docs/architecture.md)
- [配置字段与身份边界](../docs/configuration.md)
- [Windows 部署与回退](../docs/deployment.md)
- [外部依赖和制品](../docs/dependencies.md)
- [运维、授权与故障排查](../docs/operations.md)
- [测试与验收](../docs/validation.md)
- [版本变更](../CHANGELOG.md)
- [贡献和提交规范](../CONTRIBUTING.md)

真实 settings、凭据、会话标识、日志、任务记录和报告不进入 Git。完整分析器的 12 个 Python 模块及 o.ini 已包含，历史私有默认值已清除；第三方运行时和工具二进制通过获准渠道准备。详见 [分析器来源清单](../docs/ANALYZER-SOURCE-PROVENANCE.json)。

飞书接入不可省略：[lark-cli 与阿斯加德完整配置](../docs/lark-asgard-integration.md) 说明首次双 profile 初始化、用户 OAuth/DPAPI 副本、续期和可信查询关联；实际调用代码为 product_version.py/report_policy.py。

## GitHub 源码检查

GitHub Actions 在提交、合并请求或手动运行时执行轻量的语法与完整性检查。检查不启动业务服务、安装器、实验、训练、台架或部署；通过范围以 `.github/workflows/source-checks.yml` 为准。
