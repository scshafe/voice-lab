import React from "react";

// ============================================================================
// JourneyEducationCard — the tutorial layer under each subject tab. Static
// content, one entry per subject: the physics, music theory, and statistics
// behind that tab's chart, written against the actual encodings on screen
// (axes, bands, thresholds), not generic theory. Numbers quoted here that
// describe THIS archive (e.g. the 200/300 Hz register strips) are the values
// the charts actually use.
// ============================================================================

const EDUCATION = {
  verdict: {
    title: "Understanding the Verdict",
    intro: "Why this table is the strictest lens in the archive, and what “flat” does and doesn’t mean.",
    body: (
      <>
        <h3>Why compare only within the same song</h3>
        <p>
          Year-over-year averages mix two different stories: how you sing, and <em>what</em> you sing. Pick harder
          repertoire and your average pitch, sustain, and clarity all move — with zero change in skill. Comparing takes
          of the <em>same recurring song</em> across years removes repertoire as a variable, the way a paired experiment
          compares each subject against themselves. Whatever survives this control is execution drift, not song choice.
        </p>
        <h3>The statistics</h3>
        <p>
          Each song’s trend is a <strong>Theil–Sen slope</strong>: the median of all pairwise slopes between its takes.
          Because it takes a median of all pairwise slopes rather than a least-squares fit, one bad recording (a distant mic, a sick day) can’t drag
          the line the way it would in ordinary regression. The per-dimension verdict then <strong>bootstraps</strong> across
          songs: resample the song list with replacement thousands of times, recompute the median slope each time, and keep
          the middle 95% as the confidence interval. If that interval straddles zero, the honest call is
          <strong> flat</strong> — the data can’t distinguish the trend from no trend.
        </p>
        <h3>How to read a row</h3>
        <p>
          The slope is in the metric’s own units per year (cents/yr for tuning, dB/yr for CPPS). “Songs” counts the
          recurring clusters with enough history to qualify. A flat verdict with a tight interval is a strong statement:
          plenty of evidence, no drift. A flat verdict with a wide interval mostly means “not enough takes to know.”
        </p>
      </>
    )
  },
  range: {
    title: "Understanding pitch & range",
    intro: "The physics that makes the axis logarithmic, and the music theory behind the note names.",
    body: (
      <>
        <h3>The physics of pitch</h3>
        <p>
          Pitch is the rate your vocal folds open and close — the <strong>fundamental frequency (f0)</strong>, measured in
          hertz. Perception is logarithmic: one octave is a doubling of frequency, so the step from A3 (220 Hz) to A4
          (440 Hz) sounds the same size as A4 to A5 (880 Hz), though one spans 220 Hz and the other 440 Hz. That’s why
          this chart’s frequency axis is log-scaled — equal vertical distances are equal musical intervals. A
          <strong> semitone</strong> is one twelfth of an octave (a frequency ratio of 2<sup>1/12</sup> ≈ 1.059), and a
          <strong> cent</strong> is a hundredth of a semitone — the units the tuning panels use.
        </p>
        <h3>The music theory</h3>
        <p>
          Note names use scientific pitch notation: C4 is middle C (≈262 Hz), A4 is concert pitch (440 Hz). The
          headline’s <strong>working top</strong> is the 95th-percentile note per quarter — the ceiling of where you
          actually live (your <em>tessitura</em>), not the single highest squeak ever caught. That distinction is why the
          C4 → G4 gain is meaningful: it’s reach you use, not reach you touched once.
        </p>
        <h3>Reading the river</h3>
        <p>
          The wide envelope is p05–p95 — 90% of your sung time lives inside it; the darker band is the p25–p75 typical
          zone; the line is the median. Percentiles resist outliers, so a stray whistle or rumble can’t fake a range
          change. The register strip’s thresholds (high ≥300 Hz, low &lt;200 Hz) are calibration points for <em>this</em> voice’s
          chest–head transition zone, not universal constants — they make the strip comparable across years, which is
          what a longitudinal chart needs.
        </p>
      </>
    )
  },
  practice: {
    title: "Understanding practice & payoff",
    intro: "What a shared time axis can honestly argue, and the breath physics behind sustain.",
    body: (
      <>
        <h3>What the alignment argues</h3>
        <p>
          The shaded eras aren’t hand-picked: they’re derived from the cadence series itself (sustained high-volume
          months), which protects the chart from cherry-picking. When sustain and clarity rise <em>inside</em> those bands
          and sag after them, the alignment is doing the arguing. Correlation is not causation — season, repertoire, and
          recording habits all shift together — but repeated alignment across independent eras is how observational
          evidence gets strong.
        </p>
        <h3>The physics of sustain</h3>
        <p>
          A long sustained note is a pressure-management feat: steady <em>subglottal pressure</em> from the lungs against
          vocal folds that close efficiently on each cycle. Leaky closure wastes air as breathiness and shortens the note;
          efficient closure spends less air per cycle and buys seconds. CPPS (the clarity line) tends to rise with the
          same consistent fold closure, which is why the two panels move together when practice is dense.
        </p>
        <h3>The practice science</h3>
        <p>
          Bursty practice — months of daily takes, then quiet — is visible here as era bands with plateaus after them.
          Skills consolidated during a burst (the 2021 register gain) can persist through quiet years, while
          fine-motor polish (vibrato regularity, tuning) tends to decay faster without maintenance. The chart is your own
          evidence for which kind each skill is.
        </p>
      </>
    )
  },
  vibrato: {
    title: "Understanding vibrato",
    intro: "The oscillator behind the ornament: what rate and extent measure, and what “settled” means.",
    body: (
      <>
        <h3>The physics</h3>
        <p>
          Vibrato is a periodic wobble in f0 produced by reflex oscillation of the laryngeal muscles — a
          <strong> rate</strong> (how many wobbles per second, in Hz) and an <strong>extent</strong> (how far each swings
          around the center, in cents). In trained classical voices the reflex settles between 5 and 7 Hz with extents
          around ±50–100 cents. Slower than ~4 Hz reads as a wobble; faster than ~7–8 Hz reads as a tremolo or bleat.
          The blue 5–6 Hz zone on the rate panel marks that settled classical band — the trainable gap between your
          ~4 Hz default and the classical target.
        </p>
        <h3>The music theory</h3>
        <p>
          Vibrato is also a stylistic dial, not just a reflex. Classical singing keeps it nearly always on; pop and folk
          use it <em>selectively</em> — straight-tone onsets that bloom into vibrato late in a sustain. That selectivity is
          what the third panel (pervasiveness: the share of sustained time carrying vibrato) measures. Your arc — wide and
          pervasive early, narrower and more selective after 2021 — is a move toward deliberate deployment, which is a
          control story even though the rate never classicalized.
        </p>
        <h3>Reading the fingerprint</h3>
        <p>
          Each dot is one take: extent across, rate up, brighter dots more recent, hollow dots flagged low-reliability.
          A cloud that migrates over the years is your default setting changing; outliers are performances worth clicking
          — the inspector opens the take’s actual pitch contour so you can see the wobble itself.
        </p>
      </>
    )
  },
  trust: {
    title: "Understanding the measurements",
    intro: "Why two “voice quality” lines disagree on purpose — a short course in measurement validity.",
    body: (
      <>
        <h3>The signal physics</h3>
        <p>
          <strong>HNR</strong> (harmonics-to-noise ratio) compares the energy in your tone’s periodic part against
          everything aperiodic. That “everything” is the problem: room reverb, mic distance, and noise floor all pour
          into the denominator, so HNR faithfully tracks seven years of changing phones and rooms.
          <strong> CPPS</strong> — smoothed cepstral peak prominence — asks a narrower question: how sharply does the
          signal’s periodicity spike stand out above its own spectral baseline? Because it’s self-normalizing, it largely
          shrugs off the recording chain, which is why it’s the standard clinical voice-quality measure. Higher CPPS =
          clearer, more efficient phonation.
        </p>
        <h3>The statistics of confounds</h3>
        <p>
          A metric that declines monotonically across seven years, in lockstep with known gear changes and against the
          practice arc, carries the signature of <em>instrument drift</em>, not vocal decline. The general lesson: before
          trusting a trend, ask what else changed on the same timeline, and prefer measures designed to be invariant to it.
        </p>
        <h3>Tuning, done right</h3>
        <p>
          The retired every-frame tuning metric punished portamento and melisma — musical gestures, not errors — by
          scoring every 50 ms frame against the nearest note. The green panel scores only <strong>note cores</strong>:
          stable centers of held notes, slides excluded by construction. That’s the difference between measuring
          expression and measuring accuracy; ~17–18 cents median error on note cores is the honest number.
        </p>
      </>
    )
  },
  songs: {
    title: "Understanding the scorecards",
    intro: "Per-song trends, and why the eligibility floor exists.",
    body: (
      <>
        <h3>The statistics</h3>
        <p>
          Each row fits a Theil–Sen slope per dimension <em>within one song’s takes</em>. The floor — at least six
          reliable takes spanning at least a year and a half — exists because slopes computed on too few takes are mostly
          noise: two takes always draw a perfect line. Arrow direction is calibrated per metric: tuning error falling
          (▼) is good, clarity rising (▲) is good. The sparkline is per-year median note-core tuning error — down
          is better.
        </p>
        <h3>The music</h3>
        <p>
          Songs move independently because songs are different instruments: key, tessitura fit, and difficulty interact
          with your technique. One song’s tuning improving while another’s decays usually means the second sits where
          your voice changed (a raised working top shifts which notes are comfortable). Names are privacy-generic labels
          (“Song A”); click through to the Corpus table to hear the actual takes behind any line.
        </p>
      </>
    )
  },
  threads: {
    title: "Understanding the threads",
    intro: "Where the song clusters come from, and what a repertoire lifecycle looks like.",
    body: (
      <>
        <h3>How threads are found</h3>
        <p>
          Clusters come from the local speech-to-text pass: takes whose transcripts share enough repeated word patterns
          (n-gram overlap scored with Jaccard similarity) are threaded as the same song. The labels are deliberately
          generic and no lyric text ever leaves this machine — the manifests store cluster ids and counts only. Dot area
          is takes per year, so a thread’s thickness is how hard you drilled it that year.
        </p>
        <h3>The repertoire lifecycle</h3>
        <p>
          Threads make the adopt → drill → retire arc visible: most songs get a dense year or two, then fade. Long
          threads that resurface years later are the interesting ones — returning to old material is the cleanest natural
          experiment this archive has (it feeds the same-song Verdict), and a thread that returns thinner but with better
          scorecard numbers is consolidation you can point at. Click any thread to open its takes in the Corpus table.
        </p>
      </>
    )
  }
};

export function JourneyEducationCard({ subject }) {
  const content = EDUCATION[subject];
  if (!content) return null;
  return (
    <section className="vj-card vj-edu" data-subject={subject}>
      <h2>{content.title}</h2>
      <p className="vj-sub">{content.intro}</p>
      {content.body}
    </section>
  );
}
