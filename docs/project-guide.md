# 使用与验证

[返回项目介绍](../README.md)

本页保留具体使用、环境和验证入口；命令均从仓库根目录执行，示例路径需按自己的环境替换。

同一车辆两个时段的智能底盘日志走查与对比服务。通过 CC Connect 接收飞书请求，获取日志并调用 Windows 原生分析器，交付一份主报告和 Base/PCM 更新各一份七子表明细。

项目提供完整业务适配源码、模板、测试和独立调度工具。实际服务需要 **Windows x64、CPython 3.11**、获准车云来源及飞书接入；新候选环境默认关闭业务执行。

## 使用方式

在已部署的入口发送自然语言请求。以下 VIN 为合成示例：

```text
走查测试 DEM0VEH1CLE000001
Base：2026-11-03 13:10 至 13:17
PCM 更新：2026-11-03 09:53 至 10:10
```

- 显式角色优先；没有角色时按输入顺序解释，不按时间先后重排。
- 未标注时区时按北京时间处理；无法确定日期时先补齐。
- 两个时窗分别获取同窗一致来源的数据，冻结同一份完整规则快照后统计。
- 主报告链接到两份实际明细；缺失或未解析项在报告中保留说明。

接入层需传入同一请求的真实身份、消息和接收时间。任务按来源去重与恢复，完成状态要求报告资产治理和原消息送达凭据。

## 获取源码与校验

```powershell
git clone https://github.com/Inner-vic/walkthrough-test.git
cd walkthrough-test
python -B -X utf8 tools/check_repository.py
```

上述检查不取日志、不启动任务。完整分析器源码已入库，运行依赖和原生工具在部署时另行准备。

## 新环境部署

1. 按[依赖清单](../docs/dependencies.md)准备 Windows CPython 3.11 x64、飞书 CLI、Git 原生工具及分析器依赖。
2. 在该平台按[散列锁定清单](../workspace/runtime/wtr096-hardening/skill/requirements-windows-native.lock)下载 wheel，然后生成全新候选工作区。

   ```powershell
   # 以下均为示例路径，先替换为自己的解释器、wheel 目录和新工作区。
   & "C:/Path/To/Python311/python.exe" -I -m pip download --only-binary=:all: --require-hashes -r workspace/runtime/wtr096-hardening/skill/requirements-windows-native.lock -d "D:/packages/walkthrough-py311"
   & "C:/Path/To/Python311/python.exe" -B -X utf8 tools/prepare_workspace.py --workspace "D:/workspaces/walkthrough" --base-python "C:/Path/To/Python311/python.exe" --wheelhouse "D:/packages/walkthrough-py311"
   ```

3. 按[配置说明](../docs/configuration.md)填写候选私有配置，核对 User/Bot 身份、规则、模板与可信查询来源。
4. 按[部署说明](../docs/deployment.md)配置独立调度、预检和规则核验，完成授权首单验收后启用。

准备工具会创建 venv 并安装锁定依赖，但不会授权、启动任务或覆盖已有部署。已有服务升级使用版本目录，保留原任务与回执。

## 源码结构

| 路径 | 内容 |
| --- | --- |
| `workspace/.agents/skills/` | 走查 Skill 入口、公共路由及时间解析 |
| `workspace/runtime/wtr096-hardening/skill/` | 当前版本业务适配、模板和测试 |
| `workspace/runtime/approved-full-analyzer/` | 原生完整分析器源码 |
| `workspace/deployment/` | 宿主、调度与授权维护 |
| `tools/`、`examples/`、`docs/` | 部署工具、配置样例和维护说明 |

这里的 `runtime/` 收录版本化源码，不包含业务运行数据。共享解析模块也不代表已部署日志分析或时延分析服务。

## 验证与维护

完整离线测试入口为 `tools/run_tests.py`，须由准备后的 CPython 3.11 独立解释器运行。GitHub 的 [Source checks](../.github/workflows/source-checks.yml) 检查源码和提交清单；离线、环境跳过项与真实服务验收分别记录。

- [架构与数据流](../docs/architecture.md)
- [运维、状态与失败恢复](../docs/operations.md)
- [测试与验收说明](../docs/validation.md)
- [飞书接入](../docs/lark-asgard-integration.md)
- [版本变更](../CHANGELOG.md)与[公开副本的验证范围](../PUBLICATION.md)

本仓库对应“走查测试”入口，“走查测试New”仍停用。凭据、真实日志、用户会话、任务记录和业务报告不随源码分发。
