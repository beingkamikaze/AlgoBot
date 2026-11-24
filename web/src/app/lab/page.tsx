"use client";

import type { Session } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { useFeedback } from "@/components/ui/feedback";
import { IconCheck, IconFlask, IconPlus, IconRefresh, IconSparkle } from "@/components/ui/icons";
import { Badge, Check, Empty, Notice, Page, PageHeader, PageLoading, Panel, RValue, SignInRequired, SkeletonRows, StateBadge, TableWrap } from "@/components/ui/kit";
import { api, type LabData, type LabProposal, type StrategyInfo } from "@/lib/api";
import { supabase } from "@/lib/supabase";

function PctValue({ v }: { v: number | null | undefined }) {
  if (v == null) return <span className="num text-subtle">–</span>;
  const x = Math.round(v * 100) / 100;
  return <span className={`num ${x > 0 ? "text-up" : x < 0 ? "text-down" : "text-muted"}`}>{x > 0 ? "+" : x < 0 ? "−" : ""}{Math.abs(x).toFixed(2)}%</span>;
}

function Score({ v, pct }: { v: number | null | undefined; pct: boolean }) {
  return pct ? <PctValue v={v} /> : <RValue v={v} />;
}

function TestResult({ p }: { p: LabProposal }) {
  const t = p.test;
  if (!t) return null;
  if (t.status === "running") return (
    <div className="flex items-center gap-2 rounded-md border border-info/30 bg-info/[0.06] px-3 py-2 text-xs text-info">
      <IconRefresh size={13} className="animate-spin" />Backtesting the original and the change on {t.universe}… (a minute or two)
    </div>
  );
  if (t.error) return <Notice tone="error">Test failed: {t.error}</Notice>;
  if (!t.candidate || !t.baseline) return null;
  const pct = t.unit === "%";
  return (
    <div className="space-y-2 rounded-md border border-line bg-canvas/50">
      <table className="dtable dtable-compact">
        <thead><tr><th></th><th className="r">First 70% of days</th><th className="r">Held back {t.held_back_from}+ <span className="text-accent">(decides)</span></th><th className="r">Held-back trades</th></tr></thead>
        <tbody>
          <tr><td className="text-muted">{pct ? "Holding the universe" : "Original"}</td><td className="r"><Score v={t.baseline.in_sample} pct={pct} /></td><td className="r"><Score v={t.baseline.held_back} pct={pct} /></td><td className="r text-muted">{t.baseline.held_back_trades}</td></tr>
          <tr><td className="text-muted">{pct ? "Swing strategy" : "With the change"}</td><td className="r"><Score v={t.candidate.in_sample} pct={pct} /></td><td className="r font-semibold"><Score v={t.candidate.held_back} pct={pct} /></td><td className="r text-muted">{t.candidate.held_back_trades}</td></tr>
        </tbody>
      </table>
      <div className="space-y-1 px-3 pb-2.5 text-xs">
        <div className="flex items-center gap-3">
          <span className="text-muted">{pct ? "Better than holding the universe in" : "Better than the original in"}</span>
          <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-raised">
            <div className={`h-full rounded-full ${(t.bootstrap_share_better ?? 0) >= (t.bootstrap_needed ?? 1) ? "bg-up" : "bg-warn"}`} style={{ width: `${(t.bootstrap_share_better ?? 0) * 100}%` }} />
            <span className="absolute inset-y-0 w-px bg-fg" style={{ left: `${(t.bootstrap_needed ?? 0) * 100}%` }} title="needed" />
          </div>
          <span className="num">{((t.bootstrap_share_better ?? 0) * 100).toFixed(0)}% <span className="text-subtle">/ {((t.bootstrap_needed ?? 0) * 100).toFixed(1)}% needed</span></span>
        </div>
        {pct && <p className="text-2xs text-subtle">Swing test: average % per trade after delivery charges, against {t.baseline_is ?? "holding the universe"}.</p>}
        <p className="text-2xs text-subtle">Resamples of the held-back trades; the bar rises with every test this month (this is test {t.tests_this_month}).</p>
        {t.passed ? <p className="flex items-center gap-1.5 font-medium text-up"><IconCheck size={13} />Passed every check.</p> :
          <ul className="space-y-0.5">{t.reasons?.map((x) => <Check key={x} ok={false}><span className="text-down">{x}</span></Check>)}</ul>}
        {t.warnings?.map((w) => <p key={w} className="text-warn">Note: {w}</p>)}
      </div>
    </div>
  );
}

