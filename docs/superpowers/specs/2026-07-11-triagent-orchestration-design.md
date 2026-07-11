# TriAgent 多 CLI 编排系统设计

日期：2026-07-11  
状态：待用户书面审阅  
目标平台：Windows 控制主机、Ubuntu 24.04 DGX Spark、Windows 出差电脑、可选腾讯云中继

## 1. 目标

构建一套面向非代码审阅用户的多 Agent 编排范式。用户主要通过 ChatGPT 手机 App 的原生 Codex Remote 下达任务、查看进度和审批结果，不需要阅读代码。系统使用 Codex 作为唯一主控，Cursor 作为首选实施 Agent，Google Antigravity 作为独立审查 Agent，并预留 DeepSeek API 作为 Cursor 额度不足时的实施接力。

系统必须支持：

- Windows 与 Ubuntu 24.04 跨平台运行；
- Windows 工作电脑长期在线并承载 Codex App Remote 入口；
- DGX Spark 承担代码修改、测试、训练、GPU 任务和 Isaac Lab/Isaac Sim 图形任务；
- Windows 出差电脑临时作为控制端或小任务执行端；
- 任务断线后继续运行并可恢复；
- 用户只审批结果、合并、部署及高风险操作；
- 三家订阅额度受控，禁止默认自动超额付费；
- 腾讯云仅作为可选 OpenMemory、状态信标、备份和 SSH 中继，不作为 Agent 执行节点。

## 2. 非目标

第一版不实现：

- 自建 ChatGPT App、网页 App 或公网 MCP；
- 自动部署到实体机器人；
- 多用户权限系统；
- 多 DGX 集群调度；
- 自动购买 Cursor、Codex、Antigravity 或 DeepSeek 额度；
- 将 Isaac 可视化端口直接暴露到公网；
- 将完整代码仓库、密钥或大体积产物存入 OpenMemory。

## 3. 总体架构

正常控制路径：

```text
ChatGPT 手机 App
        │ Codex Remote
        ▼
Windows 工作电脑上的 Codex App
        │ 唯一主控、审批、汇报
        │ SSH + 结构化任务协议
        ▼
DGX Spark / Ubuntu 24.04
        ├─ triagent-runner
        ├─ Cursor CLI
        ├─ Cursor + DeepSeek BYOK（条件可用）
        ├─ OpenCode + DeepSeek（灾备）
        ├─ Antigravity CLI
        ├─ Codex CLI
        ├─ Git worktrees
        └─ 后台任务与图形任务运行环境
```

腾讯云不在正常调用链中。只有跨网络访问 DGX 时，DGX 主动向腾讯云建立受限 SSH 反向隧道，Windows 再通过中继连接。DGX 不开放公网入站端口。

## 4. Agent 角色与信任边界

### 4.1 Codex：唯一主控与验收方

Codex负责：

- 将自然语言需求转为任务规格和验收标准；
- 选择实施 Agent；
- 推进任务状态机；
- 独立重跑测试、构建和仿真；
- 汇总独立审查意见；
- 管理重试、接力和停止条件；
- 向用户输出非技术报告；
- 在审批点暂停。

Codex不得仅根据实施 Agent 的文字声明判定任务完成。

### 4.2 Cursor：首选实施 Agent

Cursor负责：

- 在隔离 worktree 中搜索、修改和调试代码；
- 编写或更新测试；
- 执行快速验证；
- 输出已改文件、已执行命令、已知问题和剩余事项。

Cursor无权修改验收标准、合并主分支或宣布最终交付。

### 4.3 Antigravity：独立审查 Agent

Antigravity只接收原始任务规格、最终 diff、测试/仿真证据和必要源文件。默认不接收 Cursor 的推理和自我评价，避免审查受到实施者叙事影响。

审查结果分为：

- `BLOCKER`：禁止交付；
- `MAJOR`：交付前必须修复；
- `MINOR`：建议修复；
- `NOTE`：非阻断建议。

