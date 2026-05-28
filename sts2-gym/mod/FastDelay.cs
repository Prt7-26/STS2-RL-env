using System;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym;

/// <summary>
/// Day-14 speed-tune: Task.Delay wrapper that scales waits by the current
/// FastMode. Throughout the mod we have ~30 ``await Task.Delay(N)`` calls used
/// to "let the game state propagate after a click" before the next /observe
/// refresh. Those are sized for Normal/Fast animations; under FastMode.Instant
/// the underlying animations are 0-length, so the wait can be shrunk to one or
/// two engine frames (~30-50ms) without breaking the post-click race guard.
///
/// Policy (rough heuristic — favours correctness over a few extra ms):
///
///     Normal  | Fast    | Instant
///     --------+---------+--------
///     full N  | N / 2   | clamp(N / 6, 30, 80) ms
///
/// The Instant floor keeps a single Godot frame available for `_Process` /
/// signal dispatch — going below ~30ms regularly missed the post-click frame
/// in informal probes. The 80ms ceiling guards against the rare case where a
/// caller passed N=2000+ that wouldn't shrink enough at /6 alone.
/// </summary>
internal static class FastDelay
{
    /// <summary>Scale ``ms`` according to the current FastMode setting.</summary>
    public static int Scale(int ms)
    {
        FastModeType mode;
        try { mode = SaveManager.Instance?.PrefsSave?.FastMode ?? FastModeType.Normal; }
        catch { return ms; }

        return mode switch
        {
            // Day-14.9: lowered Instant floor from 30ms -> 15ms. SPEED9 showed
            // combat at 12 step/s, dominated by /step noop's 2-frame marshal
            // floor (16ms @ 60fps). Tightening the in-mod poll cadence to
            // sub-frame (15ms) lets the poll wake on the very next Godot
            // _Process tick after the awaited Task completes, instead of
            // overshooting one whole frame. Ceiling stays at 80ms.
            FastModeType.Instant => Math.Clamp(ms / 6, 15, 80),
            FastModeType.Fast    => ms / 2,
            _                    => ms,
        };
    }

    /// <summary>``await Task.Delay(Scale(ms))`` — drop-in replacement.</summary>
    public static Task Of(int ms) => Task.Delay(Scale(ms));

    /// <summary>
    /// Scale a ``WaitAsync(timeout)`` budget by FastMode. Used for "wait for a
    /// task to either complete OR signal that a sub-screen is in play".
    /// Instant collapses the budget to ~1/5 of the original because the
    /// in-progress task either completes within a frame or two (real fast
    /// path) or never completes synchronously (opened a sub-screen — agent
    /// must drive next). Burning the original 1.5s waiting on the latter is
    /// pure wall-clock waste.
    ///
    ///     Normal  | Fast | Instant
    ///     --------+------+--------
    ///     full ms | ms/2 | clamp(ms / 5, 200, 500)
    /// </summary>
    public static TimeSpan TimeoutOf(int ms)
    {
        FastModeType mode;
        try { mode = SaveManager.Instance?.PrefsSave?.FastMode ?? FastModeType.Normal; }
        catch { return TimeSpan.FromMilliseconds(ms); }
        int scaled = mode switch
        {
            FastModeType.Instant => Math.Clamp(ms / 5, 200, 500),
            FastModeType.Fast    => ms / 2,
            _                    => ms,
        };
        return TimeSpan.FromMilliseconds(scaled);
    }
}
