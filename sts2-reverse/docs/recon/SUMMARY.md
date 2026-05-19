# SUMMARY：STS2-Gym 反编译侦察总结

> 5 个调查任务（A-E）完成。本文档回答 brief §6 的四个综合问题：组件工作量重新评估、dev plan 假设修正、最小 mod 骨架、7 天行动清单。

---

## 1. dev plan §2 各组件的真实工作量评估

四档分级：**几乎免费（直接 reuse）/ 轻量包装 / 中等工作量 / 重写**。

| dev plan 组件 | 原假设 | 侦察后等级 | 关键依据 |
|---|---|---|---|
| **§2.1 Serializer**（整 run 部分） | 中等 | ⭐ **几乎免费** | `RunManager.ToSave()` / `RunState.FromSerializable()` 现成 public；含 RNG state；schemaVersion 现成 |
| **§2.1 Serializer**（mid-combat 部分） | 中等 | ⭐⭐ **中等** | 需写 4 个 dataclass（CombatState/Creature/PlayerCombatState/Power），但字段全部公开可读，无神秘黑盒 |
| **§2.2 ScenarioInjector**（Combat-level P0） | 重 | ⭐⭐ **中等** | 三条路径：(1) 走 `SerializableRun` 加载 + `EnterRoomDebug`，(2) 直接构造 CombatState + `SetUpCombat`，(3) 串行 `*Cmd` 微调。Path (1)+(3) 最稳，Path (2) 最激进 |
| **§2.2 ScenarioInjector**（Floor/Run-level P1/P2） | 重 | ⭐⭐ **中等** | `RunManager.SetUpNewSinglePlayer/SetUpTest` 已有公开 ctor 路径，主要工作是 ScenarioSpec → RunState 字段映射 |
| **§2.3 ActionDispatcher**（核心 step API） | 重 | ⭐⭐ **中等** | **不是写 dispatcher**，是组合现成 19 个 `*Cmd` 静态方法 + 设计干净的 hook 接口（参照 `ICardSelector` 模式扩展到 end-turn/target/map）。async/await 链就是 step 同步语义 |
| **§2.4 FastMode** | 中等 | ⭐ **几乎免费** | 🟢 游戏自带 `FastModeType.Instant`，62 处代码点已 honoring。一行 `SaveManager.Instance.PrefsSave.FastMode = Instant`。仅 P1 需要实测验证 Instant 与逻辑无副作用 |
| **§2.5 RngController** | 重 | ⭐ **几乎免费** | `RunRngSet` 集中 12 流 + `PlayerRngSet` 补 3 流。44 个 raw 命中点中 40 个受控、3-4 个仅 VFX/leaderboard、1 个 debug-only outlier（`EncounterModel.DebugRandomizeRng`）。**只需接管 master seed 入口** |
| **§2.6 Transport (HTTP)** | 中等 | ⭐⭐ **中等** | 与游戏无关，正常工程任务。从 mod 入口起 Kestrel/HttpListener |
| **§2.7 实例生命周期** | 中等 | ⭐⭐ **中等** | 与游戏无关，按 instance_id 命名 socket/lockfile，PID watch |
| **§2.8 HumanRenderer**（text + json） | 中等 | ⭐⭐ **中等** | RunState/CombatState 字段都可读，`ModelDb` 提供卡牌/敌人/relic 的本地化字符串。两份 view 共享 RawState |
| Mod 框架（patcher 等） | 中等 | ⭐ **几乎免费** | 官方 `ModInitializerAttribute` + `Harmony.PatchAll` 自动，无需第三方 framework |
| **Trajectory dump（dev plan §8.2）** | 未估 | ⭐ **几乎免费** | 🟢 `CombatHistory.Entries` 现成的事件日志（CardPlayStarted/Finished、DamageReceived、MonsterPerformedMove 等），D4RL 风格 dataset 几乎现成 |
| **Action mask 计算** | 未估 | ⭐ **几乎免费** | `card.CanPlay(out UnplayableReason, out AbstractModel)` 现成，`CombatState.HittableEnemies` 现成 target 列表 |

