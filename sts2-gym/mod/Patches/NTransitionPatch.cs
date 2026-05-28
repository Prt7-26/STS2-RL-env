using System.Threading.Tasks;
using HarmonyLib;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym.Patches;

// Vanilla bug, Day-14 diagnosis: NTransition.RoomFadeIn has an Instant guard at
// the top that short-circuits the VISUAL setup (sets simpleTransition.Modulate.A
// to 0), but the method then falls through to a Fast vs Normal branch where
// FastMode.Instant lands in the "Normal" 0.8s tween case:
//
//     if (!showTransition || FastMode == Instant) { /* set modulate.A=0 only */ }
//     // (no return)
//     if (FastMode == Fast)  _tween.TweenProperty(..., 0.3);
//     else                   _tween.TweenProperty(..., 0.8);  // Instant lands here
//     await _tween.AwaitFinished(this);  // 800ms wait, scene already invisible
//
// Result: every room transition in Instant blocks for ~800ms while the (already
// invisible) tween animates. Measured 840ms/map step in choose_map_node timing.
//
// Fix: Prefix that, in Instant mode, replaces the entire method with a Task
// that completes synchronously after a single frame's grace (so the scene tree
// gets one frame to settle). Identical effect on visible state — the early
// guard already made the transition layer transparent.
[HarmonyPatch(typeof(NTransition), "RoomFadeIn")]
internal static class NTransitionRoomFadeInPatch
{
    static bool Prefix(bool showTransition, ref Task __result)
    {
        FastModeType mode;
        try { mode = SaveManager.Instance?.PrefsSave?.FastMode ?? FastModeType.Normal; }
        catch { return true; }
        if (mode != FastModeType.Instant) return true; // let original run

        // The original mutates a few Modulate / shader-parameter fields before
        // the tween. We don't bother reproducing them — the visual layer is
        // already faded out / set transparent by the time RoomFadeIn is called
        // in Instant mode (RoomFadeOut also Instant-short-circuits and sets
        // baseline transparent state). Just complete the task. One frame's
        // worth of delay is enough margin for in-process scene-tree settling.
        __result = Task.CompletedTask;
        return false;
    }
}
