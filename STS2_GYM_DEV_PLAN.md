# STS2-Gym 开发计划

> 给 coding agent 的工程实施文档。包含：要建什么、易错点、关键设计决策。

---

## 0. 项目定位（这一节决定后面所有设计）

把 Slay the Spire 2 包装成 Gymnasium 风格的强化学习环境，支持自由指定角色、难度（Ascension）、关卡、卡组、敌人、起始状态。

**核心定位：双一等公民环境（Dual First-Class Citizens）**

本环境从设计之初就同时服务两类使用者，且二者地位平等：

1. **传统 RL agent**：消费 tensor observation、输出 discrete action id、按 mask 训练
2. **LLM agent**：消费人类可读 observation（文本 prose **或** 结构化 JSON，二者并存）、输出文本或 tool-call 形式的 action、按 prompt 推理

**这不是"主做 RL，顺便支持 LLM"，而是两套接口并列、共享同一套底层状态与奖励**。STS2 的卡牌名、效果描述、敌人意图本身就是自然语言，让这套环境成为 LLM agent 评测基础设施的天然契合度远高于 Atari / MuJoCo / Procgen。这是本项目最大的差异化优势，所有设计决策必须为此让路。

具体含义：

- 每一个状态字段，都必须能派生出**三种视图**：tensor（给 RL）、人类可读文本（给 LLM prose 模式）、人类可读 JSON（给 LLM tool-use 模式）
- 每一个动作，都必须既有 discrete id，也有 canonical 文本形式（如 `"play Strike on Cultist"`），还能用结构化 dict 表达
- 每一个 task，都必须提供 RL 评估协议 **和** LLM 评估协议
- 文档、baseline、example 必须 RL 和 LLM 各一套

---

## 1. 系统架构

```
┌─────────────────────────────────────────┐         ┌──────────────────────────┐
│  Python: gymnasium.Env                  │  HTTP/  │  C# Mod inside STS2      │
│  ┌─────────────────┐ ┌────────────────┐ │   IPC   │  ─ /reset                │
│  │ RL Interface    │ │ LLM Interface  │ │ ──────► │  ─ /step                 │
│  │  obs: Tensor    │ │  obs: Text     │ │ ◄────── │  ─ /observe              │
│  │  action: int    │ │       + JSON   │ │  JSON   │  ─ /serialize            │
│  │  action_mask    │ │  action: str   │ │         │  ─ /deserialize          │
│  │                 │ │       / dict   │ │         │                          │
│  │                 │ │  tool_schema   │ │         │                          │
│  └─────────────────┘ └────────────────┘ │         └──────────────────────────┘
│           └──────────┬──────────┘       │
│                      ▼                  │
│           Unified State Representation  │         Godot 4 + sts2.dll
│           (single source of truth)      │         (Harmony patches)
└─────────────────────────────────────────┘
```

**关键原则**：底层状态表示是唯一真相，所有视图（tensor / 人类可读文本 / 人类可读 JSON）都是从同一份状态派生。**禁止**任一接口维护独立状态。

---

## 2. Mod 侧组件（C#，游戏进程内）

### 2.1 状态序列化器 `Serializer`

将运行时游戏对象序列化为版本化的结构化 JSON。**双路径架构**：

**(a) Between-rooms state**（map / event / shop / rest / treasure / reward / upgrade / transform / enchant / game_over 等所有非战斗 phase）：

直接复用游戏现成的 `RunManager.Instance.ToSave(preFinishedRoom)` → `SerializableRun`（18+ top-level 字段，含 `SchemaVersion`、完整 `SerializableRunRngSet` RNG state、`Anonymized()` 隐私 wrapper、JSON + Binary 双格式）。**几乎免费**，无需自己写。

**(b) Mid-combat state**（combat phase 独有，游戏自身**没有官方序列化机制**——save 只在 between-rooms 取，multiplayer sync 也只在 combat 边界做，依赖 deterministic replay）：

自己写 `SerializableCombatState` + 3 个子 dataclass，字段映射 4 个公开类的 attribute：

| 子 dataclass | 来源类 | 关键字段 |
|---|---|---|
| `SerializableCombatState` | `CombatState` | `RoundNumber`、`CurrentSide`、`Encounter.Id`、`Modifiers`、`EscapedCreatures` |
| `SerializableCreature` | `Creature` | `CombatId`、`MonsterId`（or PlayerNetId）、`CurrentHp`、`MaxHp`、`Block`、`Powers`、`Side`、`SlotName`、（怪物专属）`NextMove` intent |
| `SerializablePlayerCombatState` | `PlayerCombatState` | **5 个 pile**（`Hand` / `DrawPile` / `DiscardPile` / `ExhaustPile` / **`PlayPile`**）的卡牌列表、`Energy`、`Stars`（Necrobinder 专属）、`Pets` 列表 |
| `SerializablePower` | `PowerModel` | `Id`、`Amount`（stack count）、`IsHidden` flag（PartialObs 用）|

**注意 5 个 pile 不是 4 个**：`PlayPile` 是卡牌"正在结算中"的临时 pile，序列化时如果漏掉、在动画/连锁触发期间 snapshot 会有信息缺失。

**RNG state**：通过 (a) 路径自带（`SerializableRunRngSet.Counters`）。**禁止**在 (b) 路径里再单独存 RNG —— 单一真相原则。

**字段描述文本不由 schema metadata 提供**——卡牌效果、power 描述、relic 描述、event 选项等用户可见文本**全部走游戏现成 `MegaCrit.Sts2.Core.Localization.LocManager`**（详见 §2.8）。Serializer 只输出**结构化数据 + ModelId 引用**，由 §2.8 HumanRenderer 在派生 view 时查 LocManager 拼出人类可读字符串。这条让 Serializer 单一职责：**只管"是什么"，不管"读起来怎么样"**。

### 2.2 状态注入器 `ScenarioInjector`

接受 `ScenarioSpec` 跳过正常 Run 启动流程，直接构造目标状态。注入颗粒度三档：

| 颗粒度 | 用途 | 优先级 |
|---|---|---|
| Combat-level | 直接进入指定 (character, ascension, deck, hand, energy, hp, enemies, relics) 战斗 | P0 |
| Floor-level | 从指定 (character, ascension, floor, map_position) 开始 | P1 |
| Run-level | 从角色选择后第一步开始（character + ascension 必填） | P2 |

`character` 和 `ascension` 是三档颗粒度共有的必填项——它们决定 RunState 构造时的角色配置与难度修饰，无法在 Combat-level injection 中省略（即使只想测战斗，仍需要一个携带正确角色 / ascension 的 scaffolding RunState）。

先做 Combat-level，因为它最小、最常用、最容易写正确。

### 2.3 动作执行器 `ActionDispatcher`

把外部传来的结构化动作映射到游戏内方法调用。**底层 API 几乎全部现成**——`MegaCrit.Sts2.Core.Commands/*Cmd` 19 个静态类（`CardCmd` / `CardPileCmd` / `CreatureCmd` / `PlayerCmd` / `RelicCmd` / `PowerCmd` / `PotionCmd` 等）提供了所有动作原语，async/await 链就是天然的同步语义。

**两类动作 + 两条注入路径**：

#### (i) 战斗内动作（直接调 `*Cmd` 静态方法）

| Action | 底层 API |
|---|---|
| Play card | `await CardCmd.AutoPlay(new BlockingPlayerChoiceContext(), card, target)` |
| End turn | `PlayerCmd.EndTurn(player, canBackOut: false)` |
| Use potion | `potion.EnqueueManualUse(target)` + `await Task.Delay(300)` |
| Action mask 计算 | `card.CanPlay(out UnplayableReason _, out AbstractModel _)` |
| 合法目标列表 | `card.CombatState.HittableEnemies`（依 `card.TargetType` 过滤） |

#### (ii) 非战斗 phase 决策（通过 `ICardSelector` 注入栈）

`MegaCrit.Sts2.Core.TestSupport.ICardSelector` 是 Mega Crit 官方留给 testing / automation 的 policy 注入接口，AutoSlay 已在使用：

```csharp
// 静态栈，IDisposable scope 管理
CardSelectCmd.UseSelector(selector);   // 独占，要求栈空
CardSelectCmd.PushSelector(selector);  // 叠加
```

`ICardSelector` 实现两个方法：
```csharp
Task<IEnumerable<CardModel>> GetSelectedCards(IEnumerable<CardModel> options, int minSelect, int maxSelect);
CardModel? GetSelectedCardReward(IReadOnlyList<CardCreationResult> options, IReadOnlyList<CardRewardAlternative> alts);
```

**覆盖范围（官方）**：card reward 选取、deck 内卡牌选择（upgrade / transform / enchant / select-to-discard / select-to-exhaust）等所有 card-select 弹窗。

**Mod 端要扩展的 hook 点**（官方没覆盖、我们要自己引入同款 stack-IDisposable 模式）：
- `IMapNodeSelector`：MapScreen 上"下一节点选哪条路"
- `IEventOptionSelector`：Event room 选项
- `IShopActionSelector`：Shop 购买 / 撤销 / 离开
- `IRestSiteSelector`：Rest 选 rest 还是 smith
- `IRewardScreenSelector`：reward screen 上多个 reward 选哪个（card / gold / potion / relic）的顺序