### 综合工作量再分布

| 等级 | 组件占比 | 含义 |
|---|---|---|
| 几乎免费 | ~40% | mod 框架、FastMode、RNG、整 run Serializer、CombatHistory dump、action mask |
| 轻量包装 | ~15% | RunState load/save 重组、ICardSelector 注入、phase resolver |
| 中等工作量 | ~45% | ScenarioInjector、ActionDispatcher hook 接口设计、mid-combat Serializer、HumanRenderer、HTTP/IPC、ScenarioSpec→RunState 映射 |
| 重写 | 0% | **没有需要 ground-up 重写的组件** |

**对项目可行性的结论**：dev plan 假设的"重型 mod 工程"严重低估了 Mega Crit 已经提供的基础设施。**整体工作量大约比原估算少 30-40%**，而且 mod 侧 API 表面比假设的干净——官方留了 mod 入口、testing hook、RNG 集中管理、save/load 公开方法。**这个项目从工程可行性看完全成立**。

---

## 2. dev plan 假设需要修正的清单

按破坏性排序。SUMMARY 之后用户可考虑直接 patch `STS2_GYM_DEV_PLAN.md`。

### 2.1 重大修正（影响架构）

#### M1. ❌ **DumpConsoleCmd 不是 Serializer**

- **原假设**（brief §5 任务 B）：DumpConsoleCmd 可能是 dev plan §2.1 Serializer 的现成实现
- **实情**：它只 dump `ModelIdSerializationCache`（model id ↔ short id 字典），不 dump 任何 RunState/CombatState
- **修正**：删掉这条假设。正确路径是 `RunManager.ToSave()` (任务 E)

#### M2. ✅ **`FastModeType.Instant` 已经存在**

- **原假设**（dev plan §2.4）："Harmony patch 短路所有纯视觉 await 调用"
- **实情**：游戏自带 `FastModeType { None, Normal, Fast, Instant }`，62 处代码点 honor 该 flag，`InstantConsoleCmd` 一行切换 `SaveManager.Instance.PrefsSave.FastMode = Instant`
- **修正**：§2.4 改为"切换 FastMode + 实测吞吐 + 不足时 Harmony 补刀"。不要假装从零写。**警告**：AutoSlay 在自动测试中用的是 `Fast` 不是 `Instant`——可能 Instant 有未发现 corner case。**P1 实测必查**

#### M3. ✅ **RunRngSet 已经集中管理 95% 的 RNG**

- **原假设**（dev plan §2.5）："维护一份显式的 RNG 调用点审计清单（grep `Random` / `Randf` / `Randi` / `Shuffle` / `Choose`），逐个 patch"
- **实情**：`RunRngSet` 12 流 + `PlayerRngSet` 3 流 + 11 处 derived seed 全部从 master 派生。`docs/rng_audit_raw.txt` 的 44 个命中点中只有 0 个 wild RNG 影响游戏逻辑
- **修正**：§2.5 改为"在 RunState 构造时传入用户 seed + 接管 `NGame.DebugSeedOverride` + Harmony postfix `EncounterModel.DebugRandomizeRng` 或直接绕过 FightConsoleCmd 路径"。删除"几十个 hook 点逐个 patch"假设

#### M4. ⚠️ **AutoSlay 不是 ActionDispatcher 的现成实现**

- **原假设**（brief 任务 C 隐含）：AutoSlay 可能直接作为 step API 改造
- **实情**：AutoSlay 是端到端 UI 测试，吞吐 0.8-1.3 step/s（dev plan 目标 50 step/s 慢 40-60 倍），通过 Godot 节点路径点 UI 按钮
- **修正**：AutoSlay 的**借鉴价值**比直接复用价值大：
  - 借鉴：`ICardSelector` 模式 + step 原语清单（`CardCmd.AutoPlay` / `PlayerCmd.EndTurn` / `potion.EnqueueManualUse`）
  - 不借鉴：UiHelper.Click + WaitHelper.Until polling

#### M5. ✅ **`SerializableRun` 已经是完美的 between-rooms 序列化**

