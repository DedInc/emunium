"""Pointer motion planners (emunium._motion) and their wiring, against fakes.

Every planner runs on a seeded ``random.Random``; no test sends OS input.
"""

from __future__ import annotations

import ctypes
import itertools
import math
import random
import time
from typing import Any

import pytest

from emunium import _element_interactor as ei
from emunium._browser.context import BrowserContext, DragInteractor
from emunium._browser.facade import Browser
from emunium._element_interactor import ElementInteractor
from emunium._motion import (
    DEFAULT_TARGET_SIZE,
    HUMAN_MAX_DURATION,
    HUMAN_MIN_DURATION,
    PRESET_NAMES,
    BezierTremorPlanner,
    FittsTiming,
    LinearPlanner,
    LognormalStroke,
    MotionStep,
    Overshoot,
    ScreenBounds,
    SigmaDriftPlanner,
    Tremor,
    WindMousePlanner,
    resolve_motion,
    tremor_envelope,
)
from emunium.errors import InvalidArgumentError

SCREEN = ScreenBounds(0, 0, 1920, 1080, primary=(1920, 1080))
PLANNER_CLASSES = (SigmaDriftPlanner, BezierTremorPlanner, WindMousePlanner)
HUMAN_PLANNERS = [cls.preset(name) for cls in PLANNER_CLASSES for name in PRESET_NAMES]
ALL_PLANNERS = [*HUMAN_PLANNERS, LinearPlanner()]
MOVES = [
    ((100, 100), (1100, 100)),
    ((1900, 1060), (20, 15)),
    ((960, 540), (965, 541)),
    ((3, 3), (1916, 1076)),
    ((500, 500), (500, 500)),
]
QUIET = Tremor(amplitude_px=(0.0, 0.0), drift_px=0.0)
# Deliberately mistyped arguments for the validation tests.
NOT_A_PLANNER: Any = 42
ONE_VALUE: Any = (0.3,)
NOT_TIMING: Any = "fast"
FLOAT_PAIR: Any = (1920.5, 1080)
TEXT_PAIR: Any = (1920, "1080")


def planner_id(planner):
    return type(planner).__name__


def speeds(start, steps):
    """(time, px/s) per step, time at the step's end."""
    result, last, now = [], start, 0.0
    for step in steps:
        now += step.dt
        if step.dt > 0:
            result.append(
                (now, math.hypot(step.x - last[0], step.y - last[1]) / step.dt)
            )
        last = (step.x, step.y)
    return result


# -- guarantees shared by every planner ------------------------------------------


@pytest.mark.parametrize("planner", ALL_PLANNERS, ids=planner_id)
def test_last_step_lands_exactly_on_end(planner):
    for seed in range(15):
        for start, end in MOVES:
            steps = planner.plan(
                start, end, target_size=20, rng=random.Random(seed), bounds=SCREEN
            )
            assert steps and steps[-1] == MotionStep(end[0], end[1], steps[-1].dt)
            assert all(isinstance(s.x, int) and isinstance(s.y, int) for s in steps)
            assert all(s.dt >= 0 for s in steps)


@pytest.mark.parametrize("planner", HUMAN_PLANNERS, ids=planner_id)
def test_steps_stay_on_the_virtual_screen_and_off_every_corner(planner):
    # Two monitors: a secondary one left of and above the primary.
    bounds = ScreenBounds(-1280, -200, 3200, 1280, primary=(1920, 1080))
    targets = [(0, 0), (1919, 0), (0, 1079), (1919, 1079), (-1280, -200), (1919, 1079)]
    for seed in range(20):
        rng = random.Random(seed)
        start = (-1270, 1070)
        for end in targets:
            steps = planner.plan(start, end, target_size=4, rng=rng, bounds=bounds)
            assert (steps[-1].x, steps[-1].y) == end
            for step in steps[:-1]:
                point = (step.x, step.y)
                assert bounds.contains(point)
                assert not bounds.is_corner(point)
            start = end