实现方式：在 mod 启动时 Harmony postfix 到游戏内对应"等用户点 UI"的 await 点，提供我们的 selector stack 注入。**模式严格对齐 `CardSelectCmd.UseSelector(...)`**，让外部接口风格统一。

**关键不变量：同步语义**。一次 step 调用必须等到游戏状态稳定（动画、连锁触发、敌人回合、buff 结算全部完成）才返回。**用 `CombatManager.Instance` 上的事件 (`TurnEnded` / `PlayerActionsDisabledChanged` / `AboutToSwitchToEnemyTurn`) 配合 `TaskCompletionSource` 做事件驱动同步，不要用 polling**（AutoSlay 用 100ms polling 是其慢的根因——任务 C 已确认）。不允许 Python 端拿到中间态。在 phase 转换点插入断言。

### 2.4 速度模式 `FastMode`

**🟢 游戏自带 FastMode 系统，dev plan 假设的"Harmony patch 短路所有纯视觉 await 调用"工作量大部分已被官方代替**。

游戏侧实现：`MegaCrit.Sts2.Core.Settings.FastModeType { None, Normal, Fast, Instant }` 4 档枚举，**62 处代码点**在 tween 时长 / `Task.Delay` 等待 / 动画起点据此分支。`InstantConsoleCmd` 提供一行切换：

```csharp
SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant;
```

**STS2-Gym 的工作分两步**：

1. **P0：拨开关 + 验证 Instant 安全**
   - mod init 时设 `FastMode = Instant`
   - **关键验证 (P1 milestone)**：跑同 seed 同 policy 在 `Normal` / `Fast` / `Instant` 三档下的 trajectory，断言**逻辑事件序列 bit-exact 一致**（用 `CombatHistory.Entries` 比对——见 §2.1）
   - **已知风险**：AutoSlayer.PlayRunAsync 用 `FastMode = Fast` 而非 `Instant`，可能 Instant 在某些动画-逻辑耦合点有 corner case。如果 P1 验证发现 Instant 不 bit-exact，**降回 Fast**或针对性 Harmony patch 那些泄漏点（**不要全盘自己写 FastMode**，那是已经做过的事）

2. **P1：Harmony 补刀（仅在 Instant 仍不够快时）**
   - 找出 Instant 下仍存在的 `Task.Delay` / tween 等候点（用 profiler 定位）
   - 针对性 Harmony prefix 让那些 await 立即 return
   - **保留**所有逻辑相关 await（如 buff trigger 顺序、回合切换 lifecycle 事件）—— 短路这些会改 trajectory

**验证方法**：上述 §2.1 提到的 `CombatHistory.Entries` event log 在三档 FastMode 下应字段 bit-exact 一致；step/s 应**至少**从 `Normal` 的 ~5 step/s 提升到 `Instant` 的 ≥ 50 step/s（dev plan §11 P1 milestone 目标）。

### 2.5 RNG 控制器 `RngController`

**🟢 游戏自身已经把 RNG 集中管理到位，dev plan 假设的"几十个 hook 点逐个 patch"工作量被大幅压缩**。

侦察验证：`MegaCrit.Sts2.Core.Runs.RunRngSet` 集中持有 **12 个 RNG 流**（`UpFront` / `Shuffle` / `UnknownMapPoint` / `CombatCardGeneration` / `CombatPotionGeneration` / `CombatCardSelection` / `CombatEnergyCosts` / `CombatTargets` / `MonsterAi` / `Niche` / `CombatOrbs` / `TreasureRoomRelics`），每个玩家额外持有 `PlayerRngSet` 提供 **3 个 player-scope 流**（`Rewards` / `Shops` / `Transformations`）。全部从单一字符串 master seed 用 `hashCode(seed) + hashCode(snake_case_type_name)` 确定性派生。

跨文件 grep `Random` / `Randf` / `Randi` / `Shuffle` 44 处命中点中：
- **40 处**直接走 `runState.Rng.*` / `player.PlayerRng.*`（受控）
- **11 处**通过 `new Rng(runState.Rng.Seed, ...)` derived seed（仍受控，master 一致即一致）
- **3-4 处** `Rng.Chaotic` / `GD.Randf` / `new System.Random()` —— 全部在 `Nodes.Vfx*` / UI 节点 / map 装饰 —— **0 处影响游戏逻辑**
- **1 处 wild outlier**：`EncounterModel.DebugRandomizeRng()` 用 `DateTime.UtcNow` 做 seed，**唯一 caller 是 `FightConsoleCmd`** —— 不通过 fight console 命令进战斗即完全避开

**真正要做的工作**（极轻量）：

1. **接管 master seed 入口**：mod 在 `RunManager.SetUpNewSinglePlayer(runState, ...)` 调用前注入由外部传入的 seed，`RunState.CreateForNewRun(..., ascensionLevel, seed)` 第 6 参数收下即可
2. **避开 / 屏蔽 `EncounterModel.DebugRandomizeRng()`**：ScenarioInjector 走 `RunManager.EnterRoomDebug(RoomType.Monster, ..., encounter)` 路径而非 `FightConsoleCmd.Process(...)`。如确需 console 路径，Harmony postfix 替换该方法为接受外部 seed
3. **暴露 RNG state 给 Save/Restore**：`RunRngSet.ToSerializable()` / `FromSave(SerializableRunRngSet)` / `LoadFromSerializable(...)` 已现成；`PlayerRngSet` 同。`SerializableRun.SerializableRng` 字段一并打包 RunRngSet state，§2.1 (a) 路径自带

**交付物**：`docs/rng_audit.md` 列：
- 12 + 3 个流的用途表（侦察文档 [D_rng.md §1.1](sts2-reverse/docs/recon/D_rng.md) 已有）
- 11 处 derived seed 的具体公式（每个 `new Rng(...)` 站点 + 输入参数）
- 1 处 outlier 的处理方式
- 验证策略：固定 seed 跑 1M 步 random policy，trajectory bit-exact

**严禁**："grep Random/Randf/Randi/Shuffle/Choose 逐个 patch" —— 那是侦察前的错误假设，会引入几十个无用 Harmony hook 并制造 schema drift 风险。RunRngSet 已经替我们做了集中。

### 2.6 通信层 `Transport`

HTTP server 起步（跨平台、易调试），后续可换 IPC（Unix domain socket / named pipe）。

**强制要求**：每个游戏实例独立端口/独立 socket 路径，从环境变量读取。**绝不**硬编码。

### 2.7 实例生命周期

- 启动时把 endpoint 写到 lockfile（`/tmp/sts2_gym_<instance_id>.lock`）
- 退出时干净释放端口
- 处理崩溃：Python 端检测 game 进程死亡 → 重启 → 重新注入上次 ScenarioSpec

**关键约束：游戏侧大量 manager 是 per-process singleton**——`RunManager.Instance` / `CombatManager.Instance` / `SaveManager.Instance` / `ModelDb` 等。重要的是：**游戏内逻辑通过这些单例查全局状态**，而不是把状态烘到对象 mutable copy 里。最具代表性的例子是 ascension——每只怪物的 HP 和伤害在 attribute getter 里通过 `RunManager.Instance.HasAscension(level)` 实时查询，因此 ascension 等级是**进程级**的，不是 RunState-instance 级。

这带来一组工程不变量，**VectorEnv / 并发设计必须遵守**：

1. **单进程 = 单 episode 并发**——同一时刻一个游戏进程内只能有一个 active RunState / CombatState。可以**串行**复用（reset 多次切 scenario），但**绝对不能**让 Python 端同时持有两个不同进程外 handle 指向"同一进程内两个不同 RunState 引用"
2. **VectorEnv N 个 env = N 个独立 game process**——dev plan §2.6 "每实例独立端口"已隐含，但要明确写出来。`pip install` 出的 `GameProcess` Python 类必须负责生成 N 个 OS process，每个绑定独立端口 / lockfile / 进程 PID
3. **ScenarioSpec.ascension 切换 = 同一进程内 reset 即可**（合法），因为 `SetUpNewSinglePlayer` 替换 `RunManager.Instance` 持有的 State 引用，旧 State 失去单例锚点自然不再被任何代码 query。但**Python 端不能保留对旧 obs 状态的"活引用"用于后续 step**——reset 之后所有旧 handle 必须丢弃
4. **跨 env 的 ascension 多任务训练**（如 VectorEnv 同时跑 A0/A5/A10 做难度泛化）合法且推荐，前提是 **N 个进程 × N 个 ascension 配置**，每个进程的 `GameProcess.spawn(ascension=N)` 用 env var 或 startup arg 注入默认 ascension。**严禁**在同一进程内并发持有不同 ascension 的两个 RunState
5. **`AsyncVectorEnv` pickle**：Env 持有的 socket / process handle 必须显式实现 `__getstate__` / `__setstate__`（dev plan §9 易错点 10 已提）；ascension 配置也算 env-init 参数，pickle 后子进程要重新 spawn 游戏进程

这条约束不限于 ascension——任何"通过 `RunManager.Instance` / `CombatManager.Instance` 全局查询的状态"都遵循同一规则，包括 game mode、modifier list、ascension level、当前 character、unlock state。**进程隔离是 STS2-Gym VectorEnv 的硬性物理边界，文档（特别是 README + `GameProcess` docstring）必须明确告知用户**。

