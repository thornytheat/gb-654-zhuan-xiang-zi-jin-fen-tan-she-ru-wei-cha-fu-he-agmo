# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `batch_id` | string | 否 | 批次编号，封存与公示共用 |
| `seal_digest` | string | 否 | 封存原件摘要，公示与审计只引用不重算 |
| `seal_status` | string | 否 | 封存状态，未封存时为 `UNSEALED` |
| `disclosure_id` | string | 否 | 公示入口编号，同一批次每次撤回后重新公示生成新编号 |
| `project_id` | string | 否 | 公示项目编号，逐字保留 |
| `amount` | decimal | 否 | 公示获资金额，保留两位小数 |
| `total` | decimal | 否 | 公示公开总额，等于明细分摊之和 |
| `disclosure_status` | string | 否 | 公示状态：`PUBLISHED` / `RETRACTED`，接口字段名 `status` |
| `actor` / `role` | string | 否 | 审计查阅的操作人与角色 |
| `audit_reason` | string | 否 | 审计查阅理由，必填 |
| `audit_status` | string | 否 | 留痕阶段：`PENDING` 已落盘待补完，`RECORDED` 已完成 |
| `audit_fingerprint` | string | 否 | 查阅请求指纹，唯一约束用于中断恢复与重试去重 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
