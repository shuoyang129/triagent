# TriAgent Windows Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在出差 Windows 电脑上安装并验证三套 CLI，构建可测试的 TriAgent 编排核心，并生成回到局域网后部署 DGX 的安装材料。

**Architecture:** Python 编排核心通过稳定的适配器接口调用真实或模拟 CLI；SQLite 是任务状态权威来源，Git worktree 隔离修改。第一阶段只声明 Windows 本地与模拟 DGX 验证通过，不声明现场 Remote、DGX GPU 或 Isaac 图形链路通过。

**Tech Stack:** Python 3.12、Typer、Pydantic 2、SQLite、pytest、Git、Codex CLI、Cursor CLI、Antigravity CLI、可选 OpenCode 与 DeepSeek API。

## Global Constraints

- 支持 Windows 11 和 Ubuntu 24.04；路径处理必须使用 `pathlib.Path`。
- Codex 是唯一主控；实施 Agent 无权修改验收标准或合并主分支。
- 默认关闭自动充值、自动部署和实体机器人操作。
- 所有真实 Agent 均可被模拟适配器替代。
- 密钥不得进入 Git、SQLite、普通日志或 OpenMemory。
- 普通修复最多两轮，高风险修复最多三轮。
- 里程碑 A 完成措辞固定为：“核心功能在 Windows 本地及模拟 DGX 环境中通过；等待局域网设备上的集成验收。”

## File Map

```text
pyproject.toml                         项目、依赖和命令入口
src/triagent/domain.py                 任务、状态、风险和审查类型
src/triagent/store.py                  SQLite状态与事件存储
src/triagent/run_layout.py             runs/<id>目录布局
src/triagent/adapters/base.py          Agent适配器协议
src/triagent/adapters/fake.py          确定性模拟Agent
src/triagent/adapters/process.py       安全子进程执行器
src/triagent/adapters/codex.py         Codex CLI适配器
src/triagent/adapters/cursor.py        Cursor CLI适配器
src/triagent/adapters/antigravity.py   Antigravity CLI适配器
src/triagent/adapters/deepseek.py      DeepSeek/OpenCode能力探测
src/triagent/router.py                 额度感知实施路由
src/triagent/orchestrator.py           状态机推进与审批门禁
src/triagent/git_workspace.py          分支/worktree生命周期
src/triagent/report.py                 非技术最终报告
src/triagent/cli.py                    triagent命令行
skills/triagent/SKILL.md               Windows Codex Skill
profiles/windows.example.toml          Windows示例配置
profiles/dgx.example.toml              DGX示例配置
scripts/bootstrap-windows.ps1          Windows安装与诊断脚本
scripts/bootstrap-dgx.sh               后续DGX安装脚本
docs/operations/windows-bootstrap.md   Windows安装登录手册
docs/operations/dgx-onsite-checklist.md 现场集成清单
tests/                                 对应单元与集成测试
```

---

### Task 1: Windows 工具链安装与能力清单

**Files:**
- Create: `scripts/bootstrap-windows.ps1`
- Create: `docs/operations/windows-bootstrap.md`
- Create: `profiles/windows.example.toml`
- Test: `tests/test_bootstrap_contract.py`

**Interfaces:**
- Produces: `work/capabilities/windows.json`，字段为 `python`、`git`、`codex`、`cursor`、`antigravity`、`opencode`，每项包含 `installed`、`version`、`authenticated`、`headless`。

- [ ] **Step 1: 写失败的能力文件契约测试**

```python
def test_capability_record_has_required_fields():
    required = {"installed", "version", "authenticated", "headless"}
    sample = {"installed": False, "version": None, "authenticated": False, "headless": False}
    assert set(sample) == required
```

- [ ] **Step 2: 运行测试并确认初始失败**

Run: `python -m pytest tests/test_bootstrap_contract.py -v`  
Expected: FAIL，因为项目与测试文件尚未就绪。

- [ ] **Step 3: 编写 PowerShell 安装/诊断脚本**

脚本必须依次检查 `python --version`、`git --version`、`codex --version`、`cursor-agent --version`、`antigravity --version`、`opencode --version`；缺少工具时只打印对应官方安装命令并退出码返回 `2`，不得静默安装或修改系统 PATH。认证检查使用各 CLI 的只读状态命令；不把输出中的令牌写入文件。

- [ ] **Step 4: 按官方渠道人工安装并登录**

安装顺序与命令如下。每一步安装后重开终端并记录版本。登录动作由用户在交互终端完成，计划执行器不得收集密码、Cookie或令牌。