@pytest.mark.parametrize("planner", HUMAN_PLANNERS, ids=planner_id)
def test_same_seed_same_plan_and_no_global_randomness(planner, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("module-level random used")

    for name in ("random", "uniform", "choice", "gauss", "lognormvariate"):
        monkeypatch.setattr(random, name, forbidden)
    first = planner.plan((10, 10), (900, 700), target_size=30, rng=random.Random(4))
    again = planner.plan((10, 10), (900, 700), target_size=30, rng=random.Random(4))
    other = planner.plan((10, 10), (900, 700), target_size=30, rng=random.Random(5))
    assert first == again and first != other


@pytest.mark.parametrize("planner", HUMAN_PLANNERS, ids=planner_id)
def test_total_time_is_the_clamped_fitts_duration(planner):
    timing = planner.timing
    for seed in range(10):
        for start, end in MOVES[:2]:
            steps = planner.plan(start, end, target_size=20, rng=random.Random(seed))
            total = sum(s.dt for s in steps)
            assert timing.min_duration - 1e-9 <= total <= timing.max_duration + 1e-9
            assert HUMAN_MIN_DURATION <= total <= HUMAN_MAX_DURATION


def test_linear_planner_is_one_instant_step():
    steps = LinearPlanner().plan(
        (0, 0), (800, 600), target_size=10, rng=random.Random(1)
    )
    assert steps == [MotionStep(800, 600, 0.0)]


# -- Fitts' law -------------------------------------------------------------------


def test_fitts_duration_grows_with_distance_and_shrinks_with_target_size():
    timing = FittsTiming(a=0.1, b=0.13, variability=0.0, max_duration=1.6)
    by_distance = [timing.nominal(d, 20) for d in (10, 50, 200, 800, 1600)]
    assert by_distance == sorted(by_distance) and len(set(by_distance)) == 5
    by_width = [timing.nominal(600, w) for w in (4, 16, 64, 256)]
    assert by_width == sorted(by_width, reverse=True) and len(set(by_width)) == 4
    expected = 0.1 + 0.13 * math.log2(600 / 16 + 1)
    assert timing.nominal(600, 16) == pytest.approx(expected)
    assert timing.duration(600, 16, random.Random(1)) == pytest.approx(expected)


def test_fitts_duration_is_clamped_and_seeded():
    timing = FittsTiming(variability=0.35, min_duration=0.2, max_duration=1.0)
    assert timing.nominal(0, 20) == pytest.approx(0.2)
    assert timing.nominal(10**9, 1) == pytest.approx(1.0)
    draws = [timing.duration(400, 20, random.Random(seed)) for seed in range(200)]
    assert all(0.2 <= d <= 1.0 for d in draws) and len(set(draws)) > 50
    assert draws == [timing.duration(400, 20, random.Random(s)) for s in range(200)]


def test_planned_duration_follows_fitts_monotonically_on_average():
    planner = SigmaDriftPlanner.preset("natural")

    def mean_total(distance, width):
        totals = [
            sum(
                s.dt
                for s in planner.plan(
                    (100, 500),
                    (100 + distance, 500),
                    target_size=width,
                    rng=random.Random(seed),
                )
            )
            for seed in range(30)
        ]
        return sum(totals) / len(totals)

    assert mean_total(60, 24) < mean_total(300, 24) < mean_total(1200, 24)
    assert mean_total(600, 8) > mean_total(600, 120)


# -- SigmaDrift ---------------------------------------------------------------------


def test_lognormal_stroke_speed_is_the_derivative_of_progress():
    stroke = LognormalStroke(distance=500.0, duration=0.6, sigma=0.12)
    assert stroke.progress(0.0) == 0.0 and stroke.progress(0.6) == 1.0
    h = 1e-6
    # progress is the lognormal CDF renormalised over mu - 3.2 sigma .. mu + 3 sigma.
    covered = 0.5 * (math.erf(3 / math.sqrt(2)) + math.erf(3.2 / math.sqrt(2)))
    for t in (0.05, 0.15, 0.3, 0.45):
        slope = 500.0 * (stroke.progress(t + h) - stroke.progress(t - h)) / (2 * h)
        assert slope == pytest.approx(stroke.speed(t) / covered, rel=1e-4)
    assert stroke.time_at(3.0) == pytest.approx(0.6)
    assert stroke.time_at(-3.2) == pytest.approx(0.0, abs=1e-12)
    assert stroke.time_at(1.0) / 0.6 == pytest.approx(
        LognormalStroke.share_at(0.12, 1.0)
    )


@pytest.mark.parametrize("sigma", [0.08, 0.10, 0.12, 0.14, 0.16])
def test_lognormal_speed_peaks_at_35_to_50_percent_and_ends_near_rest(sigma):
    stroke = LognormalStroke(distance=800.0, duration=0.8, sigma=sigma)
    times = [i * 0.8 / 2000 for i in range(2001)]
    values = [stroke.speed(t) for t in times]
    peak = values.index(max(values))
    assert 0.35 <= times[peak] / 0.8 <= 0.5
    assert times[peak] / 0.8 == pytest.approx(stroke.peak_share, abs=1e-3)
    assert all(a <= b for a, b in itertools.pairwise(values[: peak + 1]))
    assert all(a >= b for a, b in itertools.pairwise(values[peak:]))
    assert values[0] < 0.01 * values[peak] and values[-1] < 0.01 * values[peak]


def numeric_speed(trajectory, t, h=1e-5):
    """Speed (px/s) from central differences, one-sided at the ends."""
    low, high = max(t - h, 0.0), min(t + h, trajectory.duration)
    (ax, ay), (bx, by) = trajectory.position(low), trajectory.position(high)
    return math.hypot(bx - ax, by - ay) / (high - low)


def along(trajectory, t, direction, h=1e-5):
    """Velocity component (px/s) along ``direction``."""
    (ax, ay), (bx, by) = trajectory.position(t - h), trajectory.position(t + h)
    return ((bx - ax) * direction[0] + (by - ay) * direction[1]) / (2 * h)


def trajectories(kind, seeds=range(12), **kwargs):
    overshoot = Overshoot(probability=1.0 if kind == "overshoot" else 0.0)
    correction = 1.0 if kind == "undershoot" else 0.0
    found = []
    for preset in PRESET_NAMES:
        base = SigmaDriftPlanner.preset(preset)
        planner = SigmaDriftPlanner(
            timing=base.timing,
            sigma=base.sigma,
            correction_onset=base.correction_onset,
            correction_ratio=base.correction_ratio,
            correction_probability=correction,
            overshoot=overshoot,
            **kwargs,
        )
        for seed in seeds:
            for start, end, width in (
                ((100, 500), (1500, 500), 24),
                ((900, 900), (300, 120), 60),
                ((400, 400), (500, 420), 24),
            ):
                trajectory = planner.trajectory(
                    start, end, target_size=width, rng=random.Random(seed)
                )
                assert trajectory.kind == kind
                found.append((start, end, trajectory))
    return found


@pytest.mark.parametrize("kind", ["single", "undershoot", "overshoot"])
def test_stroke_junctions_and_the_end_are_near_rest(kind):
    for start, end, trajectory in trajectories(kind):
        total = trajectory.duration
        grid = [i * total / 600 for i in range(601)]
        peak = max(numeric_speed(trajectory, t) for t in grid)
        assert trajectory.position(total) == pytest.approx(end)
        assert trajectory.position(0.0) == pytest.approx(start)
        assert numeric_speed(trajectory, total) < 0.02 * peak
        assert numeric_speed(trajectory, 0.0) < 0.02 * peak
        # Speed is continuous across every stroke start and end.
        for junction in trajectory.junctions:
            h = 1e-5
            before = numeric_speed(trajectory, max(junction - 2 * h, 0.0), h)
            after = numeric_speed(trajectory, min(junction + 2 * h, total), h)
            assert abs(after - before) < 0.02 * peak
        for index in range(len(trajectory.strokes)):
            onset, stroke = trajectory.stroke(index)
            assert onset + stroke.duration <= total + 1e-9


@pytest.mark.parametrize("kind", ["single", "undershoot"])
def test_no_reversal_without_overshoot(kind):
    for start, end, trajectory in trajectories(kind):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
        total = trajectory.duration
        for i in range(1, 400):
            assert along(trajectory, i * total / 400, direction) > -1e-6


def test_overshoot_corrective_stroke_overlaps_and_turns_back_smoothly():
    # Straight paths: curvature would add normal acceleration to the measure.
    for start, end, trajectory in trajectories("overshoot", curvature=(0.0, 0.0)):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
        total = trajectory.duration
        primary_onset, primary = trajectory.stroke(0)
        corrective_onset, _ = trajectory.stroke(1)
        # The corrective stroke starts while the primary still decelerates.
        assert primary_onset == 0.0
        peak_time = primary.peak_share * primary.duration
        assert peak_time < corrective_onset < primary.duration
        # Exactly one forward-to-back turn.
        grid = [i * total / 800 for i in range(1, 800)]
        signs = [along(trajectory, t, direction) > 0 for t in grid]
        flips = [
            round(grid[i] / total, 3)
            for i, (a, b) in enumerate(itertools.pairwise(signs))
            if a != b
        ]
        assert signs[0] and not signs[-1]
        assert len(flips) == 1, (start, end, corrective_onset / total, flips)
        # The turn is smooth: acceleration after the corrective onset stays
        # within 1.5x the peak acceleration of the primary alone (before it).
        step = total / 800
        velocities = []
        for t in grid:
            (ax, ay), (bx, by) = (
                trajectory.position(t - 1e-5),
                trajectory.position(t + 1e-5),
            )
            velocities.append(((bx - ax) / 2e-5, (by - ay) / 2e-5))
        accelerations = [
            (t, math.hypot(b[0] - a[0], b[1] - a[1]) / step)
            for t, (a, b) in zip(grid, itertools.pairwise(velocities))
        ]
        primary_peak = max(a for t, a in accelerations if t < corrective_onset)
        turn_peak = max(a for t, a in accelerations if t >= corrective_onset)
        assert turn_peak <= 1.5 * primary_peak


def test_sigma_drift_path_speed_peaks_mid_stroke():
    planner = SigmaDriftPlanner(
        tremor=QUIET, correction_probability=0.0, overshoot=Overshoot(probability=0.0)
    )
    for seed in range(10):
        steps = planner.plan(
            (100, 500), (1500, 500), target_size=24, rng=random.Random(seed)
        )
        profile = speeds((100, 500), steps)
        total = sum(s.dt for s in steps)
        peak_time, peak = max(profile, key=lambda item: item[1])
        assert 0.3 * total <= peak_time <= 0.55 * total
        assert profile[0][1] < 0.2 * peak and profile[-1][1] < 0.2 * peak


def test_undershoot_primary_covers_most_of_the_way_then_corrects():
    planner = SigmaDriftPlanner(
        correction_probability=1.0,
        primary_fraction=(0.95, 0.95),
        curvature=(0.0, 0.0),
        overshoot=Overshoot(probability=0.0),
    )
    trajectory = planner.trajectory(
        (0, 500), (1000, 500), target_size=24, rng=random.Random(2)
    )
    onset, corrective = trajectory.stroke(1)
    _, primary = trajectory.stroke(0)
    assert primary.distance == pytest.approx(950.0)
    assert corrective.distance == pytest.approx(50.0)
    assert onset + corrective.duration == pytest.approx(trajectory.duration)
    # When the corrective stroke starts the primary has done most of its work.
    assert 700 <= trajectory.position(onset)[0] <= 950


def test_sigma_drift_curves_and_tremor_shakes():
    curved = SigmaDriftPlanner(tremor=QUIET, curvature=(0.08, 0.08))
    steps = curved.plan((100, 500), (1100, 500), target_size=24, rng=random.Random(1))
    assert max(abs(s.y - 500) for s in steps) >= 70
    shaky = SigmaDriftPlanner(
        curvature=(0.0, 0.0), tremor=Tremor(amplitude_px=(1.0, 1.0))
    )
    steps = shaky.plan((100, 500), (1100, 500), target_size=24, rng=random.Random(1))
    assert 1 <= max(abs(s.y - 500) for s in steps) <= 3


def test_tremor_never_exceeds_a_quarter_of_a_small_target():
    tremor = Tremor(amplitude_px=(2.0, 2.0), drift_px=2.0)
    for seed in range(10):
        offset = tremor.offsets(random.Random(seed), 1.0, target_size=4.0)
        worst = max(max(abs(v) for v in offset(i / 500)) for i in range(501))
        assert worst <= 1.0 + 1e-9
        assert offset(0.0) == (0.0, 0.0)


def test_tremor_envelope_peaks_while_homing_and_lasts_to_the_end():
    assert tremor_envelope(0.0) == 0.0
    assert tremor_envelope(1.0) == 1.0
    assert tremor_envelope(0.3) == pytest.approx(0.4)
    gains = [tremor_envelope(i / 100) for i in range(101)]
    assert all(a <= b for a, b in itertools.pairwise(gains))
    assert max(gains[:60]) <= 0.4 + 1e-9 and min(gains[60:]) >= 0.4


def test_tremor_is_strongest_in_the_homing_phase():
    tremor = Tremor(amplitude_px=(1.0, 1.0), drift_px=0.0)
    for seed in range(10):
        offset = tremor.offsets(random.Random(seed), 1.0, target_size=24.0)

        def rms(low, high, offset=offset):
            values = [math.hypot(*offset(i / 1000)) for i in range(low, high)]
            return math.sqrt(sum(v * v for v in values) / len(values))

        assert rms(600, 960) > 1.5 * rms(100, 600)
        assert rms(900, 960) > 0.5  # still shaking while homing in...
        assert offset(1.0) == pytest.approx((0.0, 0.0), abs=1e-12)  # ...not at the end


def test_tremor_fades_to_zero_over_the_last_polls():
    assert tremor_envelope(1.0, 0.04) == 0.0
    assert tremor_envelope(0.96, 0.04) == pytest.approx(0.94)
    assert tremor_envelope(0.98, 0.04) == pytest.approx(0.25 * 0.97)
    # The fade lasts max(4 polls, 5 % of the move).
    tremor = Tremor(amplitude_px=(1.0, 1.0), drift_px=0.0)
    for duration, hz, fade in ((1.0, 125.0, 0.05), (0.2, 125.0, 0.032)):
        offset = tremor.offsets(random.Random(1), duration, 24.0, hz)
        assert math.hypot(*offset(duration - 0.1 * fade)) <= 0.015
        before = [math.hypot(*offset(duration - fade - i / 1000)) for i in range(20)]
        assert max(before) > 0.3


@pytest.mark.parametrize("planner", HUMAN_PLANNERS[:6], ids=planner_id)
def test_final_jump_onto_the_target_is_at_most_one_pixel(planner):
    for seed in range(40):
        for start, end in (((100, 500), (1500, 520)), ((900, 900), (860, 300))):
            steps = planner.plan(start, end, target_size=24, rng=random.Random(seed))
            last, final = steps[-2], steps[-1]
            # Within one pixel per axis: the last step lands from a neighbour.
            assert max(abs(final.x - last.x), abs(final.y - last.y)) <= 1
            # The move that finally arrives on end is tremor-sized, not a snap.
            points = [(s.x, s.y) for s in steps]
            away = max(i for i, p in enumerate(points) if p != end)
            dx, dy = end[0] - points[away][0], end[1] - points[away][1]
            assert max(abs(dx), abs(dy)) <= 2


def test_sigma_drift_last_steps_still_tremble():
    planner = SigmaDriftPlanner(
        curvature=(0.0, 0.0),
        correction_probability=0.0,
        overshoot=Overshoot(probability=0.0),
        tremor=Tremor(amplitude_px=(1.5, 1.5), drift_px=1.0),
    )
    shaken = 0
    for seed in range(10):
        steps = planner.plan(
            (100, 500), (1100, 500), target_size=24, rng=random.Random(seed)
        )
        total = sum(s.dt for s in steps)
        elapsed, late = 0.0, []
        for step in steps[:-1]:
            elapsed += step.dt
            if elapsed >= 0.9 * total:
                late.append(step.y)
        shaken += any(y != 500 for y in late)
    assert shaken >= 8


# -- overshoot ------------------------------------------------------------------------


def test_overshoot_amount_scales_with_distance_and_is_capped():
    overshoot = Overshoot(probability=1.0, share=(0.04, 0.04))
    assert overshoot.amount(random.Random(1), 500, 100) == pytest.approx(20.0)
    assert overshoot.amount(random.Random(1), 2000, 200) == pytest.approx(40.0)
    assert overshoot.amount(random.Random(1), 50, 10) == pytest.approx(4.0)
    # A small target caps the overshoot at W / 2 + slack.
    assert overshoot.amount(random.Random(1), 1000, 8) == pytest.approx(8.0)
    # Too short a move (D < 3 W) never overshoots.
    assert overshoot.amount(random.Random(1), 100, 40) == 0.0
    assert Overshoot(probability=0.0).amount(random.Random(1), 1000, 24) == 0.0


def test_preset_overshoot_probabilities():
    expected = {"natural": 0.25, "careful": 0.35, "brisk": 0.15}
    for cls in (SigmaDriftPlanner, BezierTremorPlanner):
        for name, probability in expected.items():
            assert cls.preset(name).overshoot.probability == probability
    natural = Overshoot()
    hits = sum(natural.amount(random.Random(s), 800, 24) > 0 for s in range(2000))
    assert 0.21 <= hits / 2000 <= 0.29


@pytest.mark.parametrize("cls", [SigmaDriftPlanner, BezierTremorPlanner])
def test_overshoot_passes_the_target_then_lands_exactly(cls):
    planner = cls(
        tremor=QUIET, curvature=(0.0, 0.0), overshoot=Overshoot(probability=1.0)
    )
    for width in (8, 24, 100):
        cap = min(40.0, width / 2 + 4.0)
        for seed in range(5):
            steps = planner.plan(
                (100, 400), (1100, 400), target_size=width, rng=random.Random(seed)
            )
            beyond = max(s.x for s in steps) - 1100
            # Overlapping strokes turn back before the primary's aim point, so
            # the visible overshoot is at least a pixel and at most the cap.
            assert 1 <= beyond <= cap + 0.5
            assert steps[-1] == MotionStep(1100, 400, steps[-1].dt)
            # The corrective sub-stroke comes back: the last moves approach end.
            assert steps[-2].x >= 1100


# -- BezierTremor ---------------------------------------------------------------------


def test_bezier_without_overshoot_stays_short_of_target_and_eases():
    planner = BezierTremorPlanner(
        tremor=QUIET, overshoot=Overshoot(probability=0.0), curvature=(0.0, 0.0)
    )
    steps = planner.plan((100, 400), (900, 400), target_size=24, rng=random.Random(6))
    assert max(s.x for s in steps) == 900
    profile = [v for _, v in speeds((100, 400), steps)]
    middle = max(profile)
    assert profile[0] < 0.25 * middle and profile[-1] < 0.25 * middle


# -- WindMouse ------------------------------------------------------------------------


@pytest.mark.parametrize("hz", [60.0, 125.0, 1000.0])
def test_wind_mouse_emits_at_the_polling_rate(hz):
    planner = WindMousePlanner(sample_hz=hz)
    for seed in range(10):
        steps = planner.plan(
            (50, 50), (1500, 900), target_size=24, rng=random.Random(seed)
        )
        total = sum(s.dt for s in steps)
        assert all(s.dt >= 1.0 / hz - 1e-9 for s in steps[:-1])
        # Never faster than the polling rate, and close to it for a long walk.
        assert len(steps) - 1 <= total * hz + 1
        assert len(steps) >= min(0.5 * total * hz, 40)


def test_wind_mouse_walk_respects_the_step_cap():
    planner = WindMousePlanner()
    for seed in range(10):
        last = (50.0, 50.0)
        for x, y in planner._walk((50, 50), (1500, 900), random.Random(seed)):
            assert math.hypot(x - last[0], y - last[1]) <= 15 + 1e-9
            last = (x, y)
        assert math.hypot(1500 - last[0], 900 - last[1]) < 1.0


# -- parameter validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "build, name",
    [
        (lambda: FittsTiming(a=-0.1), "FittsTiming.a"),
        (lambda: FittsTiming(b=0.5), "FittsTiming.b"),
        (lambda: FittsTiming(variability=1.0), "FittsTiming.variability"),
        (lambda: FittsTiming(min_duration=0.05), "FittsTiming.min_duration"),
        (lambda: FittsTiming(max_duration=2.0), "FittsTiming.max_duration"),
        (lambda: FittsTiming(min_duration=0.5, max_duration=0.4), "min_duration"),
        (lambda: FittsTiming(a=0.3, b=0.3, max_duration=0.5), "a + b"),
        (lambda: FittsTiming(a=float("nan")), "FittsTiming.a"),
        (lambda: FittsTiming(a=True), "FittsTiming.a"),
        (lambda: Tremor(frequency_hz=(2.0, 12.0)), "Tremor.frequency_hz[0]"),
        (lambda: Tremor(frequency_hz=(12.0, 8.0)), "Tremor.frequency_hz"),
        (lambda: Tremor(amplitude_px=ONE_VALUE), "Tremor.amplitude_px"),
        (lambda: Tremor(drift_px=5.0), "Tremor.drift_px"),
        (
            lambda: Tremor(amplitude_px=(1.0, 2.0), drift_px=2.5),
            "amplitude_px[1] + drift_px",
        ),
        (lambda: SigmaDriftPlanner(sigma=(0.05, 0.3)), "SigmaDriftPlanner.sigma[0]"),
        (
            lambda: SigmaDriftPlanner(primary_fraction=(0.5, 0.9)),
            "SigmaDriftPlanner.primary_fraction[0]",
        ),
        (
            lambda: SigmaDriftPlanner(correction_probability=1.5),
            "SigmaDriftPlanner.correction_probability",
        ),
        (lambda: SigmaDriftPlanner(sigma=(0.1, 0.3)), "SigmaDriftPlanner.sigma[1]"),
        (
            lambda: SigmaDriftPlanner(correction_onset=(0.2, 1.0)),
            "SigmaDriftPlanner.correction_onset[0]",
        ),
        (
            lambda: SigmaDriftPlanner(correction_ratio=(0.5, 1.5)),
            "SigmaDriftPlanner.correction_ratio[1]",
        ),
        (lambda: SigmaDriftPlanner(sample_hz=1500), "SigmaDriftPlanner.sample_hz"),
        (lambda: SigmaDriftPlanner(timing=NOT_TIMING), "SigmaDriftPlanner.timing"),
        (
            lambda: SigmaDriftPlanner(overshoot=NOT_TIMING),
            "SigmaDriftPlanner.overshoot",
        ),
        (lambda: Overshoot(probability=2.0), "Overshoot.probability"),
        (lambda: Overshoot(share=(0.06, 0.02)), "Overshoot.share"),
        (lambda: Overshoot(px=(4.0, 100.0)), "Overshoot.px[1]"),
        (lambda: Overshoot(slack_px=-1.0), "Overshoot.slack_px"),
        (lambda: WindMousePlanner(sample_hz=30), "WindMousePlanner.sample_hz"),
        (lambda: ScreenBounds(0, 0, 10, 10, primary=ONE_VALUE), "ScreenBounds.primary"),
        (
            lambda: ScreenBounds(0, 0, 10, 10, primary=FLOAT_PAIR),
            "ScreenBounds.primary[0]",
        ),
        (
            lambda: ScreenBounds(0, 0, 10, 10, primary=TEXT_PAIR),
            "ScreenBounds.primary[1]",
        ),
        (
            lambda: ScreenBounds(0, 0, 10, 10, primary=NOT_A_PLANNER),
            "ScreenBounds.primary",
        ),
        (lambda: ScreenBounds(0, 0, 10, 10, primary=(0, 5)), "ScreenBounds.primary[0]"),
        (lambda: ScreenBounds(0, 0, FLOAT_PAIR[0], 10), "ScreenBounds.width"),
        (lambda: ScreenBounds(TEXT_PAIR[1], 0, 10, 10), "ScreenBounds.left"),
        (
            lambda: BezierTremorPlanner(curvature=(0.1, 0.5)),
            "BezierTremorPlanner.curvature[1]",
        ),
        (lambda: WindMousePlanner(max_step=2), "WindMousePlanner.max_step"),
        (lambda: WindMousePlanner(gravity=4, wind=6), "WindMousePlanner.wind"),
        (lambda: SigmaDriftPlanner.preset("slow"), "SigmaDriftPlanner preset"),
        (lambda: ScreenBounds(0, 0, 0, 10), "ScreenBounds.width"),
        (
            lambda: LinearPlanner().plan(
                (0, 0), (1, 1), target_size=0, rng=random.Random()
            ),
            "target_size",
        ),
        (lambda: resolve_motion("fast"), "motion"),
        (lambda: resolve_motion(NOT_A_PLANNER), "motion"),
    ],
)
def test_invalid_parameters_name_the_parameter_and_range(build, name):
    with pytest.raises(InvalidArgumentError) as info:
        build()
    assert name in str(info.value)


