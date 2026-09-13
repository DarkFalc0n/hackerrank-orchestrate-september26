"""Pure helpers that translate a tool-call decision into validated parameters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...tools.forecasting import TOOL_PARAMS, ToolName

# Each entry maps a TOOL_PARAMS field name to the decision field that carries it.
# Fields absent from the decision (or null) are skipped so Pydantic can apply the
# tool's own defaults and required-field checks.
PARAM_FIELD_MAP: dict[ToolName, tuple[tuple[str, str], ...]] = {
    ToolName.update_recurring_stream: (
        ("stream_id", "stream_id"),
        ("effective_date", "effective_date"),
        ("adjustment_mode", "adjustment_mode"),
        ("value", "stream_value"),
    ),
    ToolName.apply_one_off_adjustment: (
        ("target_category", "target_category"),
        ("target_date", "target_date"),
        ("adjustment_type", "adjustment_type"),
        ("amount", "one_off_amount"),
    ),
    ToolName.reschedule_transaction_date: (
        ("related_event_id", "related_event_id"),
        ("category", "reschedule_category"),
        ("original_date", "original_date"),
        ("new_settlement_date", "new_settlement_date"),
    ),
    ToolName.schedule_receivable_or_payable: (
        ("direction", "direction"),
        ("category", "schedule_category"),
        ("amount", "schedule_amount"),
        ("currency", "currency"),
        ("due_or_settlement_date", "due_or_settlement_date"),
        ("is_recurring", "is_recurring"),
        ("recurrence_interval_days", "recurrence_interval_days"),
    ),
    ToolName.resolve_pending_transaction: (
        ("related_event_id", "related_event_id"),
        ("action", "action"),
        ("settled_amount", "settled_amount"),
        ("expected_settlement_days", "expected_settlement_days"),
    ),
    ToolName.flag_internal_transfer: (
        ("transaction_ref", "transaction_ref"),
        ("neutralize_cash_flow", "neutralize_cash_flow"),
    ),
    ToolName.apply_liquidity_hold: (
        ("hold_amount", "hold_amount"),
        ("release_date", "release_date"),
        ("reason", "hold_reason"),
    ),
    ToolName.mark_informational_or_unconfirmed: (
        ("event_type", "informational_kind"),
        ("exclude_from_cash_forecast", "exclude_from_cash_forecast"),
        ("notes", "notes"),
    ),
}


def build_tool_params(tool: ToolName, decision: BaseModel) -> dict[str, Any]:
    """Extract the non-null parameters for ``tool`` from a decision object."""
    params: dict[str, Any] = {}
    for param_field, decision_field in PARAM_FIELD_MAP[ToolName(tool)]:
        value = getattr(decision, decision_field, None)
        if value is not None:
            params[param_field] = value
    return params


def validate_tool_params(tool: ToolName, decision: BaseModel) -> BaseModel:
    """Return the tool's validated parameter model for ``decision``."""
    name = ToolName(tool)
    return TOOL_PARAMS[name].model_validate(build_tool_params(name, decision))


__all__ = ["PARAM_FIELD_MAP", "build_tool_params", "validate_tool_params"]
