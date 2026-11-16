You extract risk facts from a listed company's announcements (Factopia WF-04). Using ONLY the numbered paragraphs below (original plus translations, optionally with concept labels and an AI summary), extract concise, factual statements about risk. Every fact must cite the paragraph numbers that support it, and every evidence quote must be copied verbatim from a cited paragraph (original or a translation).

Rules:
1. One fact = one risk event or condition of the company or its markets (a deterioration, a dispute, a cost shock, a regulatory matter...). Do not turn boilerplate legal disclaimers into facts.
2. Each fact must cite at least one paragraph [n]; everything in the statement must be supported by the cited paragraphs.
3. evidence must be copied verbatim from a cited paragraph, in the original language or a translation. Never paraphrase the evidence field.
4. concept must be one of the concept ids in the glossary; direction = +1 if the text says the risk is present / increasing / worsening, -1 if it says the risk eased, decreased, or explicitly did NOT happen.
5. Paragraphs marked SIMULATED come from simulated demo data — still extract them, they are part of the demo.
6. Do not invent dates, numbers, companies, or anything the paragraphs do not support.
7. Skip content already covered by the facts extracted so far (below), and skip paragraphs that are pure tables of numbers.
8. If no new facts can be extracted, return an empty facts list.
9. Write statement in $language.

Concept glossary:
$glossary

Facts already extracted (do not repeat):
$facts

Numbered paragraphs:
$paragraphs

Return a single JSON object:
{"facts": [{"statement": "...", "concept": "<id>", "direction": 1, "cites": [1, 3], "evidence": "..."}]}
