using System;
using System.Threading.Tasks;
using Godot;

namespace Sts2Gym;

/// <summary>
/// Day-5.1 fix: marshaling helper to start async work on Godot's main thread.
///
/// Why this exists: HttpListener serves requests on background threads, so
/// without marshaling `await CardCmd.AutoPlay(...)` runs entirely off-main.
/// Some internal operations (e.g. DISMANTLE's card rename in deck via
/// Godot.Node.Name=...) hit Godot's cross-thread guard:
///
///     ERROR: Changing the name to nodes inside the SceneTree is only
///     allowed from the main thread.
///
/// Side-effect: the scene tree gets left in an inconsistent state and the
/// combat state machine can wedge between turns. dev plan §2.3 already
/// flagged this as the "Godot.Callable.CallDeferred escape hatch".
///
/// Pattern (game's own code in NGame / NTopBar / NCard / CreatureCmd):
///     Callable.From(() => DoSomething()).CallDeferred();
/// which schedules the action to run on the main thread at the next idle
/// frame. We wrap that with TaskCompletionSource to bridge back to async
/// callers (HTTP request handler).
/// </summary>
internal static class GameThread
{
    /// <summary>
    /// Start <paramref name="work"/> on Godot's main thread and await its result.
    ///
    /// The first <c>await</c> inside the produced Task captures Godot's
    /// SynchronizationContext, so subsequent await continuations stay on main
    /// thread (avoiding follow-on cross-thread mutations).
    /// </summary>
    public static Task<T> RunOnMainAsync<T>(Func<Task<T>> work)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);

        Callable.From(() =>
        {
            // We are now on the main thread (CallDeferred runs us at next idle frame).
            try
            {
                var task = work();
                task.ContinueWith(t =>
                {
                    if (t.IsCanceled) tcs.TrySetCanceled();
                    else if (t.IsFaulted)
                    {
                        Exception ex = t.Exception is { InnerExceptions.Count: > 0 } agg
                            ? agg.InnerExceptions[0]
                            : (Exception?)t.Exception ?? new Exception("unknown task fault");
                        tcs.TrySetException(ex);
                    }
                    else tcs.TrySetResult(t.Result);
                });
            }
            catch (Exception ex)
            {
                tcs.TrySetException(ex);
            }
        }).CallDeferred();

        return tcs.Task;
    }

    /// <summary>
    /// Fire-and-forget variant for synchronous main-thread work. Used for
    /// non-awaiting commands (e.g. <c>PlayerCmd.EndTurn</c>) that need to
    /// touch scene tree but whose return value is meaningless.
    /// </summary>
    public static Task RunOnMainSync(Action work)
    {
        var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);

        Callable.From(() =>
        {
            try
            {
                work();
                tcs.TrySetResult(true);
            }
            catch (Exception ex)
            {
                tcs.TrySetException(ex);
            }
        }).CallDeferred();

        return tcs.Task;
    }
}
