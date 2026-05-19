# STS2-Gym 推广与基础设施战略（仅供你自己）

> 这份文档不给 coding agent 看。它讲的是怎么让这个项目从"一个能跑的 env"变成"RL/LLM agent 评测领域被广泛使用的基础设施"。

---

## 1. 核心战略洞察

成功的 RL env（ALE / DM Control / Procgen / NetHack LE / Crafter / MineRL）有几个共性，按这些标准对齐：

- 安装 30 秒内能跑
- 单实例吞吐量 ≥ 50 step/s，并行可扩
- 三大主流算法库（SB3、CleanRL、Tianshou）官方对接示例
- 一句命令复现 README 里的 baseline 数字
- 有命名 task suite、有 leaderboard、有 env paper

照这些标准对齐 STS2-Gym 是必要不充分条件。**充分条件是找到差异化定位**。

---

## 2. 差异化定位：三个故事，第三个最值钱

你的 env 能讲三个故事：

1. **Long-horizon strategic decision making with discrete combinatorial action space**
   它和 MuJoCo / Atari 的根本不同。卖给传统 RL 圈

2. **Real commercial game with real player data**
   Offline RL 的天然 benchmark。卖给 offline RL / imitation learning 圈

3. **Natural LLM agent testbed** ⭐
   这是 2025-2026 最有传播力的方向

LLM agent 评测当前的痛点：
- WebArena / SWE-bench / OSWorld 这类 benchmark 要么噪声大、要么饱和快、要么标注昂贵
- 缺少"规则清晰、状态可序列化、决策长程、有 ground truth、能大规模自动评测、token 成本可控"的 benchmark
- STS2 几乎完美契合：
  - 规则清晰（卡牌效果文本化）
  - 状态可序列化（你的 env 就是干这个的）
  - 决策长程（一个 run 几百步）
  - Ground truth 明确（赢/输/HP 残留）
  - 自动评测无需人工标注
  - 单 episode token 成本可控（几千到几万）

**把"双一等公民"作为整个项目的卖点向外讲**。不是"我们做了个 RL env，也支持 LLM"，而是"我们是第一个把 RL 和 LLM agent 放在同一套接口、用同一个 reward 做横向比较的 benchmark"。这个 framing 在当下能直接进 NeurIPS Benchmark Track。

---

## 3. 命名与品牌

- 项目名：**STS2-Gym** 或 **SpireBench**（后者更适合 paper 引用）
- 论文 framing 候选：
  - "SpireBench: A Unified Benchmark for RL and LLM Agents on Long-Horizon Strategic Card Games"
  - "Slay the Spire 2 as a Testbed: Bridging RL and LLM Agent Evaluation"
- 写一篇 7–10 页的 env paper 挂 arXiv，命名 task suite，给出 baseline 表（PPO / DQN / GPT-4o / Claude / Llama / Qwen 并列），分析挑战点
- 论文里**必须**有 RL agent 与 LLM agent 同分数横向比较的大表。这是核心卖点的具象化

---

## 4. 推广路径

### 4.1 技术层面

| 检查项 | 标准 |
|---|---|
| 零摩擦安装 | `pip install` → `gym.make` → 三行能跑 |
| 吞吐量 | ≥ 50 step/s 单实例，并行能扩 |
| 算法库适配 | SB3、CleanRL、Tianshou 各一个示例脚本 |
| LLM 库适配 | openai、anthropic、litellm（覆盖大部分模型）各一个示例 |
| 复现脚本 | 一行命令复现 README 里报告的所有 baseline 数字 |
| Leaderboard | 网站或 GitHub README 表格，开放提交 |

### 4.2 内容层面

- **明星用例**：训一个 PPO + transformer agent 在 IroncladCombat 上跑出 90%+ 通关率，做成视频/博客，配 "复现这个结果只需要 50 行代码"
- **LLM 横评博客**：对比 GPT-4o / Claude / Gemini / DeepSeek / Qwen 在 STS2-Gym 上的分数。这种横评博客现在自带传播效应
- **Twitter/小红书/知乎短视频**：让 GPT-4o 边推理边玩 STS2，把 thinking trace 录下来。这个内容形式非常容易病毒传播
- **关键 demo**：演示同一个 task 上 PPO 和 Claude 直接比分数

### 4.3 社区层面

