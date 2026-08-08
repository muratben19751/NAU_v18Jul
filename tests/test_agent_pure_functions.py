"""Unit tests for agent.py's pure/near-pure helper functions.

DeepR audit (2026-08-08) found ~90 easily-testable pure functions in agent.py
with zero direct coverage — retry/backoff detection, JSON extraction, spec
clamping/validation. These are cheap to test in isolation (no LLM client, no
network) and catch exactly the kind of silent regression a refactor of this
3900-line file would otherwise sneak past.
"""

from __future__ import annotations

import pytest

from agent import (
    _clamp,
    _extract_json_object,
    _is_rate_limited,
    _repair_exit_return_literals,
    _retry_after_seconds,
    _validate_composed,
    _validate_strategy_options,
)


class TestIsRateLimited:
    def test_status_code_429(self):
        exc = Exception()
        exc.status_code = 429
        assert _is_rate_limited(exc) is True

    def test_code_attribute_429(self):
        exc = Exception()
        exc.code = 429
        assert _is_rate_limited(exc) is True

    def test_rate_limit_error_type_name(self):
        RateLimitError = type("RateLimitError", (Exception,), {})
        assert _is_rate_limited(RateLimitError()) is True

    def test_unrelated_exception_not_rate_limited(self):
        assert _is_rate_limited(ValueError("boom")) is False

    def test_status_code_500_not_rate_limited(self):
        exc = Exception()
        exc.status_code = 500
        assert _is_rate_limited(exc) is False


class TestRetryAfterSeconds:
    def test_no_response_attribute(self):
        assert _retry_after_seconds(Exception("plain")) is None

    def test_retry_after_header_present(self):
        class Resp:
            headers = {"retry-after": "12.5"}

        exc = Exception()
        exc.response = Resp()
        assert _retry_after_seconds(exc) == 12.5

    def test_retry_after_header_capitalized(self):
        class Resp:
            headers = {"Retry-After": "7"}

        exc = Exception()
        exc.response = Resp()
        assert _retry_after_seconds(exc) == 7.0

    def test_non_numeric_header_returns_none(self):
        class Resp:
            headers = {"retry-after": "not-a-number"}

        exc = Exception()
        exc.response = Resp()
        assert _retry_after_seconds(exc) is None

    def test_no_headers_at_all(self):
        class Resp:
            headers = None

        exc = Exception()
        exc.response = Resp()
        assert _retry_after_seconds(exc) is None


class TestClamp:
    def test_within_range_unchanged(self):
        assert _clamp(5.0, 0.0, 10.0, 1.0) == 5.0

    def test_below_range_clamped_to_lo(self):
        assert _clamp(-5.0, 0.0, 10.0, 1.0) == 0.0

    def test_above_range_clamped_to_hi(self):
        assert _clamp(50.0, 0.0, 10.0, 1.0) == 10.0

    def test_non_numeric_returns_default(self):
        assert _clamp("not-a-number", 0.0, 10.0, 3.0) == 3.0

    def test_none_returns_default(self):
        assert _clamp(None, 0.0, 10.0, 3.0) == 3.0

    def test_nan_returns_default(self):
        assert _clamp(float("nan"), 0.0, 10.0, 3.0) == 3.0

    def test_string_number_coerced(self):
        assert _clamp("7.5", 0.0, 10.0, 1.0) == 7.5


