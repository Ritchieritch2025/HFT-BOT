#!/usr/bin/env python3
"""Fee-only dual-accounting replay for the sealed post-fill discovery run.

The sealed replay remains authoritative for every episode, action, queue,
exit timestamp, and book walk.  This wrapper leaves the generic whole-cent
fee function in place and adds a side-channel calculation of the observed
direct-account centicent fee on the exact same IOC fill slices.

It is deliberately incapable of candidate selection or live trading.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib
import importlib.util
import os
import sys


A1_REPLAY_PATH = os.environ.get(
    "POSTFILL_A1_REPLAY",
    "/tmp/z3_postfill_stopping_receive_clock_sampled_a1skip.py",
)
A1_REPLAY_SHA256 = (
    "b667c811707b7203bc532b5d51accc1ff2e082d2fff284e76b7377e8945d1b09"
)
FEE_RATE = Decimal("0.07")
E4 = Decimal("10000")
CENTICENT_DOLLARS = Decimal("0.0001")
AUDIT_FIELDS = (
    "ioc_fill_slices_e4",
    "ioc_filled_e4",
    "ioc_remaining_e4",
    "gross_pnl_before_ioc_fee_c_exact",
    "old_generic_whole_cent_fee_c_exact",
    "direct_centicent_fee_c_exact",
    "fee_saving_c_exact",
    "old_pnl_reproduced_c",
    "direct_centicent_pnl_c",
)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sealed_replay():
    actual = file_sha256(A1_REPLAY_PATH)
    if actual != A1_REPLAY_SHA256:
        raise RuntimeError(
            "sealed A1 replay SHA mismatch "
            f"expected={A1_REPLAY_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location(
        "sealed_postfill_a1_replay_for_fee_audit",
        A1_REPLAY_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sealed A1 replay")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


R = load_sealed_replay()
F = R.F
B = F.D.B
OriginalCanonicalPolicyRows = F.canonical_policy_rows


def direct_centicent_fee_c_exact(fills):
    """One IOC order fee in cents, summed then ceiled once to $0.0001."""
    with localcontext() as context:
        context.prec = 50
        fee_dollars = Decimal("0")
        for price_e4, qty_e4 in fills:
            price = Decimal(int(price_e4)) / E4
            quantity = Decimal(int(qty_e4)) / E4
            fee_dollars += (
                FEE_RATE
                * quantity
                * price
                * (Decimal("1") - price)
            )
        if fee_dollars == 0:
            return Decimal("0")
        return (
            fee_dollars.quantize(
                CENTICENT_DOLLARS,
                rounding=ROUND_CEILING,
            )
            * Decimal("100")
        )


def gross_pnl_before_fee_c_exact(episode, fills, remaining_e4):
    with localcontext() as context:
        context.prec = 50
        gross = sum(
            (
                (Decimal(int(price_e4)) - Decimal(episode.entry_e4))
                / Decimal("100")
                * (Decimal(int(qty_e4)) / E4)
                for price_e4, qty_e4 in fills
            ),
            Decimal("0"),
        )
        if remaining_e4:
            won = (
                (episode.result == "yes")
                == (episode.held_side == "y")
            )
            settle_e4 = Decimal("10000") if won else Decimal("0")
            gross += (
                (settle_e4 - Decimal(episode.entry_e4))
                / Decimal("100")
                * (Decimal(int(remaining_e4)) / E4)
            )
        return gross


class FeeAuditEpisodeTracker(R.S.SampledEpisodeTracker):
    """Run the sealed tracker and annotate its already-chosen IOC snapshots."""

    def _ioc_snapshot_for_episode(
        self,
        episode,
        scheduled_ts,
        observed_event_ts,
    ):
        snapshot = super()._ioc_snapshot_for_episode(
            episode,
            scheduled_ts,
            observed_event_ts,
        )
        book = self.market_sim.books[episode.held_side]
        fills, filled_e4 = B.walk_book_sell(book, F.D.CLIP_E4)
        remaining_e4 = F.D.CLIP_E4 - filled_e4
        old_fee_c = Decimal(
            str(B.taker_fee_c_per_order(fills) if filled_e4 else 0)
        )
        direct_fee_c = direct_centicent_fee_c_exact(fills)
        gross_c = gross_pnl_before_fee_c_exact(
            episode,
            fills,
            remaining_e4,
        )
        clip = Decimal(str(F.D.CLIP_CT))
        old_pnl = float((gross_c - old_fee_c) / clip)
        direct_pnl = float((gross_c - direct_fee_c) / clip)
        engine_old_pnl = float(snapshot["pnl_c"])
        if abs(old_pnl - engine_old_pnl) > 1e-12:
            raise RuntimeError(
                "old IOC PnL failed exact reconstruction "
                f"episode={episode.episode_id} scheduled={scheduled_ts} "
                f"engine={engine_old_pnl} reconstructed={old_pnl}"
            )
        saving = old_fee_c - direct_fee_c
        if saving < 0 or saving > Decimal("0.99"):
            raise RuntimeError(
                "whole-cent to centicent saving outside [0,0.99]c "
                f"episode={episode.episode_id} saving={saving}"
            )
        snapshot.update(
            {
                "ioc_fill_slices_e4": [
                    [int(price_e4), int(qty_e4)]
                    for price_e4, qty_e4 in fills
                ],
                "ioc_filled_e4": int(filled_e4),
                "ioc_remaining_e4": int(remaining_e4),
                "gross_pnl_before_ioc_fee_c_exact": str(gross_c),
                "old_generic_whole_cent_fee_c_exact": str(old_fee_c),
                "direct_centicent_fee_c_exact": str(direct_fee_c),
                "fee_saving_c_exact": str(saving),
                "old_pnl_reproduced_c": old_pnl,
                "direct_centicent_pnl_c": direct_pnl,
            }
        )
        return snapshot

    def _policy_result(self, episode, name, due):
        row = super()._policy_result(episode, name, due)
        if row is None or row["exit_kind"] != "ioc":
            return row
        snapshot = episode.ioc_snapshots[name]
        for field in AUDIT_FIELDS:
            row[field] = snapshot[field]
        return row


class FeeAuditMarket(R.EtaGuardedSampledMarket):
    """Retain the A1 admission guard; swap only the result-accounting tracker."""

    def __init__(self, ticker, close_us, result, policies, writer):
        super().__init__(ticker, close_us, result, policies, writer)
        self.tracker = FeeAuditEpisodeTracker(self, writer)


def canonical_policy_rows_with_fee_audit(cycles, shadow_rows):
    """Preserve old canonical actions and attach current/oracle fee annotations."""
    by_policy, current, cycle_by_episode, shadow_audit = (
        OriginalCanonicalPolicyRows(cycles, shadow_rows)
    )
    shadow_current = {
        row["episode_id"]: row
        for row in shadow_rows
        if row["policy"] == F.D.CURRENT_POLICY
    }
    for row in by_policy[F.D.CURRENT_POLICY]:
        shadow = shadow_current[row["episode_id"]]
        for field in (
            "exit_kind",
            "exit_ts",
            "pnl_c",
            "capital_time_s",
        ):
            if row[field] != shadow[field]:
                raise RuntimeError(
                    "canonical current differs from shadow during fee audit "
                    f"episode={row['episode_id']} field={field} "
                    f"canonical={row[field]} shadow={shadow[field]}"
                )
        if row["exit_kind"] == "ioc":
            for field in AUDIT_FIELDS:
                row[field] = shadow[field]

    indexed = {
        policy: {
            row["episode_id"]: row
            for row in rows
        }
        for policy, rows in by_policy.items()
        if policy != F.D.ORACLE_POLICY
    }
    for oracle in by_policy[F.D.ORACLE_POLICY]:
        source = indexed[oracle["selected_source_policy"]][
            oracle["episode_id"]
        ]
        if source["exit_kind"] == "ioc":
            for field in AUDIT_FIELDS:
                oracle[field] = source[field]
    return by_policy, current, cycle_by_episode, shadow_audit


F.CausalTrackedMarket = FeeAuditMarket
F.canonical_policy_rows = canonical_policy_rows_with_fee_audit


if __name__ == "__main__":
    F.main()
