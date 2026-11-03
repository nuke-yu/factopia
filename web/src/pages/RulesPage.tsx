import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, ErrorBox, Loading, Notice, Panel, Seg } from "../components/ui";
import PrecheckView from "../components/PrecheckView";
import { api, type Market, type RulePack, type RulePacks, type VersionRules } from "../api";
import { MARKET_LABEL, useAsync } from "../lib";
import { t, tf, joinList } from "../i18n";

// 风控台（Spec 第 14.12 节；BR-19、20、21、39）：地区规则包、数据来源登记表、每个研究版本的两次预检

type Tab = "check" | "packs" | "sources";
const SEV_TONE = { block: "bad", warn: "warn", info: "muted" } as const;
const STATUS_TONE: Record<string, "good" | "warn" | "bad" | "muted"> = { approved: "good", limited: "warn", forbidden: "bad" };

// 合成违规演示：同一市场里加入一只受限证券（正权重）与一笔卖空；有英国时再加入一只烟草股，验证规则能否拦截
const DEMO: Record<Market, { restricted: string; short: string }> = {
  hk: { restricted: "0700.HK", short: "0005.HK" }, uk: { restricted: "HSBA.L", short: "SHEL.L" }, fr: { restricted: "OR.PA", short: "MC.PA" },
};
function demoOverrides(markets: Market[]) {
  const m = markets[0];
  const date = "2023-06-01";
  const positions: Record<string, { date: string; symbol: string; weight: number }[]> = {
    [m]: [{ date, symbol: DEMO[m].restricted, weight: 0.04 }, { date, symbol: DEMO[m].short, weight: -0.03 }],
  };
  if (markets.includes("uk")) positions.uk = [...(positions.uk ?? []), { date, symbol: "BATS.L", weight: 0.03 }];
  return { mandate: { restricted_securities: [DEMO[m].restricted], free_text: ["避免烟草与争议性武器行业"] }, positions };
}

