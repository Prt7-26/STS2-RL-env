# STS2-Gym 实施笔记

这份文档记录**实际写代码过程中**沉淀的架构决策、踩过的坑、和将来接手的人需要知道的"为什么这样做"。

`STS2_GYM_DEV_PLAN.md` 是事前的设计意图；这份是事后的实施事实。两份合在一起读。

---

## 1. 系统拓扑

```
┌─────────────────────────────┐          HTTP/JSON               ┌──────────────────────────┐
│  Python: sts2_gym 包         │  ◄─────  127.0.0.1:7777  ─────►  │  Mod (C# / .NET 9.0)     │
│  ┌─────────┐ ┌─────────┐    │                                  │  ┌──────────────────┐    │
│  │ env.py  │ │renderer │    │  GET  /health                    │  │ HttpBridge.cs    │    │
│  │ Discrete│ │ text+json│   │  GET  /version                   │  │ HttpListener     │    │
│  │ (173)   │ │          │    │  GET  /observe[?partial=1]      │  │ (background      │    │
│  │+selector│ │+strip    │    │  GET  /action_mask               │  │  thread)         │    │
│  └─────────┘ │ BBCode   │    │  GET  /registry                  │  └────────┬─────────┘    │
│  ┌─────────┐ └─────────┘    │  POST /step                      │           │              │
│  │client.py│ ┌─────────┐    │  POST /reset                     │  ┌────────▼─────────┐    │
│  │ urllib  │ │ codec   │    │  POST /start_run                 │  │ StepRunner.cs    │    │
│  │ wrapper │ │ ↔ text  │    │  POST /selector/enable           │  │ + _stepLock      │    │
│  └─────────┘ │ ↔ JSON  │    │  POST /selector/disable          │  │ + GameThread     │    │
│  ┌─────────┐ └─────────┘    │                                  │  │   marshaling     │    │
│  │full_run │ ┌─────────┐    │                                  │  └────────┬─────────┘    │
│  │_agent   │ │llm_parser│   │                                  │           │              │
│  │ phase   │ │ robust   │   │                                  │  ┌────────▼─────────┐    │
│  │ dispatch│ └─────────┘    │                                  │  │ NonCombatHandlers│    │
│  └─────────┘ ┌─────────┐    │                                  │  │ + RunStarter     │    │
│  ┌─────────┐ │registry │    │                                  │  │ + ScenarioInject │    │
│  │doctor.py│ │ +cache  │    │                                  │  │ + ModelRegistry  │    │
│  │install  │ └─────────┘    │                                  │  └────────┬─────────┘    │
│  └─────────┘                │                                  │           │              │
└─────────────────────────────┘                                  │  ┌────────▼─────────┐    │
                                                                 │  │ Sts2GymCardSel-  │    │
                                                                 │  │ ector (impl of   │    │
                                                                 │  │ ICardSelector)   │    │
                                                                 │  └──────────────────┘    │
                                                                 └──────────────────────────┘
```

**Mod 在 STS2 进程内** — Godot 主线程跑游戏，我们的 HTTP listener 跑在后台线程。两者通过 `Callable.From(...).CallDeferred()`（见 `GameThread.RunOnMainAsync`）安全互通。

**Python 端纯 stdlib HTTP** — 不强依赖 numpy / gymnasium / requests。Gymnasium 和 numpy 仅 env.py 用，agent / client / codec / parser / doctor 都能裸跑。

---

## 2. 三条核心架构决策

### 2.1 短 await + agent 接力 sub-screen（最重要）

`/step` 在 mod 端持 `_stepLock`（SemaphoreSlim, 单线程）+ HTTP listener 也是单线程。任何 `/step` 卡住 → **所有请求**包括 `/observe` 全部 timeout。

**绝对不能** `await` 一个会触发我们 selector 的 backend call 到完成，因为：

