import { useEffect, useMemo, useRef, useState } from "react";
import EChart from "../EChart";
import type { Cube, Market, PeriodKey } from "../../api";
import { cssVar, labelColor, MARKET_LABEL, reducedMotion, useThemeVersion, fix } from "../../lib";
import { Seg, ViewToggle } from "../ui";

const PERIODS: PeriodKey[] = ["train", "valid", "test"];
const AXIS: Market[] = ["hk", "uk", "fr"];
type Pt = [number, number, number];

/** 显著性立方体的 12 条棱，按一条折线走完（部分棱重复经过）。 */
function cubeEdges(t: number): Pt[] {
  const v = (x: number, y: number, z: number): Pt => [x * t, y * t, z * t];
  const [A, B, C, D] = [v(-1, -1, -1), v(1, -1, -1), v(1, 1, -1), v(-1, 1, -1)];
  const [E, F, G, H] = [v(-1, -1, 1), v(1, -1, 1), v(1, 1, 1), v(-1, 1, 1)];
  return [A, B, C, D, A, E, F, B, F, G, C, G, H, D, H, E];
}

const ease = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

export default function TransferCube({ cube }: { cube: Cube }) {
  const theme = useThemeVersion();
  const [period, setPeriod] = useState<PeriodKey>("train");
  const [precise, setPrecise] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  // 当前绘制的位置（时期切换时在两个时期之间插值，形成"迁移"动画）
  const [pos, setPos] = useState<Pt[]>(() => cube.factors.map((f) => AXIS.map((m) => f.periods.train.t[m] ?? 0) as Pt));
  const anim = useRef<number>(0);

  const target = (p: PeriodKey) => cube.factors.map((f) => AXIS.map((m) => f.periods[p].t[m] ?? 0) as Pt);

  useEffect(() => {
    const from = pos, to = target(period);
    if (reducedMotion()) { setPos(to); return; }
    const t0 = performance.now(), dur = 1400;
    cancelAnimationFrame(anim.current);
    const tick = (now: number) => {
      const k = ease(Math.min(1, (now - t0) / dur));
      setPos(from.map((a, i) => [0, 1, 2].map((j) => a[j] + (to[i][j] - a[j]) * k) as Pt));
      if (k < 1) anim.current = requestAnimationFrame(tick);
    };
    anim.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(anim.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period]);

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => setPeriod((p) => PERIODS[(PERIODS.indexOf(p) + 1) % PERIODS.length]), 3200);
    return () => clearInterval(id);
  }, [playing]);

  const lim = useMemo(() => {
    const all = cube.factors.flatMap((f) => PERIODS.flatMap((p) => AXIS.map((m) => Math.abs(f.periods[p].t[m] ?? 0))));
    return Math.ceil(Math.max(6, ...all));
  }, [cube]);

  const counts = useMemo(() => {
    const c = new Map<string, number>();
    cube.factors.forEach((f) => c.set(f.periods[period].label, (c.get(f.periods[period].label) ?? 0) + 1));
    return [...c.entries()].sort((a, b) => b[1] - a[1]);
  }, [cube, period]);

  const option = useMemo(() => {
    const text = cssVar("--text"), muted = cssVar("--muted"), line = cssVar("--line-2"), accent = cssVar("--accent");
    const axis = (m: Market) => ({
      name: `${MARKET_LABEL[m]} t`, min: -lim, max: lim, nameTextStyle: { color: cssVar(`--${m}`), fontSize: 13 },
      axisLine: { lineStyle: { color: cssVar(`--${m}`), width: 2 } }, axisLabel: { color: muted },
      splitLine: { lineStyle: { color: line, opacity: 0.35 } }, axisPointer: { show: false },
    });
    return {
      tooltip: {
        backgroundColor: cssVar("--bg-2"), borderColor: line, textStyle: { color: text },
        formatter: (p: { dataIndex: number; seriesType: string }) => {
          if (p.seriesType !== "scatter3D") return "";
          const f = cube.factors[p.dataIndex], d = f.periods[period];
          return `<b>${f.name}</b> · ${f.group}<br/>${f.desc}<br/>香港 t=${fix(d.t.hk, 2)}　英国 t=${fix(d.t.uk, 2)}　法国 t=${fix(d.t.fr, 2)}<br/>分类：${d.label}`;
        },
      },
      grid3D: {
        boxWidth: 100, boxHeight: 100, boxDepth: 100,
        environment: "none",
        light: { main: { intensity: 1.1 }, ambient: { intensity: 0.4 } },
        postEffect: { enable: true, bloom: { enable: true, bloomIntensity: 0.25 } },
        viewControl: { autoRotate: !reducedMotion() && !precise, autoRotateSpeed: 4, distance: 230, alpha: 18, beta: 35 },
        axisPointer: { show: false },
      },
      xAxis3D: axis("hk"), yAxis3D: axis("uk"), zAxis3D: axis("fr"),
      series: [
        {
          type: "scatter3D",
          data: pos.map((p, i) => ({
            value: p,
            itemStyle: {
              color: labelColor(cube.factors[i].periods[period].label),
              opacity: cube.factors[i].periods[period].label === "无效" ? 0.35 : 1,
            },
            symbolSize: cube.factors[i].name === selected ? 16 : cube.factors[i].periods[period].label === "无效" ? 5 : 10,
          })),
          emphasis: { itemStyle: { color: accent } },
        },
        {
          type: "line3D", data: cubeEdges(cube.threshold),
          lineStyle: { color: accent, opacity: 0.55, width: 1.5 }, silent: true,
        },
      ],
    };
  }, [cube, pos, period, lim, precise, selected, theme]);

  const significant = cube.factors
    .map((f) => ({ f, d: f.periods[period] }))
    .filter(({ d }) => d.label !== "无效")
    .sort((a, b) => Math.max(...AXIS.map((m) => Math.abs(b.d.t[m] ?? 0))) - Math.max(...AXIS.map((m) => Math.abs(a.d.t[m] ?? 0))));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Seg label="评估时期" value={period} onChange={(p) => { setPlaying(false); setPeriod(p); }}
          options={PERIODS.map((p) => ({ value: p, label: cube.periods[p].label }))} />
        <button className="btn" onClick={() => setPlaying((x) => !x)} aria-pressed={playing}>{playing ? "暂停" : "播放时期变化"}</button>
        <span className="text-xs text-muted">{cube.periods[period].note}</span>
        <div className="ml-auto"><ViewToggle precise={precise} onChange={setPrecise} /></div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[1fr_260px]">
        {precise ? (
          <div className="overflow-x-auto max-h-[460px] overflow-y-auto">
            <table className="w-full text-sm num">
              <thead className="sticky top-0 bg-bg-2 text-muted text-xs">
                <tr><th className="text-left py-1.5 pr-3">因子</th><th className="text-left pr-3 font-sans">类型</th>
                  {AXIS.map((m) => <th key={m} className="text-right pr-3" style={{ color: cssVar(`--${m}`) }}>{MARKET_LABEL[m]} t</th>)}
                  <th className="text-left font-sans">分类</th></tr>
              </thead>
              <tbody>
                {significant.map(({ f, d }) => (
                  <tr key={f.name} className="border-t border-line">
                    <td className="py-1.5 pr-3">{f.name}</td><td className="pr-3 font-sans text-muted">{f.group}</td>
                    {AXIS.map((m) => <td key={m} className={`text-right pr-3 ${Math.abs(d.t[m] ?? 0) > cube.threshold ? "text-text" : "text-faint"}`}>{fix(d.t[m], 2)}</td>)}
                    <td className="font-sans" style={{ color: labelColor(d.label) }}>{d.label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {significant.length === 0 && <div className="text-muted py-6 text-center">该时期没有显著因子</div>}
          </div>
        ) : (
          <EChart option={option} className="h-[460px]" onClick={(p) => p.seriesType === "scatter3D" && setSelected(cube.factors[p.dataIndex].name)} />
        )}
        <aside className="flex flex-col gap-3">
          <div>
            <div className="label mb-2">分类（{cube.periods[period].label}，|t| &gt; {cube.threshold}）</div>
            <ul className="flex flex-col gap-1.5">
              {counts.map(([label, n]) => (
                <li key={label} className="flex items-center gap-2 text-sm">
                  <span className="size-2.5 rounded-full" style={{ background: labelColor(label) }} />
                  <span className="flex-1">{label}</span><span className="num text-muted">{n}</span>
                </li>
              ))}
            </ul>
          </div>
          <p className="text-xs text-muted leading-relaxed">
            每个点是一个 Alpha158 因子，三条坐标轴是它在香港、英国、法国的显著性（t 值）。
            中间的立方体是"不显著"区域；沿某条轴伸出去为地区特定，落在角上为三市场通用。
          </p>
          {selected && (() => {
            const f = cube.factors.find((x) => x.name === selected)!;
            return (
              <div className="border-t border-line pt-3 text-sm">
                <div className="num font-medium">{f.name}</div>
                <div className="text-muted text-xs mb-2">{f.group} · {f.desc}</div>
                {PERIODS.map((p) => (
                  <div key={p} className="flex gap-2 text-xs num">
                    <span className="w-20 font-sans text-faint">{cube.periods[p].label}</span>
                    {AXIS.map((m) => <span key={m} className="w-12 text-right" style={{ color: cssVar(`--${m}`) }}>{fix(f.periods[p].t[m], 1)}</span>)}
                  </div>
                ))}
              </div>
            );
          })()}
        </aside>
      </div>
    </div>
  );
}