### 4.4 DeepSeek：备用实施模型

实施路由按以下顺序工作：

1. Cursor 原生订阅模型；
2. Cursor + DeepSeek BYOK；
3. OpenCode + DeepSeek API。

Cursor BYOK 只有在启动能力探测确认以下条件后才可启用：

- DeepSeek API 可调用；
- Cursor CLI 可识别指定模型；
- Cursor CLI 可用该模型完成隔离的文件读写和测试任务；
- 费用确实计入 DeepSeek，而不是 Cursor 订阅额度。

任何条件失败时，使用 OpenCode 适配器。模型名称使用当前配置发现结果；初始候选为 `deepseek-v4-flash` 和 `deepseek-v4-pro`，不依赖即将弃用的旧别名。

## 5. 任务状态机

```text
SPEC → IMPLEMENT → VERIFY → REVIEW → APPROVAL
           ▲                    │
           └──── REPAIR ────────┘
```

附加等待状态：

- `WAITING_FOR_USER`：需求或高风险决策不明确；
- `WAITING_FOR_VISUAL_APPROVAL`：机器人或仿真需要人工视觉确认；
- `WAITING_FOR_GUI`：DGX 图形会话不可用；
- `PAUSED_BUDGET`：供应商额度或单任务预算不足；
- `FAILED_RECOVERABLE`：可重试故障；
- `FAILED_FINAL`：超过重试上限或无法安全继续。

### 5.1 SPEC

任务规格至少包含：

```yaml
goal: 用户可理解的结果
scope: 允许修改的目录与组件
acceptance: 可执行或可观察的验收条件
risk: low | medium | high | robot-safety
visual_check: required | optional | none
forbidden: 禁止行为
budget: 调用、时间和费用上限
```

验收条件不明确时必须询问用户，不得由实施 Agent 自行降低或补写。

### 5.2 IMPLEMENT

每个任务使用独立分支和 Git worktree。实施 Agent只能操作授权范围。完成后生成结构化交接包。

### 5.3 VERIFY

Codex通过普通脚本重新执行项目 `AGENTS.md` 中规定的测试、构建、静态检查和仿真命令。日志收集、状态轮询和退出码判断优先使用确定性脚本，避免用模型轮询浪费额度。

### 5.4 REVIEW

Antigravity执行增量独立审查。普通代码默认单次审查；机器人安全相关改动允许完整复核。

### 5.5 REPAIR

存在 `BLOCKER` 或 `MAJOR` 时退回实施 Agent。普通任务最多自动修复两轮，高风险任务最多三轮。超过上限后停止并报告用户。

### 5.6 APPROVAL

用户收到非技术报告，内容包括结果、验证证据、审查结论、视觉材料、剩余风险、回滚方式和可选操作。未经用户批准不得合并、部署或操作实体机器人。

## 6. 人工审批边界

可自动执行：

- 读取代码和创建 worktree；
- 修改任务分支；
- 运行测试、构建和仿真；
- 修复审查问题；
- 生成草稿 PR；
- 采集日志、截图、视频和指标。

必须由用户批准：

- 合并主分支；
- 部署服务；
- 操作实体机器人；
- 修改机器人安全限制；
- 删除数据、检查点或重要产物；
- 开放端口或安装系统组件；
- 使用真实生产密钥；
- 产生未预先授权的额外费用。

机器人运动、碰撞、安全边界或实机部署相关变化，即使自动测试通过，也必须进入 `WAITING_FOR_VISUAL_APPROVAL`。

## 7. DGX 后台与图形任务

DGX提供两类执行环境：

| 类型 | 运行方式 | 用途 |
|---|---|---|
| 后台任务 | 持久化任务运行器或 `systemd --user` | 编译、测试、训练、审查、Agent调用 |
| 图形任务 | DGX本地桌面会话或局域网流式可视化 | Isaac Lab、Isaac Sim、机器人仿真、人工观察 |

