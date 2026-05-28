using System;
using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Nodes.Vfx;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym.Patches;

// Vanilla bug: TalkCmd.Play computes the speech-bubble duration as
//   num = charCount * (FastMode == Fast ? 0.1 : 0.12)
// — there is NO Instant branch, so under FastMode.Instant a 50-character
// line still takes ~6 seconds (clamped to 0.5s minimum). Same pattern in
// the duration-enum path (VeryShort/Short/Standard/Long/VeryLong).
// Event rooms call TalkCmd for every dialogue line; under Instant the
// agent ends up watching 0.5s-per-line minimum waits between options.
//
// Fix: Prefix-patch TalkCmd.Play and pass through with a forced
// VfxDuration.None when Instant — duration ends up 0 (vanilla still clamps
// to 0.5s minimum in the duration-enum path, so we also clamp our patched
// path by hand). Result: speech bubble disappears in one frame, agent
// moves on instantly. NSpeechBubbleVfx tween animation is still kicked off
// (it just expires immediately).
[HarmonyPatch(typeof(TalkCmd), "Play")]
internal static class TalkCmdPatch
{
    private const string LogTag = "[sts2gym/TalkCmdPatch]";

    static bool Prefix(LocString line, Creature speaker, VfxColor vfxColor, VfxDuration duration, ref NSpeechBubbleVfx? __result)
    {
        FastModeType mode;
        try { mode = SaveManager.Instance?.PrefsSave?.FastMode ?? FastModeType.Normal; }
        catch { return true; }
        if (mode != FastModeType.Instant) return true; // let original run

        if (speaker == null || speaker.IsDead)
        {
            __result = null;
            return false;
        }

        try
        {
            string formattedText = line.GetFormattedText();
            // Force a tiny duration; NSpeechBubbleVfx.Create still constructs
            // the node so any downstream code that checks the return value
            // for null gets a real object back, but the bubble's TweenInterval
            // is 0.05s and the whole thing free's itself almost immediately.
            double dur = 0.05;
            __result = NSpeechBubbleVfx.Create(formattedText, speaker, dur, vfxColor);
            if (__result != null)
            {
                var room = MegaCrit.Sts2.Core.Nodes.Rooms.NCombatRoom.Instance;
                room?.CombatVfxContainer.AddChildSafely(__result);
            }
        }
        catch (Exception ex)
        {
            MegaCrit.Sts2.Core.Logging.Log.Warn($"{LogTag} prefix swallowed: {ex.Message}");
            return true; // fall back to original on any reflection failure
        }
        return false; // skip original
    }
}
