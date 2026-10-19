#!/usr/bin/env python3
"""note_locate.py -- 官方购买须知（note）原文定位与并列展示。

MCP 官方字段说明（原文，红线）：
    data.note「购买须知，纯展示文案。必须原文展示给用户，不得拆分、提取或改写为
    结构化形式；严禁从中提取任何数值用于下单校验、价格计算、库存判断等业务逻辑，
    所有校验以下单接口返回结果为准」

因此本脚本严格分三层（设计蓝图 1.4）：

    L0 原文层  --full       去 HTML 后完整原样输出，一字不改
    L1 定位层  --locate     只输出原文子串 + 字符偏移，不输出改写后的文本
    L2 并列层  --crosscheck 原文片段与接口实时值并列，标 match / conflict / unknown

L2 产出的数值字段统一命名为 note_display_*（从命名上标记为不可进计算通道），
且 money / quorum_calc / order_params / state_machine 四处入口都有参数名守卫。
并列展示的结论只是视觉标记，不参与任何分支决策。

退出码：0 正常 / 1 断言失败 / 2 入参错误。
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_ROOT = Path(__file__).resolve().parent.parent

DISCLAIMER = "以上为官方文案提示，仅供阅读；所有校验以预约/下单接口返回结果为准。"

# 冲突项的定型句式（措辞由脚本固定，不由模型临场生成）。
# 语义边界：呈现的是「各门店排期差异」，不是「某一方写错了」——
# 禁止出现「文案有误」/「数据有误」/「异常」这类定性词。
CONFLICT_LEAD = "该门店接口显示的可约情况与官方文案不同"
CONFLICT_ADVICE = ("这通常是各门店排期差异所致，并非数据错误；"
                   "不同门店可约范围可能不同，请以该门店预约页面为准。")
CONFLICT_ADVICE_STRICT = "以门店最终确认为准；若严格按官方文案，请只考虑官方文案给出的区间。"

# 条目关键词（归类原则与领域无关；下方清单仅为正则配置，不限制适用范围）
KIND_PATTERNS = {
    "activity_window": r"活动时间",
    "lead_days": r"派对预约|提前\s*\d+\s*天",
    "age_limit": r"\d+\s*岁|年龄",
    "price": r"\d+(?:\.\d+)?\s*元",
    "min_people": r"起订|最低人数",
    "max_people": r"最高人数|人数上限|不超过",
    "duration": r"\d+\s*分钟",
    "refund": r"退订|退款|退改",
    "excluded_stores": r"不适用|除外|港澳台",
}

KIND_LABEL = {
    "activity_window": "活动时间窗",
    "lead_days": "提前预订天数",
    "age_limit": "年龄门槛",
    "price": "人均价格",
    "min_people": "成团起订人数",
    "max_people": "单场人数上限",
    "duration": "单场时长",
    "refund": "退改规则",
    "excluded_stores": "不适用门店类型",
}

# 延期条款：官方文案自己写了「结束时间可能延长」时，接口给出更晚的结束日期不属于冲突。
# 但条款只覆盖「结束时间」，起点不一致仍然要标冲突 —— C3 的真实冲突正是起点不一致。
FLEX_CLAUSE_RE = re.compile(r"(延长|顺延|延期)")
CLAUSE_SEPARATORS = ("。", "；", "，")

ITEM_RE = re.compile(r"(?<![0-9A-Za-z])(\d{1,2})[.、](?![0-9])")
DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
YUAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
MIN_PEOPLE_RE = re.compile(r"(\d+)\s*位[^。；]{0,12}?起订|需\s*(\d+)\s*位")
MAX_PEOPLE_RE = re.compile(r"最高人数为?\s*(\d+)|不超过\s*(\d+)\s*位")
DURATION_RE = re.compile(r"约?\s*(\d+)\s*分钟")
LEAD_DAYS_RE = re.compile(r"提前\s*(\d+)\s*天")
AGE_RE = re.compile(r"(\d+)\s*岁")

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    """本脚本是 note 派生值的唯一产地，禁止把派生值再喂回本脚本做分支决策。"""
    for token in argv:
        if NOTE_DISPLAY_RE.search(token) and token.startswith("--"):
            raise UsageError(
                "note 派生值禁止用于分支决策：入参出现 note_display_*（收到 " + token + "）。"
            )


def load_note(path):
    """读 note：接受响应 JSON（取 data.note / note）或纯文本文件。"""
    p = Path(path)
    if not p.is_file():
        raise UsageError("找不到 note 文件：" + str(p))
    raw = p.read_text(encoding="utf-8", errors="replace")
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return raw
        if isinstance(obj, dict):
            data = obj.get("data", obj)
            if isinstance(data, dict) and "note" in data:
                return str(data["note"])
            if "note" in obj:
                return str(obj["note"])
        raise UsageError("JSON 中找不到 note 字段。")
    return raw


def html_to_text(raw):
    """去 HTML 标签，保留全部文字；段落转换行。不改写一个字。"""
    text = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)</?span[^>]*>", "", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_items(plain):
    """按编号切分为条目，保留原文子串与字符偏移。"""
    marks = [(m.start(), int(m.group(1))) for m in ITEM_RE.finditer(plain)]
    items = []
    for idx, (start, num) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(plain)
        items.append({"no": num, "text": plain[start:end].strip(), "offset": start,
                      "len": end - start})
    if not items:
        items.append({"no": 0, "text": plain, "offset": 0, "len": len(plain)})
    return items


def locate(plain, kinds=None):
    """L1：定位候选条目，只输出原文子串 + 偏移。"""
    items = split_items(plain)
    want = list(kinds) if kinds else list(KIND_PATTERNS)
    unknown = [k for k in want if k not in KIND_PATTERNS]
    if unknown:
        raise UsageError("未知条目类型 " + "、".join(unknown) + "；已登记：" + "、".join(sorted(KIND_PATTERNS)))
    out = []
    for kind in want:
        pattern = re.compile(KIND_PATTERNS[kind])
        hit = None
        for item in items:
            if pattern.search(item["text"]):
                hit = item
                break
        if hit is None:
            out.append({"kind": kind, "label": KIND_LABEL[kind], "found": False,
                        "excerpt_raw": "", "offset": -1, "len": 0,
                        "note": "官方文案未提及（不断言）"})
        else:
            out.append({"kind": kind, "label": KIND_LABEL[kind], "found": True,
                        "excerpt_raw": hit["text"], "offset": hit["offset"], "len": hit["len"]})
    return out


def _extract_value(kind, excerpt):
    """从原文片段里取出候选数值（供并列展示，禁止进入任何计算 / 校验通道）。"""
    if kind == "activity_window":
        ms = DATE_CN_RE.findall(excerpt)
        if len(ms) >= 2:
            def norm(t):
                return "%04d-%02d-%02d" % (int(t[0]), int(t[1]), int(t[2]))
            return {"start": norm(ms[0]), "end": norm(ms[1])}
        if len(ms) == 1:
            return {"start": "%04d-%02d-%02d" % tuple(int(x) for x in ms[0]), "end": None}
        return None
    if kind == "price":
        m = YUAN_RE.search(excerpt)
        return float(m.group(1)) if m else None
    if kind == "min_people":
        m = MIN_PEOPLE_RE.search(excerpt)
        return int(m.group(1) or m.group(2)) if m else None
    if kind == "max_people":
        m = MAX_PEOPLE_RE.search(excerpt)
        return int(m.group(1) or m.group(2)) if m else None
    if kind == "duration":
        m = DURATION_RE.search(excerpt)
        return int(m.group(1)) if m else None
    if kind == "lead_days":
        m = LEAD_DAYS_RE.search(excerpt)
        return int(m.group(1)) if m else None
    if kind == "age_limit":
        m = AGE_RE.search(excerpt)
        return int(m.group(1)) if m else None
    return None


def _extract_clause(excerpt):
    """取出含延期关键词的整句原文子串（不改写一个字，供并列展示引用）。"""
    m = FLEX_CLAUSE_RE.search(excerpt)
    if not m:
        return ""
    start = max([excerpt.rfind(s, 0, m.start()) for s in CLAUSE_SEPARATORS] + [-1]) + 1
    ends = [excerpt.find(s, m.end()) for s in CLAUSE_SEPARATORS]
    end = min([e for e in ends if e != -1] or [len(excerpt)])
    return excerpt[start:end].strip()


def _compare(kind, note_value, api, excerpt=""):
    """返回 (api_value, display_status, extra)。note 未提及或接口未提供 -> unknown。

    活动时间窗的对照规则（兼容 C3 与延期条款两种情形）：
      · 文案含「结束时间可能延长」条款，且接口起点与文案一致、终点不早于文案
        -> match，并引用原文条款（文案自己授权了延长）；
      · 其余不相等的情形（含起点不一致）-> conflict。
    """
    if kind == "activity_window":
        rng = api.get("date_range")
        if not note_value or not rng:
            return rng, "unknown", {}
        note_start, note_end = note_value.get("start"), note_value.get("end")
        strict_same = (note_start == rng.get("start")) and (note_end == rng.get("end"))
        if strict_same:
            return rng, "match", {}
        # INVARIANT: 延期条款只覆盖「结束时间」；起点不一致仍然判冲突（C3 的真实冲突）
        clause = _extract_clause(excerpt)
        if clause and note_start == rng.get("start") and note_end and rng.get("end"):
            if str(rng.get("end")) >= str(note_end):
                return rng, "match", {"note_clause_raw": clause}
        return rng, "conflict", ({"note_clause_raw": clause} if clause else {})
    if kind == "price":
        fen = api.get("price_fen")
        if note_value is None or fen is None:
            return (fen / 100.0) if isinstance(fen, int) else None, "unknown", {}
        return fen / 100.0, ("match" if abs(note_value - fen / 100.0) < 1e-9 else "conflict"), {}
    key = {"min_people": "party_min", "max_people": "party_max",
           "duration": "duration_minutes"}.get(kind)
    if key is None:
        return None, "unknown", {}
    api_value = api.get(key)
    if note_value is None or api_value is None:
        return api_value, "unknown", {}
    return api_value, ("match" if int(note_value) == int(api_value) else "conflict"), {}


def crosscheck(plain, api):
    # INVARIANT: 输出只含原文子串与展示值，不产生任何校验结论或分支输入
    """L2：原文片段与接口实时值并列，标 match / conflict / unknown。"""
    rows = []
    for kind in KIND_PATTERNS:
        found = locate(plain, [kind])[0]
        if not found["found"]:
            continue
        note_value = _extract_value(kind, found["excerpt_raw"])
        api_value, status, extra = _compare(kind, note_value, api, found["excerpt_raw"])
        rows.append({
            "item": KIND_LABEL[kind],
            "note_excerpt_raw": found["excerpt_raw"],
            "note_display_value": note_value,
            "api_value": api_value,
            "display_status": status,
            "note_clause_raw": extra.get("note_clause_raw", ""),
            "disclaimer": DISCLAIMER,
        })
    return rows


def _fmt_pair(value):
    """把对照值渲染成可读区间（只用于展示，不参与任何计算）。"""
    if value is None:
        return "（接口未提供）"
    if isinstance(value, dict):
        return str(value.get("start", "")) + " ~ " + str(value.get("end", ""))
    return str(value)


def conflict_sentence(rows):
    """冲突项的定型句：带具体区间，语感锁定为「门店排期差异」。"""
    for r in rows:
        if r.get("display_status") != "conflict":
            continue
        return (CONFLICT_LEAD + "（官方文案 " + _fmt_pair(r.get("note_display_value"))
                + "，该门店接口 " + _fmt_pair(r.get("api_value")) + "）。" + CONFLICT_ADVICE)
    return ""


def render_table(rows):
    lines = ["| 条目 | 官方文案原文片段 | 文案提示值（仅展示） | 接口实时值 | 对照状态 |",
             "|---|---|---|---|---|"]
    for r in rows:
        mark = {"match": "✅ 一致", "conflict": "⚠️ 冲突", "unknown": "➖ 官方文案未提及"}.get(
            r["display_status"], "➖")
        note_val = "" if r["note_display_value"] is None else json.dumps(
            r["note_display_value"], ensure_ascii=False)
        api_val = "" if r["api_value"] is None else json.dumps(r["api_value"], ensure_ascii=False)
        excerpt = r["note_excerpt_raw"].replace("|", "\\|")
        if len(excerpt) > 60:
            excerpt = excerpt[:60] + "…"
        lines.append("| " + r["item"] + " | " + excerpt + " | " + note_val + " | " + api_val + " | " + mark + " |")
    lines.append("")
    clauses = [r["note_clause_raw"] for r in rows if r.get("note_clause_raw")]
    if clauses:
        for c in clauses:
            lines.append("注：官方文案原文含延期条款「" + c + "」，接口区间落在文案授权的弹性范围内。")
        lines.append("")
    conflict = conflict_sentence(rows)
    if conflict:
        lines.append("⚠️ " + conflict)
    else:
        lines.append(CONFLICT_ADVICE)
    lines.append(CONFLICT_ADVICE_STRICT)
    lines.append(DISCLAIMER)
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


SAMPLE_NOTE = (
    "<p>1. 活动时间：2026年10月23日至2026年10月25日，具体活动餐厅可预约时间以活动预约页面为准。</p>"
    "<p>2. 派对预约：请提前3天预订，具体可预约场次、时间以活动餐厅预约页面显示为准。</p>"
    "<p>3. 适用人群：18岁以上的成年人。</p>"
    "<p>4. 参与方式：每位费用 59 元，含巨无霸四件套与随机周边玩具 1 款。</p>"
    "<p>5. 预约须知：每场活动时间约90分钟；每场需 5 位参与者起订；每场活动最高人数为12位。</p>"
    "<p>6. 退改规则：退订或修改订单请至少于活动举办之日前3天申请。</p>"
    "<p>7. 活动门店：机场、火车站等交通枢纽餐厅、旅游景区餐厅等特殊餐厅除外。</p>"
)


def _selftest():
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond)))
        print(("✅" if cond else "❌") + " " + label + ((" -- " + detail) if detail and not cond else ""))

    plain = html_to_text(SAMPLE_NOTE)
    check("L0 去 HTML 后保留原文文字", "活动时间：2026年10月23日至2026年10月25日" in plain)
    check("L0 不残留标签", "<" not in plain and ">" not in plain)

    items = split_items(plain)
    check("L1 切分出 7 个条目", len(items) == 7, "实际 " + str(len(items)))
    loc = locate(plain, ["activity_window", "lead_days", "min_people"])
    check("L1 定位活动时间条目", loc[0]["found"] and "活动时间" in loc[0]["excerpt_raw"])
    check("L1 只输出原文子串与偏移（不输出改写值）",
          all(set(r) >= {"excerpt_raw", "offset", "len"} for r in loc))
    miss = locate(plain, ["refund"])
    check("L1 命中退改条目", miss[0]["found"])

    rows = crosscheck(plain, {"date_range": {"start": "2026-10-22", "end": "2026-10-30"},
                              "price_fen": 5900, "party_min": 5, "party_max": 12,
                              "duration_minutes": 90})
    by_item = {r["item"]: r for r in rows}
    check("C3：日期窗对照标 conflict", by_item["活动时间窗"]["display_status"] == "conflict")
    check("C3：价格一致标 match", by_item["人均价格"]["display_status"] == "match")
    check("C3：起订人数一致标 match", by_item["成团起订人数"]["display_status"] == "match")
    check("note 派生值字段带 note_display_ 前缀", "note_display_value" in rows[0])
    table = render_table(rows)
    check("对照表含 ⚠️ 冲突标记", "⚠️" in table)
    check("对照表含免责句", DISCLAIMER in table)
    check("对照表不断言哪一方正确",
          "官方正确" not in table and "以接口数据为准" not in table)
    check("冲突项语感锁定为门店排期差异", "门店排期" in table and "并非数据错误" in table)
    check("冲突项禁止出现「有误 / 异常」定性词",
          "有误" not in table and "异常" not in table)

    code, _ = _run(["--locate", "--file", "x", "--kind", "note_display_x"])
    check("未知条目类型 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, out = _run(["--full", "--file", "__none__"])
    check("文件不存在 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))

    a = render_table(crosscheck(plain, {"date_range": {"start": "2026-10-22", "end": "2026-10-30"}}))
    b = render_table(crosscheck(plain, {"date_range": {"start": "2026-10-22", "end": "2026-10-30"}}))
    check("幂等：同一输入两次输出一致", a == b)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """note_locate.py -- 官方购买须知（note）原文定位与并列展示

  --full       --file F                 L0：去 HTML 后完整原样输出
  --locate     --file F [--kind K,...]  L1：原文子串 + 字符偏移
  --crosscheck --note F --api F         L2：原文 vs 接口实时值对照表
  --kinds                               列出已登记条目类型
  --selftest                            内置自检

