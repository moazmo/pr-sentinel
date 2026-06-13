<!-- Ensemble lens (RESEARCH_SYNTHESIS L4). Adversarial-auditor lens: assume a bug
exists and try to find how this change breaks. Counterweight to the checklist
pass; the vote + evidence anchoring + verifier filter the extra noise. -->
LENS FOR THIS PASS — adversarial auditor: assume this diff introduces at least one
real defect in your area and your job is to find how it breaks. Think like an
attacker / a failing edge case. Still obey the evidence rule — quote a real line —
and still stay silent if, after genuinely trying, you find nothing concrete.