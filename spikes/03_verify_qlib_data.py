"""验证三套 Qlib 数据集：能被 qlib 加载，读出的价格与标准化表一致，Alpha158 表达式可计算（Spec DATA-08 验收）。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python spikes/03_verify_qlib_data.py
"""
import numpy as np
import pandas as pd
import qlib
from qlib.data import D

CHECKS = {"hk": (["0700.HK", "0005.HK"], "hsi"), "uk": (["HSBA.L", "CPG.L"], "ftse100"), "fr": (["MC.PA", "AI.PA"], "cac40")}

for market, (symbols, pool) in CHECKS.items():
    qlib.init(provider_uri=f"/workspace/runtime/qlib_data/{market}", region="us", kernels=1)
    std = pd.read_parquet(f"/workspace/runtime/data/standardized/{market}.parquet")
    std["date"] = pd.to_datetime(std["date"])
    print(f"== {market}: 股票池 {pool} {len(D.instruments(pool) and D.list_instruments(D.instruments(pool), as_list=True))} 只，"
          f"日历 {len(D.calendar())} 天")
    for s in symbols:
        code = s.replace(".", "_")
        q = D.features([code], ["$close", "$volume"]).droplevel(0)
        src = std[std.symbol == s].set_index("date")[["close", "volume"]]
        joined = q.join(src, how="inner")
        max_diff = float(np.nanmax(np.abs(joined["$close"] - joined["close"]) / joined["close"]))
        print(f"  {s}: Qlib 读出 {len(q)} 行，与标准化表比对 {len(joined)} 行，收盘价最大相对误差 {max_diff:.1e}")
    # 几个 Alpha158 中的表达式
    exprs = {"KMID": "($close-$open)/$open", "ROC20": "Ref($close, 20)/$close", "STD20": "Std($close, 20)/$close",
             "CORR10": "Corr($close, Log($volume+1), 10)"}
    f = D.features(D.instruments(pool), list(exprs.values()), start_time="2024-01-01", end_time="2024-12-31")
    f.columns = list(exprs)
    print(f"  Alpha158 示例因子（2024 年）: {f.shape[0]} 行，非空比例 " +
          ", ".join(f"{k}={f[k].notna().mean():.0%}" for k in exprs))
    bench = D.features(["IDX_" + {"hk": "HSI", "uk": "FTSE", "fr": "FCHI"}[market]], ["$close"]).droplevel(0)
    print(f"  基准指数: {len(bench)} 行，最新 {bench['$close'].iloc[-1]:.2f}")
