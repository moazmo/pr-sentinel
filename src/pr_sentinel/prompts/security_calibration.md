<!-- Calibration anchors for the Security agent (accuracy.calibration). Stable,
cache-friendly. Two flag cases and two stay-silent cases to anchor precision. -->

## Calibration (how a careful security reviewer calls these)

FLAG — `query = f"SELECT * FROM users WHERE name = '{name}'"` where `name` comes
from a request → critical `sql-injection`: untrusted input is concatenated into SQL.

FLAG — a new route handler added next to siblings that all call `require_auth(...)`,
but this one omits it → high `broken-authorization`: the diff shows the missing check.

STAY SILENT — `conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))`:
parameterized; the `?` placeholder is the fix, not a bug. Do not flag it.

STAY SILENT — a hardcoded string that only *looks* like a token but is an obvious
test fixture (`"test-token"`, `"xxx"`) with no real key format → not a secret.

When the visible code already does the safe thing, report nothing. A confirmed
false positive costs more trust than a missed low-severity nit.