- **原假设**（dev plan §2.1）：自己设计版本化 JSON schema、确保含 RNG
- **实情**：`SerializableRun` 已有 18 个 top-level 字段、`SchemaVersion` 字段、`SerializableRunRngSet` 完整 RNG state、`IPacketSerializable` 二进制格式 + `[JsonPropertyName]` JSON 格式双轨、`Anonymized()` 隐私清洗
- **修正**：§2.1 改为"reuse `SerializableRun` + 写一个补充的 `SerializableCombatState` 处理 mid-combat"

#### M6. ⚠️ **`RunManager.State` 是 private，但 `DebugOnlyGetState()` 是 public**

- **原假设**（dev plan 隐含）：可能要 Harmony reverse-patch 拿 RunState
- **实情**：`RunManager.Instance.DebugOnlyGetState()` 和 `CombatManager.Instance.DebugOnlyGetState()` 都是 public，可直接调。"DebugOnly" 是命名警示，不是 build-time 剔除（DevConsole 自己就在调用）
- **修正**：不需要 reverse-patch，直接调

### 2.2 中等修正（影响实施）

#### M7. ✅ **Mod 系统是官方完备的**

- **原假设**（dev plan 隐含）："基于 Harmony patch 自己搭"
- **实情**：`[ModInitializer]` attribute + auto `Harmony.PatchAll` + 目录 + JSON manifest + 依赖图 + 启用/禁用 UI + log 系统全部由 Mega Crit 官方提供
- **修正**：mod 实现细节章节明确说"用官方 mod 系统，不引入第三方 modloader"

#### M8. ⚠️ **mod 入口跑得极早**

- **新发现**：`ModInitializer` 在 `OneTimeInitialization.ExecuteVeryEarly()` 阶段触发，此时 `ModelDb` / `LocManager` / Godot scene 都**未**初始化
- **修正**：dev plan 实施方案应明确"Init 里只挂事件订阅 + Harmony patch，真正工作 defer 到 `RunManager.RunStarted` / `CombatManager.CombatSetUp` 事件"

#### M9. ⚠️ **`PlayerAgreedToModLoading` 是 UX 门**

- **新发现**：首次使用 mod 必须用户在 UI 点同意，否则所有 mod `ModLoadState.Disabled`
- **修正**：Docker 镜像 / pip 自动安装流程要预先写一份 `SettingsSave.ModSettings.PlayerAgreedToModLoading = true`

#### M10. ✅ **`ICardSelector` 是官方 testing hook，扩展它即可**

- **新发现**：`MegaCrit.Sts2.Core.TestSupport.ICardSelector` + `CardSelectCmd.UseSelector(...) / PushSelector(...)` 是官方留的 policy 注入点，AutoSlay 已经在用
- **修正**：ActionDispatcher 章节明确"扩展 `ICardSelector` 模式到 end-turn/target/map-direction/event-choice/shop-buy/potion-use 等所有决策点"

### 2.3 小修正（细节）

| 编号 | 内容 |
|---|---|
| m11 | `FightConsoleCmd` 仅接 encounter id，**不支持 deck override**——ScenarioInjector 组合多步实现，或走 CombatState 直接注入 |
| m12 | 命令行 flag `nomods` 跳过 mod 加载——调试 vanilla 行为有用 |
| m13 | `AutoSlayer.IsActive` 标志机制可以借鉴——mod 启用时关掉 FTUE/UI prompt |
| m14 | `CombatHistory` 是 free trajectory log——D4RL 数据集（dev plan §8.2）几乎免费 |
| m15 | Mid-combat 状态游戏自身无 save——这是 dev plan §2.1 的唯一真实"自己写"部分 |
| m16 | `ModHelper.AddModelToPool<...>()` + `SubscribeForCombatStateHooks(...)` 是给 mod 加自定义 content 用的——我们不加 content，**不需要这些**，但 mod 系统验证时可作"hello world" target |

---

## 3. 第一个最小 mod 应该长什么样

### 3.1 文件布局（绝对路径示例）