### 2.8 人类可读状态渲染器 `HumanRenderer`（LLM 接口的核心组件）

LLM agent 接口的核心组件。每个状态对象暴露**两个并存**的渲染方法，都从同一份底层 state 派生：

- **`to_human_text()`**：自然语言 / Markdown prose 格式
- **`to_human_json()`**：人类可读的结构化 JSON

**两者并存，不二选一**。把两种 view 都暴露出来，由 wrapper 或用户层决定。

**`to_human_json()` 与 mod 内部 raw 序列化的区别**

`Serializer`（§2.1）输出的 raw JSON 是为 RL tensor encoding 服务的，字段缩写、ID 引用、嵌套结构对程序友好对 LLM 不友好。`to_human_json()` 是为 LLM 重新设计的视图：字段名是英文短语而不是缩写、卡牌效果直接展开为描述文本而不是引用 card id、buff 堆叠展开成可读 list、不含 RNG state 等内部字段。

**所有人类可读文本必须走游戏现成本地化**

游戏所有玩家可见字符串（卡牌名 / 卡牌效果 / relic 名 / relic 描述 / power 名 / power 描述 / event 选项文本 / monster name / monster intent 描述 / ascension modifier title&description / 等）已通过 `MegaCrit.Sts2.Core.Localization.LocManager + LocString` 加载，本地化文件在 `raw_pck/localization/<lang>/*.json`（`eng / fra / esp / pol / ptb / deu / tur`，STS2 暂未官方支持 chs）。

HumanRenderer **直接从 LocManager 拿字符串**，不要自建 `human_description` schema metadata（旧 dev plan 提的方案），更不要自己英译游戏术语。具体规则：

1. 输入是 §2.1 Serializer 输出的结构化数据（含 `ModelId` 引用，如 `{"id": "STRIKE", "amount": 1}`）
2. HumanRenderer 用 `LocString("cards", "STRIKE.name") / LocString("cards", "STRIKE.description") / ...` 拼出"Strike (cost 1, attack): Deal 6 damage."
3. **BBCode strip 在入口统一做一次正则替换**（`\[\/?[a-z]+\]` → `""`），见下方设计要点 6
4. STS2-Gym 默认走 `eng` 包；用户可通过 env 构造参数切换（P2 议题）
5. 如有本地化 key missing（游戏更新后新增内容），HumanRenderer fallback 到 `ModelId.Entry` 显示原始 enum 字符串，并在 `info["render_warnings"]` 标注，**不要崩 env**

**为什么不自建 description schema**：(a) 已经有 ground truth、自建就是双轨维护源；(b) 游戏更新增删 / 改 description 时，自建 schema 不会自动跟进、CI 不会发现；(c) 多语言扩展（P2）走官方包 0 成本，自建得另写一套

**为什么两个方法都要提供**

不同 LLM 使用模式适合不同格式，这不是哲学问题而是工程问题：

- **长 reasoning chain / multi-turn 对话场景**：text 格式 token 更省，更接近 LLM 训练数据分布
- **Tool-use / function-call pipeline**：JSON 更容易被程序化后处理与 reasoning trace 解耦
- **Few-shot in-context learning**：JSON 让 few-shot examples 之间 schema 一致，模式更明显
- **多模型横向对比**：不同模型对格式的敏感度不一样，提供两种让研究者能控制变量

让 env 替用户选格式是错的，让用户能 A/B 自己的输入格式才是基础设施该做的事。

**`to_human_text()` 示例输出**：

```
You are playing Ironclad. HP: 56/72. Energy: 3/3.

Your hand (5 cards):
  1. Strike (cost 1, attack): Deal 6 damage.
  2. Strike (cost 1, attack): Deal 6 damage.
  3. Defend (cost 1, skill): Gain 5 block.
  4. Bash (cost 2, attack): Deal 8 damage. Apply 2 Vulnerable.
  5. Cleave (cost 1, attack): Deal 8 damage to ALL enemies.

Enemies:
  [A] Cultist  (HP 48/48). Intent: Buff — gain 3 Ritual next turn.
  [B] Jaw Worm (HP 40/40). Intent: Attack — deal 11 damage.

Your relics: Burning Blood, Vajra.
Your status effects: none.
Piles: 4 cards in draw, 0 in discard, 0 in exhaust.
```

**`to_human_json()` 示例输出**：

```json
{
  "character": "Ironclad",
  "hp": {"current": 56, "max": 72},
  "energy": {"current": 3, "max": 3},
  "hand": [
    {"id": "h0", "name": "Strike", "cost": 1, "type": "attack", "effect": "Deal 6 damage."},
    {"id": "h1", "name": "Strike", "cost": 1, "type": "attack", "effect": "Deal 6 damage."},
    {"id": "h2", "name": "Defend", "cost": 1, "type": "skill",  "effect": "Gain 5 block."},
    {"id": "h3", "name": "Bash",   "cost": 2, "type": "attack", "effect": "Deal 8 damage. Apply 2 Vulnerable."},
    {"id": "h4", "name": "Cleave", "cost": 1, "type": "attack", "effect": "Deal 8 damage to ALL enemies."}
  ],
  "enemies": [
    {"id": "A", "name": "Cultist",  "hp": {"current": 48, "max": 48},
     "intent": {"type": "buff",   "description": "Gain 3 Ritual next turn."}},
    {"id": "B", "name": "Jaw Worm", "hp": {"current": 40, "max": 40},
     "intent": {"type": "attack", "description": "Deal 11 damage.", "damage": 11}}
  ],
  "relics": ["Burning Blood", "Vajra"],
  "status_effects": [],
  "piles": {"draw": 4, "discard": 0, "exhaust": 0}
}
```

**两种格式共同的设计要点**：

1. **稳定且可学习的格式**：相同状态在不同 step 渲染结果稳定，便于 LLM 模式匹配
2. **完备性**：包含 RL 接口能拿到的所有信息（在 FullInfo 模式下）
3. **可定位性**：用 `[A]` `[B]` / `"id": "A"` 给敌人和手牌加 label，让 action 能引用
4. **效果文本对齐游戏内显示**：直接复用游戏卡牌描述字符串，不要自己翻译
5. **不暴露内部字段**：RNG state、未来事件、牌库顺序在 PartialObs 模式下隐藏
6. **BBCode 标记必须剥离**：游戏本地化字符串（卡牌效果、relic 描述、event 选项、ascension 描述等）大量包含 Godot RichText BBCode（`[gold]X[/gold]`、`[blue]80%[/blue]`、`[red]Cursed[/red]`）。`to_human_text()` 必须 strip 这些 markup 保留纯文本；`to_human_json()` 也同样剥离（数字 / 关键词通过 JSON 字段结构本身表达，不需要 markup）。**统一在 HumanRenderer 入口做一次正则替换**（`\[\/?[a-z]+\]` → `""`），不要让每个调用点各自处理

**为什么需要这一层（不把 raw JSON 或 tensor 直接喂给 LLM）**：

1. **Token 效率**：raw JSON 充满冗余括号、内部 ID 引用、schema 元数据；人类可读格式（无论 text 还是 json）剥离这些
2. **游戏原生语义就是自然语言**：卡牌效果、敌人意图在游戏里就是英文句子，强行转 tensor/ID 会丢字段间的细微语义（条件触发、buff 顺序）
3. **LLM 训练分布对齐**：LLM 见过大量"战斗状态报告"风格的文本，没见过 STS2 专用的 raw schema
4. **避免 schema 学习负担**：每场战斗都在 prompt 里塞 schema 解释会浪费上下文

提供 PartialObs 模式，隐藏 draw pile 顺序等人类玩家看不到的信息。FullInfo 与 PartialObs 在两种格式下都要支持。

---

## 3. Python 侧组件

### 3.1 Env 类

继承 `gymnasium.Env`。observation_space 用 `gym.spaces.Dict`：

```python
# Phase 枚举（基于侦察任务 C 在 AutoSlayer 中识别出的 12 个 phase，对应 6 个 RoomHandler + 13 个 ScreenHandler）：
PHASES = [
    "main_menu",  # 仅在 reset 之间瞬时出现，正常 step 不会观察到
    "map",        # MapScreen 上选下一节点
    "combat",     # Monster / Elite / Boss 战斗中
    "event",      # Event room 选项
    "shop",       # Merchant room
    "rest",       # Rest site
    "treasure",   # Treasure room
    "reward",     # 战斗后 reward screen (gold / card / potion / relic)
    "upgrade",    # Deck upgrade screen
    "transform",  # Deck transform screen
    "enchant",    # Deck enchant screen
    "game_over",  # win / loss 终态
]
observation_space = Dict({
    "phase": Discrete(len(PHASES)),  # 12
    "combat": Dict({...}),            # 仅 phase=="combat" 时有意义
    "run":    Dict({...}),            # 整 run 状态（hp/gold/relics/potions/deck/...）
    "map":    Dict({...}),            # 仅 phase=="map" 时主导
    "meta":   Dict({...}),            # ascension / character / seed / floor 等不变量
})
```

