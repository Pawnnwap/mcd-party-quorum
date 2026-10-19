#!/usr/bin/env python3
"""quorum_calc.py -- 组局结算：发车进度 / 人数上限 / 预算 / 预订死线。

覆盖设计蓝图 5.6 与分工矩阵 S4 / S5 / S6：

  need      = partyMin - confirmed            （整数减法，禁止 LLM 心算）
  cap       = min(partyMax, leftNum)          （人数判定只认 session 返回的整型值）
  over_cap  = confirmed > cap                 （唯一允许阻断出卡的条件，C7）
  金额      只接受 --price-fen（分），出口只有 format_cny，禁止裸显 5900
  死线      deadline_date = 场次日期 - lead_days 个自然日，时刻取当日 23:59:59

死线的语义纠正（设计蓝图 1.5）：「提前 3 天预订」约束的是最晚下单时刻，不是最早
允许时刻。取 00:00 会错误砍掉用户一整天的合法预订窗口 —— 那不是保守，是失真。

倒计时恐慌禁令（主理人拍板，必须实现）：
  overdue / hours_left / deadline_kind 一律是纯展示字段。已过 advisory 死线依然
  正常产出组局卡，只在卡面追加一句标注；禁止因超时拒绝出卡、禁止断言「你赶不上了」、
  禁止把死线相关值送进任何 gate / 转移 / 分支判断。

退出码：0 正常（含 blocked / need_input）/ 1 断言失败 / 2 入参错误。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import money  # noqa: E402  金额规范化层是本脚本的唯一金额出口

DEADLINE_TIME = "23:59:59"
DEADLINE_KIND = "advisory"
OVERDUE_NOTE = "已过官方文案建议的提前预订期，是否仍可预订请以预约页面为准"
DEADLINE_NOTE = "实际可约日期与场次请以预约页面 / 门店确认为准"

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")
# 死线相关值若作为分支决策入参出现（而非展示用途），一律拒绝
DEADLINE_DECISION_KEYS = ("--overdue", "--deadline-kind", "--deadline-passed", "--if-overdue")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入计算通道：入参出现 note_display_*（收到 " + token + "）。"
                "note 只能原文展示，人数与金额判定只用接口返回的 partyMin/partyMax/leftNum/price。"
            )
        low = token.lower()
        if low in DEADLINE_DECISION_KEYS:
            raise UsageError(
                "死线相关值禁止用于分支决策：" + token + " 不是合法入参。"
                "overdue / deadline_kind 是纯展示字段，已过死线仍必须正常出组局卡。"
            )


def _positive_int(value, name):
    if value is None:
        raise UsageError("缺少 " + name + "。")
    s = str(value).strip()
    if not re.fullmatch(r"\d+", s):
        raise UsageError(name + " 必须是非负整数，收到 '" + s + "'。")
    n = int(s)
    return n


def parse_date(text, name):
    raw = "" if text is None else str(text).strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise UsageError(name + " 必须是 YYYY-MM-DD，收到 '" + raw + "'。")


def parse_now(text):
    if not text:
        raise UsageError("缺少 --now：禁用本地时间，服务器时间必须来自 now-time-info。")
    raw = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise UsageError("--now 时间格式无法解析：'" + raw + "'。")


def compute(party_min, party_max, left_num, confirmed, price_fen,
            session_date, now, lead_days=3):
    """组局结算。返回结构化结果（金额一律 fen -> format_cny 出口）。"""
    # INVARIANT: overdue / deadline_kind / hours_left 只写入展示字段，
    # 绝不参与 can_start / over_cap / outcome 的任何判定
    cap = min(party_max, left_num)
    need = party_min - confirmed
    can_start = need <= 0
    over_cap = confirmed > cap

    deadline_date = session_date - timedelta(days=lead_days)
    deadline_dt = datetime.strptime(deadline_date.strftime("%Y-%m-%d") + " " + DEADLINE_TIME,
                                    "%Y-%m-%d %H:%M:%S")
    delta = deadline_dt - now
    total_minutes = int(delta.total_seconds() // 60)
    overdue = delta.total_seconds() < 0
    hours_left = round(delta.total_seconds() / 3600.0, 2)

    per_person_cny = money.format_cny(price_fen)
    total_cny = money.format_cny(price_fen * confirmed)

    result = {
        "outcome": "blocked" if over_cap else "ok",
        "party_min": party_min,
        "party_max": party_max,
        "left_num": left_num,
        "confirmed": confirmed,
        "need": max(need, 0),
        "can_start": can_start,
        "cap": cap,
        "over_cap": over_cap,
        "per_person_cny": per_person_cny,
        "total_cny": total_cny,
        "price_fen": price_fen,
        "session_date": session_date.strftime("%Y-%m-%d"),
        "lead_days": lead_days,
        "deadline": deadline_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "deadline_date": deadline_date.strftime("%Y-%m-%d"),
        "deadline_kind": DEADLINE_KIND,
        "hours_left": hours_left,
        "minutes_left": total_minutes,
        "overdue": overdue,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result


def human_left(minutes):
    sign = "-" if minutes < 0 else ""
    m = abs(minutes)
    return sign + str(m // 60) + " 小时 " + str(m % 60) + " 分"


def render_card(r):
    # INVARIANT: over_cap 是唯一允许不产出组局卡的条件；已过死线不是
    lines = []
    if r["over_cap"]:
        lines.append("⛔ 本场无法成团（C7：人数超上限，不产出卡片）")
        lines.append("")
        lines.append("已确认 " + str(r["confirmed"]) + " 人，本场上限 " + str(r["party_max"])
                     + " 人、余位 " + str(r["left_num"]) + " 人，实际可承接 "
                     + str(r["cap"]) + " 人。")
        lines.append("可执行修正建议：")
        lines.append("  1. 拆成两场（每场最多 " + str(r["cap"]) + " 人）；")
        lines.append("  2. 改约同一门店的其他场次或其他日期；")
        lines.append("  3. 减少参与人数到 " + str(r["cap"]) + " 人以内。")
        lines.append("")
        lines.append("人数判定依据：接口返回的 partyMax / leftNum（不含任何文案推断）。")
        return "\n".join(lines)

    lines.append("## 组局卡")
    lines.append("")
    if r["can_start"]:
        lines.append("👥 已确认 " + str(r["confirmed"]) + " 人 · 最少 " + str(r["party_min"])
                     + " 人 → 已成团，可发车（上限 " + str(r["party_max"])
                     + "，余位 " + str(r["left_num"]) + "）")
    else:
        lines.append("👥 已确认 " + str(r["confirmed"]) + " 人 · 最少 " + str(r["party_min"])
                     + " 人 → 还差 " + str(r["need"]) + " 人发车（上限 "
                     + str(r["party_max"]) + "，余位 " + str(r["left_num"]) + "）")
    lines.append("💰 人均 " + r["per_person_cny"] + " · " + str(r["confirmed"]) + " 人共 "
                 + r["total_cny"] + "（每加 1 人 +" + r["per_person_cny"] + "）")
    lines.append("⏳ 预订死线（按官方文案「提前 " + str(r["lead_days"]) + " 天」估算）："
                 + r["deadline"] + " → 还剩 " + human_left(r["minutes_left"])
                 + "（" + r["deadline_kind"] + "，提示性）")
    lines.append("   " + DEADLINE_NOTE)
    if r["overdue"]:
        lines.append("   " + OVERDUE_NOTE)
    lines.append("")
    lines.append("📌 参与方式：回复 +1，凑满 " + str(r["party_min"]) + " 人我发起预订。")
    lines.append("📌 本技能不会代你下单；下单参数请在你自己的 MCP 客户端确认后执行。")
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


BASE = ["--party-min", "5", "--party-max", "12", "--left-num", "12", "--confirmed", "3",
        "--price-fen", "5900", "--date", "2026-10-23", "--now", "2026-10-18 23:07:42"]


def _selftest():
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond)))
        print(("✅" if cond else "❌") + " " + label + ((" -- " + detail) if detail and not cond else ""))

    r = compute(5, 12, 12, 3, 5900, parse_date("2026-10-23", "--date"),
                parse_now("2026-10-18 23:07:42"), 3)
    check("C6：need = partyMin - confirmed = 2", r["need"] == 2 and r["can_start"] is False)
    check("C6：confirmed 达到 partyMin 即成团",
          compute(5, 12, 12, 5, 5900, parse_date("2026-10-23", "--date"),
                  parse_now("2026-10-18 23:07:42"), 3)["can_start"] is True)
    check("C8：金额经 format_cny 出口 -> ¥59 / ¥177",
          r["per_person_cny"] == "¥59" and r["total_cny"] == "¥177")

    # C10 死线口径
    check("C10：死线日期 = 场次日期 - 3 个自然日", r["deadline_date"] == "2026-10-20")
    check("C10：死线时刻取当日 23:59:59", r["deadline"].endswith("23:59:59"))
    check("C10：剩余时长以脚本计算值为基准（约 48 小时 52 分，容差 1 小时）",
          abs(r["hours_left"] - 48.87) < 1.0, "实际 " + str(r["hours_left"]))
    check("死线恒标 advisory（note 派生值，不作硬断言）", r["deadline_kind"] == "advisory")

    # 倒计时恐慌禁令
    late = compute(5, 12, 12, 3, 5900, parse_date("2026-10-23", "--date"),
                   parse_now("2026-10-21 10:00:00"), 3)
    check("C10-b：已过死线 -> overdue 为 True（纯展示）", late["overdue"] is True)
    check("C10-b：已过死线仍 outcome=ok，正常出卡", late["outcome"] == "ok")
    card_late = render_card(late)
    check("C10-b：卡面含「已过官方文案建议的提前预订期」标注", OVERDUE_NOTE in card_late)
    check("C10-b：卡面不出现「赶不上」类断言", "赶不上" not in card_late)
    check("C10-b：卡面仍含组局卡", "组局卡" in card_late)

    # C7 超上限
    over = compute(5, 12, 4, 6, 5900, parse_date("2026-10-23", "--date"),
                   parse_now("2026-10-18 23:07:42"), 3)
    check("C7：confirmed > min(partyMax, leftNum) -> over_cap", over["over_cap"] and over["cap"] == 4)
    check("C7：over_cap -> outcome blocked", over["outcome"] == "blocked")
    over_card = render_card(over)
    check("C7：不出组局卡", "组局卡" not in over_card)
    check("C7：给出可执行修正建议", "拆成两场" in over_card)

    # 出口守卫
    card = render_card(r)
    check("C8：卡面不含裸值 5900", not money.find_bare_fen(card, [5900]))
    check("C8：卡面不含裸值 17700", not money.find_bare_fen(card, [17700]))

    # 入参守卫
    code, _ = _run(BASE + ["--lead-days", "3", "--confirmed", "0"])
    check("confirmed 非正整数 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--party-min", "5", "--party-max", "12", "--left-num", "12",
                    "--confirmed", "3", "--price-fen", "5900", "--date", "2026-10-23"])
    check("缺 --now -> exit 2（禁用本地时间）", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--party-min", "5", "--party-max", "12", "--confirmed", "3",
                    "--price-fen", "5900", "--date", "2026-10-23", "--now", "2026-10-18 23:07:42"])
    check("缺 leftNum -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(BASE + ["--confirmed", "note_display_min"])
    check("note 派生值进入结算 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(BASE + ["--overdue", "true"])
    check("overdue 作为分支入参 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--party-min", "5", "--party-max", "12", "--left-num", "12",
                    "--confirmed", "3", "--price-yuan", "59", "--date", "2026-10-23",
                    "--now", "2026-10-18 23:07:42"])
    check("只接受 --price-fen（拒绝 --price-yuan）-> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))

    # 幂等
    code, a = _run(BASE + ["--json"])
    code2, b = _run(BASE + ["--json"])
    check("幂等：同一输入两次执行结果逐字一致", a == b and code == EXIT_OK)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """quorum_calc.py -- 组局结算

  --party-min N --party-max N --left-num N --confirmed N --price-fen N
  --date YYYY-MM-DD --now "YYYY-MM-DD HH:MM:SS" [--lead-days 3] [--json]

