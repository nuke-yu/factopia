"""运行中的实时状态：边读 RD-Agent 控制台输出边解析（后台运行器每 3 秒写一次 jobs/<任务>.live.json）。

RD-Agent 的结构化日志要到一个步骤结束时才记录开始时间，代码编写与回测期间没有任何进度；控制台输出则是实时的：
    "Start Loop 2, Step 1: coding"                         步骤开始
    "│ Entry │ ... qrun conf_baseline.yaml"、"Running time"  回测容器开始与结束
    "Execution succeeded without error."                   因子代码执行成功
    "Using chat model"                                     一次大模型调用
    "...excess return with cost(1day)." + 风险指标表          Qlib 回测结果
以及大模型接口的致命错误（余额不足、密钥无效），用于暂停排队中的任务。
"""
import re
from collections import deque

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.\d+ \|")  # 进度条输出与日志常在同一行，不限行首
START = re.compile(r"Start Loop (\d+), Step (\d+): (\w+)")
SCRIPT = re.compile(r"\b(conf_[\w]+\.yaml|generate\.py|read_exp_res\.py|runtime_info\.py)")
METRIC = re.compile(r"^(annualized_return|information_ratio|max_drawdown)\s+(-?[\d.]+(?:e-?\d+)?)\s*$")
STEP_ZH = {"direct_exp_gen": "提出假设", "coding": "编写代码", "running": "Qlib 回测", "feedback": "评审结果", "record": "记录"}
SCRIPT_ZH = {"generate.py": "生成 RD-Agent 数据", "conf_baseline.yaml": "起点回测（20 个基础因子）",
             "read_exp_res.py": "读取回测结果", "runtime_info.py": "检查运行环境"}
FATAL = [  # (日志中的特征, 给研究员看的原因)：出现后排队中的任务暂停，避免连续失败
    ("Insufficient Balance", "大模型账户余额不足（DeepSeek）：请充值后点击\"继续运行\""),
    ("AuthenticationError", "大模型 API Key 无效或已过期：请检查 .env 后点击\"继续运行\""),
    ("Incorrect API key", "大模型 API Key 无效或已过期：请检查 .env 后点击\"继续运行\""),
]


def _script_zh(name: str) -> str:
    if name.startswith("conf_combined"):
        return "Qlib 训练模型并回测（基础因子 + 新因子）"
    return SCRIPT_ZH.get(name, name)


class LiveParser:
    """逐行喂入控制台输出，维护当前状态。"""

    def __init__(self) -> None:
        self.phase = "prepare"            # prepare 准备 → loop 挖掘轮次（起点回测在第 0 轮的回测步骤中执行）
        self.loop: int | None = None
        self.step: str | None = None
        self.step_started_at: str | None = None
        self.counters = {"llm": 0, "exec_ok": 0, "docker": 0}
        self.activity: deque = deque(maxlen=40)
        self.backtests: list[dict] = []
        self.docker: dict | None = None
        self.fatal: str | None = None
        self._last_time: str | None = None
        self._entry_lines = 0             # "│ Entry" 之后继续读两行找脚本名
        self._entry_text = ""
        self._tail = False                # 正在读回测容器输出的最后几行
        self._block: dict | None = None
        self._exec_in_step = 0
        self._last_script: str | None = None

    def _add(self, kind: str, text: str) -> None:
        self.activity.append({"t": self._last_time, "kind": kind, "text": text})

    def _flush_exec(self) -> None:
        if self._exec_in_step:
            self._add("exec", f"因子代码执行成功 {self._exec_in_step} 次")
            self._exec_in_step = 0

    def feed(self, raw: str) -> None:
        line = ANSI.sub("", raw).rstrip("\n")
        m = None
        for m in STAMP.finditer(line):  # 取本行最后一个时间戳
            pass
        if m:
            self._last_time = f"{m.group(1)}T{m.group(2)}+00:00"
            self._tail = False
            self._close_block()
        if (s := START.search(line)) and m:
            self._flush_exec()
            self.phase, self.loop, self.step = "loop", int(s.group(1)), s.group(3)
            self.step_started_at = self._last_time
            self._add("step", f"第 {self.loop} 轮 · {STEP_ZH.get(self.step, self.step)}")
            return
        if "│ Entry" in line:
            self._entry_lines, self._entry_text = 3, ""
        if self._entry_lines:
            self._entry_text += line
            self._entry_lines -= 1
            if (sm := SCRIPT.search(self._entry_text)):
                name = sm.group(1)
                self._entry_lines = 0
                self._last_script = name
                self.docker = {"name": name, "text": _script_zh(name), "started_at": self._last_time}
                self.counters["docker"] += 1
                self._add("docker", f"开始：{_script_zh(name)}")
            return
        if "Running time:" in line and self.docker:
            secs = re.search(r"Running time: ([\d.]+)", line)
            self._add("docker", f"完成：{self.docker['text']}（{float(secs.group(1)):.0f} 秒）" if secs else f"完成：{self.docker['text']}")
            self.docker = None
            return
        if line.startswith("[Showing last"):
            self._tail = True
            return
        if self._tail:
            low = line.lower()
            if "excess return without cost" in low:
                self._start_block("without_cost")
            elif "excess return with cost" in low:
                self._start_block("with_cost")
            elif self._block and (mm := METRIC.match(line.strip())):
                self._block["values"][mm.group(1)] = float(mm.group(2))
            return
        if "Using chat model" in line:
            self.counters["llm"] += 1
        elif "Execution succeeded without error" in line:
            self.counters["exec_ok"] += 1
            self._exec_in_step += 1
        for mark, reason in FATAL:
            if mark in line and not self.fatal:
                self.fatal = reason
                self._add("error", reason)

    def _start_block(self, kind: str) -> None:
        self._close_block()
        self._block = {"kind": kind, "values": {}}

    def _close_block(self) -> None:
        b, self._block = self._block, None
        if not b or not b["values"]:
            return
        key = None if self._last_script == "conf_baseline.yaml" else self.loop  # 起点回测
        if not self.backtests or self.backtests[-1]["loop"] != key or b["kind"] in self.backtests[-1]:
            self.backtests.append({"loop": key, "t": self._last_time})
        self.backtests[-1][b["kind"]] = b["values"]
        if b["kind"] == "with_cost":
            v = b["values"]
            who = "起点" if key is None else f"第 {key} 轮"
            self._add("result", f"{who}回测：扣成本年化 {v.get('annualized_return', 0) * 100:.1f}%，"
                                f"最大回撤 {v.get('max_drawdown', 0) * 100:.1f}%")

    def state(self) -> dict:
        return {"phase": self.phase, "loop": self.loop, "step": self.step, "step_zh": STEP_ZH.get(self.step or "", None),
                "step_started_at": self.step_started_at, "docker": self.docker, "counters": self.counters,
                "activity": list(self.activity), "backtests": self.backtests, "fatal": self.fatal,
                "last_log_at": self._last_time}