每个 phase 的合法动作集合由 `info["action_mask"]` 表达。**phase 之间不是 Markov 等价**——battle 期间 hand/draw/discard 字段有意义，map 期间则是节点列表，所以 obs 的非平稳 sub-Dict 用 zeros / sentinel 填充由 wrapper 处理。

action_space：`Discrete(N)` + `info["action_mask"]`，其中 N 是所有 phase 中合法动作数上界。

构造函数：
```python
SlayTheSpire2Env(
    scenario: ScenarioSpec | None = None,        # None 则走默认随机 run
    obs_mode: Literal["tensor", "text", "json", "all"] = "tensor",
    info_mode: Literal["full", "partial"] = "partial",
    render_mode: Literal["human", "rgb_array", "ansi"] | None = None,
    game_path: str | None = None,                # 自动检测
    instance_id: int = 0,
)
```

`obs_mode` 选项：
- `"tensor"`：RL 默认，返回 tensor dict
- `"text"`：LLM prose 模式，返回 `to_human_text()` 字符串
- `"json"`：LLM tool-use 模式，返回 `to_human_json()` dict
- `"all"`：三种视图同时返回（key 为 `tensor` / `text` / `json` 的 dict）

**`info["text_obs"]` 与 `info["json_obs"]` 在所有模式下都必须存在**，方便 wrapper 转换、debug 检查、双格式同时记录。

### 3.2 进程管理器 `GameProcess`

负责拉起 / kill / restart 游戏进程、分配端口、管理 lockfile、健康检查。`Env.__init__` 内部使用，对用户透明。

**关键 P0 职责清单**（任一缺失都会让 fresh install 上 mod 完全沉默禁用，没有 obvious 错误信号）：

1. **预写 `PlayerAgreedToModLoading = true`**：游戏的 `SettingsSave.ModSettings` 在用户**首次启动游戏 + 在 Mods UI 上明确点同意**前所有 mod 强制 `ModLoadState.Disabled`。Docker / CI / pip install 流程**必须**在拉起游戏前把 settings.json 中的 `mods_enabled: true` 写好（路径取决于平台，由 `python -m sts2_gym.doctor` 检测并补写）。**这条不做整个 pipeline 静默死锁——mod 加载、HTTP 端点起不来、Python 端 timeout，但 game log 只会有一行"mods disabled"**

2. **检查 STS2 game version**：STS2 处于 EA，build 可能频繁变。GameProcess 启动后第一件事用 `/version` 端点（mod 暴露）读 `sts2.dll` 的版本字符串，与 STS2-Gym 自身声明的 supported version range 比对，超出 → fail-fast 报错并指向迁移文档（dev plan §5.3）

3. **支持 `nomods` debug 模式**：游戏命令行参数 `nomods` 跳过 mod 加载——用于验证"问题是 mod 引起的还是游戏自身的"。`GameProcess.spawn(no_mods=True)` 应支持该 flag，但**正常 Env 启动严禁使用**——`/observe` 没有 mod 撑场子就拿不到状态

4. **`ascension` 不通过 RunState 而是通过进程级 startup 注入**：见 §2.7 process-singleton 约束。`GameProcess.spawn(ascension=N)` 把目标 ascension 通过环境变量或 startup arg 传给 mod，mod 启动时存为进程级"默认 ascension"，后续 reset 都使用这个值（如需切换 ascension 必须重启该进程）

5. **健康检查 + 重启**：周期性 ping mod `/health`，超时 → kill + respawn → 重新注入上次 ScenarioSpec（含 ascension）。重启的 episode 标 `info["restarted"] = True`，调用方可决定丢弃还是续跑

6. **lockfile 与端口分配**：每实例独立 `/tmp/sts2_gym_<instance_id>.{lock,port}`，从环境变量读取 base 路径，绝不硬编码 `7777` 类端口号

配套：

- `python -m sts2_gym.install`：检测 STS2 install 路径、copy mod 文件、写 manifest、提示首次启动注意事项
- `python -m sts2_gym.doctor`：检查项 = [STS2 install 路径正确？ / mod 已 copy 到位？ / `mods_enabled=true` 已写？ / DLL 版本在支持范围？ / 测试 mod 加载 + HTTP 端点起得来？]，逐项 ✓/✗ 报告

### 3.3 编码层

**`obs_encoder`**：raw state → tensor dict。独立模块，独立测试，**不与 Env 耦合**。
**`human_renderer`**：raw state → `{"text": str, "json": dict}`。两种视图共享同一份底层 state，确保一致性。
**`action_codec`**：双向映射，至关重要——见 §3.4。

### 3.4 Action Codec（双接口的关键粘合层）

这一层必须支持三种 action 形式之间的互转：

```
Discrete int  ◄──►  Structured dict  ◄──►  Canonical text
       │                    │                       │
       │                    │                       │
   ┌───┴────┐         ┌─────┴─────┐          ┌──────┴──────┐
   │  RL    │         │  Internal │          │   LLM       │
   │ agent  │         │   form    │          │   agent     │
   └────────┘         └───────────┘          └─────────────┘
```

**Structured action**（内部规范）—— 覆盖 §3.1 列出的 12 个 phase 全部决策点：

```python
# Combat phase
{"type": "play_card", "card_idx": 2, "target": "B"}
{"type": "end_turn"}
{"type": "use_potion", "potion_idx": 0, "target": "A"}
{"type": "discard_potion", "potion_idx": 0}                      # 部分场合需主动 discard

# Map phase
{"type": "choose_map_node", "node_coord": [row, col]}            # 选下一节点
{"type": "open_map_overview"}                                    # 看完整地图（不消耗 step）

# Event phase
{"type": "choose_event_option", "option_idx": 0}                 # 当前 event 选项 0..N-1

# Shop phase
{"type": "buy_card", "shop_idx": 2}
{"type": "buy_relic", "shop_idx": 0}
{"type": "buy_potion", "shop_idx": 1}
{"type": "purge_card", "deck_card_idx": 5}                       # 付费删卡
{"type": "leave_shop"}

# Rest site phase
{"type": "rest"}                                                 # 回血
{"type": "smith", "deck_card_idx": 3}                            # 升级一张卡
{"type": "leave_rest"}                                           # 跳过（如有其他 mod 选项）

# Treasure phase
{"type": "open_chest"}
{"type": "skip_chest"}                                           # 部分 ascension 配置下 chest 可跳

# Reward phase (战斗后)
{"type": "pick_card_reward", "choice": 1}                        # 0..N-1，N+1 为 skip
{"type": "pick_relic_reward"}
{"type": "pick_gold_reward"}
{"type": "pick_potion_reward", "choice": 0}
{"type": "skip_reward_item", "item_kind": "card"|"relic"|"gold"|"potion"}
{"type": "leave_reward_screen"}                                  # 全部处理完离开

# Card selection screens (upgrade / transform / enchant / select-to-discard 等)
{"type": "select_cards", "indices": [0, 2], "screen": "upgrade"|"transform"|"enchant"|"discard"|"exhaust"|"...")
{"type": "confirm_selection"}                                    # 提交多选
{"type": "skip_selection"}                                       # 跳过（如允许）

# Game over phase
{"type": "proceed"}                                              # 仅一个动作，进入下一 episode 边界
```

非战斗 phase 的 action 在 mod 端**全部走 §2.3 列出的 ICardSelector + 5 个 mod 自引入的 selector 注入栈**——Python step 端拿到 structured action 后，mod 端把对应 selector 注册到栈顶、解锁等待中的 await、然后 selector 自动 pop。Python 端拿到的 obs 中 `info["action_mask"]` 告诉当前 phase 哪些 action type / 哪些 index 合法。

**Canonical text**（LLM 输出格式）：
```
# Combat
play Defend                  # 无目标
play Bash on B               # 有目标
end turn
use Fire Potion on A
discard Block Potion

# Map / event / shop / rest / treasure
choose map A2                # 节点坐标的可读形式
choose option 0              # event 选项
buy card Inflame
buy relic Vajra
purge Strike#2               # 删除 deck 中第 2 张 Strike（多张同名时用 #idx 消歧）
leave shop
rest
smith Defend
open chest
skip chest

# Reward / card selection
pick card Bash
pick gold
skip card reward
upgrade Strike, Defend       # 多选（comma 分隔）
confirm
skip selection

# Game over
proceed
```

**Action codec 必须实现：**

1. `to_discrete(structured) -> int`：用于 RL 接口
2. `from_discrete(int, current_state) -> structured`：mask 后采样的反查
3. `to_text(structured) -> str`：canonical text 输出
4. `from_text(str, current_state) -> structured | ParseError`：**鲁棒文本解析器**

### 3.5 鲁棒 LLM Action 解析器（关键组件）

LLM 输出无法假设格式严格。解析器必须处理：

- 大小写不一致：`Play STRIKE on b` → OK
- 卡牌歧义：手里有两张 Strike，输出 `play strike` → 默认选第一张，或返回 `AmbiguousAction` 让 wrapper 决定
- 同义词：`attack with Bash` / `cast Bash` / `use Bash` 都映射到 `play Bash`
- 多余的解释文本：`I'll play Strike on the Cultist because it's about to buff` → 提取 `play Strike on A`
- Tool-use 格式（JSON）也要支持：`{"action": "play_card", "card": "Strike", "target": "A"}`
- 完全无法解析：返回 `ParseError`，**不要**抛异常崩溃 env

