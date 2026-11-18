You rewrite a search query for a cross-lingual announcement search engine (Factopia WF-04). The engine matches glossary terms and literal keywords against original announcement paragraphs (French, English, or Chinese) and their machine translations. Expand the planner's query into a short query plus multilingual term variants and concept ids, so the search hits the same risk thread expressed in different languages and phrasings.

Query to expand: $query

Risk concepts (ids and names):
$glossary

Rules:
1. query: the cleaned, short query itself (max 100 characters) — keep the original meaning.
2. terms: multilingual keyword variants (max 8, each max 40 characters) that capture the same risk thread, e.g. English, French, Chinese phrasings and common synonyms.
3. concepts: only concept ids from the glossary above that the query is about (max 4); empty list if the query is not about any concept.
4. Do not add concepts or terms the query does not imply.
5. Write query in $language; terms can be in any language.

Return a single JSON object:
{"query": "...", "terms": ["..."], "concepts": ["..."]}
