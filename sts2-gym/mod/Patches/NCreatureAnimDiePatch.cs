using System;
using System.Threading;
using System.Threading.Tasks;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models.Monsters;
using MegaCrit.Sts2.Core.Nodes.Combat;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym.Patches;

// Vanilla NCreature.AnimDie has an unguarded null-deref when FastMode==Instant:
//   NMonsterDeathVfx nMonsterDeathVfx = NMonsterDeathVfx.Create(this, cancelToken); // returns null in Instant
//   Node parent = GetParent();
//   parent.AddChildSafely(nMonsterDeathVfx);          // ext method null-checks, OK
//   parent.MoveChild(nMonsterDeathVfx, GetIndex());   // Godot native, NRE on first kill
//
// AnimDie is fire-and-forget (see NCreature line 706-707: TaskHelper.RunSafely(task)),
// so the caller never observes the returned Task. Replacing the whole async body in
// Instant mode is safe: the only logic side-effect we must preserve is
// QueueFreeSafely() when shouldRemove (so the engine's combat cleanup proceeds).
[HarmonyPatch(typeof(NCreature), "AnimDie")]
internal static class NCreatureAnimDiePatch
{
    private const string LogTag = "[sts2gym/AnimDiePatch]";

    static bool Prefix(NCreature __instance, bool shouldRemove, CancellationToken cancelToken, ref Task __result)
    {
        var fast = SaveManager.Instance?.PrefsSave?.FastMode;
        if (fast != FastModeType.Instant) return true;

        __result = InstantAnimDieAsync(__instance, shouldRemove, cancelToken);
        return false;
    }

    private static async Task InstantAnimDieAsync(NCreature self, bool shouldRemove, CancellationToken ct)
    {
        try
        {
            if (self.Hitbox != null)
                self.Hitbox.MouseFilter = Control.MouseFilterEnum.Ignore;

            if (shouldRemove)
            {
                try { self.AnimHideIntent(); } catch { /* visual-only */ }

                if (ct.IsCancellationRequested) return;

                self.QueueFreeSafely();
            }

            if (self.Entity?.Monster is Osty)
            {
                try { self.OstyScaleToSize(0f, 0.75f); } catch { /* visual-only */ }
            }
        }
        catch (Exception ex)
        {
            Log.Warn($"{LogTag} InstantAnimDieAsync swallowed: {ex.Message}");
        }
        await Task.CompletedTask;
    }
}