```
<STS2_install>/mods/sts2gym/
├── sts2gym.json          # manifest，文件名可任意，惯用同 id 一致
└── sts2gym.dll           # assembly，**文件名强制** = manifest.id + ".dll"
```

`sts2gym.json`:

```json
{
  "id": "sts2gym",
  "name": "STS2-Gym Bridge",
  "author": "<you>",
  "version": "0.0.1",
  "description": "RL/LLM environment bridge — pre-MVP smoke test",
  "has_dll": true,
  "has_pck": false,
  "dependencies": [],
  "affects_gameplay": true
}
```

### 3.2 sts2gym.dll 内容（C# 伪代码）

> 不是可编译代码，是骨架草图。法律红线：不引用反编译方法体。

```csharp
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym;

[ModInitializer(nameof(Init))]
public static class Sts2GymMod
{
    static void Init()
    {
        Log.Info("[sts2gym] mod init — game models NOT YET available");

        // 1) 在 ModelDb / Godot 都未就绪时只能挂事件 / Harmony
        RunManager.Instance.RunStarted   += OnRunStarted;
        CombatManager.Instance.CombatSetUp += OnCombatSetUp;
        CombatManager.Instance.TurnStarted += OnTurnStarted;
        CombatManager.Instance.TurnEnded   += OnTurnEnded;

        // 2) 强制 instant mode 验证 FastMode 假设
        //    （需要 PlayerAgreedToModLoading=true 才会跑到这里）
        SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant;

        Log.Info("[sts2gym] mod init done — handlers registered");
    }

    static void OnRunStarted(RunState run)
    {
        Log.Info("[sts2gym] run started");

        // 3) 测试 Serializer reuse：拿 SerializableRun
        var save = RunManager.Instance.ToSave(preFinishedRoom: null);
        Log.Info($"[sts2gym] SerializableRun snapshot: schema={save.SchemaVersion}, " +
                 $"players={save.Players.Count}, rng_streams={save.SerializableRng.Counters.Count}");
    }

    static void OnCombatSetUp(CombatState s)
    {
        Log.Info($"[sts2gym] combat setup: encounter={s.Encounter?.Id.Entry}, " +
                 $"enemies={s.Enemies.Count}");
    }

    static void OnTurnStarted(CombatState s)
    {
        Log.Info($"[sts2gym] turn {s.RoundNumber} started");
        // 占位：未来此处触发 step → 等 Python 端 action
    }

    static void OnTurnEnded(CombatState s)
    {
        // 占位：未来此处发送 obs 到 Python
    }
}
```

### 3.3 验证 pipeline 通畅的最小动作

按顺序执行，每一步看 log（`<STS2_install>/logs/` 或 console）：

| 步骤 | 验证点 |
|---|---|
| 1. 启动游戏 | 出现 `Loaded 1 mods (1 total)` 和 `[sts2gym] mod init done`——说明 manifest 解析、DLL 加载、initializer 调用全 OK |
| 2. 主菜单 → 开新 run | 出现 `[sts2gym] run started` 和 `SerializableRun snapshot: ...`——说明事件订阅 + Save 公开 API 都通畅 |
| 3. 进第一场战斗 | 出现 `[sts2gym] combat setup: encounter=..., enemies=N`——说明 `CombatManager.CombatSetUp` 触发、`CombatState.Encounter` 可访问 |
| 4. 看战斗动画速度 | Instant 模式下卡牌动画基本秒过、伤害数字立即结算——说明 FastMode 切换生效 |
| 5. 回合开始 | 出现 `[sts2gym] turn 1 started`——说明 TurnStarted 事件订阅 OK |

任一步失败就**立即定位**：
- 步骤 1 失败 → manifest JSON 格式或 DLL 路径问题
- 步骤 2 失败 → mod 已加载但事件订阅时机错——重读任务 A 的"加载时机"章节
- 步骤 3 失败 → 单例访问问题或 mod 没启用（`PlayerAgreedToModLoading` 未点同意）
- 步骤 4 失败 → FastMode 未真正切换（可能在 Init 中调用太早）
- 步骤 5 失败 → 战斗启动有自定义路径未走 CombatSetUp 事件