提供 `LLMActionParser` 配置：
```python
LLMActionParser(
    strategy="strict" | "lenient" | "fuzzy",
    on_parse_fail="raise" | "noop" | "random_legal" | "end_turn",
    on_ambiguous="first" | "raise" | "ask_clarify",
)
```

**关键设计原则：不强制 LLM 整段输出都是 strict JSON**

即使 `to_human_json()` 输出 JSON 形式的状态，**不要**反过来要求 LLM 用 constrained decoding 或 OpenAI strict JSON mode 来输出 action。强制结构化生成会让模型跳过 reasoning，质量明显下降——模型会在还没想清楚时就提交一个 well-formatted 但糟糕的 action。

推荐 pattern（**reasoning-then-action 两步**）：

1. 让 LLM 在 `<thinking>...</thinking>` 标签内或 free-form prose 段落中完成 reasoning
2. 在结尾以可解析格式给出 action：标签如 `<action>play Strike on A</action>`，或单独 JSON 代码块如 ` ```json\n{"action":"play_card",...}\n``` `
3. parser 从输出中**提取** action 段落，对其余部分宽容

不在 token-level 强约束，在解析层做容错。`ChainOfThoughtWrapper` 默认走这个 pattern；`ToolUseSchemaWrapper` 把 action 提取放到 native tool call 里，LLM 在 tool call 之前仍可自由 reasoning。

### 3.6 ScenarioSpec

```python
@dataclass
class ScenarioSpec:
    character: Literal["Ironclad", "Silent", "Defect", "Necrobinder", "Regent"]
    ascension: int = 0                           # 0–10，见下方 Ascension 表
    
    # Combat-level fields (P0)
    deck: list[CardSpec] | None = None
    starting_hand: list[CardSpec] | None = None  # 不指定则正常 draw
    energy: int | None = None
    hp: tuple[int, int] | None = None            # (current, max)
    relics: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)
    enemies: list[EnemySpec] | str | None = None  # 可指定具体或 encounter id
    
    # Floor-level fields (P1)
    floor: int | None = None
    map_position: tuple[int, int] | None = None
    
    # Reproducibility
    seed: int | None = None
    
    # LLM-facing: 自然语言描述（自动生成 + 可覆盖）
    description: str | None = None
    
    @classmethod
    def random(cls, **constraints) -> "ScenarioSpec": ...
    
    def to_prompt(self) -> str:
        """生成 LLM 友好的场景描述，用于 system prompt 或 task brief。"""
```

`to_prompt()` 让 LLM 评测能直接拿到任务描述。**核心原则**：人类玩家在 Ascension 选择面板上能看到的所有 modifier title + description，LLM 也必须看到——这是 dev plan §0 双一等公民的应然推论。`to_prompt()` 中的 ascension 部分**必须从游戏本地化文件直接读取**，不能自己重述，避免 STS1 知识污染 LLM：

```
You are playing Ironclad at Ascension 6 (Inflation).
Active ascension modifiers (cumulative A1-A6):
  +A1 Swarming Elites: Elites spawn more often.
  +A2 Weary Traveler: Ancients only heal 80% of your missing HP.
  +A3 Poverty: Enemies and Treasure Chests drop 25% less Gold.
  +A4 Tight Belt: Start each run with 1 less potion slot.
  +A5 Ascender's Bane: Start each run Cursed.
  +A6 Inflation: Removing cards from your deck at the Merchant is more expensive.

You are now in a combat scenario.
Your deck contains 10 Strikes, 9 Defends, and 1 Bash.
You start at full HP (72/72) with 3 energy.
You face: Cultist (48 HP) and Jaw Worm (40 HP).
Goal: defeat all enemies. Reward: +200 on victory, -100 on defeat,
plus HP-preservation shaping.
```

**Ascension 等级表**（11 级，**累加生效**：A6 同时启用 A1–A6 全部修饰，由 `AscensionManager.HasLevel(level) => _level >= (int)level` 实现）。"Title" 与 "Player-visible description" 来自 `raw_pck/localization/eng/ascension.json`，与游戏内 UI 完全一致；"代码层数值 ground truth" 来自相应 helper 调用点，给 P0 实施时做断言验证 & RL tensor encoding 用：

| Lvl | Enum 名 | Title | Player-visible description (官方本地化) | 代码层数值 ground truth |
|---|---|---|---|---|
| A0 | `None` | No Ascension | Play without any Ascension modifiers. | — |
| A1 | `SwarmingElites` | Swarming Elites | Elites spawn more often. | `NumOfElites = round(5 × 1.6) = 8`（默认 5）—— 见 `MapPointTypeCounts.cs:14` |
| A2 | `WearyTraveler` | Weary Traveler | Ancients only heal 80% of your missing HP. | 仅 Ancient 事件（如 Neow）治疗量 `× 0.8`，作用于 `MaxHp - CurrentHp` —— 见 `AncientEventModel.cs:153` |
| A3 | `Poverty` | Poverty | Enemies and Treasure Chests drop 25% less Gold. | 战斗 / 精英 / Boss / treasure room 全部金币奖励 `× 0.75`（`PovertyAscensionGoldMultiplier`）—— 见 `EncounterModel.cs:53,72` + `OneOffSynchronizer.cs:112` |
| A4 | `TightBelt` | Tight Belt | Start each run with 1 less potion slot. | `player.SubtractFromMaxPotionCount(1)` 在 `ApplyEffectsTo(player)` 中执行（run 启动一次）—— 见 `AscensionManager.cs:29` |
| A5 | `AscendersBane` | Ascender's Bane | Start each run Cursed. | 起始 deck 追加 1 张 `AscendersBane` 诅咒卡（`FloorAddedToDeck=1`）—— 见 `AscensionManager.cs:33` |
| A6 | `Inflation` | Inflation | Removing cards from your deck at the Merchant is more expensive. | 商店 card-removal 服务费基础价 `75 → 100`、每次使用累计涨价 `25 → 50` —— 见 `MerchantCardRemovalEntry.cs:17,19` |
| A7 | `Scarcity` | Scarcity | Rare and Upgraded cards appear less often. | 卡牌稀有度 odds 调整：常规 rare `0.03 → 0.0149`、shop rare `0.09 → 0.045`、elite rare `0.1 → 0.05`、common odds 略升对冲；升级卡概率缩放 `0.25 → 0.125` —— 见 `CardRarityOdds.cs` + `CardFactory.cs:22` |
| A8 | `ToughEnemies` | Tough Enemies | All enemies are harder to kill. | 101 个怪物的 `MinInitialHp` / `MaxInitialHp` 上浮（典型 +1 到 +10，看怪种） —— 分布在 `MegaCrit.Sts2.Core.Models.Monsters/` |
| A9 | `DeadlyEnemies` | Deadly Enemies | All enemies have deadlier attacks. | 96 个怪物的攻击伤害 / block / strength gain 上浮（典型 +1 到 +5） —— 同上目录 |
| A10 | `DoubleBoss` | Double Boss | Fight two bosses at the end of Act 3. | 最后一个 act 的 boss 房追加第二个随机 boss encounter（从 act 全部 boss 池里去除当前 boss 后随机选一个）—— 见 `RunManager.cs:499` |

**关于本地化文本的实现细节**：

- 游戏内通过 `AscensionHelper.GetTitle(level)` / `GetDescription(level)` 加载（key 格式 `ascension.LEVEL_{NN}.title` / `ascension.LEVEL_{NN}.description`），其中 NN 是 `D2` 格式两位整数（00–10）
- 这些 string 含游戏内 BBCode 标记（`[gold]Elites[/gold]`、`[blue]80%[/blue]`、`[red]Cursed[/red]`）—— `to_prompt()` 输出给 LLM 前应**剥离 BBCode**保留纯文本。**这条规则适用所有走本地化字符串的 LLM-facing 输出**（卡牌描述、relic 描述、event 选项文本等也都带 BBCode），由 §2.8 HumanRenderer 在 `to_human_text()` / `to_human_json()` 派生路径上统一做一次 strip——保留语义、剥离 markup
- 多语言版本同目录下都有（`pol/fra/esp/ptb/deu/tur`），但 STS2-Gym 的 LLM eval 默认走 `eng/` ——多语言 evaluation 是 P2 议题
- 玩家在 portrait hover-tip 上**只看到 title 列表**（`PORTRAIT_DESCRIPTION = "{ascensions:list: +{}|\n}"` 拼出"+Swarming Elites\n+Weary Traveler\n..."），description 要点开 Ascension 选择面板才能看到。`to_prompt()` 把两者都塞给 LLM，**给得比玩家 portrait 多一点**——但仍是玩家在游戏 UI 内能查到的合法信息