```powershell
# Python 3.12 与 Git（winget可用时）
winget install --id Python.Python.3.12 --exact
winget install --id Git.Git --exact

# Codex CLI；先安装Node.js LTS，再安装Codex
winget install --id OpenJS.NodeJS.LTS --exact
npm install -g @openai/codex
codex --version
codex

# Cursor CLI官方Windows路径是WSL。启用WSL需要管理员批准并可能重启。
wsl --install -d Ubuntu-24.04
wsl bash -lc "curl https://cursor.com/install -fsS | bash"
wsl bash -lc "~/.local/bin/cursor-agent --version"
wsl bash -lc "~/.local/bin/cursor-agent"

# Antigravity CLI原生支持Windows
irm https://antigravity.google/cli/install.ps1 | iex
agy --version
agy
```

Expected: `codex --version`、WSL内`cursor-agent --version`和`agy --version`均返回版本；首次运行分别打开或给出官方登录流程。若企业策略禁止`irm | iex`，改用浏览器从官方页面下载脚本、人工检查后执行；不得从第三方镜像安装。

- [ ] **Step 5: 生成并验证能力清单**

Run: `powershell -ExecutionPolicy Bypass -File scripts/bootstrap-windows.ps1 -Output work/capabilities/windows.json`  
Expected: JSON存在；已安装工具包含非空版本；未安装工具被明确标为`installed=false`。

- [ ] **Step 6: 提交**

```bash
git add scripts/bootstrap-windows.ps1 docs/operations/windows-bootstrap.md profiles/windows.example.toml tests/test_bootstrap_contract.py
git commit -m "chore: add Windows CLI bootstrap diagnostics"
```

### Task 2: Python项目与任务领域模型

**Files:**
- Create: `pyproject.toml`
- Create: `src/triagent/__init__.py`
- Create: `src/triagent/domain.py`
- Test: `tests/test_domain.py`

**Interfaces:**
- Produces: `TaskSpec`、`TaskState`、`RiskLevel`、`ReviewSeverity`、`Budget`。

- [ ] **Step 1: 写领域约束测试**

```python
def test_robot_safety_requires_visual_approval():
    spec = TaskSpec(goal="walk", scope=["robot/"], acceptance=["sim passes"], risk=RiskLevel.ROBOT_SAFETY)
    assert spec.visual_check == "required"
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_domain.py -v`  
Expected: FAIL with import error for `triagent.domain`.

- [ ] **Step 3: 实现最小领域类型**

使用Pydantic模型；`TaskSpec`拒绝空的`goal`、`scope`和`acceptance`；风险为`robot-safety`时强制`visual_check="required"`；`Budget`包含`max_agent_calls`、`max_minutes`、`max_usd`且值不得为负。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_domain.py -v`  
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml src/triagent tests/test_domain.py
git commit -m "feat: define TriAgent task domain"
```

### Task 3: 任务目录与SQLite持久化

**Files:**
- Create: `src/triagent/run_layout.py`
- Create: `src/triagent/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `RunLayout.create(root, task_id) -> RunLayout`、`TaskStore.create_task(spec)`、`TaskStore.transition(task_id, expected, target, event)`、`TaskStore.load(task_id)`。

- [ ] **Step 1: 写原子状态转换失败测试**

```python
def test_transition_rejects_stale_expected_state(store, task):
    store.transition(task.id, TaskState.SPEC, TaskState.IMPLEMENT, "start")
    with pytest.raises(StateConflict):
        store.transition(task.id, TaskState.SPEC, TaskState.VERIFY, "stale")
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_store.py -v`  
Expected: FAIL because `TaskStore` is undefined.

- [ ] **Step 3: 实现目录和事务存储**

创建`task.yaml`、`state.json`、`events.jsonl`、`logs/`、`artifacts/`和`worktree/`。SQLite转换使用`BEGIN IMMEDIATE`并比较预期状态；成功后再原子替换`state.json`。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_store.py -v`  
Expected: PASS，包括进程重开后的恢复测试。

- [ ] **Step 5: 提交**

```bash
git add src/triagent/run_layout.py src/triagent/store.py tests/test_store.py
git commit -m "feat: persist task state and audit events"
```

### Task 4: Agent协议、模拟适配器与安全进程执行器

