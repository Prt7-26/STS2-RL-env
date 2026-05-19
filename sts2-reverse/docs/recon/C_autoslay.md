# 任务 C：AutoSlay 是不是 step 驱动的现成实现

> Scope：`MegaCrit.Sts2.Core.AutoSlay/`（4 文件）+ `Handlers/`（3 接口） + `Handlers.Rooms/`（6 个） + `Handlers.Screens/`（13 个） + `Helpers/`（4 文件）。Sampled `AutoSlayer.cs`、`CombatRoomHandler.cs`、`MapScreenHandler.cs`、`CardRewardScreenHandler.cs`、`WaitHelper.cs`、`AutoSlayCardSelector.cs`。
> 一句话结论：**AutoSlay 不是 step API，是端到端 UI 驱动的 self-play 测试框架。借鉴价值大、直接复用价值小**。它告诉我们 step 原语 (`CardCmd.AutoPlay` / `PlayerCmd.EndTurn`) 怎么用，但它本身的"驱动方式（用 UiHelper.Click 点按钮 + 100ms polling）"对 STS2-Gym 来说太慢、太脆弱。

---

## 1. 关键回答

### 1.1 主循环结构（问 1）

**同步 polling 模型，async/await 风格但不是事件驱动**。伪代码：

```
AutoSlayer.PlayRunAsync(seed):
  await WaitHelper.Until(() => NGame.Instance != null, ...)       # 等游戏 boot
  NGame.Instance.DebugSeedOverride = seed
  SaveManager.Instance.PrefsSave.FastMode = FastModeType.Fast     # ⚠️ Fast 不是 Instant
  SaveManager.Instance.SetFtuesEnabled(false)
  SaveManager.Instance.ObtainEpochOverride(...) x4                # 解锁所有角色
  _cardSelectorScope = CardSelectCmd.UseSelector(new AutoSlayCardSelector(rng))
  await PlayMainMenuAsync(ct)                                     # 通过 UI 点 SingleplayerButton → 角色选择
  await WaitHelper.Until(() => RunManager.Instance.DebugOnlyGetState() != null, ...)
  runState = RunManager.Instance.DebugOnlyGetState()
  while (runState.TotalFloor < 49):
    roomType = runState.CurrentRoom.RoomType
    await _roomHandlers[roomType].HandleAsync(rng, ct)            # 进 room handler
    await WaitForRewardsScreenAsync(ct) 或 Task.Delay(500)
    await DrainOverlayScreensAsync(ct)                            # 处理叠加屏（reward / upgrade / etc）
    if RestSite: ClickProceed
    if Event:    ClickProceed
    if Boss:     等 act transition，act 末则进 victory event
    await _mapHandler.HandleAsync(rng, ct)                        # 选下一节点
  await AbandonRunAsync(ct)                                       # 或 WaitForMainMenuAsync
```

完全 async/await。但**每个 `await WaitHelper.Until(...)` 都是 100ms 一次的 polling**。

### 1.2 怎么知道 handler "完成了"（问 2）

**返回值（Task 完成）+ 内部 polling 游戏状态**。

- `IHandler.HandleAsync(Rng, CancellationToken) -> Task` —— 返回 Task，await 完成就是完成
- handler 内部用 `WaitHelper.Until(predicate, ct, timeout, msg)` polling 关键状态：
  - 例：`CombatRoomHandler.HandleAsync` 内部 `while (CombatManager.Instance.IsInProgress && turnCount < 100) { ... }`
  - 例：`MapScreenHandler` 同时用 polling + **事件订阅**（`RunManager.Instance.RoomEntered` 事件，配合 `TaskCompletionSource`）
- 没有统一的"完成信号" — 每个 handler 自己负责退出

### 1.3 动画 / 网络等待 + AutoSlayTimeoutException（问 3）

- **三种 wait 原语**（`WaitHelper.cs`）：
  - `Until(condition, ct, timeout, msg)` — 100ms polling 直到 predicate 真
  - `ForNode<T>(root, path, ct, timeout)` — 等 Godot 节点存在 + 可见 + (button) enabled
  - `ForTask(task, ct, timeout, msg)` — 等 Task 完成
- 超时 → 抛 `AutoSlayTimeoutException : TimeoutException`
- 出现的超时场景（按 PlayRunAsync 出现频率）：
  - "Game instance not initialized" — 10s
  - "Run state not initialized" — 30s
  - "Room type not assigned" — 10s
  - "Map screen not visible" — 10s
  - "Map point not enabled" — 10s
  - "Combat not started" / "Play phase not started" — 10-30s
  - "Rewards screen did not appear after combat" — 10s
  - "Card reward screen did not close after selection" — 10s
  - "Main menu did not appear after game over" — 30s
  - "Operation timed out after Xs"（任何 `HandleAsync` 整体超时）
