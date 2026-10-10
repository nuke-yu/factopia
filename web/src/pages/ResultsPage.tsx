import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Metrics, type Quantiles, type Round, type Run } from "../api";
import { cssVar, fix, MARKET_LABEL, pct, signed, useAsync } from "../lib";
import { Badge, ErrorBox, Loading, Notice, Panel, Stat, ViewToggle } from "../components/ui";
import ResearchTrail from "../components/charts/ResearchTrail";
import TransferCube from "../components/charts/TransferCube";
import IcSurface from "../components/charts/IcSurface";
import NavChart from "../components/charts/NavChart";
import PositionsBars from "../components/charts/PositionsBars";
import DrawdownChart from "../components/charts/DrawdownChart";
import MonthlyReturns from "../components/charts/MonthlyReturns";
import { QuantileBars, QuantileNav } from "../components/charts/QuantileChart";

export default function ResultsPage() {
  const runs = useAsync(() => api.runs(), []);
  const [params, setParams] = useSearchParams();
  const runId = params.get("run") ?? runs.data?.find((r) => r.rounds >= 3)?.run_id ?? runs.data?.[0]?.run_id ?? "";

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <div className="label">04 · 结果</div>
          <h1 className="font-display text-2xl font-semibold tracking-wide mt-1">挖掘与验证结果</h1>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label htmlFor="run" className="label whitespace-nowrap">运行</label>
          <select id="run" className="field w-auto num text-sm" value={runId} onChange={(e) => setParams({ run: e.target.value })}>
            {runs.data?.map((r) => (
              <option key={r.run_id} value={r.run_id}>{r.run_id} · {MARKET_LABEL[r.market]} · {r.rounds} 轮</option>
            ))}
          </select>
        </div>
      </div>
      {runs.error && <ErrorBox error={runs.error} />}
      {runs.data && runs.data.length === 0 && (
        <Notice tone="accent" title="还没有导出的运行结果">先运行 factopia.export.rdagent_run 与 factopia.export.backtests（见 README）。</Notice>
      )}
      {runId && <RunSection key={runId} runId={runId} />}
      <Panel eyebrow="Alpha158 · 三市场" title="迁移性立方体" className="min-w-0">
        <CubeSection />
      </Panel>
      <Panel eyebrow="Alpha158 · 时间维度" title="因子时间曲面">
        <IcSurface horizon={20} />
      </Panel>
    </div>
  );
}

function CubeSection() {
  const cube = useAsync(() => api.cube(20), []);
  if (cube.error) return <ErrorBox error={cube.error} />;
  if (!cube.data) return <div className="h-[460px]"><Loading what="迁移性立方体" /></div>;
  return <TransferCube cube={cube.data} />;
}

