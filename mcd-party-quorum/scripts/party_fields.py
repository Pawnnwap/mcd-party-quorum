#!/usr/bin/env python3
"""party_fields.py -- 字段抽取与过滤（活动雷达 / 在售窗 / 空态 / 展示型区间）。

覆盖设计蓝图 5.4 与分工矩阵 S1 / S2 / S9 / S15 / S17：

  --filter-products   shopId == 5 且 status == 2 且在售窗内（C1 / C13）
  --sale-window       upDate/downDate 优先，回退 upTime/downTime，皆无 exit 2
  --is-empty          空态枚举判定（data == [] / null / 文本空提示）
  --people-range      "5-12" -> (5, 12)，仅展示（display_only）
  --city-check        用户城市是否在开放城市集合内（C12，禁止编造门店）

时间字段名坑（设计蓝图 1.2 坑 B）：mall-points-products 返回 upTime/downTime，
而 mall-product-detail 实际返回 upDate/downDate（它自己的接口文档还写错了）。
因此抽取函数双读 + 回退；两组都没有 -> exit 2 报缺哪个字段，绝不猜。

所有涉及时间的判定必须传 --now（取自 now-time-info），禁用本地时间。
退出码：0 正常 / 1 断言失败 / 2 入参错误。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SKILL_ROOT = Path(__file__).resolve().parent.parent

PARTY_SHOP_ID = 5      # 派对 / 体验类（走现金 price）
POINTS_SHOP_ID = 2     # 纯积分兑换类（本技能不处理）
ON_SALE_STATUS = 2

WINDOW_PRIMARY = ("upDate", "downDate")     # mall-product-detail 实际返回
WINDOW_FALLBACK = ("upTime", "downTime")    # mall-points-products 返回

# 接口返回文本型空提示（登记制，命中即判空；未登记一律显式报错，不猜）
EMPTY_TEXT_HINTS = (
    "暂无", "无数据", "没有找到", "暂未开放", "无可用", "未查询到",
    "暂无可预约", "尚未开放", "没有可预约",
)

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")

DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入校验通道：入参出现 note_display_*（收到 " + token + "）。"
            )


def load_json(path):
    p = Path(path)
    if not p.is_file():
        raise UsageError("找不到输入文件：" + str(p))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError("输入文件不是合法 JSON：" + str(exc))


def pick_data(obj):
    """从响应里取 data；data 缺失返回 (False, None)。"""
    if isinstance(obj, list):
        return True, obj
    if isinstance(obj, dict):
        if "data" in obj:
            return True, obj["data"]
        return False, None
    return False, None


def parse_dt(text, field):
    raw = "" if text is None else str(text).strip()
    if not raw:
        return None
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise UsageError("字段 " + field + " 的时间格式无法解析：'" + raw + "'")


def require_now(now):
    if not now:
        raise UsageError("缺少 --now：禁用本地时间，服务器时间必须来自 now-time-info。")
    return parse_dt(now, "--now")


def sale_window(item, now=None):
    # INVARIANT: 双读顺序固定（先 upDate/downDate，回退 upTime/downTime）；
    # 两组都缺绝不默认在售，只显式报错
    """双读上下架时间字段。返回 {up, down, window_source, on_sale}。"""
    present_primary = [k for k in WINDOW_PRIMARY if k in item and str(item[k]).strip()]
    present_fallback = [k for k in WINDOW_FALLBACK if k in item and str(item[k]).strip()]
    if len(present_primary) == 2:
        up_key, down_key = WINDOW_PRIMARY
        window_source = "upDate/downDate"
    elif len(present_fallback) == 2:
        up_key, down_key = WINDOW_FALLBACK
        window_source = "upTime/downTime"
    else:
        missing_p = [k for k in WINDOW_PRIMARY if k not in present_primary]
        missing_f = [k for k in WINDOW_FALLBACK if k not in present_fallback]
        raise UsageError(
            "无法判定在售窗：缺失上架/下架时间字段。upDate/downDate 缺 "
            + "、".join(missing_p) + "；upTime/downTime 缺 " + "、".join(missing_f)
            + "。两组都不完整，不猜、不默认在售（spuId="
            + str(item.get("spuId", "未知")) + "）。"
        )
    up = parse_dt(item.get(up_key), up_key)
    down = parse_dt(item.get(down_key), down_key)
    on_sale = None
    if now is not None:
        on_sale = (now >= up) and (now <= down)
    return {"up": str(item[up_key]), "down": str(item[down_key]),
            "window_source": window_source, "on_sale": on_sale}


def filter_products(payload, now):
    # INVARIANT: 输出集合 ⊆ {shopId == 5 且 status == 2 且在售窗内}，无例外通道
    """shopId == 5 且 status == 2 且在售窗内。"""
    has_data, data = pick_data(payload)
    if not has_data or data is None:
        raise UsageError("响应缺少 data 字段，无法做活动过滤（不伪装成空态）。")
    if not isinstance(data, list):
        raise UsageError("data 不是数组，收到 " + type(data).__name__)
    kept, dropped = [], []
    for item in data:
        if not isinstance(item, dict):
            raise UsageError("data 元素不是对象，收到 " + type(item).__name__)
        spu_id = item.get("spuId")
        if "shopId" not in item:
            raise UsageError("条目缺 shopId 字段（spuId=" + str(spu_id) + "），无法路由商城类型。")
        if "status" not in item:
            raise UsageError("条目缺 status 字段（spuId=" + str(spu_id) + "），无法判定上下架。")
        try:
            shop_id = int(item["shopId"])
        except (TypeError, ValueError):
            raise UsageError("shopId 不是整数（spuId=" + str(spu_id) + "）。")
        try:
            status = int(item["status"])
        except (TypeError, ValueError):
            raise UsageError("status 不是整数（spuId=" + str(spu_id) + "）。")
        if shop_id != PARTY_SHOP_ID:
            dropped.append({"spuId": spu_id, "reason": "shop_id_not_party",
                            "detail": "shopId=" + str(shop_id) + "（仅处理 shopId=" + str(PARTY_SHOP_ID) + "）"})
            continue
        if status != ON_SALE_STATUS:
            dropped.append({"spuId": spu_id, "reason": "status_not_onsale",
                            "detail": "status=" + str(status) + "（要求 status=" + str(ON_SALE_STATUS) + "）"})
            continue
        win = sale_window(item, now)
        if win["on_sale"] is False:
            dropped.append({"spuId": spu_id, "reason": "not_in_sale_window",
                            "detail": "窗口 " + win["up"] + " ~ " + win["down"] + "（源 " + win["window_source"] + "）"})
            continue
        entry = {k: item.get(k) for k in ("spuId", "spuName", "catName", "price", "point", "selling")}
        entry["window_source"] = win["window_source"]
        entry["up"] = win["up"]
        entry["down"] = win["down"]
        kept.append(entry)
    return {"kept": kept, "dropped": dropped,
            "kept_spu_ids": [k["spuId"] for k in kept],
            "dropped_spu_ids": [d["spuId"] for d in dropped]}


def is_empty(payload, tool=""):
    # INVARIANT: 空态只由三种可枚举形态产生；技术故障必须报错，绝不降级伪装成空态
    """空态枚举判定。三种形态：data == [] / data 为 null 或缺失 / 登记过的文本空提示。"""
    has_data, data = pick_data(payload)
    if has_data:
        if data is None:
            return {"outcome": "empty", "reason": "data_null", "tool": tool}
        if isinstance(data, list) and len(data) == 0:
            return {"outcome": "empty", "reason": "data_empty_array", "tool": tool}
        return {"outcome": "not_empty", "reason": "data_nonempty", "tool": tool,
                "size": len(data) if isinstance(data, list) else 1}
    # 无 data 字段：只认登记过的文本空提示，其余一律显式报错，不猜
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for hint in EMPTY_TEXT_HINTS:
        if hint in text:
            return {"outcome": "empty", "reason": "text_hint", "hint": hint, "tool": tool}
    raise UsageError(
        "响应既无 data 字段、也未命中登记过的文本空提示，无法判定空态。"
        "这是技术故障而非业务空态，禁止伪装成空态（tool=" + (tool or "未知") + "）。"
    )


def people_range(raw):
    """partyPeople 是字符串区间，只解析为展示字段，绝不进校验通道。"""
    # INVARIANT: partyPeople 的解析结果恒带 display_only，永不参与任何人数校验
    s = "" if raw is None else str(raw).strip()
    out = {"raw": s, "display_only": True, "source": "partyPeople",
           "usage": "仅展示，不得用于任何人数校验（人数判定只用 session 的 partyMin/partyMax/leftNum）"}
    m = re.fullmatch(r"\s*(\d+)\s*[-~至]\s*(\d+)\s*", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        out["min"] = min(lo, hi)
        out["max"] = max(lo, hi)
        out["parsed"] = True
    else:
        out["parsed"] = False
        out["note"] = "解析失败，原样透传字符串（不做任何数值推断）"
    return out


def city_check(payload, city):
    # INVARIANT: 非开放城市只产出清单与引导，绝不产出任何门店（禁止编造）
    """用户城市是否在开放城市集合内（C12）。不在集合时列出开放城市，禁止编造门店。"""
    has_data, data = pick_data(payload)
    if not has_data or not isinstance(data, list):
        raise UsageError("城市响应缺少 data 数组，无法判定开放城市集合。")
    names = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            names.append({"code": item.get("code"), "name": str(item["name"])})
    target = "" if city is None else str(city).strip()
    hit = [c for c in names if c["name"] == target]
    return {"outcome": "ok" if hit else "degraded",
            "city": target,
            "in_open_list": bool(hit),
            "open_cities": names,
            "matched": hit[0] if hit else None,
            "rule": "不在开放城市集合时只列清单与引导，禁止编造门店"}


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

    # 在售窗双读：主字段 upDate/downDate
    check("双读主字段 upDate/downDate",
          sale_window({"upDate": "2026-10-17 10:30:00", "downDate": "2026-10-25 23:49:59"})["window_source"]
          == "upDate/downDate")
    # 回退字段 upTime/downTime
    check("回退字段 upTime/downTime",
          sale_window({"upTime": "2026-10-17 10:30:00", "downTime": "2026-10-25 23:59:59"})["window_source"]
          == "upTime/downTime")
    # 混合缺失 -> exit 2
    try:
        sale_window({"upDate": "2026-10-17 10:30:00", "downTime": "2026-10-25 23:59:59"})
        check("字段组不完整 -> exit 2", False, "未报错")
    except UsageError as exc:
        check("字段组不完整 -> exit 2（不猜、不默认在售）", "不猜" in str(exc))

    # 空态三种形态
    check("空态：data == []", is_empty({"data": []})["outcome"] == "empty")
    check("空态：data == null", is_empty({"data": None})["outcome"] == "empty")
    check("空态：文本空提示", is_empty({"message": "暂无可预约门店"})["outcome"] == "empty")
    try:
        is_empty({"message": "系统内部错误"})
        check("未登记的异常响应不伪装成空态", False, "未报错")
    except UsageError:
        check("未登记的异常响应 -> exit 2（不伪装成空态）", True)

    # partyPeople 只展示
    pr = people_range("5-12")
    check("partyPeople 解析 5-12 -> (5, 12)", pr["min"] == 5 and pr["max"] == 12)
    check("partyPeople 恒带 display_only", pr["display_only"] is True)
    check("partyPeople 解析失败原样透传", people_range("待定")["parsed"] is False)

    # 活动过滤：shopId / status / 在售窗
    payload = {"data": [
        {"spuId": 91001, "spuName": "主题活动A", "shopId": 5, "status": 2,
         "upDate": "2026-10-17 10:30:00", "downDate": "2026-10-25 23:49:59", "price": "59"},
        {"spuId": 91002, "spuName": "积分兑换B", "shopId": 2, "status": 2,
         "upTime": "2026-10-01 00:00:00", "downTime": "2026-12-31 23:59:59", "price": "0"},
        {"spuId": 91003, "spuName": "已下架C", "shopId": 5, "status": 1,
         "upDate": "2026-10-01 00:00:00", "downDate": "2026-12-31 23:59:59", "price": "99"},
        {"spuId": 91004, "spuName": "过期D", "shopId": 5, "status": 2,
         "upDate": "2026-09-01 00:00:00", "downDate": "2026-09-30 23:59:59", "price": "79"},
    ]}
    res = filter_products(payload, parse_dt("2026-10-18 23:07:42", "--now"))
    check("C1：只保留 shopId==5 且 status==2 且在售",
          [k["spuId"] for k in res["kept"]] == [91001])
    check("C13：下架/过期条目被丢弃并给出原因",
          {d["spuId"]: d["reason"] for d in res["dropped"]}
          == {91002: "shop_id_not_party", 91003: "status_not_onsale", 91004: "not_in_sale_window"})

    # 缺字段 -> exit 2
    code, _ = _run(["--filter-products", "--now", "2026-10-18 23:07:42", "--file", "__none__"])
    check("输入文件不存在 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--filter-products", "--file", "__none__"])
    check("缺 --now -> exit 2（禁用本地时间）", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--sale-window", "--file", "x", "--source", "note_display_price"])
    check("note 派生值进入字段层 -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))

    # 幂等
    a = json.dumps(filter_products(payload, parse_dt("2026-10-18 23:07:42", "--now")),
                   ensure_ascii=False, sort_keys=True)
    b = json.dumps(filter_products(payload, parse_dt("2026-10-18 23:07:42", "--now")),
                   ensure_ascii=False, sort_keys=True)
    check("幂等：同一输入两次执行结果一致", a == b)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """party_fields.py -- 字段抽取与过滤

  --filter-products --file F --now "YYYY-MM-DD HH:MM:SS"
  --sale-window     --file F [--now ...]        在售窗双读（upDate/downDate -> upTime/downTime）
  --is-empty        --file F [--tool T]         空态枚举判定
  --people-range    "5-12"                      partyPeople 展示型解析
  --city-check      --file F --city 名称        城市是否在开放集合内
  --selftest                                    内置自检

