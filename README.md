# AlpacaPlatform

QuantStrategyLab 的 Alpaca 执行边界。它分为两个互不混用的阶段：

- P5 `SHADOW`：只生成虚拟账本回执，不连接券商、不读取凭据、不提交订单。
- P4 `PAPER_DRY_RUN`：未来只会使用独立 paper endpoint 与独立 paper 凭据；不能从 P5 或市场数据配置推断资格。

当前实现仅包含 P5 的 `qsl.tqqq_shadow_cycle_input.v1` 和
`qsl.tqqq_shadow_ledger_receipt.v1`。每个输入固定候选、策略 revision、P1/P2/P3
摘要、风险控制摘要与目标权重（basis points）；输出记录相对上一回执的**虚拟**权重变化。
它不保存市场数据、价格、金额、账户、订单、broker URL 或任何凭据。

P5 receipt 不是 P4/P6 权限。实际调度器只能消费已验签且未过期的独立 P5 policy；P6
仍由所有者明确决定。

```bash
python -m alpaca_platform.shadow_ledger --input cycle.json --output receipt.json
```

输出采用 create-only 写入，避免无意覆盖已有 receipt。运行测试：

```bash
python -m pytest -q
```
Bounded Alpaca paper and shadow execution gateway for QuantStrategyLab
