# Coding Agent 工作手册：STS2-Gym 开发阶段

> 项目已经过了侦察阶段。Mod + Python 客户端 + 文档都到位。这份文档是给**接手继续开发**的 coding agent 用的——快速上下文 + 不再重复踩坑。

---

## 1. 你是谁、用户是谁

- **用户**：机器人 PhD 学生 (lingchao726@gmail.com)。中文交流，技术内容中英混合。**直接、不啰嗦、不堆砌恭维**
- **你**：能读本地文件、跨文件 grep、跟用户对话的 coding agent。**写代码而不是侦察**

**协作硬纪律**：
1. 不确定就问用户，不要自己拍板
2. 不确定的事情明说"不确定"，不瞎猜
3. **不复制反编译代码体到笔记里**（法律红线，见 §6）
4. 每完成一个任务主动告知进度
5. 发现死胡同立刻告诉用户，别硬套
6. 不为漂亮结论省略不利发现

---

## 2. 当前项目状态（2026-05）

**P0 几乎全清**（10.5/11 项），剩余项见 [STS2_GYM_DEV_PLAN.md §12](STS2_GYM_DEV_PLAN.md)。

可工作功能：
- HTTP bridge (mod 内) + 11 个 endpoint
- Gymnasium 风格 env (Discrete 173 action space + Dict obs + action_mask)
- HumanRenderer 双视图 (text + json)
- Action codec 三向（Discrete ↔ Structured ↔ Canonical Text）
- LLMActionParser（鲁棒解析 prose / synonyms / tool-use JSON）
- 完整 phase 覆盖：combat / card_select / map / event / reward / card_reward_select / relic_select / bundle_select / shop / rest / game_over
- `sts2_gym.doctor` + `sts2_gym.install` 自动配 settings.save
- Claude baseline 示例（`sts2_gym.examples.claude_baseline`）

可跑 demos：

```bash
cd sts2-gym/py
python -m sts2_gym.doctor                            # 6 项自检
python -m sts2_gym.random_agent --verbose            # 单战 random
python -m sts2_gym.env_smoke --episodes 1 --verbose  # gym.Env API smoke
python -m sts2_gym.full_run_agent --character IRONCLAD --verbose   # 整 run
```

---

## 3. 工作区结构

```
STS2env/
├── CODING_AGENT_BRIEF.md           # 本文档
├── IMPLEMENTATION_NOTES.md         # 架构沉淀 + 调试 playbook（必读）
├── STS2_GYM_DEV_PLAN.md            # 项目北极星 + §12 实施进度
├── README.md
├── sts2-reverse/                   # 反编译产物 (gitignore，本地)
│   ├── decompiled_dll/             # ILSpy 输出，参考用
│   ├── raw_pck/                    # GDRE 资源
│   ├── sts2.dll / 0Harmony.dll     # 给 mod 链接
│   └── docs/recon/                 # 侦察阶段笔记（local-only，不公开）
└── sts2-gym/                       # ← 主要工作区
    ├── mod/                        # C# mod
    │   ├── Sts2GymMod.cs           # ModInitializer + 9 个 game event hooks
    │   ├── HttpBridge.cs           # HTTP listener + observe/action_mask 构建 + phase resolver
    │   ├── StepRunner.cs           # /step dispatch (play_card / end_turn / select_*)
    │   ├── NonCombatHandlers.cs    # 所有非战斗 phase 的 /step 处理
    │   ├── ScenarioInjector.cs     # /reset Level-A
    │   ├── RunStarter.cs           # /start_run Level-B
    │   ├── ModelRegistry.cs        # /registry (card/monster/relic id→int)
    │   ├── Sts2GymCardSelector.cs  # ICardSelector 实现 (Day-8)
    │   ├── GameThread.cs           # marshal HTTP → Godot main thread
    │   └── Sts2Gym.csproj          # net9.0 class library
    ├── py/sts2_gym/                # Python 包
    │   ├── client.py               # ModBridgeClient (stdlib urllib)
    │   ├── env.py                  # STS2CombatEnv (gym.Env)
    │   ├── renderer.py             # render_text + render_json + strip_bbcode
    │   ├── action_codec.py         # Structured ↔ Canonical Text
    │   ├── llm_parser.py           # LLMActionParser
    │   ├── registry.py             # card_id ↔ int 缓存
    │   ├── random_agent.py         # combat-only random baseline
    │   ├── env_smoke.py            # gym.Env E2E
    │   ├── full_run_agent.py       # 整 run dispatch loop
    │   ├── probe.py / determinism_test.py
    │   ├── doctor.py               # `python -m sts2_gym.doctor`
    │   ├── install.py              # `python -m sts2_gym.install`
    │   ├── test_env_pure.py        # 27 纯函数单测
    │   └── examples/
    │       └── claude_baseline.py  # ~150 行 LLM baseline
    └── scripts/
        ├── smoke_test.sh           # 一键 build + deploy + tail log
        └── unstick.sh              # 手动玩遇 selector 时应急
```