注：--file 接受「响应 JSON」或「裸数组」；--sale-window/--is-empty 的单条目形态
    也接受直接传条目 JSON。涉及时间的判定必须传 --now。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="party_fields.py", description="字段抽取与过滤")
    ap.add_argument("--filter-products", action="store_true")
    ap.add_argument("--sale-window", action="store_true")
    ap.add_argument("--is-empty", action="store_true")
    ap.add_argument("--people-range")
    ap.add_argument("--city-check", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--now")
    ap.add_argument("--tool")
    ap.add_argument("--city")
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

    def emit(obj, human):
        if args.json:
            print(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(human if human else json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2))

    try:
        if args.filter_products:
            if not args.file:
                raise UsageError("--filter-products 需要 --file")
            now = require_now(args.now)
            res = filter_products(load_json(args.file), now)
            res["outcome"] = "ok" if res["kept"] else "empty"
            res["now"] = args.now
            human = "保留 " + str(len(res["kept"])) + " 个活动；丢弃 " + str(len(res["dropped"])) + " 个\n"
            for k in res["kept"]:
                human += "  ✅ spuId=" + str(k["spuId"]) + " " + str(k.get("spuName")) + "（窗口 " + str(k["up"]) + " ~ " + str(k["down"]) + "，源 " + k["window_source"] + "）\n"
            for d in res["dropped"]:
                human += "  ⛔ spuId=" + str(d["spuId"]) + " 丢弃：" + d["reason"] + " -- " + d["detail"] + "\n"
            if not res["kept"]:
                human += "当前没有可预约的派对活动（C13 降级出口）。"
            emit(res, human)
            return EXIT_OK

        if args.sale_window:
            if not args.file:
                raise UsageError("--sale-window 需要 --file")
            payload = load_json(args.file)
            item = payload.get("data", payload) if isinstance(payload, dict) else payload
            if isinstance(item, list):
                item = item[0] if item else {}
            now = parse_dt(args.now, "--now") if args.now else None
            win = sale_window(item, now)
            emit(win, "上架 " + win["up"] + " ｜ 下架 " + win["down"] + " ｜ 字段源 " + win["window_source"]
                 + (" ｜ 在售 " + ("是" if win["on_sale"] else "否") if now else ""))
            return EXIT_OK

        if args.is_empty:
            if not args.file:
                raise UsageError("--is-empty 需要 --file")
            res = is_empty(load_json(args.file), args.tool or "")
            emit(res, "空态判定：" + res["outcome"] + "（原因 " + res["reason"] + "）")
            return EXIT_OK

        if args.people_range is not None:
            res = people_range(args.people_range)
            emit(res, "partyPeople 展示解析：" + json.dumps(res, ensure_ascii=False, sort_keys=True))
            return EXIT_OK

        if args.city_check:
            if not args.file or not args.city:
                raise UsageError("--city-check 需要 --file 与 --city")
            res = city_check(load_json(args.file), args.city)
            human = ("城市 " + res["city"] + " 在开放城市集合内" if res["in_open_list"]
                     else "城市 " + res["city"] + " 不在开放城市集合内；当前开放城市："
                     + "、".join(c["name"] for c in res["open_cities"])
                     + "。禁止编造门店：该城市没有门店数据就不给门店，只做引导。")
            emit(res, human)
            return EXIT_OK
    except UsageError as exc:
        sys.stderr.write("❌ " + str(exc) + "\n")
        return EXIT_USAGE

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