**对 Combat-level injection 的影响**：构造 RunState 时把 ascension 传给 `RunState.CreateForNewRun(..., ascensionLevel, seed)`，游戏会自动给 deck 加 `AscendersBane` curse 卡（A5+）、给敌人按 act 缩放 HP（A8+/A9+）等。**ScenarioInjector 端无需自己实现这些 modifier**——只传 int 参数。但**注意 ascension 与 deck override 的交互**：若用户同时指定了 `deck` 和 `ascension >= 5`，AscendersBane curse 卡会被游戏侧追加进 deck，导致最终 deck 与 spec.deck 不完全一致。这种"用户意图 vs 难度规则"的冲突，默认按"难度规则胜出"处理，并在 `info["scenario_warnings"]` 中明确告知。

配套 `ScenarioSampler`：
```python
sampler = ScenarioSampler.from_distribution("Act1Encounters", character="Ironclad")
for episode in range(1000):
    spec = sampler.sample()  # 随机一个第一章遭遇
    obs, info = env.reset(options={"scenario": spec})
```

### 3.7 Reward 系统

**核心 Env 默认 reward**：
```
r_step  = -α · hp_lost_this_step
r_combat_end = +β · hp_fraction_remaining
r_floor = +γ
r_die   = -100
r_win   = +200
```

α, β, γ 设默认值，但**所有 reward 分量**必须放进 `info["reward_components"]`，让用户能用 `RewardShapingWrapper` 重新组合。

**不要**把多种 shaping 焊死在 Env 里。

**关于 ascension 与 reward 的语义校准**：

默认 reward 公式跨 ascension **不做缩放**——`r_win=+200` 不管 A0 还是 A10 都给相同奖励。这是有意的设计选择，**因为它把"难度"和"奖励"解耦，让 ascension 真正成为独立的研究坐标轴**：研究者可以问"同一 agent 在 A0/A5/A10 上分别能拿到多少 mean episode return"，差异本身就是 benchmark 区分度。

但默认 reward 不缩放有几个**用户必须知道的语义陷阱**：

1. **HP 损失"代价"跨 ascension 不等价**——A9 (DeadlyEnemies) 怪伤害高，"丢 10 HP"在 A9 是一次普通过招、在 A0 是失误。如果用户拿 A9 数据训 agent，再迁移到 A0 评测，agent 会过度防御
2. **金币奖励跨 ascension 不等价**——A3 (Poverty) 全奖励 × 0.75，但默认 reward 不包含金币项，所以**直接不受影响**。但用户若打开 `RewardShapingWrapper` 加入金币 reward shaping，必须自己处理这条
3. **通关奖励 `r_win=+200` 在 A10 显著低估实际难度**——A10 DoubleBoss 通关本身就比 A0 通关难 5-10×。论文里若把 A0 / A10 的 mean return 直接平均会得出误导性结论

**API 表达**：

- `info["reward_components"]` 必须包含 `ascension_level` 字段（不参与默认 reward，但让 shaping wrapper 可见难度上下文）
- 提供 `AscensionScalingRewardWrapper`（dev plan §3.8 wrapper 套件应加入）实现 `r_scaled = r * ascension_scale(level)`，让做难度归一化分析的研究者一行接入

**核心原则不变**：默认 reward 是 benchmark 含义的定义点（§10.4），跨 ascension 不悄悄缩放——任何缩放都必须显式 opt-in。

### 3.8 双接口 Wrapper 套件

提供开箱即用的 wrapper，分两组：

**RL 通用**：
- `FlattenObservation`
- `RecordEpisodeStatistics`
- `RecordVideo`（rgb_array 模式）
- `TimeLimit`
- `NormalizeReward`
- `ActionMaskedRandomAgent`（baseline）
- `HeuristicAgent`（baseline，rules-based 启发策略：贪心打牌、保命阈值、固定 reward 优先级）—— §8.1 baselines 表的"Heuristic 数字"对应实现
- `PartialObsWrapper`：把 FullInfo obs（含 RNG state、draw pile 顺序、未来 reward 池等）过滤到 PartialObs（人类玩家视角）。**dev plan §4 / §6 都明确两种模式必须存在**，此 wrapper 提供 FullInfo → PartialObs 的统一转换路径，避免每个 env 实现自己重做 filter 逻辑。filter 规则与 §2.8 中列出的 7 类隐藏字段对齐
- `SaveStateRestoreWrapper`：暴露 `env.save_state() -> bytes` / `env.restore_state(bytes)` API，内部走 mod 端 `RunManager.ToSave()` + `SetUpSavedSinglePlayer(...)` 配合 `SerializableCombatState`（§2.1）。**§11 P1 milestone "MCTS / branching rollout" 的对接组件**——任何分支搜索算法都靠这个 wrapper 复制当前节点

**Reward**：
- `RewardShapingWrapper`：read `info["reward_components"]` 重组 reward 分量（§3.7）
- `AscensionScalingRewardWrapper`：opt-in 难度归一化，`r_scaled = r * scale_fn(ascension_level)`，默认 `scale_fn = lambda a: 1 + 0.2 * a`（A10 时 reward × 3，可覆盖）

**LLM 专用**：
- `TextObservationWrapper`：obs → `to_human_text()` 字符串
- `JsonObservationWrapper`：obs → `to_human_json()` dict
- `LLMActionWrapper`：吃 text 或 JSON 形式 action，内部走解析器
- `HistoryCompressionWrapper`：长 episode 中压缩历史（只保留最近 N 回合 + 摘要）
- `ToolUseSchemaWrapper`：把 action_mask 转成 OpenAI/Anthropic tool-use JSON Schema，让 LLM 不需要看 mask、直接在合法动作集合里选；LLM 仍可在 tool call 前自由 reasoning
- `TokenBudgetTracker`：累计估算 token 消耗（监督评测成本）
- `ChainOfThoughtWrapper`：reasoning-then-action 两步法（见 §3.5），允许 LLM 输出 reasoning + action，从结尾标签/代码块提取 action 不影响 env

---

## 4. 双接口设计的关键约束

### 4.1 状态唯一性

底层只有一份 state。tensor obs、text obs、json obs 都是同一份 state 的视图函数。**禁止**任两者出现内容不一致的情况。提供自动化测试：随机生成 state → 同时生成 tensor / text / json 三种视图 → 反解析回结构化数据 → 三者断言一致（在 FullInfo 模式下完全等价）。

### 4.2 动作语义统一

`step(discrete_id=5)` 与 `step(text="play Strike on A")` 在解析后对应同一个 structured action 时，必须产生 bit-exact 一致的下一状态。这是 dual interface 正确性的根本保证。

### 4.3 同一 reward

RL 和 LLM 用同一个 reward。这样它们的分数才能直接比较，研究价值才存在。**禁止**为 LLM 单独设计 reward。

### 4.4 同一 task suite

每一个 `gym.make("STS2/XXX-v0")` 都自动同时支持两种 agent。这是论文里"我们在 STS2-Gym 上对比了 PPO 和 GPT-4o"这种论述的前提。

### 4.5 同一 seed 复现

LLM 模式下用 `temperature=0` + fixed seed 应能复现 trajectory。env 必须不引入额外的随机性。

---

## 5. 接口契约

### 5.1 协议版本号

所有跨 mod-python 的 message 带 `protocol_version`。版本不匹配立即报错。

### 5.2 Schema 文档

ScenarioSpec / Observation / Action / SaveState 四份 schema 用 JSON Schema 或 protobuf 定义，**代码生成**，不手工维护两份。

### 5.3 兼容性策略

明确写入 README：
- v0.x 阶段不保证向后兼容
- v1.0 之后 minor 版本向后兼容，major 不兼容
- 任何破坏性变更附迁移工具

---

## 6. 测试与质量保证

| 测试类型 | 验证内容 |
|---|---|
| Property test | 跑 1M 步随机 policy，断言 obs ∈ space、reward 是 finite scalar、mask 至少有一个合法动作 |
| Determinism test | 固定 seed + policy → trajectory bit-exact |
| Reset cleanliness | 同 seed reset 两次 traj 一致；不同 seed reset 后 traj 必须不同 |
| VectorEnv 隔离 | N=4 并行各自跑 random policy，与单独跑同 seed 结果集合一致 |
| Game version regression | 每次游戏更新跑金标 trajectory，diff 字段。**首选方案：复用 `MegaCrit.Sts2.Core.AutoSlay.AutoSlayer`**——Mega Crit 自带的端到端 self-play 测试框架，对 game version drift 天然敏感，不用我们自己写一套 deterministic agent。CI 用固定 seed 跑 AutoSlayer 整 run，hash 输出的 `CombatHistory.Entries` 序列做 fingerprint |
| Throughput benchmark | random policy 跑 10k steps 记录 step/s，CI 内回归。每个 FastMode 档（Normal / Fast / Instant）各跑一份，验证 Instant 真的达到 ≥ 50 step/s 目标（§2.4 / §11 P1） |
| **Dual-interface consistency** | 同一 state → tensor / text / json 三视图 → 反解析回结构化数据，三者在 FullInfo 模式下应等价 |
| **Action symmetry** | discrete action 与对应 text / JSON action 输入产生同样的下一状态 |
| **LLM parser robustness** | 灌入 1000 条带噪声/带 reasoning/带格式错误的 LLM 输出（既 text 风格也 JSON 风格），解析正确率 ≥ 95% |
| **Human obs completeness** | text obs 与 json obs 在 FullInfo 模式下反解析得到的状态完全一致 |
| **PartialObs 信息隔离** | 三种视图在 PartialObs 模式下均不暴露 RNG state、draw pile 顺序、未来 reward 池等隐藏信息（§2.8 列出的 7 类） |
| **Ascension 缩放正确性** | 固定 scenario（同 character / 同 deck / 同 encounter / 同 seed），在 A0 / A5 / A10 三档分别跑首回合敌人 turn，断言：(1) 怪物 HP / 伤害按 §3.6 表中的代码层 ground truth 缩放，(2) A3 金币奖励 × 0.75，(3) A4 起手药水槽 -1，(4) A5 deck 起手多 1 张 AscendersBane，(5) A1 地图 elite 房数变化 |
| **Process 隔离** | 同一 Python 进程内拉起 N=2 个 `GameProcess` 分别设 ascension=0 / ascension=10，各跑 100 episode，断言两进程的 obs / reward 序列完全不互相干扰（即两进程内的 `RunManager.Instance` 互不可见）。这是 §2.7 process-singleton 约束的运行时验证 |
| **BBCode strip 覆盖** | 抽样 100 张卡 / 50 个 relic / 全部 ascension description，断言 `to_human_text()` 输出**不包含**任何 `[gold]`、`[blue]`、`[red]` 等 BBCode 标记。CI grep `\[/?[a-z]+\]` regex 必须 0 命中 |

