# UK market (London Stock Exchange, XLON)

## Research scope

- Universe: current constituents of the FTSE 100, 100 stocks in this dataset (99 in the mining dataset; current constituents only, so the data has survivorship bias).
- Benchmark: FTSE 100 Index (`IDX_FTSE`).
- Currency: pound sterling (GBP). Most stocks are quoted in pence (GBp) at the source and have been divided by 100 to GBP; CPG.L and IHG.L are quoted in USD and MTLN.L in EUR, and have been converted to GBP at the daily exchange rate.
- Data: daily bars, adjusted prices, from 2015-01-02. Factor mining may only use data up to 2023-12-31.

## Trading rules

- Trading hours (UK time, UTC+0 in winter, UTC+1 in summer): 08:00–16:30, with opening and closing auctions.
- No daily price limits; large price moves trigger a short auction call.
- Buying shares incurs 0.5% stamp duty (example value, pending compliance confirmation); selling does not, so trading costs are asymmetric.
- Settlement: currently T+2; moves to T+1 on 11 October 2027 (same date as the EU and Switzerland).
- Ex-dividend dates normally fall on Thursdays, with the record date on the next business day, usually a Friday (LSE Dividend Procedure Timetable).

## Market structure

- The FTSE 100 is dominated by multinationals that earn most of their revenue outside the UK. When sterling weakens, their overseas revenue is worth more in GBP and the index often benefits; the index is only loosely linked to the domestic UK economy.
- Sector concentration: energy (e.g. Shell, BP), mining (e.g. Rio Tinto, Anglo American), banks (e.g. HSBC, Barclays, Lloyds), pharmaceuticals (e.g. AstraZeneca, GSK) and consumer staples (e.g. Unilever, Diageo) carry large weights; commodity prices have a visible effect on the index.
- FTSE 100 constituents are reviewed quarterly.
- Some constituents are incorporated outside the UK.

## Features relevant to factor research

- The 2016 Brexit referendum and the 2020 pandemic both caused clear sector rotations.
- Stamp duty applies only on purchases, so frequent rebalancing is costly.
- Company announcements are published in English.
