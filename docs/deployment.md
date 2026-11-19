# Windows 部署与回退

本仓库提供新工作区部署准备。已有生产工作区应先备份并逐文件兼容合入，不能把 `workspace/` 整目录覆盖过去。

## 1. 校验与准备候选

在仓库根执行 `python -B -X utf8 tools/check_repository.py`。按 README 准备锁定 wheel，再运行 `tools/prepare_workspace.py --workspace <不存在的绝对目录> --base-python <CPython3.11.exe> --wheelhouse <wheel目录>`。脚本拒绝现有目标、链接/联接及非 3.11 x64 基础解释器；创建独立 venv、安装散列锁定依赖并生成禁用候选。

完整分析器源码随候选自动复制；Git 完整工具放入候选专用依赖目录；CLI 和 CC Connect 按组织流程安装。此阶段不创建业务任务，不读取线上日志或发布报告。

## 2. 配置和授权

将 `private/walkthrough.settings.json.example`、`private/walkthrough-cloud.settings.json.example` 及 `deployment/cc-connect-lark-isolation.json.example` 复制为去掉 `.example` 的本机文件，逐项填写批准值。按 [配置说明](configuration.md) 准备 User/Bot、规则、查询群和真实模板桥接。保留 `execution_enabled=false`。

`feishu_auth_maintenance.py` 在本仓库中接到走查自身 `report_policy.py`，无需部署 AD P99 worker；授权身份、凭据锁、续期判定和健康记录契约保持。外部部署应由与飞书授权相同的 Windows 用户执行，DPAPI 与用户态凭据不能跨账户照搬。

## 3. 安装独立调度

在目标候选目录已配置完成后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/Install-Dispatcher.ps1 -Workspace $NewWorkspace
```

脚本读取目标清单，核对目录和解释器，注册 `CC Connect Walkthrough Dispatcher` 当前用户交互态任务并写入对应散列清单；已有同名任务直接拒绝覆盖。因此同一用户的多个安装不能并行抢用此固定服务。该工具不启动任务、不打开新窗口；候选仍禁用。

## 4. 预检与启用

```powershell
$Install = Get-Content "$NewWorkspace/deployment/walkthrough-test.installation.json" -Raw | ConvertFrom-Json
& $Install.host_python -B -X utf8 "$NewWorkspace/deployment/walkthrough_entry.py" preflight
& $Install.host_python -B -X utf8 "$NewWorkspace/deployment/walkthrough_entry.py" rules-check
```

独立 preflight 只读，不能用于判断自动续期已成功；rules-check 会读取获准规则但不创建车辆任务。目标身份、完整依赖/原生工具、来源契约、模板桥接、调度服务和完整规则均核验后，记录验收证据。更新安装清单 `settings_sha256` 为最终配置 SHA-256，确认 `source_manifest_sha256` 与该版本源码清单相符，再将 `execution_enabled` 改为 true。

将 `workspace/AGENTS.md` 和对应 Skill 接入 CC Connect。真实首单必须来自授权来源：检查双窗环境/来源、完整统计、两份七子表和主报告、三份资产权限/所有权/L3、原消息普通回复及送达凭证。预检和离线测试不能代替这一步。

## 升级与回退

暂停新接单并等待现有 worker 结束；保存当前源码/配置散列、调度 action、任务记录及最终结果。使用独立版本目录和新清单，不让旧任务进入新 worker 重新计算。回退只恢复明确备份的入口/清单/调度 action；保留新任务和诊断，按原执行版本显式恢复。禁止递归删除共享数据、凭据或原分析器目录。

仓库中的 legacy 宿主仅保留兼容恢复及合成测试接口，未携带任何历史 release、真实配置或任务。新部署 `legacy={}`、`retained_releases=[]`；接入已有历史任务需在原机逐版本核验配置和源码，不能仅复制 JSON 清单。

首次飞书初始化与续期部署必须按 [lark-cli 与阿斯加德接入](lark-asgard-integration.md) 执行；配置占位符不是有效授权。源码内历史硬编码 Secret/资源已移除，不得从旧机拷贝回来。
