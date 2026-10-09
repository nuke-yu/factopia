import { useEffect, useMemo, useState } from "react";
import EChart from "../EChart";
import { api, type Positions } from "../../api";
import { cssVar, reducedMotion, useAsync, useThemeVersion } from "../../lib";
import { ErrorBox, Loading, ViewToggle } from "../ui";

const TOP = 30; // 只显示持有时间最长的 30 只股票

export default function PositionsBars({ runId, roundKey }: { runId: string; roundKey: string }) {
  const theme = useThemeVersion();
  const [precise, setPrecise] = useState(false);
  const { data, error } = useAsync<Positions>(() => api.positions(runId, roundKey, 10), [runId, roundKey]);
  const [shown, setShown] = useState(0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => { if (data) setShown(data.dates.length); setPlaying(false); }, [data]);
  useEffect(() => {
    if (!playing || !data) return;
    if (shown >= data.dates.length) { setPlaying(false); return; }
    const id = setTimeout(() => setShown((x) => x + 1), 160);
    return () => clearTimeout(id);
  }, [playing, shown, data]);

  const option = useMemo(() => {
    if (!data) return {};
    const muted = cssVar("--muted"), line = cssVar("--line-2"), text = cssVar("--text"), accent = cssVar("--accent");
    const inst = data.instruments.slice(0, TOP);
    const all = data.weights.flatMap((r) => r.slice(0, TOP)).filter((w) => w > 0).sort((a, b) => a - b);
    const maxW = Math.max(0.01, all.at(-1) ?? 0);
    const p95 = Math.max(0.01, all[Math.floor(all.length * 0.95)] ?? maxW); // 颜色按 95% 分位，个别大权重不压暗其他柱子
    const tooltip = {
      backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
      formatter: (p: { value: number[] }) => `<b>${inst[p.value[0]]}</b><br/>${data.dates[p.value[1]]}　权重 ${(p.value[2] * 100).toFixed(1)}%`,
    };
    const cells: [number, number, number][] = [];
    data.weights.forEach((row, d) => row.slice(0, TOP).forEach((w, i) => { if (w > 0 && d < shown) cells.push([i, d, w]); }));
    const visualMap = { show: false, min: 0, max: p95, inRange: { color: [cssVar("--line-2"), "#1f8f86", accent] } };
    if (precise) {
      return {
        tooltip, visualMap: { ...visualMap, dimension: 2 },
        grid: { left: 80, right: 10, top: 10, bottom: 40 },
        xAxis: { type: "category", data: inst, axisLabel: { color: muted, rotate: 60, fontSize: 9 }, axisLine: { lineStyle: { color: line } } },
        yAxis: { type: "category", data: data.dates, axisLabel: { color: muted, fontSize: 10 }, axisLine: { lineStyle: { color: line } } },
        series: [{ type: "heatmap", data: cells }],
      };
    }
    return {
      tooltip, visualMap: { ...visualMap, dimension: 2 },
      grid3D: {
        boxWidth: 200, boxDepth: 120, boxHeight: 70, environment: "none",
        light: { main: { intensity: 1.2, alpha: 40, beta: 40 }, ambient: { intensity: 0.35 } },
        postEffect: { enable: true, bloom: { enable: true, bloomIntensity: 0.15 } },
        viewControl: { distance: 270, alpha: 28, beta: 32, autoRotate: !reducedMotion(), autoRotateSpeed: 3 },
        axisPointer: { show: false },
      },
      xAxis3D: { type: "category", data: inst, name: "股票", nameTextStyle: { color: muted }, axisLabel: { color: muted, fontSize: 9, interval: 2 }, axisLine: { lineStyle: { color: line } } },
      yAxis3D: { type: "category", data: data.dates, name: "日期", nameTextStyle: { color: muted }, axisLabel: { color: muted, fontSize: 9, interval: 7 }, axisLine: { lineStyle: { color: line } } },
      zAxis3D: { type: "value", name: "权重", max: maxW, nameTextStyle: { color: muted }, axisLabel: { color: muted, formatter: (v: number) => `${Math.round(v * 100)}%` }, axisLine: { lineStyle: { color: line } } },
      series: [{ type: "bar3D", data: cells, shading: "lambert", barSize: [1.6, 1.6], bevelSize: 0.3 }],
    };
  }, [data, shown, precise, theme]);

  if (error) return <ErrorBox error={error} />;
  if (!data) return <div className="h-[400px]"><Loading what="持仓" /></div>;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {!precise && <button className="btn" disabled={reducedMotion()} onClick={() => { if (shown >= data.dates.length) setShown(1); setPlaying((x) => !x); }}>{playing ? "暂停" : "按时间回放"}</button>}
        <span className="num text-xs text-muted">{data.dates[Math.max(0, shown - 1)]}</span>
        <div className="ml-auto"><ViewToggle precise={precise} onChange={setPrecise} /></div>
      </div>
      <EChart option={option} notMerge className="h-[400px]" />
      <p className="text-xs text-muted">每 10 个交易日取一次持仓；显示持有时间最长的 {TOP} 只（共 {data.instruments.length} 只出现过）。柱子升起、落下即模型换仓。</p>
    </div>
  );
}
