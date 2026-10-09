"""Alpha158 因子名前缀的含义（英文供 AI 提示词，中文供界面），不依赖 Qlib。"""

# 前缀 → (分组, 英文说明, 中文说明)；分组用于界面上的排序与着色
ALPHA158_FAMILIES = {
    "KMID": ("K 线", "candlestick body relative to open", "K 线实体相对开盘价"),
    "KLEN": ("K 线", "candlestick range relative to open", "K 线振幅相对开盘价"),
    "KUP": ("K 线", "upper shadow", "上影线"),
    "KLOW": ("K 线", "lower shadow", "下影线"),
    "KSFT": ("K 线", "candlestick shift", "收盘在当日区间中的位置"),
    "OPEN": ("价格位置", "today's open relative to close", "开盘价相对收盘价"),
    "HIGH": ("价格位置", "today's high relative to close", "最高价相对收盘价"),
    "LOW": ("价格位置", "today's low relative to close", "最低价相对收盘价"),
    "VWAP": ("价格位置", "today's VWAP relative to close", "成交均价相对收盘价"),
    "ROC": ("动量 / 反转", "past price change: Ref($close, d)/$close", "过去 d 日价格变化"),
    "MA": ("动量 / 反转", "moving average relative to price: Mean($close, d)/$close", "d 日均线相对价格"),
    "BETA": ("趋势", "price trend slope over d days", "d 日价格趋势斜率"),
    "RSQR": ("趋势", "R-squared of the price trend over d days", "d 日趋势的拟合优度"),
    "RESI": ("趋势", "residual of the price trend over d days", "d 日趋势的残差"),
    "STD": ("波动", "price volatility: Std($close, d)/$close", "d 日价格波动率"),
    "MAX": ("价格位置", "highest price over d days relative to close", "d 日最高价相对收盘价"),
    "MIN": ("价格位置", "lowest price over d days relative to close", "d 日最低价相对收盘价"),
    "QTLU": ("价格位置", "upper quantile of price over d days relative to close", "d 日价格上分位数"),
    "QTLD": ("价格位置", "lower quantile of price over d days relative to close", "d 日价格下分位数"),
    "RANK": ("价格位置", "percentile of today's close within d days", "收盘价在 d 日内的分位"),
    "RSV": ("价格位置", "position of close within the d-day high-low range", "收盘价在 d 日高低区间中的位置"),
    "IMAX": ("价格位置", "days since the d-day high", "距 d 日最高点的天数"),
    "IMIN": ("价格位置", "days since the d-day low", "距 d 日最低点的天数"),
    "IMXD": ("价格位置", "gap between days since high and days since low", "高点与低点的时间差"),
    "CORR": ("量价关系", "correlation between price and log volume over d days", "d 日价格与成交量的相关"),
    "CORD": ("量价关系", "correlation between price change and volume change over d days", "d 日涨跌与量变的相关"),
    "CNTP": ("涨跌统计", "share of up days over d days", "d 日上涨天数占比"),
    "CNTN": ("涨跌统计", "share of down days over d days", "d 日下跌天数占比"),
    "CNTD": ("涨跌统计", "share of up days minus share of down days", "上涨减下跌天数占比"),
    "SUMP": ("涨跌统计", "share of total gains over d days", "d 日涨幅占比"),
    "SUMN": ("涨跌统计", "share of total losses over d days", "d 日跌幅占比"),
    "SUMD": ("涨跌统计", "gains minus losses share over d days", "涨幅减跌幅占比"),
    "VMA": ("成交量", "moving average of volume relative to volume", "成交量均线相对当日"),
    "VSTD": ("成交量", "volatility of volume over d days", "d 日成交量波动"),
    "WVMA": ("成交量", "volume-weighted volatility of returns over d days", "成交量加权的收益波动"),
    "VSUMP": ("成交量", "share of volume increases over d days", "d 日放量占比"),
    "VSUMN": ("成交量", "share of volume decreases over d days", "d 日缩量占比"),
    "VSUMD": ("成交量", "volume increases minus decreases share over d days", "放量减缩量占比"),
}
GROUP_ORDER = ["K 线", "价格位置", "动量 / 反转", "趋势", "波动", "量价关系", "涨跌统计", "成交量"]


def family(name: str) -> tuple[str, str, str]:
    """返回 (分组, 英文说明, 中文说明)；按最长前缀匹配，如 VSUMP 优先于 VSUM、KLOW 优先于 LOW。"""
    for prefix in sorted(ALPHA158_FAMILIES, key=len, reverse=True):
        if name.startswith(prefix):
            return ALPHA158_FAMILIES[prefix]
    return ("其他", "Alpha158 price/volume factor", "Alpha158 量价因子")
