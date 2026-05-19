# 任务 E：State 序列化盘点

> Scope：`Runs/RunState.cs` + `Combat/CombatState.cs` + `Combat.History/CombatHistory.cs` + `Multiplayer/CombatStateSynchronizer.cs` + `Saves/SerializableRun.cs` + `Entities.Players/Player.cs` + `Entities.Players/PlayerCombatState.cs` + 跨文件 `ToSerializable` grep。
> 一句话结论：**整 run（between-rooms）几乎免费——`RunManager.Instance.ToSave(...)` 一行返回完整 `SerializableRun`。但 mid-combat 状态（hand/discard/draw/energy/block/buffs）整个游戏都没有官方序列化机制——这部分是 dev plan §2.1 的主要工作**。

---

## 1. 关键回答

### 1.1 `RunState` 是否是 run 的根对象（问 1）

✅ 是。`public class RunState : IRunState, ICardScope, IPlayerCollection`，包含：

- `Players: IReadOnlyList<Player>`
- `Acts: IReadOnlyList<ActModel>` + `CurrentActIndex`
- `Map: ActMap` + `VisitedMapCoords` + `MapPointHistory`
- `Rng: RunRngSet` ✅
- `Odds: RunOddsSet`
- `SharedRelicGrabBag: RelicGrabBag`
- `UnlockState`
- `Modifiers: IReadOnlyList<ModifierModel>`
- `GameMode`、`AscensionLevel`、`MultiplayerScalingModel`
- `ExtraFields: ExtraRunFields`
- `_currentRooms` stack (`CurrentRoom`、`BaseRoom`)
- `_visitedEventIds`

**重要**：`RunState.State` 字段（私有）持有当前 run，但用 `RunManager.Instance.DebugOnlyGetState()` 公开访问。**`RunState` 本身不含 RNG 数据持久化方法**——RNG 在 `Rng: RunRngSet` 子字段里，**RunRngSet.ToSerializable() 已有**（见任务 D）。

构造路径（dev plan ScenarioInjector P0/P1/P2 都要用）：

- `RunState.CreateForNewRun(players, acts, modifiers, gameMode, ascensionLevel, seed)` — **public static**
- `RunState.FromSerializable(SerializableRun)` — **public static**
- `RunState.CreateForTest(...)` — **public static**，参数全 optional，适合 unit test 用

### 1.2 `CombatState` 是否完整（问 2）

⚠️ "完整"但**只在内存里**：

```
CombatState {
  IRunState RunState
  IReadOnlyList<Creature> Allies / Enemies / Creatures / PlayerCreatures
  IReadOnlyList<Player> Players
  IReadOnlyList<ModifierModel> Modifiers
  MultiplayerScalingModel?
  int RoundNumber
  CombatSide CurrentSide
  EncounterModel? Encounter
  List<Creature> EscapedCreatures
  IReadOnlyList<Creature> CreaturesOnCurrentSide
  IReadOnlyList<Creature> HittableEnemies
  // 内部：_allCards、_nextCreatureId
}
```

每个 `Creature`：

```
Creature {
  int Block, CurrentHp, MaxHp
  MonsterModel? Monster                 # includes NextMove (intent)
  Player? Player
  CombatSide Side
  IReadOnlyList<PowerModel> Powers      # buffs/debuffs
  uint? CombatId
  ...
}
```

每个 `Player.PlayerCombatState`（**combat-only 数据**）：

```
PlayerCombatState {
  CardPile Hand, DrawPile, DiscardPile, ExhaustPile, PlayPile
  int _energy, _stars
  List<Creature> _pets
  ...
}
```

**❌ CombatState、PlayerCombatState、Creature 都没有 `ToSerializable()` 方法**。整个 mid-combat 状态没有官方序列化路径。

构造路径：

- `new CombatState(EncounterModel? encounter, IRunState? runState, IReadOnlyList<ModifierModel>? modifiers, MultiplayerScalingModel? scaling)` — **public ctor**
- `CombatManager.Instance.SetUpCombat(combatState)` — **public**（任务 B 确认）