只接受 --price-fen（分）。死线一律 advisory；已过死线仍正常出卡，只追加标注句。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="quorum_calc.py", description="组局结算")
    ap.add_argument("--party-min")
    ap.add_argument("--party-max")
    ap.add_argument("--left-num")
    ap.add_argument("--confirmed")
    ap.add_argument("--price-fen")
    ap.add_argument("--date")
    ap.add_argument("--now")
    ap.add_argument("--lead-days", default="3")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    try:
        guard_note_derived(argv)
        party_min = _positive_int(args.party_min, "--party-min")
        party_max = _positive_int(args.party_max, "--party-max")
        left_num = _positive_int(args.left_num, "--left-num")
        confirmed = _positive_int(args.confirmed, "--confirmed")
        price_fen = _positive_int(args.price_fen, "--price-fen")
        lead_days = _positive_int(args.lead_days, "--lead-days")
        if confirmed <= 0:
            raise UsageError("--confirmed 必须是正整数，收到 '" + str(args.confirmed) + "'。")
        session_date = parse_date(args.date, "--date")
        now = parse_now(args.now)
        if party_min > party_max:
            raise UsageError("接口返回的 partyMin 大于 partyMax（" + str(party_min) + " > "
                             + str(party_max) + "），两者自相矛盾，无法结算。")
    except UsageError as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    r = compute(party_min, party_max, left_num, confirmed, price_fen,
                session_date, now, lead_days)
    card = render_card(r)

    # 出卡前再跑一次裸值自检（C8 的最后一道闸）
    hits = money.find_bare_fen(card, [price_fen, price_fen * confirmed])
    if hits:
        sys.stderr.write("❌ 出口守卫命中裸分数值 " + str(len(hits)) + " 处，拒绝出卡。\n")
        return EXIT_FAIL

    if args.json:
        r["card"] = card
        print(json.dumps(r, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(card)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