---

## 4. 开发循环

### 4.1 构建 + 部署 + 运行

```bash
# 单一命令搞定 build + deploy 到 STS2 app bundle
cd /Users/mac/code/STS2env/sts2-gym
bash scripts/smoke_test.sh --no-game   # 跳过自动起游戏，自己手动启动
```

输出 mod DLL 到 `<STS2_install>/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym/`。**Mod 改动必须重启 STS2 才能加载新版本**。

### 4.2 测试

```bash
cd sts2-gym/py
python -m sts2_gym.test_env_pure       # 27/27 单测，纯函数，无需游戏
python -m sts2_gym.doctor              # 6 项 self-check
python -m sts2_gym.full_run_agent ...  # E2E 集成测试
```

### 4.3 调试 hung agent

详细 playbook 见 [IMPLEMENTATION_NOTES.md §5](IMPLEMENTATION_NOTES.md#5-调试-playbook)。简要：

```bash
# 1. live state — 看 phase 和 age_ms
curl -s http://127.0.0.1:7777/observe | python3 -m json.tool | head -40

# 2. mod log
grep -E "sts2gym" ~/Library/Application\ Support/SlayTheSpire2/logs/godot.log | tail -30

# 3. 手动 unstick
sts2-gym/scripts/unstick.sh status

# 4. 强杀游戏（HTTP listener 死锁时只能这样）
pkill -9 -f SlayTheSpire2
```

---

## 5. 常见任务模板

### 5.1 加新 phase 处理

详见 [IMPLEMENTATION_NOTES.md §6](IMPLEMENTATION_NOTES.md#6-加新-phase--新-action-的-checklist)。10 步标准化流程。**先看 AutoSlay 同款 handler** —— `sts2-reverse/decompiled_dll/MegaCrit.Sts2.Core.AutoSlay.Handlers.*` 里它怎么点。

### 5.2 修 race / deadlock

详见 [IMPLEMENTATION_NOTES.md §2](IMPLEMENTATION_NOTES.md#2-三条核心架构决策) 和 §5。

要点：
- HTTP listener 单线程，任何 /step 死锁 → 所有请求 hang
- 不能 await 触发 selector 的 backend call → 短 await 模式
- Cache 只在 9 个 subscribed event 触发时刷新
- 死锁解除前 `pkill -9 -f SlayTheSpire2` 强杀

### 5.3 添加 P1 功能

剩余 P1（[STS2_GYM_DEV_PLAN.md §11](STS2_GYM_DEV_PLAN.md)）：
- **FastMode.Instant fix**（Harmony patch NCreature.AnimDie，1-2 天）— 训练速度瓶颈
- **Save/Restore endpoints**（`/save_run` + `/restore_run`，1 天）— MCTS / branching rollout
- **Ascension scaling test**（半天）— 论文严谨性
- **VectorEnv**（process singleton 约束下 1 周+）

---

## 6. 法律红线（不可违反）

1. **`decompiled_dll/` / `raw_pck/` / `sts2.dll` / `0Harmony.dll` 已 gitignore**，**永不上传任何反编译产物到公开仓库**
2. **不在公开渠道分享反编译代码片段**（issues / Slack / Twitter / blog）
3. **笔记可以引用类名、方法签名、namespace 结构**，**不复制方法体的反编译代码**——用自己的话描述行为
4. **本仓库 local-only**——任何 fork / publish 前需要用户审核

---

## 7. 推荐阅读顺序

1. **本文档**（你在读）
2. **[README.md](README.md) §6-§7** — 用户视角的 Python / HTTP API + 法律红线
3. **[IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) §1-§5** — 架构 + quirks + 调试
4. **[STS2_GYM_DEV_PLAN.md](STS2_GYM_DEV_PLAN.md) §11 + §12** — 优先级表 + 实施进度
5. **[sts2-gym/mod/NonCombatHandlers.cs](sts2-gym/mod/NonCombatHandlers.cs)** — 11 个 phase handler 的 reference impl
6. 加新功能前看一遍 **AutoSlay** 的对应 Handler（本地 `sts2-reverse/decompiled_dll/MegaCrit.Sts2.Core.AutoSlay.Handlers.*`，开发机才有），它的 click 流程通常就是正确的

---

## 8. 用户偏好（已多次表达）

- **狠狠推进**：批量做完一组工作再 ship；不要每改一行就 ask 一次
- **不确定就问**：但只问真正的设计决策点，不要为简单选择 ping
- **每个 commit 干净**：commit message 解释**为什么**而不只是"什么"
- **测试一起 ship**：纯函数加 `test_env_pure.py`，端到端用 `full_run_agent.py` 验证
- **错误信息说人话**：用户会读 stack trace，不要藏；mod 端 catch 异常时把 ex.StackTrace 也返