export default function LabPage() {
  const sb = supabase();
  const { toast, confirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<LabData | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [form, setForm] = useState<{ hypothesis: string; strategy: string; params: Record<string, number | boolean> }>({ hypothesis: "", strategy: "orb", params: {} });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    sb?.auth.getSession().then(({ data }) => setSession(data.session));
    api<StrategyInfo[]>("/strategies").then((s) => {
      setStrategies(s);
      setForm((f) => ({ ...f, params: s.find((x) => x.id === f.strategy)?.defaults ?? {} }));
    }).catch(() => {});
  }, [sb]);

  useEffect(() => {
    if (!session) return;
    const load = () => api<LabData>("/lab").then(setData).catch((e: Error) => setError(e.message));
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [session]);

  async function run(label: string, path: string, body?: unknown) {
    setBusy(label); setError(null);
    try { await api(path, { method: "POST", body: body ?? {} }); setData(await api<LabData>("/lab")); }
    catch (e) { setError((e as Error).message); toast("error", "Lab action failed", (e as Error).message); } finally { setBusy(null); }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="The lab" />;

  const strat = strategies.find((s) => s.id === form.strategy);
  const changed = Object.fromEntries(Object.entries(form.params).filter(([k, v]) => strat && strat.defaults[k] !== v));

  return (
    <Page wide>
      <PageHeader title="Lab" subtitle="Ideas become tested strategy versions. A change is judged only on days it was not found in, and nothing reaches your signals without your approval."
        meta={data && <Badge tone="neutral">{data.tests_this_month} test{data.tests_this_month === 1 ? "" : "s"} this month</Badge>} />
      {data && (
        <Notice tone="info" title="How a change passes">
          At least <span className="num text-fg">{data.rules.min_held_back_trades}</span> trades on the held-back last 30% of days; a positive average after costs there;
          at least <span className="num text-fg">+{data.rules.min_improvement_r}R</span> better than the original there; and better in enough resamples
          (<span className="num text-fg">{((1 - data.rules.base_alpha) * 100).toFixed(0)}%</span> for one test, stricter with every test this month).
          An approved version runs on paper; after <span className="num text-fg">{data.rules.paper_trades_to_judge}</span> paper trades a losing one is retired.
        </Notice>
      )}
      {error && <Notice tone="error">{error}</Notice>}

      <div className="grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-8">
          <Panel title="New hypothesis">
            <div className="space-y-3">
              <label className="block"><span className="field-label">Hypothesis</span>
                <input value={form.hypothesis} onChange={(e) => setForm({ ...form, hypothesis: e.target.value })} placeholder="e.g. ORB breakouts from wider opening ranges work better" className="field" /></label>
              <label className="block max-w-sm"><span className="field-label">Strategy</span>
                <select value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value, params: strategies.find((s) => s.id === e.target.value)?.defaults ?? {} })} className="field">
                  {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select></label>
              <div>
                <p className="field-label">Change one or more parameters <span className="text-subtle">· changed ones are highlighted</span></p>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                  {Object.entries(form.params).map(([k, v]) => (
                    <label key={k} title={strat?.param_help[k]} className={`flex items-center justify-between gap-2 rounded-md border px-2 py-1.5 ${k in changed ? "border-accent/50 bg-accent/[0.06]" : "border-line"}`}>
                      <span className={`num min-w-0 text-xs break-all ${k in changed ? "text-accent" : "text-muted"}`}>{k}</span>
                      {typeof v === "boolean"
                        ? <input type="checkbox" checked={v} onChange={(e) => setForm({ ...form, params: { ...form.params, [k]: e.target.checked } })} />
                        : <input type="number" step="any" value={v} onChange={(e) => setForm({ ...form, params: { ...form.params, [k]: Number(e.target.value) } })}
                            className="field field-num h-7 w-20" />}
                    </label>
                  ))}
                </div>
              </div>
              <button disabled={busy !== null || form.hypothesis.length < 5 || !Object.keys(changed).length}
                onClick={() => run("create", "/lab/proposals", { hypothesis: form.hypothesis, strategy_id: form.strategy, params: changed })}
                className="btn btn-primary"><IconPlus size={14} />Add hypothesis</button>
            </div>
          </Panel>

          <section className="flex flex-col gap-3">
            <h2 className="text-[14px] font-semibold">Hypotheses</h2>
            {!data && <div className="panel"><SkeletonRows rows={3} cols={2} /></div>}
            {data && data.proposals.length === 0 && <div className="panel"><Empty title="No hypotheses yet" icon={<IconFlask size={16} />}>Add one above. Weekly reviews add some once you have 20+ trades a week.</Empty></div>}
            {data?.proposals.map((p) => (
              <article key={p.id} className="panel space-y-2.5 p-3 text-[12.5px]">
                <header className="flex flex-wrap items-center gap-2">
                  <StateBadge state={p.status} />
                  <span className="font-medium text-fg">{p.hypothesis}</span>
                  <span className="ml-auto text-2xs text-subtle">{p.change.origin ?? "review"} · {new Date(p.created_at).toLocaleDateString("en-IN")}</span>
                </header>
                {p.change.params
                  ? <p className="text-xs text-muted">Change: <span className="num text-fg">{p.change.strategy}</span>{" "}{Object.entries(p.change.params).map(([k, v]) => <span key={k} className="num mr-1 rounded-sm bg-raised px-1.5 py-0.5 text-accent">{k} = {String(v)}</span>)}{p.change.translation && <span className="mt-1 block text-subtle">{p.change.translation}</span>}</p>
                  : <p className="text-xs text-muted">Suggested: {p.change.change} (not yet a concrete parameter change)</p>}
                {p.evidence?.not_testable && <p className="text-xs text-muted">Not testable: {p.evidence.not_testable}</p>}
                <TestResult p={p} />
                <div className="flex flex-wrap gap-2">
                  {!p.change.params && p.status === "new" && <button disabled={busy !== null} onClick={() => run(`t-${p.id}`, `/lab/proposals/${p.id}/translate`)} className="btn btn-sm"><IconSparkle size={13} />{busy === `t-${p.id}` ? "Translating…" : "Make it testable (GPT-5)"}</button>}
                  {p.change.params && ["new", "failed"].includes(p.status) && <button disabled={busy !== null} onClick={() => run(`x-${p.id}`, `/lab/proposals/${p.id}/test`)} className="btn btn-sm">{p.test ? "Test again" : "Test it"}</button>}
                  {p.status === "passed" && <button disabled={busy !== null} onClick={async () => {
                    if (await confirm({ title: "Approve this version for paper trading?", body: "It becomes a strategy version you can pick on Signals and Backtests. It is retired automatically if it loses after 20 paper trades.", confirm: "Approve", tone: "primary" }))
                      run(`a-${p.id}`, `/lab/proposals/${p.id}/approve`);
                  }} className="btn btn-primary btn-sm">Approve: paper-trade this version</button>}
                  {!["approved", "rejected"].includes(p.status) && <button disabled={busy !== null} onClick={() => run(`r-${p.id}`, `/lab/proposals/${p.id}/reject`)} className="btn btn-ghost btn-sm">Reject</button>}
                </div>
              </article>
            ))}
          </section>
        </div>

        <Panel className="self-start xl:col-span-4" title="Strategy versions" flush
          actions={<button disabled={busy !== null} onClick={() => run("rv", "/lab/variants/review")} className="btn btn-ghost btn-sm">Check paper records</button>}>
          {!data ? <SkeletonRows rows={5} cols={3} /> : (
            <TableWrap>
              <table className="dtable dtable-compact">
                <thead><tr><th>Version</th><th>Status</th><th className="r">Paper</th><th className="r">Avg</th></tr></thead>
                <tbody>
                  {data.variants.map((v) => (
                    <tr key={v.id} title={v.note ?? (v.user_id ? "" : "default parameters")}>
                      <td className="max-w-48"><p className="truncate font-medium">{v.strategy_id}</p><p className="num truncate text-2xs text-subtle">v{v.version}</p></td>
                      <td><StateBadge state={v.status} /></td>
                      <td className="r text-muted">{v.paper_trades}</td><td className="r"><RValue v={v.paper_average_r} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </Panel>
      </div>
    </Page>
  );
}