### 3.4 这个最小 mod 验证了 dev plan 的几个核心假设

| 假设 | 验证方式 |
|---|---|
| mod 系统可工作 | 步骤 1 |
| Init 后能挂事件订阅 | 步骤 2、3、5 |
| `RunManager.ToSave()` public 可用 | 步骤 2 |
| `CombatManager.DebugOnlyGetState()` / events 可用 | 步骤 3、5 |
| `FastModeType.Instant` 安全 | 步骤 4 |

---

## 4. 接下来 7 天的具体动作清单

每条 1-3 小时颗粒度。任务在前后均依赖时按顺序，可并行的标 **‖**。

### Day 1 — 工具链 + minimal mod compile

| # | 时长 | 任务 |
|---|---|---|
| 1.1 | 1h | 装 .NET SDK 8.0+（dotnet --version 检查） |
| 1.2 | 1h | 装 Slay the Spire 2（Steam）+ 找到 `<install>/mods/` 目录 |
| 1.3 | 1h | 把 `sts2.dll` 和 `0Harmony.dll` 拷出来作为 assembly reference（`<workspace>/sts2-reverse/` 已经有） |
| 1.4 | 2h | 起一个 `Sts2Gym.Mod` 项目（`<class lib>` SDK）：csproj 设 TargetFramework=net8.0、nullable enable、unsafe true，把上面 2 个 dll 加为 `<Reference>` private=true、`<Reference HintPath="...">` |
| 1.5 | 1h | 写 §3.2 的 hello-world `Sts2GymMod` 代码，编译输出 `sts2gym.dll`，加 `sts2gym.json` manifest |

**Day 1 ✅ 验收**：`dotnet build` 出 `sts2gym.dll`，文件大小 < 50KB

### Day 2 — Mod 加载验证 + FastMode 实测

| # | 时长 | 任务 |
|---|---|---|
| 2.1 | 0.5h | 把 `sts2gym/` 目录拷到游戏 `mods/` 下 |
| 2.2 | 0.5h | 启动游戏，点 Mods UI → 同意 `PlayerAgreedToModLoading` → 重启 |
| 2.3 | 1h | 查 log 看是否出现 `Loaded 1 mods` + `[sts2gym] mod init done`——验证步骤 1 |
| 2.4 | 1h | 进新 run，过主菜单角色选择，看 `RunStarted` 事件——验证步骤 2 |
| 2.5 | 1h | 进第一场战斗，看 `CombatSetUp`、`TurnStarted`——验证步骤 3、5 |
| 2.6 | 2h | **关键实测**：FastMode=Instant 跑 5 场战斗，对比 FastMode=Fast、Normal 跑同 seed 战斗轨迹：(a) 动画时长目测、(b) 用 CombatHistory 比对事件序列是否 bit-exact 一致。这决定 dev plan §2.4 的工作量定级 |
| 2.7 | 1h | 把"Instant 是否 bit-exact 等价于 Normal"的结论写入 `docs/recon/F_fastmode_impl_test.md`（不在 brief 任务里，但 SUMMARY 里要重新确认 M2 修正） |

**Day 2 ✅ 验收**：mod 跑起来；FastMode 实测结论落地

### Day 3 — Transport + 第一个 read-only HTTP 端点 ‖

| # | 时长 | 任务 |
|---|---|---|
| 3.1 | 2h | mod 内启一个 Kestrel / `HttpListener` 服务（端口从 env 读，**不硬编码**），fail-fast 端口冲突 |
| 3.2 | 2h | 实现 `GET /observe`：调 `RunManager.Instance.ToSave(null)` 序列化为 JSON 返回 |
| 3.3 | 1h | Python 端 sanity：`curl http://localhost:7777/observe` 拿到一份 JSON，用 `json.loads` 解析，断言 `schema_version` / `players` / `rng` 三字段存在 |
| 3.4 | 1h | 处理"未在 run 中"的 case：返回 `{"phase": "main_menu"}` |

**Day 3 ✅ 验收**：Python 端能拉到 between-rooms run state JSON

