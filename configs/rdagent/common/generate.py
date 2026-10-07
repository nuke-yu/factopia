# Factopia：替换 RD-Agent v1.0.0 的 factor_data_template/generate.py（所有市场共用）。
# 原版问题：调试数据取"前 100 只股票在 2018–2019 年的数据"，若其中有股票在该时段没有数据
#（如 2019 年后才上市的港股），pandas 抛 KeyError，调试数据生成失败；而模板目录中残留的旧文件
# 会被继续使用，导致调试数据与正式数据不属于同一市场。
# 修改：调试数据只选在调试时段内有数据的股票；其余逻辑与原版一致。
import qlib

qlib.init(provider_uri="~/.qlib/qlib_data/cn_data")

from qlib.data import D

instruments = D.instruments()
fields = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
data = D.features(instruments, fields, freq="day").swaplevel().sort_index().loc["2008-12-29":].sort_index()

data.to_hdf("./daily_pv_all.h5", key="data")

debug = D.features(instruments, fields, start_time="2018-01-01", end_time="2019-12-31", freq="day").swaplevel().sort_index()
available = set(debug.index.get_level_values("instrument"))
chosen = [i for i in data.reset_index()["instrument"].unique() if i in available][:100]
debug = debug.swaplevel().loc[chosen].swaplevel().sort_index()

debug.to_hdf("./daily_pv_debug.h5", key="data")