**这意味着 dev plan §2.2 ScenarioInjector 的"直接构造 CombatState 注入"路径理论可行**——但要手动填 `Allies`/`Enemies`、把 `Player.PlayerCombatState` 里 5 个 pile 装满指定 card、设 energy/stars、给敌人贴 PowerModel buff——**工作量不小，但有清晰的字段映射**。

### 1.3 现有 serialization 机制（问 3，核心）

游戏自己有 **3 套独立机制**，**完整度不同**：

#### (1) Save/Load — `SerializableRun`

📍 位置：`MegaCrit.Sts2.Core.Saves/SerializableRun.cs`，与 `RunManager.ToSave(preFinishedRoom)` / `RunState.FromSerializable(save)` 配对

**完整度**：✅ 完整 run 状态（between-rooms），❌ 不含 mid-combat

字段：

| Field | 类型 | 用途 |
|---|---|---|
| `SchemaVersion: int` | int | **版本管理已就位** |
| `Acts: List<SerializableActModel>` | 含 `SavedMap` | 整地图（含 act 切换） |
| `Modifiers, GameMode, Ascension, CurrentActIndex` | enum/int | run 设置 |
| `EventsSeen: List<ModelId>` | | |
| `PreFinishedRoom: SerializableRoom?` | | 当前房间快照（room 完成前的态） |
| `SerializableOdds: SerializableRunOddsSet` | | |
| `SerializableSharedRelicGrabBag` | | |
| `Players: List<SerializablePlayer>` | **见下** | |
| `SerializableRng: SerializableRunRngSet` ✅ | 含 RNG state | 完整 RNG counters |
| `VisitedMapCoords, MapPointHistory` | | 走过的路径 |
| `MapDrawings: SerializableMapDrawings?` | | UI 草稿 |
| `ExtraFields, SaveTime, StartTime, RunTime, WinTime, PlatformType, DailyTime` | | metrics |

`SerializablePlayer` 包含：
- `CharacterId, CurrentHp, MaxHp, MaxEnergy, MaxPotionSlotCount, BaseOrbSlotCount, NetId, Gold`
- `Rng: SerializablePlayerRngSet` ✅
- `Odds: SerializablePlayerOddsSet`
- `RelicGrabBag, Deck (List<SerializableCard>), Relics (List<SerializableRelic>), Potions`
- `ExtraFields, UnlockState, DiscoveredCards/Enemies/Epochs/Potions/Relics`

**❌ NOT included** in `SerializablePlayer`: `PlayerCombatState`（hand/draw/discard/exhaust/energy/stars/pets）、`Creature.Block`、`Creature.Powers`、当前回合数等。

**双格式**：
- JSON：所有字段带 `[JsonPropertyName("snake_case")]`，可直接 `System.Text.Json` 序列化。`MegaCritSerializerContext` 注册了所有 `ObjectCreator`
- Binary：`SerializableRun : IPacketSerializable`，提供 `Serialize(PacketWriter)` / `Deserialize(PacketReader)`，紧凑二进制格式

**Anonymizer**：`SerializableRun.Anonymized()` 已有——剥离 NetId 等隐私字段，用作 leaderboard 上报。对 STS2-Gym PartialObs **不直接适用**（它只去 NetId，不去 RNG state / draw pile order）

#### (2) Multiplayer Sync — `CombatStateSynchronizer`

📍 位置：`MegaCrit.Sts2.Core.Multiplayer/CombatStateSynchronizer.cs`

**完整度**：❌ **不是更细粒度的 mid-combat 同步**——它只发：
- `SyncPlayerDataMessage` 含 `SerializablePlayer`（同 Save/Load 用的 SerializablePlayer）
- `SyncRngMessage` 含 `SerializableRunRngSet` + `SerializableRelicGrabBag`

也就是说**游戏只在 combat 边界做 multiplayer sync**，不在战斗中间同步任何状态。两端的 combat 通过"相同初始 state + 相同 RNG counter + 相同 action 顺序"deterministic 重现。

