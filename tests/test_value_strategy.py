"""Tests for value/Kelly criterion strategy."""

import pytest

from kalshi_bot.strategies.value import ValueStrategy


@pytest.fixture
def value_strategy():
    params = {
        "edge_threshold": 0.05,
        "kelly_fraction": 0.25,
        "max_position": 50,
        "rebalance_interval": 0,  # no delay for tests
    }
    vs = ValueStrategy(name="test_value", params=params)
    vs.set_capital(100_000)  # $1000
    return vs


class TestKellySizing:
    def test_kelly_positive_edge(self, value_strategy):
        # Fair prob 60%, market at 50 cents
        size = value_strategy._kelly_size(0.60, 50)
        assert size > 0

    def test_kelly_no_edge(self, value_strategy):
        # Fair prob 50%, market at 50 cents -> no edge
        size = value_strategy._kelly_size(0.50, 50)
        assert size == 0

    def test_kelly_negative_edge(self, value_strategy):
        # Fair prob 40%, market at 50 cents -> negative edge
        size = value_strategy._kelly_size(0.40, 50)
        assert size == 0

    def test_kelly_large_edge(self, value_strategy):
        # Fair prob 80%, market at 50 cents -> big edge
        size = value_strategy._kelly_size(0.80, 50)
        assert size > 0

    def test_kelly_fraction_reduces(self, value_strategy):
        size_quarter = value_strategy._kelly_size(0.70, 50)
        value_strategy.params["kelly_fraction"] = 1.0
        size_full = value_strategy._kelly_size(0.70, 50)
        assert size_full > size_quarter

    def test_kelly_clamps_price(self, value_strategy):
        # Edge cases: very low and very high prices
        size_low = value_strategy._kelly_size(0.90, 1)
        size_high = value_strategy._kelly_size(0.10, 99)
        assert size_low >= 0
        assert size_high == 0


class TestFairValue:
    def test_external_override(self, value_strategy):
        value_strategy.set_fair_value("TEST", 0.65)
        assert value_strategy._fair_values["TEST"] == 0.65

    def test_clamp_fair_value(self, value_strategy):
        value_strategy.set_fair_value("TEST", 1.5)
        assert value_strategy._fair_values["TEST"] == 0.99
        value_strategy.set_fair_value("TEST", -0.5)
        assert value_strategy._fair_values["TEST"] == 0.01

    def test_no_estimate_insufficient_data(self, value_strategy):
        result = value_strategy._estimate_fair_value("UNKNOWN")
        assert result is None


class TestState:
    def test_state_roundtrip(self, value_strategy):
        value_strategy.set_fair_value("TEST", 0.65)
        state = value_strategy.get_state()
        new_vs = ValueStrategy(name="new", params=value_strategy.params)
        new_vs.load_state(state)
        assert new_vs._fair_values["TEST"] == 0.65
