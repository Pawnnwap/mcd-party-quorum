#!/usr/bin/env python3
"""money.py -- 金额规范化层（元 <-> 分）与唯一渲染出口。

背景（设计蓝图 1.1 坑 A）：同一个键名 price 在不同接口里单位差 100 倍。

    mall-points-products        data[].price        string  "59"   单位=元
    mall-product-detail         skuList[].price     string  "59"   单位=元
    query-party-store-session   data[].price        int     5900   单位=分

裁决：
  1. 内部一律以「分（int）」存储与计算；
  2. 渲染出口只有 format_cny()，禁止任何地方裸显 5900；
  3. 来源单位由 SOURCE_UNIT 按「字段路径」查表决定，未知来源 exit 2，禁止猜测单位；
  4. note 派生的任何数值（参数名含 note_display）禁止进入本模块。

退出码：0 正常 / 1 断言失败（出口守卫命中裸值）/ 2 入参错误。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_ROOT = Path(__file__).resolve().parent.parent

# 来源字段路径 -> 单位。新增来源必须显式登记，禁止默认猜测。
SOURCE_UNIT = {
    "mall-points-products.data[].price": "yuan_str",
    "mall-product-detail.skuList[].price": "yuan_str",
    "mall-product-detail.data.skuList[].price": "yuan_str",
    "query-party-store-session.data[].price": "fen_int",
}

UNIT_LABEL = {
    "yuan_str": "元（字符串，如 \"59\"）",
    "fen_int": "分（整数，如 5900）",
}

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    # INVARIANT: note 派生值永不进入金额层 —— 命中前缀即拒绝，金额只认接口返回
    """参数名守卫：note 派生值禁止进入计算通道。

    命中即 exit 2 —— 这是设计蓝图 1.4 L3 层的结构性保证，不是靠自觉。
    """
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入计算通道：入参出现 note_display_*（收到 "
                + token
                + "）。note 只能原文展示，所有校验以接口返回为准。"
            )


def to_fen(value, source):
    """按来源把金额规范为「分（int）」。未知来源直接报错，绝不猜测单位。"""
    if source is None:
        raise UsageError("缺少 --source：单位必须由来源字段路径查表决定，禁止猜测。")
    # INVARIANT: 单位只能由来源字段路径查表得到，未知来源一律拒绝，绝不猜测
    unit = SOURCE_UNIT.get(source)
    if unit is None:
        known = "、".join(sorted(SOURCE_UNIT))
        raise UsageError(
            "未知金额来源 '" + str(source) + "'：无法判定单位，禁止默认猜测。"
            "已登记来源：" + known
        )
    raw = "" if value is None else str(value).strip()
    if raw == "":
        raise UsageError("金额为空值。")
    if unit == "yuan_str":
        try:
            dec = Decimal(raw)
        except InvalidOperation:
            raise UsageError("金额 '" + raw + "' 不是合法数字（来源单位=元）。")
        if dec < 0:
            raise UsageError("金额不允许为负数：'" + raw + "'。")
        return int((dec * 100).to_integral_value(rounding=ROUND_HALF_UP))
    # fen_int
    if not re.fullmatch(r"\d+", raw):
        raise UsageError(
            "金额 '" + raw + "' 不是非负整数分（来源单位=分，收到小数或负数即报错）。"
        )
    return int(raw)


def format_cny(fen):
    # INVARIANT: 用户可见金额字符串的唯一出口 —— 卡面上不允许出现裸分数值
    """唯一渲染出口：分 -> 人民币展示串。整数元不带 .00，非整数保留两位。"""
    if isinstance(fen, bool) or not isinstance(fen, int):
        raise UsageError("format_cny 只接受整数分，收到 " + repr(fen))
    sign = "-" if fen < 0 else ""
    n = abs(fen)
    if n % 100 == 0:
        return sign + "¥" + str(n // 100)
    return sign + "¥" + ("%.2f" % (n / 100.0))


def find_bare_fen(text, fen_values):
    """出口守卫：文本中出现裸分数值即返回命中列表（C8）。"""
    # INVARIANT: 出口文本中不得残留任何裸分数值（C8），命中即拒绝出卡
    hits = []
    for fen in fen_values:
        pattern = re.compile(r"(?<![\d.])" + str(int(fen)) + r"(?![\d.])")
        for m in pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append({"fen": int(fen), "line": line_no, "matched": m.group(0)})
    return hits


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _run(argv):
    """进程内执行本脚本 CLI，返回 (exit_code, stdout_text)。"""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(argv)
    except SystemExit as exc:  # argparse 失败
        code = int(exc.code) if exc.code is not None else EXIT_OK
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue()


def _selftest():
    results = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))
        print(("✅" if cond else "❌") + " " + label + ((" -- " + detail) if detail and not cond else ""))

    check("元字符串转分：\"59\" -> 5900", to_fen("59", "mall-product-detail.skuList[].price") == 5900)
    check("分整数直通：5900 -> 5900", to_fen(5900, "query-party-store-session.data[].price") == 5900)
    check("元带小数：\"59.5\" -> 5950", to_fen("59.5", "mall-points-products.data[].price") == 5950)
    check("渲染出口：5900 -> ¥59", format_cny(5900) == "¥59")
    check("渲染出口：17700 -> ¥177", format_cny(17700) == "¥177")
    check("渲染出口：1234 -> ¥12.34", format_cny(1234) == "¥12.34")

    code, _ = _run(["--to-fen", "59", "--source", "unknown.price"])
    check("未知来源单位 -> exit 2（禁止猜测单位）", code == EXIT_USAGE, "实际 exit=" + str(code))

    code, _ = _run(["--to-fen", "59", "--source", "note_display_price"])
    check("note 派生值进入金额层 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))

    code, out1 = _run(["--check", "人均 ¥59 · 3 人共 ¥177", "--fen", "5900"])
    check("出口守卫：洁净文本 -> exit 0", code == EXIT_OK, "实际 exit=" + str(code))
    code, out2 = _run(["--check", "price=5900", "--fen", "5900"])
    check("出口守卫：裸值 5900 -> exit 1", code == EXIT_FAIL, "实际 exit=" + str(code))
    check("出口守卫报告命中位置", "5900" in out2 and "裸值" in out2)

    code, a = _run(["--format", "5900"])
    code2, b = _run(["--format", "5900"])
    check("幂等：同一输入两次执行输出逐字一致", a == b, repr(a) + " != " + repr(b))

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """money.py -- 金额规范化（内部一律用分，出口只有 format_cny）

  --to-fen VALUE --source KEY   按来源把金额规范为分
  --format FEN                  分 -> 人民币展示串（唯一渲染出口）
  --check TEXT --fen FEN        出口守卫：文本中出现裸分数值 -> exit 1
  --sources                     列出已登记来源与单位
  --selftest                    内置自检（不联网、不读 fixtures）