### Day 4 — Phase resolver + Combat snapshot

| # | 时长 | 任务 |
|---|---|---|
| 4.1 | 1.5h | 实现 phase resolver：用 `RunManager.IsInProgress` / `CombatManager.IsInProgress` / `RunState.CurrentRoom.RoomType` / `NOverlayStack.Peek()` 类型判 phase。返回 `main_menu / map / combat / event / shop / rest / treasure / reward / game_over` 之一 |
| 4.2 | 3h | 写 `SerializableCombatState` 4 个 dataclass（CombatState / Creature / PlayerCombatState / Power），实现 `From(CombatState)` 字段映射 |
| 4.3 | 1h | 扩展 `/observe` 端点：phase=combat 时把 `SerializableCombatState` 嵌入返回 JSON |
| 4.4 | 1.5h | 实现 P0 PartialObs filter：mask RNG counters + draw pile 顺序——其它字段先全开 |

**Day 4 ✅ 验收**：战斗中调 `/observe` 返回 full combat state（hand + draw count + enemies + powers + intents），PartialObs 模式下 draw pile 内容隐藏

### Day 5 — Step 原语 + 第一个 action 端点

| # | 时长 | 任务 |
|---|---|---|
| 5.1 | 1h | 实现 `GET /action_mask`：在 combat phase 下，遍历 hand 调 `card.CanPlay(out _, out _)`，列出可玩牌 + 各自合法 target list（来自 `CombatState.HittableEnemies` 等） |
| 5.2 | 2.5h | 实现 `POST /step` accept `{"type": "play_card", "card_idx": int, "target_combat_id": uint?}`，内部调 `await CardCmd.AutoPlay(new BlockingPlayerChoiceContext(), card, target)`。**同步语义**：等 Task 完成再返回，并加 `combat.PlayerActionsDisabledChanged` / `TurnEnded` 事件双重确认 |
| 5.3 | 1.5h | 实现 `POST /step` 处理 `{"type": "end_turn"}` → `PlayerCmd.EndTurn(player, canBackOut: false)` |
| 5.4 | 1h | Python 端写一个 hand-coded random policy：循环 `/observe` → `/action_mask` → 随机选合法动作 → `/step`，跑通一整场战斗 |

**Day 5 ✅ 验收**：random policy 能在战斗里打完牌、结束回合、跑完一场 combat 不崩

### Day 6 — Determinism + ScenarioInjector P0 ‖

| # | 时长 | 任务 |
|---|---|---|
| 6.1 | 1.5h | 实现 `POST /reset` 接受 `{"seed": str, "scenario": ScenarioSpec?}`。Spec=null 时走默认随机 run，否则走 ScenarioSpec 路径。先只支持 spec=null + seed |
| 6.2 | 3h | Determinism test：同 seed reset 两次，random policy 同一序列 action，断言 trajectory bit-exact 一致。这是 dev plan §2.5 验收 |
| 6.3 | 2h | ScenarioInjector P0 (Combat-level 最简版)：spec 指定 `character` + `encounter_id`，路径为 `RunManager.SetUpNewSinglePlayer(constructed_RunState_with_default_deck, ...)` + `RunManager.EnterRoomDebug(Monster, Unassigned, encounter)`。**先不支持 deck/hp override**——这一阶段 P0 只验路径通畅 |
| 6.4 | 1h | 把 §1 的工作量表更新到本文档（用真实 day-by-day 数据校正） |

**Day 6 ✅ 验收**：(a) determinism test pass；(b) 能从 Python 端 `POST /reset` 跳到指定 encounter 直接打

### Day 7 — Buffer day / 回路 / 报告

| # | 时长 | 任务 |
|---|---|---|
| 7.1 | 2h | 把 6 天累积的 surprise / 不确定项 / bug 整理成 `docs/recon/G_day7_findings.md`，包括：哪些 dev plan 假设进一步颠覆、哪些字段映射有坑、哪些 hook 出现 race |
| 7.2 | 2h | 写一份 `docs/setup.md` — 给重启游戏 / 重装 mod / 改端口的步骤化指南（自己 onboarding 文档） |
| 7.3 | 2h | 用户审阅 + 决策下周方向：是冲 ScenarioInjector P0 全功能（deck/hp 注入）？是冲 LLM action parser 起步？是冲 throughput benchmark？ |