function RunSection({ runId }: { runId: string }) {
  const detail = useAsync(() => api.run(runId), [runId]);
  const backtest = useAsync(() => api.backtest(runId), [runId]);
  const quantiles = useAsync(() => api.quantiles(runId), [runId]);
  const [selected, setSelected] = useState("baseline");
  const [precise, setPrecise] = useState(false);
  useEffect(() => {
    const adopted = detail.data?.rounds.find((r) => r.decision);
    setSelected(adopted ? String(adopted.loop) : "baseline");
  }, [detail.data]);

  if (detail.error) return <ErrorBox error={detail.error} />;
  if (!detail.data) return <Loading what="运行结果" />;
  const { run, rounds } = detail.data;
  const best = rounds.filter((r) => r.decision).at(-1);
  const posKey = selected === "baseline" ? (best ? String(best.loop) : "0") : selected;

  return (
    <>
      <div className="flex flex-col gap-2">
        {run.context_leak && (
          <Notice tone="bad" title="此次运行受数据泄漏影响，结果仅用于展示流程">
            运行时提供给 AI 的已有发现包含测试期（2022–2023）的评估结论（MINE-11 修复前），不能作为研究结论。
          </Notice>
        )}
        <Notice title="回测结果可信度待排查（Spec 第 8 章 #21）">
          各轮 IC 均为负而收益为正，原因未明；比较基准为 RD-Agent 默认的 20 个基础因子，而非完整 Alpha158。
        </Notice>
      </div>

      <div>
        <Panel eyebrow={`${MARKET_LABEL[run.market]} · 预测周期 ${run.horizon} 个交易日`} title="本次运行">
          <div className="grid gap-6 lg:grid-cols-[minmax(0,4fr)_minmax(0,8fr)]">
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-4">
                <Stat label="轮数 / 采纳" value={`${run.rounds} / ${run.adopted}`} />
                <Stat label="大模型费用" value={`$${run.cost_usd.toFixed(2)}`} sub="DeepSeek" />
                <Stat label="起点（扣成本年化）" value={pct(run.baseline?.metrics?.annual_return_with_cost)} sub="20 个基础因子" />
                <Stat label="最好（扣成本年化）" value={pct(best?.metrics?.annual_return_with_cost ?? run.baseline?.metrics?.annual_return_with_cost)}
                  tone={cssVar("--good")} sub={best ? `第 ${best.loop} 轮` : "没有采纳"} />
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                <dt className="text-faint">训练</dt><dd className="num">{run.settings.train_start} 至 {run.settings.train_end}</dd>
                <dt className="text-faint">验证</dt><dd className="num">{run.settings.valid_start} 至 {run.settings.valid_end}</dd>
                <dt className="text-faint">测试（回测）</dt><dd className="num">{run.settings.test_start} 至 {run.settings.test_end}</dd>
              </dl>
              <p className="text-xs text-muted leading-relaxed">净值为各轮在测试期的扣成本后超额收益累计，虚线为基准指数。回撤是累计超额收益从此前最高点回落的幅度，最低点就是表中的最大回撤。模型训练不使用测试期数据，但 AI 每轮会看到测试期成绩来决定是否采纳，因此仍需在 2024 年以后的数据上复核。</p>
              <Link className="btn self-start" to={`/run?run=${run.run_id}`}>回放研究过程 →</Link>
            </div>
            <div>
              <div className="label mb-1">回测净值（测试期，扣成本后超额）</div>
              {backtest.data && Object.keys(backtest.data).length === 0 ? (
                <div className="text-sm text-muted py-10">逐日回测明细尚未导出：运行结束后由 exporter 服务自动导出（约 1 分钟），起点轮次没有逐日结果。</div>
              ) : backtest.data ? (
                <>
                  <NavChart backtest={backtest.data} rounds={rounds} selected={selected} className="h-[290px]" />
                  <div className="label mt-2 mb-1">回撤（选中轮次为红色面积）</div>
                  <DrawdownChart backtest={backtest.data} rounds={rounds} selected={selected} />
                </>
              ) : <Loading what="回测" />}
            </div>
          </div>
        </Panel>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <Panel eyebrow="RD-Agent · 逐轮" title="AI 研究轨迹" actions={<ViewToggle precise={precise} onChange={setPrecise} />}>
          {precise ? <RoundTable run={run} rounds={rounds} selected={selected} onSelect={setSelected} /> : (
            <div className="h-[520px] -mx-1">
              <ResearchTrail run={run} rounds={rounds} selected={selected} onSelect={setSelected} />
            </div>
          )}
          <p className="text-xs text-muted mt-2">
            每个节点是一轮：绿色为被采纳，红色为未采纳；竖线长度为扣成本后年化超额收益；虚线表示该轮与当时的最好结果比较。点击节点查看详情。
          </p>
        </Panel>
        <Panel eyebrow="详情" title={selected === "baseline" ? "起点（基础因子）" : `第 ${selected} 轮`}>
          <RoundDetail run={run} round={rounds.find((r) => String(r.loop) === selected)} />
        </Panel>
      </div>

      <Panel eyebrow={`第 ${posKey} 轮 · 测试期`} title="分组收益">
        {quantiles.error ? <ErrorBox error={quantiles.error} />
          : !quantiles.data ? <Loading what="分组收益" />
          : quantiles.data[posKey] ? <QuantileSection q={quantiles.data[posKey]} />
          : <div className="text-muted text-sm">该轮没有分组收益数据（先运行 factopia.export.backtests）。</div>}
      </Panel>

      <Panel eyebrow={`第 ${posKey} 轮 · 测试期`} title="月度收益">
        <MonthlyReturns runId={run.run_id} roundKey={posKey} />
      </Panel>

      <Panel eyebrow={`第 ${posKey} 轮 · 测试期`} title="持仓轮动">
        <PositionsBars runId={run.run_id} roundKey={posKey} />
      </Panel>
    </>
  );
}