- 还有 `Watchdog.Check()` 在每次 polling 时调用 —— 30s 不 `.Reset(phase_name)` 也会抛 timeout（看 `AutoSlayConfig.watchdogTimeout`），独立于 polling 超时

### 1.4 是否有 policy 接口能插（问 4，关键）

**部分有**，但**不够覆盖全决策点**。

#### ✅ **有现成的 policy 接口**（局部）

`MegaCrit.Sts2.Core.TestSupport.ICardSelector`（**注意 namespace 是 `TestSupport`，不是 AutoSlay 专属**）：

```csharp
public interface ICardSelector {
  Task<IEnumerable<CardModel>> GetSelectedCards(IEnumerable<CardModel> options, int min, int max);
  CardModel? GetSelectedCardReward(IReadOnlyList<CardCreationResult> options, IReadOnlyList<CardRewardAlternative> alts);
}
```

注入方式（**这是关键 mechanism**）：

```csharp
// CardSelectCmd 提供静态 stack：
public static IDisposable UseSelector(ICardSelector selector);   // 独占
public static IDisposable PushSelector(ICardSelector selector);  // 叠加
public static ICardSelector? Selector { get; }                   // 当前栈顶
```

`Stack<ICardSelector>` 全局静态。`UseSelector` 要求栈空、`PushSelector` 任意叠加、`Dispose` 弹出。**这是 mega crit 给 testing 留的官方 hook 点**。

**对 STS2-Gym 的意义**：
- ✅ Card reward 决策、card transform / upgrade / enchant / select-to-discard 等都过 `ICardSelector`
- ✅ STS2-Gym 可以注册自己的 `ICardSelector` 实现（Wrapping LLM/RL policy），**完全无需 Harmony patch、官方支持**

#### ❌ **没有覆盖的决策点**（要自己挖）

- **战斗中"打哪张牌"**：CombatRoomHandler 直接调 `CardCmd.AutoPlay(...)`，目标选择 / 打牌选择都硬编码在 handler 内
- **end-turn 决策**：直接 `PlayerCmd.EndTurn(player, false)`
- **map node 选择**：MapScreenHandler 直接点 `nMapPoint` 的 Click
- **event option 选择**：未细读 EventRoomHandler，但参照模式应为直接点 UI button
- **shop 决策**：同上
- **potion 使用决策**：CombatRoomHandler 内调 `item.EnqueueManualUse(target)`

也就是说，**dev plan §2.3 ActionDispatcher 真正要做的工作不能复用 CombatRoomHandler**——CombatRoomHandler 是**策略实现**，不是策略接口。

**借鉴价值**：把 `ICardSelector` 的"global stack + IDisposable scope"模式扩展到 end-turn、card-target、map-direction 等所有决策点。这是给 STS2-Gym 提的**核心架构建议**，不需要侵入式 Harmony patch，对外接口干净。

### 1.5 渲染 / 音频是否禁用、吞吐量（问 5）

- **渲染**：**没有禁用**。AutoSlay 通过 Godot 节点路径（`/root/Game/RootSceneContainer/...`）找 UI 控件、`UiHelper.Click(...)`、`EmitSignal`。它依赖 UI 实际渲染存在
- **音频**：没看到禁用代码（unsigned by recon）—— 默认走游戏 sound mixer
- **FastMode**：`PlayRunAsync` 设 `FastModeType.Fast`（**不是 `Instant`**）。这一选择有意——可能 Instant 模式下某些动画-逻辑耦合点会出 bug（CombatRoomHandler 内大量 `Task.Delay(100, ct)`、`Task.Delay(500, ct)`，似乎在等动画/状态稳定）
- **吞吐**：
  - `runTimeout` 25 分钟、`maxFloor` 49 —— 单 run 大约 15-25 分钟 wall-clock
  - `pollingInterval` 100ms —— 任何 wait 至少 100ms 量级
  - CombatRoomHandler 每张牌之间 `Task.Delay(100, ct)`
  - DrainOverlayScreens 之后 `Task.Delay(100, ct)`，每个 boss/non-combat 后 `Task.Delay(500, ct)`
  - **大致估算**：50-80 step/min = 0.8-1.3 step/s
  - **dev plan 目标 ≥50 step/s**——AutoSlay 慢 40-60 倍

### 1.6 完整 run 的 wall-clock（问 6）

- `AutoSlayConfig.runTimeout = 25 min`（硬上限）
- `defaultRoomTimeout = 2 min`、`CombatRoomHandler.Timeout = 5 min`、`MapScreenHandler.Timeout = 30s`、`defaultScreenTimeout = 30s`
- 在 `FastModeType.Fast` 下：一 run 大致 15-25 分钟（推测，无实测数据）
- 49 楼 × 几乎每楼一战 × 5 min combat budget——25 min 是紧的，估计游戏正常跑大约 10-20 min/run

