import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { Button, InputField, SelectField } from "@scshafe/ui";
import { fetchCoachThunk, selectCoach } from "../../state/CoachManager.js";
import { abandonGoalThunk, createGoalThunk, fetchGoalsThunk, selectGoals } from "../../state/GoalsManager.js";
import { openCorpusWithQueryThunk } from "../../state/RowsManager.js";

const RULE_LABELS = {
  "range-stretch": "range stretch",
  "vibrato-settling": "vibrato settling",
  "sustain-pb": "sustain PB",
  "tuning-near": "nearly in tune",
  "clarity-thin": "evidence-thin"
};

function WhyChips({ why }) {
  return (
    <div className="vj-whychips">
      {(why ?? []).map((reason) => <span className="vj-whychip" key={reason}>{reason}</span>)}
    </div>
  );
}

function DueCard({ dueSongs, dueAfterDays, onOpenCluster }) {
  return (
    <section className="vj-card">
      <h2>Due for a take</h2>
      <p className="vj-sub">
        Spaced re-recording for the same-song engine: recurring songs whose <strong>next take buys the most evidence</strong> —
        stale threads, judged songs whose span a take today extends, songs one clean take from eligibility.
        Always framed as adding data, never as chasing a verdict.
      </p>
      {dueSongs.length ? dueSongs.map((song) => (
        <div className="vj-duerow" key={song.clusterId}>
          <span className="vj-songname" role="link" tabIndex={0} onClick={() => onOpenCluster(song.clusterId)}>
            {song.label}
            <small>{song.clusterId}</small>
          </span>
          <span className="vj-num">{song.takeCount} takes<small>{song.firstYear}–{song.lastYear}</small></span>
          <span className="vj-num">last sung<small>{song.daysSinceLast} d ago</small></span>
          <WhyChips why={song.why} />
        </div>
      )) : (
        <p className="vj-muted">Nothing due — every recurring song has a take inside the {dueAfterDays ?? 90}-day window. Go sing something new.</p>
      )}
    </section>
  );
}

function FrontierCard({ frontier, onOpenCluster }) {
  return (
    <section className="vj-card">
      <h2>The frontier</h2>
      <p className="vj-sub">
        One step away, measured: songs sitting just under a threshold, each flag carrying the numbers that say so.
        These are the takes where tonight's practice moves a chart.
      </p>
      {frontier.length ? frontier.map((entry) => (
        <div className="vj-frontrow" key={entry.clusterId}>
          <div className="vj-fronthead">
            <span className="vj-songname" role="link" tabIndex={0} onClick={() => onOpenCluster(entry.clusterId)}>
              {entry.label}
              <small>{entry.clusterId}</small>
            </span>
            {entry.flags.map((flag) => (
              <span className="vj-rulechip" key={flag.rule}>{RULE_LABELS[flag.rule] ?? flag.rule}</span>
            ))}
          </div>
          {entry.flags.map((flag) => (
            <p className="vj-frontdetail" key={`${flag.rule}-detail`}>{flag.detail}</p>
          ))}
        </div>
      )) : (
        <p className="vj-muted">No frontier flags right now — thresholds move as you do; check back after the next ingest.</p>
      )}
    </section>
  );
}

const GOAL_CHIP_CLASS = {
  active: "vj-chip-flat",
  achieved: "vj-chip-improving",
  missed: "vj-chip-declining",
  abandoned: "vj-chip-insufficient_data"
};

const EMPTY_GOAL_FORM = { metric: "", direction: "at_least", target: "", takesRequired: "10", clusterId: "", title: "" };

