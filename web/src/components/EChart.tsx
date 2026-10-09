import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import "echarts-gl";

type Props = {
  option: echarts.EChartsOption | Record<string, unknown>;
  className?: string;
  notMerge?: boolean;
  onReady?: (chart: echarts.ECharts) => void;
  onClick?: (params: echarts.ECElementEvent) => void;
};

/** ECharts（含 ECharts GL 3D 图表）的通用容器：自动适应尺寸，option 变化时增量更新。 */
export default function EChart({ option, className, notMerge = false, onReady, onClick }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const clickRef = useRef(onClick);
  clickRef.current = onClick;

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    c.on("click", (p) => clickRef.current?.(p as echarts.ECElementEvent));
    onReady?.(c);
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chart.current?.setOption(option as echarts.EChartsOption, { notMerge, lazyUpdate: true });
  }, [option, notMerge]);

  return <div ref={el} className={className} />;
}