**Files:**
- Create: `src/triagent/adapters/base.py`
- Create: `src/triagent/adapters/fake.py`
- Create: `src/triagent/adapters/process.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Produces: `AgentAdapter.capabilities()`、`AgentAdapter.run(request) -> AgentResult`、`ProcessRunner.run(argv, cwd, timeout, env_allowlist)`。

- [ ] **Step 1: 写超时与密钥过滤测试**

```python
def test_process_runner_redacts_secret_and_times_out(tmp_path):
    runner = ProcessRunner(redactions=["super-secret"])
    result = runner.run([sys.executable, "-c", "print('super-secret')"], tmp_path, 5, {})
    assert "super-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_adapters.py -v`  
Expected: FAIL because adapter types do not exist.

- [ ] **Step 3: 实现协议与模拟器**

`AgentRequest`包含角色、任务文件、工作目录、输出schema和超时；`FakeAgent`按测试夹具返回成功、审查失败、超时或格式错误。`ProcessRunner`只接受argv数组，禁止shell字符串，环境变量采用白名单。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_adapters.py -v`  
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/triagent/adapters tests/test_adapters.py
git commit -m "feat: add safe agent adapter contract"
```

### Task 5: 编排状态机与审批门禁

**Files:**
- Create: `src/triagent/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `TaskStore`、`AgentAdapter`。
- Produces: `Orchestrator.advance(task_id)`、`Orchestrator.approve(task_id, action)`。

- [ ] **Step 1: 写修复上限和机器人审批测试**

```python
def test_robot_task_cannot_reach_approval_without_visual_confirmation(orchestrator, robot_task):
    orchestrator.run_until_blocked(robot_task.id)
    assert orchestrator.state(robot_task.id) == TaskState.WAITING_FOR_VISUAL_APPROVAL
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_orchestrator.py -v`  
Expected: FAIL because `Orchestrator` is undefined.

- [ ] **Step 3: 实现最小状态机**

只允许设计文档中列出的转换；`BLOCKER`/`MAJOR`进入`REPAIR`；普通任务两轮、高风险三轮后进入`FAILED_FINAL`；预算耗尽进入`PAUSED_BUDGET`；合并、部署、删除和实机操作必须提供用户审批记录。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_orchestrator.py -v`  
Expected: PASS for happy path, review loop, budget pause, visual gate and restart recovery.

- [ ] **Step 5: 提交**

```bash
git add src/triagent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: enforce TriAgent workflow gates"
```

### Task 6: Git worktree隔离与结构化交接

**Files:**
- Create: `src/triagent/git_workspace.py`
- Test: `tests/test_git_workspace.py`

**Interfaces:**
- Produces: `GitWorkspace.create(repo, task_id)`、`GitWorkspace.handoff() -> Handoff`、`GitWorkspace.diff()`。

- [ ] **Step 1: 写临时仓库隔离测试**

```python
def test_task_workspace_does_not_modify_main_worktree(temp_repo):
    ws = GitWorkspace.create(temp_repo, "task-1")
    (ws.path / "new.txt").write_text("x", encoding="utf-8")
    assert not (temp_repo / "new.txt").exists()
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_git_workspace.py -v`  
Expected: FAIL because `GitWorkspace` is undefined.

- [ ] **Step 3: 实现worktree和handoff**

分支名为`triagent/<task-id>`；handoff包含`base_commit`、`current_commit`、`changed_files`、`completed`、`remaining`、测试结果和已知问题。删除worktree属于显式清理动作，不在失败路径自动执行。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_git_workspace.py -v`  
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/triagent/git_workspace.py tests/test_git_workspace.py
git commit -m "feat: isolate tasks with Git worktrees"
```

### Task 7: 真实CLI适配器和能力探测

**Files:**
- Create: `src/triagent/adapters/codex.py`
- Create: `src/triagent/adapters/cursor.py`
- Create: `src/triagent/adapters/antigravity.py`
- Create: `src/triagent/adapters/deepseek.py`
- Test: `tests/test_cli_capabilities.py`

**Interfaces:**
- Produces: 四个适配器的`capabilities()`与`run()`；未安装或未认证时返回结构化`UNAVAILABLE`，不抛出未处理异常。

- [ ] **Step 1: 写缺失CLI回退测试**

```python
def test_missing_cli_is_unavailable_not_fatal(tmp_path):
    adapter = CursorAdapter(executable="definitely-missing")
    assert adapter.capabilities().available is False
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_cli_capabilities.py -v`  
Expected: FAIL because adapters are undefined.

- [ ] **Step 3: 实现能力探测与输出解析**

每个适配器先调用版本和只读认证状态；真实执行只使用非交互参数和结构化输出。Cursor DeepSeek BYOK探测必须区分“模型可列出”“Agent工具测试通过”“费用归属已人工确认”三项，任一未通过均不可自动启用。

- [ ] **Step 4: 用模拟可执行文件运行测试**

