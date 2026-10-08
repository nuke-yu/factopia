# French market (Euronext Paris, XPAR)

## Research scope

- Universe: current constituents of the CAC 40, 40 stocks in this dataset (current constituents only, so the data has survivorship bias).
- Benchmark: CAC 40 Index (`IDX_FCHI`).
- Currency: euro (EUR).
- Data: daily bars, adjusted prices, from 2015-01-02. Factor mining may only use data up to 2023-12-31.

## Trading rules

- Trading hours (Paris time, UTC+1 in winter, UTC+2 in summer): 09:00–17:30, with opening and closing auctions.
- No daily price limits; prices outside preset thresholds trigger a short trading halt.
- Buying shares of French companies with a market capitalisation above EUR 1 billion incurs the French financial transaction tax (example value 0.4%, pending compliance confirmation); constituents incorporated outside France are not subject to it.
- Short selling is allowed, but positions above set thresholds must be disclosed (EU Short Selling Regulation).
- Settlement: currently T+2; the EU plans to move to T+1 on 11 October 2027.
- Most companies pay one dividend a year, concentrated after the spring and summer annual general meetings.

## Market structure

- Only 40 stocks, so the index is concentrated and a single large company has a big influence.
- Sector concentration: luxury and consumer (e.g. LVMH, Hermès, Kering, L'Oréal), aerospace and industrials (e.g. Airbus, Safran, Schneider Electric), energy (TotalEnergies), banks and insurance (e.g. BNP Paribas, Crédit Agricole, AXA), pharmaceuticals (Sanofi).
- Luxury companies' results are closely tied to Chinese and global high-end consumer demand.
- Some constituents are incorporated outside France (e.g. Airbus, Stellantis, STMicroelectronics).
- Trading hours almost fully overlap with London; the market is sensitive to European Central Bank policy and euro-area economic data.

## Features relevant to factor research

- With only 40 stocks, each day's cross-sectional correlation is computed from 40 observations, so results are noisy; longer samples or cross-market validation are needed.
- Company announcements are mainly in French, with English versions for large companies (available in the AMF open data).