function GoalsCard() {
  const dispatch = useDispatch();
  const goals = useSelector(selectGoals);
  const [form, setForm] = React.useState(EMPTY_GOAL_FORM);

  React.useEffect(() => {
    dispatch(fetchGoalsThunk());
  }, [dispatch]);

  const data = goals.data ?? {};
  const entries = data.goals ?? [];
  const metrics = data.metrics ?? [];
  const clusterOptions = data.clusterOptions ?? [];
  const setField = (key) => (value) => setForm((current) => ({ ...current, [key]: value }));
  const canSubmit = form.metric && form.target !== "" && Number.isFinite(Number(form.target));
  const submit = () => {
    if (!canSubmit) return;
    dispatch(createGoalThunk({
      metric: form.metric,
      direction: form.direction,
      target: Number(form.target),
      takesRequired: Number(form.takesRequired) || 10,
      clusterId: form.clusterId || null,
      title: form.title.trim() || null
    }));
    setForm(EMPTY_GOAL_FORM);
  };

  return (
    <section className="vj-card">
      <h2>Goals — pre-registered experiments</h2>
      <p className="vj-sub">
        Declare the metric, target, and sample size <strong>before</strong> you sing; the first {""}
        N qualifying takes after creation decide the verdict — no optional stopping, no peeking.
        Textbook Part VII, running in production.
      </p>
      {goals.status === "failed" ? <div className="vj-notice">{goals.error}</div> : null}
      {entries.length ? entries.map((goal) => (
        <div className="vj-goalrow" key={goal.goalId}>
          <div className="vj-goalhead">
            <span className="vj-goaltitle">{goal.title}</span>
            <span className={`vj-chip ${GOAL_CHIP_CLASS[goal.derivedStatus] ?? "vj-chip-flat"}`}>{goal.derivedStatus}</span>
            {goal.clusterId ? <span className="vj-goalmeta">{goal.clusterId}</span> : <span className="vj-goalmeta">whole corpus</span>}
            {goal.derivedStatus === "active" ? (
              <Button size="mini" label="Abandon" onClick={() => dispatch(abandonGoalThunk({ goalId: goal.goalId }))} />
            ) : null}
          </div>
          <div className="vj-goalmeta">
            {goal.progress
              ? <>{goal.progress.takesCounted} of {goal.progress.takesRequired} takes · running median {goal.progress.runningMedian ?? "—"} vs target {goal.target} · registered {String(goal.createdAt).slice(0, 10)}</>
              : <>abandoned {String(goal.abandonedAt ?? "").slice(0, 10)}</>}
          </div>
        </div>
      )) : (
        <p className="vj-muted">No goals yet — declare one below and it starts counting from your next take.</p>
      )}
      <div className="vj-goalform">
        <SelectField
          id="goal-metric"
          field="metric"
          label="Metric"
          value={form.metric}
          options={[{ value: "", label: "Choose…" }, ...metrics.map((metric) => ({ value: metric.key, label: metric.label }))]}
          onChange={setField("metric")}
        />
        <SelectField
          id="goal-direction"
          field="direction"
          label="Direction"
          value={form.direction}
          options={[{ value: "at_least", label: "at least" }, { value: "at_most", label: "at most" }]}
          onChange={setField("direction")}
        />
        <InputField id="goal-target" label="Target" type="number" placeholder="5.5" value={form.target} onChange={setField("target")} />
        <InputField id="goal-takes" label="Takes (N)" type="number" placeholder="10" value={form.takesRequired} onChange={setField("takesRequired")} />
        <SelectField
          id="goal-cluster"
          field="clusterId"
          label="Scope"
          value={form.clusterId}
          options={[{ value: "", label: "Whole corpus" }, ...clusterOptions.map((cluster) => ({ value: cluster.clusterId, label: `${cluster.label} (${cluster.takeCount})` }))]}
          onChange={setField("clusterId")}
        />
        <InputField id="goal-title" label="Title (optional)" type="text" placeholder="settle vibrato on Song C" value={form.title} onChange={setField("title")} />
        <Button label="Pre-register" onClick={submit} disabled={!canSubmit} />
      </div>
    </section>
  );
}

export function PracticePageComponent() {
  const dispatch = useDispatch();
  const coach = useSelector(selectCoach);

  React.useEffect(() => {
    dispatch(fetchCoachThunk());
  }, [dispatch]);

  if (coach.status === "loading" || coach.status === "idle") {
    return <div className="vj-page vj-page-narrow"><p className="vj-empty">Consulting the coach…</p></div>;
  }
  if (coach.status === "failed") {
    return <div className="vj-page vj-page-narrow"><div className="vj-notice">{coach.error}</div></div>;
  }
  const data = coach.data ?? {};
  const onOpenCluster = (clusterId) => dispatch(openCorpusWithQueryThunk(clusterId));

  return (
    <div className="vj-page vj-page-narrow">
      <DueCard dueSongs={data.dueSongs ?? []} dueAfterDays={data.params?.dueAfterDays} onOpenCluster={onOpenCluster} />
      <FrontierCard frontier={data.frontier ?? []} onOpenCluster={onOpenCluster} />
      <GoalsCard />
    </div>
  );
}