**含义**：multiplayer 路径**没有解决 mid-combat 序列化问题**——它依赖 determinism。对 STS2-Gym 来说，能不能借力？只有当 SnapShot 必须在战斗外（map / reward / shop 等）取时——那就直接用 `SerializableRun`。

#### (3) Combat History — `CombatHistory`

📍 位置：`MegaCrit.Sts2.Core.Combat.History/`

**完整度**：✅ 战斗事件**完整记录**，❌ 不是状态快照

`CombatHistory.Entries: IEnumerable<CombatHistoryEntry>` 含：

- `CardPlayStartedEntry`、`CardPlayFinishedEntry`
- `CardAfflictedEntry`、`CardDiscardedEntry`、`CardDrawnEntry`、`CardExhaustedEntry`、`CardGeneratedEntry`
- `CreatureAttackedEntry`、`DamageReceivedEntry`、`BlockGainedEntry`
- `EnergySpentEntry`、`OrbChanneledEntry`、`PotionUsedEntry`、`MonsterPerformedMoveEntry`

**含义**：
- ❌ **不能**作为 dev plan §2.1 Serializer 实现——它是 event log，不是 state snapshot
- ✅ **可以**作为 **trajectory dump** 用——对 D4RL-style offline 数据集天然契合
- ✅ 也提供 **action history**——LLM agent prompt 里贴最近 N 个 entry 当 "recent action log"

### 1.4 `CombatStateSynchronizer` 粒度（问 4）

已答（见 1.3 (2)）：**整状态 snapshot，combat 边界触发**，不是增量 delta。**对 STS2-Gym 没有额外利用价值**——它能做的 `SerializableRun` 都能做，且 SerializableRun 更全。

### 1.5 Serializer 最优策略（问 5，决策）

四个候选评估：

| 候选 | 评估 |
|---|---|
| (a) 复用 multiplayer 同步用的 state serialization | ❌ 与 (b) 内容重复，更窄。Drop |
| (b) 复用 save/load 用的机制 | ✅ **between-rooms 完美**，`RunManager.ToSave()` 一行 |
| (c) 复用 DumpConsoleCmd 的逻辑 | ❌ 已 debunk（任务 B），它只 dump model id 表 |
| (d) 自己直接序列化 RunState / CombatState 的字段 | ⚠️ **mid-combat 必须走这条** |

**推荐：(b) + (d) 混合**：

```
SerializableSts2GymState {
  SerializableRun run                  # 复用 (b)，直接调 RunManager.ToSave()
  SerializableCombatState? combat      # 自己写 (d)，仅 in-combat 时填
  Phase phase                          # map / combat / event / shop / reward / game_over
}

SerializableCombatState {              # 我们写的扩展
  int round_number, current_side
  ModelId? encounter_id
  List<SerializableCreature> creatures
  Dictionary<ulong, SerializablePlayerCombatState> player_combat_states
}

SerializablePlayerCombatState {
  List<SerializableCard> hand, draw_pile, discard_pile, exhaust_pile, play_pile
  int energy, stars
  List<SerializableCreature> pets
}

SerializableCreature {
  uint? combat_id
  ModelId? monster_id                  # null for players
  CombatSide side
  int current_hp, max_hp, block
  List<SerializablePower> powers       # buffs/debuffs
  SerializableMonsterIntent? next_move # for monsters only
  string? slot_name
}
```

`SerializableCard` 等子组件直接复用游戏现有的（每个 `CardModel.ToSerializable()` 已有）。

**为什么不全部走 (d)**：(b) 已经覆盖了 run-scope 18 个字段、3 个 RNG 序列化器、Card/Relic/Potion 完整序列化。**重写就是浪费**。

### 1.6 PartialObs 过滤清单（问 6）

人类玩家**看不到**的字段（必须在 obs 层过滤掉）：

