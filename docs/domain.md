# 数据约定

时间使用UTC ISO8601，不接受无偏移时刻。运行日按配置固定偏移时区分组。里程为十进制米，epoch变化表示计数域重置。模式AUTONOMOUS、MANUAL、PARKED与冲突unknown区分，停用区间优先排除。默认最大采样间隔10秒、最大速度30米每秒。

样本为合成资料，包含正常、缺失、冲突等记录；这些现象用于保留业务数据原貌，读取成功不代表领域结果有效。所有计算原语位于datapack/domain.py；调用者负责组合、约束和最终验证。在这些原语之上组合出的运行暴露量口径（可信/未知/排除、定点分摊、事件分子、指纹与修订）见docs/exposure.md，实现位于datapack/exposure.py。

samples/query.json提供本地调用参数；edge_issues和prorate使用解析后的相对秒数，原始时间由带偏移ISO8601表示。