function QuantileSection({ q }: { q: Quantiles }) {
  const n = q.groups;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted leading-relaxed max-w-[110ch]">
        每个交易日按模型打分把股票从低到高平均分成 {n} 组，看每组接下来 {q.horizon} 个交易日的实际涨跌。打分有效时，柱子应从左到右逐级升高，
        右图 {n} 条线像扇子一样展开、第 {n} 组在最上面。这是判断因子好坏最直观的图，与持仓数、交易成本无关。
      </p>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Stat label={`第 ${n} 组减第 1 组`} value={signed(q.spread_mean, 2)} tone={(q.spread_mean ?? 0) > 0 ? cssVar("--good") : cssVar("--bad")}
          sub={`平均未来 ${q.horizon} 日收益之差`} />
        <Stat label={`第 ${n} 组跑赢第 1 组`} value={pct(q.spread_positive_share)} sub={`占 ${q.days} 个交易日`} />
        <Stat label="全部股票平均" value={pct(q.universe_mean, 2)} sub={`未来 ${q.horizon} 日收益`} />
        <Stat label="打分完全相同的股票" value={pct(q.tie_share, 0)} sub="每日最大同分组占比（中位数）" />
      </div>
      {(q.tie_share ?? 0) > 0.2 && (
        <Notice title="模型打分区分度不足">
          每天约 {pct(q.tie_share, 0)} 的股票得到完全相同的打分，只能放进同一组，所以各组股票数不等（平均 {q.size.map((s) => s.toFixed(0)).join(" / ")} 只），
          部分交易日有的组没有股票（累计收益里按持有现金计）。这与第 8 章 #21（打分几乎相同、IC 为负）是同一个问题。
        </Notice>
      )}
      <div className="grid gap-5 lg:grid-cols-2">
        <div>
          <div className="label mb-1">各组平均未来 {q.horizon} 日收益</div>
          <QuantileBars q={q} />
        </div>
        <div>
          <div className="label mb-1">各组累计收益（每 {q.horizon} 个交易日调仓）</div>
          <QuantileNav q={q} />
        </div>
      </div>
    </div>
  );
}