`tmux` 保留为人工调试工具，但不作为正式任务状态来源。正式任务必须具有任务 ID、日志、退出状态、超时、检查点和产物目录。

图形任务需要：

- 启动前检查 GPU、桌面会话和显示环境；
- 持续保存日志和指标；
- 定时保存截图或短视频；
- GUI丢失时进入 `WAITING_FOR_GUI`；
- 支持在可信局域网使用 Isaac WebRTC、Rerun 或 Viser；
- 禁止将无认证的流媒体端口暴露到公网；
- 视觉正确性无法由自动指标判断时，明确要求用户确认。

## 8. 跨设备组件

### 8.1 Windows 工作电脑

- Codex App及原生 Remote入口；
- `triagent-control` 控制命令；
- 面向 Codex 的 TriAgent Skill；
- SSH客户端；
- 只保存任务摘要与连接配置，不保存其他供应商密钥。

### 8.2 DGX Spark

- `triagent-runner` 持久化状态机；
- CLI适配器与能力探测；
- Git worktree管理；
- SQLite任务数据库；
- 后台任务与GUI适配器；
- 日志和产物目录；
- 每家供应商独立登录和凭据隔离。

### 8.3 Windows 出差电脑

- 安装同一控制端和Codex Skill；
- 可在本机运行小任务；
- 可连接DGX查看或接管任务；
- 通过任务租约避免与工作电脑同时写同一任务。

### 8.4 腾讯云

- 继续运行 OpenMemory；
- 可选在线状态信标；
- 可选加密任务元数据和最终报告备份；
- 可选受限SSH反向隧道落点；
- 不运行代码 Agent，不保存完整仓库或供应商密钥。

## 9. 任务持久化与恢复

DGX上每项任务的目录：

```text
runs/<task-id>/
├─ task.yaml
├─ state.json
├─ handoff.yaml
├─ events.jsonl
├─ worktree/
├─ logs/
├─ artifacts/
└─ final-report.md
```

SQLite是状态权威来源，文件用于审计和恢复。高频状态不提交到项目Git仓库。

恢复规则：

- Windows断线：DGX继续当前已授权阶段，但不得越过新审批点；
- DGX重启：后台任务从持久状态恢复，训练从最近检查点恢复；
- Agent超时：保存Git状态和交接包后重试或切换；
- 多控制端：同一任务只有一个可写租约；
- Agent格式错误：允许一次格式修复，仍失败则停止推进；
- GUI会话丢失：暂停视觉验证，不宣布通过。

## 10. Agent接力协议

实施 Agent切换时必须生成：

```yaml
task_spec: 原始任务规格
base_commit: 实施起点
current_commit: 当前提交
changed_files: 已修改文件
completed: 已完成事项
remaining: 未完成事项
tests:
  passed: []
  failed: []
known_issues: []
```

接力 Agent只能继续既定任务，不得修改验收标准。切换后 Codex必须重新执行完整验证。

## 11. 额度和费用控制

预计常态消耗占比：Cursor 45%–60%，Codex 25%–40%，Antigravity 10%–20%。DeepSeek只承担节省或接力任务。

路由阈值：

- Cursor使用量低于70%：正常模式；
- 达到70%：普通任务优先DeepSeek；
- 达到90%或额度错误：新实施任务切换DeepSeek；
- 已开始的复杂步骤尽量完成后再切换，避免重复上下文成本。

预算约束：

- 每任务设置Agent调用数、最长时间和费用上限；
- 达到预算时进入 `PAUSED_BUDGET`；
- 默认关闭所有自动充值和自动超额付费；
- DeepSeek按任务选择Flash或Pro；
- Antigravity默认只读增量diff；
- 日志先机械过滤再交给模型；
- 每月根据各平台Usage面板校正阈值。

## 12. 凭据与安全

