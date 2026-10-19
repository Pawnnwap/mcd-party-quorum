#!/usr/bin/env python3
"""order_params.py -- party-order-create 下单参数装配与 schema 对齐校验。

11 个字段：partyType（必填）+ code / count / dateStr / id / leftNum / skuId /
spuId / storeCode / timeEnd / timeStart。

规则（设计蓝图 5.7 与分工矩阵 S8）：
  · 缺字段 -> exit 2 并列出缺失项，禁止带病输出；
  · 每个字段必须标注 source，取值只能是 api 或 user；
  · source == agent_invented -> exit 2。spuId / skuId / orderId 只能来自接口返回
    或用户明确提供，禁止由 Agent 编造（C14 / C2）。

本脚本只做「对齐校验 + 列出参数清单」，绝不发起 party-order-create 调用。
退出码：0 正常 / 1 断言失败 / 2 入参错误。
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

REQUIRED_FIELDS = [
    "code", "count", "dateStr", "id", "leftNum", "partyType",
    "skuId", "spuId", "storeCode", "timeEnd", "timeStart",
]

FIELD_LABEL = {
    "code": "城市编码（来自 query-party-city）",
    "count": "购买数量（用户确认）",
    "dateStr": "预约日期（来自 query-party-store-date）",
    "id": "场次 ID（来自 query-party-store-session）",
    "leftNum": "本场余位（来自 query-party-store-session）",
    "partyType": "成团方式（1 包场 / 2 拼团，用户拍板）",
    "skuId": "SKU ID（来自 mall-product-detail 的 skuList）",
    "spuId": "活动 ID（来自 mall-points-products）",
    "storeCode": "门店编码（来自 query-party-store）",
    "timeEnd": "场次结束时间（来自 query-party-store-session）",
    "timeStart": "场次开始时间（来自 query-party-store-session）",
}

VALID_SOURCES = ("api", "user")
FORBIDDEN_SOURCES = ("agent_invented", "model_invented", "guessed")

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入下单参数：入参出现 note_display_*（收到 " + token + "）。"
            )


def load_fields(path):
    p = Path(path)
    if not p.is_file():
        raise UsageError("找不到参数文件：" + str(p))
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError("参数文件不是合法 JSON：" + str(exc))
    if isinstance(obj, dict) and isinstance(obj.get("fields"), dict):
        return obj["fields"]
    if isinstance(obj, dict):
        return obj
    raise UsageError("参数文件必须是对象（字段 -> {value, source}）或含 fields 键的对象。")


def load_schema(path):
    if not path:
        return list(REQUIRED_FIELDS)
    p = Path(path)
    if not p.is_file():
        raise UsageError("找不到 schema 文件：" + str(p))
    obj = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(obj, list):
        return list(obj)
    if isinstance(obj, dict):
        for key in ("required", "fields", "properties"):
            if key in obj:
                val = obj[key]
                return list(val.keys()) if isinstance(val, dict) else list(val)
    raise UsageError("schema 文件必须是字段数组，或含 required / fields / properties 的对象。")


def check(fields, schema):
    """对齐校验：缺失项、来源合法性、臆造值一律 exit 2。"""
    # INVARIANT: 缺失字段只报错，绝不静默填默认值；来源只认 api 或 user
    errors = []
    rows = []
    for name in schema:
        entry = fields.get(name)
        if entry is None:
            errors.append("缺少字段 " + name + "（" + FIELD_LABEL.get(name, "") + "）")
            rows.append({"field": name, "present": False, "source": None, "value": None,
                         "label": FIELD_LABEL.get(name, "")})
            continue
        if isinstance(entry, dict):
            value = entry.get("value")
            source = entry.get("source")
        else:
            value = entry
            source = None
        if not source:
            errors.append("字段 " + name + " 缺少 source 标注（必须是 api 或 user）")
        elif source not in VALID_SOURCES:
            if source in FORBIDDEN_SOURCES:
                errors.append("字段 " + name + " 的来源是 " + source
                              + "：禁止由 Agent 编造，spuId / skuId / orderId 只能来自接口返回或用户明确提供")
            else:
                errors.append("字段 " + name + " 的 source '" + str(source)
                              + "' 非法，只能是 " + " 或 ".join(VALID_SOURCES))
        if value is None or (isinstance(value, str) and value.strip() == ""):
            errors.append("字段 " + name + " 缺值")
        rows.append({"field": name, "present": True, "source": source, "value": value,
                     "label": FIELD_LABEL.get(name, "")})
    extra = [k for k in fields if k not in schema]
    return {"rows": rows, "errors": errors, "extra": extra,
            "complete": not errors and not extra}


def render(rows, errors, extra):
    lines = ["| 字段 | 值 | 来源 | 说明 |", "|---|---|---|---|"]
    for r in rows:
        val = "" if r["value"] is None else str(r["value"])
        src = str(r["source"]) if r["source"] else "缺失"
        lines.append("| " + r["field"] + " | " + val + " | " + src + " | " + r["label"] + " |")
    lines.append("")
    if errors:
        lines.append("❌ 对齐校验未通过：")
        for e in errors:
            lines.append("   - " + e)
    else:
        lines.append("✅ 11 个字段齐备，每个字段的 source 均为 api 或 user，无臆造值。")
    if extra:
        lines.append("⚠ 多余字段：" + "、".join(extra) + "（不在 party-order-create schema 内）")
    lines.append("")
    lines.append("📌 本技能不会代你执行：请在你自己的 MCP 客户端确认后执行下单。")
    lines.append("📌 以上参数仅供核对，实际可下单性以 party-order-create 返回结果为准。")
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


GOOD_FIELDS = {
    "code": {"value": "330100", "source": "api"},
    "count": {"value": 3, "source": "user"},
    "dateStr": {"value": "2026-10-23", "source": "api"},
    "id": {"value": 37220241, "source": "api"},
    "leftNum": {"value": 12, "source": "api"},
    "partyType": {"value": 2, "source": "user"},
    "skuId": {"value": 18998, "source": "api"},
    "spuId": {"value": 91001, "source": "api"},
    "storeCode": {"value": "9001001", "source": "api"},
    "timeEnd": {"value": "12:30", "source": "api"},
    "timeStart": {"value": "11:00", "source": "api"},
}


def _selftest():
    results = []

    def expect(label, cond, detail=""):
        results.append((label, bool(cond)))
        print(("✅" if cond else "❌") + " " + label + ((" -- " + detail) if detail and not cond else ""))

    expect("schema 共 11 个字段", len(REQUIRED_FIELDS) == 11)
    expect("partyType 在 schema 内", "partyType" in REQUIRED_FIELDS)

    res = check(GOOD_FIELDS, REQUIRED_FIELDS)
    expect("C14：齐备参数通过对齐校验", res["complete"] and not res["extra"])
    expect("C14：每个字段 source 均为 api 或 user",
          all(r["source"] in VALID_SOURCES for r in res["rows"]))

    partial = dict(GOOD_FIELDS)
    del partial["spuId"]
    res2 = check(partial, REQUIRED_FIELDS)
    expect("缺字段 -> 报缺失项", any("缺少字段 spuId" in e for e in res2["errors"]))

    invented = dict(GOOD_FIELDS)
    invented["spuId"] = {"value": 91001, "source": "agent_invented"}
    res3 = check(invented, REQUIRED_FIELDS)
    expect("source == agent_invented -> 报臆造值",
          any("agent_invented" in e for e in res3["errors"]))

    no_source = dict(GOOD_FIELDS)
    no_source["skuId"] = {"value": 18998}
    res4 = check(no_source, REQUIRED_FIELDS)
    expect("缺 source 标注 -> 报错", any("缺少 source 标注" in e for e in res4["errors"]))

    code, out = _run(["--from", "__none__"])
    expect("参数文件不存在 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--from", "__none__", "--schema", "note_display_schema"])
    expect("note 派生值进入参数装配 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))

    text = render(res["rows"], res["errors"], res["extra"])
    expect("参数清单含「本技能不会代你执行」提示", "本技能不会代你执行" in text)
    expect("参数清单不含写调用动作", "party-order-create(" not in text)

    a = render(check(GOOD_FIELDS, REQUIRED_FIELDS)["rows"], [], [])
    b = render(check(GOOD_FIELDS, REQUIRED_FIELDS)["rows"], [], [])
    expect("幂等：同一输入两次输出一致", a == b)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """order_params.py -- 下单参数装配与 schema 对齐校验

  --from FILE [--schema FILE]    校验参数齐备性与来源合法性
  --fields                       列出 party-order-create 的 11 个字段
  --selftest                     内置自检

字段来源只能是 api 或 user；agent_invented 一律 exit 2。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="order_params.py", description="下单参数对齐校验")
    ap.add_argument("--from", dest="src")
    ap.add_argument("--schema")
    ap.add_argument("--fields", action="store_true")
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
        if args.fields:
            print(json.dumps(REQUIRED_FIELDS, ensure_ascii=False, indent=2))
            return EXIT_OK
        if args.src:
            fields = load_fields(args.src)
            schema = load_schema(args.schema)
            res = check(fields, schema)
            text = render(res["rows"], res["errors"], res["extra"])
            if args.json:
                print(json.dumps(res, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                print(text)
            return EXIT_OK if res["complete"] else EXIT_USAGE
    except (UsageError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
