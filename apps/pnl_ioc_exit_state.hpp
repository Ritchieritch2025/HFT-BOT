#pragma once
//
// Pure, network-free state machine for the PnL-spine IOC exit probe.
//
// The production adapter supplies exact decoded V2 evidence.  This layer owns
// the mutation cardinality and terminal decision: exactly one POST is possible;
// every later operation is a bounded GET by the known order_id or an exact
// ticker/subaccount position GET.  There is deliberately no list-orders
// fallback and no retry of the POST.

#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace pnl_ioc {

inline constexpr int kMaximumKnownOrderReadAttempts = 3;

enum class PostDisposition {
  Created201,
  UnambiguousRejection,
  Ambiguous,
};

enum class ExitState {
  ReadyTrace,
  RiskResolvedEvidenceNotPublishable,
  BlockedPrePositionUnproven,
  BlockedPostRejected,
  BlockedPostAmbiguousWithoutKnownOrderId,
  BlockedCreateAckInvalid,
  BlockedKnownOrderReadbackMissing,
  BlockedOrderIdentityMismatch,
  BlockedFillOrFeeBinding,
  BlockedPartialOrResidualPosition,
};

inline constexpr std::string_view state_name(ExitState state) {
  switch (state) {
    case ExitState::ReadyTrace:
      return "IOC_EXIT_RECONCILED";
    case ExitState::RiskResolvedEvidenceNotPublishable:
      return "BLOCKED_RISK_RESOLVED_EVIDENCE_NOT_PUBLISHABLE";
    case ExitState::BlockedPrePositionUnproven:
      return "BLOCKED_EXACT_PRE_POSITION_UNPROVEN";
    case ExitState::BlockedPostRejected:
      return "BLOCKED_IOC_POST_REJECTED";
    case ExitState::BlockedPostAmbiguousWithoutKnownOrderId:
      return "BLOCKED_AMBIGUOUS_IOC_WITHOUT_KNOWN_ORDER_ID";
    case ExitState::BlockedCreateAckInvalid:
      return "BLOCKED_IOC_CREATE_ACK_INVALID";
    case ExitState::BlockedKnownOrderReadbackMissing:
      return "BLOCKED_KNOWN_ORDER_READBACK_INCOMPLETE";
    case ExitState::BlockedOrderIdentityMismatch:
      return "BLOCKED_KNOWN_ORDER_IDENTITY_MISMATCH";
    case ExitState::BlockedFillOrFeeBinding:
      return "BLOCKED_IOC_FILL_OR_FEE_BINDING";
    case ExitState::BlockedPartialOrResidualPosition:
      return "BLOCKED_PARTIAL_FILL_OR_RESIDUAL_POSITION";
  }
  return "BLOCKED_UNKNOWN_IOC_STATE";
}

struct ExitPlan {
  std::string ticker;
  std::string client_order_id;
  std::string book_side;
  std::string outcome_side;
  std::string price_limit_semantics;
  std::int64_t expected_position_before_e4 = 0;
  std::int64_t quantity_e4 = 0;
  std::int64_t limit_price_e4 = 0;
  std::int64_t maximum_cash_loss_e6 = 0;
  std::int64_t maximum_fee_e6 = 0;
  bool exact_pretrade_fee_bound = false;
  std::int64_t pretrade_maximum_fee_e6 = 0;
  std::int64_t subaccount = 0;
};

struct PositionEvidence {
  bool exact = false;
  std::int64_t position_e4 = 0;
  std::string response_sha256;
};

struct CreateEvidence {
  PostDisposition disposition = PostDisposition::Ambiguous;
  bool mutation_attempted = false;
  long http_status = 0;
  bool exact_ack = false;
  std::string order_id;
  std::string client_order_id;
  std::int64_t fill_count_e4 = 0;
  std::int64_t remaining_count_e4 = 0;
  std::int64_t matching_engine_ts_ms = 0;
  std::optional<std::int64_t> average_fee_paid_e6;
  std::optional<std::int64_t> average_fill_price_e4;
  int average_fee_precision_digits = 0;
  int average_fill_price_precision_digits = 0;
  std::string response_sha256;
};

