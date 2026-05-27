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

        // Wait briefly for the new room to come up.
        await Task.Delay(200);
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(10)); }
            catch (TimeoutException) { /* best effort */ }
        }
        HttpBridge.RefreshObservation();

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
                await Task.Delay(100);
            }
            if (proceedBtn == null)
                return (500, "{\"ok\":false,\"error\":\"no PROCEED NEventOptionButton found on finished event\"}");

            try { await UiHelper.Click(proceedBtn); }
            catch (Exception ex)
            {
                return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
            }
            await Task.Delay(500);
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
            await chosenTask.WaitAsync(TimeSpan.FromSeconds(1.5));
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
        await Task.Delay(200);
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
            await Task.Delay(80);
        }
        await Task.Delay(200);
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
            await Task.Delay(100);
            // Bail early when the relic-select screen is no longer on top.
            var top = NOverlayStack.Instance?.Peek();
            if (top is not MegaCrit.Sts2.Core.Nodes.Screens.NChooseARelicSelection)
            {
                // Continue waiting a little for downstream chain.
                await Task.Delay(400);
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
        await Task.Delay(300);
        HttpBridge.RefreshObservation();
        return (200, $"{{\"ok\":true,\"action\":\"take_reward_item\",\"idx\":{idx}," +
            $"\"reward_type\":{JsonStr(btn.Reward?.GetType().Name)}," +
            $"\"selector_active\":{(Sts2GymMod.Selector.IsActive ? "true" : "false")}}}");
    }

    public static async Task<(int, string)> LeaveRewardScreenAsync()
    {
        var overlay = NOverlayStack.Instance?.Peek();
        var screen = overlay as NRewardsScreen;
        if (screen == null)
            return (409, $"{{\"ok\":false,\"error\":\"not on reward screen\",\"overlay\":{JsonStr(overlay?.GetType().Name ?? "null")}}}");

        var proceed = UiHelper.FindFirst<NProceedButton>(screen);
        if (proceed == null)
            return (500, "{\"ok\":false,\"error\":\"no proceed button found on reward screen\"}");

        var enabled = proceed.IsEnabled;
        Log.Info($"{Tag} reward proceed button: IsEnabled={enabled}");

        if (!enabled)
        {
            // Hook.ShouldProceedToNextMapPoint(_runState) may need to settle, or
            // taking the last reward fired async cleanup that hasn't finished.
            // Wait up to 2s for the button to enable.
            var enableDeadline = DateTime.UtcNow.AddSeconds(2);
            while (DateTime.UtcNow < enableDeadline && !proceed.IsEnabled)
            {
                await Task.Delay(100);
            }
            enabled = proceed.IsEnabled;
            Log.Info($"{Tag} reward proceed button after settle: IsEnabled={enabled}");
        }

        if (!enabled)
        {
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
            await Task.Delay(80);
        }

        // Day-10.I extra: if screen STILL there, try direct NOverlayStack.Remove
        // as a last resort. The Released signal sometimes doesn't propagate to
        // OnProceedButtonPressed if the screen consumed it elsewhere.
        if (ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
        {
            Log.Warn($"{Tag} reward screen still on top after click — forcing Remove");
            try { NOverlayStack.Instance.Remove(screen); } catch { /* best effort */ }
            await Task.Delay(300);
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

        // Game-over screen usually has a single "back to main menu" button. Find
        // any NProceedButton or generic NButton via UiHelper.
        var btn = UiHelper.FindFirst<NProceedButton>(screen)
                  ?? (NClickableControl?)UiHelper.FindAll<NButton>(screen).FirstOrDefault(b => b.IsEnabled);
        if (btn == null)
            return (500, "{\"ok\":false,\"error\":\"no enabled button found on game-over screen\"}");

        Log.Info($"{Tag} clicking game-over screen button: {btn.GetType().Name}");
        try { await UiHelper.Click(btn); }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        await Task.Delay(500);
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"proceed_after_game_over\"}");
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
        bool success;
        try
        {
            success = await entry.OnTryPurchaseWrapper(room.Inventory);
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} OnTryPurchaseWrapper threw: {ex}");
            return (500, "{\"ok\":false,\"error\":\"OnTryPurchaseWrapper threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        if (!success)
            return (409, "{\"ok\":false,\"error\":\"purchase failed (see PurchaseStatus event)\"}");

        // Wait for any selector / animations to drain (CardRemoval kicks a card selector).
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(10)); }
            catch (TimeoutException) { /* best effort */ }
        }
        await Task.Delay(150);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"shop_buy\",\"entry_idx\":{idx},\"type\":{JsonStr(entry.GetType().Name)},\"cost\":{entry.Cost}}}");
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
        await Task.Delay(300);
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
        bool ok;
        try
        {
            ok = await option.OnSelect();
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} option.OnSelect threw: {ex}");
            return (500, "{\"ok\":false,\"error\":\"option.OnSelect threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        if (!ok)
            return (409, $"{{\"ok\":false,\"error\":\"OnSelect returned false\",\"option_id\":{JsonStr(option.OptionId)}}}");

        // Wait for any selector / queue (Smith triggers a card-pick selector → ICardSelector path).
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(15)); }
            catch (TimeoutException) { /* best effort */ }
        }
        await Task.Delay(200);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"rest_choose\",\"option_idx\":{idx},\"option_id\":{JsonStr(option.OptionId)}}}");
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
