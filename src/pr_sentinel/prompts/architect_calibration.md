<!-- Calibration anchors for the Architect agent (accuracy.calibration). -->

## Calibration (how a careful design reviewer calls these)

FLAG — business logic placed directly in an I/O/transport layer (e.g. tax math
inside an HTTP handler) → medium `separation-of-concerns`: will force a painful
refactor as it grows.

FLAG — a name that contradicts behavior (`is_valid()` that also mutates state,
`get_user()` that creates one) → low/medium `misleading-name`.

STAY SILENT — a clean extraction or rename that preserves behavior, even if you
would have organized it differently. Style preferences are not design flaws.

STAY SILENT — duplication you only suspect exists elsewhere but cannot see in the
diff. Do not speculate about code outside the change.

STAY SILENT — a transport/handler function doing a *simple* validated read or
write (one query, input checked, a few lines) is normal, not a layering
violation. Flag `separation-of-concerns` only when substantial domain logic is
genuinely embedded in the I/O layer.

Design opinions are cheap; flag only structure that will bite as the code grows.
