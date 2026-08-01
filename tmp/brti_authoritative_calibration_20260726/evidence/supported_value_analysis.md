# Supported authoritative BRTI calibration

Status: **DISCOVERY_RESULT** (discovery only).

- Pre-value supported checkpoints: `854`
- Kernel checkpoints analyzed: `848`
- Paired causal-book checkpoints: `213`
- Unique calibration markets: `284`
- Kernel Brier: `0.071884105`
- Market-mid Brier: `0.097696973`
- Kernel minus market Brier: `0.0024102757`
- Kernel log loss: `0.2331112718`
- Market-mid log loss: `0.3088931678`
- Kernel minus market log loss: `0.0095959933`

Negative paired differences favor the kernel; positive differences favor the market mid. Per-horizon results and market-cluster 95% confidence intervals are in the JSON.

## 1/2/5/10-second lead

**Blocked and reported separately.** Historical CF rows do not contain their original Kalshi receipt timestamp, so BRTI source time cannot be mixed with book receive time for causal markouts.

This audit does not identify queue fills, opposite-leg completion, forced exits, or realizable market-making PnL, and does not authorize deployment.
