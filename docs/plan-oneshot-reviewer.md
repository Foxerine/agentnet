# 实现计划：reviewer 改为「一次性调用」形态（`-p --resume`）

> **状态：待用户批准。** 按项目规矩「设计定稿 ≠ 开始实现」——本文件只描述**怎么落地**，
> 未写任何实现代码。批准后按阶段执行，每阶段可独立验证、可单独回滚。

---

## 一、为什么要改（问题陈述）

reviewer 类实例**拿不到 `Monitor` 工具**，只能用「投递即退出」的 `agentnet poll`，
于是**空闲后收不到信**。今晚为此建的补偿机制（deaf-child 通知 / 终态信欠账 /
保活文案 / 死循环例外）全都是在绕开这个缺失。

### 已被单变量实测**证伪**的候选（不要重复排查）

| 候选 | 判定依据 |
|---|---|
| LLM / 模型家族 | opus 探针同样无 Monitor |
| CCR 网关 | 去掉后 `ToolSearch` 回来了、`Monitor` 没有 |
| `CLAUDE_CODE_CHILD_SESSION` | 设与不设结果相同 |
| 模型档位（`--model opus`） | 探针仍无 |
| `--permission-mode auto` | `-p` 加它仍 56 个工具 |
| `--session-id` / `-n` | 同上 |
| `agentnet run` 包装 | 同上 |
| **`AI_AGENT` 的值** | 🔴 强制覆盖为 `_harness` 后 **Monitor 仍在** ⇒ 它是**结果标签，不是原因** |

### 实测确立的分界

```
claude -p …            → 有 Monitor（56 工具），AI_AGENT 后缀 _agent
wt 拉起的交互式会话     → 无 Monitor，        AI_AGENT 后缀 _harness
```

⇒ **分界是「非交互 vs 交互式启动」**，由 harness 决定、`AI_AGENT` 只是忠实反映它。
⛔ **不改 Claude Code 就改不了这条**。

### ⭐ 于是换问题：不是「给 reviewer 装 Monitor」，而是「让它不需要 Monitor」

`-p` 调用是**同步**的：agentnet 调它 → 拿到 stdout → 那就是评审结果。
**没有异步、没有保活、没有失联、没有"聋掉"。**

---

## 二、可行性：已实测通过的四项

| 能力 | 验证 |
|---|---|
| **多轮上下文** | `--session-id` 存 → `--resume` 取，第二轮准确答出只有第一轮说过的 token |
| **禁用工具** | `--disallowed-tools <tools...>` 存在（reviewer 需禁 `Edit`/`NotebookEdit`） |
| **注入职责边界** | `--append-system-prompt` 存在（承载现有 `scope_note`） |
| **结构化结果** | `--output-format json` 返回 `is_error` / `num_turns` / `stop_reason` / `duration_ms` / `total_cost_usd` / `session_id` |

---

## 三、🔴 前置阻塞项：SessionStart 引导会白烧轮次

**实测**：任务只是「回一个数字 7」，却跑了 **5 轮、$2.13** ——
因为 agentnet 的 SessionStart 引导让它去**挂了一个 `-p` 根本不需要的收信器**。

⚠️ `--settings '{"hooks":{}}'` **关不掉**：钩子注册在用户全局
`~/.claude/settings.json`，`--settings` 是叠加不是覆盖。

⇒ **必须先解决它，否则每次评审调用都带 ~4 轮固定浪费。**

**解法**：`cmd_hook` 的 SessionStart 分支已有先例——
它用 `payload.get('agent_id') or payload.get('agent_type')` 排除子 agent。
在同一位置增加**一次性形态**的识别，命中则**只注册身份、不注入保活引导**。

⚠️ **识别方式待定，须先查证**（属实现阶段第一步）：
`SessionStart` 的 payload 是否带 `source` 字段可区分 `-p`；
若不带，退路是由 `agentnet` 在调用 `-p` 时注入一个自有环境变量
（如 `AGENTNET_ONESHOT=1`）供钩子识别——**那是我们自己的变量，不依赖 harness**。

---

## 四、⭐ 跨平台约束（用户明确要求）

⛔ **不得假设所有 harness 都支持 `-p`。** Codex 等其它 harness 可能没有非交互模式。

⇒ **能力按角色声明，不按 harness 猜**，与 config.toml 既有的 `claude_compatible` 同构：

```toml
[roles.reviewer]
command = "claude"
claude_compatible = true
oneshot = true          # 🆕 支持 -p --resume 形态
```

