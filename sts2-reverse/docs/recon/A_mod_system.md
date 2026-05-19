# 任务 A：STS2 Mod 系统盘点

> Scope：`MegaCrit.Sts2.Core.Modding/` 全部 13 个文件 + `OneTimeInitialization` 调用点。
> 结论一句话：Mega Crit 提供了完整、文档化的官方 mod 加载器（基于 Harmony + Godot PCK），STS2-Gym 的 mod 侧基础设施**几乎免费**，**不需要自己写 patcher**。

---

## 1. 关键回答

### 1.1 Mod 入口形式（问 1）

有**两条并存的入口路径**，由 `ModManager.TryLoadMod` 自动判定：

| 路径 | 触发条件 | 调用机制 |
|---|---|---|
| **显式 initializer** | mod assembly 中存在带 `[ModInitializer("MethodName")]` 的 class | 反射查找该 class 的指定方法（**必须 static**），无参调用 |
| **隐式 PatchAll** | assembly 中没有任何带 `ModInitializer` attribute 的类 | 自动 `new Harmony(author+"."+modId).PatchAll(assembly)` |

- `ModInitializerAttribute` 只允许标在 class 上（`AttributeUsage(Class, Inherited=false)`）
- 一个 assembly 可有多个 ModInitializer 类，全部依次调用；只要有一个返回失败，整个 mod 标记失败
- 如果方法不是 static 却被声明为 initializer，会记录明确错误（"Declare it to be static"），不会 fallback

**对 STS2-Gym 的意义**：我们既可以用 `[ModInitializer]` 显式启动 HTTP server，也可以单纯放 Harmony patch 类、靠 PatchAll 自动挂钩子，二选一不冲突。

### 1.2 Mod 物理形态（问 2）

- **目录形态**，目录里至少包含一份 JSON manifest 文件
- manifest 文件名**可以是任意 `.json`**——`ReadModsInDirRecursive` 把目录下所有 `.json` 当 manifest 试着解析
- 但 DLL / PCK 文件名**强制**为 `<manifest.id>.dll` 和 `<manifest.id>.pck`（path = manifest 所在目录拼接）
- `ModManifest` 字段：`id`(必填)、`name`、`author`、`description`、`version`、`has_dll`、`has_pck`、`dependencies: [string]`、`affects_gameplay`(默认 true)
- **两个来源**：
  - `<exec_dir>/mods/`（递归扫描，子目录嵌套 OK）→ `ModSource.ModsDirectory`
  - Steam Workshop 已订阅的 item（app id `2868840`）→ `ModSource.SteamWorkshop`
- 同一 mod id 不能加载两次（重复 → `MOD_ERROR.DUPLICATE_ID`）

### 1.3 加载时机（问 3）

**很早**。`OneTimeInitialization.ExecuteVeryEarly()` 第一件事就是初始化 `SaveManager`、第二件事是 `ModManager.Initialize(...)`。之后 `ExecuteEssential()` 才做 `ModelDb.Init()`、`LocManager.Init()`、`AtlasManager.LoadEssentialAtlases()`。

这意味着 mod 入口跑在：
1. SettingsSave 已加载 ✅
2. ModelDb / 资源 / 本地化字符串表 **尚未** 初始化 ❌
3. Godot scene tree 未构建 ❌

**对我们的影响**：在 ModInitializer 里**不能**直接访问 `ModelDb.GetById<>()`、不能拿 Godot 节点。要在 mod 入口里**注册延迟回调**（Harmony patch、订阅 `RunManager.RunStarted` 之类的事件、或 `ModHelper.SubscribeFor*StateHooks`），等游戏到合适阶段再动手。

启动命令行参数 `nomods` 完全跳过 mod 加载 — 调试 vanilla 行为时有用。

### 1.4 依赖 / 版本 / 加载顺序（问 4）

完整支持：
- 拓扑排序（kahn-like，`PriorityQueue` 内排序）
- 循环依赖检测（DFS，循环成员标记为 `MOD_ERROR.CIRCULAR_DEPENDENCY` 而不是死循环）
- 缺失依赖检测（`MOD_ERROR.MISSING_DEPENDENCY`）
- 用户在设置里**手动指定优先级**（`ModSettings.ModList`），同 deps 层次内按用户排序展平
- 版本号字段存在（`manifest.version`）但**没看到版本约束语法**（没有 `>=1.2.0` 这种）—— 依赖只是 id 字符串匹配

### 1.5 启用/禁用 / 配置目录 / log（问 5）