---

## 2. AutoSlayer 控制流（伪代码）

```
PlayRunAsync(seed, ct):
  await wait for NGame.Instance                       # boot
  set fastMode = Fast, ftues off, all chars unlocked, seed override
  use AutoSlayCardSelector for card selection screens
  watchdog = new Watchdog()                           # 30s "did anything happen?" guard
  
  await PlayMainMenuAsync()                           # UI: abandon if needed → singleplayer → random char
  await wait for RunState != null
  
  while (runState.TotalFloor < 49):
    roomType = runState.CurrentRoom.RoomType
    watchdog.Reset(f"Entering {roomType}")
    await _roomHandlers[roomType].HandleAsync(rng, ct)
                                                       # 内部用 IsInProgress / IsPlayPhase polling
    if roomType ∈ {Monster, Elite, Boss}:
      await WaitForRewardsScreenAsync()
    else:
      await Task.Delay(500ms)
    await DrainOverlayScreensAsync()                  # 遍历 NOverlayStack，每屏调 _screenHandlers[type]
    
    if RestSite: try click Proceed button
    if Event:    try click Proceed event option
    if Boss:     wait for act transition; if last act, special victory path
    
    await _mapHandler.HandleAsync(rng, ct)           # 选 next NMapPoint
  
  await AbandonRunAsync(ct)                          # 通过 UI 选 Options → Abandon Run
  QuitGame(exitCode)
```

Handler dispatch 表：

```
Room handlers:
  Monster/Elite/Boss → CombatRoomHandler
  Event → EventRoomHandler
  Shop → ShopRoomHandler
  Treasure → TreasureRoomHandler
  RestSite → RestSiteRoomHandler

Screen handlers (NOverlayStack-driven):
  NRewardsScreen → RewardsScreenHandler
  NCardRewardSelectionScreen → CardRewardScreenHandler
  NDeckUpgradeSelectScreen → DeckUpgradeScreenHandler
  NDeckTransformSelectScreen → DeckTransformScreenHandler
  NDeckEnchantSelectScreen → DeckEnchantScreenHandler
  NDeckCardSelectScreen → DeckCardSelectScreenHandler
  NSimpleCardSelectScreen → SimpleCardSelectScreenHandler
  NChooseACardSelectionScreen → ChooseACardScreenHandler
  NChooseABundleSelectionScreen → ChooseABundleScreenHandler
  NChooseARelicSelection → ChooseARelicScreenHandler
  NGameOverScreen → GameOverScreenHandler
  NCrystalSphereScreen → CrystalSphereScreenHandler
```

---

## 3. 决策：用 AutoSlay 改造成 step API 是否现实

**结论：不要硬改 AutoSlay，但借鉴它的 handler pattern 和 step 原语**。

### 不要硬改的理由

| 问题 | 影响 |
|---|---|
| UI 驱动 (`UiHelper.Click`, `EmitSignal`) | 必须维持渲染、节点路径稳定；游戏更新可能改 Godot 路径 |
| 100ms polling | 每决策点至少 100ms 延迟，吞吐天花板 |
| 大量 `Task.Delay(100~500)` 等动画 | 即使 Instant mode 也会被这些 hardcoded delay 卡住 |
| Watchdog 30s 报警 | 高并发场景下不够稳定 |
| handler 内部硬编码策略 | 不是策略接口，重写也只是替换 handler 实现，没有省事 |
| 设计目标是"测整 run 流程"，不是"高吞吐数据采集" | 错配 |

### 应当借鉴的部分

1. **`ICardSelector` 全局栈模式** — STS2-Gym 应**扩展**到更多决策点（end-turn、card-target、map、event、shop、potion-use）。建议在 mod 启动时把这套 hook 添加到 `MegaCrit.Sts2.Core.Commands/*Cmd` 上（用 Harmony postfix 或独立 hook 注册）
2. **`WaitHelper.Until` 思路** — 但不是 polling，是 **`TaskCompletionSource` 配合 `CombatManager.Instance.TurnEnded` / `CombatSetUp` / `PlayerActionsDisabledChanged` 事件**。事件驱动 → 0 polling 延迟
3. **handler 类型映射的"哪些 screen / room 需要处理"列表** — 直接复用作为 STS2-Gym phase enum 和 obs schema 的依据（map / combat / event / shop / rest / treasure / reward / upgrade / transform / enchant / game_over 这些 phase 全部覆盖）
4. **AutoSlayer.IsActive 全局标志机制** — 我们可以做类似的 `Sts2Gym.IsActive` 标志，Harmony patch 在 mod 启用时关闭 FTUE、autosave 提示、退出确认等 UI 阻塞

