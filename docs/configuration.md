# 配置与身份

仓库 `examples/` 中全部为占位值。`tools/prepare_workspace.py` 为新工作区生成 `private/*.example` 和默认禁用的安装清单。复制并填写本机私有配置，配置文件不提交 Git。

| 字段 | 含义 |
| --- | --- |
| `workspace_root` | 已部署工作区绝对路径；具有 AGENTS、deployment 与 .agents 结构 |
| `runtime_state_dir` | 工作区内 `data/walkthrough-test-windows081/runtime`，独立于其他业务 |
| `execution_release` | 固定 `walkthrough-target16-hardening` |
| `execution_policy` | 固定 lifecycle v1、heavy_jobs=1、publication_workers=2、parallel_window_discovery=true |
| `cloud_settings_path` | 与 settings 同目录的 `walkthrough-cloud.settings.json`，仅含获准 `username` |
| `cloud_environment` | Prod/Test 的 ORIGIN、DOWNLOAD_HTTPS_HOSTS、DOWNLOAD_HTTP_HOSTS 六项，按目标批准值配置 |
| `lark_cli` / `lark_home` | 实际 CLI 和独立身份目录；home 不能为个人主目录 |
| `project_id` / profiles | 项目命名空间；User、Bot profile 均以 `project_id-` 开头且不能相同 |
| `expected_lark_app_id` | 固定获准 App |
| `expected_operator_open_id` / `expected_dispatch_open_id` | 固定规则/制品查询 User，与本次请求者身份分开 |
| `auth_credential_replica_path` | 项目 home 内的受控凭据副本路径，使用既有加密和跨进程锁策略 |
| `auth_expected_cli_version` | 实际已验证兼容版本；隔离清单也须一致 |
| `local_analysis` | Windows、native_full、独立 python、完整 analyzer_root、实际 Git usr/bin、完整规则来源、超时和预算 |
| `product_version` | 固定获准 query_chat_id、asgard_bot_id、asgard_app_id 及有限轮询参数 |
| `report_delivery` | native_sheets、include_images=false、fixed_user 原模板读取模式及真实桥接 token/ready 路径 |
| `native_sheet_revision_guard` | 当前启用 true；revision 异常或改变时回退完整读取 |
| `report_secure_label_name` | 当前 L3，按企业密级唯一解析 |

`deployment/cc-connect-lark-isolation.json` 必须与 settings 的项目、App、User、两个 profile、CLI/home/凭据路径逐项一致；`expected_cli_version`、`python_executable` 也须绑定当前解释器和 CLI。样例只列授权维护必需的 scope；实际业务读表、文档、消息、复制、权限和所有权转移权限按组织批准配置核验，不能把样例视为完整授权清单。

固定 User 的 OAuth 初始化由 `deployment/feishu_project_oauth.py` 处理；先看 `--help`，在目标机受控操作。首次授权与已撤销/refresh 失效需要实际用户完成授权，不能靠重复重试恢复。业务 `start` 仅在 User/App 已匹配且凭据明确过期时尝试既有受控续期，随后必须再次通过完整预检。

模板桥接须取得官方唯一复制回执、实际 Bot 身份、私有范围与 L3 保留、Bot view/copy、七子表完整等价证据。`prepare_native_template_bridge.py` **仅生成本地操作计划，不执行云端复制/授权，不生成 ready**；实际准备和独立核验流程见 `references/template-reference-reader.md`。禁止手写 ready 或照搬其他部署的绑定。

配置变更后重新绑定安装清单中的 settings SHA；不得通过更新指纹接管旧任务。旧任务仍需原版本、原配置、原绑定恢复。
