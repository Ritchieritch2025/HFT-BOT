"""Pricing-model reference implementation (PLAN_PRICING_MODEL, Phase 1.5).

Pure research-grade Python — the executable spec for the Phase-2 C++ port.
Modules: lo (W-P1 log-odds core), fair (W-P2), quote (W-P3). No network, no
DuckDB inside these modules; fixtures feed plain values. All strategy math in
log-odds space (GUARDRAILS Q1); fees via config/kalshi_facts.yaml only (Q3).
"""
