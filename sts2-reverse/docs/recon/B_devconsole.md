# 任务 B：DevConsole 是不是 ScenarioInjector 的现成实现

> Scope：`MegaCrit.Sts2.Core.DevConsole/`（8 文件）+ `MegaCrit.Sts2.Core.DevConsole.ConsoleCommands/`（38 命令，sampled 15 个具代表性的） + 涉及到的 `MegaCrit.Sts2.Core.Commands/*Cmd` 静态类（19 个）。
> 一句话结论：**DevConsole 不是 ScenarioInjector，但它指明了正确实现路径——绕过 DevConsole，直接调用其下的 `MegaCrit.Sts2.Core.Commands/*Cmd` 静态 API 和 `RunManager` / `CombatManager` 上的公开方法**。

---

## 1. 关键回答

### 1.1 命令如何注册（问 1）

两条来源拼接（在 `DevConsole` ctor）：

1. **编译期 source-generated 清单**：`AbstractConsoleCmdSubtypes.All` —— 一份 38 元素的 `IReadOnlyList<Type>`，由 `[GenerateSubtypes]` 源代码生成器在编译期填充。每个类型必须有公开无参 ctor
2. **运行期 mod 扩展点**：`ReflectionHelper.GetSubtypesInMods<AbstractConsoleCmd>()` —— **mod 可以加自己的 console 命令**

每个候选类 `Activator.CreateInstance` 实例化，过 `DebugOnly` 过滤后 `_commands[CmdName] = cmd`。命令名小写匹配，重复后注册的覆盖前一个。

**对 STS2-Gym 的意义**：我们想加 `serialize` / `step` 这类命令时，**只要在 mod 里继承 `AbstractConsoleCmd` 就会自动注册**。但其实没必要——直接走 HTTP server 调 `*Cmd` API 更干净。

### 1.2 命令参数解析（问 2）

手写、非常简陋：
- `inputBuffer.Trim().Split(' ')` 切分
- 每个命令在 `Process(Player?, string[] args)` 内自己解析（`int.TryParse` / `Enum.TryParse` / 小写比对 / `ModelDb.AllX.FirstOrDefault(...)`）
- 没有结构化 arg parser library
- Tab completion 通过 `GetArgumentCompletions(...)` 提供候选

含义：要从代码调用一个 console 命令，**不需要走文本解析层**——直接 `new XxxConsoleCmd().Process(player, new[]{"ARG1", "ARG2"})` 即可。

### 1.3 console 命令调用的底层 API（问 3，核心）

**最重要的发现**：38 个 console 命令几乎全部是**薄包装**，调用 `MegaCrit.Sts2.Core.Commands/` 下的静态 `*Cmd` 类。

`MegaCrit.Sts2.Core.Commands/` 包含 19 个 cmd 类：
```
CardCmd, CardPileCmd, CardSelectCmd, Cmd, CreatureCmd, DamageCmd,
ForgeCmd, MapCmd, OrbCmd, OstyCmd, PlayerCmd, PotionCmd, PowerCmd,
RelicCmd, RelicSelectCmd, RewardsCmd, SfxCmd, TalkCmd, ThinkCmd, VfxCmd
```

这些类的方法签名大致是 `public static async Task DoX(...)`，返回 Task 供 caller `await`。**这才是真正的 ActionDispatcher 底层 API**。

**结论**：
- **dev plan §2.2 ScenarioInjector 应当绕过 DevConsole，直接调 `*Cmd` 静态方法**
- **dev plan §2.3 ActionDispatcher 同样直接调 `*Cmd` 静态方法**
- DevConsole 命令是"调用示例 + 参数解析模板"，不是中间层

### 1.4 `FightConsoleCmd` 的能力边界（问 4）

- 只接受**一个参数**：encounter id（如 `CULTIST_1`）
- 调用 `ModelDb.GetById<EncounterModel>(id).ToMutable()` + `encounterModel.DebugRandomizeRng()` + `RunManager.Instance.EnterRoomDebug(RoomType.Monster, MapPointType.Unassigned, encounterModel)`
- **不支持 deck override / hp override / relic override**
- 要求 `RunManager.Instance.IsInProgress` —— **必须先有一个 run**，不能在主菜单调用
- 可以在任意时刻调用（不限制必须在 map 上）