- 各供应商凭据仅存在实际调用它的机器上；
- DeepSeek Key使用系统密钥服务或权限为`0600`的独立环境文件；
- 子 Agent不可读取其他供应商凭据；
- 日志过滤API Key、访问令牌和私钥；
- SSH使用密钥登录，禁用密码登录；
- 腾讯云中继采用最小权限、来源限制和命令限制；
- DGX不向公网开放SSH、任务API或Isaac流媒体；
- OpenMemory只保存偏好、决策、任务摘要和待办，不保存密钥、完整代码、原始聊天或敏感机器人数据。

## 13. 第一版交付范围

第一版应交付：

1. 跨平台Python编排核心；
2. Windows `triagent-control` 与Codex Skill；
3. DGX持久化runner和SQLite状态机；
4. Cursor、Antigravity、Codex CLI适配器；
5. Cursor DeepSeek BYOK能力探测；
6. OpenCode DeepSeek灾备适配器；
7. Git worktree隔离和结构化交接；
8. 后台任务与Isaac图形任务分类；
9. 日志、截图、视频和非技术报告；
10. 额度阈值、重试上限和审批门禁；
11. 局域网SSH路径；
12. 腾讯云中继配置文档，但默认不启用中继。

## 14. 验收标准

第一版完成时必须证明：

- 用户可从Windows Codex App提交一项DGX代码任务；
- Windows断开后任务继续，重连后状态可恢复；
- Cursor可完成隔离修改，Codex可独立重跑验证；
- Antigravity可仅基于任务规格和diff输出结构化审查；
- 审查失败可触发有限修复循环；
- Cursor不可用时可生成交接包并切换DeepSeek实施器；
- 预算或额度达到阈值时任务安全暂停或切换；
- Isaac类任务可区分后台与图形模式，并产生可审阅视觉材料；
- GUI丢失和视觉未确认时禁止进入实机部署；
- 手机Remote可查看主控线程并处理审批；
- 无密钥、令牌或敏感代码写入Git、OpenMemory或普通日志；
- 未经用户批准不能合并、部署、删除数据或操作实体机器人。

## 15. 实施顺序建议

实施分为两个里程碑，且不得把模拟验证表述为现场集成通过。

### 里程碑 A：出差期间的 Windows 本地核心

当前出差电脑尚未安装 Codex CLI、Cursor CLI 和 Antigravity CLI。第一阶段先安装并登录三套 CLI，记录版本和非交互能力；任一 CLI 暂时无法安装或登录时，使用模拟适配器继续开发，不阻塞编排核心。

1. 安装 Python、Git、Codex CLI、Cursor CLI、Antigravity CLI，并执行版本与登录检查；
2. 实现本地 runner、SQLite 状态机和任务目录；
3. 用模拟 Agent 和临时 Git 仓库验证完整状态流程；
4. 接入本机可用的真实 CLI，验证实施、复核和独立审查；
5. 实现 DeepSeek API 探测、Cursor BYOK 探测和 OpenCode 灾备接口；
6. 实现 Windows 控制命令、Codex Skill、预算与审批门禁；
7. 生成 Ubuntu/DGX 安装包、配置模板和现场验收清单。

里程碑 A 的完成措辞固定为：“核心功能在 Windows 本地及模拟 DGX 环境中通过；等待局域网设备上的集成验收。”

### 里程碑 B：返回局域网后的现场集成

1. 在 Windows 工作电脑安装控制端和 Codex Skill；
2. 在 DGX 安装 runner、CLI 适配器并完成三家登录；
3. 配置局域网 SSH、任务租约、断线恢复和后台服务；
4. 验证 Isaac 后台、窗口、截图、视频及 WebRTC 模式；
5. 验证 ChatGPT 手机 Remote 到 Windows 主控的审批流程；
6. 按需启用腾讯云 SSH 中继和 OpenMemory 脱敏摘要；
7. 运行一个月额度观测后调整模型和订阅策略。