class TestExtractJsonObject:
    def test_plain_json(self):
        assert _extract_json_object('{"a": 1}') == '{"a": 1}'

    def test_json_with_preamble_and_trailing_text(self):
        text = 'Here is the result:\n{"a": 1, "b": 2}\nHope that helps!'
        assert _extract_json_object(text) == '{"a": 1, "b": 2}'

    def test_code_fenced_json(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json_object(text) == '{"a": 1}'

    def test_code_fenced_without_json_tag(self):
        text = '```\n{"a": 1}\n```'
        assert _extract_json_object(text) == '{"a": 1}'

    def test_nested_braces(self):
        text = '{"a": {"b": {"c": 1}}}'
        assert _extract_json_object(text) == text

    def test_braces_inside_string_literal_ignored(self):
        text = '{"a": "text with } brace inside"}'
        assert _extract_json_object(text) == text

    def test_escaped_quote_inside_string(self):
        text = r'{"a": "she said \"hi\""}'
        assert _extract_json_object(text) == text

    def test_no_brace_returns_original(self):
        assert _extract_json_object("no json here") == "no json here"

    def test_unbalanced_braces_returns_from_start(self):
        text = '{"a": 1'
        assert _extract_json_object(text) == '{"a": 1'


class TestRepairExitReturnLiterals:
    def test_repairs_long_literal_to_exit(self):
        src = "def evaluate(ctx):\n    return 'long'\n"
        out = _repair_exit_return_literals(src)
        assert out is not None
        assert "'exit'" in out
        assert "'long'" not in out

    def test_repairs_short_literal_to_exit(self):
        src = "def evaluate(ctx):\n    return 'short'\n"
        out = _repair_exit_return_literals(src)
        assert out is not None
        assert "'exit'" in out

    def test_no_change_when_already_exit(self):
        src = "def evaluate(ctx):\n    return 'exit'\n"
        assert _repair_exit_return_literals(src) is None

    def test_no_change_for_dynamic_return(self):
        src = "def evaluate(ctx):\n    return ctx.signal\n"
        assert _repair_exit_return_literals(src) is None

    def test_syntax_error_returns_none(self):
        assert _repair_exit_return_literals("def evaluate(:\n  broken") is None

    def test_no_evaluate_function_returns_none(self):
        src = "def other_fn():\n    return 'long'\n"
        assert _repair_exit_return_literals(src) is None


class TestValidateStrategyOptions:
    def test_non_dict_input_returns_defaults(self):
        opts = _validate_strategy_options(None)
        assert opts["entry_logic"] == "OR"
        assert opts["sl_value"] == 2.0

    def test_invalid_enum_falls_back_to_default(self):
        opts = _validate_strategy_options({"entry_logic": "XOR"})
        assert opts["entry_logic"] == "OR"

    def test_valid_enum_passes_through(self):
        opts = _validate_strategy_options({"entry_logic": "AND"})
        assert opts["entry_logic"] == "AND"

    def test_out_of_range_sl_value_clamped(self):
        opts = _validate_strategy_options({"sl_value": 999.0})
        assert opts["sl_value"] == 50.0

    def test_negative_sl_value_clamped_to_min(self):
        opts = _validate_strategy_options({"sl_value": -5.0})
        assert opts["sl_value"] == 0.1

    def test_atr_period_coerced_to_int(self):
        opts = _validate_strategy_options({"atr_period": 20.7})
        assert opts["atr_period"] == 20
        assert isinstance(opts["atr_period"], int)


class TestValidateComposed:
    def test_missing_blocks_key_raises(self):
        with pytest.raises(ValueError, match="missing 'blocks'"):
            _validate_composed({})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            _validate_composed("not a dict")

    def test_unknown_block_type_dropped(self):
        # Dropping the only block leaves no entry block -> the function
        # refuses the whole proposal rather than silently emptying it.
        data = {
            "blocks": [
                {"type": "not_a_real_block", "role": "entry", "params": {}},
            ]
        }
        with pytest.raises(ValueError, match="missing entry block"):
            _validate_composed(data)

    def test_invalid_role_dropped(self):
        data = {
            "blocks": [
                {"type": "rsi_threshold", "role": "sideways", "params": {}},
            ]
        }
        with pytest.raises(ValueError, match="missing entry block"):
            _validate_composed(data)

    def test_valid_block_kept_with_defaults(self):
        # A single entry-only proposal gets an auto-added fallback exit block
        # (see _validate_composed's "Add one of several exit options" step).
        data = {"blocks": [{"type": "rsi_threshold", "role": "entry", "params": {}}]}
        out = _validate_composed(data)
        entries = [b for b in out["blocks"] if b["role"] == "entry"]
        exits = [b for b in out["blocks"] if b["role"] == "exit"]
        assert len(entries) == 1
        assert len(exits) == 1  # auto-added since none was supplied
        b = entries[0]
        assert b["type"] == "rsi_threshold"
        assert b["params"]["period"] == 14  # default

    def test_out_of_range_param_clamped(self):
        data = {
            "blocks": [
                {
                    "type": "rsi_threshold",
                    "role": "entry",
                    "params": {"period": 9999},
                }
            ]
        }
        out = _validate_composed(data)
        assert out["blocks"][0]["params"]["period"] == 50  # catalog max

    def test_invalid_enum_param_falls_back_to_default(self):
        data = {
            "blocks": [
                {
                    "type": "rsi_threshold",
                    "role": "entry",
                    "params": {"cross": "sideways"},
                }
            ]
        }
        out = _validate_composed(data)
        assert out["blocks"][0]["params"]["cross"] == "below"  # catalog default