**dev plan §2.2 Combat-level injection 的工作分解**（修正原假设）：

```
ScenarioInjector.InjectCombat(spec):
  1. RunManager.Instance.SetUpNewSinglePlayer(constructed_RunState, ...)
     # 构造一个带目标 (character, deck, relics, potions, hp, max_hp) 的 RunState
  2. RunManager.Instance.EnterRoomDebug(RoomType.Monster, Unassigned, encounter)
     # 进入战斗
  3. 微调（按需）：
     - PlayerCmd.GainEnergy / SetEnergy
     - CardPileCmd.Add(specific_card, PileType.Hand) for each starting_hand card
     - PowerCmd.Apply for each pre-applied buff
     - CreatureCmd.GainBlock for starting block
```

或者更激进的 **CombatState 直接注入**：
```
CombatManager.Instance.SetUpCombat(custom_CombatState_constructed_outside)
```
— 已确认 `SetUpCombat(CombatState)` 是 public。但 CombatState 构造的复杂度未量化，**留给任务 E 评估**。

### 1.5 `DumpConsoleCmd` 详细分析（问 5）

⚠️ **brief 假设错误**。

```
public override CmdResult Process(Player? issuingPlayer, string[] args)
{
    Log.Info(ModelIdSerializationCache.Dump());
    return new CmdResult(success: true, "Model ID database dumped to console & logs");
}
```

`DumpConsoleCmd` 输出的是 **`ModelIdSerializationCache.Dump()`** —— 即"model id ↔ 整型 short-id"的字典表（用于 multiplayer packet 序列化压缩），**不是 RunState/CombatState 的内容**。

**含义**：
- ❌ `DumpConsoleCmd` 不是 dev plan §2.1 Serializer 的现成实现
- ✅ 但 `ModelIdSerializationCache` 这个类对我们**仍有用**：它给出了所有 model id 的稳定整型编码，对 RL tensor encoding 直接可用作"card vocabulary"

Serializer 的真正答案应当来自 `SerializableRun`（出现在 `ModManager.OnMetricsUpload` 签名里）+ `CombatStateSynchronizer`（多人同步用的状态序列化）。这两条**留给任务 E 重点查**。

### 1.6 能否从代码直接调（问 6）

✅ **可以，两条路径**：

| 路径 | 调用 | 优缺点 |
|---|---|---|
| **直接实例化** | `var r = new FightConsoleCmd().Process(player, new[]{"CULTIST_1"}); await r.task;` | 干净、无需 DevConsole 实例、绕过 DebugOnly 注册门 |
| **走 DevConsole 实例** | 需先获取 DevConsole 单例（位置未查），`devConsole.ProcessCommand("fight CULTIST_1")` | 多一层文本解析，无意义 |

`AbstractConsoleCmd.Process` 是 `public abstract`，签名公开，没有 protection。

**但请直接调 `*Cmd` 静态方法**：例如不要 `new FightConsoleCmd().Process(player, ["CULTIST_1"])`，而是 `RunManager.Instance.EnterRoomDebug(RoomType.Monster, Unassigned, encounterModel)`。少一层包装、错误信息直接、不依赖 `IsInProgress` guard（如果你已经验证过）。

---

## 2. ScenarioInjector 工作量映射表

> 列出每个对 ScenarioInjector 有用的 console 命令、关键参数、它内部调用的底层 API、ScenarioInjector 应该直接调什么。

