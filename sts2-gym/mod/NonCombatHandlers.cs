using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Nodes.Screens.TreasureRoomRelic;

namespace Sts2Gym;

/// <summary>
/// Day-10.A: non-combat phase handlers covering the run-loop critical path.
///
/// Four phases shipped here:
///   * Map navigation — choose_map_node (direct API: RunManager.EnterMapCoord)
///   * Event option pick — choose_event_option (direct API: EventOption.Chosen)
///   * Reward screen leave — leave_reward_screen (UI click via Godot signal)
///   * Game over proceed — proceed_after_game_over (UI click)
///
/// Shop + Rest deferred to Day-10.B — they're rarer on the average map and the
/// agent can mostly route around them by picking combat nodes.
///
/// Implementation pattern follows AutoSlay (UiHelper.Click for UI-driven phases,
/// direct API for the rest). All async; all marshaled to game-main-thread by
/// the StepRunner dispatch wrapper.
/// </summary>
internal static class NonCombatHandlers
{
    private const string Tag = "[sts2gym/noncombat]";

    // -------------------------------------------------- Map navigation

    public static async Task<(int, string)> ChooseMapNodeAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("col", out var colProp) || colProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'col'\"}");
        if (!cmd.TryGetProperty("row", out var rowProp) || rowProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'row'\"}");
        int col = colProp.GetInt32();
        int row = rowProp.GetInt32();

        var state = RunManager.Instance.DebugOnlyGetState();
        if (state == null)
            return (409, "{\"ok\":false,\"error\":\"not in a run\"}");
        if (state.Map == null)
            return (409, "{\"ok\":false,\"error\":\"no map for current act\"}");

        // Two legal-source cases:
        //   (1) Already on a map node — children of CurrentMapCoord are legal.
        //   (2) At the start of an act (CurrentMapCoord == null) — the act's
        //       starting nodes are legal (Map.startMapPoints).
        HashSet<MapPoint> legalSet;
        var curCoord = state.CurrentMapCoord;
        if (curCoord.HasValue)
        {
            var curPoint = state.Map.GetPoint(curCoord.Value);
            if (curPoint == null)
                return (409, "{\"ok\":false,\"error\":\"current map coord has no point\"}");
            legalSet = curPoint.Children;
        }
        else
        {
            legalSet = state.Map.startMapPoints;
        }

        var target = legalSet.FirstOrDefault(p => p.coord.col == col && p.coord.row == row);
        if (target == null)
        {
            var legal = string.Join(",", legalSet.Select(p => $"[{p.coord.col},{p.coord.row}]"));
            return (400, $"{{\"ok\":false,\"error\":\"map node not reachable from current location\"," +
                $"\"requested\":[{col},{row}],\"legal\":[{legal}]}}");
        }

        // Day-14.7: log per-segment timing. SPEED7 attributed 15s / 16 map steps
        // (~940ms each) to the agent's map phase — but in Instant most of that
        // ought to be the agent's choose_map_node round-trip, which is
        // dominated by either EnterMapCoord (the full StartCombat → StartTurn
        // chain) or the trailing BecameEmpty drain. Logging breakdown so we
        // can see which one to attack next.
        var tEnter = DateTime.UtcNow;
        try
        {
            Log.Info($"{Tag} EnterMapCoord [{col},{row}] (point_type={target.PointType})");
            await RunManager.Instance.EnterMapCoord(target.coord);
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} EnterMapCoord threw: {ex}");
            return (500, "{\"ok\":false,\"error\":\"EnterMapCoord threw\",\"message\":" + JsonStr(ex.Message) +
                ",\"stack\":" + JsonStr(ex.StackTrace ?? "") + "}");
        }
        var tAfterEnter = DateTime.UtcNow;

        // Wait briefly for the new room to come up.
        await FastDelay.Of(200);
        var tAfterDelay = DateTime.UtcNow;
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(10)); }
            catch (TimeoutException) { /* best effort */ }
        }
        var tAfterDrain = DateTime.UtcNow;
        HttpBridge.RefreshObservation();
        var tAfterRefresh = DateTime.UtcNow;

        Log.Info($"{Tag} choose_map_node timings (ms): enter={(int)(tAfterEnter-tEnter).TotalMilliseconds} " +
                 $"delay={(int)(tAfterDelay-tAfterEnter).TotalMilliseconds} " +
                 $"drain={(int)(tAfterDrain-tAfterDelay).TotalMilliseconds} " +
                 $"refresh={(int)(tAfterRefresh-tAfterDrain).TotalMilliseconds} " +
                 $"total={(int)(tAfterRefresh-tEnter).TotalMilliseconds} point_type={target.PointType}");

        return (200, $"{{\"ok\":true,\"action\":\"choose_map_node\",\"col\":{col},\"row\":{row}," +
            $"\"point_type\":{JsonStr(target.PointType.ToString())}}}");
    }

    // -------------------------------------------------- Event options

    public static async Task<(int, string)> ChooseEventOptionAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("option_idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'option_idx'\"}");
        int idx = idxProp.GetInt32();

        var state = RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as EventRoom;
        if (room == null)
            return (409, $"{{\"ok\":false,\"error\":\"not in an event room\",\"current_room\":{JsonStr(state?.CurrentRoom?.RoomType.ToString() ?? "null")}}}");

        var evt = RunManager.Instance.EventSynchronizer.GetLocalEvent();
        if (evt == null)
            return (500, "{\"ok\":false,\"error\":\"no local event\"}");

        var options = evt.CurrentOptions;
        // Day-10.F: when evt.IsFinished, NEventRoom.SetOptions synthesizes a
        // PROCEED EventOption on the UI side but evt.CurrentOptions stays empty
        // (see NEventRoom.cs:200-204). The synthetic button lives only as an
        // NEventOptionButton in the scene tree. Find + click it directly.
        if (evt.IsFinished && (options == null || options.Count == 0))
        {
            Log.Info($"{Tag} event '{evt.Id.Entry}' IsFinished — clicking synthetic PROCEED button");
            // The event room node is NRun.Instance's current room scene.
            // Find any NEventOptionButton with Option.IsProceed.
            // (UiHelper.FindAll walks the whole scene tree from a root node.)
            var roomNode = NRun.Instance;
            if (roomNode == null)
                return (500, "{\"ok\":false,\"error\":\"NRun.Instance null on finished event\"}");

            NEventOptionButton? proceedBtn = null;
            var deadline = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < deadline && proceedBtn == null)
            {
                proceedBtn = UiHelper.FindAll<NEventOptionButton>(roomNode)
                    .FirstOrDefault(b => b.Option != null && b.Option.IsProceed && !b.Option.IsLocked);
                if (proceedBtn != null) break;
                await FastDelay.Of(100);
            }
            if (proceedBtn == null)
                return (500, "{\"ok\":false,\"error\":\"no PROCEED NEventOptionButton found on finished event\"}");

            try { await UiHelper.Click(proceedBtn); }
            catch (Exception ex)
            {
                return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
            }
            await FastDelay.Of(500);
            HttpBridge.RefreshObservation();
            return (200, $"{{\"ok\":true,\"action\":\"choose_event_option\",\"proceeded_finished_event\":true,\"event_id\":{JsonStr(evt.Id.Entry)}}}");
        }

        if (options == null || options.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"event has no current options\"}");
        if (idx < 0 || idx >= options.Count)
            return (400, $"{{\"ok\":false,\"error\":\"option_idx out of range\",\"got\":{idx},\"count\":{options.Count}}}");

        var option = options[idx];
        Log.Info($"{Tag} event '{evt.Id.Entry}' choose option idx={idx}");

        // Day-10.E: option.Chosen() awaits the FULL effect chain — including any
        // sub-screen the option opens (NChooseARelicSelection for Neow's
        // PRECARIOUS_SHEARS, NCardRewardSelectionScreen for some events, our
        // own ICardSelector for SimpleGrid-based picks, …). We can't await it
        // to completion: the agent's next /step must be free to drive the sub-
        // screen via /step relic_pick / select_pick / …
        //
        // Fire the Task, wait up to 1.5s for early completion (covers
        // immediate-effect options like "lose 5 max HP" with no sub-screen),
        // then return regardless. The agent's next /observe will reveal the
        // resulting phase / selector / relic-select state and dispatch
        // accordingly. Engine continues its async chain in the background.
        Task chosenTask;
        try
        {
            chosenTask = option.Chosen();
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} option.Chosen threw synchronously: {ex}");
            return (500, "{\"ok\":false,\"error\":\"option.Chosen threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        bool finished = false;
        try
        {
            await chosenTask.WaitAsync(FastDelay.TimeoutOf(1500));
            finished = true;
        }
        catch (TimeoutException)
        {
            // Sub-screen opened — let the agent drive next.
        }
        catch (Exception ex)
        {
            Log.Warn($"{Tag} option.Chosen async error: {ex.Message}");
        }
        await FastDelay.Of(200);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"choose_event_option\",\"option_idx\":{idx}," +
            $"\"event_id\":{JsonStr(evt.Id.Entry)},\"option_finished\":{(finished ? "true" : "false")}}}");
    }

    // -------------------------------------------------- Card-reward sub-screen (Day-10.G)

    /// <summary>
    /// Day-10.G: enumerate NCardHolder buttons on the current NCardReward-
    /// SelectionScreen. Mirrors AutoSlay's CardRewardScreenHandler pattern.
    /// </summary>
    public static List<NCardHolder> EnumerateCardRewardHolders()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NCardRewardSelectionScreen;
        if (screen == null) return new();
        return UiHelper.FindAll<NCardHolder>(screen).ToList();
    }

    public static async Task<(int, string)> CardRewardPickAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'idx'\"}");
        int idx = idxProp.GetInt32();

        var cards = EnumerateCardRewardHolders();
        if (cards.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"not on card-reward selection screen\"}");
        if (idx < 0 || idx >= cards.Count)
            return (400, $"{{\"ok\":false,\"error\":\"idx out of range\",\"got\":{idx},\"count\":{cards.Count}}}");

        var holder = cards[idx];
        var cardId = holder.CardNode?.Model?.Id.Entry ?? "?";
        Log.Info($"{Tag} card_reward_pick idx={idx} card_id={cardId}");

        try
        {
            // AutoSlay pattern: emit the Pressed signal directly.
            holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
        }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"EmitSignal threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Wait for the screen to actually close.
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (DateTime.UtcNow < deadline)
        {
            var top = NOverlayStack.Instance?.Peek();
            if (top is not NCardRewardSelectionScreen) break;
            await FastDelay.Of(80);
        }
        await FastDelay.Of(200);
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(5)); }
            catch (TimeoutException) { /* best effort */ }
        }
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"card_reward_pick\",\"idx\":{idx},\"card_id\":{JsonStr(cardId)}}}");
    }

    // -------------------------------------------------- Relic-select screen (Day-10.E)

    /// <summary>
    /// Enumerate clickable buttons on the current NChooseARelicSelection screen.
    /// AutoSlay uses the same pattern (UiHelper.FindAll&lt;NClickableControl&gt;).
    /// </summary>
    public static List<NClickableControl> EnumerateRelicButtons()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as MegaCrit.Sts2.Core.Nodes.Screens.NChooseARelicSelection;
        if (screen == null) return new();
        return UiHelper.FindAll<NClickableControl>(screen).ToList();
    }

    public static async Task<(int, string)> RelicPickAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'idx'\"}");
        int idx = idxProp.GetInt32();

        var buttons = EnumerateRelicButtons();
        if (buttons.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"no relic buttons found (not on relic-select screen?)\"}");
        if (idx < 0 || idx >= buttons.Count)
            return (400, $"{{\"ok\":false,\"error\":\"idx out of range\",\"got\":{idx},\"count\":{buttons.Count}}}");

        var btn = buttons[idx];
        if (!btn.IsEnabled)
            return (409, $"{{\"ok\":false,\"error\":\"relic button disabled\",\"idx\":{idx}}}");

        Log.Info($"{Tag} relic pick idx={idx} type={btn.GetType().Name}");
        try { await UiHelper.Click(btn); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // After clicking, NChooseARelicSelection closes async (fade-out animation),
        // then the awaiting EventOption.Chosen() continuation resumes, then the
        // event's OnChosen function continues — possibly with more SetEventState
        // calls. We need to wait long enough for this whole chain to settle so
        // /observe reflects the post-event-state options. 2s budget.
        var settleDeadline = DateTime.UtcNow.AddSeconds(2.5);
        while (DateTime.UtcNow < settleDeadline)
        {
            await FastDelay.Of(100);
            // Bail early when the relic-select screen is no longer on top.
            var top = NOverlayStack.Instance?.Peek();
            if (top is not MegaCrit.Sts2.Core.Nodes.Screens.NChooseARelicSelection)
            {
                // Continue waiting a little for downstream chain.
                await FastDelay.Of(400);
                break;
            }
        }
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(5)); }
            catch (TimeoutException) { /* best effort */ }
        }
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"relic_pick\",\"idx\":{idx}}}");
    }

    // -------------------------------------------------- Reward screen — take + leave

    /// <summary>
    /// Day-10.C: enumerate the reward screen's NRewardButton[] in screen order.
    /// Used both for /observe (so agents know what's on offer) and for the
    /// take_reward_item handler. Safe to call only on game-main-thread.
    /// </summary>
    public static List<MegaCrit.Sts2.Core.Nodes.Rewards.NRewardButton> EnumerateRewardButtons()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NRewardsScreen;
        if (screen == null) return new();
        return UiHelper.FindAll<MegaCrit.Sts2.Core.Nodes.Rewards.NRewardButton>(screen).ToList();
    }

    public static async Task<(int, string)> TakeRewardItemAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'idx'\"}");
        int idx = idxProp.GetInt32();

        var buttons = EnumerateRewardButtons();
        if (buttons.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"no reward buttons found (not on reward screen?)\"}");
        if (idx < 0 || idx >= buttons.Count)
            return (400, $"{{\"ok\":false,\"error\":\"idx out of range\",\"got\":{idx},\"count\":{buttons.Count}}}");

        var btn = buttons[idx];
        if (!btn.IsEnabled)
            return (409, $"{{\"ok\":false,\"error\":\"reward button disabled\",\"reward_type\":{JsonStr(btn.Reward?.GetType().Name)}}}");

        Log.Info($"{Tag} take reward idx={idx} type={btn.Reward?.GetType().Name}");
        try { await UiHelper.Click(btn); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Card-reward picks open a sub-screen routed through ICardSelector. Wait
        // briefly so the agent's next /observe sees either selector_active or
        // the post-claim state. Gold/potion claims apply immediately.
        await FastDelay.Of(300);
        HttpBridge.RefreshObservation();
        return (200, $"{{\"ok\":true,\"action\":\"take_reward_item\",\"idx\":{idx}," +
            $"\"reward_type\":{JsonStr(btn.Reward?.GetType().Name)}," +
            $"\"selector_active\":{(Sts2GymMod.Selector.IsActive ? "true" : "false")}}}");
    }

    public static async Task<(int, string)> LeaveRewardScreenAsync(JsonElement? cmd = null)
    {
        // Day-14.5: opt-in force flag. When the agent has tried + failed to
        // claim a reward (e.g. potion when all 3 slots full — NRewardButton.
        // IsEnabled stays true but click no-ops), it marks the idx unclaimable
        // and calls leave_reward_screen with force=true. Mod then bypasses the
        // "remaining items" guard and force-Removes the screen.
        bool force = false;
        if (cmd.HasValue && cmd.Value.TryGetProperty("force", out var f) && f.ValueKind == JsonValueKind.True)
            force = true;

        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NRewardsScreen;
        if (screen == null)
            return (409, $"{{\"ok\":false,\"error\":\"not on reward screen\",\"overlay\":{JsonStr(overlay?.GetType().Name ?? "null")}}}");

        var proceed = UiHelper.FindFirst<NProceedButton>(screen);
        if (proceed == null)
            return (500, "{\"ok\":false,\"error\":\"no proceed button found on reward screen\"}");

        var enabled = proceed.IsEnabled;
        Log.Info($"{Tag} reward proceed button: IsEnabled={enabled} force={force}");

        if (!enabled)
        {
            // Hook.ShouldProceedToNextMapPoint(_runState) may need to settle, or
            // taking the last reward fired async cleanup that hasn't finished.
            // Wait up to 2s for the button to enable.
            var enableDeadline = DateTime.UtcNow.AddSeconds(2);
            while (DateTime.UtcNow < enableDeadline && !proceed.IsEnabled)
            {
                await FastDelay.Of(100);
            }
            enabled = proceed.IsEnabled;
            Log.Info($"{Tag} reward proceed button after settle: IsEnabled={enabled}");
        }

        if (!enabled)
        {
            if (force)
            {
                Log.Warn($"{Tag} proceed button disabled but force=true — bypassing via NOverlayStack.Remove");
                try { NOverlayStack.Instance?.Remove(screen); } catch { /* best effort */ }
                await FastDelay.Of(200);
                HttpBridge.RefreshObservation();
                return (200, "{\"ok\":true,\"action\":\"leave_reward_screen\",\"forced\":true}");
            }
            return (409, "{\"ok\":false,\"error\":\"proceed button disabled — Hook.ShouldProceedToNextMapPoint may block leaving\"}");
        }

        try
        {
            await UiHelper.Click(proceed);
        }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Wait for either the screen to close OR the map to open OR phase change.
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            var top = NOverlayStack.Instance?.Peek();
            if (top != screen) break;
            if (NMapScreen.Instance?.IsOpen == true) break;
            if (!RunManager.Instance.IsInProgress) break;
            await FastDelay.Of(80);
        }

        // Day-10.K: only force-Remove if all reward buttons are gone. Otherwise
        // we'd silently skip unclaimed rewards (Day-10.I bug: the agent would
        // poll quickly, see empty items because the screen was still
        // constructing, call leave, our click no-op'd because button-pressed
        // didn't fire, fallback Remove dismissed the screen WITH rewards still
        // claimable — agent walked into the next combat broke). Don't force-
        // Remove when buttons remain; let agent claim them properly.
        if (ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
        {
            var remaining = UiHelper.FindAll<MegaCrit.Sts2.Core.Nodes.Rewards.NRewardButton>(screen)
                                    .Where(b => b.IsEnabled).Count();
            if (remaining > 0 && !force)
            {
                Log.Warn($"{Tag} reward screen still on top + {remaining} reward(s) still claimable — NOT force-removing");
                return (409, $"{{\"ok\":false,\"error\":\"reward screen has {remaining} unclaimed items\",\"hint\":\"call /step take_reward_item first, or retry with force=true to abandon them\",\"remaining\":{remaining}}}");
            }
            if (remaining > 0 && force)
            {
                Log.Warn($"{Tag} reward screen still on top + {remaining} unclaimable — force-removing per agent");
            }
            Log.Warn($"{Tag} reward screen still on top (no claimable items) — forcing Remove");
            try { NOverlayStack.Instance.Remove(screen); } catch { /* best effort */ }
            await FastDelay.Of(300);
        }

        HttpBridge.RefreshObservation();
        return (200, $"{{\"ok\":true,\"action\":\"leave_reward_screen\",\"button_was_enabled\":{(enabled ? "true" : "false")}}}");
    }

    // -------------------------------------------------- Game over — proceed

    public static async Task<(int, string)> ProceedGameOverAsync()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NGameOverScreen;
        if (screen == null)
            return (409, $"{{\"ok\":false,\"error\":\"not on game-over screen\",\"overlay\":{JsonStr(overlay?.GetType().Name ?? "null")}}}");

        // Day-10.K: AutoSlay's GameOverScreenHandler shows the right flow.
        // Stage 1: NGameOverContinueButton — opens the summary panel.
        // Stage 2: NReturnToMainMenuButton — actually returns to main menu.
        // Earlier impl fell through to any-enabled-NButton, which picked
        // NDiscoveredItem (the unlock-history items) and clicked it 5×.
        var clicked = new List<string>();
        var deadline = DateTime.UtcNow.AddSeconds(30);

        // Stage 1: continue button (opens summary).
        var contBtn = UiHelper.FindFirst<MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen.NGameOverContinueButton>(screen);
        if (contBtn != null)
        {
            // Wait for it to become enabled (animations may delay it).
            while (DateTime.UtcNow < deadline && !contBtn.IsEnabled)
                await FastDelay.Of(100);
            if (contBtn.IsEnabled)
            {
                Log.Info($"{Tag} clicking NGameOverContinueButton");
                try { await UiHelper.Click(contBtn); clicked.Add("continue"); }
                catch (Exception ex) { Log.Warn($"{Tag} continue click: {ex.Message}"); }
                await FastDelay.Of(500);
            }
        }

        // Stage 2: return-to-main-menu button (after summary animation).
        MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen.NReturnToMainMenuButton? menuBtn = null;
        while (DateTime.UtcNow < deadline)
        {
            menuBtn = UiHelper.FindFirst<MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen.NReturnToMainMenuButton>(screen);
            if (menuBtn != null && menuBtn.Visible && menuBtn.IsEnabled) break;
            await FastDelay.Of(150);
        }
        if (menuBtn != null && menuBtn.IsEnabled)
        {
            Log.Info($"{Tag} clicking NReturnToMainMenuButton");
            try { await UiHelper.Click(menuBtn); clicked.Add("main_menu"); }
            catch (Exception ex) { Log.Warn($"{Tag} main-menu click: {ex.Message}"); }
            await FastDelay.Of(800);
        }
        else
        {
            Log.Warn($"{Tag} main-menu button never became enabled");
        }

        HttpBridge.RefreshObservation();
        var clickedStr = string.Join(",", clicked);
        return (200, $"{{\"ok\":true,\"action\":\"proceed_after_game_over\",\"clicked\":[\"{clickedStr.Replace(",","\",\"")}\"]}}");
    }

    // -------------------------------------------------- Shop (Day-10.B)

    /// <summary>
    /// Day-10.B: enumerate the merchant's entries in a stable order so the
    /// agent can address them by flat index. Order is the same as
    /// MerchantInventory.AllEntries: character cards, then colorless cards,
    /// then relics, then potions, then card-removal (if present).
    /// </summary>
    public static List<MerchantEntry> FlattenMerchantEntries(MerchantInventory inv)
    {
        var list = new List<MerchantEntry>();
        list.AddRange(inv.CharacterCardEntries);
        list.AddRange(inv.ColorlessCardEntries);
        list.AddRange(inv.RelicEntries);
        list.AddRange(inv.PotionEntries);
        if (inv.CardRemovalEntry != null) list.Add(inv.CardRemovalEntry);
        return list;
    }

    public static async Task<(int, string)> ShopBuyAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("entry_idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'entry_idx'\"}");
        int idx = idxProp.GetInt32();

        var state = RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as MerchantRoom;
        if (room == null)
            return (409, "{\"ok\":false,\"error\":\"not in a merchant room\"}");

        var entries = FlattenMerchantEntries(room.Inventory);
        if (idx < 0 || idx >= entries.Count)
            return (400, $"{{\"ok\":false,\"error\":\"entry_idx out of range\",\"got\":{idx},\"count\":{entries.Count}}}");

        var entry = entries[idx];
        if (!entry.IsStocked)
            return (409, "{\"ok\":false,\"error\":\"entry sold out / unavailable\"}");
        if (!entry.EnoughGold)
            return (409, $"{{\"ok\":false,\"error\":\"not enough gold\",\"cost\":{entry.Cost},\"have\":{(state!.Players.FirstOrDefault()?.Gold ?? 0)}}}");

        Log.Info($"{Tag} shop buy entry_idx={idx} type={entry.GetType().Name} cost={entry.Cost}");
        // Day-10.L: same deadlock guard as RestChooseAsync. CardRemoval entry's
        // OnTryPurchase opens a deck-card-pick selector (CardSelectCmd → our
        // Selector). Awaiting fully would deadlock _stepLock.
        Task<bool> purchaseTask;
        try
        {
            purchaseTask = entry.OnTryPurchaseWrapper(room.Inventory);
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} OnTryPurchaseWrapper threw synchronously: {ex}");
            return (500, "{\"ok\":false,\"error\":\"OnTryPurchaseWrapper threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        bool finished = false;
        bool success = true;
        try
        {
            await purchaseTask.WaitAsync(FastDelay.TimeoutOf(1500));
            finished = true;
            success = purchaseTask.Result;
        }
        catch (TimeoutException) { /* sub-screen opened, agent drives next */ }
        catch (Exception ex) { Log.Warn($"{Tag} purchase async error: {ex.Message}"); }

        if (finished && !success)
            return (409, "{\"ok\":false,\"error\":\"purchase failed (see PurchaseStatus event)\"}");

        if (finished)
        {
            var aqs = RunManager.Instance.ActionQueueSet;
            if (aqs != null)
            {
                try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(5)); }
                catch (TimeoutException) { /* best effort */ }
            }
        }
        await FastDelay.Of(150);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"shop_buy\",\"entry_idx\":{idx},\"type\":{JsonStr(entry.GetType().Name)},\"cost\":{entry.Cost},\"finished\":{(finished ? "true" : "false")},\"selector_active\":{(Sts2GymMod.Selector.IsActive ? "true" : "false")}}}");
    }

    public static async Task<(int, string)> ShopLeaveAsync()
    {
        var state = RunManager.Instance.DebugOnlyGetState();
        if (state?.CurrentRoom is not MerchantRoom)
            return (409, "{\"ok\":false,\"error\":\"not in a merchant room\"}");

        // NRun exposes the current MerchantRoom node directly.
        var roomNode = NRun.Instance?.MerchantRoom;
        if (roomNode == null)
            return (500, "{\"ok\":false,\"error\":\"NMerchantRoom node not found via NRun.Instance.MerchantRoom\"}");

        NClickableControl? btn = roomNode.ProceedButton;
        if (btn == null || !btn.IsEnabled)
        {
            btn = UiHelper.FindFirst<NBackButton>(roomNode);
        }
        if (btn == null)
            return (500, "{\"ok\":false,\"error\":\"no leave/back button found\"}");

        Log.Info($"{Tag} shop leave (button={btn.GetType().Name})");
        try { await UiHelper.Click(btn); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        await FastDelay.Of(300);
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"shop_leave\"}");
    }

    // -------------------------------------------------- Rest (Day-10.B)

    public static async Task<(int, string)> RestChooseAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("option_idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'option_idx'\"}");
        int idx = idxProp.GetInt32();

        var state = RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as RestSiteRoom;
        if (room == null)
            return (409, "{\"ok\":false,\"error\":\"not in a rest site\"}");

        var options = room.Options;
        if (idx < 0 || idx >= options.Count)
            return (400, $"{{\"ok\":false,\"error\":\"option_idx out of range\",\"got\":{idx},\"count\":{options.Count}}}");

        var option = options[idx];
        if (!option.IsEnabled)
            return (409, $"{{\"ok\":false,\"error\":\"option disabled\",\"option_id\":{JsonStr(option.OptionId)}}}");

        Log.Info($"{Tag} rest choose option_idx={idx} option_id={option.OptionId}");

        // Day-10.M: route via NRestSiteButton click, NOT direct option.OnSelect.
        // OnSelect skips the button's DisableOptions + AfterSelectingOption logic
        // (see NRestSiteButton.SelectOption). Symptoms when bypassed:
        //   1. Options stay is_enabled=True forever → agent keeps re-picking.
        //   2. Player gets healed 30+ times → HP back to max.
        //   3. Room never exits → infinite loop.
        // Use UiHelper.Click on the matching button instead. Direct API for the
        // sub-screen drives the rest of the chain (SMITH's card pick still
        // routes through our ICardSelector).
        var roomNode = MegaCrit.Sts2.Core.Nodes.Rooms.NRestSiteRoom.Instance;
        if (roomNode == null)
            return (500, "{\"ok\":false,\"error\":\"NRestSiteRoom.Instance null\"}");

        var buttons = UiHelper.FindAll<MegaCrit.Sts2.Core.Nodes.RestSite.NRestSiteButton>(roomNode)
            .Where(b => b.Option != null && b.Option.OptionId == option.OptionId)
            .ToList();
        if (buttons.Count == 0)
            return (500, $"{{\"ok\":false,\"error\":\"no NRestSiteButton for option_id\",\"option_id\":{JsonStr(option.OptionId)}}}");
        var btn = buttons[0];
        if (!btn.IsEnabled)
            return (409, $"{{\"ok\":false,\"error\":\"rest site button disabled — option already chosen?\",\"option_id\":{JsonStr(option.OptionId)}}}");

        try
        {
            await UiHelper.Click(btn);
        }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // The button click async-runs ChooseLocalOption + DisableOptions. For
        // simple options (HEAL, LIFT) the chain finishes in <1s. SMITH opens a
        // card-select sub-screen via our ICardSelector → /step select_pick will
        // drive it. Same short-wait pattern: 1.5s for finished, else return
        // and let agent observe phase change.
        var settleDeadline = DateTime.UtcNow.AddSeconds(1.5);
        while (DateTime.UtcNow < settleDeadline)
        {
            await FastDelay.Of(100);
            if (Sts2GymMod.Selector.IsActive) break;
            // Room transitioned away (e.g. HEAL completed and went to map).
            var s2 = RunManager.Instance.DebugOnlyGetState();
            if (s2?.CurrentRoom is not RestSiteRoom) break;
        }

        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null && !Sts2GymMod.Selector.IsActive)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(3)); }
            catch (TimeoutException) { /* best effort */ }
        }
        await FastDelay.Of(200);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"rest_choose\",\"option_idx\":{idx},\"option_id\":{JsonStr(option.OptionId)},\"selector_active\":{(Sts2GymMod.Selector.IsActive ? "true" : "false")}}}");
    }

    // -------------------------------------------------- Bundle select (Day-10.O)

    /// <summary>
    /// NChooseABundleSelectionScreen — pick 1 of N card bundles. AutoSlay
    /// pattern: UiHelper.FindAll&lt;NCardBundle&gt; → click bundle.Hitbox →
    /// click NConfirmButton.
    /// </summary>
    public static List<NCardBundle> EnumerateBundles()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NChooseABundleSelectionScreen;
        if (screen == null) return new();
        return UiHelper.FindAll<NCardBundle>(screen).ToList();
    }

    public static async Task<(int, string)> BundlePickAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'idx'\"}");
        int idx = idxProp.GetInt32();

        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NChooseABundleSelectionScreen;
        if (screen == null)
            return (409, "{\"ok\":false,\"error\":\"not on bundle-select screen\"}");

        var bundles = UiHelper.FindAll<NCardBundle>(screen).ToList();
        if (idx < 0 || idx >= bundles.Count)
            return (400, $"{{\"ok\":false,\"error\":\"idx out of range\",\"got\":{idx},\"count\":{bundles.Count}}}");

        Log.Info($"{Tag} bundle_pick idx={idx}");
        try { await UiHelper.Click(bundles[idx].Hitbox); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click(hitbox) threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        await FastDelay.Of(400);

        // Confirm step (separate button per AutoSlay pattern).
        var confirm = UiHelper.FindFirst<NConfirmButton>(screen);
        if (confirm == null)
            return (500, "{\"ok\":false,\"error\":\"NConfirmButton not found after bundle pick\"}");

        try { await UiHelper.Click(confirm); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click(confirm) threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Wait for screen to close.
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (DateTime.UtcNow < deadline)
        {
            var top = NOverlayStack.Instance?.Peek();
            if (top is not NChooseABundleSelectionScreen) break;
            await FastDelay.Of(80);
        }
        await FastDelay.Of(300);
        HttpBridge.RefreshObservation();
        return (200, $"{{\"ok\":true,\"action\":\"bundle_pick\",\"idx\":{idx}}}");
    }

    // -------------------------------------------------- Rest leave (Day-10.N)

    /// <summary>
    /// After a rest option is chosen and resolved, NRestSiteRoom shows a "前进"
    /// (Proceed) button to leave. NRestSiteRoom : IRoomWithProceedButton, so we
    /// can grab .ProceedButton directly. Without this, agent sees options=[]
    /// and just sleeps forever (stuck-detector eventually bails).
    /// </summary>
    public static async Task<(int, string)> RestLeaveAsync()
    {
        var roomNode = MegaCrit.Sts2.Core.Nodes.Rooms.NRestSiteRoom.Instance;
        if (roomNode == null)
            return (409, "{\"ok\":false,\"error\":\"NRestSiteRoom.Instance null (not in a rest room)\"}");
        var btn = roomNode.ProceedButton;
        if (btn == null)
            return (500, "{\"ok\":false,\"error\":\"NRestSiteRoom.ProceedButton null\"}");
        if (!btn.IsEnabled)
            return (409, "{\"ok\":false,\"error\":\"proceed button disabled — option not yet chosen?\"}");

        Log.Info($"{Tag} rest_leave clicking proceed");
        try { await UiHelper.Click(btn); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        await FastDelay.Of(400);
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"rest_leave\"}");
    }

    // -------------------------------------------------- treasure room

    public struct TreasureHolderInfo
    {
        public bool IsEnabled;
        public string? RelicId;
        public string? Rarity;
    }

    public struct TreasureRoomInfo
    {
        public bool ChestOpen;
        public bool CanProceed;
        public List<TreasureHolderInfo> Holders;
    }

    /// <summary>
    /// Best-effort snapshot of the current NTreasureRoom. Returns
    /// ChestOpen=false + empty holders if there's no treasure room visible
    /// (or the scene tree hasn't finished setting up).
    /// </summary>
    public static TreasureRoomInfo PeekTreasure()
    {
        var info = new TreasureRoomInfo { Holders = new List<TreasureHolderInfo>() };
        try
        {
            var room = FindCurrentTreasureRoom();
            if (room == null) return info;
            info.ChestOpen = room.Get(NTreasureRoom.PropertyName._hasChestBeenOpened).AsBool();
            info.CanProceed = room.ProceedButton?.IsEnabled == true;
            foreach (var h in UiHelper.FindAll<NTreasureRoomRelicHolder>(room))
            {
                if (!h.Visible) continue;
                var model = h.Relic?.Model;
                info.Holders.Add(new TreasureHolderInfo
                {
                    IsEnabled = h.IsEnabled,
                    RelicId = model?.Id.Entry,
                    Rarity = model?.Rarity.ToString(),
                });
            }
        }
        catch (Exception ex)
        {
            Log.Warn($"{Tag} PeekTreasure failed: {ex.Message}");
        }
        return info;
    }

    private static NTreasureRoom? FindCurrentTreasureRoom()
    {
        // NTreasureRoom is mounted under /root/Game/RootSceneContainer/Run/RoomContainer/TreasureRoom
        // per AutoSlay's TreasureRoomHandler; UiHelper.FindAll from the scene root
        // surfaces the live instance without us depending on absolute paths.
        var sceneTree = Godot.Engine.GetMainLoop() as Godot.SceneTree;
        var root = sceneTree?.Root;
        if (root == null) return null;
        return UiHelper.FindFirst<NTreasureRoom>(root);
    }

    public static async Task<(int, string)> TreasureOpenAsync()
    {
        var room = FindCurrentTreasureRoom();
        if (room == null)
            return (409, "{\"ok\":false,\"error\":\"no NTreasureRoom in scene tree\"}");

        if (room.Get(NTreasureRoom.PropertyName._hasChestBeenOpened).AsBool())
            return (200, "{\"ok\":true,\"action\":\"treasure_open\",\"already_open\":true}");

        var chest = room.GetNodeOrNull<NClickableControl>("%Chest");
        if (chest == null)
            return (500, "{\"ok\":false,\"error\":\"chest button not found in NTreasureRoom\"}");

        try { await UiHelper.Click(chest); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Chest open animation + RelicCollection populate need a moment.
        await FastDelay.Of(400);
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"treasure_open\"}");
    }

    public static async Task<(int, string)> TreasurePickAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'idx'\"}");
        int idx = idxProp.GetInt32();

        var room = FindCurrentTreasureRoom();
        if (room == null)
            return (409, "{\"ok\":false,\"error\":\"no NTreasureRoom in scene tree\"}");

        var holders = UiHelper.FindAll<NTreasureRoomRelicHolder>(room)
                              .Where(h => h.Visible)
                              .ToList();
        if (holders.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"no visible relic holders — open chest first?\"}");
        if (idx < 0 || idx >= holders.Count)
            return (400, $"{{\"ok\":false,\"error\":\"idx out of range\",\"got\":{idx},\"count\":{holders.Count}}}");

        var holder = holders[idx];
        if (!holder.IsEnabled)
            return (409, $"{{\"ok\":false,\"error\":\"relic holder disabled\",\"idx\":{idx}}}");

        var relicId = holder.Relic?.Model?.Id.Entry;
        try { await UiHelper.Click(holder); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        await FastDelay.Of(400);
        HttpBridge.RefreshObservation();
        return (200, $"{{\"ok\":true,\"action\":\"treasure_pick\",\"idx\":{idx},\"relic_id\":{JsonStr(relicId)}}}");
    }

    public static async Task<(int, string)> TreasureLeaveAsync()
    {
        var room = FindCurrentTreasureRoom();
        if (room == null)
            return (409, "{\"ok\":false,\"error\":\"no NTreasureRoom in scene tree\"}");

        var proceed = room.ProceedButton;
        if (proceed == null)
            return (500, "{\"ok\":false,\"error\":\"no proceed button found in NTreasureRoom\"}");

        // Wait briefly for proceed to enable (claim animations).
        var deadline = DateTime.UtcNow.AddSeconds(2);
        while (DateTime.UtcNow < deadline && !proceed.IsEnabled)
            await FastDelay.Of(80);

        if (!proceed.IsEnabled)
            return (409, "{\"ok\":false,\"error\":\"proceed button not enabled — claim or skip remaining relics first\"}");

        try { await UiHelper.Click(proceed); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        await FastDelay.Of(300);
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"treasure_leave\"}");
    }

    // -------------------------------------------------- helpers

    private static string JsonStr(string? s)
    {
        if (s == null) return "null";
        var sb = new StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (var c in s)
        {
            if (c == '"') sb.Append("\\\"");
            else if (c == '\\') sb.Append("\\\\");
            else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }
}
