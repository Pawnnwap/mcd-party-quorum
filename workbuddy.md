# workbuddy.md — 本项目的 WorkBuddy 开发上下文

本文件记录 `mcd-party-quorum`（麦麦组局官）**如何使用腾讯 WorkBuddy 开发**，
用于「麦当劳程序员创意开发大赛」WorkBuddy 专项奖励的核验联动。
开发全程发生在 WorkBuddy 桌面端会话内，以下为可核验的事实记录。

---

## 1. 开发方式总览

| 项 | 事实 |
|---|---|
| 开发工具 | 腾讯 WorkBuddy（Windows 桌面端） |
| 工作流 | 「技能全流程创作工坊」专家团（skill-lifecycle-studio，SOP 六阶段） |
| 团队编排 | TeamCreate 组建 `mcd-skill-studio` 团队：1 名主理人 + 5 个专职 subagent |
| 开发周期 | 2026-10-18 23:00 – 2026-10-19（连续会话） |
| 工作区 | `E:\pyprojects\competition_hunter\skill-studio\20261018-230411-mcd\` |
| 运行环境 | Windows 10 专业版，Python 3.13.12 / 3.12.10（WorkBuddy 托管运行时），Git Bash |

## 2. 六阶段 SOP 与各阶段产物

| 阶段 | 执行者 | 产物 |
|---|---|---|
| 1 快照 / 构思 | 主理人 + skill-ideator | 实调 35 个工具清单、竞争情报（RANKING.md：102 个在榜项目）、需求说明书 v2（30 条验收标准） |
| 2 设计 | skill-architect | 设计蓝图（436 行）、状态机（126 行，每状态工具白名单）、分工矩阵 |
| 3 实现 | skill-developer | 技能本体：SKILL.md + 5 个 references + 10 个纯标准库脚本 + 15 场景 + 8 内建守卫用例 |
| 4 QA | skill-qa | 独立审查报告（30 条验收核对、变异验证、跨 cwd 复跑） |
| 5 打包 / 提交 | 主理人 | package_skill.py 打包、GitHub 推送、报名 Issue |
| 6 沉淀 | 主理人 | 本文档 + 经验沉淀 |

## 3. WorkBuddy 能力的具体使用点

1. **专家团编排（Agent 工具）**：5 个 subagent 串行执行阶段任务，出口 gate 由主理人
   判定； teammate 间通过 SendMessage 传递裁决（如「note 派生值禁令」的传达与
   「门店排期差异」措辞的下达）。
2. **Skill 工具**：加载 `skill-lifecycle-toolkit`（contract.yaml v2.0.0 作为命名与
   字段唯一事实源），并用其 `validate_skill.py` / `package_skill.py` 做合规校验与
   打包；所有脚本遵循该 toolkit 的 skill 规范（frontmatter 六字段、md 数量与行数
   上限、content_scan 排除规则）。
3. **MCP 客户端**：WorkBuddy 内置 MCP 配置（`~/.workbuddy/mcp.json`）接入麦当劳
   MCP（`https://mcp.mcd.cn`），实调抓取全部真实数据。
4. **Bash / PowerShell 双通道**：Git Bash 跑 Python 回归与 Git 操作，PowerShell 做
   Windows 侧配置核查。
5. **托管运行时隔离**：全部 Python 执行走 WorkBuddy 托管解释器
   （`~/.workbuddy/binaries/python/versions/3.13.12/`），零第三方依赖，
   未污染用户环境。

## 4. 关键工程决策（在 WorkBuddy 会话内裁定）

1. **选题避红海**：读比赛 RANKING.md 发现省钱类 9 个项目垄断前三、派对类仅 4 个，
   选定「派对组局」切入，目标进前 100。
2. **可测试性三件套**：MockTransport（进程内执行）+ Ledger 账本（权威事实）+
   Gate 票据（无票据不调用）—— 让「写工具调用次数为 0」成为真断言而非君子协定。
3. **note 派生值禁令**：`mall-product-detail` 官方字段说明禁止从 `note` 提取数值
   做业务逻辑 → 全部降级为「定位 + 并列对照」，`note_display_` 前缀命名守卫，
   四个核心脚本对这类参数直接 exit 2。
4. **延期条款判定表**：note 自带「结束时间可能延长」条款时，接口终点更宽判 match；
   起点不一致一律 conflict（字面授权只覆盖结束端）。
5. **conflict 措辞锁定**：真实世界门店排期本就不同，冲突文案固定渲染为「门店排期
   差异，并非数据错误」，禁止把差异说成「数据打架」。
6. **过期死线仍出卡**：须知「提前 3 天」是提示性字段，已过死线照样正常出卡并加
   标注句 —— 用提示性字段拦截等于换条路违规。
7. **零硬编码 spuId**：活动雷达动态过滤在架活动，18229 下架后技能仍能工作。

## 5. 质量证据（复现命令）

```bash
# 27 个回归用例全部进程内 Mock 执行（不联网、不烧 Token）
python scripts/run_scenarios.py --all          # 27/27 通过
python scripts/mcp_client.py --selftest        # 各脚本均有 --selftest

# 合规校验（skill-lifecycle-toolkit）
python validate_skill.py <技能目录>             # exit 0，0 warning
```

真实联调证据（2026-10-18/19 实调）：9 城门店日期差异表、spuId=18229 全链路响应、
组局卡真实输出，均记录于 `MCP_INTEGRATION.md` 与 README。

## 6. 声明

- 本项目为参赛者独立作品，非麦当劳官方产品；
- 开发过程真实使用腾讯 WorkBuddy 智能体完成（本文件即为上下文存档）；
- 不含任何真实 Token / 凭据 / 个人信息。
