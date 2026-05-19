# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 当前阶段 ⚠️

**这个仓库当前处于"侦察阶段"，不写代码、不开发 mod。** 任务是阅读反编译出的 STS2 源码、做笔记、回答问题，为后续构建 STS2-Gym 收集情报。

侦察任务的具体指令在 [CODING_AGENT_BRIEF.md](CODING_AGENT_BRIEF.md) 里（任务 A-E + SUMMARY，按顺序执行，产出写到 `sts2-reverse/docs/recon/`）。

## 协作纪律（用户硬性要求）

1. **如果有不太确定的一定要问用户，不要自己做决策。**
2. **不确定的事情明说"不确定"**，不要为了显得自信而瞎猜。
3. **不要复制粘贴反编译出的代码块到笔记里**。用自己的话总结类的行为（这是法律红线，见下）。
4. **每完成一个任务，主动告知用户进度**，并把当前任务文档的简要结论放在对话里。
5. **发现金矿目录其实是死胡同时，立刻告诉用户**，不要硬把它套进 dev plan。
6. **不要为了让结论更"漂亮"而省略不利发现。**

## 法律红线（不可违反）

1. 仓库 local-only。`decompiled_dll/`、`raw_pck/`、`sts2.dll`、`0Harmony.dll`、`*.pck` 已经在 `.gitignore`。**永不上传反编译产物到任何公开仓库。**
2. 不在公开渠道分享反编译源码片段。
3. 笔记可引用类名、方法签名、namespace 结构，**不复制反编译代码体**。要描述行为时用自己的话总结。

## 项目背景（North Star）

最终目标是把 Slay the Spire 2 包装成 Gymnasium 风格的 RL/LLM 双一等公民环境。读 [STS2_GYM_DEV_PLAN.md](STS2_GYM_DEV_PLAN.md) 的 §0（项目定位）、§2（Mod 侧组件）、§9（易错点）足以理解上下文。**"双一等公民"是核心卖点**：RL agent 和 LLM agent 共享同一套底层状态、同一份 reward，所有设计为此让路。

`STS2_GYM_STRATEGY.md` 是用户的对外推广战略文档，**不需要 coding agent 参与**。

## 工作区结构

```
STS2env/
├── CODING_AGENT_BRIEF.md       # 当前侦察任务指令（你的工作手册）
├── STS2_GYM_DEV_PLAN.md        # 项目最终目标（north star）
├── STS2_GYM_STRATEGY.md        # 用户自留战略文档（不需关心）
├── README.md
└── sts2-reverse/               # 反编译产物工作区（gitignore）
    ├── decompiled_dll/         # ILSpy 输出，3369 个 .cs，约 100+ 个 namespace
    ├── raw_pck/                # GDRE 输出资源
    ├── sts2.dll                # 原始 DLL（mod 引用用）
    ├── 0Harmony.dll
    └── docs/
        ├── VERSION.md          # 反编译时间和版本信息
        ├── rng_audit_raw.txt   # 已预生成的 RNG 调用点清单（44 条）
        └── recon/              # 你的产出目标位置（任务 A-E 各一份 md）
```

## 已识别的金矿命名空间

侦察任务围绕这几个 namespace 展开（细节见 [CODING_AGENT_BRIEF.md](CODING_AGENT_BRIEF.md) §4.2）：

| Namespace | 价值 |
|---|---|
| `MegaCrit.Sts2.Core.Modding` | 官方 mod 系统（`ModInitializerAttribute`、`ModManager`），不用自己搞 patcher |
| `MegaCrit.Sts2.Core.DevConsole.ConsoleCommands` | 30+ 控制台命令，覆盖几乎所有状态注入需求 |
| `MegaCrit.Sts2.Core.AutoSlay` | 官方"AI 自动跑 run"框架，可能能复用 step 同步语义 |
| `MegaCrit.Sts2.Core.Runs/RunRngSet.cs` | 中央 RNG 管理类，hook 它可能就够 |
| `MegaCrit.Sts2.Core.Combat.History` | 游戏自带战斗事件流记录 |
| `MegaCrit.Sts2.Core.Multiplayer/CombatStateSynchronizer.cs` | 联机同步证明 CombatState 已可远程重建 |
| `MegaCrit.Sts2.Core.Commands*` | 游戏动作很可能已经封装成 Command 对象 |

## 常用调研命令

```bash
# 工作目录
cd /Users/mac/code/STS2env/sts2-reverse

# ripgrep 优于 grep。跨文件引用图先于深读单文件
rg -l "RunState" decompiled_dll/ --type cs | head -20
rg "class FightConsoleCmd" decompiled_dll/ --type cs

# 已经预生成的 RNG 审计原始清单
cat docs/rng_audit_raw.txt

# 列出某个 namespace 下所有文件
ls decompiled_dll/MegaCrit.Sts2.Core.Modding/

# 建/更新产出目录
mkdir -p docs/recon
```

## 优先级与时间预算

- 任务 A（mod 系统）、B（DevConsole）、D（RNG）优先——决定能否开始写 mod
- 任务 C（AutoSlay）、E（序列化）次之——决定具体怎么写
- 总耗时预期 4-8 小时 reading + writing。**超过 12 小时还没出 SUMMARY，停下来问用户。**

## 不在本仓库做的事

- 不写 C# mod 代码、不写 Python env 代码——侦察阶段只读 + 做笔记
- 不修改 `decompiled_dll/`、`raw_pck/` 下任何内容
- 不在 dev plan 或 strategy 文档上做大改动（如确有事实修正再问用户）
