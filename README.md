# STS2-Gym

> Gymnasium-style **RL / LLM** environment bridge for *Slay the Spire 2*.
> 双一等公民设计：RL agent 和 LLM agent 共享同一套底层状态、同一份 reward、同一组 action 接口。

Status: **P0 + P1 大部分完成**，全 run loop 跑通，速度 8 step/s (比起点 7-10×)，4 份 JSON Schema codegen 完成。详细进度见 [`STS2_GYM_DEV_PLAN.md §12`](STS2_GYM_DEV_PLAN.md)。

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、硬性前提](#二硬性前提)
- [三、仓库结构](#三仓库结构)
- [四、安装步骤（首次）](#四安装步骤首次)
- [五、第一次跑通](#五第一次跑通)
- [六、Python API 速查](#六python-api-速查)
- [七、HTTP 端点完整列表](#七http-端点完整列表)
- [八、Action 空间（22 种）](#八action-空间22-种)
- [九、Observation 格式](#九observation-格式)
- [十、Save / Restore](#十save--restore)
- [十一、多实例 (VectorEnv)](#十一多实例-vectorenv)
- [十二、Ascension 测试](#十二ascension-测试)
- [十三、JSON Schemas](#十三json-schemas)
- [十四、性能 & FastMode](#十四性能--fastmode)
- [十五、调试 cheatsheet](#十五调试-cheatsheet)
- [十六、架构图](#十六架构图)
- [十七、已知坑 / 边界](#十七已知坑--边界)
- [十八、开发计划文档](#十八开发计划文档)

---

## 一、它能做什么

把正在运行的 STS2 游戏进程变成一个**可编程的 RL / LLM 环境**：

- **HTTP endpoint**：`/observe`、`/step`、`/start_run`、`/save_run` 等 13 个端点，对游戏进程内的 `RunManager.Instance` / `CombatManager.Instance` 全量读写
- **Gymnasium Env**：`STS2CombatEnv(gym.Env)` 包装好，`obs / reward / terminated / truncated / info` 标准接口
- **三种 obs view**（同一底层状态）：
  - tensor (Dict spaces，给 RL 用)
  - 人类可读 text（给 LLM prose 模式用）
  - 人类可读 json（给 LLM tool-use 模式用）
- **22 种结构化 action**（覆盖 12 个 phase 全部决策点，combat / map / event / reward / shop / rest / treasure / 各种 sub-screen）
- **三向 action codec**：discrete int ↔ structured dict ↔ canonical text
- **鲁棒 LLM action 解析器**：吃 LLM 的 reasoning prose + JSON tool-call，提取出可执行 action
- **整 run 自动 driver** (`full_run_agent`)：从角色选择跑到死或赢
- **Save/Restore**：between-rooms snapshot 通过 `SerializableRun` JSON round-trip
- **VectorEnv**：基于 `GameProcess`，支持 N 个 STS2 进程并行（process singleton 约束）
- **JSON Schema codegen**：4 份 Draft 2020-12 schema 自动生成，drift test 守护

---

## 二、硬性前提

### 2.1 必须有合法 STS2 副本

本仓库**不分发任何游戏文件**（`sts2.dll` / `0Harmony.dll` / 反编译产物全部 gitignore）。需要：

- 通过 Steam 购买并安装 *Slay the Spire 2*（v0.103.2 测试通过，新版本可能需要适配）
- macOS arm64 已测试。Windows / Linux 路径未适配（mod 部署路径是 macOS 专用的 `.app` bundle 内部）

**仓库的 mod 编译需要的本地依赖**（你自己生成，**不要 push**）：

```
sts2-reverse/
├── sts2.dll              # 从你的 STS2 安装目录复制
├── 0Harmony.dll          # 同上
├── GodotSharp.dll        # 同上
└── decompiled_dll/       # 可选：ILSpy 反编译输出（开发时参考用）
```

所有这些路径都在根目录 `.gitignore` 里，确保不会误推到公开仓库。**这是法律红线**：

- 反编译产物（`decompiled_dll/`、`raw_pck/`、`sts2.dll`、`0Harmony.dll`、`*.pck`）和 `sts2-reverse/` 整个目录 **绝对不上传到任何公开仓库**
- 笔记 / 设计文档可以引用游戏的类名、方法签名、namespace 结构，但**不复制反编译方法体**
- 编译 mod 需要的运行时依赖（`sts2.dll` / `0Harmony.dll`）必须由每个开发者从自己合法拥有的 STS2 副本中复制

### 2.2 软件版本

| | 最低版本 | 测试版本 |
|---|---|---|
| **macOS** | 13+ (Apple Silicon) | 14.x |
| **.NET SDK** | 9.0 | 10.0.107 |
| **Python** | 3.10 (用了 `match` / new union syntax) | 3.11 |
| **STS2** | v0.103.2 | v0.103.2 |

### 2.3 Python 依赖

核心 client 是 stdlib only（`urllib` / `json`）。可选依赖：

```bash
# 用 STS2CombatEnv（gym.Env wrapper）
pip install gymnasium numpy

# 跑 Claude baseline
pip install anthropic
```

---

## 三、仓库结构

```
STS2env/
├── README.md                          # ← 本文档
├── STS2_GYM_DEV_PLAN.md               # 项目设计文档（north star）
├── CODING_AGENT_BRIEF.md              # 给接手 coding agent 的速成手册
├── IMPLEMENTATION_NOTES.md            # 架构沉淀 + 调试 playbook（必读）
├── sts2-reverse/                      # ← gitignore，本地生成
│   ├── sts2.dll                       # 需自己拷
│   ├── 0Harmony.dll                   # 需自己拷
│   ├── GodotSharp.dll                 # 需自己拷
│   └── decompiled_dll/                # ILSpy 输出（可选，开发参考）
└── sts2-gym/                          # ← 主代码
    ├── README.md                      # 简版 quickstart
    ├── docs/schemas/                  # 自动生成的 JSON Schema
    │   ├── action.schema.json
    │   ├── observation.schema.json
    │   ├── save_state.schema.json
    │   └── scenario_spec.schema.json
    ├── mod/                           # C# mod（在游戏进程内运行）
    │   ├── Sts2Gym.csproj
    │   ├── sts2gym.json               # mod manifest
    │   ├── Sts2GymMod.cs              # ModInitializer 入口
    │   ├── HttpBridge.cs              # HTTP listener + 端点路由
    │   ├── StepRunner.cs              # /step dispatch（22 action types）
    │   ├── NonCombatHandlers.cs       # 非战斗 phase 处理（map/event/reward/...）
    │   ├── CombatSnapshot.cs          # 战斗状态序列化
    │   ├── ScenarioInjector.cs        # /reset Level-A scenario 注入
    │   ├── RunStarter.cs              # /start_run fresh run
    │   ├── SaveRestore.cs             # /save_run + /restore_run
    │   ├── ModelRegistry.cs           # /registry (id ↔ int 映射)
    │   ├── Sts2GymCardSelector.cs     # ICardSelector 实现
    │   ├── GameThread.cs              # HTTP → Godot main thread marshal
    │   ├── FastDelay.cs               # FastMode-scaled Task.Delay 包装
    │   └── Patches/                   # Harmony patches (fix vanilla Instant bugs)
    │       ├── NCreatureAnimDiePatch.cs   # 解锁 FastMode.Instant
    │       ├── NTransitionPatch.cs        # RoomFadeIn 漏 return 修复
    │       └── TalkCmdPatch.cs            # 对话气泡 Instant 分支
    ├── py/sts2_gym/                   # Python 客户端 + 工具
    │   ├── client.py                  # ModBridgeClient（HTTP + keep-alive）
    │   ├── env.py                     # STS2CombatEnv (gym.Env)
    │   ├── renderer.py                # render_text / render_json / strip_bbcode
    │   ├── action_codec.py            # 结构化 ↔ canonical text
    │   ├── llm_parser.py              # LLMActionParser（鲁棒 prose+JSON）
    │   ├── registry.py                # card/monster/relic id 缓存
    │   ├── schemas.py                 # JSON Schema source-of-truth
    │   ├── gen_schemas.py             # python -m sts2_gym.gen_schemas
    │   ├── process.py                 # GameProcess（spawn + health check）
    │   ├── vector_env.py              # STS2VectorEnv + build_async_vector_env
    │   ├── full_run_agent.py          # 整 run random agent
    │   ├── random_agent.py            # 单战 random agent
    │   ├── env_smoke.py               # gym.Env E2E
    │   ├── doctor.py                  # python -m sts2_gym.doctor (6 项自检)
    │   ├── install.py                 # python -m sts2_gym.install --enable-mods
    │   ├── probe.py / determinism_test.py
    │   ├── bench.py                   # HTTP / mod latency 微基准
    │   ├── save_restore_test.py       # /save_run + /restore_run 烟测
    │   ├── ascension_test.py          # A0/A5/A10 缩放验证
    │   ├── vector_smoke.py            # N=2 进程隔离验证
    │   ├── test_env_pure.py           # 30 纯函数单测（不需要游戏）
    │   └── examples/
    │       └── claude_baseline.py     # ~150 行 LLM baseline
    └── scripts/
        ├── smoke_test.sh              # build + deploy + tail log
        └── unstick.sh                 # 手动 unstick 卡住的 selector
```

---

## 四、安装步骤（首次）

### 4.1 克隆 + 准备 sts2-reverse

```bash
git clone <repo-url> STS2env
cd STS2env
```

把 STS2 安装目录里的依赖 DLL 拷到 `sts2-reverse/`：

```bash
mkdir -p sts2-reverse
STS2_APP="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/MacOS"
cp "$STS2_APP/sts2.dll"         sts2-reverse/
cp "$STS2_APP/0Harmony.dll"     sts2-reverse/
cp "$STS2_APP/GodotSharp.dll"   sts2-reverse/
```

可选：反编译 `sts2.dll` 到 `sts2-reverse/decompiled_dll/`（用 [ILSpy](https://github.com/icsharpcode/ILSpy) 或 [dnSpy](https://github.com/dnSpy/dnSpy)）。**不是必须**，但开发时方便对照游戏源码。

### 4.2 编译并部署 mod

```bash
cd sts2-gym
bash scripts/smoke_test.sh --no-game
```

这个脚本会：
1. `dotnet build -c Release` 编译 mod
2. 把 `sts2gym.dll` 和 `sts2gym.json` 拷到 `<STS2 install>/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym/`
3. 不启动游戏（`--no-game`）

期望输出最后一行：
```
[Done (--no-game, skipping game launch)]
  Mod is deployed. Launch STS2 manually to verify.
```

如果失败：
- "STS2 not installed at: ..." → 用 `STS2_INSTALL=/some/path` 环境变量重指路径
- "'dotnet' not on PATH" → 装 .NET SDK ≥ 9.0
- 编译错误关于找不到类型 → 4.1 步骤的 DLL 没拷对

### 4.3 启用 mod 加载

STS2 默认不加载 mod，需要在 `settings.save` 里设 `mod_settings.mods_enabled = true`。脚本一键搞定：

```bash
cd sts2-gym/py
pip install -e .                        # 装 sts2_gym 包
python -m sts2_gym.install --enable-mods
```

预期输出：
```
[install] patched settings.save: mods_enabled false -> true
```

如果你愿意手动也可以：启动 STS2 一次 → 主菜单弹"是否加载 mod"对话框 → 点同意 → 退出 → 再启动。脚本省了这一步。

### 4.4 启动游戏 + 自检

```bash
open -a "Slay the Spire 2"   # 或者通过 Steam 启动
```

主菜单加载完成后，跑自检：

```bash
cd sts2-gym/py
python -m sts2_gym.doctor
```

应该看到 6 项 ✓：

```
[doctor] 1/6 STS2 install path detected     ✓ /Users/.../Slay the Spire 2
[doctor] 2/6 Mod deployed at correct dir    ✓ .../mods/sts2gym/sts2gym.dll
[doctor] 3/6 settings.save mods_enabled     ✓ true
[doctor] 4/6 /health responds                ✓ port=7777 protocol=1
[doctor] 5/6 /version responds               ✓ mod=0.0.1
[doctor] 6/6 /observe responds               ✓ phase=main_menu
```

任何一项失败照提示修。

### 4.5 验证 mod 加载日志

```bash
grep "sts2gym\|Harmony" ~/Library/Application\ Support/SlayTheSpire2/logs/godot.log | tail -15
```

期望看到：

```
[INFO] [sts2gym] hello — ModInitializer.Init invoked
[INFO] [sts2gym] subscriptions: RunStarted, RoomEntered, RoomExited, CombatSetUp, ...
[INFO] [sts2gym] Harmony patches applied: NCreature.AnimDie, NTransition.RoomFadeIn, TalkCmd.Play
[INFO] [sts2gym/http] listening on http://127.0.0.1:7777/
[INFO] Finished mod initialization for 'STS2-Gym Bridge' (sts2gym).
```

如果 Harmony patches 那行只有 `NCreature.AnimDie`（缺 NTransition / TalkCmd），说明你跑的是老版本 DLL，重新 `bash scripts/smoke_test.sh --no-game`。

---

## 五、第一次跑通

三条不同的入口，挑一条开始：

### 路径 A：整 run random agent（最快出结果）

```bash
cd sts2-gym/py
python -m sts2_gym.full_run_agent --character IRONCLAD --run-seed MYRUN1 --verbose
```

`full_run_agent` 自己调 `/start_run` 开新 run，然后驱动整个 run：map / 战斗 / event / reward / shop / rest / treasure / game_over 全部 phase 自己处理。verbose 模式打每一步在做什么。

预期：
- 大约 20-40s 跑完一次（取决于死得早晚）
- 最后打 summary，含 `step_per_phase`、`seconds_per_phase`、`elapsed_s`、`stopped` 原因
- step/s 应在 **6-10** 之间（FastMode.Instant 全开 + 三个 Harmony patch）

### 路径 B：gym.Env 风格（写自己的 policy）

```python
import gymnasium as gym
import numpy as np
from sts2_gym import STS2CombatEnv

env = STS2CombatEnv(
    character="IRONCLAD",      # 第一次 reset 时自动开新 run
    ascension=0,                # 0..10
    run_seed="MYSEED",          # 同 seed → 同 trajectory
    encounter="CHOMPERS_NORMAL",  # 可选：跳到指定战斗
    partial_obs=False,          # True 隐 RNG state + draw pile 顺序
    reward_mode="sparse",       # 或 "shaped" (每步 HP-delta shaping)
)

obs, info = env.reset()
print(info["text_obs"])         # LLM 可读视图（同一状态）
print(info["action_mask"])      # numpy bool array

for step in range(200):
    legal = np.flatnonzero(info["action_mask"])
    action = int(np.random.choice(legal))    # 替换成你的 policy
    obs, reward, terminated, truncated, info = env.step(action)
    if terminated or truncated:
        break

env.close()
```

### 路径 C：LLM baseline（Claude / GPT）

```bash
export ANTHROPIC_API_KEY=sk-ant-...
cd sts2-gym/py
python -m sts2_gym.examples.claude_baseline --model claude-haiku-4-5
```

完整 baseline 实现在 [`sts2-gym/py/sts2_gym/examples/claude_baseline.py`](sts2-gym/py/sts2_gym/examples/claude_baseline.py) — 约 150 行，包含：
- text obs view（system prompt 里说明规则）
- canonical text action 解析（LLM 输出 `play Strike on A` 这种）
- 失败时的 fallback（random legal action）

替换 LLM 客户端即可改成 GPT / Gemini / 开源模型。

---

## 六、Python API 速查

### 6.1 `ModBridgeClient`

低层 HTTP 客户端，可以脱离 `gym.Env` 直接用：

```python
from sts2_gym import ModBridgeClient

c = ModBridgeClient()           # 默认 127.0.0.1:7777
c.health()                       # → {"status": "ok", ...}
c.version()                      # → 协议版本

# Run 控制
c.start_run("IRONCLAD", ascension=5, seed="X")
c.abandon_run()                  # 强行 CleanUp 当前 run（用于连开多 run）

# 状态读取
obs = c.observe()                # 完整 SerializableRun JSON + phase + combat 子结构
obs = c.observe(partial=True)    # PartialObs view（隐 RNG/draw pile）
obs = c.observe(with_mask=True)  # 内联 action_mask 进 obs (省一次 HTTP)
mask = c.action_mask()           # 单独的 action_mask 视图

# Action 执行 - 战斗
c.step({"type": "play_card", "card_idx": 0, "target_combat_id": 5})
c.step({"type": "end_turn"})

# Action 执行 - 非战斗
c.choose_map_node(col=2, row=3)
c.choose_event_option(0)
c.take_reward_item(0)
c.leave_reward_screen(force=False)  # force=True 强行跳过满槽 potion
c.card_reward_pick(idx=2)
c.relic_pick(idx=0)
c.bundle_pick(idx=1)
c.shop_buy(entry_idx=0); c.shop_leave()
c.rest_choose(option_idx=0); c.rest_leave()
c.treasure_open(); c.treasure_pick(0); c.treasure_leave()
c.proceed_after_game_over()

# Selector（in-combat 卡片选择，如 Survivor 的 discard）
c.enable_selector()
c.step({"type": "select_pick", "option_idx": 0})
c.step({"type": "select_confirm"})
c.step({"type": "select_skip"})
c.disable_selector()

# Save / Restore（仅 between-rooms）
snap = c.save_run()              # 返回完整 SerializableRun
c.restore_run(snap["save"])      # 用 snap reload，CleanUp 当前 run

# 元数据
c.registry()                     # 卡片/怪物/relic id ↔ int 映射 + content_hash

c.close()                        # 关 HTTP 连接
# 或: with ModBridgeClient() as c: ...
```

### 6.2 `STS2CombatEnv`（gym.Env）

```python
from sts2_gym import STS2CombatEnv

env = STS2CombatEnv(
    encounter=None,            # str | None - 跳到指定战斗，None 用当前
    character="IRONCLAD",      # 触发 /start_run
    ascension=0,
    run_seed=None,
    client=None,               # 自定义 ModBridgeClient (多实例时用)
    max_steps=200,             # truncation horizon
    reward_mode="sparse",      # "sparse" 或 "shaped"
    render_mode=None,          # "ansi" | "human"
    partial_obs=False,
    use_registry=True,         # card_id → int (RL tensor encoding)
)

obs, info = env.reset()
# info["text_obs"] —— LLM 可读 prose
# info["action_mask"] —— np.bool_ array
# info["reward_components"] —— 拆分的 reward 分量（给 wrapper 重组）

obs, reward, terminated, truncated, info = env.step(action_int)

env.close()  # 自动 disable_selector
```

环境也注册到 gym：

```python
import gymnasium as gym
env = gym.make("STS2-Combat-v0", character="IRONCLAD")
```

### 6.3 Renderer / Codec / Parser

```python
from sts2_gym import render_text, render_json, strip_bbcode
from sts2_gym import to_text, from_text, ParseError
from sts2_gym import LLMActionParser

obs = c.observe()

# 三种视图
text = render_text(obs)                  # 人类可读 prose（已 strip BBCode）
json_view = render_json(obs)             # tool-use 友好 JSON

# Action codec
text = to_text({"type": "play_card", "card_idx": 0,
                "target_combat_id": 5}, context=obs)
# → "play Strike on A"

action = from_text("play Bash on B", context=obs)
# → {"type": "play_card", "card_idx": 3, "target_combat_id": 6}

# 鲁棒 LLM parser
parser = LLMActionParser(context=obs, on_ambiguity="last",
                         on_parse_fail="raise")
action = parser.parse(
    "I should weaken the front-line first. play Bash on A"
)
# 提取 prose 里的 action，处理同义词、tool-use JSON、cases 不一致
```

### 6.4 Process / Vector

```python
from sts2_gym import GameProcess, STS2VectorEnv

# 单实例（连接已经启动的 STS2）
proc = GameProcess(port=7777, owns_process=False)
proc.client.health()

# 自动 spawn（启 STS2 binary 直接，绕过 Steam）
proc = GameProcess.spawn(port=7778, wait=30)
# ... 用 proc.client ...
proc.close()

# N 实例 VectorEnv
venv = STS2VectorEnv.from_ports([7777, 7778, 7779, 7780],
                                 character="IRONCLAD",
                                 ascension=[0, 0, 5, 10])
# 或 spawn:
venv = STS2VectorEnv.spawn(num_envs=4, base_port=7777,
                            character="IRONCLAD")

obs, info = venv.reset()
obs, r, term, trunc, info = venv.step(action_batch)
venv.close()
```

---

## 七、HTTP 端点完整列表

mod 默认监听 `127.0.0.1:7777`，可用 `STS2GYM_PORT` 环境变量覆盖。

| 端点 | 方法 | 用途 | 实现 |
|---|---|---|---|
| `/health` | GET | 探活 | `HttpBridge.cs` |
| `/version` | GET | 协议 + mod 版本 | 同上 |
| `/observe` | GET (`?partial=1` / `?with_mask=1`) | 完整状态快照 | `HttpBridge.BuildObservation` |
| `/action_mask` | GET | 当前合法 action 集 | `HttpBridge.BuildActionMask` |
| `/step` | POST | 执行 action（22 种） | `StepRunner.HandleStep` |
| `/reset` | POST | 重置到 Combat-level scenario | `ScenarioInjector.HandleReset` |
| `/start_run` | POST | 开新 run | `RunStarter.HandleStartRunAsync` |
| `/abandon_run` | POST | 强行结束当前 run | `HttpBridge` |
| `/save_run` | GET | 完整 run 快照 (`SerializableRun` JSON) | `SaveRestore.HandleSave` |
| `/restore_run` | POST | 加载快照 | `SaveRestore.HandleRestoreAsync` |
| `/selector/enable` | POST | 启用我们的 ICardSelector | `Sts2GymMod.EnableSelector` |
| `/selector/disable` | POST | 禁用 | `Sts2GymMod.DisableSelector` |
| `/registry` | GET | card / monster / relic id ↔ int + content_hash | `ModelRegistry` |

### 示例 - 命令行直接调

```bash
# 查 phase + age
curl -s http://127.0.0.1:7777/observe | python3 -m json.tool | head -20

# 开新 run
curl -X POST -H "Content-Type: application/json" \
     -d '{"character":"IRONCLAD","ascension":0,"seed":"MYRUN"}' \
     http://127.0.0.1:7777/start_run

# 出牌
curl -X POST -H "Content-Type: application/json" \
     -d '{"type":"play_card","card_idx":0,"target_combat_id":5}' \
     http://127.0.0.1:7777/step

# 结束当前 run
curl -X POST http://127.0.0.1:7777/abandon_run
```

每个响应都带 header：

- `X-Sts2Gym-Protocol: 1` — 协议版本
- `X-Snapshot-Age-Ms: N` — cache 多久没刷新

---

## 八、Action 空间（22 种）

完整列表在 [`sts2-gym/docs/schemas/action.schema.json`](sts2-gym/docs/schemas/action.schema.json)（自动生成）。下面是分类速查：

### 8.1 战斗内（5 种）

```python
{"type": "play_card", "card_idx": 0, "target_combat_id": 5}
# 可选: "card_id": "STRIKE_RED", "cost": 1 （advisory，server 不校验）

{"type": "end_turn"}
{"type": "noop"}                           # 同步探针，永远 200
```

### 8.2 ICardSelector 驱动的 sub-screen（4 种）

```python
# 拿任意 in-combat selector / post-combat card pick / deck upgrade / discard 这类
{"type": "select_pick", "option_idx": 0}    # 索引 selector.options
{"type": "select_unpick", "option_idx": 0}  # multi-select 中撤销
{"type": "select_confirm"}                  # 确认当前累积
{"type": "select_skip"}                     # 跳过（min_select == 0 才合法）
```

### 8.3 非战斗 phase（13 种）

```python
# 地图
{"type": "choose_map_node", "col": 2, "row": 3}

# Event
{"type": "choose_event_option", "option_idx": 1}

# 战斗后 reward
{"type": "take_reward_item", "idx": 0}      # 拿一个 reward（gold/potion/relic/card)
{"type": "leave_reward_screen"}             # POST body 可加 "force": true 跳过满槽

# 子屏幕（reward 内嵌）
{"type": "card_reward_pick", "idx": 0}      # 从 3 张候选选一张
{"type": "relic_pick", "idx": 0}            # Neow PRECARIOUS_SHEARS / treasure
{"type": "bundle_pick", "idx": 1}           # NChooseABundleSelectionScreen

# Shop
{"type": "shop_buy", "entry_idx": 0}        # 跨 cards/relics/potions/purge 扁平索引
{"type": "shop_leave"}

# Rest
{"type": "rest_choose", "option_idx": 0}    # REST / SMITH / DIG / MEND
{"type": "rest_leave"}                      # 选完后点"前进"

# Treasure
{"type": "treasure_open"}
{"type": "treasure_pick", "idx": 0}
{"type": "treasure_leave"}

# Game over
{"type": "proceed_after_game_over"}         # 两 stage 自动 chain
```

### 8.4 Canonical text 形式（LLM agent 输出格式）

```
play Strike on A          # 战斗
end turn
select pick 0 / select confirm / select skip
choose map 2,3
choose option 1
take reward 0 / leave reward
card reward pick 0
relic pick 0
bundle pick 1
shop buy 2 / shop leave
rest 0 / rest leave
treasure open / treasure pick 0 / treasure leave
proceed                   # game over
noop
```

### 8.5 三向转换

```python
from sts2_gym import to_text, from_text
from sts2_gym import build_action_mask, decode_action  # Discrete int 编码

# Structured ↔ Text
text = to_text({"type": "play_card", "card_idx": 0, "target_combat_id": 5},
               context=obs)  # → "play Strike on A"
struct = from_text("play Strike on A", context=obs)

# Discrete int ↔ Structured（用于 RL）
# env.action_space = Discrete(173)
# 见 sts2_gym.env 的 ACTION_DIM / *_BASE / *_IDX 常量
```

---

## 九、Observation 格式

`/observe` 返回顶层字段：

```json
{
  "phase": "combat",
  "in_run": true,
  "snapshot_age_ms": 12,
  "partial": false,
  "combat": { ... },           // 仅 combat / card_select / combat_pending 有
  "selector": { ... },         // 仅 selector active 时有
  "map":   { ... },            // 仅 phase=="map" 有
  "event": { ... },
  "reward": { ... },
  "card_reward_select": { ... },
  "relic_select": { ... },
  "bundle_select": { ... },
  "shop": { ... },
  "rest": { ... },
  "treasure": { ... },
  "game_over": { ... },
  "run": { ... }               // 完整 SerializableRun JSON
}
```

完整 schema 在 [`sts2-gym/docs/schemas/observation.schema.json`](sts2-gym/docs/schemas/observation.schema.json)。

### 9.1 Phase 枚举（14 种）

```
main_menu          combat            card_select         relic_select
combat_pending     card_reward_select bundle_select       treasure
map                event             reward              shop
rest               game_over         between_rooms
```

### 9.2 Partial obs 隐藏字段

`?partial=1` 时屏蔽以下（为了 LLM eval 公平性）：

- `run.rng.counters` — RNG state（看到就能预测未来）
- `run.shared_relic_grab_bag.pool` — 剩余 relic 池子内容（数量保留）

### 9.3 派生视图

```python
from sts2_gym import render_text, render_json

text = render_text(obs)
# → "You are playing Ironclad. HP: 56/72. Energy: 3/3.\n\nYour hand:\n  1. Strike..."

json_view = render_json(obs)
# → {"character": "Ironclad", "hp": {"current": 56, "max": 72},
#    "hand": [...], "enemies": [...]}
```

BBCode（`[gold]X[/gold]`、`[red]Cursed[/red]`）都已经在 renderer 入口统一 strip。

---

## 十、Save / Restore

**仅 between-rooms**（map / event / reward / shop / rest / treasure）。Mid-combat 返 409，因为 `SerializableRun` 不包含 `CombatState`（dev plan §2.1 path b 还没做）。

```python
from sts2_gym import ModBridgeClient
c = ModBridgeClient()

# 保存
snap = c.save_run()
# snap = {"ok": True, "schema_version": ..., "ascension": 0,
#         "current_act_index": 0, "rng_streams": 12,
#         "deck_size": 11, "hp": 72,
#         "save": { ...完整 SerializableRun... }}

# 可以本地存盘
import json
with open("checkpoint.json", "w") as f:
    json.dump(snap["save"], f)

# 还原
with open("checkpoint.json") as f:
    save_data = json.load(f)
c.restore_run(save_data)
# 自动 CleanUp 当前 run，然后 SetUpSavedSinglePlayer + LoadRun
```

烟测：

```bash
python -m sts2_gym.save_restore_test --character IRONCLAD --ascension 0 --seed SR1
# → "[saverestore] ✓ round-trip bit-equal on core fields (hp/gold/deck/ascension/act)"
```

---

## 十一、多实例 (VectorEnv)

STS2 是 process singleton（`RunManager.Instance` / `CombatManager.Instance` 全局），所以 N 个并行 env = N 个独立 OS 进程。

### 11.1 手动启 N 个实例

```bash
# 终端 1
STS2GYM_PORT=7777 STS2GYM_PORT_LOCKFILE=/tmp/sts2_gym_7777.port \
    open -na "Slay the Spire 2"

# 终端 2（注意 -n 让 macOS 开新实例而不是聚焦已开的）
STS2GYM_PORT=7778 STS2GYM_PORT_LOCKFILE=/tmp/sts2_gym_7778.port \
    open -na "Slay the Spire 2"
```

然后从 Python：

```python
from sts2_gym import STS2VectorEnv
venv = STS2VectorEnv.from_ports([7777, 7778],
                                 character="IRONCLAD",
                                 ascension=[0, 5])
obs, info = venv.reset()
```

### 11.2 自动 spawn（绕过 Steam）

```python
venv = STS2VectorEnv.spawn(num_envs=4, base_port=7777,
                            character="IRONCLAD")
venv.close()   # 自动 kill 所有 spawn 的 STS2 进程
```

### 11.3 Process 隔离烟测

```bash
python -m sts2_gym.vector_smoke --ports 7777,7778 --ascensions 0,5
# → 验证两实例 ascension 状态互相独立（A0 0 个 AscendersBane, A5 1 个）
```

注意：所有实例共享 `~/Library/Application Support/SlayTheSpire2/` user-data 目录。Save 文件会互相覆盖。我们 `/start_run` 用 `shouldSave: false` 绕开，但 `settings.save` 仍共享。

---

## 十二、Ascension 测试

11 档难度（A0-A10），dev plan §3.6 有完整 ground-truth 表。基础验证：

```bash
python -m sts2_gym.ascension_test --levels 0,5,10
```

期望最后一行：

```
[asc] ✓ all assertions passed across ascensions [0, 5, 10]
```

断言项：

- **max_hp** 不变（不应受 ascension 影响）
- **A4 (TightBelt)**：`max_potion_slot_count` 减 1
- **A5 (AscendersBane)**：deck 多 1 张 `ASCENDERS_BANE` curse
- 去掉 curse 的 base deck 在所有档下相同（同 character + 同 seed）

未覆盖（需要 in-combat probe，TODO）：

- A8 (ToughEnemies) — 怪 HP 上浮
- A9 (DeadlyEnemies) — 怪伤害上浮  
- A3 (Poverty) — gold reward × 0.75

---

## 十三、JSON Schemas

4 份 Draft 2020-12 schema 自动生成：

```bash
cd sts2-gym/py
python -m sts2_gym.gen_schemas           # 写到 ../docs/schemas/
python -m sts2_gym.gen_schemas --check   # CI: drift 检测，非零退出
```

| 文件 | 内容 |
|---|---|
| [action.schema.json](sts2-gym/docs/schemas/action.schema.json) | 22 种 action type 的 oneOf union |
| [observation.schema.json](sts2-gym/docs/schemas/observation.schema.json) | `/observe` 顶层 shape + per-phase 子结构 |
| [save_state.schema.json](sts2-gym/docs/schemas/save_state.schema.json) | `/save_run` / `/restore_run` envelope |
| [scenario_spec.schema.json](sts2-gym/docs/schemas/scenario_spec.schema.json) | `/start_run` body |

source-of-truth 是 [`sts2-gym/py/sts2_gym/schemas.py`](sts2-gym/py/sts2_gym/schemas.py)。改了 schema 必须 regen + commit。

Drift 由 `test_env_pure.py` 守护（3 个 drift test）：

1. `schemas.ACTION_TYPE_SCHEMAS` 必须和 `mod/StepRunner.cs` 的 switch 对齐
2. `action_codec.to_text` 必须覆盖每个 schema'd type
3. `docs/schemas/*.json` 必须和内存里 schema 一致

```bash
python -m sts2_gym.test_env_pure
# → [test] ✓ 30/30 pure-function tests passed
```

---

## 十四、性能 & FastMode

### 14.1 实测数字（Day-14 调优后）

| 指标 | 数值 |
|---|---|
| /observe（cache lookup + JSON copy） | **0.18ms** |
| /observe?with_mask=1 | 0.50ms |
| /step noop（marshal floor） | mean 12.67ms, p95 33ms |
| 整 run agent | **6-10 step/s** |
| Combat phase | **11-12 step/s** |
| Map / Reward / Event | 5-7 step/s |

每个 /step 至少 2 帧 marshal（@ 60 FPS = ~33ms），这是 Godot 主线程 architecture 的硬下限。

### 14.2 FastMode 矩阵

mod 启动时强制 `FastMode = Instant`（在 `Sts2GymMod.OnRunStarted`）。这解锁三个 Harmony patch：

| Patch | 修了什么 vanilla bug |
|---|---|
| [`NCreatureAnimDiePatch`](sts2-gym/mod/Patches/NCreatureAnimDiePatch.cs) | `AnimDie` 调 `parent.MoveChild(null)` NRE（`NMonsterDeathVfx.Create` 在 Instant 返 null） |
| [`NTransitionPatch`](sts2-gym/mod/Patches/NTransitionPatch.cs) | `RoomFadeIn` Instant 分支没 return，淡入 tween 跑满 0.8s |
| [`TalkCmdPatch`](sts2-gym/mod/Patches/TalkCmdPatch.cs) | 对话气泡 char-count 计时只判 Fast 分支，Instant 走 Normal 路径，每 50 字 0.5-6s |

### 14.3 micro-benchmark

```bash
python -m sts2_gym.bench            # 主菜单状态跑 6 项
python -m sts2_gym.bench --combat   # 战斗中加测 /step end_turn
```

输出每项的 min/median/p95/mean 延迟。

### 14.4 Per-phase 时间分解

agent 内置 `seconds_per_phase` 统计：

```
seconds_per_phase = {'event': 0.55, 'map': 1.47, 'combat': 13.68,
                     'reward': 3.76, ...}
```

哪个 phase 占的秒数最大就是当前瓶颈。

---

## 十五、调试 cheatsheet

### 15.1 看活实时状态

```bash
curl -s http://127.0.0.1:7777/observe | python3 -m json.tool | head -40
```

`snapshot_age_ms` > 5000 → cache 没刷新 → 某个 event 没订阅。

### 15.2 看 mod log

```bash
grep -E "sts2gym|ERROR|Exception" \
  ~/Library/Application\ Support/SlayTheSpire2/logs/godot.log | tail -30
```

最近 30 行能诊断 90% 问题。

### 15.3 HTTP listener 卡死

`/step` 是单线程串行，**任何一个 /step deadlock → 后续所有请求 hang**。诊断：

```bash
# 看进程是不是真的活着
pgrep -fl SlayTheSpire2

# 看端口是不是真的有人监听
lsof -iTCP:7777 -sTCP:LISTEN

# 强杀重启
pkill -9 -f SlayTheSpire2 && sleep 2 && open -a "Slay the Spire 2"
```

### 15.4 卡在某个 phase

```bash
# 手动 unstick selector
bash sts2-gym/scripts/unstick.sh status
bash sts2-gym/scripts/unstick.sh skip

# 强行 CleanUp 当前 run
curl -X POST http://127.0.0.1:7777/abandon_run
```

### 15.5 ClashX / 系统代理拦截 localhost

`urllib` 走系统代理时会被 ClashX 之类的 TUN 代理拦截。我们的 `ModBridgeClient` 用的是 `http.client.HTTPConnection`（**直连本地 IP，绕开 proxy 环境变量**），不会有这个问题。但 `urllib.urlopen` 或外部脚本可能会。

```bash
# 验证 mod 端 listener 是不是真的 hang（绕 urllib 直测）
curl -v http://127.0.0.1:7777/health
```

详细 playbook 见 [IMPLEMENTATION_NOTES.md §5](IMPLEMENTATION_NOTES.md#5-调试-playbook)。

---

## 十六、架构图

```
┌───────────────────────────────────────────────────────────────┐
│ Python (你的训练 / eval 代码)                                  │
│ ┌───────────────────┐  ┌─────────────────┐  ┌──────────────┐ │
│ │ STS2CombatEnv     │  │ ModBridgeClient │  │ LLMAction    │ │
│ │ (gym.Env)         │  │ (http.client)   │  │ Parser       │ │
│ └─────────┬─────────┘  └────────┬────────┘  └──────────────┘ │
│           │                     │                              │
│           └─────────┬───────────┘                              │
└─────────────────────┼──────────────────────────────────────────┘
                      │ HTTP keep-alive
                      │ 127.0.0.1:7777
┌─────────────────────▼──────────────────────────────────────────┐
│ STS2 process (1 个 Godot OS process)                           │
│ ┌─────────────────────────────────────────────┐                │
│ │ STS2-Gym mod (sts2gym.dll)                  │                │
│ │ ┌─────────────────┐ ┌─────────────────────┐ │                │
│ │ │ HttpBridge      │ │ Harmony patches     │ │                │
│ │ │ (single-thread  │ │ - AnimDie           │ │                │
│ │ │  HttpListener)  │ │ - RoomFadeIn        │ │                │
│ │ │                 │ │ - TalkCmd           │ │                │
│ │ └────┬────────────┘ └─────────────────────┘ │                │
│ │      │                                       │                │
│ │      │ Callable.From().CallDeferred         │                │
│ │      ▼                                       │                │
│ │ ┌────────────────────────────────────────┐  │                │
│ │ │ Godot main thread                      │  │                │
│ │ │ (StepRunner / NonCombatHandlers)       │  │                │
│ │ │ - RunManager.Instance                  │  │                │
│ │ │ - CombatManager.Instance               │  │                │
│ │ │ - CardSelectCmd.PushSelector           │  │                │
│ │ └────────────────────────────────────────┘  │                │
│ └─────────────────────────────────────────────┘                │
│ Godot 4.x + sts2.dll + 0Harmony.dll                            │
└────────────────────────────────────────────────────────────────┘
```

### 16.1 关键线程模型

- **HTTP listener 单线程**：accept loop 在后台 thread，handler 同步执行。任何 `/step` deadlock → 所有请求 hang
- **`GameThread.RunOnMainAsync`**：HTTP handler 用 `Callable.From(...).CallDeferred()` marshal 到 Godot 主线程执行游戏逻辑
- **Cache 刷新事件 (9 个)**：`RunStarted` / `RoomEntered` / `RoomExited` / `CombatSetUp` / `CombatEnded` / `CombatWon` / `TurnStarted` / `TurnEnded` / `PlayerActionsDisabledChanged` + 懒订阅 `NOverlayStack.Changed`
- **短 await 模式**：触发 selector 的 backend call 不能 await 到底（会 deadlock 单线程 listener），统一用 `WaitAsync(timeout)` + 早返回 + agent 接力

详细见 [IMPLEMENTATION_NOTES.md §2](IMPLEMENTATION_NOTES.md#2-三条核心架构决策)。

---

## 十七、已知坑 / 边界

### 17.1 进程单例

- 一个 OS 进程内只能有一个 active RunState / CombatState
- VectorEnv N 个 env = N 个 OS 进程，每个绑独立 port
- 同一 Python 进程内不能并发持有两个不同 ascension 的 env（reset 切 ascension 后旧 obs handle 必须丢）

### 17.2 Mid-combat save 不支持

`SerializableRun` 不包含 `CombatState`（round / hand / draw pile / enemy intent）。`/save_run` mid-combat 返 409。修法是 dev plan §2.1 path (b) 的 `SerializableCombatState`，**未实现**。

### 17.3 ClashX / 代理

`ModBridgeClient` 用 `http.client.HTTPConnection` 直连，**不受** `HTTP_PROXY` / `HTTPS_PROXY` 影响。但 `urllib.urlopen` / `requests` 默认会走代理，需要自己设 `NO_PROXY="localhost,127.0.0.1"`。

### 17.4 macOS app bundle 路径

mod 必须放在 `<install>/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym/`（注意是 `.app` bundle **内部**，不是 install 根目录）。`smoke_test.sh` 自动处理。Steam 重新验证文件可能擦掉 mod，重 deploy 即可。

### 17.5 STS2 版本漂移

游戏在 EA，版本更新会改 `sts2.dll` 内部接口。我们 mod 引用 `Reference Include="sts2"` + `<Private>false</Private>`，运行时绑定。`/registry` 端点暴露 `content_hash` + `game_version`，Python 端可检测漂移。

### 17.6 Reward screen "satisfied" 槽位

PotionReward 在 3 槽满时 `NRewardButton.IsEnabled` **仍是 true**（游戏 UI bug），但 click 静默失败。agent 端 retry 3 次后用 `force=true` 跳过：

```python
c.leave_reward_screen(force=True)
```

### 17.7 不在公开仓库放反编译产物

`decompiled_dll/` / `raw_pck/` / `sts2.dll` / `0Harmony.dll` / `sts2-reverse/` 整个目录已 gitignore。**不要**让它们进 commit。原则：

- 不在公开仓库 / issue / PR / blog / Twitter 等任何公开渠道分享反编译源码片段
- 笔记可以引用类名、方法签名、namespace 结构（如 `MegaCrit.Sts2.Core.Combat.CombatManager`），**不复制反编译方法体**
- 描述游戏行为时用自己的话总结，不直接搬代码

---

## 十八、开发计划文档

| 文档 | 内容 |
|---|---|
| [STS2_GYM_DEV_PLAN.md](STS2_GYM_DEV_PLAN.md) | 项目设计文档，§0-§11 是设计，§12 是实施进度跟踪 |
| [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) | 架构沉淀 + 已知 quirks + 调试 playbook + 加 phase checklist |
| [CODING_AGENT_BRIEF.md](CODING_AGENT_BRIEF.md) | 给接手 coding agent 的速成手册 |
| [sts2-gym/README.md](sts2-gym/README.md) | 简版 quickstart（本文档是详细版） |

### 当前进度（截至 Day-14 收尾）

- ✅ P0 全部完成（13 个里程碑）
- ✅ FastMode.Instant 解锁 + 三个 Harmony patches
- ✅ Save/Restore (between-rooms)
- ✅ Ascension 缩放（start-of-run）
- ✅ VectorEnv（process 隔离）
- ✅ JSON Schema codegen + drift test
- ✅ 整 run loop 6-10 step/s（比起点 7-10×）
- ⚠️ 待运行时验证：Save/Restore round-trip / Ascension test / VectorEnv N=2
- ❌ Mid-combat save (path b)
- ❌ In-combat ascension test (A8/A9/A3)
- ❌ Offline 数据集 / Docker / 多语言

---

## License / Distribution

- **本仓库代码**（mod 源码 + Python 包）默认 MIT 风格（你想加 LICENSE 就加）
- **依赖**：编译需要本地 STS2 安装的 `sts2.dll` / `0Harmony.dll` —— 不分发
- **运行**：需要每个用户自己有合法 STS2 副本
- **反编译产物**：local-only，永不入公开仓库（见 §2.1 / §17.7 法律红线）

---

## 联系 / 反馈

代码 + 文档作者：lingchao726@gmail.com

发 issue / PR 之前先看一眼 [CODING_AGENT_BRIEF.md](CODING_AGENT_BRIEF.md) §1 "你是谁、用户是谁"。
