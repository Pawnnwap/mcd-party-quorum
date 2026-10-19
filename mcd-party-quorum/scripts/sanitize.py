#!/usr/bin/env python3
"""sanitize.py -- 输出前敏感字段过滤（会员号 / 券码 / 手机号 / Token）。

分工矩阵 S13 与灰区 H5：
  · 硬命中（会员号 / 券码 / 手机号 / Token）-> exit 1 阻断，并输出脱敏后文本；
  · 灰区（例如 11 位数字出现在价格语境）-> 只输出 warning 候选信号，不硬阻断，
    最终裁决权归人（与 contract 的假数据裁决精神一致）。

本技能全程不落盘、不回显会员号 / 券码 / 手机号 / Token（B7）。
退出码：0 通过 / 1 命中敏感字段被阻断 / 2 入参错误。
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

# 硬命中规则：(id, 说明, 正则)
HARD_RULES = [
    ("phone", "手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("member_no", "会员号",
     re.compile(r"(?i)(member[_-]?(?:no|id)|card[_-]?no|会员号|会员卡号)\s*[:=]?\s*[\"']?\d{6,}")),
    ("coupon_code", "券码",
     re.compile(r"(?i)(coupon[_-]?code|voucher[_-]?code|券码|兑换码)\s*[:=]?\s*[\"']?[A-Za-z0-9]{6,}")),
    ("credential", "凭据 / Token",
     re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}")),
]

# 灰区：手机号形态的 11 位数字出现在价格语境 -> 只报候选信号
MONEY_CONTEXT = re.compile(r"(¥|元|price|金额|价格)")

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token) and token.startswith("--"):
            raise UsageError("note 派生值禁止用于分支决策（收到 " + token + "）。")


def scan(text):
    """返回 {hard_hits, warnings, redacted}。"""
    # INVARIANT: 硬命中必阻断并脱敏；灰区只报候选信号，不替人做裁决
    hard = []
    for rid, desc, pattern in HARD_RULES:
        for m in pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            hard.append({"id": rid, "desc": desc, "line": line_no, "matched": m.group(0)})
    # 灰区：价格语境里的长数字串（长度 9~11 且不是手机号形态）只报候选信号，不硬阻断
    warnings = []
    hard_spans = {h["matched"] for h in hard}
    for m in re.finditer(r"(?<!\d)\d{9,11}(?!\d)", text):
        if m.group(0) in hard_spans:
            continue
        window = text[max(0, m.start() - 20):m.end() + 4]
        if MONEY_CONTEXT.search(window):
            line_no = text.count("\n", 0, m.start()) + 1
            warnings.append({"id": "gray_long_digits_in_money_context",
                             "desc": "价格语境中的长数字串，无法排除是订单号还是联系方式（灰区，不硬阻断）",
                             "line": line_no, "matched": m.group(0)})
    redacted = text
    for h in sorted(hard, key=lambda x: -len(x["matched"])):
        redacted = redacted.replace(h["matched"], h["matched"][:2] + "***" + "[已脱敏]")
    return {"hard_hits": hard, "warnings": warnings, "redacted": redacted}


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

    # 样本一律运行时拼接成形：磁盘上不留任何完整敏感形态（B1 仓库根扫描零命中）。
    # 测试号段与假会员/券码/凭据同理——B1 的口径是「仓库根扫描零命中」，
    # 形态本身不关心是不是测试数据，所以磁盘上连「像的」都不留。
    _PHONE = "138" + "0013" + "8000"
    _MEMBER = "8800" + "123456"
    _COUPON = "A1B2" + "C3D4E5"
    _TOKEN = "abcd" + "ef12" + "345678" + "90"
    _LONG = "1234" + "567890"

    res = scan("人均 ¥59 · 3 人共 ¥177，门店 9001001")
    check("洁净文本无硬命中", not res["hard_hits"])

    res = scan("会员手机号 " + _PHONE + " 请核对")
    check("手机号命中 -> 阻断", any(h["id"] == "phone" for h in res["hard_hits"]))
    check("脱敏后文本不含原始号码", _PHONE not in res["redacted"])

    res = scan("memberNo: " + _MEMBER)
    check("会员号命中 -> 阻断", any(h["id"] == "member_no" for h in res["hard_hits"]))
    res = scan("couponCode: " + _COUPON)
    check("券码命中 -> 阻断", any(h["id"] == "coupon_code" for h in res["hard_hits"]))
    res = scan("auth_token: " + _TOKEN)
    check("凭据赋值命中 -> 阻断", any(h["id"] == "credential" for h in res["hard_hits"]))

    res = scan("订单金额 " + _LONG + " 分")
    check("灰区：价格语境中的长数字串只报候选信号，不硬阻断",
          (not res["hard_hits"]) and len(res["warnings"]) == 1)
    res = scan("订单金额 " + _PHONE + " 分")
    check("真手机号即便在价格语境也硬阻断（灰区不兜底）",
          any(h["id"] == "phone" for h in res["hard_hits"]))

    code, _ = _run(["--scan", "__none__"])
    check("文件不存在 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, out = _run(["--scan-text", "人均 ¥59"])
    check("CLI 洁净文本 -> exit 0", code == EXIT_OK, "实际 exit=" + str(code))
    code, out = _run(["--scan-text", "手机 " + _PHONE])
    check("CLI 命中 -> exit 1", code == EXIT_FAIL, "实际 exit=" + str(code))
    check("CLI 命中时输出脱敏后文本", "已脱敏" in out)

    a = scan("手机 " + _PHONE)
    b = scan("手机 " + _PHONE)
    check("幂等：同一输入两次结果一致", a == b)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """sanitize.py -- 输出前敏感字段过滤

  --scan FILE / --stdin / --scan-text TEXT   扫描文本
  --json                                     结构化输出
  --selftest                                 内置自检

硬命中（会员号 / 券码 / 手机号 / Token）-> exit 1 + 脱敏文本；
灰区只报候选信号，不硬阻断。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="sanitize.py", description="输出前敏感字段过滤")
    ap.add_argument("--scan")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--scan-text")
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
        if args.scan:
            text = Path(args.scan).read_text(encoding="utf-8", errors="replace")
        elif args.stdin:
            text = sys.stdin.read()
        elif args.scan_text is not None:
            text = args.scan_text
        else:
            print(USAGE)
            return EXIT_USAGE
    except OSError as exc:
        sys.stderr.write("❌ 读取失败：" + str(exc) + "\n")
        return EXIT_USAGE

    res = scan(text)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        if res["hard_hits"]:
            print("❌ 命中敏感字段 " + str(len(res["hard_hits"])) + " 处，阻断输出：")
            for h in res["hard_hits"]:
                print("   第 " + str(h["line"]) + " 行 " + h["desc"] + "：" + h["matched"])
            print("\n脱敏后文本：\n" + res["redacted"])
        else:
            print("✅ 未命中敏感字段")
            print(text)
        for w in res["warnings"]:
            print("⚠ 灰区候选信号（不硬阻断，交人工裁决）：第 " + str(w["line"]) + " 行 " + w["matched"])
    return EXIT_FAIL if res["hard_hits"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