- 启用/禁用：`ModSettings.IsModDisabled(id, source)`，per (id, source) 持久化。`ModSource` 不同算不同 mod，便于"同 id 装在 Steam 又装在本地"区分
- **首次启用门**：`PlayerAgreedToModLoading` 标志，未同意时**所有** mod 都被标 `Disabled`。是 mega crit 的安全提示，需要研究者首跑时点过
- log：`MegaCrit.Sts2.Core.Logging.Log.Info/Warn/Error` 静态类，mod 直接调用即可
- 配置目录：没看到 mod 专属配置目录约定，但有 `UserDataPathProvider.IsRunningModded` 标志会影响保存路径——*推测* modded 与 vanilla 存档分开存（待 task E 验证）

### 1.6 运行时拿核心对象的方式（问 6，关键）

**三种合法路径，按推荐度排序：**

#### (a) 单例 + 公开事件（最干净）
- `RunManager.Instance`（公开静态属性）
  - 但 `private RunState? State { get; set; }` — **state 不直接可读**
  - 提供 `public event Action<RunState>? RunStarted` — 订阅事件可拿到 RunState 引用
  - 多个 `SetUp*(RunState state, ...)` 公开方法（SetUpNewSinglePlayer / SetUpSavedSinglePlayer / SetUpTest / SetUpReplay）
- `CombatManager.Instance`（公开静态）
  - 提供 `public CombatState? DebugOnlyGetState()` —— **state 可读**，但名字暗示"仅 debug"，可能在某些路径下返回 null
  - 大量公开事件：`CombatSetUp / CreaturesChanged / TurnStarted / TurnEnded / AboutToSwitchToEnemyTurn / PlayerActionsDisabledChanged`
  - 提供 `public void SetUpCombat(CombatState state)` 公开方法

不确定项 ⚠️：`DebugOnlyGetState()` 是否在非 dev build 中被剔除、是否始终返回当前 state。需在任务 B 或 C 中顺带核实。

#### (b) ModHelper 官方扩展点（专门给 mod 用的）
- `ModHelper.AddModelToPool<TPool, TModel>()` —— 给卡池、敌人池、relic 池追加自定义条目，必须在 `ModelDb` 初始化前调用
- `ModHelper.SubscribeForRunStateHooks(id, del)` —— `del` 签名 `IEnumerable<AbstractModel> (RunState)`，用于给 RunState 注入额外 model（buff/relic 之类）
- `ModHelper.SubscribeForCombatStateHooks(id, del)` —— combat 侧同理
- 这些 hook 是**注入**通道，不是观察通道，不能用来观察状态变化

#### (c) Harmony patch（万能）
- `0Harmony.dll` 已经随游戏分发、`HarmonyLib` 在 `using` 列表里
- 无 ModInitializer 的 mod 会自动 `PatchAll(assembly)`，所以**默认就是 Harmony 友好的**
- 任何 private 字段（包括 `RunManager.State`）都可以 reverse patch / accessor 强读

**对 STS2-Gym 的工程含义**：

- **`/observe` 端点**：可以挂 Harmony postfix 到 `RunManager.SetUp*` / `CombatManager.SetUpCombat` 抓引用；也可以订阅事件。两者都比反射强读 `State` 干净
- **`/serialize`**：拿到 RunState 引用后，复用游戏已有的 `SerializableRun`（在 ModManager 签名里出现过——见任务 E）
- **mod 间通信**：不需要——STS2-Gym 是单 mod

---

## 2. 最小 Hello-World Mod 骨架（伪代码）

> 不是可编译代码，是文件布局 + 概念性 C# 草图。法律红线：不复制反编译方法体。

### 2.1 文件布局

```
<STS2_install>/mods/sts2gym/
├── sts2gym.json          # manifest（文件名其实任意 .json，但用 id 一致最清晰）
└── sts2gym.dll           # 编译后的 assembly，必须叫 <id>.dll
```

### 2.2 sts2gym.json

```json
{
  "id": "sts2gym",
  "name": "STS2 Gym Bridge",
  "author": "<you>",
  "version": "0.0.1",
  "description": "RL/LLM env bridge",
  "has_dll": true,
  "has_pck": false,
  "dependencies": [],
  "affects_gameplay": true
}
```

`affects_gameplay: true` 会让 run 被标记为 modded（影响 leaderboard），对我们的研究目的无所谓但要诚实标。

### 2.3 sts2gym.dll 内容（伪代码 + 引用游戏 API 的概念示意）

