<!-- Calibration anchors for the Performance agent (accuracy.calibration). -->

## Calibration (how a careful performance reviewer calls these)

FLAG — a DB/API call inside a loop over records (`for u in users: fetch(u.id)`)
where one batch call exists → high `n-plus-one`: visible per-item I/O on a hot path.

FLAG — `time.sleep(...)`, `requests.get(...)`, or a blocking file read inside an
`async def` → high `blocking-call-in-async`: stalls the event loop.

STAY SILENT — a loop bounded by a small, fixed constant (e.g. iterating 7 weekdays),
or `% ` vs f-string micro-choices. No measurable impact → not a finding.

STAY SILENT — an O(n²) shape over input the diff proves is tiny and bounded.
Severity tracks realistic data size, not theoretical worst case.

Report only what visibly degrades at realistic scale. When in doubt, leave it out.
