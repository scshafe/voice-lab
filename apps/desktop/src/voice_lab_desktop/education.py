"""The Journey screen's education cards (checklist J-21): one per subject tab.

@canonical education -- the static text of voice-journey's ``journeyEducation.jsx``
(``git show 7f62275:web/src/components/journey/journeyEducation.jsx``), ported word for
word: each subject's title and intro, then its sections (a heading and a paragraph). JSX's
whitespace rule was applied (a line break next to a tag disappears), ``<strong>`` became
``<b>`` and ``<em>`` became ``<i>``; ``<sup>`` and the ``&lt;`` entity stay. This is the
one text in the app shown as Qt's StyledText: it is static, written here, and never data
from the server, so the emphasis can stay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Education:
    """A subject's card: its title, its intro, and (heading, paragraph) sections."""

    title: str
    intro: str
    sections: tuple[tuple[str, str], ...]

    def as_qml(self) -> dict[str, object]:
        return {
            "title": self.title,
            "intro": self.intro,
            "sections": [{"heading": h, "text": t} for h, t in self.sections],
        }


EDUCATION: Final[dict[str, Education]] = {
    "verdict": Education(
        title="Understanding the Verdict",
        intro=(
            "Why this table is the strictest lens in the archive, and what “flat” "
            "does and doesn’t mean."
        ),
        sections=(
            (
                "Why compare only within the same song",
                (
                    "Year-over-year averages mix two different stories: how you sing, and "
                    "<i>what</i> you sing. Pick harder repertoire and your average pitch, "
                    "sustain, and clarity all move — with zero change in skill. Comparing "
                    "takes of the <i>same recurring song</i> across years removes "
                    "repertoire as a variable, the way a paired experiment compares each "
                    "subject against themselves. Whatever survives this control is "
                    "execution drift, not song choice."
                ),
            ),
            (
                "The statistics",
                (
                    "Each song’s trend is a <b>Theil–Sen slope</b>: the median of all "
                    "pairwise slopes between its takes. Because it takes a median of all "
                    "pairwise slopes rather than a least-squares fit, one bad recording (a "
                    "distant mic, a sick day) can’t drag the line the way it would in "
                    "ordinary regression. The per-dimension verdict then <b>bootstraps</b> "
                    "across songs: resample the song list with replacement thousands of "
                    "times, recompute the median slope each time, and keep the middle 95% "
                    "as the confidence interval. If that interval straddles zero, the "
                    "honest call is<b> flat</b> — the data can’t distinguish the trend from "
                    "no trend."
                ),
            ),
            (
                "How to read a row",
                (
                    "The slope is in the metric’s own units per year (cents/yr for tuning, "
                    "dB/yr for CPPS). “Songs” counts the recurring clusters with enough "
                    "history to qualify. A flat verdict with a tight interval is a strong "
                    "statement: plenty of evidence, no drift. A flat verdict with a wide "
                    "interval mostly means “not enough takes to know.”"
                ),
            ),
        ),
    ),
    "range": Education(
        title="Understanding pitch & range",
        intro=(
            "The physics that makes the axis logarithmic, and the music theory "
            "behind the note names."
        ),
        sections=(
            (
                "The physics of pitch",
                (
                    "Pitch is the rate your vocal folds open and close — the <b>fundamental "
                    "frequency (f0)</b>, measured in hertz. Perception is logarithmic: one "
                    "octave is a doubling of frequency, so the step from A3 (220 Hz) to A4 "
                    "(440 Hz) sounds the same size as A4 to A5 (880 Hz), though one spans "
                    "220 Hz and the other 440 Hz. That’s why this chart’s frequency axis is "
                    "log-scaled — equal vertical distances are equal musical intervals. "
                    "A<b> semitone</b> is one twelfth of an octave (a frequency ratio of "
                    "2<sup>1/12</sup> ≈ 1.059), and a<b> cent</b> is a hundredth of a "
                    "semitone — the units the tuning panels use."
                ),
            ),
            (
                "The music theory",
                (
                    "Note names use scientific pitch notation: C4 is middle C (≈262 Hz), A4 "
                    "is concert pitch (440 Hz). The headline’s <b>working top</b> is the "
                    "95th-percentile note per quarter — the ceiling of where you actually "
                    "live (your <i>tessitura</i>), not the single highest squeak ever "
                    "caught. That distinction is why the C4 → G4 gain is meaningful: it’s "
                    "reach you use, not reach you touched once."
                ),
            ),
            (
                "Reading the river",
                (
                    "The wide envelope is p05–p95 — 90% of your sung time lives inside it; "
                    "the darker band is the p25–p75 typical zone; the line is the median. "
                    "Percentiles resist outliers, so a stray whistle or rumble can’t fake a "
                    "range change. The register strip’s thresholds (high ≥300 Hz, low "
                    "&lt;200 Hz) are calibration points for <i>this</i> voice’s chest–head "
                    "transition zone, not universal constants — they make the strip "
                    "comparable across years, which is what a longitudinal chart needs."
                ),
            ),
        ),
    ),
    "practice": Education(
        title="Understanding practice & payoff",
        intro=(
            "What a shared time axis can honestly argue, and the breath physics behind sustain."
        ),
        sections=(
            (
                "What the alignment argues",
                (
                    "The shaded eras aren’t hand-picked: they’re derived from the cadence "
                    "series itself (sustained high-volume months), which protects the chart "
                    "from cherry-picking. When sustain and clarity rise <i>inside</i> those "
                    "bands and sag after them, the alignment is doing the arguing. "
                    "Correlation is not causation — season, repertoire, and recording "
                    "habits all shift together — but repeated alignment across independent "
                    "eras is how observational evidence gets strong."
                ),
            ),
            (
                "The physics of sustain",
                (
                    "A long sustained note is a pressure-management feat: steady "
                    "<i>subglottal pressure</i> from the lungs against vocal folds that "
                    "close efficiently on each cycle. Leaky closure wastes air as "
                    "breathiness and shortens the note; efficient closure spends less air "
                    "per cycle and buys seconds. CPPS (the clarity line) tends to rise with "
                    "the same consistent fold closure, which is why the two panels move "
                    "together when practice is dense."
                ),
            ),
            (
                "The practice science",
                (
                    "Bursty practice — months of daily takes, then quiet — is visible here "
                    "as era bands with plateaus after them. Skills consolidated during a "
                    "burst (the 2021 register gain) can persist through quiet years, while "
                    "fine-motor polish (vibrato regularity, tuning) tends to decay faster "
                    "without maintenance. The chart is your own evidence for which kind "
                    "each skill is."
                ),
            ),
        ),
    ),
    "vibrato": Education(
        title="Understanding vibrato",
        intro=(
            "The oscillator behind the ornament: what rate and extent measure, and "
            "what “settled” means."
        ),
        sections=(
            (
                "The physics",
                (
                    "Vibrato is a periodic wobble in f0 produced by reflex oscillation of "
                    "the laryngeal muscles — a<b> rate</b> (how many wobbles per second, in "
                    "Hz) and an <b>extent</b> (how far each swings around the center, in "
                    "cents). In trained classical voices the reflex settles between 5 and 7 "
                    "Hz with extents around ±50–100 cents. Slower than ~4 Hz reads as a "
                    "wobble; faster than ~7–8 Hz reads as a tremolo or bleat. The blue 5–6 "
                    "Hz zone on the rate panel marks that settled classical band — the "
                    "trainable gap between your ~4 Hz default and the classical target."
                ),
            ),
            (
                "The music theory",
                (
                    "Vibrato is also a stylistic dial, not just a reflex. Classical singing "
                    "keeps it nearly always on; pop and folk use it <i>selectively</i> — "
                    "straight-tone onsets that bloom into vibrato late in a sustain. That "
                    "selectivity is what the third panel (pervasiveness: the share of "
                    "sustained time carrying vibrato) measures. Your arc — wide and "
                    "pervasive early, narrower and more selective after 2021 — is a move "
                    "toward deliberate deployment, which is a control story even though the "
                    "rate never classicalized."
                ),
            ),
            (
                "Reading the fingerprint",
                (
                    "Each dot is one take: extent across, rate up, brighter dots more "
                    "recent, hollow dots flagged low-reliability. A cloud that migrates "
                    "over the years is your default setting changing; outliers are "
                    "performances worth clicking — the inspector opens the take’s actual "
                    "pitch contour so you can see the wobble itself."
                ),
            ),
        ),
    ),
    "trust": Education(
        title="Understanding the measurements",
        intro=(
            "Why two “voice quality” lines disagree on purpose — a short course in "
            "measurement validity."
        ),
        sections=(
            (
                "The signal physics",
                (
                    "<b>HNR</b> (harmonics-to-noise ratio) compares the energy in your "
                    "tone’s periodic part against everything aperiodic. That “everything” "
                    "is the problem: room reverb, mic distance, and noise floor all pour "
                    "into the denominator, so HNR faithfully tracks seven years of changing "
                    "phones and rooms.<b> CPPS</b> — smoothed cepstral peak prominence — "
                    "asks a narrower question: how sharply does the signal’s periodicity "
                    "spike stand out above its own spectral baseline? Because it’s "
                    "self-normalizing, it largely shrugs off the recording chain, which is "
                    "why it’s the standard clinical voice-quality measure. Higher CPPS = "
                    "clearer, more efficient phonation."
                ),
            ),
            (
                "The statistics of confounds",
                (
                    "A metric that declines monotonically across seven years, in lockstep "
                    "with known gear changes and against the practice arc, carries the "
                    "signature of <i>instrument drift</i>, not vocal decline. The general "
                    "lesson: before trusting a trend, ask what else changed on the same "
                    "timeline, and prefer measures designed to be invariant to it."
                ),
            ),
            (
                "Tuning, done right",
                (
                    "The retired every-frame tuning metric punished portamento and melisma "
                    "— musical gestures, not errors — by scoring every 50 ms frame against "
                    "the nearest note. The green panel scores only <b>note cores</b>: "
                    "stable centers of held notes, slides excluded by construction. That’s "
                    "the difference between measuring expression and measuring accuracy; "
                    "~17–18 cents median error on note cores is the honest number."
                ),
            ),
        ),
    ),
    "songs": Education(
        title="Understanding the scorecards",
        intro="Per-song trends, and why the eligibility floor exists.",
        sections=(
            (
                "The statistics",
                (
                    "Each row fits a Theil–Sen slope per dimension <i>within one song’s "
                    "takes</i>. The floor — at least six reliable takes spanning at least a "
                    "year and a half — exists because slopes computed on too few takes are "
                    "mostly noise: two takes always draw a perfect line. Arrow direction is "
                    "calibrated per metric: tuning error falling (▼) is good, clarity "
                    "rising (▲) is good. The sparkline is per-year median note-core tuning "
                    "error — down is better."
                ),
            ),
            (
                "The music",
                (
                    "Songs move independently because songs are different instruments: key, "
                    "tessitura fit, and difficulty interact with your technique. One song’s "
                    "tuning improving while another’s decays usually means the second sits "
                    "where your voice changed (a raised working top shifts which notes are "
                    "comfortable). Names are privacy-generic labels (“Song A”); click "
                    "through to the Corpus table to hear the actual takes behind any line."
                ),
            ),
        ),
    ),
    "threads": Education(
        title="Understanding the threads",
        intro=("Where the song clusters come from, and what a repertoire lifecycle looks like."),
        sections=(
            (
                "How threads are found",
                (
                    "Clusters come from the local speech-to-text pass: takes whose "
                    "transcripts share enough repeated word patterns (n-gram overlap scored "
                    "with Jaccard similarity) are threaded as the same song. The labels are "
                    "deliberately generic and no lyric text ever leaves this machine — the "
                    "manifests store cluster ids and counts only. Dot area is takes per "
                    "year, so a thread’s thickness is how hard you drilled it that year."
                ),
            ),
            (
                "The repertoire lifecycle",
                (
                    "Threads make the adopt → drill → retire arc visible: most songs get a "
                    "dense year or two, then fade. Long threads that resurface years later "
                    "are the interesting ones — returning to old material is the cleanest "
                    "natural experiment this archive has (it feeds the same-song Verdict), "
                    "and a thread that returns thinner but with better scorecard numbers is "
                    "consolidation you can point at. Click any thread to open its takes in "
                    "the Corpus table."
                ),
            ),
        ),
    ),
}

_TAG = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """``text`` without its markup, as a reader sees it (for tests and accessibility)."""
    return _TAG.sub("", text).replace("&lt;", "<").replace("&amp;", "&")


def card(subject: str) -> dict[str, object] | None:
    """The card under ``subject``'s tab, for QML; ``None`` for an unknown subject."""
    found = EDUCATION.get(subject)
    return None if found is None else found.as_qml()
