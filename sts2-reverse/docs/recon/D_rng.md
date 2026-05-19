# 任务 D：RNG 控制盘点

> Scope：`Random/`（2 文件）+ `Runs/RunRngSet.cs` + `Entities/Rngs/`（2 enum） + `Odds/`（2 文件）+ 跨文件 grep 验证 `docs/rng_audit_raw.txt` 的 44 个原始命中点。
> 一句话结论：**RunRngSet + PlayerRngSet + 几个 derived-seed 子流构成了几乎完整的 RNG 集中管理。dev plan §2.5 工作量从"中等"降到"轻量包装"**。

---

## 1. 关键回答

### 1.1 RunRngSet 字段清单与用途（问 1）

**12 个 RNG 流**，由 `RunRngType` enum 定义（在 `MegaCrit.Sts2.Core.Entities.Rngs/RunRngType.cs`）：

| 流名 | 用途（推测/验证） |
|---|---|
| `UpFront` | 角色选择 / 起手 deck / 起始 relic 等 run 启动前 roll（推测） |
| `Shuffle` | **抽牌堆洗牌**。`CardPileCmd.Shuffle` 验证使用 |
| `UnknownMapPoint` | "?" map 节点解析（monster/elite/treasure/shop 转换） |
| `CombatCardGeneration` | 战斗中生成的临时卡（Wraith、Status 等） |
| `CombatPotionGeneration` | 战斗中生成的药（如 Alchemize 类） |
| `CombatCardSelection` | 战斗中"随机选 N 张牌"效果 |
| `CombatEnergyCosts` | 随机能耗效果（如 Spice / 某些 Ancient） |
| `CombatTargets` | 随机目标选择（如 Cleave 多目标分配） |
| `MonsterAi` | 敌人 intent 抽取 |
| `Niche` | 杂项 / one-off 效果（验证：CursedRun curse 选、Whetstone/WarHammer 升级选、MorphicGrove/Trial 变换、ToughEgg HP roll） |
| `CombatOrbs` | Defect 系 orb 通道随机性（推测） |
| `TreasureRoomRelics` | 宝箱房 relic 抽取 |

**额外 3 个 player-scope 子流**，由 `PlayerRngType` 定义（每个 Player 实例独立持有）：

| 流名 | 用途 |
|---|---|
| `Rewards` | 战斗后 card reward / relic reward 抽取（验证：NeowsBones） |
| `Shops` | 商店物品抽取 |
| `Transformations` | run 内的卡转换效果 |

每 Player 在 multiplayer 中独立——hp/rewards 等不混。

### 1.2 Seed 派生方式（问 2）

**单一 master seed 派生所有子流**。

- 用户给一个字符串 seed（如 `"hello"`）
- `RunRngSet ctor`：`Seed = (uint)StringHelper.GetDeterministicHashCode(seed)`
- 每个 `RunRngType` 子流：`new Rng(Seed + (uint)GetDeterministicHashCode(snake_case_type_name))`
  - 例：`CombatTargets` 子流 seed = `master + hash("combat_targets")`
- `PlayerRngSet ctor`：用 `(uint)(hashCode(seed) + Player.NetId)`，per-player offset
- 各子流之间数学上**独立**（hash 命名空间打散）

**deterministic from a single string** —— 这是我们想要的 ideal 形态。

### 1.3 持久化（问 3）

非常干净：

```
SerializableRunRngSet {
  Seed: string,
  Counters: { RunRngType → int }   # 每个子流的 NextX() 调用计数
}
```

`Rng` 类暴露 `FastForwardCounter(int)` —— save/load 时只存"已 advance 多少次"，restore 时用同 seed 重建 + fast-forward 到 counter。bit-exact 重现。

**这等价于 `Rng.state = (seed, counter)`**，是序列化"RNG state"的最干净形式。dev plan §2.1 "包含 RNG 内部状态" 几乎免费——拷 12+3 个 `(RunRngType, int)` 对就够。