**Day 7 ✅ 验收**：第 1 周收工，对项目可行性有定量判断（具体 step/s 数字）+ 完整 hello-world pipeline + 三份 recon docs 修订 + 下周路线选择

---

## 5. 第二周 + 之后的路线建议（超出 7 天，但作为锚点）

按 dev plan §11 优先级 + 本侦察修订：

- **Week 2**：throughput benchmark（实测 step/s，看 Instant + 直接 `*Cmd` 调用能否到 ≥50 step/s 目标），ScenarioInjector 完整功能（deck/hp/relic/potion/buff override），HumanRenderer text view 雏形
- **Week 3**：mid-combat Serializer 完整（4 个 dataclass 全字段），save/restore 端点 + 验证 round-trip bit-exact
- **Week 4**：Python `gymnasium.Env` 封装，第一个 RL baseline（random + MaskablePPO IroncladCombat）
- **Week 5-6**：LLM 端 parser + ChainOfThoughtWrapper + 第一个 LLM baseline（Claude/GPT 在 IroncladCombat 上跑评测）
- **Week 7-8**：双接口一致性 test、PartialObs filter 完整、文档 + README v0.1 发出去拿反馈

---

## 6. 关键不确定项汇总（按 P0 / P1 / 待研究分级）

整理自 5 个任务文档的"不确定项 / hand-off"章节，去重 + 重排。

### P0（开始写代码前必须实测）

1. `FastModeType.Instant` 是否 bit-exact 等价于 Normal — Day 2 实测
2. `RunManager.SetUpNewSinglePlayer(state, ...)` vs `NGame.Instance.DebugSeedOverride` 两条 seed 注入路径，哪条更可控 — Day 6 实测
3. `PowerModel.ToSerializable()` 是否存在 / 是否含 stack count — Day 4 实测
4. `MonsterModel.NextMove` 序列化时是否包含 random branch state（要序列化已 roll 结果） — Day 4 实测
5. `Card.ToSerializable()` 是否完整包含 enchant / Upgraded / modifier flags — Day 4 实测

### P1（影响功能但非阻塞）

6. `CombatManager.DebugOnlyGetState()` 是否始终返回当前 state（vs null） — 估计不会 null，但要写防御
7. `?` map 节点解析时机（PartialObs 是否要 mask） — 实测
8. 首遇敌人 max_hp 可见性（PartialObs filter） — 实测
9. `Encounter.MonstersGenerated` 在进战斗前/进战斗后的可见时机 — 实测
10. `CombatState` ctor 直接注入路径的 bit-exact 验证（mid-combat snapshot/restore） — 中后期

### 待研究（不阻塞 MVP）

11. `RunRngType.CombatOrbs / CombatPotionGeneration / UpFront` 具体触发场景
12. `Card.AfterCreated()` / `Card.AfterObtained()` / `Relic.AfterObtained()` 生命周期 hook（mod 注入新 model 时需要）
13. `MultiplayerScalingModel` 的影响（我们只跑 single-player，但 `RunState.MultiplayerScalingModel` 字段默认 init 了——可能影响 hp 缩放）
14. `Watchdog.Check()` 在 mod 高频调用时是否误触发

---

## 7. 一句话总结

**STS2-Gym 工程可行性极高，比 dev plan 估计的工作量少 30-40%**。原假设里的大头（mod patcher、RNG hook 清单、FastMode、Serializer）官方都已经做了 60-95%。**主要工作集中在三处**：(1) 写 mid-combat 序列化 4 个 dataclass，(2) 扩展 `ICardSelector` 模式到所有决策点形成干净的 ActionDispatcher hook 层，(3) HTTP/IPC transport + 进程管理 + Python `gymnasium.Env` 封装。**dev plan §11 优先级表里的 P0 milestone 一周内基本能跑通最小 demo**。