struct OrderEvidence {
  bool exact = false;
  std::string order_id;
  std::string client_order_id;
  std::string ticker;
  std::string book_side;
  std::string outcome_side;
  std::string status;
  std::int64_t subaccount = -1;
  std::int64_t requested_e4 = 0;
  std::int64_t filled_e4 = 0;
  std::int64_t remaining_e4 = 0;
  std::int64_t taker_fill_cost_e6 = 0;
  std::int64_t maker_fill_cost_e6 = 0;
  std::int64_t total_fill_cost_e6 = 0;
  std::int64_t taker_fee_e6 = 0;
  std::int64_t maker_fee_e6 = 0;
  std::int64_t total_fee_e6 = 0;
  std::string response_sha256;
};

struct RecoveryFrame {
  std::optional<OrderEvidence> order;
  std::optional<PositionEvidence> position;
};

struct ExitResult {
  ExitState state = ExitState::BlockedKnownOrderReadbackMissing;
  int ioc_post_attempts = 0;
  int known_order_read_attempts = 0;
  bool account_mutations_locked = true;
  bool publishable_latency_sample = false;
  bool exact_create_ack = false;
  bool exact_order_readback = false;
  bool exact_post_position = false;
  std::string known_order_id;
  std::optional<OrderEvidence> terminal_order;
  std::optional<PositionEvidence> terminal_position;
  CreateEvidence create;
};

class Transport {
 public:
  virtual ~Transport() = default;
  virtual std::optional<PositionEvidence> get_exact_position(
      std::string_view ticker, std::int64_t subaccount) = 0;
  virtual CreateEvidence post_reduce_only_ioc(const ExitPlan& plan) = 0;
  virtual RecoveryFrame get_known_order_and_position(
      std::string_view order_id, std::string_view ticker,
      std::int64_t subaccount) = 0;
};

inline bool checked_abs(std::int64_t value, std::int64_t& result) {
  if (value == std::numeric_limits<std::int64_t>::min()) return false;
  result = value < 0 ? -value : value;
  return true;
}

inline bool checked_product_div(
    std::int64_t left, std::int64_t right, std::int64_t divisor,
    std::int64_t& result) {
  if (left < 0 || right < 0 || divisor <= 0) return false;
  if (left != 0 &&
      right > std::numeric_limits<std::int64_t>::max() / left)
    return false;
  const std::int64_t product = left * right;
  if (product % divisor != 0) return false;
  result = product / divisor;
  return true;
}

inline bool checked_product(
    std::int64_t left, std::int64_t right, std::int64_t& result) {
  if (left < 0 || right < 0) return false;
  if (left != 0 &&
      right > std::numeric_limits<std::int64_t>::max() / left)
    return false;
  result = left * right;
  return true;
}