1. agent /step action → mod 持 `_stepLock`
2. mod 调 `option.OnSelect()` 之类 → 内部触发 `CardSelectCmd.From*` → 我们的 selector 返回 `Task<...>` 挂起，等 /step `select_pick`
3. agent /step `select_pick` → 等 `_stepLock` → 而 `_stepLock` 被 (1) 持着 → **完整死锁**

**统一模式**（已应用到 `PlayCardAsync`、`ChooseEventOptionAsync`、`RestChooseAsync`、`ShopBuyAsync`）：

```csharp
Task<T> task = backendCall();
bool finished = false;
try
{
    await task.WaitAsync(TimeSpan.FromSeconds(1.5));
    finished = true;
}
catch (TimeoutException) { /* sub-screen opened, return early */ }
// Only wait for queue drain when op finished synchronously.
if (finished) { await aqs.BecameEmpty().WaitAsync(...); }
return new { ..., selector_active = Sts2GymMod.Selector.IsActive };
```

`finished=false` 时 `_stepLock` 释放 → agent 看到 `selector_active=true` → 调 select_pick → 老 task 在后台 resume → 自然完成。

**违反这个模式 = HTTP 完全卡死直到游戏强杀**。Day-7 用 ChooseEventOption / Day-10.L 用 rest_choose+shop_buy 都犯过同样的错。

### 2.2 Cache 刷新事件清单 — 易漏的死区

`/observe` 和 `/action_mask` 服务的是 `HttpBridge._cachedFullObs` 等 volatile string，**只在游戏事件触发 `HttpBridge.RefreshObservation()` 时更新**。HTTP 线程不能直接读 Godot scene tree。

订阅的 9 个事件（`Sts2GymMod.Init` + `OnRunStarted` lazy）：

| 事件 | 触发场景 | 没订阅会怎样 |
|---|---|---|
| `RunManager.RunStarted` | 主菜单 → run 开始 | 第一个 obs stale |
| `RunManager.RoomEntered` | 进入新房间 | combat→reward 后 map 进不去 |
| `RunManager.RoomExited` | 离开房间 | 房间 transition 看不到 |
| `CombatManager.CombatSetUp` | 战斗 spawn | 战斗起始 obs stale |
| `CombatManager.CombatEnded` | **战斗结束** | **agent 卡在 combat_pending**（Day-10.J 修过） |
| `CombatManager.CombatWon` | 胜利 | 同上 |
| `CombatManager.TurnStarted` | 回合开始 | 手牌 / 能量 stale |
| `CombatManager.TurnEnded` | 回合结束 | 同上 |
| `CombatManager.PlayerActionsDisabledChanged` | 可操作锁切换 | TurnStarted 太早，这个抓 in-frame 完成时刻 |
| `NOverlayStack.Changed` | overlay push/pop | reward / relic-select / 任何 sub-screen 进不去 |

**`NOverlayStack.Changed` 是 lazy 订阅**：`NOverlayStack.Instance` 在 `RunStarted` 触发时可能还是 null（NRun._Ready 异步），所以 `TryEnsureOverlayStackSubscribed()` 在 5 个 entry point 里都调一次（idempotent，flag 守卫）。最迟到 `CombatSetUp` 必能成功。

**延迟刷新**：`OnCombatEnded` / `OnCombatWon` 立即刷一次 + schedule 一次 700ms 后再刷。`NRewardsScreen` push 经常在 `CombatEnded` 之后一两帧才发生，立即刷只能抓到 IsInProgress=false + no overlay 的过渡态。

### 2.3 UiHelper.Click 优先于直接 API

某些 phase 的"正确"调用方式是**模拟点击 UI 按钮**，而不是直接调底层方法。原因：按钮的 click handler 做了**关键的状态管理**，直接调底层会跳过：