---

## 7. 分发与打包

### 7.1 安装

```bash
pip install sts2-gym
```

一行解决。

### 7.2 Mod 自动安装

`pip` 包导入时检测 STS2 安装路径，自动 copy mod 文件。用户不应该需要懂 Harmony / Godot mod / dll。

```bash
python -m sts2_gym.install   # 自动安装 mod
python -m sts2_gym.doctor    # 自检
```

### 7.3 Docker 镜像

提供官方 Docker，用户挂载自己的 Steam 游戏目录。

### 7.4 LLM 评测示例

`pip install sts2-gym[llm]` 额外装 `openai`、`anthropic` 等可选依赖。
提供 `examples/llm_baseline.py`，几十行代码跑通 GPT-4o / Claude 在某个 task 上的评测。

---

## 8. 研究 surface（命名任务套件）

提供命名好的 env id 让论文能直接引用。命名格式 `STS2/<Character><Scope>-<Variant>-A<Ascension>-v0`，**ascension 是一等命名维度**，因为它是 STS2 benchmark 的核心难度坐标轴，跟角色和 scope 一起决定难度等价类：

```
STS2/IroncladCombat-Random-A0-v0
STS2/IroncladCombat-Random-A10-v0
STS2/IroncladAct1-A0-v0
STS2/IroncladFull-A5-v0
STS2/DefectCombat-Random-A0-v0
STS2/AnyCharFull-A0-v0           # meta-RL / generalization across characters
STS2/AnyCharFull-AnyAsc-v0       # full generalization, ascension also randomized
STS2/LLMEval-Combat-A0-v0        # 标准 LLM 评测协议（baseline 起手）
STS2/LLMEval-Combat-A10-v0       # 标准 LLM 评测协议（高难度区分度）
STS2/LLMEval-Full-A5-v0
```

`AnyAsc` 表示每个 reset 随机均匀采样 ascension 0-10。Baseline 表的核心横向比较应当包含**至少 A0 / A5 / A10 三档**——这是 STS2-Gym 区分 agent 能力的关键。

### 8.1 Baselines

每个 task 都要给出：
- Random policy 数字
- Heuristic 数字
- PPO baseline + 训练脚本
- **GPT-4o / Claude / 开源模型 LLM baseline + 评测脚本**

最后一项是这套环境的差异化卖点，缺失会严重削弱影响力。

**关键：baseline 表必须沿 ascension 轴 ×3 拉开**

每个 (task, agent) 组合的数字必须**至少**在 `A0` / `A5` / `A10` 三档分别报告。原因：

1. STS2-Gym 的核心差异化卖点是"难度可控"——baseline 表如果只给单一难度的数字，读者无法判断 benchmark 是否饱和、是否还有 headroom，论文的"区分度"叙述就站不住
2. RL agent 和 LLM agent 跨难度的退化曲线**形状不同**——这是 dev plan §0 双一等公民框架的具象化。PPO 通常在 A0-A5 段陡然下降、A5-A10 段平缓；LLM 通常在 A0-A3 段陡降然后早早摆烂。**这种比较只在 multi-ascension 表里能看到**
3. 单一难度的数字会被读者默认是 A0——"PPO 88% 通关率"听起来 benchmark toy，"PPO 在 A10 12% / A5 51% / A0 88%" 才是 benchmark 区分度的具象化

推荐的 baseline 表 schema：

| Task | Agent | A0 | A5 | A10 |
|---|---|---|---|---|
| IroncladCombat-Random | Random | x.x% | x.x% | x.x% |
| IroncladCombat-Random | Heuristic | x.x% | x.x% | x.x% |
| IroncladCombat-Random | PPO | x.x% | x.x% | x.x% |
| IroncladCombat-Random | GPT-4o | x.x% | x.x% | x.x% |
| IroncladCombat-Random | Claude | x.x% | x.x% | x.x% |
| ... | ... | ... | ... | ... |

每格是 100-1000 局的 mean win rate（或 mean episode return，取决于 task），加 95% CI bracket。**总结性数字（"PPO 平均通关率 50%"）禁止**——这种数字隐藏 ascension 维度、误导读者。所有 baseline 必须按难度分层报告。

Paper 版块外（README / leaderboard）可保留"agent X 跨难度平均分"作为 one-number summary，但表本身必须三档。

### 8.2 Offline 数据集（如可行）

按 D4RL 风格打包真实玩家 run logs（如能与 Mega Crit 合作或社区上传）。这是 offline RL 与 imitation learning 研究的天然数据集。

**🟢 In-house dump 路径几乎免费：游戏自带 `CombatHistory`**

`MegaCrit.Sts2.Core.Combat.History.CombatHistory.Entries` 是 mega crit 在战斗中持续维护的**官方事件日志**，记录：`CardPlayStartedEntry` / `CardPlayFinishedEntry` / `CardAfflictedEntry` / `CardDiscardedEntry` / `CardDrawnEntry` / `CardExhaustedEntry` / `CardGeneratedEntry` / `CreatureAttackedEntry` / `DamageReceivedEntry` / `BlockGainedEntry` / `EnergySpentEntry` / `OrbChanneledEntry` / `PotionUsedEntry` / `MonsterPerformedMoveEntry`。

这意味着 STS2-Gym 不必等"与 Mega Crit 合作或社区上传"——**我们的 mod 自己跑 random / heuristic / PPO / LLM agent 时就能 dump 出 D4RL 格式数据**：

```
trajectory = {
    "scenario": ScenarioSpec.to_dict(),
    "initial_state": SerializableRun (between-rooms) + SerializableCombatState (mid-combat),
    "events": [CombatHistoryEntry, ...],     # 来自 CombatManager.Instance.History.Entries
    "agent_actions": [structured_action, ...],
    "rewards": [r_t, ...],
    "ascension": int,
    "outcome": "win"|"loss"|"abandon",
}
```

P2 milestone 出**两类 offline dataset**：

1. **STS2-Gym own**：mod 自己跑 N 万局 random + heuristic + PPO + LLM mix policy 的 trajectory，覆盖 (character × ascension × scenario distribution) 三维网格。完全可重现（mod 输出 + ScenarioSpec + seed → 任何人能跑出同样的 trajectory）
2. **Player-uploaded**（社区可选）：玩家在游戏内 opt-in 后通过 `ModManager.OnMetricsUpload` hook 上传自己的 SerializableRun + CombatHistory，社区贡献池

第一类是 **STS2-Gym v0.1 就能 ship 的 offline RL benchmark**，不需要等 Mega Crit 合作；第二类是 v1.0 后的 social 贡献途径。

---

## 9. 易错点（按破坏性排序）

