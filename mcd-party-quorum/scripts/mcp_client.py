#!/usr/bin/env python3
"""mcp_client.py -- MCP 调用层：transport 抽象 / 只读白名单 / 写工具黑名单 / 账本 / 429 策略。

设计要点（设计蓝图 5.1 与 §7）：

  1. Gate 是唯一调用入口：任何 MCP 调用必须先 --gate 取票据，无票据 invoke 被拒；
     写工具无条件拒绝（exit 3）；状态白名单外的工具拒绝（exit 3）。
  2. 账本是权威事实：每次 invoke 记账，assert_count(tool, 0) 是真的 0 —— 因为
     C 类用例全部在 MockTransport 下由 run_scenarios.py 在进程内执行。
  3. 脚本不发起真实 MCP 调用：无 Token、无 HTTP 客户端。429 的「策略」由本脚本
     确定性产出（next_delay / should_retry / attempt 封顶），「重试动作」由 LLM
     按脚本输出的下一步动作执行（设计蓝图 §8 风险 2 的拆分裁决）。

退出码：0 正常 / 1 断言失败 / 2 入参错误 / 3 写操作被拦截 / 4 不可达或退避耗尽。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_WRITE_BLOCKED = 3
EXIT_UNREACHABLE = 4

SKILL_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = SKILL_ROOT / "tests" / "fixtures"
RESPONSES_DIR = FIXTURES / "responses"
# 文件账本（仅 CLI 跨进程场景）：落在系统临时目录，不写安装目录 ——
# 安装目录只读也不能炸，打包产物也不含任何运行时数据（QA rubric C4）。
# 账本只记录工具名 / 状态码 / 成败，不含任何用户数据与凭据。
LEDGER_PATH = Path(tempfile.gettempdir()) / "mcd-party-quorum-call-ledger.json"

# 只读工具白名单（本技能启用的全部工具）
READ_ONLY_TOOLS = [
    "mall-points-products",
    "mall-product-detail",
    "query-party-city",
    "query-party-store",
    "query-party-store-date",
    "query-party-store-session",
    "now-time-info",
]

# 写工具黑名单：调用次数必须恒为 0（B3）
WRITE_TOOLS = [
    "party-order-create",
    "create-order",
    "mall-create-order",
    "draw-lottery",
    "cancel-order",
    "delivery-create-address",
    "auto-bind-coupons",
]

MAX_ATTEMPTS = 3          # 429 退避上限（超过即明确失败，不静默成功）
BACKOFF_CAP_SECONDS = 30

NOTE_DISPLAY_RE = re.compile(r"(?i)note[_-]?display")


class UsageError(Exception):
    """入参错误（退出码 2）。"""


class WriteBlocked(Exception):
    """写操作被拦截（退出码 3）。"""


class FixtureMiss(Exception):
    """mock 未命中（退出码 2，绝不返回空数组假装空态）。"""


def guard_note_derived(argv):
    for token in argv:
        if NOTE_DISPLAY_RE.search(token):
            raise UsageError(
                "note 派生值禁止进入调用参数：入参出现 note_display_*（收到 " + token + "）。"
            )


def classify(tool):
    if tool in WRITE_TOOLS:
        return "write"
    if tool in READ_ONLY_TOOLS:
        return "read"
    return "unknown"


def next_delay(attempt):
    """指数退避：attempt 1/2/3 -> 2/4/8 秒，封顶 30 秒。纯函数，可枚举。"""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        raise UsageError("attempt 必须是正整数，收到 '" + str(attempt) + "'")
    if n < 1:
        raise UsageError("attempt 必须 >= 1，收到 " + str(n))
    return min(2 ** n, BACKOFF_CAP_SECONDS)


def should_retry(attempt):
    """attempt 未超过 MAX_ATTEMPTS（3 次）才允许再发起一次；第 4 次起明确失败。"""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        raise UsageError("attempt 必须是正整数，收到 '" + str(attempt) + "'")
    return n <= MAX_ATTEMPTS


def retry_plan(attempt, status=429):
    """产出确定性的下一步动作。动作由 LLM 执行，脚本只给策略。"""
    # INVARIANT: attempt 超过 MAX_ATTEMPTS 后不再产出任何重试动作，只输出明确失败
    n = int(attempt)
    if should_retry(n):
        return {
            "outcome": "rate_limited",
            "attempt": n,
            "status": status,
            "should_retry": True,
            "delay_seconds": next_delay(n),
            "next_action": "retry_after_delay",
            "message": "请求过于频繁（429）。请先等待 "
                       + str(next_delay(n)) + " 秒，再重新发起同一次调用（第 "
                       + str(n + 1) + " 次尝试）。",
            "attempts_left": MAX_ATTEMPTS - n,
        }
    return {
        "outcome": "rate_limited",
        "attempt": n,
        "status": status,
        "should_retry": False,
        "delay_seconds": 0,
        "next_action": "stop_and_report",
        "message": "请求过于频繁（429），已连续 " + str(n) + " 次未成功，达到退避上限。"
                   "本技能到此为止，不再自动发起请求；请稍后自行再试一次。",
        "attempts_left": 0,
    }


def make_ticket(tool, state):
    digest = hashlib.sha1(("|".join([tool, str(state)])).encode("utf-8")).hexdigest()
    return "T-" + digest[:8]


# ── 账本 ─────────────────────────────────────────────────────────────
class CallLog:
    """权威账本：进程内全部调用的唯一事实来源。"""

    def __init__(self):
        self._counts = {}
        self._records = []

    def record(self, tool, status, ok):
        # INVARIANT: 账本只记录真实发生的调用；被 Gate 拒绝的调用不入账，
        # 因此 assert_count(tool, 0) 是结构上的真 0
        self._counts[tool] = self._counts.get(tool, 0) + 1
        self._records.append({"tool": tool, "status": status, "ok": ok})

    def counts(self):
        return dict(sorted(self._counts.items()))

    def count(self, tool):
        return self._counts.get(tool, 0)

    def assert_count(self, tool, expected):
        actual = self.count(tool)
        if actual != expected:
            raise AssertionError(
                "账本断言失败：" + tool + " 期望调用 " + str(expected) + " 次，实际 " + str(actual) + " 次。"
                + "当前账本 " + json.dumps(self.counts(), ensure_ascii=False)
            )
        return True

    def records(self):
        return list(self._records)


# ── Transport ────────────────────────────────────────────────────────
class MockTransport:
    """从 fixtures 读正态响应；overrides 可覆盖单个工具或注入 429。"""

    def __init__(self, fixtures_dir=None, overrides=None):
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else RESPONSES_DIR
        self.overrides = overrides or {}

    def invoke(self, tool, params=None):
        if tool in self.overrides:
            payload = self.overrides[tool]
        else:
            path = self.fixtures_dir / (tool + ".json")
            if not path.is_file():
                raise FixtureMiss(
                    "mock 未命中：找不到 " + str(path)
                    + "。未命中的工具一律显式报错，绝不返回空数组假装空态。"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        status = 200
        if isinstance(payload, dict) and isinstance(payload.get("status"), int) \
                and payload.get("status") != 200:
            status = payload["status"]
        return {"tool": tool, "status": status, "ok": status == 200,
                "params": params or {}, "payload": payload}


def gate(tool, state=None):
    # INVARIANT: 写工具在 Gate 层无条件拒绝，与状态无关
    """申请调用票据。写工具 / 白名单外工具 / 状态不允许 -> 拒绝。"""
    kind = classify(tool)
    if kind == "write":
        return {"allowed": False, "ticket": None, "state": state, "tool": tool,
                "reason": "写操作被拦截：本技能全程只读，" + tool + " 属于 7 个写工具黑名单之一，"
                          "调用次数必须恒为 0。", "block": "write_blocked"}
    if kind == "unknown":
        return {"allowed": False, "ticket": None, "state": state, "tool": tool,
                "reason": "工具 '" + tool + "' 不在本技能只读白名单内（" + "、".join(READ_ONLY_TOOLS) + "）。",
                "block": "unknown_tool"}
    if state:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import state_machine
        try:
            tools = state_machine.allowed_tools(state)
        except state_machine.UsageError as exc:
            return {"allowed": False, "ticket": None, "state": state, "tool": tool,
                    "reason": str(exc), "block": "unknown_state"}
        if tool not in tools:
            return {"allowed": False, "ticket": None, "state": state, "tool": tool,
                    "reason": "防跳步：状态 " + state + " 只允许调用 "
                              + ("、".join(tools) if tools else "无（空集）")
                              + "，不允许 " + tool + "。", "block": "state_not_allowed"}
    return {"allowed": True, "ticket": make_ticket(tool, state), "state": state, "tool": tool,
            "reason": "票据已发放；调用前必须先取票据，无票据不得调用。"}


def invoke(tool, params, ticket, transport, log, state=None):
    # INVARIANT: 写工具调用次数恒为 0；无有效票据的调用一律拒绝
    """带票据的调用。无票据 / 假票据 / 写工具 -> 拒绝；调用一律记账。

    state 为调用方显式声明的状态：字符串票据按 make_ticket(tool, state) 验签，
    杜绝「拿 A 状态的票据在 B 状态用」与「CLI 直通 transport」两类绕过。
    """
    if classify(tool) == "write":
        raise WriteBlocked("写操作被拦截：" + tool + " 属于写工具黑名单，调用次数必须恒为 0。")
    if isinstance(ticket, dict):
        if not ticket.get("allowed") or ticket.get("tool") != tool:
            raise UsageError("票据无效或被拒，禁止调用 " + tool + "。")
        effective_state = ticket.get("state")
    else:
        effective_state = state
        if ticket != make_ticket(tool, state):
            raise UsageError("缺少有效票据：调用 " + tool + " 前必须先 --gate 取票据。")
    result = transport.invoke(tool, params)
    log.record(tool, result["status"], result["ok"])
    result["ticket"] = make_ticket(tool, effective_state)
    return result


# ── 文件账本（CLI 跨进程用）────────────────────────────────────────────
def load_ledger():
    if not LEDGER_PATH.is_file():
        return {}
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_ledger(data):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                           encoding="utf-8")


def ledger_bump(tool):
    data = load_ledger()
    data[tool] = data.get(tool, 0) + 1
    save_ledger(data)
    return data


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

    # 白名单 / 黑名单
    check("7 个只读工具全部在白名单", all(classify(t) == "read" for t in READ_ONLY_TOOLS))
    check("7 个写工具全部在黑名单", all(classify(t) == "write" for t in WRITE_TOOLS))
    check("未知工具被归类为 unknown", classify("some-other-tool") == "unknown")

    # Gate
    g = gate("query-party-city", "S3")
    check("S3 放行 query-party-city", g["allowed"] and g["ticket"].startswith("T-"))
    g = gate("query-party-store-session", "S3")
    check("C5：S3 拒绝 query-party-store-session（防跳步）", not g["allowed"] and g["block"] == "state_not_allowed")
    g = gate("query-party-store", "S10")
    check("C4：S10 DEGRADED 拒绝一切调用（空集白名单）", not g["allowed"])
    g = gate("party-order-create", "S9")
    check("B3：写工具无条件拒绝", not g["allowed"] and g["block"] == "write_blocked")
    g = gate("unknown-tool", "S1")
    check("白名单外工具 -> 拒绝", not g["allowed"] and g["block"] == "unknown_tool")

    # 内存 transport + 账本（不读 fixtures）
    log = CallLog()
    tp = MockTransport(fixtures_dir=None, overrides={
        "query-party-city": {"data": [{"code": "330100", "name": "杭州"}]}})
    res = invoke("query-party-city", {"spuId": 91001}, gate("query-party-city", "S3"), tp, log)
    check("带票据调用成功", res["ok"] and log.count("query-party-city") == 1)
    check("账本断言：未调用的 store 计数为 0", log.assert_count("query-party-store", 0))
    check("账本断言：写工具计数恒为 0", log.assert_count("party-order-create", 0))
    try:
        invoke("query-party-store", {}, None, tp, log)
        check("无票据调用被拒", False, "未报错")
    except UsageError:
        check("无票据调用被拒（Gate 是唯一入口）", True)
    check("被拒调用不进账本", log.count("query-party-store") == 0)
    try:
        invoke("party-order-create", {}, gate("party-order-create", "S9"), tp, log)
        check("带票据也无法绕过写工具黑名单", False, "未报错")
    except WriteBlocked:
        check("带票据也无法绕过写工具黑名单（深度防御）", True)
    check("写工具仍未进账本", log.count("party-order-create") == 0)

    # mock 未命中 -> 显式报错
    try:
        MockTransport(fixtures_dir=SKILL_ROOT / "__no_such_dir__").invoke("now-time-info", {})
        check("mock 未命中 -> 显式报错（不返回空数组）", False, "未报错")
    except FixtureMiss:
        check("mock 未命中 -> 显式报错（不返回空数组）", True)

    # 429 策略
    check("退避序列 attempt 1/2/3 -> 2/4/8 秒",
          [next_delay(i) for i in (1, 2, 3)] == [2, 4, 8])
    check("attempt 1、2、3 允许重试", should_retry(1) and should_retry(2) and should_retry(3))
    check("attempt 4 起不再重试（连续 3 次后明确失败）", not should_retry(4))
    p = retry_plan(1)
    check("attempt 1 产出重试动作与等待秒数",
          p["should_retry"] and p["next_action"] == "retry_after_delay" and p["delay_seconds"] == 2)
    p = retry_plan(3)
    check("attempt 3 仍在退避上限内，产出 8 秒等待",
          p["should_retry"] and p["delay_seconds"] == 8)
    p = retry_plan(4)
    check("attempt 4 明确失败且不再给出重试指令",
          (not p["should_retry"]) and p["next_action"] == "stop_and_report" and "不再自动发起" in p["message"])

    # CLI 退出码
    code, _ = _run(["--gate", "party-order-create"])
    check("CLI 写工具 --gate -> exit 3", code == EXIT_WRITE_BLOCKED, "实际 exit=" + str(code))
    code, _ = _run(["--gate", "no-such-tool"])
    check("CLI 白名单外工具 --gate -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--gate", "query-party-city", "--state", "S3"])
    check("CLI 合法 --gate -> exit 0", code == EXIT_OK, "实际 exit=" + str(code))
    code, _ = _run(["--invoke", "query-party-city", "--params", "{}"])
    check("CLI 无票据 invoke -> exit 2", code == EXIT_USAGE, "实际 exit=" + str(code))
    code, _ = _run(["--plan-retry", "--attempt", "4"])
    check("CLI 退避耗尽 -> exit 4", code == EXIT_UNREACHABLE, "实际 exit=" + str(code))

    # 幂等
    a = gate("query-party-city", "S3")
    b = gate("query-party-city", "S3")
    check("幂等：同一 gate 两次结果一致", a == b)

    failed = [r for r in results if not r[1]]
    print("\n结果：" + str(len(results) - len(failed)) + "/" + str(len(results)) + " 项通过")
    return EXIT_OK if not failed else EXIT_FAIL


USAGE = """mcp_client.py -- MCP 调用层

  --gate TOOL [--state S]              申请票据（写工具 / 状态不允许 -> exit 3）
  --invoke TOOL --params F --ticket T [--mock DIR] [--attempt N]
  --ledger [--ledger-file F]           打印 tool -> count
  --assert-count TOOL=N                断言调用次数，不符 exit 1
  --reset                              清空账本
  --plan-retry --attempt N [--status 429]   产出 429 退避策略与下一步动作
  --selftest                           内置自检（不联网、不读 fixtures）