| 字段 | 来源 | 过滤理由 |
|---|---|---|
| `SerializableRunRngSet.Counters` | `SerializableRun.SerializableRng` | RNG state，玩家不可知 |
| `SerializablePlayerRngSet.Counters` | `SerializablePlayer.Rng` | 同上 |
| **`DrawPile` 顺序** | 我们自己写的 `SerializablePlayerCombatState.draw_pile` | 玩家看不到牌库顺序（仅看大小） |
| `SharedRelicGrabBag` / `Player.RelicGrabBag` | `SerializableRun.SerializableSharedRelicGrabBag` / `SerializablePlayer.RelicGrabBag` | 未来 relic 抽取池——玩家不知后面会拿什么 |
| `EventsSeen` 顺序细节 | `SerializableRun.EventsSeen` | 玩家知道哪些 event 触发过，但具体决定记录可能多余——视情况留 |
| `UnknownMapPoint` 已 resolved 但未 visited 的结果 | `SerializableRun.Acts[*].SavedMap` | 待研究——`?` 节点可能含解析后的真实类型，玩家未到达前看到的是 `?` |
| `MonsterModel` 的 follow-up move state | `SerializableCreature.next_move` 之后的 RAND branch | 玩家只看到下一步 intent，看不到再下一步 |
| `MonsterModel.MaxHp`（普通敌人首战时） | `SerializableCreature.max_hp` | STS1 中首遇敌人不显示 MaxHp，玩家只看 CurrentHp。STS2 行为待确认 |
| `Encounter.MonstersGenerated` 但战斗未开始时的具体阵容 | `SerializableRun.PreFinishedRoom` | 进入战斗前玩家不一定知道具体敌人 |
| `Powers` 列表中"hidden until triggered"的 power | `SerializableCreature.powers` 之 hidden flag | 部分 monster power 是"first time only" hidden——待具体 power 类核实 |
| 卡牌 enchant 后的隐藏属性 | `SerializableCard.enchantments`（如有） | STS2 新机制，需确认 enchant 是否对玩家完全可见 |
| `DiscoveredCards/Enemies/Epochs/Potions/Relics` | `SerializablePlayer.Discovered*` | 玩家知道自己发现了什么，但对 RL/LLM 是 meta-信息，按需暴露 |

**PartialObs 模式**：在 RawState → ObservationView 转换层做一次 filter，**默认隐藏以上字段**。FullInfo 模式则全部暴露。在 dev plan §2.8 HumanRenderer 的两条 view（text + json）都需要应用这套 filter。

---

## 2. 各 state 对象字段清单（按游戏阶段分组）

### 2.1 Map / Event / Shop / Rest / Treasure phase

直接用 `SerializableRun`（见 §1.3 (1) 字段表）。

### 2.2 Combat phase

`SerializableRun` + 我们的 `SerializableCombatState`（见 §1.5）。

### 2.3 Reward / Card-Select / Upgrade / Transform phase

`SerializableRun`（已含状态）+ 当前 screen 的 `options: List<SerializableCard>`（这层我们 wrap）。

可以利用 `CardSelectCmd.Selector`（任务 C 提到的 stack）——当 selector active 时，options 通过我们的 `ICardSelector` 实现传过来。

### 2.4 Game-Over phase

`SerializableRun` 末态 + 一个 boolean `is_victory` + `WinTime` 字段。

---

## 3. 三/四种现成 serialization 机制的对比表

| 机制 | 范围 | 完整度 | 是否含 RNG | 是否含 mid-combat | 公开 API | dev plan 用途 |
|---|---|---|---|---|---|---|
| `SerializableRun`（Save/Load） | 整 run | ⭐⭐⭐⭐ | ✅ | ❌ | `RunManager.ToSave()` / `RunState.FromSerializable()` | dev plan §2.1 主体 |
| `CombatStateSynchronizer`（Multiplayer） | combat 边界 | ⭐⭐ | ✅ | ❌ | `CombatStateSynchronizer.StartSync/WaitForSync` | **无额外用途**（同上 strictly subset） |
| `DumpConsoleCmd` | model id 字典 | ⭐ | ❌ | ❌ | `ModelIdSerializationCache.Dump()` | **不是 serializer**，是 vocabulary dump（可用作 tensor encoder 的 card-id 表） |
| `CombatHistory` | 战斗事件流 | event log | ❌ | append-only | `CombatManager.Instance.History.Entries` | **D4RL trajectory dump + LLM action log** |

---

## 4. 推荐路径（决策）

