# Independent NeuroKit2 review

This optional local application reads recorded files. It does not connect to
Bluetooth, import Qt, or call the recorder's analysis functions. The code lives
in `hrv_review/` and can be reused separately. HnH remains the recorder.

## Start

Use the existing source environment (Python 3.11):

```sh
uv pip install --python .venv/bin/python -e '.[review]'
sh scripts/start_hrv_review.sh
```

Open http://127.0.0.1:8501. The launcher binds to loopback only and disables
Streamlit usage telemetry. No upload or cloud account is required. Stop the
server with Ctrl+C in its terminal. Keep the local server private; it is a
single-user desktop tool, not a service intended for network deployment.

## Review a recording

1. Select the folder and completed recording in the sidebar. The current importer
   reads this fork's `rr_intervals.csv` plus schema-1 `meditation.json`; arbitrary
   third-party CSV formats are not silently guessed. Legacy sessions without
   original RR capture are not imported.
2. See whole-phase RMSSD, lnRMSSD, SDNN, mean HR, counts and quality reasons.
   A four-minute after phase remains visible with its actual duration.
3. Zoom the original RR chart. Orange dotted lines are recorder segment markers;
   these may represent signal warnings rather than actual link loss. Red points
   are excluded beats. Full sample indices and reasons are below the chart.
4. Edit phase names and boundaries, conditions, comparison window duration and
   notes. Times are in minutes from recording start, including any setup prelude.
   Manual exclusions use the original sample indices and never remove raw rows.
5. Apply to preview, then save a version to persist. Each save creates a new JSON
   under `~/.local/share/hrv-review/`, containing the draft, results, algorithm and
   NeuroKit2 versions, input hashes, and parent revision. The original session
   directory is never modified. A changed source invalidates old annotations;
   the UI starts from the new source with a warning and permits a new review.
6. Download phase CSV or full JSON. Exports reflect the applied draft; saving a
   persistent version is a separate operation.

## Calculation and continuity

- Selected raw RR must be finite and within 250–2500 ms. Recorder corrections,
  raw/clean disagreements, and manually rejected samples are excluded explicitly.
  No median replacement, interpolation, or automatic ectopic-beat diagnosis is
  performed by this tool.
- For each contiguous accepted run, `neurokit2.hrv_time({"RRI": ...})` computes
  RMSSD. Runs are combined as the square root of the pair-count-weighted mean
  squared RMSSD. Thus no difference crosses an excluded beat, missing index,
  recorder segment boundary, arrival gap longer than five seconds, or phase edge.
- SDNN (sample standard deviation) and mean NN use all accepted intervals in the
  selection. Mean HR = 60000 / mean NN. Zero RMSSD gives undefined/null lnRMSSD.
- Receipt times locate phases; they are not beat timestamps and are never supplied
  to NeuroKit2 as R-peak positions. Beat timing for its interval-only input is
  reconstructed from RR within each run.
- Under 90% or above 110% RR-duration coverage, over 5% rejected beats, missing
  sample indices, and arrival gaps over five seconds prevent comparison. Values
  remain inspectable. Ambiguous recorder markers require an explicit review
  checkbox; that checkbox cannot waive real gaps or join pairs across markers.
- These are transparent technical heuristics, not clinically validated quality
  guarantees. In-range artefacts can pass. Cross-tool agreement on arithmetic
  does not validate the sensor or beat classification.

## Comparisons and limits

Whole-phase values and fixed-window summaries are different quantities. The
comparison view uses only full, technically eligible windows of the selected
duration, then takes their median per session. A missing after phase does not
exclude a valid before or practice phase. Custom breathing protocols are allowed
as named conditions, including those originally stored as Other.

Groups separate profile, practice name, posture, breathing protocol, phase,
window length and six-hour time-of-day band. Labels must be consistent. This is
not automatic scientific matching: differing stage mixes, practice duration,
sleep, caffeine or actual respiration can still confound comparisons. Inspect
window counts and notes; no causal claims or meditation score are produced.

The first version covers time-domain metrics and explicit file review. Spectral
or nonlinear methods, measured respiratory synchronisation and arbitrary CSV
adapters are not implemented. They need their own duration, artefact and timing
validation rather than simply adding more reported numbers.

References: [NeuroKit2 HRV API](https://neuropsychology.github.io/NeuroKit/functions/hrv.html),
[NeuroKit2 publication](https://doi.org/10.3758/s13428-020-01516-y).

## Checks

```sh
.venv/bin/python -m pytest test/test_hrv_review.py test/test_hrv_review_app.py -q
```

Fixtures cover short phases, manual/recorder exclusions, segment and packet gaps,
batched timestamps, grouping, immutable sources, stale versions, and UI editing,
save and reload. Tests contain only synthetic recordings.
