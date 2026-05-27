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
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
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
        if (options == null || options.Count == 0)
            return (409, "{\"ok\":false,\"error\":\"event has no current options\"}");
        if (idx < 0 || idx >= options.Count)
            return (400, $"{{\"ok\":false,\"error\":\"option_idx out of range\",\"got\":{idx},\"count\":{options.Count}}}");

        var option = options[idx];
        Log.Info($"{Tag} event '{evt.Id.Entry}' choose option idx={idx}");
        try
        {
            await option.Chosen();
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} option.Chosen threw: {ex}");
            return (500, "{\"ok\":false,\"error\":\"option.Chosen threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Settle: events can trigger combat, card selection, etc.
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try { await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(15)); }
            catch (TimeoutException) { /* best effort */ }
        }
        await Task.Delay(200);
        HttpBridge.RefreshObservation();

        return (200, $"{{\"ok\":true,\"action\":\"choose_event_option\",\"option_idx\":{idx}," +
            $"\"event_id\":{JsonStr(evt.Id.Entry)}}}");
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

        Log.Info($"{Tag} clicking reward screen proceed");
        try
        {
            await UiHelper.Click(proceed);
        }
        catch (Exception ex)
        {
            return (500, "{\"ok\":false,\"error\":\"UiHelper.Click threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }

        // Wait for either the screen to close OR the map to open.
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            var top = NOverlayStack.Instance?.Peek();
            if (top != screen) break;
            if (NMapScreen.Instance?.IsOpen == true) break;
            await Task.Delay(50);
        }
        HttpBridge.RefreshObservation();
        return (200, "{\"ok\":true,\"action\":\"leave_reward_screen\"}");
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
