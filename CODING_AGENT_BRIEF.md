# Coding Agent 热启动 Brief：STS2-Gym 反编译侦察阶段

> 这份文档让你（coding agent）在一个本地工作区里启动工作。你的任务是**侦察反编译出的 STS2 代码**，为后续构建一个 Gymnasium 风格的 RL/LLM agent 环境收集情报。**这一阶段不写任何代码、不开发 mod**。只读源码、做笔记、回答问题。

---

## 1. 你是谁、用户是谁

- **用户**：一名机器人 PhD 学生，目标是把 Slay the Spire 2 包装成 Gymnasium 风格的强化学习环境，同时作为 LLM agent 评测 benchmark
- **你**：一个能读本地文件、能跨文件 grep、能跟用户对话的 coding agent。你**不要急于写代码**。这一阶段你是侦察员
- **协作风格**：用户在中文交流，技术内容用中英文混合。直接、不啰嗦、不堆砌恭维。指出错误时大方指出

---

## 2. 工作区与背景文件

工作区根目录：`~/code/sts2env/sts2-reverse/`

```
sts2-reverse/
├── README.md                    # 工作区简介
├── decompiled_dll/              # ⭐ ILSpy 反编译产物，3369 个 .cs 文件
│   ├── MegaCrit.Sts2.Core.Combat/
│   ├── MegaCrit.Sts2.Core.Runs/
│   ├── MegaCrit.Sts2.Core.Modding/
│   ├── MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/
│   ├── MegaCrit.Sts2.Core.AutoSlay/
│   └── ... (about 100+ namespaces)
├── raw_pck/                     # GDRE Tools 解出来的资源（可能还没解，可忽略）
├── sts2.dll                     # 原始 DLL（之后写 mod 要 reference）
├── 0Harmony.dll                 # 游戏自带的 Harmony 库
├── sts2.deps.json               # 依赖清单
└── docs/
    ├── VERSION.md               # 反编译时间和版本信息
    └── rng_audit_raw.txt        # 已经预生成的 RNG 调用点清单（44 条）
```

**用户会另外把这两份文档放给你或贴给你：**

- `STS2_GYM_DEV_PLAN.md` — 项目最终目标文档，**这是 north star**。你做的所有事都要服务于让它能落地。读完 §0 项目定位 + §2 Mod 侧组件 + §9 易错点这三节就够了
- 本文档（CODING_AGENT_BRIEF.md）— 当前侦察阶段的具体任务

---

## 3. 法律红线（不可违反）

1. **永远不上传反编译产物到任何公开仓库**。`decompiled_dll/` 和 `raw_pck/` 必须保持 local-only
2. **不在公开渠道（GitHub issue、博客、推特）分享反编译出的源码片段**，包括"我从 sts2.dll 看到代码是这样的..."这种引用
3. 写入的所有笔记文档可以引用类名、方法签名、namespace 结构，**但不复制粘贴方法体的反编译代码**。要描述行为时用自己的话总结
4. 这些限制不是用户的洁癖，是 Mega Crit 允许"个人检视游戏代码"但不允许"重新分发"的边界

---

## 4. 当前已知情报（基于前期人工探查）

用户已经做了顶层扫描，确认了以下事实：

### 4.1 反编译完整度
- 3369 个 `.cs` 文件
- 顶层命名空间约百余个，组织清晰
- 主要业务代码全在 `MegaCrit.Sts2.Core.*`

### 4.2 已识别的几个金矿命名空间

| 命名空间 | 为什么是金矿 |
|---|---|
| `MegaCrit.Sts2.Core.Modding` | 包含 `ModInitializerAttribute` 和 `ModManager` — **Mega Crit 提供了官方 mod 系统**，不需要自己搞 patcher 框架 |
| `MegaCrit.Sts2.Core.DevConsole.ConsoleCommands` | **30+ 个 console 命令**，覆盖加卡、删卡、设 HP、设能量、跳战斗、跳房间、跳 Act、加 buff、抽牌、即时结算、通关、dump 等几乎所有"状态注入"需求 |
| `MegaCrit.Sts2.Core.AutoSlay` | Mega Crit 内部的"AI 自动跑 run"测试框架。**说明游戏支持从代码驱动推进**，对 step 同步语义、headless 模式都有借鉴价值 |
| `MegaCrit.Sts2.Core.Runs/RunRngSet.cs` | **RNG 集中管理类**。说明所有随机性可能由一个对象 dispatch，hook 它就能控制全游戏 RNG |
| `MegaCrit.Sts2.Core.Combat.History` | `CombatHistory` / `CombatHistoryEntry` — 游戏本身记录战斗事件流，可能 free serializer |
| `MegaCrit.Sts2.Core.Multiplayer/CombatStateSynchronizer.cs` | 联机同步用，意味着 CombatState 已经有可序列化、可远程重建的能力 |
| `MegaCrit.Sts2.Core.Commands` 和 `Commands.Builders` | 游戏内动作很可能已经封装成 Command 对象，对 ActionDispatcher 是直接复用基础 |