Run: `python -m pytest tests/test_cli_capabilities.py -v`  
Expected: PASS，无需真实供应商登录。

- [ ] **Step 5: 在已安装CLI上运行标记测试**

Run: `python -m pytest -m live_cli tests/test_cli_capabilities.py -v`  
Expected: 已安装且登录的CLI PASS；其余明确SKIP，不得FAIL整个本地核心。

- [ ] **Step 6: 提交**

```bash
git add src/triagent/adapters tests/test_cli_capabilities.py
git commit -m "feat: detect and invoke coding CLIs"
```

### Task 8: 额度感知路由、报告和控制CLI

**Files:**
- Create: `src/triagent/router.py`
- Create: `src/triagent/report.py`
- Create: `src/triagent/cli.py`
- Test: `tests/test_router.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `ImplementationRouter.choose(usage, capabilities, risk)`；命令`triagent create|run|status|approve|report|doctor`。

- [ ] **Step 1: 写70%/90%路由测试**

```python
@pytest.mark.parametrize(("usage", "expected"), [(0.69, "cursor"), (0.70, "deepseek"), (0.90, "deepseek")])
def test_cursor_budget_thresholds(router, usage, expected):
    assert router.choose(cursor_usage=usage, risk="low").name == expected
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_router.py tests/test_cli.py -v`  
Expected: FAIL because router and CLI are undefined.

- [ ] **Step 3: 实现路由、报告和命令**

正常、节省、接力模式分别对应低于70%、70%至90%、达到90%或额度错误。最终报告固定输出状态、用户结果、测试、独立审查、视觉材料、剩余风险、回滚和待批准操作；不得输出内部推理。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_router.py tests/test_cli.py -v`  
Expected: PASS.

- [ ] **Step 5: 运行本地模拟端到端任务**

Run: `triagent run --profile fake tests/fixtures/sample-repo "add a health endpoint"`  
Expected: 状态到`APPROVAL`，产生`final-report.md`，主工作区未改变。

- [ ] **Step 6: 提交**

```bash
git add src/triagent/router.py src/triagent/report.py src/triagent/cli.py tests/test_router.py tests/test_cli.py
git commit -m "feat: add budget routing and operator CLI"
```

### Task 9: Codex Skill、DGX部署材料与里程碑验证

**Files:**
- Create: `skills/triagent/SKILL.md`
- Create: `profiles/dgx.example.toml`
- Create: `scripts/bootstrap-dgx.sh`
- Create: `docs/operations/dgx-onsite-checklist.md`
- Test: `tests/test_packaging.py`

**Interfaces:**
- Produces: 可复制到工作Windows和DGX的安装材料；Skill只调用`triagent`命令，不直接拼接供应商命令。

- [ ] **Step 1: 写包内容测试**

```python
def test_distribution_contains_dgx_and_skill_files():
    for path in ["skills/triagent/SKILL.md", "profiles/dgx.example.toml", "scripts/bootstrap-dgx.sh"]:
        assert Path(path).is_file()
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_packaging.py -v`  
Expected: FAIL because distribution files are missing.

- [ ] **Step 3: 编写Skill和现场材料**

Skill说明结果审批模式、禁止操作、`triagent doctor`、创建任务、查看状态、批准和报告命令。DGX脚本默认只诊断依赖；安装系统组件必须使用显式`--install`并再次确认。现场清单包含SSH、三CLI登录、systemd user、GPU、桌面、Isaac窗口/WebRTC、断线恢复和手机Remote验证。

- [ ] **Step 4: 完整本地验证**

Run: `python -m pytest -v`  
Expected: 全部非现场测试PASS，`live_cli`和`onsite`测试仅在缺少设备时SKIP。

- [ ] **Step 5: 生成能力与里程碑报告**

Run: `triagent doctor --profile profiles/windows.example.toml`  
Expected: 列出真实CLI可用性，不泄露凭据，并输出固定声明：“核心功能在 Windows 本地及模拟 DGX 环境中通过；等待局域网设备上的集成验收。”

- [ ] **Step 6: 提交**

```bash
git add skills profiles scripts docs/operations tests/test_packaging.py
git commit -m "docs: package Windows bootstrap and DGX onsite setup"
```

## Phase B Follow-up Plan

返回局域网后，另写 `triagent-dgx-onsite-integration-plan.md`，只包含真实Windows工作主机、DGX、Isaac、ChatGPT Remote和腾讯云灾备集成。该计划必须从`triagent doctor`的现场能力清单开始，不能复用模拟结果作为通过证据。