def test_presets_validate_and_differ():
    for cls in PLANNER_CLASSES:
        presets = {name: cls.preset(name) for name in PRESET_NAMES}
        assert presets["natural"] == cls()
        assert len(set(map(repr, presets.values()))) == 3
        careful, brisk = presets["careful"].timing, presets["brisk"].timing
        assert careful.nominal(800, 24) > brisk.nominal(800, 24)


def test_lists_are_accepted_and_normalised():
    sigma: Any = [0.1, 0.15]
    planner = SigmaDriftPlanner(sigma=sigma)
    assert planner.sigma == (0.1, 0.15)


# -- resolution and wiring -----------------------------------------------------------


def test_resolve_motion():
    assert resolve_motion(None) == SigmaDriftPlanner.preset("natural")
    assert resolve_motion("brisk") == SigmaDriftPlanner.preset("brisk")
    custom = WindMousePlanner()
    assert resolve_motion(custom) is custom


class RecordingPlanner:
    def __init__(self, steps):
        self.steps = steps
        self.calls = []

    def plan(self, start, end, *, target_size, rng, bounds=None):
        self.calls.append((start, end, target_size, rng, bounds))
        return list(self.steps)


class FakeGui:
    def __init__(self, position=(100, 100), size=(800, 600), virtual=None):
        self.pos = position
        self.screen = size
        self.log = []
        if virtual is not None:
            self.virtual_screen = lambda: virtual

    def position(self):
        return self.pos

    def size(self):
        return self.screen

    def moveTo(self, x, y, _pause=True):
        assert _pause is False
        self.pos = (x, y)
        self.log.append(("move", x, y))


