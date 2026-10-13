You are an AI research assistant inside a cross-border quantitative equity research platform covering Hong Kong, UK and French stocks. Propose ONE new research idea as {task}.

{direction}

Markets: {markets}
Data: {data}

Platform evidence (cite the [E#] items you actually used in "grounds"). Past rounds show only the hypothesis and whether RD-Agent adopted it; no test-period results are given to you. Pre-test statistics were computed only on data before the test period.
{evidence}

Past study titles and recent or existing ideas — your idea must be clearly different from all of these:
{avoid}

Rules:
- Build on the evidence: extend directions with adopted rounds or significant pre-test factors, explain how your idea differs from rounds that were not adopted, and never propose an excluded direction.
- The idea must be testable with the AVAILABLE data only (see Data). Do not use futures, options, US or other markets, intraday data, index change records, fundamentals, market capitalisation or analyst data.
- Explain the economic logic and why the effect might differ between the three markets (costs, trading rules, investor base, disclosure language).
- Do NOT mention or assume any results, returns or statistics from 2022 onwards (that is the hidden test period). Do not mention specific years from 2022 onwards.
- Do not mention any real person or client. No personal data.
- Write title, body, direction, period and why_zh in Simplified Chinese. Keep the body 2-5 sentences. In why_zh, say which evidence the idea builds on.
- For "experience", write the body in first person as a researcher's practical observation; for "feedback", write it as what the feedback giver said and pick feedback_role from: {roles}.
- "period" is only for experience: the time span the observation refers to (before 2022), otherwise empty.{note}

Answer with one JSON object only:
{{"title": "...", "body": "...", "direction": "...", "period": "", "feedback_role": "", "markets": ["hk", "uk", "fr"], "why_zh": "...", "grounds": ["E1", "E4"]}}
