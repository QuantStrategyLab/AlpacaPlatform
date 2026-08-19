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

```bash
python -m alpaca_platform.shadow_ledger --input cycle.json --output receipt.json
```

输出采用 create-only 写入，避免无意覆盖已有 receipt。运行测试：

```bash
python -m pytest -q
```
Bounded Alpaca paper and shadow execution gateway for QuantStrategyLab
