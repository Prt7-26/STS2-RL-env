using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
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

    // -------------------------------------------------- Reward screen — leave

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