| ConsoleCmd | 参数 | 底层调用 | ScenarioInjector 应直接调 |
|---|---|---|---|
| `fight <enc_id>` | encounter id（screaming snake） | `RunManager.Instance.EnterRoomDebug(Monster, Unassigned, EncounterModel)` | 同左 |
| `room <RoomType>` | `RoomType` enum（Monster/Event/Shop/Rest/Treasure/Boss/...） | `RunManager.Instance.EnterRoomDebug(roomType)` | 同左 |
| `event <id>` | event id | `RunManager.Instance.EnterRoom(new EventRoom(model))` + `RunState.AppendToMapPointHistory(...)` | 同左 |
| `act <int|str>` | act 索引或 id | `RunState.SetActDebug(actModel) + actModel.GenerateRooms(rng, unlock, isMP) + RunManager.Instance.EnterAct(idx)` | 同左 |
| `card <id> [pile]` | card id, `PileType` (Hand/Deck/Discard/Draw/Exhaust) | `ICardScope.CreateCard(CardModel, Player) → CardPileCmd.Add(card, pile)` | 同左。`ICardScope` 由 `RunManager.DebugOnlyGetState()` 或 `CombatManager.DebugOnlyGetState()` 提供（取决于是 combat 还是 run-scope pile） |
| `remove_card <id> [pile]` | card id, Hand/Deck | `CardPileCmd.RemoveFromCombat(card)` / `CardPileCmd.RemoveFromDeck(card)` | 同左 |
| `energy <amount>` | int | `PlayerCmd.GainEnergy(amount, player)` | 同左 |
| `heal <amount> [idx]` | int, target index | `CreatureCmd.Heal(creature, amount)` | 同左 |
| `damage <amount> [idx]` | int, idx | `CreatureCmd.Damage(ctx, creatures, amount, ...)` + `CombatManager.CheckWinCondition()` | 同左 |
| `block <amount> [idx]` | int, idx | `CreatureCmd.GainBlock(target, BlockVar, ...)` | 同左 |
| `power <id> <amount> <idx>` | power id, int, int | `PowerCmd.Apply(power, creature, amount, ...)` 或 `PowerCmd.ModifyAmount(...)` | 同左 |
| `potion <id>` | potion id | `PotionCmd.TryToProcure(potion, player)` | 同左 |
| `relic [add|remove] <id>` | relic id | `RelicCmd.Obtain(relic, player)` / `RelicCmd.Remove(relic)` | 同左 |
| `draw <n>` | int | `CardPileCmd.Draw(ctx, count, player)`（需 `HookPlayerChoiceContext`） | 同左 |
| `kill [idx|all]` | idx 或 "all" | `CreatureCmd.Kill(c)` + `CombatManager.CheckWinCondition()` | 同左 |
| `win` | — | 同 kill all | 同左 |
| `gold <n>` | int | — 未读但模式相同 | 推测 `PlayerCmd.GainGold(...)` |
| `instant` | — | **`SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant`** | 直接赋值（见 §3） |
| `dump` | — | `ModelIdSerializationCache.Dump()` 仅打印 id 表 | 不需要 |
| `godmode` / `die` / `unlock` / `achievement` / `cloud` / `leaderboard` / `multiplayer` / `trailer` / `art` / `log` / `getlogs` / `stars` / `sentry` / `ancient` / `afflict` / `enchant` / `upgrade_card` / `open` / `travel` | — | 未细读，但模式一致 | 用到时再查 |

### 注意事项

1. **`ICardScope`** 是 RunState 和 CombatState 都实现的接口（看 CardConsoleCmd 的 cast）。把 RunState 当 `ICardScope` 时 `CreateCard` 把 card 关联到 run-level scope（deck），CombatState 当 scope 时关联到 combat scope（hand/discard 等）。**ScenarioInjector 构造起手牌时要选对 scope**。
2. **`HookPlayerChoiceContext`** / **`BlockingPlayerChoiceContext`** 是 player choice 触发的上下文对象，部分 `*Cmd` 方法（draw / damage 等）要求传入。ScenarioInjector 注入"假"动作时需要构造这些 ctx —— 不复杂，DrawConsoleCmd 的实现就是抄写模板
3. **`*Cmd` 方法是 async**，返回 `Task`。同步语义（dev plan §2.3 关键不变量）就是"await 这些 Task 完成"。这等于游戏自带 step 同步原语，**dev plan §2.3 的工作主要是组合，不需要发明新机制**
4. **`IsNetworked => true`** 的命令在 multiplayer 中走 `ActionQueueSynchronizer`。我们只跑 single-player 时无影响，但 ScenarioInjector 不应该在 `IsSinglePlayerOrFakeMultiplayer == false` 时跑。建议 mod 启动时硬性 assert single-player

---

## 3. 副发现：FastMode 已经游戏自带 🟢

