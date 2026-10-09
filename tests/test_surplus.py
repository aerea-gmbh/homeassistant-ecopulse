"""The ``surplus_available`` decision, without Home Assistant (#623).

On when the excess is at least the threshold for the whole on-delay; off when it is below
``threshold - hysteresis`` (or unknown) for the whole off-delay. Every interruption restarts the
delay. ``update`` returns when it must be asked again while a switch is pending.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ecopulse.surplus import SurplusDetector, SurplusSettings

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SETTINGS = SurplusSettings(
    threshold_w=500,
    hysteresis_w=100,
    on_delay=timedelta(minutes=2),
    off_delay=timedelta(minutes=2),
)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def detector_that_is_on() -> SurplusDetector:
    detector = SurplusDetector(SETTINGS)
    detector.update(600, at(0))
    detector.update(600, at(120))
    assert detector.is_on
    return detector


def test_starts_off() -> None:
    assert not SurplusDetector(SETTINGS).is_on


def test_turns_on_only_after_the_on_delay_above_the_threshold() -> None:
    detector = SurplusDetector(SETTINGS)

    assert detector.update(500, at(0)) == at(120)
    assert not detector.is_on
    assert detector.update(800, at(119)) == at(120)
    assert not detector.is_on

    assert detector.update(800, at(120)) is None
    assert detector.is_on


def test_a_dip_below_the_threshold_restarts_the_on_delay() -> None:
    detector = SurplusDetector(SETTINGS)
    detector.update(600, at(0))

    assert detector.update(499, at(60)) is None  # nothing pending any more
    assert detector.update(600, at(61)) == at(181)
    detector.update(600, at(180))
    assert not detector.is_on
    detector.update(600, at(181))
    assert detector.is_on


def test_stays_on_inside_the_hysteresis_band() -> None:
    detector = detector_that_is_on()

    assert detector.update(400, at(200)) is None  # == threshold - hysteresis: not below
    assert detector.update(450, at(10_000)) is None
    assert detector.is_on


def test_turns_off_after_the_off_delay_below_the_band() -> None:
    detector = detector_that_is_on()

    assert detector.update(399, at(200)) == at(320)
    detector.update(-1500, at(319))
    assert detector.is_on
    assert detector.update(-1500, at(320)) is None
    assert not detector.is_on


def test_a_recovery_into_the_band_cancels_a_pending_off() -> None:
    detector = detector_that_is_on()
    detector.update(0, at(200))

    assert detector.update(450, at(300)) is None
    detector.update(0, at(301))
    detector.update(0, at(420))
    assert detector.is_on  # off-delay restarted at 301
    detector.update(0, at(421))
    assert not detector.is_on


def test_an_unknown_value_counts_as_no_surplus() -> None:
    # Fail-safe: a missing reading never turns the sensor on and turns it off like a deficit,
    # after the off-delay, so a short stream gap does not switch loads off and on again.
    off = SurplusDetector(SETTINGS)
    off.update(600, at(0))
    assert off.update(None, at(60)) is None
    off.update(600, at(61))
    off.update(600, at(120))
    assert not off.is_on  # the gap restarted the on-delay

    on = detector_that_is_on()
    assert on.update(None, at(200)) == at(320)
    on.update(None, at(320))
    assert not on.is_on


def test_zero_delays_switch_at_once() -> None:
    detector = SurplusDetector(
        SurplusSettings(
            threshold_w=500, hysteresis_w=0, on_delay=timedelta(), off_delay=timedelta()
        )
    )

    assert detector.update(500, at(0)) is None
    assert detector.is_on
    assert detector.update(499, at(1)) is None
    assert not detector.is_on


def test_new_settings_keep_how_long_the_condition_already_held() -> None:
    detector = SurplusDetector(SETTINGS)
    detector.update(600, at(0))

    shorter = SurplusSettings(500, 100, timedelta(seconds=30), timedelta(minutes=2))
    assert detector.reconfigure(shorter, at(10)) == at(30)
    detector.update(600, at(30))
    assert detector.is_on


def test_new_settings_that_end_the_condition_cancel_the_pending_switch() -> None:
    detector = SurplusDetector(SETTINGS)
    detector.update(600, at(0))

    higher = SurplusSettings(1000, 100, timedelta(minutes=2), timedelta(minutes=2))
    assert detector.reconfigure(higher, at(10)) is None
    detector.update(600, at(500))
    assert not detector.is_on


def test_new_settings_that_are_already_satisfied_switch_at_once() -> None:
    detector = SurplusDetector(SETTINGS)
    detector.update(600, at(0))

    instant = SurplusSettings(500, 100, timedelta(), timedelta(minutes=2))
    assert detector.reconfigure(instant, at(10)) is None
    assert detector.is_on


def test_a_restored_on_state_is_held_until_the_off_rule_says_otherwise() -> None:
    detector = SurplusDetector(SETTINGS, is_on=True)

    assert detector.update(None, at(0)) == at(120)
    assert detector.update(450, at(5)) is None
    assert detector.is_on


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_is_unknown(value: float) -> None:
    detector = detector_that_is_on()

    assert detector.update(value, at(200)) == at(320)


# Threshold 0 and no delays: here 1 W already turns the sensor on, so "unknown" and "a small
# number" lead to different states (with threshold 500 both stay off and prove nothing).
INSTANT_AT_ZERO = SurplusSettings(
    threshold_w=0, hysteresis_w=0, on_delay=timedelta(), off_delay=timedelta()
)


@pytest.mark.parametrize("value", ["2000", True, False, [1], {"w": 1}])
def test_a_value_that_is_no_number_is_unknown(value: object) -> None:
    detector = SurplusDetector(INSTANT_AT_ZERO)

    detector.update(value, at(0))

    assert not detector.is_on


@pytest.mark.parametrize("value", [1, 0, 0.5])
def test_a_number_at_or_above_a_zero_threshold_turns_it_on(value: float) -> None:
    # Counterpart of the test above: the setting does let a number through.
    detector = SurplusDetector(INSTANT_AT_ZERO)

    detector.update(value, at(0))

    assert detector.is_on