function RoundTable({ run, rounds, selected, onSelect }: { run: Run; rounds: Round[]; selected: string; onSelect: (k: string) => void }) {
  const rows: { key: string; name: string; m: Metrics | null | undefined; r?: Round }[] = [
    { key: "baseline", name: "起点", m: run.baseline?.metrics }, ...rounds.map((r) => ({ key: String(r.loop), name: `第 ${r.loop} 轮`, m: r.metrics, r })),
  ];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs text-muted"><tr>
          <th className="text-left py-1.5">轮次</th><th className="text-left">假设（AI 原文）</th>
          <th className="text-right">IC</th><th className="text-right">扣成本年化</th><th className="text-right">最大回撤</th><th className="text-right">结论</th>
        </tr></thead>
        <tbody>
          {rows.map(({ key, name, m, r }) => (
            <tr key={key} onClick={() => onSelect(key)} className={`border-t border-line cursor-pointer ${selected === key ? "bg-accent/8" : "hover:bg-bg-3"}`}>
              <td className="py-2 pr-3 whitespace-nowrap">{name}</td>
              <td className="pr-3 text-muted max-w-[420px] truncate" title={r?.hypothesis ?? ""}>{r?.hypothesis ?? "20 个基础因子"}</td>
              <td className="num text-right pr-3">{fix(m?.ic)}</td>
              <td className="num text-right pr-3">{pct(m?.annual_return_with_cost)}</td>
              <td className="num text-right pr-3">{pct(m?.max_drawdown_with_cost)}</td>
              <td className="text-right">{r ? (r.decision ? <Badge tone="good">采纳</Badge> : <Badge tone="bad">未采纳</Badge>) : <Badge>起点</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Compare({ cur, ref_ }: { cur: Metrics | null | undefined; ref_: Metrics | null | undefined }) {
  const rows: [string, keyof Metrics, (x: number | null | undefined) => string][] = [
    ["IC", "ic", (x) => fix(x)], ["RankIC", "rank_ic", (x) => fix(x)],
    ["扣成本年化超额", "annual_return_with_cost", (x) => pct(x)], ["扣成本前年化超额", "annual_return_without_cost", (x) => pct(x)],
    ["最大回撤（扣成本）", "max_drawdown_with_cost", (x) => pct(x)], ["信息比率（扣成本）", "information_ratio_with_cost", (x) => fix(x, 2)],
  ];
  return (
    <table className="w-full text-sm">
      <thead className="text-xs text-muted"><tr><th className="text-left py-1">指标</th><th className="text-right">本轮</th><th className="text-right">当时最好</th></tr></thead>
      <tbody>
        {rows.map(([n, k, f]) => (
          <tr key={k} className="border-t border-line">
            <td className="py-1.5 text-muted">{n}</td><td className="num text-right">{f(cur?.[k])}</td><td className="num text-right text-faint">{f(ref_?.[k])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RoundDetail({ run, round }: { run: Run; round?: Round }) {
  const totalTime = useMemo(() => round ? Object.values(round.steps).reduce((a, b) => a + b, 0) : 0, [round]);
  if (!round) {
    return (
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-muted">RD-Agent 的起点：Alpha158 中的 20 个基础因子（ALPHA20），由 LightGBM 训练后在测试期回测。后续每一轮都在此基础上加入 AI 的新因子。</p>
        <Compare cur={run.baseline?.metrics} ref_={null} />
        <p className="text-xs text-faint">起点的回测由 RD-Agent 缓存，没有保留逐日结果，因此净值图中不显示起点曲线。</p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4 text-sm max-h-[560px] overflow-y-auto pr-1">
      <div className="flex flex-wrap gap-2">
        {round.decision ? <Badge tone="good">被采纳</Badge> : <Badge tone="bad">未采纳</Badge>}
        <Badge>编码尝试 {round.coding_attempts} 次</Badge>
        <Badge>费用 ${round.cost_usd.toFixed(3)}</Badge>
        <Badge>耗时 {Math.round(totalTime)} 秒</Badge>
      </div>
      <section>
        <div className="label mb-1">假设（AI 原文）</div>
        <p className="leading-relaxed">{round.hypothesis}</p>
        <p className="text-muted text-xs leading-relaxed mt-1">{round.reason}</p>
      </section>
      <section>
        <div className="label mb-1">因子（{round.factors.length}）</div>
        <ul className="flex flex-col gap-2">
          {round.factors.map((f) => (
            <li key={f.name} className="border border-line rounded p-2">
              <div className="num font-medium text-accent">{f.name}</div>
              <div className="text-xs text-muted mt-0.5">{f.description}</div>
              <div className="num text-[11px] text-faint mt-1 break-all">{f.formulation}</div>
              {f.code && (
                <details className="mt-1">
                  <summary className="text-xs text-muted cursor-pointer">查看代码</summary>
                  <pre className="num text-[11px] mt-1 p-2 bg-bg rounded overflow-x-auto max-h-64">{f.code}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </section>
      <section>
        <div className="label mb-1">Qlib 回测成绩</div>
        <Compare cur={round.metrics} ref_={round.sota_before} />
      </section>
      {round.feedback && (
        <section>
          <div className="label mb-1">AI 评审（原文）</div>
          <p className="text-muted leading-relaxed">{round.feedback.observations}</p>
          <div className="label mt-2 mb-1">下一轮方向</div>
          <p className="text-muted leading-relaxed">{round.feedback.new_hypothesis}</p>
        </section>
      )}
    </div>
  );
}
