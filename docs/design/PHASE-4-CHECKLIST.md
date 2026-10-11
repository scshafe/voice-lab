# Phase 4a checklist: every React view and field, and its desktop screen

Design [VOICE-PLATFORM.md](VOICE-PLATFORM.md) §9, phase 4a: "each React view has a screen
with the same data (a written checklist)". This is that checklist. It is the acceptance of
4a, and the owner's sign-off on it is one of the two gates of the PR that removes the React
SPA's routes and voice-journey's unversioned API paths (the other is the cutover, runbook
F); see [PHASE-4-LOG.md](PHASE-4-LOG.md).

**The source.** voice-journey's SPA as it was imported (`7f62275:web/src`, read with
`git show 7f62275:web/src/<file>`; D-035 kept it out of the tree): four views (Corpus,
Journey, Practice, Referee), the header's link to the evidence report, and the toast tray.
Every component of `web/src/components/` and every state slice of `web/src/state/` is
covered below; the education texts (`journeyEducation.jsx`) are static content and move
as they are.

**The data.** The SPA read voice-journey's unversioned paths (`/api/rows`, `/api/row/…`,
`/api/audio/…`); the desktop app reads only `/api/v1` (design §3.4). The third column names
the `/api/v1` route that carries each field. The values inside a document (`trends`,
`journeys`, `coach`, `goals`, a recording row, the referee fit) keep the manifests'
camelCase keys (`api/journey_contracts.py`), so the field names below are the SPA's.

**How an item is ticked.** Each item names the tests that show it. They drive the app's
screens offscreen (pytest-qt, `QT_QPA_PLATFORM=offscreen`) against a local `/api/v1`
server started on synthetic manifests (`apps/desktop/tests/synthetic.py`; nothing real) and
assert what the screen shows. A test carries the item's id as
`@pytest.mark.checklist("C-09")`, and `tests/test_checklist.py` fails when an item has no
test, a test names an item that is not here, or a row below names a test that does not
exist. An item is ticked (☑) when its tests pass in `scripts/verify`. Hearing audio is
not something a test can do: the playback items are ticked on what is sent to the audio
device (the decoded samples, the gain, the filter), and the owner's own listening is an
owner's step.

Screens are named as in the app's sidebar. "N/A" marks a behaviour of a web page that has
no desktop counterpart, with the reason; the owner's sign-off covers those too.

## A. The app shell (`AppComponent.jsx`, `main.jsx`, `ToastTrayComponent.jsx`)

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| A-01 | Navigation: Corpus, Journey, Practice, Referee | Sidebar: Corpus; Analysis → Journey, Practice; Referee | — | `test_sidebar_reaches_every_screen` | ☑ |
| A-02 | Header link "Report" (`/report`, the evidence report) | Status: "Open the evidence report" opens `<server>/view/report` in the browser (the web view renders it; the app holds no copy) | `/view/report` | `test_status_offers_the_evidence_report` | ☑ |
| A-03 | Each view's title and tagline | Each screen's header: the same title and tagline | — | `test_every_screen_has_the_spa_title_and_tagline` | ☑ |
| A-04 | Corpus status line: "Showing N of M takes", "Loading rows…", "Row fetch failed" | Corpus: status line under the table | `recordings` (`page.total_filtered`) | `test_corpus_status_line` | ☑ |
| A-05 | Toast tray: success and error messages, dismissed on click | Toasts (scshafe-qt `SuiToastHost`), same messages | — | `test_toasts_carry_the_spa_messages` | ☑ |
| A-06 | Boot: summary and the first page of rows load at start | The app loads `summary` and the first page when the Corpus screen first shows, after sign-in | `summary`, `recordings` | `test_corpus_loads_summary_and_first_page` | ☑ |
| A-07 | Deep links: `/?bucket=…&q=…`, `?subject=…`, filters written into the URL | N/A: a desktop app has no URL. The in-app click-throughs (C-18, J-18, J-19, P-02, P-03) open the Corpus filtered, and the Journey screen remembers its subject (J-04) | — | `test_click_throughs_replace_deep_links` | ☑ |

