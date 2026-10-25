# AlpacaPlatform

English | [简体中文](README.zh-CN.md)

The Alpaca execution boundary for QuantStrategyLab. It has two phases that are never mixed:

- P5 `SHADOW`: produces only virtual ledger receipts, connects to no broker, reads no credentials, and submits no orders.
- P4 `PAPER_DRY_RUN`: will only ever use a separate paper endpoint and separate paper credentials; eligibility cannot be inferred from P5 or from the market data configuration.

The current implementation contains only P5's `qsl.tqqq_shadow_cycle_input.v2` and
`qsl.tqqq_shadow_ledger_receipt.v2`. Each input fixes the deployment bundle, candidate, strategy revision, P1/P2/P3
digests, risk control digest, target weights (basis points), and the upstream
`qsl.gcp_kms_policy_gate_receipt.v1`; the output records the **virtual** weight change relative to the previous receipt.
It stores no market data, prices, amounts, accounts, orders, broker URLs, or any credentials.

A P5 receipt is not P4/P6 permission. The shadow input re-checks the upstream policy-gate receipt's closed fields,
self-check digest, `SHADOW` stage, validity window, and its exact binding to the risk control; if any is missing it
rejects. That receipt must be produced by an isolated KMS verification gate and delivered to the scheduler; this module
does not verify signatures, issue policy, or mistake a self-check digest for independent authorization. P6 requires a
separate maintainer decision.

The earlier v1 was only an unenabled pure-ledger prototype and issued no real receipt; v2 therefore keeps no
compatibility path that could bypass the policy-gate receipt.

## P4 paper admission (local admission contract)

`paper_admission` only builds and validates `qsl.alpaca_paper_admission.v1`; it does not connect to Alpaca, read
credentials, access the network, schedule, or submit orders. The contract must explicitly use `PAPER_DRY_RUN`, and
contains only SHA-256 digests of the paper endpoint, configuration, strategy, deployment, and risk, plus an immutable
`cycle_id` and a UTC validity window; it stores no raw URL, account, order, or credential fields.

The same `cycle_id` with the same content can return `RECONCILED` through `reconcile_paper_admission`; the same cycle
with different content fails closed as a conflict. `LIVE`, `SHADOW`, shadow-receipt escalation, and unknown/sensitive
fields are all rejected. This contract does not mean a paper account is connected, and it grants no P4/P6 execution
permission.

UESP is only responsible for producing `qsl.tqqq-forward-observation.v1`. The adapter in this repo then combines it
with an independent policy-gate receipt, the risk digest, and the deployment bundle digest into the v2 shadow input;
the research layer therefore does not need to import or understand the broker, policy signing, or the runtime
environment. The adapter itself also does not schedule or write to the ledger:

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

The output is likewise written create-only. The later `shadow_ledger` still re-validates the entire v2 input instead of
trusting the adapter's successful output.

`shadow_scheduler` now provides an **undeployed, side-effect-free** P5 control step: if the forward observation, the
independent policy-gate receipt, the risk digest, the deployment bundle, or the previous ledger receipt is missing or
invalid, it only returns the `PARKED` reason code of `qsl.tqqq_shadow_scheduler_result.v1`; only when they are all valid
does it return the not-yet-persisted virtual ledger receipt (`RECEIPT_READY`). It does not schedule, fetch any upstream
file, write storage, or connect to Alpaca, and it does not mean P5 is enabled or that P4/P6 is permitted. A later
deployment will, for this pure control step alone, wire in restricted artifact reads, create-only receipt writes, and
status publication.

P5 post-admission is now `qsl.tqqq_shadow_receipt_admission.v2`. Besides the `RECEIPT_READY` result already returned by
the controller and the re-checked virtual ledger receipt, it must also consume a
`qsl.tqqq_shadow_risk_gate_decision_envelope.v1`. That envelope contains the complete, recomputable digest of the QSL
`qsl.deterministic_risk_gate_decision.v1`, bound to this P5 cycle's `cycle_id`, `computed_at`, and the
`qsl.forward_observation_risk_control.v1` triple.

The adapter does not import, copy, or re-execute the risk kernel of `QuantRuntimeSettings`; it only strictly checks the
QSL decision's closed fields, canonical SHA-256, `ALLOW_NEW_RISK`, `CLOSED` breaker recommendation,
`manual_reset_required=true`, and the P5 binding above. The written v2 admission keeps only the sanitized source
risk-control, the risk-gate policy id/version/SHA-256, and the decision SHA-256; it keeps no account, price, notional,
projected amount, or order material.