function CheckTab({ packs }: { packs: RulePacks }) {
  const [params, setParams] = useSearchParams();
  const projects = useAsync(() => api.projects(), []);
  const list = (projects.data ?? []).filter((p) => !p.example && p.versions.length > 0);
  const id = params.get("id") ?? list[0]?.id ?? "";
  const proj = list.find((p) => p.id === id);
  const version = Number(params.get("v") ?? proj?.versions[proj.versions.length - 1] ?? 0);
  const [demo, setDemo] = useState(false);
  const [res, setRes] = useState<{ data?: VersionRules; error?: string; loading: boolean }>({ loading: false });

  useEffect(() => {
    if (!id || !version) return;
    let alive = true;
    setRes({ loading: true });
    const spec = proj ? { markets: proj.markets } : null;
    api.versionRules(id, version, demo && spec ? demoOverrides(spec.markets) : undefined)
      .then((data) => alive && setRes({ data, loading: false }), (e) => alive && setRes({ error: (e as Error).message, loading: false }));
    return () => { alive = false; };
  }, [id, version, demo]); // eslint-disable-line react-hooks/exhaustive-deps

  if (projects.loading) return <Loading what={t("研究项目")} />;
  if (projects.error) return <ErrorBox error={projects.error} />;
  if (!list.length) return <Notice tone="accent" title={t("还没有锁定的研究版本")}>{t("在“研究立项”起草并锁定后，这里显示两次预检。")}</Notice>;
  const d = res.data;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm flex items-center gap-2 whitespace-nowrap">{t("研究项目")}
          <select id="rules-project" className="field w-auto" value={id} onChange={(e) => { setDemo(false); setParams({ id: e.target.value }); }}>
            {list.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
        </label>
        <label className="text-sm flex items-center gap-2 whitespace-nowrap">{t("版本")}
          <select id="rules-version" className="field w-auto" value={version} onChange={(e) => { setDemo(false); setParams({ id, v: e.target.value }); }}>
            {(proj?.versions ?? []).map((v) => <option key={v} value={v}>v{v}</option>)}
          </select>
        </label>
        <span className="text-xs text-muted">{proj ? joinList(proj.markets.map((m) => MARKET_LABEL[m])) : ""} · {tf("{n} 个规则包", { n: packs.packs.length })}</span>
        <label className="ml-auto text-sm flex items-center gap-2" title={t("不写入任何文件，只用于验证规则能否拦截")}>
          <input id="rules-demo" type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />
          {t("合成违规演示")}
        </label>
      </div>
      {demo && proj && (
        <Notice tone="accent" title={t("合成违规演示（不写入任何文件）")}>
          {tf("授权加入受限证券 {r}，自由文本加“避免烟草与争议性武器行业”；在{m}组合里追加该证券 4% 持仓与 {s} −3% 卖空{uk}（2023-06-01）。", {
            r: DEMO[proj.markets[0]].restricted, m: MARKET_LABEL[proj.markets[0]], s: DEMO[proj.markets[0]].short,
            uk: proj.markets.includes("uk") ? t("，英国组合追加 BATS.L（英美烟草）3%") : "",
          })}
        </Notice>
      )}
      {res.loading && <Loading what={t("预检")} />}
      {res.error && <ErrorBox error={res.error} />}
      {d && !demo && <Notice tone="accent" title={t("规则命中的处理")}>
        {d.handling ? tf("本页显示最新一次审批提交（{s}，{d} UTC）中每条命中的处理状态{e}。确认与例外在审批台由负责角色操作，记入该提交的留痕。", {
          s: t(d.handling.status_zh), d: d.handling.created_at.slice(0, 16).replace("T", " "),
          e: d.handling.earlier_submissions > 0 ? tf("；此前另有 {n} 次提交", { n: d.handling.earlier_submissions }) : "",
        }) : t("该版本尚未提交审批。提交后，警告命中由所列审批人在审批台逐条确认或例外处理，处理状态显示在这里。")}
      </Notice>}
      {d && (
        <div className="grid gap-5 xl:grid-cols-2">
          <Panel eyebrow={tf("立项锁定前 · {d} UTC", { d: d.setup.checked_at.slice(0, 16).replace("T", " ") })} title={t("立项预检")}>
            <PrecheckView pre={d.setup} handling={d.handling ?? null} highlight={params.get("hit")} />
          </Panel>
          <Panel eyebrow={t("组合生成后 · 各市场采纳轮次的持仓与诊断")} title={t("组合预检")}>
            {d.portfolio ? (
              <>
                <div className="text-xs text-faint num mb-2">
                  {Object.entries(d.focus).map(([m, f]) => `${MARKET_LABEL[m as Market]} ${f.run_id ?? t("未运行")}${f.loop != null ? ` ${tf("第 {n} 轮", { n: f.loop })}` : ""}`).join(" · ")}
                </div>
                <PrecheckView pre={d.portfolio} handling={d.handling ?? null} highlight={params.get("hit")} />
              </>
            ) : <p className="text-sm text-muted">{d.portfolio_note}</p>}
          </Panel>
        </div>
      )}
    </div>
  );
}

function Costs({ p }: { p: RulePack }) {
  if (!p.costs) return null;
  const f = (x: number) => `${(x * 100).toFixed(2)}%`;
  return (
    <div className="text-xs text-muted num">
      {tf("单边成本：买入 {b}（税 {bt}）· 卖出 {s}（税 {st}）", {
        b: f(p.costs.buy.tax + p.costs.buy.commission), bt: f(p.costs.buy.tax),
        s: f(p.costs.sell.tax + p.costs.sell.commission), st: f(p.costs.sell.tax),
      })}
      <span className="text-faint">{t(" · 回测与诊断从这里读取")}</span>
    </div>
  );
}