## C. Corpus (`CorpusPageComponent.jsx`, `CorpusFiltersComponent.jsx`, `CorpusTableComponent.jsx`, `RowDetailSheetComponent.jsx`; `RowsManager`, `DetailManager`, `PlaybackManager`, `SummaryManager`)

Filters (`CorpusFiltersComponent`): each change refetches from the first page.

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| C-01 | Search (`q`): "filename, bucket, rationale" | Corpus: search field, same placeholder | `recordings?q=` | `test_corpus_search_filters_rows` | ☑ |
| C-02 | Bucket select ("All" + `filterOptions.buckets`) | Corpus: Bucket select | `summary.filter_options.buckets`; `recordings?bucket=` | `test_corpus_select_filters` | ☑ |
| C-03 | Year select | Corpus: Year select | `filter_options.years`; `?year=` | `test_corpus_select_filters` | ☑ |
| C-04 | Queue select ("Spot-check sample" = `reviewQueue=selected`) | Corpus: Queue select | `?reviewQueue=selected` | `test_corpus_select_filters` | ☑ |
| C-05 | Spot-check select | Corpus: Spot-check select | `filter_options.spot_check_statuses`; `?spotCheckStatus=` | `test_corpus_select_filters` | ☑ |
| C-06 | Transcript select | Corpus: Transcript select | `filter_options.transcript_statuses`; `?transcriptStatus=` | `test_corpus_select_filters` | ☑ |
| C-07 | Lyric mark select | Corpus: Lyric mark select | `filter_options.lyric_match_statuses`; `?lyricMatchStatus=` | `test_corpus_select_filters` | ☑ |
| C-08 | "Clear filters", shown only while a filter is set | Corpus: Clear filters button, same rule | — | `test_corpus_clear_filters` | ☑ |