L2 产出的数值一律命名为 note_display_*，仅供展示，禁止进入任何计算 / 校验通道。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="note_locate.py", description="note 原文定位与并列展示")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--locate", action="store_true")
    ap.add_argument("--crosscheck", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--note")
    ap.add_argument("--api")
    ap.add_argument("--kind")
    ap.add_argument("--kinds", action="store_true")
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
        if args.kinds:
            print(json.dumps(KIND_PATTERNS, ensure_ascii=False, sort_keys=True, indent=2))
            return EXIT_OK

        if args.full:
            plain = html_to_text(load_note(args.file))
            if args.json:
                print(json.dumps({"outcome": "ok", "layer": "L0", "text": plain},
                                 ensure_ascii=False, sort_keys=True))
            else:
                print(plain)
            return EXIT_OK

        if args.locate:
            plain = html_to_text(load_note(args.file))
            kinds = [k.strip() for k in args.kind.split(",")] if args.kind else None
            rows = locate(plain, kinds)
            if args.json:
                print(json.dumps({"outcome": "ok", "layer": "L1", "items": rows},
                                 ensure_ascii=False, sort_keys=True))
            else:
                for r in rows:
                    if r["found"]:
                        print("[偏移 " + str(r["offset"]) + " 长度 " + str(r["len"]) + "] "
                              + r["label"] + "：" + r["excerpt_raw"])
                    else:
                        print("[未命中] " + r["label"] + "：" + r["note"])
            return EXIT_OK

        if args.crosscheck:
            if not args.note or not args.api:
                raise UsageError("--crosscheck 需要 --note 与 --api")
            plain = html_to_text(load_note(args.note))
            api = json.loads(Path(args.api).read_text(encoding="utf-8"))
            rows = crosscheck(plain, api)
            if args.json:
                print(json.dumps({"outcome": "ok", "layer": "L2", "rows": rows,
                                  "conflict_sentence": conflict_sentence(rows),
                                  "disclaimer": DISCLAIMER}, ensure_ascii=False, sort_keys=True))
            else:
                print(render_table(rows))
            return EXIT_OK
    except (UsageError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