脚本不持有任何 Token，也不发起真实 HTTP 调用。
"""


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _force_utf8()
    if not argv:
        print(USAGE)
        return EXIT_USAGE
    ap = argparse.ArgumentParser(prog="mcp_client.py", description="MCP 调用层与只读护栏")
    ap.add_argument("--gate")
    ap.add_argument("--invoke")
    ap.add_argument("--state")
    ap.add_argument("--params")
    ap.add_argument("--ticket")
    ap.add_argument("--mock")
    ap.add_argument("--attempt", default="1")
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--ledger-file")
    ap.add_argument("--assert-count")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--plan-retry", action="store_true")
    ap.add_argument("--status", default="429")
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

    # -- 账本（跨进程落盘）--
    if args.reset:
        if LEDGER_PATH.is_file():
            LEDGER_PATH.unlink()
        print("✅ 账本已清空")
        return EXIT_OK
    if args.ledger:
        path = Path(args.ledger_file) if args.ledger_file else LEDGER_PATH
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    if args.assert_count:
        if "=" not in args.assert_count:
            sys.stderr.write("❌ --assert-count 形如 TOOL=N\n")
            return EXIT_USAGE
        tool, n = args.assert_count.rsplit("=", 1)
        data = load_ledger()
        actual = data.get(tool, 0)
        if actual != int(n):
            sys.stderr.write("❌ 账本断言失败：" + tool + " 期望 " + n + " 次，实际 " + str(actual) + " 次\n")
            return EXIT_FAIL
        print("✅ 账本断言通过：" + tool + " = " + str(actual))
        return EXIT_OK

    # -- 429 退避策略 --
    if args.plan_retry:
        try:
            plan = retry_plan(args.attempt, int(args.status))
        except UsageError as exc:
            sys.stderr.write("❌ " + str(exc) + "\n")
            return EXIT_USAGE
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        else:
            print(plan["message"])
            print("下一步动作：" + plan["next_action"])
        return EXIT_OK if plan["should_retry"] else EXIT_UNREACHABLE

    # -- Gate --
    if args.gate:
        res = gate(args.gate, args.state)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, sort_keys=True))
        else:
            print(("✅ 允许调用 " + res["tool"] + "（票据 " + str(res["ticket"]) + "）")
                  if res["allowed"] else "⛔ 拒绝调用 " + res["tool"] + "：" + res["reason"])
        if res["allowed"]:
            return EXIT_OK
        return EXIT_WRITE_BLOCKED if res["block"] in ("write_blocked", "state_not_allowed") else EXIT_USAGE

    # -- Invoke --
    if args.invoke:
        if not args.ticket:
            sys.stderr.write("❌ 缺少 --ticket：调用前必须先 --gate 取票据，无票据不得调用。\n")
            return EXIT_USAGE
        if classify(args.invoke) == "write":
            sys.stderr.write("❌ 写操作被拦截：" + args.invoke + " 属于写工具黑名单，调用次数必须恒为 0。\n")
            return EXIT_WRITE_BLOCKED
        params = {}
        if args.params:
            try:
                params = json.loads(Path(args.params).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                sys.stderr.write("❌ --params 文件不可读或不是合法 JSON：" + str(exc) + "\n")
                return EXIT_USAGE
        transport = MockTransport(fixtures_dir=args.mock) if args.mock else MockTransport()
        # INVARIANT: CLI 与库路径共用 invoke() —— 假票据 / 错状态票据一律被拒，
        # 杜绝「CLI 只查票据存在性、直通 transport」的绕过口子（QA P3）。
        # 票据是 (tool, state) 的确定性函数，验签相等即隐含「该状态允许该工具」。
        log = CallLog()
        try:
            result = invoke(args.invoke, params, args.ticket, transport, log, state=args.state)
        except WriteBlocked as exc:
            sys.stderr.write("❌ " + str(exc) + "\n")
            return EXIT_WRITE_BLOCKED
        except (UsageError, FixtureMiss) as exc:
            sys.stderr.write("❌ " + str(exc) + "\n")
            return EXIT_USAGE
        ledger_bump(args.invoke)
        if result["status"] != 200:
            plan = retry_plan(args.attempt, result["status"])
            if args.json:
                print(json.dumps({"result": result, "retry_plan": plan}, ensure_ascii=False, sort_keys=True))
            else:
                print(plan["message"])
            return EXIT_UNREACHABLE
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print("✅ " + args.invoke + " 调用成功（HTTP " + str(result["status"]) + "）")
        return EXIT_OK

    print(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
