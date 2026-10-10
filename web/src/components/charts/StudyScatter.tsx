import { useMemo } from "react";
import EChart from "../EChart";
import type { Market, Study } from "../../api";
import { cssVar, MARKET_LABEL, useThemeVersion } from "../../lib";

/**
 * 各轮的 rank IC（横轴）与扣成本年化超额（纵轴）。打分真正有效时，点应落在预期方向一侧且收益为正；
 * 落在"方向与假设相反"一侧、收益却为正的点，就是第 8 章 #21 说的"收益提高但排序能力没有"。
 */
export default function StudyScatter({ study }: { study: Study }) {
  const theme = useThemeVersion();
  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text"), bad = cssVar("--bad");
    const sign = study.rule.ric_sign;
    const entries = Object.values(study.markets).filter((e) => e && e.rounds.length > 0);
    const all = entries.flatMap((e) => [...e!.rounds.map((r) => r.rank_ic ?? 0), e!.baseline?.rank_ic ?? 0]);
    const lim = Math.max(0.02, ...all.map((v) => Math.abs(v))) * 1.25;
    type P = { value: [number, number]; loop: number | null; decision: boolean | null; passed: boolean | null; m: Market };
    const series = entries.flatMap((e) => {
      const m = e!.market, color = cssVar(`--${m}`);
      const pts: P[] = e!.rounds.filter((r) => r.rank_ic != null && r.return != null)
        .map((r) => ({ value: [r.rank_ic!, r.return!], loop: r.loop, decision: r.decision, passed: r.passed, m }));
      const base = e!.baseline;
      return [
        {
          name: MARKET_LABEL[m], type: "scatter", itemStyle: { color }, data: pts.map((p) => ({
            ...p, symbolSize: p.decision ? 18 : 10,
            itemStyle: { color, opacity: p.decision ? 1 : 0.55, borderColor: p.decision ? text : "transparent", borderWidth: p.decision ? 1.5 : 0 },
          })),
          label: { show: true, position: "right", color: muted, fontSize: 10, formatter: (p: { data: P }) => (p.data.decision ? `R${p.data.loop} 采纳` : "") },
        },
        ...(base?.rank_ic != null && base.annual_return_with_cost != null ? [{
          name: MARKET_LABEL[m], type: "scatter", symbol: "diamond", symbolSize: 12,
          data: [{ value: [base.rank_ic, base.annual_return_with_cost], loop: null, decision: null, passed: null, m }],
          itemStyle: { color: "transparent", borderColor: color, borderWidth: 2 },
        }] : []),
      ];
    });
    const wrongSide = sign > 0 ? [-lim, 0] : [0, lim];
    return {
      animationDuration: 900,
      legend: { top: 0, right: 8, textStyle: { color: muted }, data: entries.map((e) => MARKET_LABEL[e!.market]) },
      grid: { left: 56, right: 24, top: 36, bottom: 44 },
      tooltip: {
        backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        formatter: (p: { data: P }) => {
          const d = p.data, who = d.loop == null ? "起点（20 个基础因子）" : `第 ${d.loop} 轮${d.decision ? "（RD-Agent 采纳）" : ""}`;
          const v = d.loop == null ? "" : `<br/>可证伪测试：${d.passed === true ? "通过" : d.passed === false ? "未通过" : "—"}`;
          return `<b>${MARKET_LABEL[d.m]} · ${who}</b><br/>rank IC ${d.value[0].toFixed(4)}<br/>扣成本年化 ${(d.value[1] * 100).toFixed(1)}%${v}`;
        },
      },
      xAxis: {
        type: "value", min: -lim, max: lim, name: "rank IC（打分与未来收益的排序相关）", nameLocation: "middle", nameGap: 28,
        nameTextStyle: { color: muted }, axisLabel: { color: muted, formatter: (v: number) => v.toFixed(2) },
        axisLine: { lineStyle: { color: line } }, splitLine: { lineStyle: { color: line, opacity: 0.3 } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: muted, formatter: (v: number) => `${Math.round(v * 100)}%` }, splitLine: { lineStyle: { color: line, opacity: 0.3 } },
      },
      series: [
        ...series,
        {  // 方向与假设相反的一侧
          type: "scatter", data: [], silent: true,
          markArea: { silent: true, itemStyle: { color: bad, opacity: 0.07 },
            label: { color: bad, position: "insideTop", fontSize: 11 },
            data: [[{ xAxis: wrongSide[0], name: "rank IC 方向与假设相反" }, { xAxis: wrongSide[1] }]] },
          markLine: { symbol: "none", silent: true, lineStyle: { color: muted, type: "dashed" }, label: { show: false },
            data: [{ xAxis: 0 }, { yAxis: 0 }] },
        },
      ],
    };
  }, [study, theme]);
  return <EChart option={option} notMerge className="h-[340px]" />;
}