class ExactClock:
    """A deterministic clock: sleeping advances it by exactly the seconds asked."""

    def __init__(self, sleeps):
        self.now = 0.0
        self.sleeps = sleeps

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_interactor(motion, gui=None):
    sleeps = []
    clock = ExactClock(sleeps)
    gui = gui or FakeGui()
    interactor = ElementInteractor(
        backend=gui,
        platform="linux",
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(9),
        motion=motion,
    )
    return interactor, gui, sleeps


def test_interactor_follows_the_configured_planner():
    planner = RecordingPlanner(
        [
            MotionStep(150, 120, 0.01),
            MotionStep(200, 140, 0.02),
            MotionStep(300, 200, 0.03),
        ]
    )
    interactor, gui, sleeps = make_interactor(planner)
    interactor.move(300.2, 199.6, target_size=40)
    assert gui.log == [("move", 150, 120), ("move", 200, 140), ("move", 300, 200)]
    assert sleeps == pytest.approx([0.01, 0.02, 0.03])
    ((start, end, size, rng, bounds),) = planner.calls
    assert (start, end, size) == ((100, 100), (300, 200), 40)
    assert rng is interactor._rng
    assert bounds == ScreenBounds(0, 0, 800, 600, primary=(800, 600))


def test_interactor_default_target_size_and_virtual_screen_bounds():
    planner = RecordingPlanner([MotionStep(-500, 50, 0.0)])
    gui = FakeGui(size=(1920, 1080), virtual=(-1280, 0, 3200, 1080))
    interactor, _, sleeps = make_interactor(planner, gui)
    interactor.move(-500, 50)
    ((_, _, size, _, bounds),) = planner.calls
    assert size == DEFAULT_TARGET_SIZE and sleeps == []
    assert bounds == ScreenBounds(-1280, 0, 3200, 1080, primary=(1920, 1080))