### 1.4 "受控 RNG" vs "野生 RNG" 分类（问 4，核心）

下面以 `docs/rng_audit_raw.txt` 的 44 个命中点为基础，加上额外 grep 出来的命中点综合分类。

#### ✅ 受控 RNG（无需额外 hook）

**来自 `runState.Rng.*` 或 `player.PlayerRng.*` 的所有调用点**。这些就是 dev plan §2.5 不用再写 hook 的部分。代表：

| 命中点 | 流 |
|---|---|
| `CardPileCmd.cs:432` | `runState.Rng.Shuffle.NextInt(...)` (random pile position) |
| `CardPileCmd.cs:795` | `list.StableShuffle(player.RunState.Rng.Shuffle)` ✅ |
| `Models.Cards/Uproar.cs / Catastrophe.cs / BeatDown.cs / Reboot.cs` | combat-scope（推测走 RunState.Rng） |
| `Models.Potions/BottledPotential.cs` | `await CardPileCmd.Shuffle(...)` ✅ |
| `Models.Relics/NeowsBones.cs:42` | `base.Owner.PlayerRng.Rewards.Shuffle(...)` ✅ |
| `Models.Relics/ToastyMittens.cs` | 同 pattern ✅ |
| `Models.Events/TheArchitect.cs` | `base.Owner.RunState.Rng...` ✅ |
| `Models.Events/MorphicGrove.cs / Trial.cs / DoorsOfLightAndDark.cs / BattlewornDummy.cs` | `base.Owner.RunState.Rng.Niche` ✅ |
| `Models.Modifiers/CursedRun.cs` | `RunState.Rng.Niche` ✅ |
| `Models.Powers/DrumOfBattlePower.cs / StampedePower.cs / ForegoneConclusionPower.cs` | combat-scope（推测走 RunState.Rng）|
| `Models.Monsters/*`（17 个） | `monster.Rng` 受控（per-monster seed 派生自 `RunState.Rng.Seed`，**详见下条 derived RNG**） |
| `Models.Relics/Whetstone.cs / WarHammer.cs` | `RunState.Rng.Niche` ✅ |
| `RunRngSet.cs` 自身 | — |

#### ✅ Derived RNG（受控，从 master seed 确定性派生）

`new Rng(...)` 直接构造但 **seed 全部源自 `runState.Rng.Seed`**——确定性，所以一旦 master seed 控住，这些子 RNG 也跟着确定：

| 文件 | seed 公式 |
|---|---|
| `Combat/CombatState.cs:137` | `new Rng((uint)((RunState.Rng.Seed + currentMapCoord.col) ?? row ?? (actIdx + creature.CombatId.Value)))` — 每只怪物自己一个 RNG |
| `Models/EventModel.cs:193` | `new Rng((uint)(RunState.Rng.Seed + (isShared ? 0 : NetId) + hashCode(eventId)))` |
| `Models/EncounterModel.cs:199` | `new Rng((uint)(RunState.Rng.Seed + TotalFloor + hashCode(encounterId)))` |
| `Models.Modifiers/BigGameHunter.cs:16` | `new Rng(RunState.Rng.Seed, $"act_{idx+1}_map")` |
| `Map/StandardActMap.cs:96` / `SpoilsActMap.cs:69` | `new Rng(RunState.Rng.Seed, "act_X_map")` |
| `Nodes.Screens.Map/NMapScreen.cs:460` | `new Rng(seed, "map_jitter_{actIdx}")` — 视觉抖动，但 deterministic |
| `Nodes.Screens.Map/MapSplitVoteAnimation.cs:59` | multiplayer vote — `HashCode.Combine(seed, ActFloor)` |
| `Nodes.Events/EventSplitVoteAnimation.cs:39` | 同上 |
| `Nodes.Rooms/NCombatRoom.cs:265` | `new Rng(state.Rng.Seed + num)` |
| `Models.Relics/Byrdpip.cs:48` / `FurCoat.cs:78` / `PaelsLegion.cs:121` | `new Rng((uint)(NetId + RunState.Rng.Seed))` — relic 皮肤选择 |
| `Multiplayer.Game/MapSelectionSynchronizer.cs:44` / `EventSynchronizer.cs:53` | multiplayer 同步 — seed 来自同步消息（仍 master 控制下） |

