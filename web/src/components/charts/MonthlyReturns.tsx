import { useMemo, useState } from "react";
import EChart from "../EChart";
import { api, type Monthly, type PeriodReturn } from "../../api";
import { cssVar, reducedMotion, useAsync, useThemeVersion } from "../../lib";
import { ErrorBox, Loading, ViewToggle } from "../ui";

const MONTHS = Array.from({ length: 12 }, (_, i) => `${i + 1} 月`);
const pctLabel = (v: number | null | undefined, nd = 1) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(nd)}%`);

/** 月度收益：3D 视图为年 × 月的涨跌柱，精确视图为带数值的热力图（含全年列）。绿色为跑赢基准，红色为跑输。 */
export default function MonthlyReturns({ runId, roundKey }: { runId: string; roundKey: string }) {
  const theme = useThemeVersion();
  const [precise, setPrecise] = useState(false);
  const all = useAsync(() => api.monthly(runId), [runId]);
  const data: Monthly | undefined = all.data?.[roundKey];

  const option = useMemo(() => {
    if (!data) return {};
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text");
    const years = data.years.map((y) => String(y.year));
    const cells = data.months.map((m) => ({ ...m, x: m.month - 1, y: years.indexOf(String(m.year)) }));
    const bound = Math.max(0.01, ...cells.map((c) => Math.abs(c.excess)));
    const visualMap = { show: false, dimension: 2, min: -bound, max: bound, inRange: { color: [cssVar("--bad"), cssVar("--bg-3"), cssVar("--good")] } };
    const tip = (year: number, label: string, r: { days: number; excess: number; bench: number }) =>
      `<b>${year} 年 ${label}</b>（${r.days} 个交易日）<br/>超额收益（扣成本）${pctLabel(r.excess, 2)}<br/>基准指数 ${pctLabel(r.bench, 2)}`;
    const tooltip = { backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text } };

    if (precise) {
      const yearCells = data.years.map((y, i) => ({ value: [12, i, y.excess], r: y, label: "全年" }));
      return {
        tooltip: { ...tooltip, formatter: (p: { data: { value: number[]; r: PeriodReturn; label: string } }) =>
          tip(Number(years[p.data.value[1]]), p.data.label, p.data.r) },
        visualMap,
        grid: { left: 56, right: 12, top: 8, bottom: 28 },
        xAxis: { type: "category", data: [...MONTHS, "全年"], axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } }, splitArea: { show: false } },
        yAxis: { type: "category", data: years, inverse: true, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } } },
        series: [{
          type: "heatmap",
          data: [...cells.map((c) => ({ value: [c.x, c.y, c.excess], r: c, label: `${c.month} 月` })), ...yearCells],
          label: { show: true, color: text, fontSize: 11, formatter: (p: { value: number[] }) => pctLabel(p.value[2]) },
          itemStyle: { borderColor: cssVar("--bg-2"), borderWidth: 2 },
        }],
      };
    }
    return {
      tooltip: { ...tooltip, formatter: (p: { value: number[] }) => {
        const c = cells.find((x) => x.x === p.value[0] && x.y === p.value[1]);
        return c ? tip(c.year, `${c.month} 月`, c) : "";
      } },
      visualMap,
      grid3D: {
        boxWidth: 200, boxDepth: Math.max(60, years.length * 45), boxHeight: 80, environment: "none",
        light: { main: { intensity: 1.2, alpha: 40, beta: 30 }, ambient: { intensity: 0.4 } },
        postEffect: { enable: true, bloom: { enable: true, bloomIntensity: 0.12 } },
        viewControl: { distance: 175, alpha: 22, beta: 8, autoRotate: !reducedMotion(), autoRotateSpeed: 2 },
        axisPointer: { show: false },
      },
      xAxis3D: { type: "category", data: MONTHS, name: "", axisLabel: { color: muted, fontSize: 10 }, axisLine: { lineStyle: { color: line } } },
      yAxis3D: { type: "category", data: years, name: "", axisLabel: { color: muted, fontSize: 11 }, axisLine: { lineStyle: { color: line } } },
      zAxis3D: { type: "value", name: "", splitNumber: 4,
        axisLabel: { color: muted, formatter: (v: number) => `${Math.round(v * 100)}%` }, axisLine: { lineStyle: { color: line } } },
      series: [{ type: "bar3D", data: cells.map((c) => [c.x, c.y, c.excess]), shading: "lambert", barSize: [11, 22], bevelSize: 0.2 }],
    };
  }, [data, precise, theme]);

  if (all.error) return <ErrorBox error={all.error} />;
  if (!all.data) return <div className="h-[320px]"><Loading what="月度收益" /></div>;
  if (!data) return <div className="text-muted text-sm">该轮没有逐日回测数据。</div>;
  const wins = data.months.filter((m) => m.excess > 0).length;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
        {data.years.map((y) => (
          <span key={y.year} className="text-muted">
            {y.year} 年 <span className="num" style={{ color: y.excess >= 0 ? cssVar("--good") : cssVar("--bad") }}>{pctLabel(y.excess)}</span>
            <span className="text-faint">（基准 {pctLabel(y.bench)}）</span>
          </span>
        ))}
        <span className="text-muted">跑赢基准的月份 <span className="num text-text">{wins} / {data.months.length}</span></span>
        <div className="ml-auto"><ViewToggle precise={precise} onChange={setPrecise} /></div>
      </div>
      <EChart option={option} notMerge className={precise ? "h-[160px]" : "h-[340px]"} />
      <p className="text-xs text-muted">
        每格是一个月的扣成本后超额收益（相对基准指数，按日复利），绿色跑赢、红色跑输。月份颜色是否集中在某个时段，能看出策略是否只靠少数行情赚钱。
        首尾月份交易日不完整，以提示中的交易日数为准。
      </p>
    </div>
  );
}