function PacksTab({ packs }: { packs: RulePacks }) {
  return (
    <div className="flex flex-col gap-5">
      {packs.packs.map((p) => (
        <Panel key={p.pack} eyebrow={tf("{f} · {v} · 生效 {e} · 复核 {r}", { f: p.file, v: p.version, e: p.effective_date, r: p.last_review })}
          title={tf("{m}规则包", { m: p.market === "all" ? t("通用") : MARKET_LABEL[p.market as Market] })}
          actions={<Badge tone="warn">{t(p.status)}</Badge>}>
          <div className="flex flex-wrap gap-x-4 gap-y-1 mb-2 text-xs text-muted"><span>{tf("负责人：{o}", { o: t(p.owner) })}</span><Costs p={p} /></div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="label text-left">
                <tr><th className="py-1 pr-3">{t("编号")}</th><th className="pr-3">{t("程度")}</th><th className="pr-3">{t("检查时点")}</th><th className="pr-3">{t("规则")}</th>
                  <th className="pr-3">{t("所需审批")}</th><th>{t("依据")}</th></tr>
              </thead>
              <tbody>
                {p.rules.map((r) => (
                  <tr key={r.id} className="border-t border-line align-top">
                    <td className="py-2 pr-3 num text-xs whitespace-nowrap">{r.id}</td>
                    <td className="pr-3"><Badge tone={SEV_TONE[r.severity]}>{t(packs.severity_zh[r.severity])}</Badge></td>
                    <td className="pr-3 text-xs text-muted whitespace-nowrap">{joinList(r.stages.map((s) => t(packs.stage_zh[s])))}</td>
                    <td className="pr-3"><div>{r.title}</div><div className="text-xs text-faint mt-0.5">{r.impact}</div></td>
                    <td className="pr-3 text-xs whitespace-nowrap">{joinList(r.approvers.map((x) => t(x))) || "—"}</td>
                    <td className="text-xs text-muted max-w-[36ch]">{r.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ))}
    </div>
  );
}

function SourcesTab({ packs }: { packs: RulePacks }) {
  const reg = packs.sources;
  return (
    <div className="flex flex-col gap-5">
      <Panel eyebrow={tf("{v} · {o} · 复核 {r}", { v: reg.version, o: t(reg.owner), r: reg.last_review })} title={t("数据来源登记表")}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">{t("来源")}</th><th className="pr-3">{t("状态")}</th><th className="pr-3">{t("许可")}</th><th>{t("说明")}</th></tr></thead>
            <tbody>
              {Object.entries(reg.sources).map(([k, s]) => (
                <tr key={k} className="border-t border-line align-top">
                  <td className="py-2 pr-3"><div className="font-medium">{t(s.name)}</div><div className="num text-xs text-faint">{k}</div></td>
                  <td className="pr-3"><Badge tone={STATUS_TONE[s.status] ?? "muted"}>{t(packs.status_zh[s.status] ?? s.status)}</Badge></td>
                  <td className="pr-3 text-xs text-muted max-w-[40ch]">{t(s.licence)}</td>
                  <td className="text-xs text-faint max-w-[48ch]">{s.note ? t(s.note) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel eyebrow={t("研究规格的数据需求 → 各市场实际使用的来源")} title={t("数据类别与来源")}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="label text-left"><tr><th className="py-1 pr-3">{t("数据类别")}</th><th className="pr-3">{MARKET_LABEL.hk}</th><th className="pr-3">{MARKET_LABEL.uk}</th><th>{MARKET_LABEL.fr}</th></tr></thead>
            <tbody>
              {Object.entries(reg.category_sources).map(([c, ms]) => (
                <tr key={c} className="border-t border-line">
                  <td className="py-2 pr-3">{t(packs.category_zh[c] ?? c)}</td>
                  {(["hk", "uk", "fr"] as Market[]).map((m) => <td key={m} className="pr-3 text-xs text-muted">{ms[m] ? t(reg.sources[ms[m]!]?.name ?? ms[m]) : t("未接入")}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-faint mt-2">{tf("每个研究都会用到：{l}。未列出的类别（如财务、另类数据）尚未接入。", { l: joinList(reg.always.map((k) => t(reg.sources[k]?.name ?? k))) })}</p>
      </Panel>
    </div>
  );
}

export default function RulesPage() {
  const { data, error, loading } = useAsync(() => api.rulePacks(), []);
  const [tab, setTab] = useState<Tab>("check");
  if (loading) return <Loading what={t("风控台")} />;
  if (error || !data) return <ErrorBox error={error ?? t("无数据")} />;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <div className="label">{t("风控台")}</div>
          <h1 className="font-display text-xl font-semibold">{t("地区规则包与预检")}</h1>
        </div>
        <span className="text-sm text-muted">{t("立项锁定前 · 组合生成后 · 发布前")}</span>
        <span className="ml-auto text-xs text-faint max-w-[60ch] text-right">{data.disclaimer}</span>
      </div>
      <Seg label={t("视图")} value={tab} onChange={setTab} options={[
        { value: "check", label: t("研究预检") }, { value: "packs", label: t("规则包") }, { value: "sources", label: t("数据来源") },
      ]} />
      {tab === "check" && <CheckTab packs={data} />}
      {tab === "packs" && <PacksTab packs={data} />}
      {tab === "sources" && <SourcesTab packs={data} />}
    </div>
  );
}