### 4.3 已知的关键文件路径

```
MegaCrit.Sts2.Core.Modding/ModInitializerAttribute.cs
MegaCrit.Sts2.Core.Modding/ModManager.cs
MegaCrit.Sts2.Core.Runs/RunState.cs
MegaCrit.Sts2.Core.Runs/RunRngSet.cs
MegaCrit.Sts2.Core.Runs/RunManager.cs
MegaCrit.Sts2.Core.Combat/CombatState.cs
MegaCrit.Sts2.Core.Combat/CombatSideExtensions.cs
MegaCrit.Sts2.Core.Combat.History/CombatHistory.cs
MegaCrit.Sts2.Core.Combat.History/CombatHistoryEntry.cs
MegaCrit.Sts2.Core.AutoSlay/AutoSlayer.cs
MegaCrit.Sts2.Core.AutoSlay/AutoSlayConfig.cs
MegaCrit.Sts2.Core.AutoSlay/IRoomHandler.cs
MegaCrit.Sts2.Core.AutoSlay/IScreenHandler.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/AbstractConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/FightConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/DumpConsoleCmd.cs
```

### 4.4 已知的 DevConsole 命令清单

```
AchievementConsoleCmd    ActConsoleCmd            AfflictConsoleCmd
AncientConsoleCmd        ApplyPowerConsoleCmd     ArtConsoleCmd
BlockConsoleCmd          CardConsoleCmd           CloudConsoleCmd
DamageConsoleCmd         DieConsoleCmd            DrawConsoleCmd
DumpConsoleCmd           EnchantConsoleCmd        EnergyConsoleCmd
EventConsoleCmd          FightConsoleCmd          GetLogsConsoleCmd
GodModeConsoleCmd        GoldConsoleCmd           HealConsoleCmd
InstantConsoleCmd        KillConsoleCmd           LeaderboardConsoleCmd
LogConsoleCmd            MultiplayerConsoleCmd    OpenConsoleCmd
PotionConsoleCmd         RelicConsoleCmd          RemoveCardConsoleCmd
RoomConsoleCmd           SentryConsoleCmd         StarsConsoleCmd
TrailerConsoleCmd        TravelConsoleCmd         UnlockConsoleCmd
UpgradeCardConsoleCmd    WinConsoleCmd
```

---

## 5. 你的任务

下面五个调查任务**按顺序**执行。每完成一个，把发现写入 `docs/recon/` 下对应的 markdown 文件。如果某项发现颠覆前期判断，**直接告诉用户**，不要硬撑。

### 任务 A：Mod 系统怎么工作

**目标**：搞清楚怎么写一个能被游戏加载并执行的最小 mod。

**读这些文件**：
```
MegaCrit.Sts2.Core.Modding/   # 整个目录所有文件
```

**回答这些问题**：
1. Mod 的入口形式是什么？是某个 attribute 标记的方法？某个接口的实现？
2. Mod 的物理形态是什么？单 DLL？带 manifest 的目录？走 Steam Workshop？
3. Mod 加载时机？游戏启动早期 / 主菜单后 / 进入 run 时？
4. Mod 之间是否有依赖系统、版本号、加载顺序？
5. 是否有"启用/禁用 mod"的开关、配置目录、log 目录？
6. **关键**：mod 在运行时能拿到哪些核心对象的引用？是 singleton（`Game.Instance.Run` 这种），还是要订阅事件，还是要 Harmony patch？

**产出**：`docs/recon/A_mod_system.md`，包含一个最小 mod 的"hello world"伪代码（不需要可编译，但要说清楚类怎么写、放哪、用什么入口）。

---

### 任务 B：DevConsole 是不是 ScenarioInjector 的现成实现

**目标**：判断 `STS2_GYM_DEV_PLAN.md §2.2 ScenarioInjector` 是不是可以直接基于 DevConsole 命令实现，还是要自己往下挖一层调用游戏内部 API。

**读这些文件**：
```
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/AbstractConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/FightConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/CardConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/RoomConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/ActConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/EnergyConsoleCmd.cs
MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/HealConsoleCmd.cs
# 以及 ConsoleCommands/ 下你认为另外几个有代表性的
```

