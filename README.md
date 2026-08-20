# AlpacaPlatform

QuantStrategyLab 的 Alpaca 执行边界。它分为两个互不混用的阶段：

- P5 `SHADOW`：只生成虚拟账本回执，不连接券商、不读取凭据、不提交订单。
- P4 `PAPER_DRY_RUN`：未来只会使用独立 paper endpoint 与独立 paper 凭据；不能从 P5 或市场数据配置推断资格。

当前实现仅包含 P5 的 `qsl.tqqq_shadow_cycle_input.v2` 和
`qsl.tqqq_shadow_ledger_receipt.v2`。每个输入固定 deployment bundle、候选、策略 revision、P1/P2/P3
摘要、风险控制摘要、目标权重（basis points）以及上游
`qsl.gcp_kms_policy_gate_receipt.v1`；输出记录相对上一回执的**虚拟**权重变化。
它不保存市场数据、价格、金额、账户、订单、broker URL 或任何凭据。

P5 receipt 不是 P4/P6 权限。shadow input 会复核上游 policy-gate receipt 的闭合字段、
自校验摘要、`SHADOW` stage、有效窗口以及与风险控制的精确绑定；缺一即拒绝。该 receipt
必须由隔离的 KMS 验签 gate 生成并交付给调度器，本模块不会验签、签发 policy 或把自校验
摘要误作独立授权。P6 仍由所有者明确决定。

此前的 v1 仅为未启用的纯账本原型，未签发任何真实 receipt；v2 因此不保留可绕过
policy-gate receipt 的兼容入口。

UESP 只负责产生 `qsl.tqqq-forward-observation.v1`。本仓的适配器再把它与独立的
policy-gate receipt、风险摘要和 deployment bundle 摘要组成 v2 shadow input；因此研究层
不需要导入或理解券商、政策签名或运行环境。适配器本身也不会排程或写入账本：

```bash
python -m alpaca_platform.shadow_cycle_input \
  --forward-observation observation.json \
  --policy-gate-receipt policy-gate-receipt.json \
  --risk-control risk-control.json \
  --deployment-bundle-sha256 <sha256> \
  --cycle-id <immutable-id> \
  --produced-at <rfc3339-utc> \
  --output cycle-input.json
```

输出同样采用 create-only 写入。后续 `shadow_ledger` 仍会重新验证整个 v2 input，而不是信任
该适配器的成功输出。

`shadow_scheduler` 现提供了一个**未部署、无副作用**的 P5 控制步骤：如果前向观察、独立
policy-gate receipt、风险摘要、deployment bundle 或前一账本回执缺失/无效，它只会返回
`qsl.tqqq_shadow_scheduler_result.v1` 的 `PARKED` 原因码；只有它们全部有效时，才返回尚未
持久化的虚拟账本回执（`RECEIPT_READY`）。它不排程、不抓取任何上游文件、不写存储、不连接
Alpaca，也不代表 P5 已启用或 P4/P6 已获许可。后续部署会为该纯控制步骤单独接入受限的
工件读取、create-only 回执写入与状态发布。

P5 后置 admission 现为 `qsl.tqqq_shadow_receipt_admission.v2`。它除了 controller 已返回的
`RECEIPT_READY` 结果和已复核的虚拟账本回执，还必须消费一个
`qsl.tqqq_shadow_risk_gate_decision_envelope.v1`。该 envelope 包含 QSL
`qsl.deterministic_risk_gate_decision.v1` 的完整、可重算摘要，以及与本 P5 cycle 的
`cycle_id`、`computed_at` 和 `qsl.forward_observation_risk_control.v1` 三元组绑定。

适配器不导入、不复制或重新执行 `QuantRuntimeSettings` 的风险内核；它只严格检查 QSL decision
的闭合字段、canonical SHA-256、`ALLOW_NEW_RISK`、`CLOSED` breaker 建议、
`manual_reset_required=true` 及上述 P5 绑定。写入的 v2 admission 只保留脱敏的 source
risk-control、risk-gate policy id/version/SHA-256 和 decision SHA-256；不会保留账户、价格、
notional、投影金额或订单材料。

缺少 envelope、结构/摘要无效、cycle/时间/source risk-control 不匹配，或 decision 为
`NEW_RISK_PROHIBITED` 时，`persist_shadow_cycle_outcome` 都在读取或调用存储前返回 `PARKED`；
不会自动 reset breaker。`CreateOnlyShadowReceiptStore` 仍是未来受限工件存储的最小接口：按
`cycle_id` 原子地 create-if-absent，已存在时读取后按 admission digest 对账；digest 不同只返回
`PARKED/receipt_conflict`，绝不覆盖。仓内 `InMemoryShadowReceiptStore` 仅用于确定性测试与本地
回放，不连接文件系统、GCS、GitHub Actions、券商或任何凭据。

未来每个 P5 周期必须先提供完整 forward observation、独立 policy-gate receipt、风险摘要、
deployment bundle 和通过上述 adapter 的 deterministic risk decision，且（如果存在）上一账本回执
必须有效；任一缺失/无效仍由 controller 或 admission 返回 `PARKED`。只有同时满足
`RECEIPT_READY` 与风险决定的 create-only port 才可写入；本次没有添加 cron、环境变量或部署。

这仍不是账户级 shadow 启动：真正启用前，独立 gateway 必须从受限、已对账的快照运行 QSL risk
kernel，生成 per-cycle envelope，并与 policy-gate 身份、工件读取和持久化 adapter 一起部署；AI、
网页、GitHub Actions、策略代码都不能伪造输入或 reset breaker。此仓没有任何此类身份、账户、
券商或网络能力。

```bash
python -m alpaca_platform.shadow_ledger --input cycle.json --output receipt.json
```

输出采用 create-only 写入，避免无意覆盖已有 receipt。运行测试：

```bash
python -m pytest -q
```
Bounded Alpaca paper and shadow execution gateway for QuantStrategyLab
