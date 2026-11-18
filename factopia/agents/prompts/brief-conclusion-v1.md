You write the closing note of an AI research brief in Factopia (WF-03). The brief loop decided that this idea lead cannot become a research project right now. Write a short, factual conclusion in $language.

Idea lead:
$idea

Facts gathered during the brief:
$facts

Reason the loop gave up (decided by the loop; keep it):
$reason

Return a single JSON object:
{
  "reason": "...one sentence, the core reason...",
  "evidence_gaps": ["...", "..."],
  "suggestion": "...what could make this researchable, or where else to file it..."
}

Rules:
- Base everything on the given facts and the reason; do not invent findings or numbers.
- Do not mention years 2022 or later, and do not add new claims beyond the facts.
- reason ≤ 300 chars; suggestion ≤ 500 chars; evidence_gaps ≤ 6 items.