| `oneshot` | 行为 |
|---|---|
| `true` | 走本计划的一次性调用路径 |
| `false` / 缺省 | 🔴 **回退到现状**：常驻交互实例 + 收信器 + 今晚那套补偿机制 |

⇒ **两条路径长期并存**，不是替换。`[roles.codex]` 保持 `oneshot` 缺省即可。

---

## 五、分阶段落地

### 阶段 0：解除前置阻塞（无此步则后续全部带 4 轮浪费）

- 查证 `SessionStart` payload 能否识别一次性形态；不能则引入 `AGENTNET_ONESHOT`
- 钩子在该形态下**跳过保活引导**，仅保留身份注册与信件投递
- **验收**：`-p` 跑「回一个数字 7」应为 **1 轮**、结果不含 `agentnet` 字样

### 阶段 1：一次性调用原语（不接线，可独立测）

- 新增 `oneshot_call(role, session_id, prompt) -> dict`：
  装配 `-p --resume/--session-id --output-format json --disallowed-tools --append-system-prompt`，
  同步执行并返回解析后的结构
- **四段成败判定**（缺一不可，见 §六）
- **验收**：单测覆盖四种失败路径；真实调用一次读文件任务并断言 `is_error == false`

### 阶段 2：接入 spawn / review 流程

- `spawn --role <oneshot 角色>` 改为**不开窗口**，转为记录一条「会话槽」（session_id + 角色）
- 作者发起评审 = 调用一次；reviewer 的回复 = 该次调用的 `result`
- **每轮都记下返回的 `session_id`**（用于 resume 与崩溃续跑）
- **验收**：跑通一次两轮往返评审，第二轮能引用第一轮的内容

### 阶段 3：成本与失败可观测

- 每次调用把 `total_cost_usd` / `num_turns` / `duration_ms` 累加进该评审线的账
- 超过预算上限即停并报告（⚠️ 实测单次 6 轮 ≈ **$2.48 / 98 秒**，多轮评审会显著更贵）
- **验收**：人为设一个低上限，确认会停

### 阶段 4：收敛既有补偿机制

- oneshot 路径上，deaf-child 通知 / 终态信欠账 / 保活文案**全部不适用** ⇒ 按角色形态跳过
- ⛔ **常驻路径的那套一律保留**（Codex 等仍依赖）

---

## 六、异常处理（🔴 每次调用后四段判，缺一不可）

| # | 检查 | 判定 |
|---|---|---|
| 1 | 退出码 | `124` = 超时被杀；其它非 0 = 进程级失败 |
| 2 | **stdout 首行能否解析为 JSON** | 不能 ⇒ 失败。⚠️ **实测 `--resume <不存在的 session>` 打印错误但退出码是 `0`** |
| 3 | `is_error` | `true` ⇒ 模型侧报错 |
| 4 | `stop_reason` | 非 `end_turn` ⇒ 中断（长度上限 / 拒答 / 工具错） |

⚠️ **第 2 项是必需的**：退出码会骗人——与今晚那条「被杀的 commit `exit 0` 却没产生提交」
**同形**（见 [[feedback_commit_verification_four_steps]]）。

**恢复策略**

- `session_id` 每轮记录 ⇒ 中途挂掉可 `--resume` **接着上一轮继续**，不丢上下文
- 超时（124）⇒ **可安全重试**：`-p` 无"半个提交"式副作用，最坏重跑一遍
- ⚠️ 重试前先核**成本账**（阶段 3），避免失败重试烧穿预算

---

## 七、刻意不做

- ⛔ **不删除常驻 + 收信器路径** —— 跨平台要求，Codex 等仍需要它
- ⛔ **不改 Claude Code 本身**（用户约束）
- ⛔ **不给 reviewer 换后端 / 换模型档位** —— 已实测无效，且会牺牲「换模型比换同模型
  实例强」的独立性设计
- ⛔ **不实现「等待」原语的瞬时谓词那一半** —— 见其单调/瞬时边界，第一版只支持单调谓词

---

## 八、这个改动顺带解决的

| 今晚为 reviewer 建的机制 | oneshot 路径上的状态 |
|---|---|
| deaf-child 通知（收件箱积压提醒拉起方） | **不需要**——无异步 |
| 终态信欠账（`review_debts`） | **不需要**——调用返回即交付 |
| 保活文案 / 死循环例外 / 横幅延迟二义 | **不需要**——不挂收信器 |
| 「等待」原语（用户要求的超时兜底） | 🔴 **仍需要**——它管的是**常驻路径**与跨实例等待，射程更广 |