```csharp
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Combat;

namespace Sts2Gym;

[ModInitializer(nameof(Init))]
public static class Sts2GymMod
{
    static void Init()
    {
        Log.Info("[sts2gym] hello from mod init");

        // 注意：此刻 ModelDb / Godot 节点尚未就绪。
        // 真正干活的 hook 挂在事件里，等游戏进入合适阶段。

        RunManager.Instance.RunStarted += OnRunStarted;
        CombatManager.Instance.CombatSetUp += OnCombatSetUp;
        CombatManager.Instance.TurnStarted += OnTurnStarted;

        // 之后再起 HTTP server（待任务 B 决定要不要在 Init 里直接起）
        // HttpBridge.Start(port: ResolvePortFromEnv());
    }

    static void OnRunStarted(RunState run)   { Log.Info("[sts2gym] run started"); }
    static void OnCombatSetUp(CombatState s) { Log.Info("[sts2gym] combat set up"); }
    static void OnTurnStarted(CombatState s) { Log.Info("[sts2gym] turn started"); }
}
```

### 2.4 验证 pipeline 通畅的最小动作

1. 装 mod → 启游戏（首次需要在 Mods UI 同意 `PlayerAgreedToModLoading`）
2. 看日志里有没有 `Loaded 1 mods (1 total)` 和 `[sts2gym] hello from mod init`
3. 开一场战斗，看 `[sts2gym] combat set up` 和 `[sts2gym] turn started` 是否出现

如果以上三步都过，整个 modding pipeline（manifest 解析 → dll 加载 → initializer 调用 → 事件订阅）就跑通了，剩下都是接口层工作。

---

## 3. 对 STS2_GYM_DEV_PLAN.md 的工作量影响

| dev plan 组件 | 原假设 | 侦察后判断 |
|---|---|---|
| §2 Mod 框架本身（patcher、加载器） | 需要自己搭 | **几乎免费**，官方完整 |
| §2.6 Transport（HTTP server） | 自己起 | 仍要自己起，但 mod 入口非常清晰 |
| §2.7 实例生命周期（lockfile / 端口） | 自己处理 | 仍要自己处理，与 mod 框架正交 |
| §2.5 RngController（Harmony hook） | 自己写 hook | **Harmony 已就位**，无需引入第三方 patcher |
| §2.3 ActionDispatcher 拿 CombatState | 不确定 | `CombatManager.Instance.DebugOnlyGetState()` 可拿，但需在任务 C 验证可靠性 |

---

## 4. 已识别的注意事项 / 不确定项

1. **`DebugOnlyGetState()` 的"debug only"含义不明** — 可能是 release 构建剔除，可能只是命名警示。如果真剔除，需要 Harmony reverse-patch `RunManager.State` private property。**待任务 C/E 核实**
2. **ModInitializer 跑得太早** — 不能在里面碰 ModelDb / Godot 节点。所有"需要游戏世界"的初始化要 defer 到事件里
3. **PlayerAgreedToModLoading 是 UX 门** — 自动化测试 / Docker 镜像要预先把这个标志置 true，否则 mod 全部 Disabled。位置在 `SettingsSave.ModSettings.PlayerAgreedToModLoading`，需要预热脚本写一份 settings
4. **`affects_gameplay` 的副作用** — 怀疑影响存档兼容、metrics 上报、leaderboard。研究环境不上传 leaderboard 没关系，但 metrics hook（`ModManager.OnMetricsUpload`）的存在提示：游戏对"被改过的 run"有官方记账，对我们想生成 D4RL 风格 offline 数据集**可能反而有用**（说明 run history 有结构化序列化，task E 应顺带看 `SerializableRun`）
5. **Workshop 路径未验证** — 我们应该用本地 `mods/` 路径开发，Workshop 是发布渠道，**目前阶段不碰**
6. **manifest 文件名宽松、dll 文件名严格** — 写文档时要明确，否则用户容易踩坑

---

## 5. 给后续任务的 hand-off 备忘

- 任务 B（DevConsole）：顺便核实 console 命令是否能从 mod 代码里直接 `new XxxConsoleCmd().Execute(...)` 触发——这决定 ScenarioInjector 能否走"调 console 命令"的捷径
- 任务 C（AutoSlay）：核实 `DebugOnlyGetState()` 的真实可用性；看 AutoSlay 是否也走 Harmony 还是用 `ModHelper.SubscribeFor*` 之外的特殊路径
- 任务 E（Serialization）：跟进 `SerializableRun` 类，这是 `ModManager.OnMetricsUpload` 事件携带的对象，很可能是游戏自带的整 run 序列化形式
