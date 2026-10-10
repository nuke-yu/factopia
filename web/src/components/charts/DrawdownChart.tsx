import { useMemo } from "react";
import EChart from "../EChart";
import type { Columns, Round } from "../../api";
import { cssVar, useThemeVersion } from "../../lib";

/** 各轮扣成本后超额收益的回撤（与 Qlib 最大回撤指标同一算法），选中轮次用面积突出。 */
export default function DrawdownChart({ backtest, rounds, selected }: { backtest: Record<string, Columns>; rounds: Round[]; selected: string }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text"), bad = cssVar("--bad");
    const keys = Object.keys(backtest).filter((k) => k !== "baseline").sort((a, b) => Number(a) - Number(b));
    const first = backtest[keys[0]];
    if (!first) return {};
    const decision = (k: string) => rounds.find((r) => String(r.loop) === k)?.decision;
    return {
      animationDuration: 1600,
      tooltip: { trigger: "axis", backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        valueFormatter: (v: number) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`) },
      grid: { left: 48, right: 16, top: 10, bottom: 28 },
      xAxis: { type: "category", data: first.date, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", max: 0, axisLabel: { color: muted, formatter: (v: number) => `${Math.round(v * 100)}%` },
        splitLine: { lineStyle: { color: line, opacity: 0.4 } } },
      series: keys.map((k) => {
        const on = k === selected;
        const color = on ? bad : decision(k) ? cssVar("--good") : cssVar("--faint");
        return {
          name: `第 ${k} 轮${decision(k) ? "（采纳）" : ""}`, type: "line", showSymbol: false, data: backtest[k].drawdown_with_cost,
          lineStyle: { width: on ? 1.8 : 1, color, opacity: on ? 1 : 0.6 }, itemStyle: { color },
          areaStyle: on ? { color: bad, opacity: 0.18 } : undefined, z: on ? 5 : 2,
        };
      }),
    };
  }, [backtest, rounds, selected, theme]);
  return <EChart option={option} notMerge className="h-[170px]" />;
}
