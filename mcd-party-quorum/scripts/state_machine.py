#!/usr/bin/env python3
"""state_machine.py -- 状态定义、转移裁决与每状态工具白名单（状态表单一事实源）。

设计目的（状态机.md §1）：把「严禁跳步」与「空态不产生下一跳」从提示词约束
升级为机器可裁决、可断言的结构约束。

  · 每个下钻状态的工具白名单只含本层那一个工具 -> 跳步请求被 Gate 拒绝（C5）
  · S10 DEGRADED 的白名单是空集 -> 进入后结构上不可能再产生任何一跳（C4）
  · 事件取值是封闭枚举；deadline_passed / overdue / deadline_kind 不是合法事件
    —— 死线是 note 派生值，让它驱动状态转移等于绕道进入业务逻辑通道（§1.4）

references/state-machine.md 是本表的渲染产物（人类视图）；run_scenarios.py 的
T-table-sync 用例解析该 md 表格与本表比对，漂移即 exit 1。

退出码：0 正常（含 need_input）/ 1 断言失败 / 2 入参错误（含非法转移）。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_ROOT = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_ROOT / "references"

# ── 状态定义（单一事实源）──────────────────────────────────────────────
# (id, 名称, 含义, 允许调用的 MCP 工具)
STATES = [
    ("S0", "IDLE", "意图未确认", ["now-time-info"]),
    ("S1", "DISCOVER", "活动雷达：拉活动列表并逐个取详情",
     ["mall-points-products", "mall-product-detail", "now-time-info"]),
    ("S2", "PICK_ACTIVITY", "已列出活动，等待用户选一个", []),
    ("S3", "CITY_LISTED", "已选定活动，等待用户选城市", ["query-party-city"]),
    ("S4", "STORE_LISTED", "已选城市，等待用户选门店", ["query-party-store"]),
    ("S5", "DATE_LISTED", "已选门店，等待用户选日期", ["query-party-store-date"]),
    ("S6", "SESSION_LISTED", "已选日期，等待用户选场次", ["query-party-store-session"]),
    ("S7", "ASK_PARTY_TYPE", "partyType 为 -1，必须反问包场 / 拼团", []),
    ("S8", "QUORUM", "组局结算计算（纯脚本，无工具调用）", ["now-time-info"]),
    ("S9", "CARD", "出组局卡 + 下单参数清单（终态）", []),
    ("S10", "DEGRADED", "降级终态：空态 / 不可预约 / 城市未开放 / 超上限 / 退避耗尽 / MCP 不可达", []),
    ("S11", "REFUSED", "拒答终态：写操作 / 套利 / 营养 / 域外 / 闲聊 / 索要凭据", []),
]

STATE_IDS = [s[0] for s in STATES]
STATE_BY_ID = {s[0]: s for s in STATES}
ALLOWED_TOOLS = {s[0]: list(s[3]) for s in STATES}

# ── 转移表（from, event) -> 目标状态集合（多于一个表示由守卫条件决定）──
TRANSITIONS = {
    ("S0", "intent_matched"): {"S1"},
    ("S0", "off_topic"): {"S11"},
    ("S0", "refuse_intent"): {"S11"},
    ("S1", "activities_found"): {"S2"},
    ("S1", "no_activity"): {"S10"},
    ("S1", "not_onsale"): {"S10"},
    ("S1", "mcp_unreachable"): {"S10"},
    ("S2", "user_selected_activity"): {"S3"},
    ("S3", "cities_nonempty"): {"S3"},
    ("S3", "user_selected_city"): {"S4"},
    ("S3", "cities_empty"): {"S10"},
    ("S3", "user_city_not_in_open_list"): {"S10"},
    ("S4", "stores_nonempty"): {"S4"},
    ("S4", "user_selected_store"): {"S5"},
    ("S4", "stores_empty"): {"S10"},
    ("S5", "dates_nonempty"): {"S5"},
    ("S5", "user_selected_date"): {"S6"},
    ("S5", "dates_empty"): {"S10"},
    ("S6", "sessions_nonempty"): {"S6"},
    ("S6", "user_selected_session"): {"S7", "S8"},
    ("S6", "sessions_empty"): {"S10"},
    ("S7", "user_answered_party_type"): {"S8"},
    ("S8", "calc_ok"): {"S9"},
    ("S8", "over_cap"): {"S10"},
    ("S9", "done"): {"S0"},
    ("S10", "user_restart"): {"S0"},
    ("S11", "user_restart"): {"S0"},
    ("ANY", "rate_limited"): {"S10"},
    ("ANY", "write_op_requested"): {"S11"},
}

TRANSITION_DESC = {
    ("S0", "intent_matched"): "命中组局 / 查场次 / 查规则 / 雷达 / 出卡意图",
    ("S0", "off_topic"): "13 类硬边界，走固定话术",
    ("S0", "refuse_intent"): "13 类硬边界，走固定话术",
    ("S1", "activities_found"): "存在 shopId==5 且 status==2 且在售的活动",
    ("S1", "no_activity"): "C13：明确告知当前没有可预约的派对活动",
    ("S1", "not_onsale"): "C13：活动已下架或已过下架时间",
    ("S1", "mcp_unreachable"): "MCP 不可达 / 未配置 -> exit 4 文案 + 降级",
    ("S2", "user_selected_activity"): "必须有显式用户选择",
    ("S3", "cities_nonempty"): "展示城市清单，不自动推进",
    ("S3", "user_selected_city"): "携带该城市的 code 与 latitude/longitude",
    ("S3", "cities_empty"): "C4：出降级卡，query-party-store 调用次数恒为 0",
    ("S3", "user_city_not_in_open_list"): "C12：列出开放城市清单 + 引导，禁止编造门店",
    ("S4", "stores_nonempty"): "展示门店清单，不自动推进",
    ("S4", "user_selected_store"): "携带 storeCode",
    ("S4", "stores_empty"): "空态熔断",
    ("S5", "dates_nonempty"): "展示日期清单，不自动推进",
    ("S5", "user_selected_date"): "携带 dateStr",
    ("S5", "dates_empty"): "空态熔断",
    ("S6", "sessions_nonempty"): "展示场次卡片，不自动推进",
    ("S6", "user_selected_session"): "C9：partyType 为 -1 时经 S7 反问，1 / 2 直入 S8",
    ("S6", "sessions_empty"): "空态熔断",
    ("S7", "user_answered_party_type"): "用户拍板，脚本不得替选",
    ("S8", "calc_ok"): "出卡。不受死线影响：已过 advisory 死线也走这条，只追加标注句",
    ("S8", "over_cap"): "C7：给可执行修正建议，不出组局卡（唯一允许阻断出卡的条件）",
    ("S9", "done"): "终态输出到用户后复位",
    ("S10", "user_restart"): "用户重新发起",
    ("S11", "user_restart"): "用户重新发起",
    ("ANY", "rate_limited"): "C11：明确失败提示，不静默成功",
    ("ANY", "write_op_requested"): "B3：7 个写工具一律拒，调用次数恒为 0",
}

EVENTS = sorted({e for (_, e) in TRANSITIONS})

# 死线相关值：note 派生，禁止作为转移条件（状态机.md §2.1）
FORBIDDEN_EVENT_PATTERN = re.compile(r"(?i)(deadline|overdue|note[_-]?display)")

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入业务逻辑通道：入参出现 note_display_*（收到 " + token + "）。"
            )


def allowed_tools(state):
    # INVARIANT: S10 DEGRADED 与 S11 REFUSED 的白名单恒为空集
    # —— 空态与拒答之后结构上不可能再产生任何一跳
    if state not in ALLOWED_TOOLS:
        raise UsageError("未知状态 '" + str(state) + "'；合法状态：" + "、".join(STATE_IDS))
    return list(ALLOWED_TOOLS[state])


def tools_allowed(state, tool):
    return tool in ALLOWED_TOOLS.get(state, [])


def transition(state, event, party_type=None):
    """转移裁决。返回 dict；非法转移抛 UsageError。"""
    if state not in ALLOWED_TOOLS:
        raise UsageError("未知状态 '" + str(state) + "'；合法状态：" + "、".join(STATE_IDS))
    # INVARIANT: 死线相关值不是合法事件；S8 转 S10 只允许由 over_cap 触发
    if FORBIDDEN_EVENT_PATTERN.search(str(event)):
        raise UsageError(
            "死线相关值禁止作为转移条件：事件 '" + str(event) + "' 不是合法事件。"
            "死线（deadline / overdue / deadline_kind）是 note 派生的展示值，"
            "已过 advisory 死线仍必须正常出组局卡，只追加标注句。"
        )
    if event not in EVENTS:
        raise UsageError("未知事件 '" + str(event) + "'；合法事件：" + "、".join(EVENTS))
    targets = TRANSITIONS.get((state, event))
    if targets is None:
        targets = TRANSITIONS.get(("ANY", event))
    if targets is None:
        raise UsageError(
            "非法转移：状态 " + state + " 不接受事件 " + event
            + "。无事件的状态间跳转一律视为非法（防跳步）。"
        )
    if len(targets) == 1:
        return {"outcome": "ok", "from": state, "event": event,
                "next_state": sorted(targets)[0], "allowed_tools": allowed_tools(sorted(targets)[0])}
    # 多目标：由守卫条件决定，缺失守卫信息一律 need_input，绝不替用户选
    if party_type is None:
        return {"outcome": "need_input", "from": state, "event": event, "next_state": None,
                "allowed_tools": allowed_tools(state),
                "reason": "目标取决于 partyType：必须先在接口返回里确认 partyType；"
                          "partyType 为 -1 时不得替用户选，必须反问包场还是拼团。"}
    try:
        pt = int(party_type)
    except (TypeError, ValueError):
        raise UsageError("partyType 必须是 1（包场）/ 2（拼团）/ -1（二者皆可），收到 '" + str(party_type) + "'")
    if pt == -1:
        return {"outcome": "ok", "from": state, "event": event, "next_state": "S7",
                "allowed_tools": allowed_tools("S7"),
                "note": "partyType 为 -1：必须反问用户包场还是拼团，脚本不得替选"}
    if pt in (1, 2):
        return {"outcome": "ok", "from": state, "event": event, "next_state": "S8",
                "allowed_tools": allowed_tools("S8"),
                "note": "partyType 已明确，无需反问"}
    raise UsageError("partyType 必须是 1（包场）/ 2（拼团）/ -1（二者皆可），收到 '" + str(party_type) + "'")


def dump_table():
    """渲染 Markdown 表格（供 references/state-machine.md 使用，T-table-sync 比对基准）。"""
    lines = ["| 状态 | 含义 | 允许调用的工具 |", "|---|---|---|"]
    for sid, name, meaning, tools in STATES:
        cell = "、".join("`" + t + "`" for t in tools) if tools else "无（空集）"
        lines.append("| " + sid + " " + name + " | " + meaning + " | " + cell + " |")
    lines.append("")
    lines.append("| 从 | 事件 | 到 | 说明 |")
    lines.append("|---|---|---|---|")
    for (frm, event), targets in TRANSITIONS.items():
        frm_cell = "`任意`" if frm == "ANY" else "`" + frm + "`"
        to_cell = " 或 ".join("`" + t + "`" for t in sorted(targets))
        desc = TRANSITION_DESC.get((frm, event), "")
        lines.append("| " + frm_cell + " | `" + event + "` | " + to_cell + " | " + desc + " |")
    return "\n".join(lines)


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _run(argv):
    out = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, io.StringIO()
    try:
        code = main(argv)
    except SystemExit as exc:
        code = int(exc.code) if exc.code is not None else EXIT_OK
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue()


def _selftest():
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond)))
        print(("✅" if cond else "❌") + " " + label + ((" -- " + detail) if detail and not cond else ""))

    check("S3 只放行 query-party-city", allowed_tools("S3") == ["query-party-city"])
    check("C5 防跳步：S3 不放行 query-party-store-session",
          not tools_allowed("S3", "query-party-store-session"))
    check("C4 空态熔断：S10 DEGRADED 白名单为空集", allowed_tools("S10") == [])
    check("S11 REFUSED 白名单为空集", allowed_tools("S11") == [])

    # 死线禁令
    for bad in ("deadline_passed", "overdue", "deadline_kind"):
        try:
            transition("S8", bad)
            check("死线事件 '" + bad + "' 被拒", False, "未报错")
        except UsageError as exc:
            check("死线事件 '" + bad + "' -> exit 2（禁止作为转移条件）", "禁止作为转移条件" in str(exc))
    try:
        transition("S8", "note_display_overdue")
        check("note 派生事件被拒", False, "未报错")
    except UsageError:
        check("note 派生事件 -> exit 2", True)

    # C9：partyType 三态
    r = transition("S6", "user_selected_session", party_type=-1)
    check("C9：partyType -1 -> S7 反问", r["next_state"] == "S7")
    r = transition("S6", "user_selected_session", party_type=1)
    check("C9：partyType 1 -> S8 不再反问", r["next_state"] == "S8")
    r = transition("S6", "user_selected_session")
    check("缺 partyType -> need_input（不替用户选）", r["outcome"] == "need_input")
    try:
        transition("S6", "user_selected_session", party_type=9)
        check("非法 partyType -> exit 2", False, "未报错")
    except UsageError:
        check("非法 partyType -> exit 2", True)

    # 关键转移
    check("C4：S3 + cities_empty -> S10", transition("S3", "cities_empty")["next_state"] == "S10")
    check("C7：S8 + over_cap -> S10", transition("S8", "over_cap")["next_state"] == "S10")
    check("C10-b：S8 + calc_ok -> S9（不受死线影响）", transition("S8", "calc_ok")["next_state"] == "S9")
    check("B3：任意 + write_op_requested -> S11", transition("S5", "write_op_requested")["next_state"] == "S11")
    check("C11：任意 + rate_limited -> S10", transition("S6", "rate_limited")["next_state"] == "S10")
    try:
        transition("S3", "user_selected_store")
        check("非法转移 -> exit 2（防跳步）", False, "未报错")
    except UsageError as exc:
        check("非法转移 -> exit 2（防跳步）", "非法转移" in str(exc))

    # --dump-table 幂等
    t1, t2 = dump_table(), dump_table()
    check("幂等：--dump-table 两次输出一致", t1 == t2)
    check("--dump-table 覆盖全部状态", all(s[0] in t1 for s in STATES))
    check("--dump-table 覆盖全部转移", all(e in t1 for e in EVENTS))

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """state_machine.py -- 状态转移裁决与工具白名单

  --state S --event E [--party-type N]   转移裁决（多目标需 --party-type）
  --tools S                              列出该状态允许的工具（空态返回空集）
  --states / --events                    列出全部状态 / 合法事件（封闭枚举）
  --dump-table                           输出 Markdown 表格（与 references 对账）
  --selftest                             内置自检
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="state_machine.py", description="状态机与工具白名单")
    ap.add_argument("--state")
    ap.add_argument("--event")
    ap.add_argument("--party-type")
    ap.add_argument("--tools")
    ap.add_argument("--states", action="store_true")
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--dump-table", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    try:
        guard_note_derived(argv)
    except UsageError as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    try:
        if args.dump_table:
            print(dump_table())
            return EXIT_OK
        if args.states:
            print(json.dumps([{"state": s, "name": n, "meaning": m, "allowed_tools": t}
                              for s, n, m, t in STATES], ensure_ascii=False, indent=2))
            return EXIT_OK
        if args.events:
            print(json.dumps(EVENTS, ensure_ascii=False, indent=2))
            return EXIT_OK
        if args.tools:
            tools = allowed_tools(args.tools)
            if args.json:
                print(json.dumps({"state": args.tools, "allowed_tools": tools},
                                 ensure_ascii=False, sort_keys=True))
            else:
                print(("、".join(tools) if tools else "无（空集）—— 该状态不允许任何 MCP 调用"))
            return EXIT_OK
        if args.state and args.event:
            res = transition(args.state, args.event, args.party_type)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, sort_keys=True))
            else:
                print(str(res["from"]) + " + " + str(res["event"]) + " -> "
                      + str(res.get("next_state") or "（等待输入）")
                      + " ｜ 该状态允许工具：" + ("、".join(res["allowed_tools"]) or "无（空集）"))
                if res.get("reason"):
                    print("⚠ " + res["reason"])
            return EXIT_OK
    except UsageError as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