inline bool average_within_official_rounding_interval(
    std::int64_t reported_average_e6, int precision_digits,
    std::int64_t quantity_e4, std::int64_t aggregate_e6) {
  if (reported_average_e6 < 0 || precision_digits < 1 ||
      precision_digits > 6 || quantity_e4 <= 0 || aggregate_e6 < 0)
    return false;
  static constexpr std::int64_t powers_of_ten[] = {
      1'000'000, 100'000, 10'000, 1'000, 100, 10, 1};
  const std::int64_t unit_e6 = powers_of_ten[precision_digits];
  std::int64_t aggregate_scaled = 0;
  std::int64_t average_scaled = 0;
  std::int64_t tolerance_scaled = 0;
  if (!checked_product(aggregate_e6, 10'000, aggregate_scaled) ||
      !checked_product(
          reported_average_e6, quantity_e4, average_scaled) ||
      !checked_product(unit_e6, quantity_e4, tolerance_scaled))
    return false;
  const std::int64_t difference =
      aggregate_scaled >= average_scaled
          ? aggregate_scaled - average_scaled
          : average_scaled - aggregate_scaled;
  // Compare 2*error <= one displayed unit, avoiding a floating midpoint.
  return difference <= tolerance_scaled / 2;
}

inline bool exact_plan(const ExitPlan& plan) {
  std::int64_t absolute_position = 0;
  if (plan.ticker.empty() || plan.client_order_id.empty() ||
      (plan.book_side != "bid" && plan.book_side != "ask") ||
      (plan.outcome_side != "yes" && plan.outcome_side != "no") ||
      plan.expected_position_before_e4 == 0 ||
      !checked_abs(plan.expected_position_before_e4, absolute_position) ||
      absolute_position != plan.quantity_e4 || plan.quantity_e4 <= 0 ||
      plan.quantity_e4 % 100 != 0 ||
      plan.limit_price_e4 <= 0 || plan.limit_price_e4 >= 10'000 ||
      plan.maximum_cash_loss_e6 <= 0 || plan.maximum_fee_e6 <= 0 ||
      !plan.exact_pretrade_fee_bound ||
      plan.pretrade_maximum_fee_e6 < 0 ||
      plan.pretrade_maximum_fee_e6 > plan.maximum_fee_e6 ||
      plan.subaccount != 0 ||
      plan.maximum_fee_e6 > plan.maximum_cash_loss_e6)
    return false;
  if (plan.expected_position_before_e4 < 0) {
    if (plan.book_side != "bid" || plan.outcome_side != "yes" ||
        plan.price_limit_semantics != "MAXIMUM_BUY_YES_PRICE_CAP")
      return false;
  } else if (
      plan.book_side != "ask" || plan.outcome_side != "no" ||
      plan.price_limit_semantics != "MINIMUM_SELL_YES_PRICE_FLOOR") {
    return false;
  }
  if (plan.expected_position_before_e4 < 0) {
    std::int64_t maximum_fill_cost_e6 = 0;
    if (!checked_product_div(
            plan.limit_price_e4, plan.quantity_e4, 100,
            maximum_fill_cost_e6) ||
        maximum_fill_cost_e6 >
            plan.maximum_cash_loss_e6 -
                plan.pretrade_maximum_fee_e6)
      return false;
  }
  return true;
}

inline bool exact_create_ack(
    const ExitPlan& plan, const CreateEvidence& create) {
  return create.disposition == PostDisposition::Created201 &&
         create.http_status == 201 && create.exact_ack &&
         !create.order_id.empty() &&
         create.client_order_id == plan.client_order_id &&
         create.fill_count_e4 == plan.quantity_e4 &&
         create.remaining_count_e4 == 0 &&
         create.matching_engine_ts_ms > 0 &&
         create.average_fee_paid_e6.has_value() &&
         create.average_fill_price_e4.has_value() &&
         create.average_fee_precision_digits >= 1 &&
         create.average_fee_precision_digits <= 6 &&
         create.average_fill_price_precision_digits == 4 &&
         *create.average_fee_paid_e6 >= 0 &&
         *create.average_fill_price_e4 > 0 &&
         *create.average_fill_price_e4 < 10'000;
}

inline bool exact_order_identity(
    const ExitPlan& plan, std::string_view known_order_id,
    const OrderEvidence& order) {
  return order.exact && order.order_id == known_order_id &&
         order.client_order_id == plan.client_order_id &&
         order.ticker == plan.ticker &&
         order.book_side == plan.book_side &&
         order.outcome_side == plan.outcome_side &&
         order.subaccount == plan.subaccount;
}

inline bool exact_full_fill_binding(
    const ExitPlan& plan, const CreateEvidence& create,
    const OrderEvidence& order) {
  if (order.requested_e4 != plan.quantity_e4 ||
      order.filled_e4 != plan.quantity_e4 ||
      order.remaining_e4 != 0 ||
      order.status != "executed" ||
      order.taker_fill_cost_e6 < 0 ||
      order.maker_fill_cost_e6 < 0 ||
      order.total_fill_cost_e6 < 0 || order.total_fee_e6 < 0 ||
      order.taker_fee_e6 < 0 || order.maker_fee_e6 < 0 ||
      order.maker_fill_cost_e6 != 0 || order.maker_fee_e6 != 0 ||
      !create.average_fill_price_e4 ||
      !create.average_fee_paid_e6 ||
      order.total_fee_e6 > plan.maximum_fee_e6 ||
      order.total_fee_e6 > plan.pretrade_maximum_fee_e6)
    return false;

  if (order.taker_fill_cost_e6 >
          std::numeric_limits<std::int64_t>::max() -
              order.maker_fill_cost_e6 ||
      order.taker_fill_cost_e6 + order.maker_fill_cost_e6 !=
          order.total_fill_cost_e6 ||
      order.taker_fee_e6 >
          std::numeric_limits<std::int64_t>::max() -
              order.maker_fee_e6 ||
      order.taker_fee_e6 + order.maker_fee_e6 != order.total_fee_e6)
    return false;

  // Get Order aggregates are cash truth.  Create returns a displayed
  // per-contract average; bind it to the aggregate using half of one unit at
  // the response field's official display precision.
  if (!average_within_official_rounding_interval(
          *create.average_fill_price_e4 * 100,
          create.average_fill_price_precision_digits,
          plan.quantity_e4, order.total_fill_cost_e6) ||
      !average_within_official_rounding_interval(
          *create.average_fee_paid_e6,
          create.average_fee_precision_digits,
          plan.quantity_e4, order.total_fee_e6))
    return false;

  std::int64_t aggregate_scaled = 0;
  std::int64_t limit_scaled = 0;
  if (!checked_product(
          order.total_fill_cost_e6, 100, aggregate_scaled) ||
      !checked_product(plan.limit_price_e4, plan.quantity_e4, limit_scaled))
    return false;
  const bool bid_exit = plan.expected_position_before_e4 < 0;
  if (bid_exit) {
    if (aggregate_scaled > limit_scaled ||
        order.total_fill_cost_e6 >
            plan.maximum_cash_loss_e6 - order.total_fee_e6)
      return false;
  } else if (
      aggregate_scaled < limit_scaled ||
      order.total_fee_e6 > plan.maximum_cash_loss_e6) {
    return false;
  }
  return true;
}

inline ExitResult run(Transport& transport, const ExitPlan& plan) {
  ExitResult result;
  if (!exact_plan(plan)) {
    result.state = ExitState::BlockedPrePositionUnproven;
    return result;
  }

  const auto position_before =
      transport.get_exact_position(plan.ticker, plan.subaccount);
  if (!position_before || !position_before->exact ||
      position_before->position_e4 != plan.expected_position_before_e4) {
    result.state = ExitState::BlockedPrePositionUnproven;
    return result;
  }

  // This is the only mutating call in the state machine.  There is no retry
  // branch.  A caller must acquire the durable account mutation lock and
  // consume the one-shot authority before entering run().
  result.create = transport.post_reduce_only_ioc(plan);
  result.ioc_post_attempts =
      result.create.mutation_attempted ? 1 : 0;
  result.exact_create_ack = exact_create_ack(plan, result.create);
  result.known_order_id = result.create.order_id;

  if (result.known_order_id.empty()) {
    result.state =
        result.create.disposition == PostDisposition::UnambiguousRejection
            ? ExitState::BlockedPostRejected
            : ExitState::BlockedPostAmbiguousWithoutKnownOrderId;
    return result;
  }

  bool saw_identity_mismatch = false;
  bool saw_terminal_partial_or_residual = false;
  bool saw_fill_or_fee_mismatch = false;
  for (int attempt = 0; attempt < kMaximumKnownOrderReadAttempts; ++attempt) {
    ++result.known_order_read_attempts;
    RecoveryFrame frame = transport.get_known_order_and_position(
        result.known_order_id, plan.ticker, plan.subaccount);
    if (!frame.order || !frame.position ||
        !frame.position->exact)
      continue;
    if (!exact_order_identity(plan, result.known_order_id, *frame.order)) {
      saw_identity_mismatch = true;
      continue;
    }
    result.exact_order_readback = true;
    result.terminal_order = frame.order;
    result.terminal_position = frame.position;
    result.exact_post_position = frame.position->position_e4 == 0;

    const bool order_terminal =
        frame.order->requested_e4 == plan.quantity_e4 &&
        frame.order->filled_e4 >= 0 &&
        frame.order->filled_e4 <= plan.quantity_e4 &&
        frame.order->remaining_e4 >= 0 &&
        frame.order->remaining_e4 <= plan.quantity_e4 &&
        frame.order->remaining_e4 ==
            plan.quantity_e4 - frame.order->filled_e4;
    const bool fully_filled =
        order_terminal &&
        frame.order->filled_e4 == plan.quantity_e4 &&
        frame.order->remaining_e4 == 0 &&
        frame.position->position_e4 == 0;
    if (!fully_filled) {
      if (order_terminal) saw_terminal_partial_or_residual = true;
      continue;
    }

    // A known-order GET can prove that account risk is gone even after a
    // lost/invalid create response.  It cannot reconstruct a causal 201 ack
    // or its exact average fill/fee fields, so that branch stays blocked for
    // latency publication.
    if (!result.exact_create_ack) {
      result.state =
          result.create.disposition == PostDisposition::Created201
              ? ExitState::BlockedCreateAckInvalid
              : ExitState::RiskResolvedEvidenceNotPublishable;
      return result;
    }
    if (!exact_full_fill_binding(plan, result.create, *frame.order)) {
      saw_fill_or_fee_mismatch = true;
      continue;
    }

    result.state = ExitState::ReadyTrace;
    result.publishable_latency_sample = true;
    return result;
  }

  if (saw_fill_or_fee_mismatch) {
    result.state = ExitState::BlockedFillOrFeeBinding;
  } else if (saw_terminal_partial_or_residual) {
    result.state = ExitState::BlockedPartialOrResidualPosition;
  } else if (saw_identity_mismatch) {
    result.state = ExitState::BlockedOrderIdentityMismatch;
  } else if (!result.exact_create_ack &&
             result.create.disposition == PostDisposition::Created201) {
    result.state = ExitState::BlockedCreateAckInvalid;
  } else {
    result.state = ExitState::BlockedKnownOrderReadbackMissing;
  }
  return result;
}

class ScriptedTransport final : public Transport {
 public:
  std::optional<PositionEvidence> pre_position;
  CreateEvidence create;
  std::vector<RecoveryFrame> recovery;
  int position_reads = 0;
  int post_calls = 0;
  int known_order_calls = 0;
  std::vector<std::string> queried_order_ids;

  std::optional<PositionEvidence> get_exact_position(
      std::string_view, std::int64_t) override {
    ++position_reads;
    return pre_position;
  }

  CreateEvidence post_reduce_only_ioc(const ExitPlan&) override {
    ++post_calls;
    return create;
  }

  RecoveryFrame get_known_order_and_position(
      std::string_view order_id, std::string_view,
      std::int64_t) override {
    ++known_order_calls;
    queried_order_ids.emplace_back(order_id);
    const auto index = static_cast<std::size_t>(known_order_calls - 1);
    if (index >= recovery.size()) return {};
    return recovery[index];
  }
};

}  // namespace pnl_ioc