| Phase | 直接调（错） | 正确调（按钮 click） | 直接调缺什么 |
|---|---|---|---|
| Rest site | `option.OnSelect()` | `UiHelper.Click(NRestSiteButton)` | `DisableOptions()` + `AfterSelectingOption()` 退房间 |
| Reward leave | `NOverlayStack.Remove(screen)` | `UiHelper.Click(NProceedButton)` | OnProceedButtonPressed 的 ProceedFromTerminalRewardsScreen |
| Game over | 任何 enabled `NButton` | `UiHelper.Click(NGameOverContinueButton)` 然后 `NReturnToMainMenuButton` | 错点 NDiscoveredItem |
| Card reward sub-screen | `Selector.GetSelectedCardReward()` (sync) | `NCardHolder.EmitSignal(Pressed)` | sync 路径只在无 UI 时跑 |
| Bundle select | `screen.CardsSelected()` | `Hitbox.click` + `NConfirmButton.click` | 两步流程 |

**经验法则**：先看 AutoSlay 的对应 Handler（`MegaCrit.Sts2.Core.AutoSlay.Handlers.*`）怎么做，它的方式就是正确的。AutoSlay 是 MegaCrit 自家的自动化测试框架，碰过的坑跟我们一样。

直接 API（不走 UI）仍然适用于**没有按钮副作用**的场景：`RunManager.EnterMapCoord`、`EventOption.Chosen`、`MerchantEntry.OnTryPurchaseWrapper`、`option.OnSelect()` for rest（仅作为 fallback 验证，不是主路径）。

---

## 3. Phase 处理矩阵

每个 phase 的 `/observe` 字段 + `/step` action 完整列表：

| Phase | /observe 字段 | /step actions | 实现细节 |
|---|---|---|---|
| `combat` | `combat.creatures` `combat.players[0].hand` 等 | `play_card{card_idx, target_combat_id?}` / `end_turn` | `TryManualPlay` + `ActionQueueSet.BecameEmpty()` + 短 await 防 selector deadlock + killing-blow grace 400ms |
| `card_select` (ICardSelector) | `selector.options` `selector.accumulator` | `select_pick{option_idx}` / `unpick` / `confirm` / `skip` | min==max==1 时 pick 自动 confirm |
| `map` | `map.reachable=[{col,row,point_type}]` `map.current` | `choose_map_node{col,row}` | `RunManager.EnterMapCoord` 直调；reachable 是当前节点的 `Children` 或 `Map.startMapPoints` |
| `event` | `event.id` `event.options=[{idx,text_key,was_chosen,is_locked,is_proceed}]` `event.is_finished` | `choose_event_option{option_idx}` | `is_finished=true` 时点击 UI 合成的 PROCEED 按钮（不在 CurrentOptions 里） |
| `reward` | `reward.items=[{idx,reward_type,is_enabled}]` | `take_reward_item{idx}` / `leave_reward_screen` | items 空时 agent 5 次轮询再 leave（防 race）；leave 前检查 NRewardButton 残留 |
| `card_reward_select` | `card_reward_select.cards=[{idx,card_id}]` | `card_reward_pick{idx}` | `NCardHolder.EmitSignal(Pressed)` |
| `relic_select` | `relic_select.items=[{idx,is_enabled}]` | `relic_pick{idx}` | `UiHelper.Click(NClickableControl)` |
| `bundle_select` | `bundle_select.bundles=[{idx,cards:[id...]}]` | `bundle_pick{idx}` | 两步：click `Hitbox` + click `NConfirmButton` |
| `shop` | `shop.items=[{entry_idx,kind,id,cost,is_stocked,enough_gold}]` `shop.player_gold` | `shop_buy{entry_idx}` / `shop_leave` | entry flat-indexed across `CardEntries + RelicEntries + PotionEntries + CardRemovalEntry` |
| `rest` | `rest.options=[{option_idx,option_id,is_enabled}]` | `rest_choose{option_idx}` / `rest_leave` | rest_choose 走 `UiHelper.Click(NRestSiteButton)` |
| `game_over` | `game_over.can_proceed` | `proceed_after_game_over` | 两 stage：`NGameOverContinueButton` → `NReturnToMainMenuButton` |

---

## 4. 已知 quirks（接手前必读）

