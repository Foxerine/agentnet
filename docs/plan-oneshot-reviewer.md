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
| ~~**注入职责边界**~~ | ⛔ **已放弃 `--append-system-prompt`**，见下方「实现期推翻」 |
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

## 三补、🔴 reviewer 必须继续走 CCR（用户明确要求）+ 一个未闭合的验证

> **对抗性审查只有在模型不同时才有意义。** ⇒ oneshot 化只改**调用形态**，
> ⛔ **不碰上游模型**：`[roles.reviewer.env]` 的 CCR 变量一字不动。

### ✅ 已闭合（2026-08-25 20:35 实测）：`-p` **确实经 CCR**，A 方案对 reviewer 成立

此前两次测试都因上游故障超时（先 408、后**额度耗尽导致 401/503**），
测不出是结构性不兼容还是上游在坏。用户换号后复测，**决定性证据**：

```
claude -p --model gpt-5.6-sol --output-format json "Reply with only the digit 7"
→ EXIT=0  73s   is_error:false  num_turns:1  stop_reason:end_turn  result:'7'
   modelUsage: ['gpt-5.6-sol']
   stderr:     [claude-code:unrecognized_model] {"model":"gpt-5.6-sol"}
```

⭐ **判据是「第一方 Anthropic API 没有 `gpt-5.6-sol` 这个模型」**：CLI **自己都不认识**它
（stderr 明写 `unrecognized_model`），却拿回了正确答案 ⇒ **只可能是 CCR 应答的**。

#### ⚠️ 两个曾被我误当证据的东西（记下来免得重犯）

1. **`provider: "firstParty"` / 模型名显示 `claude-fable-5`** —— ⛔ **不是绕过 CCR 的证据**。
   CLI **不知道自己前面有代理**，它按**自己请求的模型名**记账。⇒ 走不走 CCR，这个字段**长得一样**。
2. **「CCR 日志里没有这笔请求」** —— ⛔ 同样无效：该日志**只记 error 级**，
   实测当前文件 **0 字节**，连一次**已知成功**的直连 curl 都没记。
   ⇒ ⭐ **先用已知为真的样本验探针，再拿它判「没有」**（分歧样本验证）。

### 🔴 但它确立了两个硬约束

1. **超时上限从"建议"升为"必需"**：上游故障在 `-p` 下表现为**卡死**而非失败
   （401/503 期实测一路挂到外层超时）。⇒ `ONESHOT_TIMEOUT_DEFAULT` 不可省。
2. **成本比预期高一个量级**：一次**平凡**调用（"回一个数字 7"）
   **73~83 秒 / $0.229**，其中 API 段仅 **4.9 秒** ⇒ **约 68 秒是 CLI 启动开销**，
   且 22830 input tokens 全是系统提示词 + 工具定义（每轮重付）。
   ⇒ ⭐ **阶段 3 的预算门是必需的，不是可选**。

### 🔴 但它已经改变了一条设计假设

计划 §六 原本写「异常同步可见」。实测发现：**上游 408 之后 `claude -p` 既不报错也不退出，
一路挂到我的 280 秒 timeout** ⇒ **上游故障在 `-p` 下表现为「卡死」而非「失败」**。

⇒ ✅ 四段判定第 1 项（退出码 `124`）仍然抓得住它，但：
**每次调用必须设超时上限，这从"建议"升级为"必需"**——没有它就是无上界挂起，
而那正是本方案要消灭的东西。

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

### ✅ 阶段 1：一次性调用原语（已实现，2026-08-25）

落地形态：`judge_oneshot()` + `build_oneshot_argv()` + `oneshot_call()`，
拆成三个函数是为了**前两个不碰 subprocess、可纯单测**。
`ONESHOT_TIMEOUT_DEFAULT = 900.0`，`ONESHOT_GATES` 把门名做成对外契约。

**验收**：单测 **16 个**覆盖四段的每一段 + argv 不变式（全套 223 → **239，OK**）。

#### 🔴 实现期推翻了计划的一处设计：**scope_note 与任务都不走 argv**

计划 §二 原写「用 `--append-system-prompt` 承载 `scope_note`」。**动手时发现它与
代码库既有的一条硬教训冲突**——`Config.role_scope_note` 的 docstring 早就写着：

> **刻意不走 argv**：非 ASCII 文本穿过 Windows 命令行有被码页损坏的风险。

⚠️ 而**评审任务本身就是长篇中文**，问题比 scope_note 更大。

✅ **改成**：
| 承载什么 | 走哪条路 | 为什么 |
|---|---|---|
| `scope_note` | **SessionStart 的 `additionalContext`**（JSON + stdin） | 与常驻形态**完全一致**，零新增机制；一次性形态下钩子照常触发，只是跳过保活引导 |
| **评审任务全文** | 🔴 **收件箱信件**（UTF-8 文件） | 无长度上限、无码页问题；⭐ 且**与常驻形态共用同一条投递通道** |
| argv 位置参数 | 只放**单行纯 ASCII 引导** | 同 `BOOTSTRAP_PROMPT` 的既有教训 |

⇒ ⭐ 这正合本计划 §三 自己总结的那条：**「投递只保留一条通道」**。

#### ⚠️ 顺带验死的一条：`-p` 的 stdin **不是**提示词载体

实测 stderr 原文：`no stdin data received in 3s, **proceeding without it**`
⇒ stdin 是**补充上下文**，⛔ 不能拿它当任务载体。
（另有一次「纯 stdin 无 argv 提示词」的调用跑到 110 秒被杀、零输出，
⚠️ 但那次基线余量只有 30%，**不足以单独定性**——结论以上面那句 stderr 为准。）

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