def test_bounds_widen_to_an_end_outside_the_reported_screen():
    planner = RecordingPlanner([MotionStep(900, 50, 0.0)])
    interactor, _, _ = make_interactor(planner)
    interactor.move(900, 50)
    bounds = planner.calls[0][4]
    assert bounds == ScreenBounds(0, 0, 901, 600, primary=(800, 600))


def test_instant_move_skips_the_planner():
    planner = RecordingPlanner([MotionStep(1, 1, 0.5)])
    interactor, gui, sleeps = make_interactor(planner)
    interactor.move(10, 20, human=False)
    assert planner.calls == [] and sleeps == [] and gui.log == [("move", 10, 20)]


def test_linear_planner_moves_once_without_sleeping():
    interactor, gui, sleeps = make_interactor(LinearPlanner())
    interactor.move(700, 500)
    # The planner's only step is the landing, which move() performs itself.
    assert gui.log == [("move", 700, 500)] and sleeps == []


def test_interactor_presets_and_default():
    assert ElementInteractor().motion == SigmaDriftPlanner.preset("natural")
    assert ElementInteractor(motion="careful").motion == SigmaDriftPlanner.preset(
        "careful"
    )
    with pytest.raises(InvalidArgumentError, match="motion"):
        ElementInteractor(motion="sloppy")