**回答这些问题**：
1. console 命令如何注册（attribute? 主动 register？反射扫描？）
2. 命令的参数怎么解析（自己写的 parser？标准库？）
3. 命令执行时直接调用哪些核心 API？目标是从这些命令的实现里"反向工程"出 ScenarioInjector 真正要调用的底层方法清单
4. `FightConsoleCmd`：是直接进入战斗，还是只是修改 next-room 标记？能否在任意时刻调用？接受哪些参数（敌人 id？encounter id？卡组覆盖？）
5. **`DumpConsoleCmd`**：它 dump 什么？格式是什么？这一项**特别重要**——可能直接就是 dev plan §2.1 Serializer 的现成实现
6. console 命令是否能从代码直接调用（即 mod 里 `new FightConsoleCmd().Execute("...")`），还是只能通过控制台 UI 触发？

**产出**：`docs/recon/B_devconsole.md`，包含：
- 命令注册与执行机制说明
- 一张表，列出每个对 ScenarioInjector 有用的命令、它的关键参数、它内部调用的底层 API
- 关于 DumpConsoleCmd 的详细分析
- 结论：ScenarioInjector 是基于 DevConsole 包装、基于 DevConsole 内部 API 包装、还是要自己挖底层

---

### 任务 C：AutoSlay 是不是 step 驱动的现成实现

**目标**：理解 AutoSlay 是怎么"驱动游戏自己跑"的，搞清楚它对 dev plan §2.3 ActionDispatcher（step 同步语义）和 §2.4 FastMode 有多少借鉴/复用价值。

**读这些文件**：
```
MegaCrit.Sts2.Core.AutoSlay/   # 整个目录
MegaCrit.Sts2.Core.AutoSlay.Handlers/IHandler.cs   # 如果存在
# 然后挑 AutoSlay.Handlers.Rooms/ 里看起来像战斗 room 的一个 handler 文件读
# 以及 AutoSlay.Handlers.Screens/ 里看起来像 reward screen 的一个 handler 读
```

**回答这些问题**：
1. AutoSlayer 主循环是什么样的？是 `while(!done) { current_handler.Step(); }` 这种同步循环，还是基于 `await` 的异步推进？
2. 它怎么知道一个 handler "完成了"——靠返回值、状态查询、还是事件订阅？
3. 它怎么处理动画/网络等待？AutoSlayTimeoutException 在什么场景下抛？
4. Room handler 和 Screen handler 是怎么决定"在战斗中要打哪张牌"的？是固定策略，还是有可插拔的 policy 接口？**如果有 policy 接口，我们就能把 RL/LLM agent 作为一个 policy 插进去**
5. AutoSlay 跑的时候渲染是否禁用？音频是否禁用？吞吐量大约是真实游戏速度的多少倍？（如果代码里有相关 flag/config，记下来）
6. AutoSlay 跑完一整 run 大约要多少 wall-clock 时间？（从代码里能看到的超时设置或 log 频率推断）

**产出**：`docs/recon/C_autoslay.md`，包含：
- AutoSlayer 控制流图（伪代码）
- 决策点：用 AutoSlay 改造成 step API 现实吗？还是只借鉴它的 handler pattern 自己重写？
- 列出可以直接复用的辅助类（如各种 IRoomHandler / IScreenHandler 的实现）

---

### 任务 D：RNG 控制盘点

**目标**：确认是不是 hook `RunRngSet` 一个类就能 deterministic-reset 整个游戏，还是要逐个 hook。

**读这些文件**：
```
MegaCrit.Sts2.Core.Runs/RunRngSet.cs
# 然后基于 RunRngSet 内部包含的 RNG 字段，进一步看是否还有别的 RNG 源
```

**参考已有的 grep 结果**：`docs/rng_audit_raw.txt`（44 条命中）。

**回答这些问题**：
1. `RunRngSet` 包含几个 RNG 流？每个流的用途（card draw? map gen? event roll?）？
2. 它怎么 seed？是单一 master seed 派生子流，还是各自独立 seed？
3. 它怎么持久化（save/load）？看 serialization 字段
4. `docs/rng_audit_raw.txt` 里的 44 个命中点，有几个是在 `RunRngSet` 体系内（"受控"），有几个游离在外（"野生"）？给出后者的清单——这是 dev plan §2.5 必须额外 hook 的点
5. `Combat` 内是否有独立的 RNG（如 shuffle drawpile）？如果有，它是从 `RunRngSet` 派生还是独立 seed？

**产出**：`docs/recon/D_rng.md`，包含：
- RunRngSet 的字段清单和每个流的用途
- "受控 RNG"vs"野生 RNG"分类清单
- 估算 dev plan §2.5（RngController）的真实工作量