**这些不需要单独 hook**——只要 master seed 一致，这些 derived 子流就一致。

#### ❌ 真正"野生" RNG（需要单独处理）

| 命中点 | 类型 | 影响 | 处理方案 |
|---|---|---|---|
| **`Models/EncounterModel.cs:278`** — `_rng = new Rng((uint)(DateTime.UtcNow - UnixEpoch).TotalSeconds)` | wall-clock seed | 仅由 `EncounterModel.DebugRandomizeRng()` 触发，唯一 caller 是 `FightConsoleCmd` | **不通过 fight console 命令进入战斗即可避开**。或 Harmony postfix 替换 |
| `LeaderboardConsoleCmd.cs:67` | `new Rng(Rng.Chaotic.NextUnsignedInt())` | leaderboard 抽样 | 不参与 gameplay |
| `Nodes.Screens.RunHistory/NRunHistory.cs:513/575` | UI 渲染 | history screen | 无关 |
| `Runs/NullRunState.cs:96` | `RunOddsSet(new Rng())` | null pattern fallback | seed=0，可视为确定性 |
| `Nodes.Vfx.Utilities/*`、`Nodes.Vfx.Ui/*`、`Nodes.Vfx/*` — **`Rng.Chaotic`** + **`GD.Randf` / `GD.Randi` / `GD.RandRange`** + 一处 `new System.Random()` | 视觉 | 粒子位置、屏幕抖动、节奏音乐选择等 | **0 个影响游戏逻辑**——不需要 hook |
| `Nodes.Audio/NRunMusicController.cs:173` | bg music 选择 | UI/audio | deterministic from RunState.Rng.Seed 但只影响声音 |
| `Nodes.Screens.DailyRun/NDailyRunScreen.cs` | daily run | 由 daily date 字符串 deterministic seed | 不进 daily run 即避开 |

**关键摘要**：`docs/rng_audit_raw.txt` 的 44 个命中点中：
- **40 个 受控**（绝大多数走 RunRngSet / PlayerRngSet / derived from master seed）
- **3-4 个 wild**（其中 0 个影响游戏逻辑——全部 VFX/UI/leaderboard）
- **1 个 debug-only outlier**（`EncounterModel.DebugRandomizeRng()`）

### 1.5 Combat 内是否独立 RNG（问 5）

**`CombatState` 没有自己的 master RNG**。所有战斗时 RNG 都从 `RunState.Rng.Combat*`（5 个 combat-scope 子流）拿。

唯一例外：**每个 `MonsterModel` 实例有自己的 `Rng`** —— 在 `CombatState` 构造时按 `new Rng((uint)(RunState.Rng.Seed + col/row/CombatId))` 创建。这是 deterministic derived RNG，已在 §1.4 说明。

Shuffle 行为：`player.RunState.Rng.Shuffle` —— 走 `RunRngType.Shuffle` 子流。AutoSlayCardSelector 用 `_random.Shuffle(...)` 是 AutoSlay 自己 init 的 seed，**不影响 game state**。

---

## 2. dev plan §2.5 RngController 工作量估算

### 原 dev plan §2.5 假设

> 劫持游戏内**所有** `RandomNumberGenerator` 实例，全部 reseed 到外部传入的 master seed 派生的子流。
> **交付物**：维护一份显式的 RNG 调用点审计清单（grep `Random` / `Randf` / `Randi` / `Shuffle` / `Choose` 等），逐个 patch 并写入 `docs/rng_audit.md`。

### 修正后的真实工作量

**轻量包装**（不再是"中等工作量"）：