```
Path = (b) SerializableRun 复用 + (d) 我们写一个 SerializableCombatState

Implementation:
  Serializer.Snapshot() -> SerializableSts2GymState:
    s = new SerializableSts2GymState()
    s.run = RunManager.Instance.ToSave(preFinishedRoom: null)   # 几乎免费
    if CombatManager.Instance.IsInProgress:
      cs = CombatManager.Instance.DebugOnlyGetState()
      s.combat = SerializableCombatState.From(cs)               # 我们写
    s.phase = ResolvePhase()                                    # 看 RunState.CurrentRoom / NOverlayStack
    return s

  Deserializer.Restore(s) -> void:
    runState = RunState.FromSerializable(s.run)
    RunManager.Instance.SetUpSavedSinglePlayer(runState, s.run) # 已有 public 方法
    if s.combat != null:
      combatState = SerializableCombatState.To(s.combat, runState)
      CombatManager.Instance.SetUpCombat(combatState)           # 已有 public 方法
```

工作量评估：

| 组件 | 工作量 |
|---|---|
| Snapshot of `SerializableRun` | **几乎免费**——一行 |
| Snapshot of mid-combat | **中等工作量**——写 SerializableCombatState / SerializableCreature / SerializablePlayerCombatState / SerializablePower 4 个 dataclass，每个约 5-10 个字段映射 |
| Restore of `SerializableRun` | **几乎免费**——`SetUpSavedSinglePlayer` 已有 |
| Restore of mid-combat | **轻量**——构造 CombatState ctor + 填 pile / energy / power |
| PartialObs filter | **中等工作量**——单独 wrapper 层，对每个 type 决定 mask |
| JSON / Tensor view 派生 | **轻量**——基于上层结构 traversal |

---

## 5. 不确定项 / hand-off

1. **`UnknownMapPoint` 节点在玩家未访问前的可见性** — STS2 中 `?` 节点的解析时机决定 PartialObs 是否要 mask 它。**需运行时验证**
2. **`MonsterModel.MaxHp` 首遇可见性** — STS1 是首遇时不显示，待 STS2 实测
3. **`Encounter` 在进战斗前的可见信息** — Map 上能看到 monster room 但不知道具体阵容；进入后立即可见？还是动画完才可见？**P0 实测**
4. **`CombatState` ctor + `SetUpCombat()` 直接注入路径的完整性** — 任务 B 提示可行，但 mid-combat snapshot/restore 是否 bit-exact 仍需 determinism test 验证
5. **`Card.ToSerializable()` 是否包含所有"in-flight"卡牌状态**（如 `Enraged` modifier、`Upgraded` flag、enchantments） — 待具体 SerializableCard 字段核实，**P0 必查**
6. **`PowerModel.ToSerializable()` 是否存在 / 是否包含 stack count** — 没查到 ToSerializable，可能直接 serialize stack 数值。**P0 必查**
7. **`MonsterModel.NextMove` 序列化** — 怪物 intent 包含"将做什么、伤害值、目标"，可能含 random branch。我们要序列化"已 roll 完的结果"（即玩家看到的 intent），不是 random state
8. **`NGame.Instance.DebugSeedOverride` 与 `RunState.CreateForNewRun(..., seed)` 的关系** — 看起来两条路径并行存在，**P0 实测哪条更可控**

---

## 6. 给 SUMMARY 的备忘

dev plan §2.1 Serializer 重新分级：

- **整 run（between-rooms）部分**：⭐⭐⭐⭐ **几乎免费**，一行 `RunManager.ToSave()`
- **mid-combat 部分**：⭐⭐ **中等工作量**——4 个 dataclass + 字段映射，但**每个字段都有清晰公开来源**
- **PartialObs filter 层**：⭐⭐ **中等工作量**——独立 wrapper，根据 7 个隐藏字段类型做 mask
- **JSON / tensor 派生 view**：⭐ **轻量**——树遍历

**SerializableRun + CombatHistory 联合**满足 dev plan **§2.1 + 数据集需求**——前者给 snapshot，后者给 trajectory event log。这是双重大礼。
