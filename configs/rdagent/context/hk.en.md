# Hong Kong market (HKEX, XHKG)

## Research scope

- Universe: current constituents of the Hang Seng Index, 84 stocks in this dataset (current constituents only, so the data has survivorship bias).
- Benchmark: Hang Seng Index (`IDX_HSI`).
- Currency: Hong Kong dollar (HKD), pegged to the US dollar within a 7.75–7.85 band.
- Data: daily bars, adjusted prices, from 2015-01-02. Factor mining may only use data up to 2023-12-31.

## Trading rules

- Trading hours (Hong Kong time, UTC+8, no daylight saving): 09:30–12:00 and 13:00–16:00, plus a pre-opening session and a closing auction session.
- No daily price limits. Some stocks are subject to the Volatility Control Mechanism (a cooling-off period when the price moves too far within 5 minutes).
- Board lot size differs by stock.
- Only designated securities may be sold short.
- Stamp duty: 0.1% on both buyer and seller (example value, pending compliance confirmation).
- Settlement: T+2.
- Severe weather: before 23 September 2024, trading was suspended under typhoon signal No. 8 or above and black rainstorm warnings (for example, three full-day closures in 2016–2017, already removed from this dataset using the exchange calendar). Since 23 September 2024, Severe Weather Trading applies: the securities market, including Stock Connect, trades normally in these conditions and settlement is no longer postponed.

## Market structure

- Mainland China companies (H shares, red chips and other companies operating mainly in mainland China) make up a large share of the index. Many report financials in RMB, and their results are closely tied to the mainland economy and policy.
- Sector concentration: technology and internet (e.g. Tencent, Alibaba, Meituan), financials (e.g. HSBC, AIA), property and utilities (e.g. Sun Hung Kai Properties, CLP) carry large weights.
- Diverse investor base: besides local and international investors, mainland investors trade some constituents through Stock Connect (southbound flows). Southbound trading pauses during mainland holidays.
- Trading hours largely overlap with mainland A-shares, so mainland market and policy news is reflected the same day. Information from the US close is reflected the next morning.

## Features relevant to factor research

- Between 2015 and 2021 there were several market-wide swings driven by mainland policy, regulation and geopolitics; market-wide moves often dominate stock-specific effects.
- The universe has only 84 stocks and is concentrated by sector, so part of the cross-sectional variation comes from sector rotation.
- Company announcements are published in both Chinese and English.