When the envelope is missing, the structure/digest is invalid, the cycle/time/source risk-control does not match, or the
decision is `NEW_RISK_PROHIBITED`, `persist_shadow_cycle_outcome` returns `PARKED` before reading or calling storage; it
does not automatically reset the breaker. `CreateOnlyShadowReceiptStore` remains the minimal interface for the future
restricted artifact storage: atomically create-if-absent by `cycle_id`, read and reconcile by admission digest when it
already exists; a different digest only returns `PARKED/receipt_conflict` and never overwrites. The in-repo
`InMemoryShadowReceiptStore` is only for deterministic tests and local replay; it connects to no filesystem, GCS, GitHub
Actions, broker, or any credential.

Each future P5 cycle must first provide a complete forward observation, an independent policy-gate receipt, the risk
digest, the deployment bundle, and a deterministic risk decision through the adapter above, and (if present) the previous
ledger receipt must be valid; if any is missing or invalid, the controller or admission still returns `PARKED`. Only the
create-only port that satisfies both `RECEIPT_READY` and the risk decision may write; this repository adds no cron job,
environment variable, or deployment.

This is still not account-level shadow startup: before it can truly be enabled, an independent gateway must run the QSL
risk kernel from a restricted, reconciled snapshot, produce a per-cycle envelope, and be deployed together with the
policy-gate identity, artifact reads, and the persistence adapter; AI, web, GitHub Actions, and strategy code must not be
able to forge inputs or reset the breaker. This repo has no such identity, account, broker, or network capability.

`p5_default_parked_scheduler` is the next layer, still **undeployed**: a single-cycle orchestration interface. It reads a
`P5ShadowArtifactSnapshot` once from an injected `RestrictedP5ShadowArtifactReader`, which may contain only the forward
observation, the policy receipt already produced by the independent P0 gate, the risk digest, the deployment bundle
digest, the previous P5 receipt, and the QSL deterministic risk-decision envelope. It does not read paths, environment
variables, the network, broker, account, or credentials, and it does not set up cron/Actions/retry loops itself.

The default behavior is `PARKED`: with no reader, snapshot, store, or any upstream artifact, it returns
`qsl.tqqq_p5_default_parked_scheduler_status.v1`, which contains only a stable reason code and a deduplication digest and
writes no receipt. `policy_gate_receipt`, the previous receipt, and the risk envelope are still passed to the existing
controller/admission for strict re-checking; invalid, expired, mismatched, or `NEW_RISK_PROHIBITED` all stay `PARKED`. It
does not issue P0 policy, does not treat a self-check digest as authorization, and has no interface to reset the breaker.

Only when all inputs are valid, the risk envelope is explicitly `ALLOW_NEW_RISK`, and the caller explicitly injects
`CreateOnlyShadowReceiptStore` does it call the existing create-only admission/store. Repeating the same immutable cycle
only yields `RECONCILED`, and the digest can be used by an outer scheduler for deduplication; a different admission for
the same cycle is still `PARKED/receipt_conflict`. The in-repo `InMemoryRestrictedP5ShadowArtifactReader` and
`InMemoryShadowReceiptStore` are only for tests/local replay, not production artifact storage.

Before real deployment, an isolated runtime still needs a restricted read-only artifact reader, an atomic create-only
store, an independent P0/KMS policy gate, the per-cycle envelope produced by the QSL risk kernel, and trusted identity
with audit/alerting. Until these external steps are done, no timed scheduling can be added and this interface cannot be
connected to any paper/live path.

`GcsP5ShadowArtifactReader` and `GcsCreateOnlyShadowReceiptStore` now provide the corresponding **restricted GCS
transport adapters**, but they are still not deployed or auto-instantiated. The caller must explicitly inject a bucket
client already bound to a workload identity; the adapters do not read environment variables, service accounts, or bucket
names. The reader reads only the exact `p5-inputs/<cycle-id>.json` and does not list or guess the "latest" artifact; the
store performs a create-only write with `if_generation_match=0` only for the exact `p5-receipts/<cycle-id>.json`, and
re-reads to reconcile on conflict. It does not delete, overwrite, or produce cron jobs, network configuration, broker
access, or P4/P6 permission. Storage unavailability closes to `PARKED/receipt_store_unavailable`.

```bash
python -m alpaca_platform.shadow_ledger --input cycle.json --output receipt.json
```

The output is written create-only to avoid accidentally overwriting an existing receipt. Run the tests:

```bash
python -m pytest -q
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI assistants and maintainers follow [AGENTS.md](AGENTS.md).