### 4.1 Phase ambiguity
- **`NRewardsScreen` 和 `NCardRewardSelectionScreen` 都是 "reward" overlay**，必须区分（Day-10.G 修过）
- **`NChooseABundleSelectionScreen` 不走 ICardSelector**，独立 phase（Day-10.O）

### 4.2 Event quirks
- `evt.IsFinished=true` 时 `evt.CurrentOptions` 是空 array，**但 UI 上还有「继续」按钮** — 是 `NEventRoom.SetOptions` 合成的临时 EventOption (`NEventRoom.cs:200-204`)
- 选项的 `IsLocked=true` 时点了无效（OnChosen=null）
- Neow / TheArchitect 等事件有多 stage —— 选完一个 option → `SetEventState` 设新 options → 继续选

### 4.3 Reward quirks
- `_isTerminal=true`（post-combat reward）时点 proceed 调 `ProceedFromTerminalRewardsScreen`（开 map）；`_isTerminal=false` 时调 `NOverlayStack.Remove(this)`（嵌套 reward）。两者都走 `OnProceedButtonPressed`
- **Card reward 不走我们的 ICardSelector** — 点 CardReward 类型的 NRewardButton 会 push NCardRewardSelectionScreen，其 `CardsSelected()` 是 own task，不经过 CardSelectCmd
- "跳过" 按钮的中文 label 误导 — `NProceedButton.IsSkip` 状态下文字是 "跳过"，但还是 proceed 按钮

### 4.4 Rest quirks
- `RestSiteOption.IsEnabled` 没被 game 自动更新 — 必须靠 NRestSiteButton click → `DisableOptions()` 才会 disable
- HEAL / LIFT 完成后**房间不自动退** — 显示 "前进" 按钮，要 `rest_leave`
- SMITH / MEND 选完触发 card-pick selector → 走 ICardSelector path

### 4.5 Combat quirks
- `CardCmd.AutoPlay` **不扣能量**（专门给 WhisperingEarring / KnifeTrap 等用），必须用 `card.TryManualPlay(target)` 才是玩家手动出牌路径
- `FastMode.Instant` 触发 `NCreature.AnimDie` 里 `Node.MoveChild(null)` NRE — 用 `FastMode.Fast`（动画快但有，bit-exact）
- `ActionQueueSet.BecameEmpty()` 返回的 Task **可能立刻 completed**（如果当时队列就是空的）。`PlayerCmd.EndTurn` 是 fire-and-forget 异步入队，调完立刻 `BecameEmpty()` 会得到 already-completed 的 Task —— 不是真的等到回合完。`EndTurnAsync` 改用 poll `IsPlayPhase` 翻转

### 4.6 RNG / Determinism
- `RunRngSet.LoadFromSerializable` 必须配 `player_snapshot`（HP / 牌组 / RelicGrabBag / PlayerRng 全量）才能 bit-exact 重放 — RNG 只是一部分
- `EncounterModel.DebugRandomizeRng()` 是 wall-clock seeded —— **不要调**，让 `_rng=null` 由 `GenerateMonsters` 走 `RunState.Rng.Seed + TotalFloor + encId` 派生
- `/action_mask` 必须和 `/observe` 同事件原子刷新（Day-6.2），否则 mask 和 obs 来自不同 in-frame 状态，trajectory 哈希不一致

---

## 5. 调试 playbook

碰到任何 "agent 卡住" 时：

```bash
# 0. 看 agent 自己的 verbose log（必须 --verbose）
python -m sts2_gym.full_run_agent --verbose ...

# 1. live state — 看现在 cache 在哪个 phase + 多老
curl -s http://127.0.0.1:7777/observe | python3 -m json.tool | head -40

# 2. age_ms 是关键 — 如果 > 5s 说明事件没触发刷新
#    → 找 cache event 没订阅的 transition

# 3. 看 mod log 里 sts2gym 行 — 最近 30 条够诊断 90% 问题
grep -E "sts2gym" ~/Library/Application\ Support/SlayTheSpire2/logs/godot.log | tail -30

# 4. 手动 unstick — 试同 /step action 看 mod 端返回啥
curl -s -X POST -H "Content-Type: application/json" -d '{"type":"<action>","..."}' http://127.0.0.1:7777/step
```

