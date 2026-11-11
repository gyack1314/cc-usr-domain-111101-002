# 运行暴露量分段归算口径

`datapack/exposure.py` 在既有读取与领域原语之上新增一层暴露量计算：后续指标
只能使用**能够证明属于指定运行模式**的时间与距离，不再从日报总里程倒推。
所有输入时间必须为带偏移 ISO8601，窗口由调用者显式给定，可信、未知与排除
时间都在同一窗口 `[query_start, query_end)` 内计算。

## 1. 时间轴与区间

- 全部区间按**左闭右开**处理；窗口右端点上的事件不属于本次计算。
- 无模式记录的时间标为 `unknown`（原因 `NO_MODE_RECORD`），**不默认自动**。
- 同一时刻有多个互相矛盾的模式重叠，该段标为 `unknown`
  （原因 `CONFLICTING_MODE`）；同模式重叠仍按该模式可信处理。
- 停用区间（downtime）优先于一切模式声明：即使模式声称 AUTONOMOUS，
  停用期间也从可用暴露量排除（原因 `DOWNTIME`）。
- 运行日按 `day_offset_minutes` 固定偏移的午夜分组，午夜也是切分点，
  边界点归右侧运行日。

切分点是窗口端点、模式端点、停用端点、里程计端点与运行日午夜的并集，
相邻切分点之间为不可再分的原子分段，每段只归属一种状态、一个模式、
一条里程计边和一个运行日。

## 2. 里程计边的可信判定

同一车辆的里程计按时间排序，同一 epoch 内相邻点构成一条边，复用
`domain.edge_issues` 的判定。只有同时满足以下条件的边才**可信**：

| 条件 | 不满足时的原因 |
| --- | --- |
| 两点属于同一 epoch（epoch 变化绝不相减） | `EDGE_EPOCH_CHANGE` |
| 间隔 `0 < dt <= max_gap_seconds`（不跨长缺口插值） | `EDGE_TIME_GAP` |
| 读数不下降 | `EDGE_COUNTER_REVERSE` |
| 平均速度不超过 `max_speed_mps` | `EDGE_SPEED_OUTLIER` |

- 边先与窗口求交，再在模式、停用、运行日边界上切分后累加。
- 可信边的窗口内增量按切分比例做**十进制定点**线性分摊
  （米，量化到 0.000001，`ROUND_HALF_UP`）；同一边最后一个分段承担
  舍入余量，因此分段距离之和与边的窗口内增量严格相等。
- 不可信边的读数**不进入任何距离累计**（不会出现负里程），其原始增量、
  问题码与拒绝事实保留在 `edge_ledger` 中供审计。
- 分段没有任何边覆盖时原因是 `NO_ODOMETER_COVERAGE`。

## 3. 分段状态与排除原因

每个原子分段的 `status`：

- `TRUSTED`：模式已知且不冲突、不在停用中、被可信边覆盖。其秒数与分摊
  距离进入该模式的可信暴露量。
- `EXCLUDED`：其余全部情况，`reasons` 列出全部命中原因；模式缺失或
  冲突的排除段同时计入 `unknown_seconds`。

车主账恒等式（回归测试强制）：

```
Σ 分段秒数            = 窗口总秒数
可信秒 + 排除秒        = 窗口总秒数
Σ 可信边窗口增量       = Σ 全部分段距离 = Σ 边台账 fragment_meters_sum
运行日维度可信距离之和 = 模式维度可信距离之和 = 车队可信距离
```

## 4. 事件分子与可计率集合

- 窗口外事件进入不可计率集合，原因 `OUTSIDE_QUERY_WINDOW`。
- 窗口内事件按左闭右开定位到分段（边界时刻归右侧区间）：仅当分段为
  `TRUSTED` 时计入该模式、该运行日的分子，并记录所属分段；其余事件
  带着该分段的排除原因进入不可计率集合。
- 比率单位为“次 / 每 100km”，在聚合分母上重算，比率量化到 0.000000001。
- **零距离不给零风险率**：可信距离为 0 时 `rate_per_100km` 为 `null`，
  `rate_status` 为 `ZERO_DISTANCE_DENOMINATOR`；事件仍可正常归属
  （例如 PARKED 零距离静止段）。
- 本层不判断事故责任或安全等级。

## 5. 指纹、幂等与修订

结果包含三个指纹（SHA-256，基于 `storage.canonical` 的稳定 JSON）：

- `input_fingerprint`：四类输入记录内容；
- `caliber_fingerprint`：窗口、上限参数、运行日偏移与引擎版本
  （`config.engine_version`），换上限或改窗口即产生新口径指纹；
- `result_fingerprint`：输入指纹 + 口径指纹 + 汇总总量，旧版结果可凭
  旧参数复算并比对。

同主键同内容的重复记录由 `records.index_rows` 去重并报告在
`duplicates` 中，重复投递不会让暴露量翻倍；同主键不同内容报为
`conflicts` 并拒绝加工。结果可通过 `storage.RevisionStore` 提交到修订
目录，同内容幂等，新旧口径以修订号共存。

## 6. 输出结构（摘要）

```
compute_exposure(rows, query_start, query_end,
                 max_gap_seconds=10, max_speed_mps=30, day_offset_minutes=0)
├── config / query_start / query_end / 三个指纹
├── vehicles[]
│   ├── 总秒/可信秒/未知秒/排除秒、可信距离/排除距离
│   ├── exclusion_reason_seconds            # 每个排除原因占多少秒
│   ├── mode_summaries[]                    # 车 × 运行日 × 模式
│   ├── segments[]                          # 原子分段 + 切分比例分摊距离 + 源边
│   ├── edge_ledger[]                       # 构成分母的源边、问题码、守恒和
│   ├── rateable_events[] / non_rateable_events[]
└── totals：by_vehicle / by_day / by_mode / fleet（含比率与 rate_status）
```

所有 Decimal 在输出中均为定点字符串，可直接 JSON 持久化与重新读取。
命令行入口：`python -m datapack.cli exposure <bundle> --start ... --end ...
[--max-gap --max-speed --day-offset --output]`。