---

### 任务 E：State 序列化盘点

**目标**：判断 dev plan §2.1 Serializer 应该如何实现——是直接 reuse 游戏现有机制，还是要自己写。

**读这些文件**：
```
MegaCrit.Sts2.Core.Runs/RunState.cs
MegaCrit.Sts2.Core.Combat/CombatState.cs
MegaCrit.Sts2.Core.Combat.History/CombatHistory.cs
MegaCrit.Sts2.Core.Multiplayer/CombatStateSynchronizer.cs
# 以及 DumpConsoleCmd（如果在任务 B 里没看完整）
```

**回答这些问题**：
1. `RunState` 是不是包含完整 run 状态的根对象？它的字段大致涵盖哪些方面？
2. `CombatState` 同上，是否完整？
3. 游戏自己有哪几套 serialization 机制？（save game / multiplayer sync / dev console dump / combat history）每个的格式、完整度、是否包含 RNG 状态？
4. 看 `CombatStateSynchronizer` —— 它发送的是什么粒度（整状态 snapshot? 增量 delta?）？格式是什么？
5. **结论**：写 Serializer 的最优策略是哪条路径？
   - (a) 复用 multiplayer 同步用的 state serialization
   - (b) 复用 save/load 用的机制
   - (c) 复用 DumpConsoleCmd 的逻辑
   - (d) 自己直接序列化 RunState / CombatState 的字段
6. 在 PartialObs 模式下，哪些字段是"人类玩家看不到"的（draw pile order、未发生 event 选项等），需要在 obs 层过滤？

**产出**：`docs/recon/E_serialization.md`，包含：
- 各 state 对象的字段清单（按"游戏阶段"分组）
- 三/四种现成 serialization 机制的对比表
- 推荐路径与理由
- PartialObs 过滤清单

---

## 6. 完成所有任务后

写一份 `docs/recon/SUMMARY.md`，2-3 页，回答下面四个问题：

1. **dev plan §2 各组件的真实工作量评估**：基于侦察，每个组件是"几乎免费（直接 reuse）"、"轻量包装"、"中等工作量"、"重写"四档中的哪档？
2. **哪些 dev plan 假设需要修正**：例如，原 dev plan 假设要自己写 RNG hook 清单，但如果 `RunRngSet` 已经搞定一切，这条假设就该改
3. **第一个最小 mod 应该长什么样**：基于已掌握的 mod 系统知识，画一个最小可运行 mod 的骨架（伪代码），它的 entry point 调用一次 DevConsole 命令，验证整个 modding pipeline 通畅
4. **接下来 7 天的具体动作清单**：从"装 mod 开发环境"到"跑通第一个 hello-world mod"的逐步任务，每条 1-3 小时颗粒度

---

## 7. 工作纪律

1. **不要写代码（除了任务 6 的伪代码）**。这一阶段是侦察，不是开发
2. **不要复制粘贴反编译出的代码块到笔记里**。用自己的话总结类的行为
3. **不确定的事情明说"不确定"**，不要为了显得自信而瞎猜
4. **跨文件 grep 优于读单文件**：例如要搞清楚 `RunState` 谁在用，跑 `grep -rln "RunState" decompiled_dll/ --include="*.cs"`，先看引用图再决定深读哪些
5. **每完成一个任务，主动告知用户进度**，并且把当前任务文档的简要结论放在对话里（不要让用户每次都去翻文件）
6. **如果发现某个金矿目录其实是死胡同（比如 AutoSlay 跟我们想的不一样），立刻告诉用户**，不要硬把它套进 dev plan
7. **不要为了让结论更"漂亮"而省略不利发现**。如果 `DumpConsoleCmd` 是个空壳什么都不 dump，老实说

---

## 8. 优先级与时间

- 任务 A、B、D 优先（决定能否开始写 mod）
- 任务 C、E 次之（决定具体怎么写）
- 总耗时预期：4-8 小时纯 reading + writing。如果超过 12 小时还没出 SUMMARY，stop 来问用户

---

## 9. 上手命令

```bash
cd ~/code/sts2env/sts2-reverse

# 建侦察笔记目录
mkdir -p docs/recon

# 先把 RNG 审计清单看一眼（已经预生成）
cat docs/rng_audit_raw.txt

# 用 ripgrep 比 grep 快很多
which rg || brew install ripgrep

# 例如：找所有 RunState 的引用点
rg -l "RunState" decompiled_dll/ --type cs | head -20

# 开始任务 A
ls decompiled_dll/MegaCrit.Sts2.Core.Modding/
```

去吧。开始任务 A。