`InstantConsoleCmd` 揭示一个**重大事实**：游戏自带 `FastModeType { None, Normal, Fast, Instant }` 枚举。62 处代码引用，主要影响动画/tween 时长（map drawing、卡牌移动、伤害数字等）。`Instant` 模式下许多动画被完全跳过或时长趋零。

切换方式：
```csharp
SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant;
```

**对 dev plan §2.4 FastMode 的修正**：

| 原假设 | 实情 |
|---|---|
| "Harmony patch 短路所有纯视觉 await 调用" | 游戏已经在 62 处代码点检查 FastMode 选择短时长。只需把开关拉到 Instant |
| "禁用粒子、音频" | 不确定 Instant 是否禁；需 task C 的 AutoSlay 一起核实 |
| "保留逻辑 await" | 游戏本身的 await 在 Instant 模式下仍执行（只是时长 0），这正是我们想要的 |

§2.4 FastMode 工作量很可能从"中等工作量"降到"几乎免费 + 少量 Harmony 补刀"。**SUMMARY.md 里会重新评级**。

---

## 4. 结论：ScenarioInjector 实现路径

**ScenarioInjector 是基于"DevConsole 内部 API"的包装，不是基于 DevConsole 命令的包装**。具体：

| 候选路径 | 评价 |
|---|---|
| (a) wrapper around DevConsole 文本命令 (`ProcessCommand("fight CULTIST")`) | ❌ 多一层文本解析、丢失类型安全、IsNetworked 路由问题 |
| (b) wrapper around `new XxxConsoleCmd().Process(player, args)` | ⚠️ 可工作但无意义——同样的代码不走 ConsoleCmd 类直接调 `*Cmd` 更短 |
| (c) **直接调 `MegaCrit.Sts2.Core.Commands/*Cmd` 静态方法 + `RunManager`/`CombatManager` 公开方法** | ✅ **推荐**。这是 ConsoleCmd 自己干的事 |
| (d) 构造 CombatState 直接 `CombatManager.Instance.SetUpCombat(state)` | 🤔 **最激进、最快、但 CombatState 构造复杂度待 task E 评估** |

推荐 **(c) + 在 P1 阶段评估 (d) 作为加速**。

---

## 5. 不确定项 / hand-off

1. **`SetUpCombat(CombatState)` 直接注入路径**：CombatState 公开构造需多少代码？是不是有 builder？**任务 E 必查**
2. **`RunManager.SetUpNewSinglePlayer(RunState, ...)` 的 RunState 怎么构造**：dev plan ScenarioInjector P0 需要"指定 character/deck/relics"，要看 RunState 是否有 builder / 是否可以 partial init。**任务 E 必查**
3. **`FastModeType.Instant` 实际跳了哪些 await**：62 处不全是 visual，可能有逻辑动画。需要 task C AutoSlay 时一起观察（如果 AutoSlay 用 Instant 跑得很快，证据足够；否则要 Harmony 补刀）
4. **`IsSinglePlayerOrFakeMultiplayer` 标志的来源**：mod 启动后想强制单人模式，要找一个 setter 或对应的"单人模式 init"路径。不紧迫但要记一笔
5. **dev plan ScenarioInjector "Hand / Energy / HP / Buffs 微调" 触发了同步语义需要 caller `await`**：`*Cmd.DoX(...)` 都返回 Task。ScenarioInjector 接收 spec → 串行 await 各 `*Cmd` → 整个注入完成才解锁，对 step 同步语义友好

---

## 6. 给后续任务的备忘

- **任务 C（AutoSlay）**：核实 AutoSlay 是否就是把 `FastModeType.Instant` + 一组 `IRoomHandler` policy 拼起来跑的——如果是，那就给我们一份"游戏官方版 step 驱动"的参考实现
- **任务 E（Serialization）**：必须查 `SerializableRun` + `CombatStateSynchronizer` + `RunState` / `CombatState` 公开构造路径
- **任务 D（RNG）**：DevConsole 看到 `EncounterModel.DebugRandomizeRng()`、`RunState.Rng.UpFront`、`RunState.Rng.Shuffle`、`State.Rng` 这些字段访问点——RNG 是分流多个的，任务 D 应当列全