常见症状 → 根因映射：

| 症状 | 最可能根因 |
|---|---|
| `/observe` HTTP timeout | `/step` 死锁了 (单线程 listener) — 检查最近 /step 是否 await 触发 selector 的 backend call |
| phase 一直停在某个状态，age_ms 巨大 | cache 没刷新 — 该 transition 对应的 game event 没订阅 |
| Agent loop 在同一 phase 反复调同一个 action | UI button 状态没更新 — 检查是否绕过了 button click 的 DisableXXX 副作用 |
| 战斗赢了之后卡在 `combat_pending` | CombatEnded 没触发刷新 OR NRewardsScreen push 在我们刷新之后 — 加 delayed refresh |
| selector active 但 cache 显示空 options | selector finalize 后 cache 没刷新 — 是不是从 HTTP 线程调的 RefreshObservation（不能！） |
| Phase 是 `card_select` 但 `selector_active=false` | NChooseACardSelectionScreen / NChooseABundleSelectionScreen 不走 ICardSelector — 它们有自己的独立 phase |

---

## 6. 加新 phase / 新 action 的 checklist

当游戏出现新的没处理过的屏幕：

1. **辨别**：`NOverlayStack.Instance.Peek().GetType().Name` 是哪个 N* 类
2. **找 AutoSlay 同款 handler**：`MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens/<Type>Handler.cs` 里 AutoSlay 怎么点
3. **Phase 命名**：通常用 lowercase + underscores，加到 `HttpBridge.ResolvePhase()` 的 if-chain
4. **暴露 state**：写 `AppendXxxJson(sb)`，在 `AppendNonCombatJson` switch 里加一行；接 `NOverlayStack.Changed` 自动刷新
5. **Handler**：在 `NonCombatHandlers.cs` 写 `XxxAsync`，遵循 §2.1 短 await 模式
6. **Dispatch**：`StepRunner.DispatchAsync` switch 加一行
7. **Client method**：`py/sts2_gym/client.py` 加 `client.xxx(...)`
8. **Agent**：`full_run_agent._do_xxx_step` + 主 loop dispatch
9. **Codec**（可选）：`action_codec.py` 加 canonical text 形式
10. **测试**：`test_env_pure.py` 加一个合成 fixture + roundtrip
11. **Commit**：单一 commit，主题 `Day-X.Y: <phase> handler`

预算：纯增量 phase ~200 行 mod + ~50 行 py。

---

## 7. 不该做 / 已经踩过的反模式

- ❌ **直接 await 触发 selector 的 backend call**（PlayCard / Event / Rest / Shop CardRemoval）
- ❌ **绕过 button click 直接调 option.OnSelect** — rest options 不 disable，房间不退
- ❌ **fallback 静默 force-Remove overlay** — 跳过 reward / 关键状态 transition，agent "成功" 但其实出了 bug（Day-10.K 修过）
- ❌ **从 HTTP 后台线程调 RefreshObservation** — Godot scene tree 不是 thread-safe（Day-8.1 修过）
- ❌ **assume `BecameEmpty()` 的 Task edge 准确** — 队列瞬间空 → 立刻 completed Task，不是真的等到下次空（Day-7.1 修过）
- ❌ **强制订阅 `NOverlayStack.Changed` 在 RunStarted 时** — Instance 还 null，必须 lazy retry（Day-10.J 修过）
- ❌ **CardCmd.AutoPlay 模拟玩家出牌** — 不扣能量（Day-5 修过）
- ❌ **FastMode.Instant** — AnimDie null ref（Day-1 修过）
- ❌ **Phase 命名冲突（`NRewardsScreen` 和 `NCardRewardSelectionScreen` 都 "reward"）** — 必须区分（Day-10.G 修过）
