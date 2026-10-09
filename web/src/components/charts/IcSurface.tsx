import { useEffect, useMemo, useState } from "react";
import EChart from "../EChart";
import type { Market, Surface } from "../../api";
import { api } from "../../api";
import { cssVar, MARKET_LABEL, reducedMotion, useAsync, useThemeVersion } from "../../lib";
import { ErrorBox, Loading, Seg, ViewToggle } from "../ui";

const Z = 0.25; // 颜色与高度的显示范围：月均 RankIC ±0.25

export default function IcSurface({ horizon }: { horizon: number }) {
  const [market, setMarket] = useState<Market>("hk");
  const [smooth, setSmooth] = useState(true);
  const [precise, setPrecise] = useState(false);
  const { data, error } = useAsync<Surface>(() => api.surface(horizon, market), [horizon, market]);
  if (error) return <ErrorBox error={error} />;
  const s = data?.markets[market];
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Seg label="市场" value={market} onChange={setMarket}
          options={(["hk", "uk", "fr"] as Market[]).map((m) => ({ value: m, label: MARKET_LABEL[m] }))} />
        <Seg label="平滑" value={smooth ? "r6" : "m"} onChange={(v) => setSmooth(v === "r6")}
          options={[{ value: "r6", label: "6 个月滚动" }, { value: "m", label: "逐月" }]} />
        <div className="ml-auto"><ViewToggle precise={precise} onChange={setPrecise} /></div>
      </div>
      {!s || !data ? <div className="h-[480px]"><Loading what="因子时间曲面" /></div>
        : <SurfaceView s={s} boundary={data.test_boundary} smooth={smooth} precise={precise} market={market} />}
      <p className="text-xs text-muted leading-relaxed">
        横轴为 158 个 Alpha158 因子（按类型分组），纵深为月份，高度与颜色为该因子当月的预测能力（RankIC，预测未来 {horizon} 个交易日收益）。
        发光的竖直平面是 RD-Agent 回测测试期的起点（{data?.test_boundary ?? "2022-01"}）：平面之前的数据可以提供给 AI，之后的不可以（MINE-11）。
      </p>
    </div>
  );
}

