# 走查测试

[实习项目总览](https://github.com/Inner-vic/internship-projects) · [使用与验证](docs/project-guide.md)

对同车两个时窗进行完整规则走查，交付主报告和两份原生明细表。

## 解决的问题

PCM 软件更新前后，需要在同一车辆的 Base 与 PCM 更新时窗中，按低速人驾、高速人驾、泊车和 CNOA 模式比较推理时延、帧率及 CPU 占用。角色、规则版本、日志来源和统计口径必须明确，才能解释差异。

项目将双窗输入、完整规则、原生分析器和飞书资产交付组织成可恢复的分析流程。

## 实现了什么

| 能力 | 实现结果 |
| --- | --- |
| 双窗规范化 | 同 VIN 的 Base/PCM 更新角色、完整时间与时区校验 |
| 来源与规则准备 | 每窗独立取数，双窗共享同一份冻结的完整规则快照 |
| 原生分析协作 | 下载检查点、双窗阶段管理，以及 AD、PCM、CPU 分析的受控重叠 |
| 原生报告交付 | 一份主报告、两份七子表明细，写入回读后完成权限和来源治理 |

走查测试自行获取日志并执行完整 AD、PCM 与 CPU 分析，上游查询仅提供可信制品码。报告保留两窗的完整明细；比较指标、缺失值处理及与时延分析的区别见[车辆分析项目对照](https://github.com/Inner-vic/internship-projects/blob/main/docs/vehicle-analysis.md)。

## 实现架构

![走查测试的主要模块、执行顺序和关键边界](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/architecture/walkthrough-test.svg)

## 关键工程设计

### 1. 固定比较口径，再进入并行执行

显式 Base/PCM 更新角色优先，不根据时间先后重排。完整规则每任务读取一次并冻结；两窗可以分别选取可用环境，同窗的各类来源保持一致。查询异常不会被当成“成功但没有数据”。

### 2. 并行度服从资源预算

下载、原生解析、重型任务和发布分别约束并行边界。`window_pipeline` 组织两窗及各分析阶段，让可重叠的工作重叠，同时避免无界扩张下载池或解析进程。

### 3. 把原生报告当作需要核验的资产

明细表按模板绑定、逐格写入并完整回读，主报告链接到实际明细。三份资产各自完成权限、所有权和来源绑定后再交付，缺失和未解析项保留在报告中。

## 实现与验证范围

源码包含业务适配、完整分析器、原生模板契约及独立调度工具。离线回归、Windows 进程行为和真实交付分别验收，依据见[验证文档](docs/validation.md)及[公开验证说明](https://github.com/Inner-vic/walkthrough-test/blob/main/PUBLICATION.md)。

这份公开副本展示“走查测试”的实现。真实日志、业务报告和用户身份数据不在仓库中。

## 源码导读

| 源码 | 关注的实现 |
| --- | --- |
| [workspace/runtime/wtr096-hardening/skill/scripts/native_lifecycle.py](workspace/runtime/wtr096-hardening/skill/scripts/native_lifecycle.py) | 持久任务生命周期 |
| [workspace/runtime/wtr096-hardening/skill/scripts/window_pipeline.py](workspace/runtime/wtr096-hardening/skill/scripts/window_pipeline.py) | 双窗与阶段协作 |
| [workspace/runtime/wtr096-hardening/skill/scripts/native_sheet_delivery.py](workspace/runtime/wtr096-hardening/skill/scripts/native_sheet_delivery.py) | 原生表格写入与回读 |
| [workspace/runtime/wtr096-hardening/skill/scripts/report_policy.py](workspace/runtime/wtr096-hardening/skill/scripts/report_policy.py) | 报告权限及交付规则 |

## 项目资料

使用步骤、依赖和本地验证见[使用与验证](docs/project-guide.md)；源码来源与验证范围见[发布说明](https://github.com/Inner-vic/walkthrough-test/blob/main/PUBLICATION.md)。本页用于介绍实习阶段的实现与设计，具体结论以所链接的代码和证据为准。

[日志分析](https://github.com/Inner-vic/uroad-log-analyzer)与[时延分析](https://github.com/Inner-vic/ad-p99-analysis)使用相关的来源/时间解析思想，但业务执行和数据分别管理。
