#!/usr/bin/env python3
"""run_scenarios.py -- 场景回归执行器（MockTransport 端到端 + 断言）。

设计目的（设计蓝图 §7）：C4 要求「断言 query-party-store 调用次数为 0」、C5 要求
「断言 session 与写工具调用次数均为 0」。若 MCP 调用由 LLM 直接发起，脚本既拦不住
也记不全，断言形同虚设。因此本执行器把编排主权从 LLM 移到脚本：

  · Gate 票据：被拒的调用从源头不发生；
  · 账本完备：全部调用发生在本进程内部，账本即全部事实，assert_count(t, 0) 是真的 0；
  · 空态熔断：DEGRADED 的工具白名单是空集，结构上不可能产生下一跳。

断言类型：call_count / output_contains / output_not_contains / outcome /
json_path_equals。

内建守卫用例（不依赖 fixtures，进程内计算）：
  T-note-guard / T-table-sync / T-money-unit / T-overdue-still-cards /
  T-selftests / T-cwd-independent / T-write-tools-zero

退出码：0 全部通过 / 1 有断言失败 / 2 入参错误。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
FIXTURES = SKILL_ROOT / "tests" / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
RESPONSES = FIXTURES / "responses"
STATE_MD = SKILL_ROOT / "references" / "state-machine.md"

sys.path.insert(0, str(SCRIPTS))
import mcp_client  # noqa: E402
import state_machine  # noqa: E402

ALL_SCRIPTS = [
    "mcp_client.py", "state_machine.py", "money.py", "party_fields.py",
    "note_locate.py", "quorum_calc.py", "order_params.py", "sanitize.py",
    "run_scenarios.py",
]


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def run_cli(script, args, cwd=None):
    """以子进程执行脚本 CLI（真实 CLI 路径，cwd 默认技能根）。"""
    cmd = [sys.executable, str(SCRIPTS / script)] + [str(a) for a in args]
    proc = subprocess.run(cmd, cwd=str(cwd or SKILL_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _json_or_none(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ── 步骤执行 ─────────────────────────────────────────────────────────
def exec_step(step, ctx):
    """执行一步，返回 (ok, 说明)。失败即记录原因。"""
    do = step.get("do")
    log = ctx["log"]
    problems = []

    if do == "gate":
        code, out, err = run_cli("mcp_client.py",
                                 ["--gate", step["tool"], "--state", step.get("state", ""), "--json"]
                                 if step.get("state") else ["--gate", step["tool"], "--json"])
        data = _json_or_none(out) or {}
        ctx["last"] = out
        ctx["outputs"][step.get("capture", "last")] = out
        if step.get("expect"):
            e = step["expect"]
            if "exit" in e and code != e["exit"]:
                problems.append("gate " + step["tool"] + " 期望 exit=" + str(e["exit"]) + "，实际 " + str(code))
            if "allowed" in e and data.get("allowed") is not e["allowed"]:
                problems.append("gate " + step["tool"] + " 期望 allowed=" + str(e["allowed"]) + "，实际 " + str(data.get("allowed")))
        return (not problems), ("；".join(problems) or "gate " + step["tool"] + " -> exit " + str(code)
                                + " allowed=" + str(data.get("allowed")))

    if do == "invoke":
        tool = step["tool"]
        params = step.get("params", {})
        transport = mcp_client.MockTransport(
            fixtures_dir=RESPONSES,
            overrides=dict(ctx["overrides"], **step.get("override", {})))
        gate_res = mcp_client.gate(tool, step.get("state"))
        if not gate_res["allowed"]:
            problems.append("调用被 Gate 拒绝：" + gate_res["reason"])
            ctx["last"] = gate_res["reason"]
            return False, "；".join(problems)
        # INVARIANT: 只有拿到票据的调用才会进账本；被 Gate 拒绝的调用不留痕
        result = mcp_client.invoke(tool, params, gate_res, transport, log)
        ctx["last"] = json.dumps(result, ensure_ascii=False, sort_keys=True)
        ctx["outputs"][step.get("capture", "last")] = ctx["last"]
        payload = result.get("payload", {})
        empty = False
        if isinstance(payload, dict):
            data = payload.get("data", None)
            empty = data is None or (isinstance(data, list) and not data)
        ctx["last_empty"] = empty
        if empty and step.get("set_state_if_empty"):
            ctx["state"] = step["set_state_if_empty"]
        if result.get("status") != 200:
            problems.append("调用返回非 200：" + str(result.get("status")))
        return (not problems), ("；".join(problems) or "invoke " + tool + " -> HTTP "
                                + str(result.get("status")))

    if do == "transition":
        args = ["--state", step["from"], "--event", step["event"], "--json"]
        if step.get("party_type") is not None:
            args += ["--party-type", str(step["party_type"])]
        code, out, err = run_cli("state_machine.py", args)
        data = _json_or_none(out) or {}
        ctx["last"] = out
        ctx["outputs"][step.get("capture", "last")] = out
        nxt = data.get("next_state")
        if nxt:
            ctx["state"] = nxt
            if nxt == "S10":
                ctx["outcome"] = "degraded"
            elif nxt == "S11":
                ctx["outcome"] = "refused"
            elif nxt == "S9":
                ctx["outcome"] = "ok"
        if step.get("expect"):
            e = step["expect"]
            if "next_state" in e and nxt != e["next_state"]:
                problems.append(step["from"] + " + " + step["event"] + " 期望 -> "
                                + str(e["next_state"]) + "，实际 " + str(nxt))
            if "exit" in e and code != e["exit"]:
                problems.append("转移期望 exit=" + str(e["exit"]) + "，实际 " + str(code))
        return (not problems), ("；".join(problems) or str(step["from"]) + " + " + step["event"] + " -> " + str(nxt))

    if do == "script":
        script = step["script"]
        args = [str(a) for a in step.get("args", [])]
        cwd = None
        if step.get("cwd") == "tmp":
            cwd = Path(tempfile.mkdtemp(prefix="mcd-cwd-"))
        code, out, err = run_cli(script, args, cwd=cwd)
        ctx["last"] = out
        ctx["outputs"][step.get("capture", "last")] = out
        ctx["outputs"][step.get("capture", "last") + "#stderr"] = err
        if step.get("outcome_from_json"):
            data = _json_or_none(out) or {}
            if data.get("outcome"):
                ctx["outcome"] = data["outcome"]
        if step.get("expect"):
            e = step["expect"]
            if "exit" in e and code != e["exit"]:
                problems.append(script + " 期望 exit=" + str(e["exit"]) + "，实际 " + str(code)
                                + "；stderr=" + err.strip()[:200])
        return (not problems), ("；".join(problems) or script + " -> exit " + str(code))

    if do == "backoff":
        code, out, err = run_cli("mcp_client.py",
                                 ["--plan-retry", "--attempt", str(step["attempt"]), "--json"])
        data = _json_or_none(out) or {}
        ctx["last"] = out
        ctx["outputs"][step.get("capture", "last")] = out
        if step.get("expect"):
            e = step["expect"]
            if "exit" in e and code != e["exit"]:
                problems.append("退避 attempt=" + str(step["attempt"]) + " 期望 exit=" + str(e["exit"])
                                + "，实际 " + str(code))
            if "should_retry" in e and data.get("should_retry") is not e["should_retry"]:
                problems.append("退避 attempt=" + str(step["attempt"]) + " 期望 should_retry="
                                + str(e["should_retry"]) + "，实际 " + str(data.get("should_retry")))
        return (not problems), ("；".join(problems) or "attempt " + str(step["attempt"])
                                + " -> should_retry=" + str(data.get("should_retry")))

    if do == "grep":
        pattern = step["pattern"]
        rx = re.compile(pattern)
        hits = []
        root = SKILL_ROOT
        scope = step.get("scope")
        targets = []
        if scope:
            for rel in scope:
                base = SKILL_ROOT / rel
                if base.is_file():
                    targets.append(base)
                elif base.is_dir():
                    targets.extend(sorted(base.rglob("*")))
        else:
            targets = sorted(root.rglob("*"))
        for p in targets:
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith(".git/") or "__pycache__" in rel:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            if rx.search(text):
                hits.append(rel)
        ctx["last"] = json.dumps({"pattern": pattern, "hits": hits}, ensure_ascii=False)
        ctx["outputs"][step.get("capture", "last")] = ctx["last"]
        if step.get("expect"):
            want = step["expect"].get("count")
            if want is not None and len(hits) != want:
                problems.append("grep '" + pattern + "' 期望命中 " + str(want)
                                + " 个文件，实际 " + str(len(hits)) + "：" + "、".join(hits[:5]))
        return (not problems), ("；".join(problems) or "grep '" + pattern + "' 命中 "
                                + str(len(hits)) + " 个文件")

    if do == "set":
        for k, v in step.items():
            if k in ("do",):
                continue
            ctx[k] = v
        return True, "set " + json.dumps({k: v for k, v in step.items() if k != "do"}, ensure_ascii=False)

    return False, "未知步骤类型：" + str(do)


# ── 断言 ─────────────────────────────────────────────────────────────
def eval_assertion(spec, ctx):
    kind = spec.get("type")
    if kind == "call_count":
        actual = ctx["log"].count(spec["tool"])
        want = spec["n"]
        return actual == want, (spec["tool"] + " 调用次数期望 " + str(want) + "，实际 " + str(actual)
                                + "（账本 " + json.dumps(ctx["log"].counts(), ensure_ascii=False) + "）")
    if kind == "output_contains" or kind == "output_not_contains":
        on = spec.get("on", "last")
        text = ctx["outputs"].get(on, ctx.get("last", "")) if on != "all" else "\n".join(ctx["outputs"].values())
        needle = spec["text"]
        has = needle in text
        if kind == "output_contains":
            return has, ("输出未包含 '" + needle + "'（on=" + on + "）")
        return (not has), ("输出不应包含 '" + needle + "'，但出现了（on=" + on + "）")
    if kind == "outcome":
        actual = ctx.get("outcome")
        want = spec["value"]
        return actual == want, ("outcome 期望 " + str(want) + "，实际 " + str(actual))
    if kind == "json_path_equals":
        on = spec.get("on", "last")
        raw = ctx["outputs"].get(on, ctx.get("last", ""))
        data = _json_or_none(raw)
        if data is None:
            return False, "输出不是合法 JSON（on=" + on + "）"
        node = data
        for part in spec["path"].split("."):
            if isinstance(node, list) and re.fullmatch(r"\d+", part):
                idx = int(part)
                if idx >= len(node):
                    return False, "JSON 路径 " + spec["path"] + " 越界"
                node = node[idx]
                continue
            if not isinstance(node, dict) or part not in node:
                return False, "JSON 路径 " + spec["path"] + " 不存在"
            node = node[part]
        return node == spec["value"], ("JSON 路径 " + spec["path"] + " 期望 "
                                       + json.dumps(spec["value"], ensure_ascii=False)
                                       + "，实际 " + json.dumps(node, ensure_ascii=False))
    return False, "未知断言类型：" + str(kind)


def run_case(case, verbose=False):
    ctx = {"log": mcp_client.CallLog(), "outputs": {}, "last": "", "state": None,
           "outcome": None, "overrides": case.get("overrides", {}), "last_empty": False}
    failures = []
    for idx, step in enumerate(case.get("steps", []), 1):
        ok, note = exec_step(step, ctx)
        if verbose:
            print("   步骤 " + str(idx) + " " + step.get("do") + "：" + note)
        if not ok:
            failures.append("步骤 " + str(idx) + "（" + str(step.get("do")) + "）：" + note)
    for spec in case.get("assertions", []):
        ok, note = eval_assertion(spec, ctx)
        if not ok:
            failures.append("断言 " + str(spec.get("type")) + "：" + note)
        elif verbose:
            print("   断言 " + str(spec.get("type")) + "：" + note)
    return failures


# ── 内建守卫用例 ─────────────────────────────────────────────────────
def _builtin_checks(collector):
    def add(label, cond, detail=""):
        collector.append((label, bool(cond), detail))

    return add


def case_note_guard():
    """T-note-guard：note 派生值禁止进入四个计算 / 校验入口。"""
    out = []
    add = _builtin_checks(out)
    # 用临时文件而非 fixtures：本用例必须在不读 fixtures 的前提下也能跑通
    tmp_dir = Path(tempfile.mkdtemp(prefix="mcd-note-guard-"))
    params_file = tmp_dir / "params.json"
    params_file.write_text(json.dumps({
        "fields": {k: {"value": "v-" + k, "source": "api"}
                   for k in ("code", "count", "dateStr", "id", "leftNum", "partyType",
                             "skuId", "spuId", "storeCode", "timeEnd", "timeStart")}
    }, ensure_ascii=False), encoding="utf-8")
    probes = [
        ("money.py 拒绝 note_display 来源", "money.py",
         ["--to-fen", "59", "--source", "note_display_price"], 2),
        ("quorum_calc.py 拒绝 note_display 人数", "quorum_calc.py",
         ["--party-min", "5", "--party-max", "12", "--left-num", "12",
          "--confirmed", "note_display_min", "--price-fen", "5900",
          "--date", "2026-10-23", "--now", "2026-10-18 23:07:42"], 2),
        ("order_params.py 拒绝 note_display schema", "order_params.py",
         ["--from", str(params_file), "--schema", "note_display_schema"], 2),
        ("state_machine.py 拒绝死线事件", "state_machine.py",
         ["--state", "S8", "--event", "deadline_passed"], 2),
        ("quorum_calc.py 拒绝 overdue 作为分支入参", "quorum_calc.py",
         ["--party-min", "5", "--party-max", "12", "--left-num", "12", "--confirmed", "3",
          "--price-fen", "5900", "--date", "2026-10-23", "--now", "2026-10-18 23:07:42",
          "--overdue", "true"], 2),
    ]
    for label, script, args, want in probes:
        code, _, err = run_cli(script, args)
        add(label, code == want, "期望 exit=" + str(want) + "，实际 " + str(code) + "：" + err.strip()[:160])
    return out


def parse_state_md(path):
    """解析 references/state-machine.md 的两张表。"""
    if not path.is_file():
        return None, None, "找不到 " + str(path)
    state_tools, transitions = {}, {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        tok1 = re.findall(r"`([^`]+)`", cells[1])
        events = [t for t in tok1 if re.fullmatch(r"[a-z][a-z0-9_]*", t)]
        if events:
            tok0 = re.findall(r"`([^`]+)`", cells[0])
            froms = [t for t in tok0 if re.fullmatch(r"S\d+", t)]
            if "任意" in cells[0]:
                froms = ["ANY"]
            if not froms or not events:
                continue
            tos = [t for t in re.findall(r"`([^`]+)`", cells[2]) if re.fullmatch(r"S\d+", t)]
            if not tos:
                continue
            for f in froms:
                for e in events:
                    transitions.setdefault((f, e), set()).update(tos)
            continue
        m = re.search(r"S\d+", cells[0])
        if m:
            state_tools[m.group(0)] = re.findall(r"`([^`]+)`", cells[2])
    return state_tools, transitions, None


def case_table_sync():
    """T-table-sync：md 表格与脚本常量表比对，漂移即失败。"""
    out = []
    add = _builtin_checks(out)
    state_tools, transitions, err = parse_state_md(STATE_MD)
    if err:
        add("references/state-machine.md 可解析", False, err)
        return out
    expect_tools = {s: list(t) for s, _, _, t in state_machine.STATES}
    add("状态数与脚本一致", set(state_tools) == set(expect_tools),
        "md=" + str(sorted(state_tools)) + " 脚本=" + str(sorted(expect_tools)))
    for sid in sorted(set(state_tools) | set(expect_tools)):
        add("状态 " + sid + " 工具白名单与脚本一致",
            state_tools.get(sid, []) == expect_tools.get(sid, []),
            "md=" + str(state_tools.get(sid)) + " 脚本=" + str(expect_tools.get(sid)))
    expect_tr = {(f, e): set(v) for (f, e), v in state_machine.TRANSITIONS.items()}
    add("转移条目数与脚本一致", set(transitions) == set(expect_tr),
        "md 多=" + str(sorted(set(transitions) - set(expect_tr)))
        + " 脚本多=" + str(sorted(set(expect_tr) - set(transitions))))
    for key in sorted(set(transitions) & set(expect_tr)):
        add("转移 " + key[0] + " + " + key[1] + " 目标一致",
            transitions[key] == expect_tr[key],
            "md=" + str(sorted(transitions[key])) + " 脚本=" + str(sorted(expect_tr[key])))
    add("md 不含死线类事件",
        not any(re.search(r"deadline|overdue", e) for (_, e) in transitions))
    return out


def case_money_unit():
    """T-money-unit：单位查表 + 渲染出口 + 裸值守卫。"""
    out = []
    add = _builtin_checks(out)
    code, o, _ = run_cli("money.py", ["--to-fen", "59", "--source",
                                      "mall-product-detail.skuList[].price", "--json"])
    data = _json_or_none(o) or {}
    add("元字符串来源 -> 5900 分", code == 0 and data.get("fen") == 5900, str(data))
    code, o, _ = run_cli("money.py", ["--to-fen", "5900", "--source",
                                      "query-party-store-session.data[].price", "--json"])
    data = _json_or_none(o) or {}
    add("分整数来源直通 -> 5900 分", code == 0 and data.get("fen") == 5900, str(data))
    code, o, _ = run_cli("money.py", ["--to-fen", "59", "--source", "some.unknown.price"])
    add("未知来源 -> exit 2（禁止猜测单位）", code == 2, "实际 exit=" + str(code))
    code, o, _ = run_cli("money.py", ["--format", "5900"])
    add("渲染出口 5900 -> ¥59", code == 0 and o.strip() == "¥59", repr(o))
    code, o, _ = run_cli("money.py", ["--format", "17700"])
    add("渲染出口 17700 -> ¥177", code == 0 and o.strip() == "¥177", repr(o))
    code, o, _ = run_cli("money.py", ["--check", "人均 ¥59 · 3 人共 ¥177", "--fen", "5900"])
    add("洁净文本通过出口守卫", code == 0, "实际 exit=" + str(code))
    code, o, _ = run_cli("money.py", ["--check", "price=5900", "--fen", "5900"])
    add("裸值 5900 被出口守卫拦下 -> exit 1", code == 1, "实际 exit=" + str(code))
    return out


def case_overdue_still_cards():
    """T-overdue-still-cards：已过 advisory 死线仍正常出卡。"""
    out = []
    add = _builtin_checks(out)
    args = ["--party-min", "5", "--party-max", "12", "--left-num", "12", "--confirmed", "6",
            "--price-fen", "5900", "--date", "2026-10-23", "--now", "2026-10-22 10:00:00", "--json"]
    code, o, err = run_cli("quorum_calc.py", args)
    data = _json_or_none(o) or {}
    add("已过死线仍 exit 0（不拒卡）", code == 0, "实际 exit=" + str(code) + " " + err.strip()[:160])
    add("overdue 标记为 True（纯展示）", data.get("overdue") is True, str(data.get("overdue")))
    add("outcome 仍为 ok", data.get("outcome") == "ok", str(data.get("outcome")))
    card = data.get("card", "")
    add("卡面仍含组局卡", "组局卡" in card)
    add("卡面含「已过官方文案建议的提前预订期」标注",
        "已过官方文案建议的提前预订期" in card)
    add("卡面不含「赶不上」类断言", "赶不上" not in card)
    add("死线恒标 advisory", data.get("deadline_kind") == "advisory")
    return out


def case_advisory_not_blocking():
    """T-advisory-not-blocking：advisory 字段完全不参与计算。

    同一组数值输入，只改 --now（一前一后跨过死线），金额换算与成团判定的
    全部数值必须逐字一致；只有展示字段（overdue / 卡面标注句）允许变化。
    """
    out = []
    add = _builtin_checks(out)
    base = ["--party-min", "5", "--party-max", "12", "--left-num", "12", "--confirmed", "3",
            "--price-fen", "5900", "--date", "2026-10-23"]
    code_a, out_a, _ = run_cli("quorum_calc.py", base + ["--now", "2026-10-18 23:07:42", "--json"])
    code_b, out_b, _ = run_cli("quorum_calc.py", base + ["--now", "2026-10-22 10:00:00", "--json"])
    add("两次运行都 exit 0（超时不阻断）", code_a == 0 and code_b == 0,
        str(code_a) + " / " + str(code_b))
    a, b = _json_or_none(out_a) or {}, _json_or_none(out_b) or {}
    numeric_keys = ["need", "can_start", "cap", "over_cap", "per_person_cny", "total_cny",
                    "party_min", "party_max", "left_num", "confirmed", "deadline_date",
                    "deadline", "deadline_kind", "lead_days", "session_date"]
    for k in numeric_keys:
        add("数值字段 " + k + " 前后逐字一致（advisory 不参与计算）", a.get(k) == b.get(k),
            "未超时=" + json.dumps(a.get(k), ensure_ascii=False)
            + " 已超时=" + json.dumps(b.get(k), ensure_ascii=False))
    add("只有展示字段 overdue 随超时变化", a.get("overdue") is False and b.get("overdue") is True)
    add("已超时侧卡面追加标注句", "已过官方文案建议的提前预订期" in (b.get("card") or ""))
    add("未超时侧卡面不含该标注句", "已过官方文案建议的提前预订期" not in (a.get("card") or ""))
    return out


def case_selftests():
    """T-selftests：9 个脚本的 --selftest 全绿。"""
    out = []
    add = _builtin_checks(out)
    for script in ALL_SCRIPTS:
        if script == "run_scenarios.py":
            continue
        code, o, err = run_cli(script, ["--selftest"])
        add(script + " --selftest -> exit 0", code == 0,
            "实际 exit=" + str(code) + " " + err.strip()[:160])
    return out


def case_cwd_independent():
    """T-cwd-independent：安装后 cwd 变化仍可用（打包必测项）。"""
    out = []
    add = _builtin_checks(out)
    with tempfile.TemporaryDirectory(prefix="mcd-cwd-") as td:
        for script, args in (("money.py", ["--format", "5900"]),
                             ("state_machine.py", ["--tools", "S3"]),
                             ("party_fields.py", ["--people-range", "5-12"])):
            code, o, err = run_cli(script, args, cwd=Path(td))
            add(script + " 在外部 cwd 下可用（不依赖调用时工作目录）", code == 0,
                "实际 exit=" + str(code) + " " + err.strip()[:160])
    return out


def case_write_tools_zero():
    """T-write-tools-zero：7 个写工具在全程账本中恒为 0。"""
    out = []
    add = _builtin_checks(out)
    log = mcp_client.CallLog()
    transport = mcp_client.MockTransport(fixtures_dir=RESPONSES, overrides={})
    # 走一遍完整只读链路
    for tool, state in (("mall-points-products", "S1"), ("mall-product-detail", "S1"),
                        ("query-party-city", "S3"), ("query-party-store", "S4"),
                        ("query-party-store-date", "S5"), ("query-party-store-session", "S6"),
                        ("now-time-info", "S1")):
        g = mcp_client.gate(tool, state)
        add("Gate 放行 " + tool + "（" + state + "）", g["allowed"], str(g.get("reason")))
        if g["allowed"]:
            mcp_client.invoke(tool, {"spuId": 91001}, g, transport, log)
    for tool in mcp_client.WRITE_TOOLS:
        g = mcp_client.gate(tool, "S9")
        add("写工具 " + tool + " 被 Gate 拒绝", not g["allowed"])
        add("写工具 " + tool + " 账本计数为 0", log.count(tool) == 0)
    add("只读链路 7 个工具各调用 1 次",
        all(log.count(t) == 1 for t in mcp_client.READ_ONLY_TOOLS),
        json.dumps(log.counts(), ensure_ascii=False))
    return out


BUILTINS = [
    ("T-note-guard", "note 派生值禁止进入计算 / 校验 / 分支通道（四入口守卫）", case_note_guard),
    ("T-table-sync", "references/state-machine.md 与脚本状态表零漂移", case_table_sync),
    ("T-money-unit", "金额单位查表 + 唯一渲染出口 + 裸值守卫", case_money_unit),
    ("T-overdue-still-cards", "已过 advisory 死线仍正常出组局卡（倒计时恐慌禁令）", case_overdue_still_cards),
    ("T-advisory-not-blocking", "advisory 字段不参与计算：超时前后数值逐字一致", case_advisory_not_blocking),
    ("T-selftests", "8 个业务脚本 --selftest 全绿", case_selftests),
    ("T-cwd-independent", "脚本不依赖调用时工作目录", case_cwd_independent),
    ("T-write-tools-zero", "7 个写工具调用次数恒为 0（B3 正向证据）", case_write_tools_zero),
]


def _selftest():
    """--selftest：跑内建守卫用例，不依赖 fixtures。"""
    _force_utf8()
    total, failed = 0, 0
    for name, desc, fn in BUILTINS:
        if name == "T-selftests":
            continue  # 避免递归调用自身
        rows = fn()
        bad = [r for r in rows if not r[1]]
        total += len(rows)
        failed += len(bad)
        print(("✅" if not bad else "❌") + " " + name + "：" + desc
              + "（" + str(len(rows) - len(bad)) + "/" + str(len(rows)) + "）")
        for label, _, detail in bad:
            print("     • " + label + (" -- " + detail if detail else ""))
    print("\n结果：" + str(total - failed) + "/" + str(total) + " 项通过（--selftest 不含 fixtures 用例）")
    return EXIT_OK if not failed else EXIT_FAIL


def list_cases():
    files = sorted(SCENARIOS.glob("*.json")) if SCENARIOS.is_dir() else []
    return files


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(__doc__)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="run_scenarios.py", description="场景回归执行器")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--case")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.list:
        for f in list_cases():
            print(f.stem.upper() + "  " + str(f))
        for name, desc, _ in BUILTINS:
            print(name + "  " + desc + "（内建）")
        return EXIT_OK

    report = {"cases": [], "total": 0, "failed": 0}
    ran = False

    def record(name, failures, extra=None):
        ran_ = True
        report["cases"].append({"case": name, "passed": not failures, "failures": failures,
                                "extra": extra or []})
        report["total"] += 1
        if failures:
            report["failed"] += 1
        if not args.json:
            print(("✅ " if not failures else "❌ ") + name + "："
                  + ("通过" if not failures else str(len(failures)) + " 项失败"))
            for f in failures:
                print("     • " + f)
            for label, ok, detail in (extra or []):
                if not ok:
                    print("     • " + label + (" -- " + detail if detail else ""))

    files = list_cases()
    if args.case:
        target = args.case.lower()
        files = [f for f in files if f.stem == target]
        builtins = [b for b in BUILTINS if b[0].lower() == target]
        if not files and not builtins:
            sys.stderr.write("❌ 找不到用例 " + args.case + "\n")
            return EXIT_USAGE
    else:
        builtins = BUILTINS

    if not args.all and not args.case:
        print(__doc__)
        return EXIT_USAGE

    for f in files:
        try:
            case = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            record(f.stem.upper(), ["场景文件不是合法 JSON：" + str(exc)])
            continue
        case.setdefault("steps", [])
        failures = run_case(case, verbose=args.verbose)
        record(case.get("id", f.stem.upper()), failures)

    for name, desc, fn in builtins:
        rows = fn()
        bad = [(l, ok, d) for l, ok, d in rows if not ok]
        record(name, ["守卫失败"] if bad else [], rows)
        if not args.json and not bad:
            print("     （" + str(len(rows) - len(bad)) + "/" + str(len(rows)) + " 项子断言通过）")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print("\n汇总：" + str(report["total"] - report["failed"]) + "/" + str(report["total"])
              + " 个用例通过，失败 " + str(report["failed"]) + " 个")
    return EXIT_OK if report["failed"] == 0 else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
