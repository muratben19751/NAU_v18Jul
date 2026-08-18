"""Unit tests for custom_block_store.py's pure name-identity/validation
functions — DeepR audit (2026-08-08) flagged these as untested despite
`agent_block_identity`'s role inference being the sole source of truth for
AUTO block entry/exit lifecycle (see its docstring: role is deliberately
read from the *name*, not LLM metadata).

Wiki References: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from custom_block_store import agent_block_identity, is_valid_name


class TestIsValidName:
    def test_valid_snake_case(self):
        assert is_valid_name("rsi_threshold") is True

    def test_valid_with_digits(self):
        assert is_valid_name("block2") is True

    def test_empty_string_invalid(self):
        assert is_valid_name("") is False

    def test_none_invalid(self):
        assert is_valid_name(None) is False

    def test_uppercase_invalid(self):
        assert is_valid_name("RSI_Threshold") is False

    def test_starts_with_digit_invalid(self):
        assert is_valid_name("1block") is False

    def test_starts_with_underscore_invalid(self):
        assert is_valid_name("_block") is False

    def test_single_char_too_short(self):
        assert is_valid_name("a") is False

    def test_max_length_40_ok(self):
        assert is_valid_name("a" + "b" * 39) is True

    def test_over_max_length_invalid(self):
        assert is_valid_name("a" + "b" * 40) is False

    def test_hyphen_invalid(self):
        assert is_valid_name("my-block") is False


class TestAgentBlockIdentity:
    def test_entry_block_legacy_shape(self):
        identity = agent_block_identity("agnt_e_ab123456_0")
        assert identity == {"role": "entry", "run_id": "ab123456"}

    def test_exit_block_legacy_shape(self):
        identity = agent_block_identity("agnt_x_ab123456_3")
        assert identity == {"role": "exit", "run_id": "ab123456"}

    def test_entry_block_with_round_suffix(self):
        identity = agent_block_identity("agnt_e_ab123456_r2_0")
        assert identity == {"role": "entry", "run_id": "ab123456"}

    def test_none_for_user_authored_name(self):
        assert agent_block_identity("rsi_threshold") is None

    def test_none_for_describe_prefixed_name(self):
        # desc_* is ephemeral but not an AUTO agent block — no identity.
        assert agent_block_identity("desc_my_block") is None

    def test_none_for_empty_string(self):
        assert agent_block_identity("") is None

    def test_none_for_none_input(self):
        assert agent_block_identity(None) is None

    def test_none_for_invalid_role_letter(self):
        assert agent_block_identity("agnt_z_ab123456_0") is None

    def test_none_for_short_run_id(self):
        assert agent_block_identity("agnt_e_short_0") is None

    def test_none_for_missing_iteration_suffix(self):
        assert agent_block_identity("agnt_e_ab123456") is None