1. **RNG 漏控** — 少 hook 一个 random 调用 → reset 不可复现 → 训练曲线方差爆炸。维护显式 RNG 审计清单
2. **同步语义错误** — step 在状态未稳定时返回 → obs 和 reward 不对应同一时刻
3. **`terminated` / `truncated` 混淆** — 直接影响 value bootstrap 正确性。单独写一组 unit test 覆盖每种结束原因
4. **Reset 状态泄漏** — 上 episode 残留 → 后续 episode 偶发异常。验证：连跑 10k episodes 不崩
5. **Action mask 时间错位** — `info["action_mask"]` 必须文档化时间含义：描述基于返回 obs 可选的下一动作
6. **端口/文件冲突** — VectorEnv 多实例硬编码资源 → 灾难。所有资源按 instance_id 命名
7. **动画 bypass 改变逻辑** — 短路了承载逻辑的 await → obs 缺字段
8. **保存/加载不 bit-exact** — 忘存 RNG state / buff counter → restore 后漂移
9. **游戏版本漂移** — README 顶部写明支持版本范围，超出拒绝启动
10. **Pickle 失败** — Env 持有 socket / thread → AsyncVectorEnv 退化或崩。显式实现 `__getstate__` / `__setstate__`
11. **Mod-Python 协议漂移** — 强制版本握手
12. **LLM 解析器静默失败** — 解析失败时返回明确 ParseError，不要 silently no-op
13. **强制 LLM 严格 JSON 输出** — 用 constrained decoding 或 strict JSON mode 强制 LLM 每一步输出严格 JSON 会破坏 reasoning，必须用 §3.5 的 reasoning-then-action 提取模式
14. **Human obs 信息泄漏** — PartialObs 模式下 text obs 或 json obs 不能泄漏 RL tensor obs 没暴露的信息
15. **三视图漂移** — 修改 schema 时只改了 tensor/text/json 中的一两个。CI 必须有三视图一致性 test
16. **`to_human_json()` 错当 `Serializer` 用** — 二者用途不同：前者给 LLM，后者给 RL encoder。维护时别合并
17. **Ascension singleton trap** — `RunManager.Instance` / `CombatManager.Instance` 是 per-process singleton，ascension 通过 `HasAscension(level)` 实时查询，**Python 端不能在同一进程内并发持有两个不同 ascension 的 RunState 引用**。VectorEnv N env = N OS process。Reset 切 ascension 后旧 obs handle **必须丢弃**——继续用会读到错乱的全局状态（详见 §2.7）
18. **BBCode markup 泄漏到 LLM obs** — 游戏本地化字符串普遍含 `[gold]X[/gold]` / `[blue]80%[/blue]` / `[red]Cursed[/red]` 等 Godot RichText 标记，**HumanRenderer 必须统一 strip**（详见 §2.8 设计要点 6）。漏 strip 会：(a) 浪费 token、(b) 打乱 LLM 解析 / 模式匹配、(c) 让 dual-interface consistency test 在 BBCode/无 BBCode 两侧产生 false diff
19. **PlayPile 漏序列化** — `PlayerCombatState.AllPiles` 是 **5 个 pile**（Hand / Draw / Discard / Exhaust / **Play**）不是 4 个。`PlayPile` 是卡牌"正在结算中"的临时 pile，连锁触发 / 动画期间 snapshot 会有卡停留在这里。漏掉 → save/restore round-trip 在 mid-combat 处不 bit-exact、determinism test 偶发失败
20. **Ascension 玩家先验失配** — A2 (Weary Traveler) / A4 (Tight Belt) / A6 (Inflation) 的 STS2 实际效果与 STS1 玩家直觉不同（详见 §3.6 表 + 三处官方文本）。`to_prompt()` **必须**输出官方本地化 description，不要让 LLM 凭 STS1 知识猜难度内涵——否则 LLM agent 会做错误的 reward shaping 假设 (e.g. 以为 A2 "进新房间扣 HP" 而保守玩，实际只是 Neow 治疗打八折，应该正常推进)

---

## 10. 关键设计决策（注意事项）

1. **Observation schema 一旦发布很难改**。v0.x 阶段多迭代收反馈，v1.0 之前不要让大家训太大模型；v1.0 之后任何变更必须有迁移工具

2. **ScenarioSpec 是产品 API**。"自由选关卡和卡组"的好坏完全由 ScenarioSpec 设计决定。既要支持显式构造 `Spec(character="Ironclad", deck=[...], enemy="Jaw Worm")`，也要支持分布采样 `Spec.random(act=1)`

3. **POMDP 诚实性**。FullInfo 与 PartialObs 必须两种模式都提供且**命名清晰**。研究结论不一样，不能混。LLM 模式默认走 PartialObs（人类玩家视角）

4. **默认 reward 定义 benchmark 含义**。引用你的人会引这个分数，所以默认 reward 必须经过思考、有意义、抗 reward hacking。建议默认走"二值通关 + 剩余 HP shaping"

5. **不要把研究者绑定到任何算法栈**。Env 只暴露 Gym 接口，不绑 SB3 / CleanRL / Tianshou / Claude / OpenAI / 任何具体框架。Adapter 提供示例但不强制依赖

6. **Mod 是实现细节，不是 API**。研究者不应该需要碰 C# 代码或 Godot mod 系统。Python 用户的心智负担接近"装了个 pip 包"

7. **文档不是事后补的**。第一周开始写 README，每加 feature 同步更新。**RL env 的成功度和 README 质量正相关，而不是和 env 复杂度正相关**

8. **Tool-use schema 与 action mask 联动**。Wrapper 把当前合法 action 自动转成 OpenAI/Anthropic tool-use JSON Schema，让 LLM 不需要看 mask、直接在合法动作集合里选

9. **Human-readable observation 是产品**。`to_human_text()` 和 `to_human_json()` 的细节（字段顺序、术语选择、缩进、HP 写法、敌人 label 风格）直接影响 LLM baseline 数字。把这两份输出当产品维护，不要当 debug log 写。**两份格式互不替代**，不要预设哪种"更好"——研究者会根据自己的 pipeline 选择

10. **不强制 LLM 严格 JSON 输出**。即使 obs 给的是 JSON，action 侧也不要用 constrained decoding 强制 LLM 每步输出严格 JSON。采用 reasoning-then-action 提取模式（见 §3.5）

11. **Token 成本是一等公民**。LLM 评测的实际门槛是 API 费用。env 必须能精确报告每个 episode 消耗的 token 数量，让用户能预算。两种 obs 格式各跑一份 token 基准

---

## 11. 实施优先级

按依赖关系排序，**严格按顺序**完成每个里程碑后再走下一个：

| 优先级 | 里程碑 | 完成标准 |
|---|---|---|
| P0 | **`PlayerAgreedToModLoading` 自动绕过 + `python -m sts2_gym.doctor`** | fresh install + Docker / CI 直接能跑通 mod 加载，不需要用户在 UI 点同意。doctor 子命令逐项 ✓/✗ 报告 install path / mod path / settings.json / DLL version / mod 加载 / HTTP 端点。**不做这条整个 pipeline 静默死锁** —— 详见 §3.2 |
| P0 | Mod HTTP server + /observe | 能从 Python 拿到一场战斗的完整 JSON state |
| P0 | obs_encoder（tensor 版） | obs ∈ observation_space，property test 通过 |
| P0 | HumanRenderer（text + json 两套） | 同 state 的 text 与 json 在 FullInfo 模式下信息等价；token 计数有 baseline；BBCode 已 strip |
| P0 | ActionDispatcher + 同步语义 + **ICardSelector 扩展** | step 返回时 game 状态稳定；ICardSelector + 5 个 mod-自引入 selector（IMapNodeSelector / IEventOptionSelector / IShopActionSelector / IRestSiteSelector / IRewardScreenSelector）全部接入，覆盖 §3.1 列出的 12 个 phase。**不接 ICardSelector 整个 run 跑不下去 —— 卡 reward / upgrade / transform / enchant 阻塞**。详见 §2.3 |
| P0 | action_codec 三向映射 + LLMActionParser | dual-interface consistency test 通过；非战斗 phase 的 structured action 类型覆盖完整（§3.4） |
| P0 | Combat-level ScenarioInjector | 可以从指定 (character, ascension, deck/enemy) 开始；ascension 必须支持全 0-10 区间 |
| P0 | RngController（轻量版） | determinism test 通过。仅接管 master seed + 避开 `EncounterModel.DebugRandomizeRng` —— **不要**逐个 grep / patch（详见 §2.5） |
| P0 | Gymnasium Env 类 + action mask | random policy + MaskablePPO 能跑通 |
| P0 | LLM baseline 示例 | 几十行能跑通 Claude / GPT 在某 task 上的评测 |
| P0 | 文档 + Schema 生成 | 外部研究者 30 分钟内能跑起来第一个 episode |
| P1 | FastMode 实测（拨开关 + 验证 Instant bit-exact） | step/s 提升到 ≥ 50；Instant / Fast / Normal 三档 `CombatHistory` 事件序列 bit-exact 一致 |
| P1 | VectorEnv 验证 | N=8 并行无冲突；process 隔离 test 通过（§6） |
| P1 | Floor-level injector + Map state | 支持完整 Act1 task |
| P1 | Run-level injector | 支持完整 run task |
| P1 | Save/Restore 端点 + `SaveStateRestoreWrapper` | MCTS / branching rollout 可行 |
| P1 | Ascension 缩放正确性 test（§6） | 同 scenario 在 A0/A5/A10 三档下怪物 HP / 伤害 / 金币 / 起手 deck / 药水槽 按代码 ground truth 缩放 |
| P2 | Docker 镜像、自动 mod 安装 | 一行 pip 装好；Docker 内首启动 doctor 通过 |
| P2 | LLM 评测协议、token tracker | 标准化 LLM benchmark |
| P2 | Offline 数据集（in-house dump + 玩家上传） | mod 自跑 N 万局 random/heuristic/PPO/LLM mix 的 trajectory，按 D4RL 打包；ModManager.OnMetricsUpload 接玩家可选贡献池（§8.2） |
| P2 | 多语言 HumanRenderer | 支持切到 fra / esp / pol / ptb / deu / tur 包做多语言 LLM eval |

---

## 12. 一句话核心

**这套环境的价值不取决于代码量，取决于：研究者能不能在 30 分钟内跑起来、能不能在论文里干净地引用它、能不能信任它的正确性、以及 RL agent 和 LLM agent 能不能在同一套接口上直接比较分数。所有开发决策按这四条加权。**
