# Meditation sessions and longitudinal analysis

The **Meditation…** and **Meditation history** buttons are above the live plots.
Use the source installation of this fork; the upstream beta.2 desktop package
does not contain this workflow.

## Record a session

1. Connect the H10. HnH normally starts a general recording on connection; if it
   does not, use **Start New**. Original RR capture starts with that recording.
2. Settle into your usual posture. Open **Meditation…**, enter the practice,
   posture and breathing mode, and any optional diary information.
3. Choose a saved template (default **5/16/5 coherence**) and click
   **Применить шаблон**, or set the three durations individually, then press
   **Begin baseline**. Data from
   before you pressed this button remain recorded but are outside the protocol.
4. **Automatic transitions and stop** advances at the planned boundaries and
   saves the recording at the end. Choose **Голосовые сообщения**, tones or
   silence; use **Проверить звук** before starting. Turn automatic transitions off to advance using
   **Next phase**; the after phase ends with **Finish & save**.
5. Use **Meditation…** again after saving to enter after-session ratings,
   or complete the separate diary in HRV Review.
   Disconnect the sensor before removing the strap.

You can always stop early using **Stop & Save**. The recording is retained;
missing or short phases do not acquire invented comparison values.

To prepare a protocol before recording, open **Meditation…**, choose its settings
and **Use with Start New**. **Start New** then starts both the recording and the
baseline cue immediately. Closing this setup without **Use with Start New**
leaves no automatic plan armed, including when cancelling edits to an earlier
prepared plan. After a saved session, **Prepare next session** opens
this setup without changing the previous diary. A normal recording without a
prepared plan announces “Запись началась”; **Begin baseline** remains available
to start its protocol after settling in.

**Pause / Resume**, next to **Stop & Save**, works for ordinary recordings and
meditation protocols. It freezes the phase timer and disables manual transitions;
automatic transitions wait until you resume. Saving while paused is supported.
The sensor stays connected and live preview continues. Original RR keep their
wall-clock receipt times even during a break, with explicit `pauses` in
`meditation.json`; the event CSV omits live metrics during the break and records
`SessionPause` / `SessionResume`. Meditation analysis and HRV Review exclude pause
ranges. Each uninterrupted section starts new fixed windows; two short sections
are never stitched into a five-minute measurement. Paused sessions remain
reviewable but are excluded from comparisons with uninterrupted practices.

**Проверить звук** is also available beside the phase timer. Audio failures and
player exit codes are retained in `session-audio.log` in the application data
folder. A successful player exit cannot confirm that your chosen speakers or
headphones were audible; check the macOS output and volume if playback is silent.

Practice, posture and breathing mode are used for grouping. Paced breathing also
needs its rate. Unrecorded conditions and “Other” posture/breathing do not enter
a comparison group. Optional diary fields include sleep, caffeine and its timing,
attention, tension and sleepiness before/after (0–10), and free-text notes.
“Not recorded” is different from a score of zero. These ratings are a personal
diary, not a validated clinical scale.

## Compare sessions

Open **Meditation history** for the active profile. Hidden sessions are omitted.

- The table shows before/after RMSSD, the paired change in RMSSD and lnRMSSD,
  diary context and counts of usable five-minute windows.
- Select a comparison group to plot a trend. Different techniques, postures,
  breathing modes/rates, local time-of-day bands (six hours each), and numbers
  of complete windows in the three phases are not pooled.
- Date, caffeine and minimum-sleep filters further restrict the sessions.
- Choose baseline, practice or after-minus-before metrics. **Weekly medians**
  aggregates the selected group by local calendar week.
- The details panel explains excluded windows and shows the subjective ratings.
  Incomplete protocols, stale analyses and protocols with a poor full window do
  not enter trend plots. Their data remain visible in the table.
- **Edit diary** updates context, **Recalculate** rebuilds the analysis, and
  **Open session folder** exposes the original data and exports.

Each phase summary is the median of its usable five-minute window metrics.
Consequently a 15-minute practice has three separate calculations; it is not
the average of the smoothed values displayed by the live chart. Incomplete
tails are retained as explicitly unusable rows, not compared to full windows.
Group durations describe analyzed windows; the exact phase durations remain in
the metadata. Manual transitions can still introduce differences in total
practice time, so automatic timing is preferable for repeated comparisons.

## Files and timing

Every new recording also writes:

- **rr_intervals.csv**: one row per received RR, preserving fractional
  milliseconds, the live filter's result and its correction reason.
  Columns: sample_index, received_at, elapsed_sec, raw_rr_ms, cleaned_rr_ms,
  correction, segment_id. This file is append-only during capture and is never
  rewritten by diary editing or analysis.
- **meditation.json**: versioned capture metadata, phase boundaries and diary.
- **meditation_analysis.json**: versioned algorithm and quality policy, input
  SHA-256 hashes, per-window and per-phase results, and after-minus-before changes.
- **meditation_windows.csv**: the same individual window results in tabular form.

