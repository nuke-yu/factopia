# 数据商参考数据导入模板（Spec 第 14.21 节；BR-16、18、36）

免费来源没有历史成分股变更，也没有可靠的合并、配股、退市、名称变更记录。取得数据商数据（LSEG / Bloomberg / FactSet / 指数公司）后，
导出为下面的格式，在 app 容器中导入。**模板中的代码（XXXX.L 等）只是格式示例，不是真实数据，不要导入。**

## 1. 历史成分股 `membership_template.csv`

| 列 | 说明 |
|---|---|
| symbol | 证券代码：香港 `.HK`、英国 `.L`、法国 `.PA` 结尾，须与市场一致 |
| start | 调入日期 YYYY-MM-DD |
| end | 调出日期 YYYY-MM-DD；仍在指数中留空 |
| note | 可选备注 |

同一股票可有多段（调出后再调入），区间不能重叠。

```
python -m factopia.refdata import-membership --market uk --file /workspace/x.csv --vendor "LSEG" --dry-run   # 先校验
python -m factopia.refdata import-membership --market uk --file /workspace/x.csv --vendor "LSEG"
```

导入后写入 `configs/universe/membership/<市场>.csv`（source = 数据商，原文件备份为 `.csv.bak`），然后依次运行：
`python -m factopia.data.download`（含曾经的成分股）→ `standardize` → `to_qlib`（两种格式）→ `python -m factopia.refdata pit`。
时间点状态变为"一致"后，研究结果不再标注幸存者偏差。

## 2. 公司行动 `actions_template.csv`

| 列 | 说明 |
|---|---|
| symbol | 证券代码（三个市场任一） |
| date | 生效日期 YYYY-MM-DD |
| type | merger 合并/收购、rights_issue 配股、delisting 退市、name_change 名称变更、spin_off 分拆、split 拆股/合股、dividend 分红、special_dividend 特别分红、ticker_change 代码变更 |
| value | 数值：拆股比例、配股比例、每股分红；其余类型可空 |
| detail | 说明 |

```
python -m factopia.refdata import-actions --file /workspace/x.csv --vendor "LSEG" --dry-run
python -m factopia.refdata import-actions --file /workspace/x.csv --vendor "LSEG"
python -m factopia.refdata actions && python -m factopia.refdata pit
```

与免费来源同股票、同日期、同类型的记录以数据商为准；"未覆盖"类型清单按实际导入的类型自动更新；
退市记录会与成分股结束日期对照，不一致的列出来。

任何一行校验失败时整份文件都不写入，并列出行号与原因。