The table (`CorpusTableComponent`, `PinnedDataTable`): rows are `fields=list` rows.

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| C-09 | Column Queue: `#reviewQueueOrder`; sorts by `reviewQueueOrder` | Corpus table: Queue | `recordings` row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-10 | Column Filename (pinned, the row header); click opens the detail | Corpus table: Filename (first column, stays in view); activating a row opens the detail | row | `test_corpus_columns_render_the_list_fields`, `test_corpus_row_opens_detail` | ☑ |
| C-11 | Column Captured: `capturedAt` as `YYYY-MM-DD HH:MM`; the default sort, ascending | Corpus table: Captured, same format and default | row | `test_corpus_columns_render_the_list_fields`, `test_corpus_sorts_on_header` | ☑ |
| C-12 | Column Year | Corpus table: Year | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-13 | Column Duration: `durationSeconds` rounded, `Ns` | Corpus table: Duration | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-14 | Column Bucket: a badge, its tone by bucket (`bucketTone`) | Corpus table: Bucket chip, the same tone per bucket | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-15 | Column Conf.: `confidence`, two decimals | Corpus table: Conf. | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-16 | Column Spot-check: status (muted when `not_reviewed`) and the human verdict under it | Corpus table: Spot-check, two lines | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-17 | Column Transcript: status and "N words" | Corpus table: Transcript, two lines | row | `test_corpus_columns_render_the_list_fields` | ☑ |
| C-18 | Column Lyric cluster: label (or id); click filters the table to it (`q=<cluster>`) | Corpus table: Lyric cluster; activating it sets the search to the cluster | row | `test_corpus_cluster_click_filters` | ☑ |
| C-19 | Column Listen / review: Play (disabled without playback, with the gate's tooltip) and Review | Corpus table: Play and Review buttons, same rule and tooltip | `summary.playback_enabled` | `test_corpus_play_is_gated`, `test_corpus_row_opens_detail` | ☑ |
| C-20 | Sort: a header click sorts by its field, a second click reverses; ▲/▼ on the active one | Corpus table: sortable headers, same toggling and arrows; sorting is the server's | `?sort=&direction=` | `test_corpus_sorts_on_header` | ☑ |
| C-21 | Rows in the spot-check sample are highlighted (`reviewQueueSelected`) | Corpus table: the same rows highlighted | row | `test_corpus_highlights_the_review_sample` | ☑ |
| C-22 | Paging: 100 rows a page, the next page loads at the end of the table ("Loading…") | Corpus table: the same, at the end of the virtualised list; about 7,000 rows stay smooth | `?limit=100&offset=` | `test_corpus_pages_at_the_end`, `test_corpus_table_handles_7000_rows` | ☑ |
| C-23 | One player at a time: Play swaps the one audio element to that row | Corpus: one player; Play on another row stops the first | `recordings/{id}/audio` | `test_corpus_one_player_at_a_time` | ☑ |

The row detail (`RowDetailSheetComponent`, `DetailManager`, `TranscriptManager`): the full
row (`/api/row/:id`) and, on request only, its local transcript text.

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| C-24 | Title: filename; description: bucket · year | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-25 | Captured, Duration, Size (MB, two decimals), Bucket, Confidence | Detail sheet: same labels and formats | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-26 | Spot-check, Human verdict, Reviewed by, Reviewed at | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-27 | Queue: `#order — reason` | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-28 | Transcript: status · N words · language | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-29 | Lyric cluster, Lyric confidence, Wordless vocalise ("marked") | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-30 | Empty values are left out of the list | Detail sheet: same | — | `test_corpus_detail_fields` | ☑ |
| C-31 | Classifier rationale | Detail sheet: same | `recordings/{id}` | `test_corpus_detail_fields` | ☑ |
| C-32 | Listen: a player when playback is on; else "Playback requires the release-gated launch." | Detail sheet: player, or the same sentence | `recordings/{id}/audio` (Range) | `test_corpus_detail_listen` | ☑ |
| C-33 | Transcript text: "View local text" (only when enabled, completed and local-only); the text, "Loading…", or the error; otherwise "No completed transcript." / "Local transcript text is not enabled for this launch." | Detail sheet: same rule, button and messages; the text is fetched only on that click and dropped when the sheet closes or another row opens | `recordings/{id}/transcript` | `test_corpus_detail_transcript_on_request` | ☑ |
| C-34 | Verdict: an optional note and the five verdict buttons, the current one primary | Detail sheet: note field and five buttons, same labels | `review/verdicts` | `test_corpus_verdict_is_recorded` | ☑ |
| C-35 | A verdict updates the open row and its table row (`reviewed`, the verdict) and the totals; toast "Verdict recorded: …" / "Verdict failed: …" | Same updates and toasts | `review/verdicts` (`totals`) | `test_corpus_verdict_is_recorded`, `test_corpus_verdict_failure_toasts` | ☑ |
| C-36 | Loading and failed states of the detail | Detail sheet: same | — | `test_corpus_detail_states` | ☑ |
| C-37 | Close | Detail sheet: Close (and Escape) | — | `test_corpus_detail_states` | ☑ |

## J. Journey (`JourneyPageComponent.jsx`, the charts in `components/charts/`, `journeyEducation.jsx`; `TrendsManager`, `JourneysManager`, `TakeInspectorManager`)

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| J-01 | "Loading trends…", a failure notice, and "Voice trends need the features manifest…" when trends are unavailable | Journey: same states and text (the hint names the PC's pipeline, not a relaunch flag) | `trends` (`available`) | `test_journey_states`, `test_journey_needs_a_sign_in` | ☑ |
| J-02 | Stat tiles: takes analyzed, h singing measured, working top `+N st` (baseline → peak year), reliable takes, flagged for listening | Journey: stat tiles, same values and labels | `trends.headline` | `test_journey_stat_tiles` | ☑ |
| J-03 | "include the flagged low-reliability takes" toggle: all-takes quarters, hollow flagged dots | Journey: the same checkbox; the panels switch to `quartersAllTakes` and the fingerprint shows flagged takes hollow | `trends.quartersAllTakes`, `vibratoTakes[].reliable` | `test_journey_include_flagged` | ☑ |
| J-04 | Subject tabs: The Verdict, Range River, Cause & Effect, Vibrato Story, What Not To Trust, Song Scorecards, Repertoire Threads; shown only when their data is (`availability`); the active one remembered | Journey: the same tabs, the same availability rule; the active tab is kept for the session | `trends`, `journeys` | `test_journey_tabs_follow_availability` | ☑ |
| J-05 | The Verdict: per dimension label, median slope/yr, CI, songs, verdict chip; the summary sentence (steady, or the counts) | Journey → The Verdict: same rows and sentence | `journeys.improvementIndex`, `totals.clustersEligible` | `test_journey_verdict` | ☑ |
| J-06 | Range River chart: C and G grid lines with note and Hz labels; reach (p05–p95) and typical (p25–p75) bands; median line, broken across quarters with fewer than 3 reliable takes; faint dots for those; the register strip (high, mid, low); year labels | Journey → Range River: the same layers (chart) | `trends.quarters[]` (`envelope`, `typical`, `medianHz`, `registerShares`, `nReliable`) | `test_journey_range_river_layers` | ☑ |
| J-07 | Range River tooltip per quarter: reliable of total takes, reach notes and Hz (or "sparse quarter"), median note, high-register share | Journey → Range River: the same text on hover | `trends.quarters[]` | `test_journey_range_river_layers` | ☑ |
| J-08 | Range headline sentence: working top note (year) → note (year), +N semitones | Journey → Range River: same sentence | `trends.headline.workingTop` | `test_journey_range_river_layers` | ☑ |
| J-09 | Range legend (reach, typical, median, register strip) | Journey → Range River: same legend | — | `test_journey_range_river_layers` | ☑ |
| J-10 | Range data table: Quarter, Reliable, Total, Floor, Top, Median, High share | Journey → Range River: "Data table" (collapsed), same columns | `trends.quarters[]` | `test_journey_data_tables` | ☑ |
| J-11 | Cause & Effect chart: recordings per month (bars, tooltip), high-practice era bands, longest sustained note and CPPS panels (band, line, dots, tooltips, axis ticks), year labels | Journey → Cause & Effect: same layers | `trends.monthlyCadence`, `eras`, `quarters` | `test_journey_cause_effect_layers` | ☑ |
| J-12 | Era buttons "▶ <era> · N takes": plays the era's sample takes one after another (playback gated) | Journey → Cause & Effect: same buttons; the takes play in order through the one player; disabled without playback | `eras[].sampleRecordingIds`, `recordings/{id}/audio` | `test_journey_era_playback`, `test_journey_eras_are_silent_without_playback` | ☑ |
| J-13 | Cause & Effect data table: Quarter, Takes, Sustain s, CPPS, HNR | Journey → Cause & Effect: "Data table", same columns | `trends.quarters[]` | `test_journey_data_tables` | ☑ |
| J-14 | Vibrato triptych: rate (with the settled 5–6 Hz zone), extent, share of sustained time; band, line, dots, tooltips | Journey → Vibrato Story: the same three panels | `trends.quarters[].vibrato`, `spread` | `test_journey_vibrato_layers` | ☑ |
| J-15 | Fingerprint scatter: extent × rate per take, brass by year, hollow when flagged, tooltip; click opens the take inspector | Journey → Vibrato Story: same scatter; activating a dot opens the inspector | `trends.vibratoTakes[]` | `test_journey_vibrato_layers`, `test_journey_take_inspector` | ☑ |
| J-16 | Year legend of the fingerprint | Journey → Vibrato Story: same legend | `vibratoTakes[].year` | `test_journey_vibrato_layers` | ☑ |
| J-17 | Take inspector: title (filename · date); tiles median pitch (note, Hz), range p05–p95, vibrato (Hz · cents), CPPS, longest sustain, bucket; the f0 contour; a player (gated); "Per-take contours need the local feature store" when it is not configured | Journey: the take inspector sheet, same tiles, contour and messages | `recordings/{id}/features`, `…/audio` | `test_journey_take_inspector`, `test_journey_take_inspector_without_the_feature_store` | ☑ |
| J-18 | What Not To Trust: CPPS and HNR panels, the note-core tuning panel (with journeys), the tuning tile (measured, or not yet) | Journey → What Not To Trust: same panels and text | `trends.quarters[]`, `journeys.noteCore.takes` | `test_journey_trust` | ☑ |
| J-19 | Song Scorecards: the ten songs with most takes: "Song A…" and the cluster id, takes and years, the tuning sparkline by year, tuning and clarity slopes (▲/▼, good or bad); click opens the Corpus on that song | Journey → Song Scorecards: same rows; activating a name opens the Corpus filtered to the cluster | `journeys.journeys[]`, `trends.clusters` | `test_journey_scorecards` | ☑ |
| J-20 | Repertoire Threads: one thread per song (Song A…), its span, a dot per year sized by takes with a tooltip, "N takes · years"; click opens the Corpus on that song | Journey → Repertoire Threads: same chart; activating a dot opens the Corpus filtered | `trends.clusters[]` | `test_journey_threads` | ☑ |
| J-21 | The education card under each subject (title, intro, body) | Journey: the same text under each tab | — | `test_journey_education_cards` | ☑ |
| J-22 | Chart tooltips follow the pointer | Charts: hover shows the same tooltip text | — | `test_journey_range_river_layers`, `test_journey_cause_effect_layers` | ☑ |

## P. Practice (`PracticePageComponent.jsx`; `CoachManager`, `GoalsManager`)

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| P-01 | "Consulting the coach…" and a failure notice | Practice: same states | `coach` | `test_practice_states`, `test_practice_failure_notice` | ☑ |
| P-02 | Due for a take: label and cluster id, takes and years, "last sung N d ago", the why chips; click opens the Corpus on the song; the empty sentence with `dueAfterDays` | Practice: same card | `coach.dueSongs`, `params.dueAfterDays` | `test_practice_due_and_frontier`, `test_practice_empty_due_and_frontier` | ☑ |
| P-03 | The frontier: label and cluster id, rule chips (range stretch, vibrato settling, sustain PB, nearly in tune, evidence-thin), each flag's detail; click opens the Corpus; the empty sentence | Practice: same card | `coach.frontier` | `test_practice_due_and_frontier`, `test_practice_empty_due_and_frontier` | ☑ |
| P-04 | Goals: title, derived status chip (active, achieved, missed, abandoned), scope (cluster or "whole corpus"), Abandon on active goals, progress ("N of M takes · running median … vs target … · registered …") or "abandoned …"; the empty sentence; a failure notice | Practice: same list | `goals` | `test_practice_goals_list`, `test_practice_goal_abandon` | ☑ |
| P-05 | Goal form: Metric (from `metrics`), Direction (at least, at most), Target, Takes (N), Scope (whole corpus or a cluster "label (takes)"), Title; Pre-register, enabled when the metric and a numeric target are set; toast and refresh | Practice: same form, rule and toasts ("Goal pre-registered — …" / "Goal not created: …") | `goals` (`metrics`, `clusterOptions`); `POST goals` | `test_practice_goal_form`, `test_practice_form_rules` | ☑ |

## R. Referee (`RefereePageComponent.jsx`, `BradleyTerryChart`; `RefereeManager`)

| Id | React: field | Desktop: where | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| R-01 | A failure notice | Referee: same | — | `test_referee_states` | ☑ |
| R-02 | Without playback: the gate notice with the number of blind pairs waiting; results still shown | Referee: same notice (the hint names the server's playback approval) and the results | `referee/next` (`403 playback_not_approved`), `referee/results.pool_size` | `test_referee_gate_notice` | ☑ |
| R-03 | The pair: players A and B, loudness-matched (the server's `gainDb` per side) and band-limited to 8 kHz | Referee: players A and B; the samples sent to the device carry each side's gain and an 8 kHz low-pass | `referee/next` (`a.gain_db`, `b.gain_db`, `audio_url`) | `test_referee_matched_playback` | ☑ |
| R-04 | Choices: A sounds better, B sounds better, Too close, Not the same song, Skip; disabled while sending or revealed | Referee: same five buttons, same rule; judging stops playback | `referee/verdicts` | `test_referee_judgment_and_reveal` | ☑ |
| R-05 | The reveal: "A was <date> · B was <date> · <cluster>", then the next pair after 1.4 s | Referee: same line and pause | `referee/verdicts` (`reveal`) | `test_referee_judgment_and_reveal` | ☑ |
| R-06 | Progress "N of M pairs judged."; "Pool complete: …" when no pair is left | Referee: same | `referee/next` (`judged`, `pool_size`) | `test_referee_judgment_and_reveal`, `test_referee_pool_complete` | ☑ |
| R-07 | A track that fails to load: one toast per pair, saying why | Referee: one toast per pair (the hint names the server's corpus access) | — | `test_referee_audio_error_toasts_once` | ☑ |
| R-08 | Results (once something was judged): Bradley–Terry strengths per year (log2), the zero line, dots sized by trials, labels | Referee: same chart | `referee/results.fit.years` | `test_referee_results` | ☑ |
| R-09 | Counts table: Year, Strength (log2), Wins, Losses, Ties, Trials; the choices line | Referee: same table and line | `fit.years`, `fit.byChoice` | `test_referee_results` | ☑ |
| R-10 | Chained-cluster flags: "<cluster> ×N" | Referee: same line | `fit.notSameSongFlags` | `test_referee_results` | ☑ |
| R-11 | Results refresh after each judgment | Referee: same | `referee/results` | `test_referee_judgment_and_reveal` | ☑ |

## Screens with no React view

Design §6.1 also gives 4a two screens the SPA never had. They are checked here so that the
whole of 4a is in one place.

| Id | Screen: field | Source | `/api/v1` | Ticked by | ✓ |
| --- | --- | --- | --- | --- | --- |
| S-01 | Sign-in and status: Pocket ID sign-in (PKCE, loopback), the account, sign-out | voice-journey-capture's sign-in | — | `test_status_sign_in_and_out`, `test_sign_in_needs_the_settings` | ☑ |
| S-02 | The server: its address, build, API versions served, the minimum client version (and a refusal when the app is older) | design §3.4 `meta` | `meta` | `test_status_shows_the_server`, `test_an_older_app_is_told_to_update` | ☑ |
| S-03 | The server's gates: playback, transcript text, feature detail; the job runner's watch | — | `summary`, `watch` | `test_status_shows_the_server`, `test_watch_lines_summarize_the_job_runner` | ☑ |
| S-04 | The spool: sessions waiting for a receipt | design §6.1 | — | `test_status_shows_the_spool` | ☑ |
| S-05 | Settings: server and issuer (no default in the code), client id; refused when invalid | publication screen §5 | — | `test_status_settings`, `test_nothing_in_the_code_names_a_server` | ☑ |
| U-01 | Uploads: each local session with its state (waiting, uploading, accepted, refused, not uploadable) and its last error | voice-journey-capture's `--sessions` | — | `test_uploads_lists_sessions` | ☑ |
| U-02 | Per-session progress while uploading (file and bytes) | voice-journey-capture's upload text | intake | `test_uploads_progress_and_receipt` | ☑ |
| U-03 | Resume: an interrupted upload continues from the server's byte count; retry refused sessions | `upload.py`, `--retry-failed` | intake | `test_uploads_resume_and_retry` | ☑ |
| U-04 | Receipts: an accepted session shows when and where it was accepted | `upload.json` | intake | `test_uploads_progress_and_receipt` | ☑ |