1. **接管 master seed 入口点（核心）**：
   - 在 `RunState` 创建时（`RunManager.SetUpNewSinglePlayer/SetUpTest` 等路径），传入用户指定的 seed
   - 已经支持：`RunState` ctor 接收 `RunRngSet`，`RunRngSet ctor` 接收 string seed
   - **几乎无需 patch**——只要 ScenarioInjector 在构造 RunState 时传我们的 seed 即可

2. **`NGame.Instance.DebugSeedOverride`**：
   - AutoSlay 用过这个字段——意味着官方留了 seed 注入点
   - 待确认（任务 E 顺带）：是否覆盖整个 run startup 流程

3. **Harmony postfix `EncounterModel.DebugRandomizeRng()`**：
   - 唯一 wall-clock outlier；改成接收外部 seed 或不调用
   - 但更简单：**ScenarioInjector 不使用 FightConsoleCmd 路径**，问题自动消失

4. **`PlayerRng` per-player seed**：
   - 已自动 derive from master seed (`hashCode(seed) + NetId`)
   - 我们只跑 single-player，NetId 固定，**也自动确定性**

5. **save/load RNG state**：
   - `RunRngSet.ToSerializable() / FromSave(...)` 现成的
   - `PlayerRngSet.ToSerializable() / FromSerializable(...)` 现成的
   - **dev plan §2.1 "包含 RNG 内部状态" → 直接调这些方法**

### 真实"RNG 审计清单"`docs/rng_audit.md` 内容

我们要写的是：
- 12 个 RunRngType 流的用途表（本文档 §1.1 已写）
- 3 个 PlayerRngType 流的用途表（同）
- 11 处 derived seed 公式表（§1.4 中段）
- 1 个 outlier 说明（`EncounterModel.DebugRandomizeRng`）+ 处理方式
- 验证策略：**determinism test 跑 1M 步固定 seed，trajectory bit-exact**

不需要逐个 patch 几十个调用点——只需"接管 master seed + 验证"。

---

## 3. 不确定项 / hand-off

1. **`UpFront` 流的具体作用** — 名字暗示 "run-start roll"（character / starting deck / starting relics / opening hand 等），未直接观察到使用点。**任务 E 顺带核实，看 RunState ctor 是否调它**
2. **`CombatPotionGeneration` / `CombatOrbs` 的实际触发场景** — Defect 类（Necrobinder?）orb 卡组路径未细读。不影响 hook 策略，仅影响完整性文档
3. **`NGame.Instance.DebugSeedOverride` 字段的影响范围** — 是否覆盖所有 run-start RNG，还是只覆盖 main run 主流。**任务 E 顺带或 P0 实测**
4. **`Rng.Chaotic` 的"懒初始化"** — 它是 `static Rng Chaotic { get; } = new Rng((uint)Now)` ——**在 mod assembly 第一次访问时初始化**。意味着 mod 加载时间影响 Chaotic 初始 seed。但 Chaotic 只用于 VFX，不影响 logic
5. **multiplayer 路径** — `MapSelectionSynchronizer` / `EventSynchronizer` 走网络消息同步 seed。我们只做单人，但 mod 启动要禁掉/绕过 multiplayer 路径（task A 已提）
6. **AutoSlay 注入 seed 的可信度** — AutoSlay 用 `NGame.Instance.DebugSeedOverride` + `CardSelectCmd.UseSelector(...)` 跑 deterministic 的 self-play。这本身就是 dev plan determinism test 的 reference impl

---

## 4. 给后续任务的备忘

- **任务 E（Serialization）**：
  - 检查 `RunState` / `Player` 是否在序列化时 `ToSerializable()` 自动包含 RNG（看上下文证据应该是的）
  - 验证 `SerializableRun.SerializableRng` 的字段——这是 dev plan §2.1 RNG state 部分的 ground truth
  - 检查 `NGame.Instance.DebugSeedOverride` 的影响范围
- **SUMMARY.md**：dev plan §2.5 重新分级为"**几乎免费 / 轻量包装**"。把原假设"维护几十个 hook 点的清单"删掉
