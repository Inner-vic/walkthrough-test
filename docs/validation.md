# 测试与验收

## 离线运行

使用已装完整锁定依赖的 Windows CPython 3.11，在仓库根运行：

```powershell
& $HostPython -B -X utf8 tools/check_repository.py
& $HostPython -B -X utf8 tools/run_tests.py
& $HostPython -B -X utf8 tools/offline_selfcheck.py
```

`run_tests.py --suite portable` 仅使用标准库运行输入规范化、导出/路由/授权加载契约和分析器静态契约；GitLab CI 默认运行这部分与源码完整性检查，不申请真实授权、不读取业务配置、不发起真实走查。

完整 suite 包含来源、时间、双窗、下载恢复、环境回退、完整统计、原生工具/进程、模板和报告、身份权限、调度与恢复等合成测试。平台相关 Linux/WSL 测试在 Windows 跳过；跳过不等于通过。完整 Windows 验证需自行准备 runner 或在部署候选执行，默认 Linux CI 不能代替 Windows 原生探针。

本次整理的最终结果保存在 [验证记录](validation-results.json)。测试中历史 VIN 已替换为合成值，不包含真实业务样本。

## 已有证据与边界

当前版本的离线回归及稳定 revision 只读核验已有记录：在同一已交付表格上，完整读取 17 次调用约 21 秒；稳定 revision 核对 1 次调用约 1.3 秒，再次完整重读的内容、样式和布局一致。这个观察只说明重复读取阶段的节省，不是完整任务耗时保证。

此前版本的已完成报告不作为新环境或本次导出代码的线上全链路验收。当前导出验证不下载真实车辆日志、不查询制品、不发布报告，也不修改活动部署。首次部署须以获准真实请求验证双窗日志与完整规则、主报告/两份七子表、所有权/权限/L3 和精确来源送达凭证，完成后才记录线上验收通过。