RR elapsed_sec values really are seconds, measured with a monotonic clock from
the start of capture. received_at is a local ISO timestamp including UTC offset.
They describe **receipt**, not precise hardware acquisition times. H10 can send
multiple RR intervals in one Bluetooth packet; identical/nearby receipt times
are retained. RR precision is preserved when converting the H10's 1/1024-second
units to milliseconds; receipt timestamps must not be mistaken for R-peak times.

The ordinary event file **session.csv** now calls its time column **elapsed_ms**.
Previous HnH files mislabeled milliseconds as elapsed_sec. Replay, tag insights,
import and report rebuilding still read those old files with their original
millisecond interpretation. Old files are not migrated or overwritten.
The ordinary live-chart report is labeled separately from the fixed-window
meditation analysis.

The live filter's correction flags distinguish unchanged, out-of-range replaced,
out-of-range unfiltered (before enough filter history exists), and invalid
samples. They describe what the software did; they are not ECG-adjudicated
normal/ectopic classifications. Existing files that only stored corrected IBIs
cannot recover the original RR stream retrospectively.

## Calculation policy (meditation-1)

- Non-overlapping 300-second windows, anchored to each phase's actual start.
- Primary metrics use finite, positive, unchanged in-range RR with no difference
  between raw and cleaned values. A 250–2500 ms guard supplements the recorded
  live-filter decisions.
- RMSSD uses only adjacent accepted samples in the same continuity segment.
  Removing a sample does not join its former neighbors. No pair crosses a phase,
  window, reconnect or rejected beat.
- SDNN uses sample standard deviation (ddof=1). Mean HR is 60000 / mean RR.
  lnRMSSD is the natural log; zero RMSSD produces null rather than negative infinity.
- A usable window requires at least 90% duration coverage by accepted RR,
  at most 5% excluded samples, no reconnect or receipt gap longer than 5 seconds,
  no implausible excess RR duration (>110%), and at least one adjacent pair.
  These are explicit, conservative project defaults, not clinically validated
  quality thresholds. A passing window can still contain in-range motion or
  ectopic artifacts. Review the original data before stronger interpretations.
- Partial windows contain reasons and counts but null primary metrics.
- Phase medians and paired changes are separate from the original biofeedback
  calibration, rolling 60-beat metrics and live smoothing.

To recalculate outside the UI, from the checkout:

    .venv/bin/python -m hnh.meditation /absolute/path/to/session-directory

This updates the derived JSON/CSV only. The stored original RR and diary permit
future alternative processing, including independent tools. New RR recordings
left active by a crash are preserved during startup recovery and marked
interrupted/abandoned, rather than deleted. Explicit user cleanup actions still
apply. A damaged recording remains available for manual recovery even when
automatic analysis cannot read it.

## Interpretation

These views compare physiology and self-reports. They do not estimate a
“meditation level” or prove an effect of practice. Breathing, posture, sleep,
caffeine, movement and other factors can change HRV. Keep before/after
conditions consistent and consider the diary alongside the cardiac measures.
LF/HF is not used as a meditation score in this workflow.

Methodological background:
- [Laborde et al., HRV experiment planning and reporting](https://doi.org/10.3389/fpsyg.2017.00213)
- [Quigley et al., 2024 psychophysiology measurement guidelines](https://doi.org/10.1111/psyp.14604)
- [Brown et al., mindfulness/meditation HRV meta-analysis](https://pubmed.ncbi.nlm.nih.gov/33395216/)

## Development

Use Python 3.11: the current hrv-analysis dependency declares Python <3.12.
Install with `python -m pip install -e ".[dev]"`.
Run `QT_QPA_PLATFORM=offscreen python -m pytest -q test/`.
Tests use synthetic data and temporary recording directories; no sensor is
required. A real full meditation protocol should still be checked with the H10
before relying on the workflow for a long-term series.

## Templates and voice notifications

The recorder reads versioned JSON templates from
`~/.local/share/hrv-review/practice-templates/` (override the review folder with
`HRV_REVIEW_STORE`). HRV Review owns the template editor; no Python dependency
between the applications is required. Built-in 5/16/5 coherence and 5/15/5 breath
attention templates remain available if no custom files exist. Reload templates
in the dialog after saving changes in HRV Review.

Each recording stores its selected template revision and actual durations.
Changing templates does not copy sleep, caffeine or personal ratings. All three
phases allow 1–120 whole minutes; existing fixed-window analysis still treats
short windows separately.

Voice messages identify baseline start, practice start, natural-breathing after
phase, completion and early stop. Automatic and manual transitions are covered.
A delayed timer announces the current phase rather than queuing obsolete phase
messages. Repeated timer refreshes do not repeat announcements.

On macOS speech uses `/usr/bin/say` with the local Milena voice; generated tones
use `afplay`. Other platforms use an available `espeak` engine or a system beep.
Unavailable speech falls back with a status message. Audio is asynchronous and
does not block RR recording. Keep the application running and test the system
output/volume before a session; browser visibility is unrelated to these cues.