function SurfaceView({ s, boundary, smooth, precise, market }: {
  s: NonNullable<Surface["markets"][Market]>; boundary: string; smooth: boolean; precise: boolean; market: Market;
}) {
  const theme = useThemeVersion();
  const z = smooth ? s.rolling6 : s.monthly;
  const nF = s.factors.length, nM = s.months.length;
  const bIdx = Math.max(0, s.months.indexOf(boundary));
  // 时间扫描：从第一个月逐步显示到最后一个月
  const [shown, setShown] = useState(nM);
  const [playing, setPlaying] = useState(false);

  useEffect(() => { setShown(nM); setPlaying(false); }, [market, nM]);
  useEffect(() => {
    if (!playing) return;
    if (shown >= nM) { setShown(6); return; }
    const id = setTimeout(() => setShown((x) => Math.min(nM, x + 3)), 90);
    return () => clearTimeout(id);
  }, [playing, shown, nM]);
  useEffect(() => { if (playing && shown >= nM) setPlaying(false); }, [playing, shown, nM]);

  const option = useMemo(() => {
    const muted = cssVar("--muted"), line = cssVar("--line-2"), accent = cssVar("--accent"), text = cssVar("--text");
    const colors = ["#5866ff", "#3b4f8f", cssVar("--bg-3"), "#1f8f86", accent];
    const tooltip = {
      backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
      formatter: (p: { value: number[]; seriesIndex: number }) => {
        if (p.seriesIndex !== 0) return "测试期起点";
        const [fi, mi] = p.value; const v = z[fi]?.[mi];
        return `<b>${s.factors[fi]}</b><br/>${s.months[mi]}　RankIC ${v == null ? "无数据" : v.toFixed(3)}`;
      },
    };
    if (precise) {
      const data: [number, number, number][] = [];
      for (let f = 0; f < nF; f++) for (let m = 0; m < nM; m++) { const v = z[f][m]; if (v != null) data.push([m, f, v]); }
      return {
        tooltip: { ...tooltip, formatter: (p: { value: number[] }) => `<b>${s.factors[p.value[1]]}</b><br/>${s.months[p.value[0]]}　RankIC ${p.value[2].toFixed(3)}` },
        grid: { left: 70, right: 20, top: 10, bottom: 60 },
        xAxis: { type: "category", data: s.months, axisLabel: { color: muted, interval: 11 }, axisLine: { lineStyle: { color: line } } },
        yAxis: { type: "category", data: s.factors, axisLabel: { color: muted, fontSize: 9, interval: 9 }, axisLine: { lineStyle: { color: line } } },
        visualMap: { min: -Z, max: Z, calculable: true, orient: "horizontal", left: "center", bottom: 0, itemHeight: 160, inRange: { color: colors }, textStyle: { color: muted } },
        series: [{
          type: "heatmap", data, progressive: 4000,
          markLine: { silent: true, symbol: "none", lineStyle: { color: accent, width: 2 }, data: [{ xAxis: boundary }] },
        }],
      };
    }
    const data: [number, number, number][] = [];
    for (let m = 0; m < nM; m++) for (let f = 0; f < nF; f++) {
      const v = m < shown ? z[f][m] : null;
      data.push([f, m, v == null ? 0 : Math.max(-Z, Math.min(Z, v))]);
    }
    return {
      tooltip,
      visualMap: { show: false, min: -Z, max: Z, dimension: 2, seriesIndex: 0, inRange: { color: colors } },
      grid3D: {
        boxWidth: 200, boxDepth: 120, boxHeight: 46, environment: "none",
        light: { main: { intensity: 1.2, shadow: false, alpha: 40, beta: 30 }, ambient: { intensity: 0.35 } },
        postEffect: { enable: true, bloom: { enable: true, bloomIntensity: 0.18 } },
        viewControl: { distance: 250, alpha: 28, beta: -32, autoRotate: false },
        axisPointer: { show: false },
      },
      xAxis3D: { type: "value", name: "因子", min: 0, max: nF - 1, axisLabel: { show: false }, nameTextStyle: { color: muted }, axisLine: { lineStyle: { color: line } }, splitLine: { show: false } },
      yAxis3D: { type: "value", name: "月份", min: 0, max: nM - 1, nameTextStyle: { color: muted },
        axisLabel: { color: muted, formatter: (v: number) => s.months[Math.round(v)]?.slice(0, 4) ?? "" }, axisLine: { lineStyle: { color: line } }, splitLine: { lineStyle: { color: line, opacity: 0.25 } } },
      zAxis3D: { type: "value", name: "RankIC", min: -Z, max: Z, nameTextStyle: { color: muted }, axisLabel: { color: muted }, axisLine: { lineStyle: { color: line } }, splitLine: { lineStyle: { color: line, opacity: 0.25 } } },
      series: [
        {
          type: "surface", data, shading: "lambert", silent: false,
          wireframe: { show: true, lineStyle: { color: line, opacity: 0.25, width: 0.5 } },
          dataShape: [nM, nF],
        },
        {
          type: "surface", parametric: true, silent: true, shading: "color",
          itemStyle: { color: accent, opacity: 0.1 },
          wireframe: { show: true, lineStyle: { color: accent, opacity: 0.45, width: 1 } },
          parametricEquation: {
            u: { min: 0, max: nF - 1, step: (nF - 1) / 8 }, v: { min: -Z, max: Z, step: Z / 2 },
            x: (u: number) => u, y: () => bIdx, z: (_u: number, v: number) => v,
          },
        },
      ],
    };
  }, [s, z, shown, precise, boundary, bIdx, nF, nM, theme]);

  return (
    <div className="relative">
      <EChart option={option} notMerge className="h-[480px]" />
      {!precise && (
        <div className="absolute left-2 top-2 flex items-center gap-2">
          <button className="btn" disabled={reducedMotion()} onClick={() => { if (shown >= nM) setShown(6); setPlaying((x) => !x); }}>
            {playing ? "暂停" : "按时间播放"}
          </button>
          <span className="num text-xs text-muted">{s.months[Math.max(0, shown - 1)]}</span>
        </div>
      )}
    </div>
  );
}