### 可以直接复用的辅助类

- ✅ `MegaCrit.Sts2.Core.TestSupport.ICardSelector` + `CardSelectCmd.UseSelector / PushSelector` —— **直接接 RL/LLM policy**
- ✅ `AutoSlayer.IsActive` 检查机制——`NonInteractiveMode.AutoSlayerCheck = () => IsActive` 这种 pattern 启用时可禁掉一堆 UI prompt（参考实现方式）
- ⚠️ `WaitHelper`——可参考接口，不要直接用（100ms polling 太慢）
- ⚠️ `AutoSlayCardSelector`——可作为"random baseline policy"参考实现

### 可以从 AutoSlay 反向工程出来的 step 原语清单

这些是 dev plan §2.3 ActionDispatcher 真正要调的 API（task B 已部分列出，这里补充 AutoSlay 揭示的）：

| 动作 | API | 来源 |
|---|---|---|
| 打牌 | `await CardCmd.AutoPlay(new BlockingPlayerChoiceContext(), card, target)` | CombatRoomHandler |
| 结束回合 | `PlayerCmd.EndTurn(player, canBackOut: false)` | CombatRoomHandler |
| 用药 | `potionModel.EnqueueManualUse(target)` 然后 `Task.Delay(300)` | CombatRoomHandler |
| 应用 power | `await PowerCmd.Apply<TPower>(creature, amount, sourceCreature, null)` 或 `PowerCmd.Apply(powerModel, ...)` | CombatRoomHandler |
| 计算 action mask（合法动作） | `card.CanPlay(out UnplayableReason _, out AbstractModel _)` | CombatRoomHandler |
| 合法目标列表 | `card.CombatState.HittableEnemies` | CombatRoomHandler |
| target type 查 | `card.TargetType` (`AnyEnemy / AnyAlly / AnyPlayer / Self / None`) | CombatRoomHandler |
| phase flag | `CombatManager.Instance.IsPlayPhase` / `IsInProgress` | CombatRoomHandler |
| 进入指定 room | `await runManager.EnterRoomDebug(roomType, ...)` | (task B 已有) |
| seed override | `NGame.Instance.DebugSeedOverride = seed` | PlayRunAsync |
| 跳过 FTUE | `SaveManager.Instance.SetFtuesEnabled(false)` | PlayRunAsync |
| 解锁角色 | `SaveManager.Instance.ObtainEpochOverride(EpochModel.GetId<XEpoch>(), EpochState.Revealed)` | PlayRunAsync |

---

## 4. 不确定项 / hand-off

1. **为什么 AutoSlay 用 `Fast` 而不是 `Instant`** — 推测 Instant 在某些动画-逻辑耦合场景有 corner case。需要在 mod 开发时实测 Instant 是否安全。**留作 P1 milestone 实测**
2. **`Watchdog.Check()` 的具体语义** — 没读 `MegaCrit.Sts2.Core.AutoSlay.Helpers.Watchdog.cs`，只看到 30s timeout、5s log interval。是否会被并发 step 调用时误触发？**留作 P1 调研**
3. **`CombatManager.IsPlayPhase` vs 其他 phase flag** — 看到 `IsInProgress`、`IsPlayPhase`，应该还有 EnemyTurn / TurnStart / TurnEnd 等。需要读 CombatManager 全文。**任务 E 顺带**
4. **`CombatState.HittableEnemies` / `card.CanPlay(...)` 的稳定性** — 是 read-only 实时计算还是缓存？后者意味着我们也许要在 step 调用前强制刷新
5. **`ICardSelector` 的 stack 在网络/重启场景下的生命周期** — `Reset()` 提示 "leaked selector(s)"，说明可能漏 pop。STS2-Gym 用时要保证 mod 卸载/异常时干净弹栈

---

## 5. 给后续任务的备忘

- **任务 D（RNG）**：AutoSlay 通过 `NGame.Instance.DebugSeedOverride = seed` 注入 seed。这是 RNG 控制的入口之一，但只覆盖 main run RNG，combat shuffle 等子流可能独立。任务 D 必查
- **任务 E（Serialization）**：AutoSlay 没用过 `SerializableRun` —— 它跑端到端，不需要序列化。但 dev plan §2.1 仍依赖 task E 的发现
- **SUMMARY**：dev plan §2.3 ActionDispatcher 工作量 = "为关键决策点引入 ICardSelector-style hook stack" + "用事件驱动同步语义替代 polling"，**不是写新的 dispatcher**。中等工作量，主要在设计干净的接口
