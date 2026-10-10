import { useMemo } from "react";
import EChart from "../EChart";
import type { Columns, Round } from "../../api";
import { cssVar, useThemeVersion } from "../../lib";

/** 各轮扣成本后的累计超额净值与基准指数净值（测试期）。 */
export default function NavChart({ backtest, rounds, selected, className = "h-[400px]" }: {
  backtest: Record<string, Columns>; rounds: Round[]; selected: string; className?: string;
}) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text");
    const keys = Object.keys(backtest).filter((k) => k !== "baseline").sort((a, b) => Number(a) - Number(b));
    const first = backtest[keys[0]];
    if (!first) return {};
    const decision = (k: string) => rounds.find((r) => String(r.loop) === k)?.decision;
    const colorOf = (k: string) => (k === selected ? cssVar("--accent") : decision(k) ? cssVar("--good") : cssVar("--faint"));
    return {
      animationDuration: 1600,
      tooltip: { trigger: "axis", backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        valueFormatter: (v: number) => v?.toFixed(3) },
      legend: { top: 0, textStyle: { color: muted }, itemWidth: 14, itemHeight: 2 },
      grid: { left: 48, right: 16, top: 34, bottom: 28 },
      xAxis: { type: "category", data: first.date, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: line, opacity: 0.4 } } },
      series: [
        ...keys.map((k) => ({
          name: `第 ${k} 轮${decision(k) ? "（采纳）" : ""}`, type: "line", showSymbol: false, data: backtest[k].nav_with_cost,
          lineStyle: { width: k === selected ? 2.5 : 1.2, color: colorOf(k) }, itemStyle: { color: colorOf(k) },
          z: k === selected ? 5 : 2,
        })),
        { name: "基准指数", type: "line", showSymbol: false, data: first.nav_bench, lineStyle: { width: 1.2, type: "dashed", color: muted }, itemStyle: { color: muted } },
      ],
    };
  }, [backtest, rounds, selected, theme]);
  return <EChart option={option} notMerge className={className} />;
}