- **公开开发**：day 1 开 GitHub，所有讨论在 issue 里
- **响应速度**：v0.x 阶段任何 issue 24 小时内回复，比 feature 更重要
- **License 选 MIT 或 Apache 2.0**：copyleft 劝退企业用户
- **接 Mega Crit**：他们对开源态度极好。主动去 Discord/邮件介绍项目，争取被官方"承认"甚至推荐。一个 official mention 抵 1000 个 star
- **找早期合作者**：
  - 做 RL benchmark 的（Farama Foundation、Stanford CRFM）
  - 做 LLM agent eval 的（Princeton、Berkeley NLP、Anthropic、OpenAI 的 eval 团队）
  - 做 STS1 AI 的老玩家（jorbs 圈、sts_lightspeed 作者）
  
  主动提供 env 换早期反馈和潜在 co-author
- **持续维护承诺**：README 写明"承诺维护至少 2 年"

### 4.4 战略层面

- **不要追求 100% 覆盖再发布**。先把 IroncladCombat 这一个 task 做到极其精致、文档极其完善、baseline 极其漂亮，发出去拿到第一波用户和反馈，再扩。90% 精力放在 10% 的 surface 上，比 50/50 散开做强 10 倍
- **接受 1.0 → 2.0 的痛苦升级**。不要为了向前兼容把烂设计永远背着。设计好破坏性升级的迁移路径
- **不要做完美主义版本规划**：v0.1 发出去拿反馈 → v0.5 收 100 issue → v1.0 稳定 → 之后是渐进改进

---

## 5. 与你的研究方向的协同

你现在做的是"多机器人任务规划的通用大模型"——这和 STS2-Gym 在数学结构上有强同构：

| 多机器人任务规划 | STS2 战斗规划 |
|---|---|
| 任务集合 → 机器人集合的分配 | 卡牌集合 → 敌人集合的分配 |
| 时序依赖（先做 A 才能做 B） | 时序依赖（先打 Vulnerable 再打 Strike） |
| 资源约束（电量、负载） | 资源约束（能量、HP、牌库容量） |
| 异质 agent（不同机器人能力不同） | 异质 card（不同卡牌效果不同） |
| 部分可观测（机器人传感器有限） | 部分可观测（牌库顺序未知） |
| 长程信用分配 | 长程信用分配 |

**潜在协同打法**：

1. 在 STS2-Gym 上做 IL-pretraining + RL-finetuning 的 pipeline 验证（你"From Imitation to Autonomy"框架的低成本验证场）
2. 论文里写"我们在 STS2-Gym 上验证了 pipeline 的关键设计选择，然后迁移到多机器人 task allocation"——双重曝光
3. STS2-Gym 本身成为你后续多机器人工作的对比 baseline 环境（"我们的方法在 STS2 决策任务上比 SOTA 高 X%，且能迁移到机器人场景"）

这个项目可以同时承担三个角色：
- 你的研究 sandbox
- 一篇独立的 benchmark paper（NeurIPS Track / ICLR Workshop）
- 一个长期的 portfolio 项目

不一定要把它定位成主线工作，但它能给你多机器人主线提供大量验证空间和发表机会。

---

## 6. 风险与对冲

- **STS2 仍在 early access，规则会变** → mod 设计要足够松，主版本号绑定明确支持范围，超出范围 fail-fast
- **Mega Crit 改主意收紧反编译政策** → 概率极低（他们公开欢迎），但仓库永远不上传反编译产物作为预防
- **Godot 引擎对 headless / 高速运行不友好** → 早期就要 benchmark step/s，如果撞到墙，提前规划部分逻辑用 Python 重写（路线 C）
- **LLM API 涨价或限流** → benchmark 里用开源模型（Qwen、Llama）也跑一份，作为可复现的 baseline。完全依赖闭源模型 baseline 会让你的 paper 后续无法复现

---

## 7. 三条不能让步的红线

1. **永远不在仓库里上传反编译产物**。任何形式都不行。这条破了，整个项目法律风险拉满
2. **永远不污染默认 reward 来 hype 数字**。一旦你为了让 baseline 好看而设计 reward，研究价值崩
3. **永远不为了短期 traction 牺牲 schema 稳定性**。v0 期可以乱改，v1 之后一改字段会让所有引用过的论文掉链子，这是基础设施项目的死亡螺旋

---

## 8. 一句话总结

**你不是在写一个游戏的 RL env。你是在写 2025–2026 年第一个把传统 RL 与 LLM agent 评测统一在同一接口下、且建立在真实商业游戏之上的开放基础设施。这个 framing 是项目影响力的天花板。**
