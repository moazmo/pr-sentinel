<!--
Chain-of-thought scan (RESEARCH_SYNTHESIS L2/L3). Appended when accuracy.cot ==
"brief". Reasoning stays SHORT and precedes the findings, but each finding is
still emitted verdict-first (severity/category before the prose message), which
PromptAudit (2605.24171) found reduces abstention vs reasoning-last. The parser
ignores the `analysis` key; the ensemble votes on findings, never on reasoning.
-->

## Think before you commit (brief)

Before the findings array, add a short `"analysis"` string (1-3 sentences): name
what the diff changes and where the real risk in YOUR area concentrates. Then list
the findings. Keep the analysis terse — it is a scan, not an essay — and never let
it talk you out of reporting a concrete issue you can see. Output shape:

```
{"analysis": "<1-3 sentence scan of the changed code in your area>",
 "findings": [ ... same finding objects as specified above ... ]}
```
