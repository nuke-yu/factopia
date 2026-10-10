import { useMemo } from "react";
import EChart from "../EChart";
import type { Quantiles } from "../../api";
import { cssVar, useThemeVersion } from "../../lib";

/** 第 1 组（打分最低）到第 5 组（打分最高）的颜色：低分偏红，高分偏绿。 */
const groupColors = () => [cssVar("--bad"), cssVar("--warn"), cssVar("--muted"), cssVar("--accent"), cssVar("--good")];
const groupName = (g: number, n: number) => `第 ${g} 组${g === 1 ? "（打分最低）" : g === n ? "（打分最高）" : ""}`;
const pctLabel = (v: number | null | undefined, nd = 2) => (v == null ? "—" : `${(v * 100).toFixed(nd)}%`);

function axisStyle() {
  const muted = cssVar("--muted"), line = cssVar("--line-2");
  return {
    tooltip: { backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: cssVar("--text") } },
    axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } }, splitLine: { lineStyle: { color: line, opacity: 0.4 } },
  };
}

/** 各组平均未来收益（柱）与全部股票平均（虚线）。 */
export function QuantileBars({ q }: { q: Quantiles }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const s = axisStyle(), colors = groupColors();
    return {
      animationDuration: 1200,
      tooltip: { ...s.tooltip, trigger: "axis", valueFormatter: (v: number) => pctLabel(v) },
      grid: { left: 52, right: 88, top: 24, bottom: 40 },
      xAxis: { type: "category", data: q.mean.map((_, i) => `第 ${i + 1} 组`), axisLabel: s.axisLabel, axisLine: s.axisLine },
      yAxis: { type: "value", axisLabel: { ...s.axisLabel, formatter: (v: number) => `${(v * 100).toFixed(1)}%` }, splitLine: s.splitLine },
      series: [{
        name: `平均未来 ${q.horizon} 日收益`, type: "bar", barWidth: "52%",
        data: q.mean.map((v, i) => ({ value: v, itemStyle: { color: colors[i], borderRadius: 2 } })),
        label: { show: true, position: "top", color: cssVar("--text"), fontSize: 11, formatter: (p: { value: number }) => pctLabel(p.value) },
        markLine: {
          symbol: "none", silent: true, data: [{ yAxis: q.universe_mean }],
          lineStyle: { color: cssVar("--muted"), type: "dashed" },
          label: { formatter: "全部股票平均", color: cssVar("--muted"), position: "end" },
        },
      }],
    };
  }, [q, theme]);
  return <EChart option={option} notMerge className="h-[300px]" />;
}

/** 各组累计收益：每隔预测周期调仓一次。理想情况下 5 条线像扇子一样展开、第 5 组在最上。 */
export function QuantileNav({ q }: { q: Quantiles }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const s = axisStyle(), colors = groupColors(), muted = cssVar("--muted");
    return {
      animationDuration: 1600,
      tooltip: { ...s.tooltip, trigger: "axis", valueFormatter: (v: number) => v?.toFixed(3) },
      legend: { top: 0, textStyle: { color: muted }, itemWidth: 14, itemHeight: 2 },
      grid: { left: 48, right: 16, top: 34, bottom: 28 },
      xAxis: { type: "category", data: q.dates, axisLabel: s.axisLabel, axisLine: s.axisLine },
      yAxis: { type: "value", scale: true, axisLabel: s.axisLabel, splitLine: s.splitLine },
      series: [
        ...q.nav.map((data, i) => ({
          name: groupName(i + 1, q.groups), type: "line", data, symbolSize: 4,
          lineStyle: { width: i === 0 || i === q.groups - 1 ? 2.2 : 1.2, color: colors[i] }, itemStyle: { color: colors[i] },
        })),
        { name: "全部股票平均", type: "line", data: q.nav_universe, showSymbol: false,
          lineStyle: { width: 1.2, type: "dashed", color: muted }, itemStyle: { color: muted } },
      ],
    };
  }, [q, theme]);
  return <EChart option={option} notMerge className="h-[300px]" />;
}