已登记来源：
""" + "\n".join("  " + k + "  ->  " + UNIT_LABEL[v] for k, v in sorted(SOURCE_UNIT.items()))


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="money.py", description="金额规范化层（元 <-> 分）")
    ap.add_argument("--to-fen")
    ap.add_argument("--source")
    ap.add_argument("--format")
    ap.add_argument("--check")
    ap.add_argument("--fen", action="append", default=[], help="出口守卫要扫描的裸分数值（可重复）")
    ap.add_argument("--text-file", help="从文件读取待检文本，替代 --check")
    ap.add_argument("--sources", action="store_true")
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

    if args.sources:
        print(json.dumps(SOURCE_UNIT, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK

    if args.to_fen is not None:
        try:
            fen = to_fen(args.to_fen, args.source)
        except UsageError as exc:
            sys.stderr.write("❌ " + str(exc) + "\n")
            return EXIT_USAGE
        if args.json:
            print(json.dumps({"outcome": "ok", "fen": fen, "source": args.source,
                              "unit": SOURCE_UNIT[args.source], "cny": format_cny(fen)},
                             ensure_ascii=False, sort_keys=True))
        else:
            print("分：" + str(fen) + " ｜ 展示：" + format_cny(fen) + " ｜ 来源单位：" + UNIT_LABEL[SOURCE_UNIT[args.source]])
        return EXIT_OK

    if args.format is not None:
        try:
            fen = int(str(args.format).strip())
        except ValueError:
            sys.stderr.write("❌ --format 只接受整数分，收到 '" + str(args.format) + "'\n")
            return EXIT_USAGE
        if args.json:
            print(json.dumps({"outcome": "ok", "fen": fen, "cny": format_cny(fen)},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(format_cny(fen))
        return EXIT_OK

    if args.check is not None or args.text_file:
        text = args.check if args.check is not None else ""
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
        values = [int(str(x).strip()) for x in args.fen]
        if not values:
            sys.stderr.write("❌ --check 至少需要一个 --fen\n")
            return EXIT_USAGE
        hits = find_bare_fen(text, values)
        if args.json:
            print(json.dumps({"outcome": "ok" if not hits else "blocked", "bare_fen_hits": hits},
                             ensure_ascii=False, sort_keys=True))
        else:
            if hits:
                print("❌ 出口守卫命中裸分数值 " + str(len(hits)) + " 处（C8：禁止裸显分）")
                for h in hits:
                    print("   第 " + str(h["line"]) + " 行出现裸值 " + h["matched"])
            else:
                print("✅ 出口守卫通过：未出现裸分数值")
        return EXIT_OK if not hits else EXIT_FAIL

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