def test_real_planner_drives_the_fake_backend():
    interactor, gui, sleeps = make_interactor("brisk", FakeGui(position=(5, 5)))
    interactor.move(790, 590)
    path = [(e[1], e[2]) for e in gui.log]
    assert path[-1] == (790, 590) and len(path) > 10
    assert all(1 <= x <= 798 and 1 <= y <= 598 for x, y in path)
    assert 0.12 <= sum(sleeps) <= 0.9 + 1e-9


# -- playback against deadlines --------------------------------------------------------

WINDOWS_TICK = 0.0156


class CoarseClock:
    """A clock whose sleep never returns before a 15.6 ms Windows tick."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(seconds, WINDOWS_TICK)


class TimedGui(FakeGui):
    def __init__(self, clock, position):
        super().__init__(position=position, size=(1920, 1080))
        self.clock = clock
        self.times = []

    def moveTo(self, x, y, _pause=True):
        super().moveTo(x, y, _pause)
        self.times.append(self.clock())


class KeepPlan:
    """Delegates to a planner and keeps the plan it returned."""

    def __init__(self, planner):
        self.planner = planner
        self.steps = []

    def plan(self, start, end, *, target_size, rng, bounds=None):
        self.steps = self.planner.plan(
            start, end, target_size=target_size, rng=rng, bounds=bounds
        )
        return self.steps


@pytest.mark.parametrize(
    "planner",
    [
        SigmaDriftPlanner.preset("natural"),
        SigmaDriftPlanner.preset("careful"),
        WindMousePlanner.preset("natural"),
        WindMousePlanner(sample_hz=1000),
    ],
    ids=["natural", "careful", "windmouse", "windmouse-1000hz"],
)
def test_coarse_sleep_drops_steps_but_keeps_the_planned_duration(planner):
    for seed in range(5):
        clock = CoarseClock()
        gui = TimedGui(clock, position=(40, 60))
        kept = KeepPlan(planner)
        interactor = ElementInteractor(
            backend=gui,
            platform="linux",
            sleep=clock.sleep,
            clock=clock,
            rng=random.Random(seed),
            motion=kept,
        )
        started = clock()
        interactor.move(1850, 1000)
        planned = sum(s.dt for s in kept.steps)
        realised = gui.times[-1] - started
        assert 0.9 * planned <= realised <= 1.1 * planned
        assert gui.log[-1] == ("move", 1850, 1000)  # the final step is played
        intermediate = len(gui.log) - 1
        assert intermediate < len(kept.steps) - 1  # some steps were dropped...
        # ...every played step is less than one step late, one move per tick.
        deadline, index = started, 0
        for (_, x, y), moved_at in zip(gui.log[:-1], gui.times[:-1]):
            while (kept.steps[index].x, kept.steps[index].y) != (x, y):
                deadline += kept.steps[index].dt
                index += 1
            deadline += kept.steps[index].dt
            assert deadline - 1e-9 <= moved_at < deadline + kept.steps[index + 1].dt
            index += 1
        assert len(set(gui.times)) == len(gui.times)


def test_playback_waits_only_for_the_time_left():
    clock = CoarseClock()
    gui = TimedGui(clock, position=(0, 0))
    plan = [MotionStep(10, 10, 0.05), MotionStep(20, 20, 0.05), MotionStep(30, 30, 0.1)]
    interactor = ElementInteractor(
        backend=gui,
        platform="linux",
        sleep=clock.sleep,
        clock=clock,
        motion=RecordingPlanner(plan),
    )
    interactor.move(30, 30)
    assert clock.sleeps == pytest.approx([0.05, 0.05, 0.1])
    assert gui.times == pytest.approx([1000.05, 1000.1, 1000.2])


def test_late_intermediate_step_is_dropped_final_is_not():
    clock = CoarseClock()
    gui = TimedGui(clock, position=(0, 0))
    plan = [
        MotionStep(10, 10, 0.005),
        MotionStep(20, 20, 0.005),
        MotionStep(30, 30, 0.005),
        MotionStep(40, 40, 0.005),
    ]
    interactor = ElementInteractor(
        backend=gui,
        platform="linux",
        sleep=clock.sleep,
        clock=clock,
        motion=RecordingPlanner(plan),
    )
    interactor.move(40, 40)
    # The first sleep overruns to 15.6 ms: steps 1 and 2 are overtaken by
    # their successors and dropped; step 3 (due 15 ms) plays; the final step
    # is still waited for and landed.
    assert [(e[1], e[2]) for e in gui.log] == [(30, 30), (40, 40)]
    assert clock.sleeps == pytest.approx([0.005, 0.0044])
    assert gui.times[-1] == pytest.approx(1000.0 + 2 * WINDOWS_TICK)


def test_clock_defaults_to_perf_counter_and_waits_never_exceed_a_step():
    sleeps = []
    plan = [MotionStep(10, 10, 0.05), MotionStep(20, 20, 0.05), MotionStep(30, 30, 0.1)]
    gui = FakeGui(position=(0, 0))
    interactor = ElementInteractor(
        backend=gui,
        platform="linux",
        sleep=sleeps.append,  # a stub: returns at once, the real clock barely moves
        motion=RecordingPlanner(plan),
    )
    assert interactor._clock is time.perf_counter
    interactor.move(30, 30)
    # Deadlines alone would ask for 0.05, 0.1, 0.2 s; each wait is capped.
    assert sleeps == pytest.approx([0.05, 0.05, 0.1], abs=0.005)
    assert all(s <= step.dt + 1e-12 for s, step in zip(sleeps, plan))
    assert [(e[1], e[2]) for e in gui.log] == [(10, 10), (20, 20), (30, 30)]


def test_early_waking_sleep_keeps_each_wait_within_its_step():
    clock = CoarseClock()

    def early(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds / 2  # wakes halfway

    plan = [
        MotionStep(10, 10, 0.04),
        MotionStep(20, 20, 0.04),
        MotionStep(30, 30, 0.04),
    ]
    interactor = ElementInteractor(
        backend=TimedGui(clock, position=(0, 0)),
        platform="linux",
        sleep=early,
        clock=clock,
        motion=RecordingPlanner(plan),
    )
    interactor.move(30, 30)
    assert clock.sleeps == pytest.approx([0.04, 0.04, 0.04])


class FakeWinmmFunction:
    def __init__(self, calls, name, result=0):
        self.calls = calls
        self.name = name
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, period):
        self.calls.append((self.name, period))
        return self.result


class FakeWinmm:
    def __init__(self, begin_result=0):
        self.calls = []
        self.timeBeginPeriod = FakeWinmmFunction(self.calls, "begin", begin_result)
        self.timeEndPeriod = FakeWinmmFunction(self.calls, "end")


def test_timer_resolution_is_raised_for_the_move_and_always_restored():
    winmm = FakeWinmm()
    with ei._fine_timer("win32", lambda: winmm):
        assert winmm.calls == [("begin", 1)]
    assert winmm.calls == [("begin", 1), ("end", 1)]
    assert winmm.timeBeginPeriod.argtypes == [ctypes.c_uint]
    assert winmm.timeEndPeriod.restype is ctypes.c_uint
    winmm = FakeWinmm()
    with pytest.raises(KeyboardInterrupt), ei._fine_timer("win32", lambda: winmm):
        raise KeyboardInterrupt
    assert winmm.calls == [("begin", 1), ("end", 1)]


def test_timer_resolution_is_skipped_when_unavailable():
    def missing():
        raise OSError("no winmm")

    with ei._fine_timer("win32", missing):
        pass
    refused = FakeWinmm(begin_result=97)  # TIMERR_NOCANDO
    with ei._fine_timer("win32", lambda: refused):
        pass
    assert refused.calls == [("begin", 1)]
    untouched = FakeWinmm()
    with ei._fine_timer("linux", lambda: untouched):
        pass
    assert untouched.calls == []


def test_human_move_raises_timer_resolution_on_windows(monkeypatch):
    winmm = FakeWinmm()
    monkeypatch.setattr(ei, "_load_winmm", lambda: winmm)
    seen = []

    class WatchingGui(FakeGui):
        def moveTo(self, x, y, _pause=True):
            seen.append(list(winmm.calls))
            super().moveTo(x, y, _pause)

    gui = WatchingGui(position=(0, 0))
    interactor = ElementInteractor(
        backend=gui,
        platform="win32",
        sleep=lambda _s: None,
        motion=RecordingPlanner([MotionStep(5, 5, 0.01), MotionStep(9, 9, 0.01)]),
    )
    interactor.move(9, 9)
    assert seen[0] == [("begin", 1)]  # raised while the steps play
    assert winmm.calls == [("begin", 1), ("end", 1)]  # restored before landing
    assert seen[-1] == [("begin", 1), ("end", 1)]


def fake_bridge() -> Any:
    """A stand-in bridge; BrowserContext construction never calls it."""
    return object()


def test_browser_context_motion():
    ctx = BrowserContext(fake_bridge(), motion="careful")
    assert isinstance(ctx.interactor, DragInteractor)
    assert ctx.interactor.motion == SigmaDriftPlanner.preset("careful")
    custom = BezierTremorPlanner()
    ctx = BrowserContext(fake_bridge(), motion=custom)
    assert (
        isinstance(ctx.interactor, DragInteractor) and ctx.interactor.motion is custom
    )
    default = BrowserContext(fake_bridge())
    assert isinstance(default.interactor, DragInteractor)
    assert default.interactor.motion == SigmaDriftPlanner.preset("natural")
    with pytest.raises(InvalidArgumentError, match="motion"):
        BrowserContext(fake_bridge(), motion="nope")
    with pytest.raises(InvalidArgumentError, match="not both"):
        BrowserContext(fake_bridge(), interactor=DragInteractor(), motion="brisk")


def test_browser_motion_keyword():
    browser = Browser(motion="brisk")
    interactor = browser.context.interactor
    assert isinstance(interactor, DragInteractor)
    assert interactor.motion == SigmaDriftPlanner.preset("brisk")
    windy = WindMousePlanner.preset("careful")
    interactor = Browser(motion=windy).context.interactor
    assert isinstance(interactor, DragInteractor) and interactor.motion is windy
    with pytest.raises(InvalidArgumentError, match="motion"):
        Browser(motion="warp")
