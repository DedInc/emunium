"""Humanized pointer motion planners.

A planner turns one pointer move into timed steps::

    plan(start, end, *, target_size, rng, bounds=None) -> list[MotionStep]

``MotionStep(x, y, dt)`` means "``dt`` seconds after the previous step, put
the cursor on ``(x, y)``". Every planner guarantees:

- the last step is exactly ``end``;
- with ``bounds`` given, every earlier step lies inside the virtual screen
  (kept one pixel off its edges) and never on a corner of the virtual screen
  or of the primary monitor (the fail-safe corners);
- all randomness comes from the ``rng`` argument (no module-level ``random``).

Planners:

- ``LinearPlanner``: one exact step, no delay.
- ``SigmaDriftPlanner`` (default): sigma-lognormal strokes after Plamondon,
  duration from Fitts' law, mild curvature, tremor and drift, an optional
  overshoot or undershoot followed by a corrective sub-stroke.
- ``BezierTremorPlanner``: cubic Bezier, minimum-jerk speed along arc length,
  tremor and drift, occasional overshoot with a corrective approach.
- ``WindMousePlanner``: Benjamin Land's gravity/wind model, emitted at a
  mouse polling rate.

Each non-linear planner has the presets ``natural``, ``careful`` and
``brisk`` (``Planner.preset(name)``). Every parameter is checked against a
documented range on construction and raises ``InvalidArgumentError`` naming
the parameter and the range.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from emunium.errors import InvalidArgumentError

__all__ = [
    "DEFAULT_TARGET_SIZE",
    "HUMAN_MAX_DURATION",
    "HUMAN_MIN_DURATION",
    "PRESET_NAMES",
    "BezierTremorPlanner",
    "FittsTiming",
    "LinearPlanner",
    "LognormalStroke",
    "MotionPlanner",
    "MotionStep",
    "Overshoot",
    "ScreenBounds",
    "SigmaDriftPlanner",
    "Trajectory",
    "Tremor",
    "WindMousePlanner",
    "resolve_motion",
    "tremor_envelope",
]

Point = tuple[int, int]
_FloatPoint = tuple[float, float]
# (time since the move started in s, x, y)
_Sample = tuple[float, float, float]

PRESET_NAMES = ("natural", "careful", "brisk")
# Total duration limits of one aimed mouse movement (s).
HUMAN_MIN_DURATION = 0.12
HUMAN_MAX_DURATION = 1.6
# Target width (input px) assumed when the caller knows none: a typical
# button or link line height.
DEFAULT_TARGET_SIZE = 24.0
# D/W is clamped to this ratio (10 bits of difficulty) before Fitts' law.
_MAX_DIFFICULTY_RATIO = 1024.0
# Tremor plus drift never exceeds this many px, whatever the preset.
_MAX_JITTER_PX = 4.0
# Tremor plus drift never exceeds this share of the target size.
_JITTER_TARGET_SHARE = 0.25
# A lognormal stroke spans mu - 3.2 sigma .. mu + 3 sigma, so it starts and
# ends below 1 % of its peak speed; with sigma in [0.08, 0.16] the peak falls
# at 37-44 % of the stroke duration.
_SIGMAS_BEFORE = 3.2
_SIGMAS_AFTER = 3.0
_SIGMA_RANGE = (0.08, 0.16)
# Tremor fades to zero over at least this many polls before the move ends.
_TREMOR_FADE_POLLS = 4.0
_TREMOR_FADE_SHARE = 0.05
# Overshoot needs a move of at least this many target sizes.
_OVERSHOOT_MIN_RATIO = 3.0
_SAMPLE_HZ_RANGE = (60.0, 1000.0)
_PATH_SAMPLES = 128
_WIND_MAX_ITERATIONS = 20000


# -- validation ---------------------------------------------------------------


def _number(name: str, value: object, low: float, high: float, unit: str = "") -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise InvalidArgumentError(
            f"{name} must be a number in [{low}, {high}]{unit}, got {value!r}"
        )
    return float(value)


def _span(
    name: str, value: object, low: float, high: float, unit: str = ""
) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise InvalidArgumentError(
            f"{name} must be a (low, high) pair within [{low}, {high}]{unit}, "
            f"got {value!r}"
        )
    first = _number(f"{name}[0]", value[0], low, high, unit)
    second = _number(f"{name}[1]", value[1], low, high, unit)
    if first > second:
        raise InvalidArgumentError(
            f"{name} must be ordered (low <= high) within [{low}, {high}]{unit}, "
            f"got {value!r}"
        )
    return first, second


def _pixels(name: str, value: object, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidArgumentError(
            f"{name} must be an int in [{minimum}, inf) px, got {value!r}"
        )
    return value


def _require_type(name: str, value: object, kind: type) -> None:
    if not isinstance(value, kind):
        raise InvalidArgumentError(
            f"{name} must be a {kind.__name__}, got {type(value).__name__}"
        )


def _store(instance: object, name: str, value: object) -> None:
    # Frozen dataclasses normalise their fields (list -> tuple, int -> float).
    object.__setattr__(instance, name, value)


def _sample_hz(name: str, value: object) -> float:
    return _number(name, value, *_SAMPLE_HZ_RANGE, " Hz")


def _target_width(target_size: object) -> float:
    return _number("target_size", target_size, 1.0, 100000.0, " px")


def _preset_name(name: object, owner: str) -> str:
    if not isinstance(name, str) or name not in PRESET_NAMES:
        raise InvalidArgumentError(
            f"{owner} preset must be one of {PRESET_NAMES}, got {name!r}"
        )
    return name


# -- shared value types ---------------------------------------------------------


@dataclass(frozen=True)
class MotionStep:
    """``dt`` seconds after the previous step, put the cursor on ``(x, y)``."""

    x: int
    y: int
    dt: float


@dataclass(frozen=True)
class ScreenBounds:
    """Virtual-screen rectangle plus the primary monitor whose corners are off-limits.

    ``(left, top, width, height)`` is the bounding box of all monitors in input
    px (ints; width and height >= 1); ``primary`` is None or the primary
    monitor's ``(width, height)`` (ints >= 1), its origin is ``(0, 0)``.
    """

    left: int
    top: int
    width: int
    height: int
    primary: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        for name in ("left", "top"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidArgumentError(
                    f"ScreenBounds.{name} must be an int, got {value!r}"
                )
        _pixels("ScreenBounds.width", self.width, 1)
        _pixels("ScreenBounds.height", self.height, 1)
        primary: object = self.primary
        if primary is None:
            return
        if not isinstance(primary, (tuple, list)) or len(primary) != 2:
            raise InvalidArgumentError(
                "ScreenBounds.primary must be None or a (width, height) pair of "
                f"ints in [1, inf) px, got {primary!r}"
            )
        _store(
            self,
            "primary",
            (
                _pixels("ScreenBounds.primary[0]", primary[0], 1),
                _pixels("ScreenBounds.primary[1]", primary[1], 1),
            ),
        )

    @property
    def right(self) -> int:
        """Last column inside the bounds."""
        return self.left + self.width - 1

    @property
    def bottom(self) -> int:
        """Last row inside the bounds."""
        return self.top + self.height - 1

    def covering(self, *points: Point) -> ScreenBounds:
        """The smallest bounds containing these bounds and ``points``.

        A backend that only knows the primary monitor under-reports the
        virtual screen; a start or end point outside it proves the screen is
        larger there.
        """
        left = min([self.left, *(p[0] for p in points)])
        top = min([self.top, *(p[1] for p in points)])
        right = max([self.right, *(p[0] for p in points)])
        bottom = max([self.bottom, *(p[1] for p in points)])
        return ScreenBounds(
            left, top, right - left + 1, bottom - top + 1, primary=self.primary
        )

    def contains(self, point: Point) -> bool:
        return (
            self.left <= point[0] <= self.right and self.top <= point[1] <= self.bottom
        )

    def is_corner(self, point: Point) -> bool:
        """True on a corner pixel of the virtual screen or the primary monitor."""
        x, y = point
        if x in (self.left, self.right) and y in (self.top, self.bottom):
            return True
        if self.primary is None:
            return False
        width, height = self.primary
        return x in (0, width - 1) and y in (0, height - 1)

    def settle(self, point: Point) -> Point:
        """Clamp an intermediate point one px inside the edges, off every corner."""
        x = _clamp_axis(point[0], self.left, self.right)
        y = _clamp_axis(point[1], self.top, self.bottom)
        if self.primary is not None:
            width, height = self.primary
            if x in (0, width - 1) and y in (0, height - 1):
                x = 1 if x == 0 else width - 2
        return x, y


def _clamp_axis(value: int, low: int, high: int) -> int:
    if high - low >= 2:
        low, high = low + 1, high - 1
    return min(max(value, low), high)


@dataclass(frozen=True)
class FittsTiming:
    """Movement time from Fitts' law (Shannon form).

    ``MT = a + b * log2(D / W + 1)`` with ``D`` the distance and ``W`` the
    target size in input px; ``D / W`` is clamped to ``[0, 1024]``. The
    result is multiplied by a lognormal factor ``exp(N(0, variability))``
    and clamped to ``[min_duration, max_duration]``.

    Ranges: ``a`` [0, 0.3] s, ``b`` [0.05, 0.3] s/bit, ``variability``
    [0, 0.35], ``min_duration`` [0.12, 0.6] s, ``max_duration`` [0.3, 1.6] s.
    Cross-checks: ``min_duration < max_duration`` and ``a + b <=
    max_duration`` (a one-bit move fits the limit).
    """

    a: float = 0.10
    b: float = 0.13
    variability: float = 0.12
    min_duration: float = HUMAN_MIN_DURATION
    max_duration: float = 1.2

    def __post_init__(self) -> None:
        _store(self, "a", _number("FittsTiming.a", self.a, 0.0, 0.3, " s"))
        _store(self, "b", _number("FittsTiming.b", self.b, 0.05, 0.3, " s/bit"))
        _store(
            self,
            "variability",
            _number("FittsTiming.variability", self.variability, 0.0, 0.35),
        )
        _store(
            self,
            "min_duration",
            _number(
                "FittsTiming.min_duration",
                self.min_duration,
                HUMAN_MIN_DURATION,
                0.6,
                " s",
            ),
        )
        _store(
            self,
            "max_duration",
            _number(
                "FittsTiming.max_duration",
                self.max_duration,
                0.3,
                HUMAN_MAX_DURATION,
                " s",
            ),
        )
        if self.min_duration >= self.max_duration:
            raise InvalidArgumentError(
                "FittsTiming.min_duration must be below max_duration, got "
                f"{self.min_duration} >= {self.max_duration}"
            )
        if self.a + self.b > self.max_duration:
            raise InvalidArgumentError(
                "FittsTiming.a + b (a one-bit move) must not exceed max_duration "
                f"{self.max_duration} s, got {self.a + self.b:.3f} s"
            )

    def _raw(self, distance: float, target_size: float) -> float:
        ratio = min(max(distance / max(target_size, 1.0), 0.0), _MAX_DIFFICULTY_RATIO)
        return self.a + self.b * math.log2(ratio + 1.0)

    def nominal(self, distance: float, target_size: float) -> float:
        """Noise-free, clamped ``MT`` in seconds."""
        movement = self._raw(distance, target_size)
        return min(max(movement, self.min_duration), self.max_duration)

    def duration(
        self, distance: float, target_size: float, rng: random.Random
    ) -> float:
        """``MT`` with lognormal variability, clamped to the duration limits."""
        movement = self._raw(distance, target_size)
        if self.variability > 0.0:
            movement *= rng.lognormvariate(0.0, self.variability)
        return min(max(movement, self.min_duration), self.max_duration)


def tremor_envelope(share: float, fade_share: float = 0.0) -> float:
    """Tremor gain at ``share`` (0..1) of the move's duration.

    Rises from 0 to 0.4 over the first 10 % (the cursor starts where it is),
    holds 0.4 through the ballistic phase, then ramps linearly to 1.0 over
    the last 40 %: tremor is strongest while homing in. Over the final
    ``fade_share`` it is multiplied by ``((1 - share) / fade_share)^2``,
    reaching exactly 0 at the end, so the last step before landing sits on
    the target instead of a tremor offset away.
    """
    share = min(max(share, 0.0), 1.0)
    onset = min(share / 0.1, 1.0)
    homing = min(max((share - 0.6) / 0.4, 0.0), 1.0)
    gain = onset * (0.4 + 0.6 * homing)
    if fade_share > 0.0:
        gain *= min((1.0 - share) / fade_share, 1.0) ** 2
    return gain


@dataclass(frozen=True)
class Tremor:
    """Physiological tremor plus slow postural drift, added to a planned path.

    Per axis: ``A * sin(2 pi f t + phase)`` with ``f`` drawn from
    ``frequency_hz`` and ``A`` from ``amplitude_px``, plus a drift sinusoid of
    0.2-1.0 Hz and amplitude up to ``drift_px``. Both are multiplied by
    ``tremor_envelope(t / T, fade)`` and scaled down so tremor plus drift
    stays at most a quarter of the target size. The fade lasts
    ``max(4 / sample_hz, 0.05 T)`` (at most a quarter of ``T``): two polls
    before the end at most a third of the tremor is left, so the last step
    before landing is within one pixel of the target.

    Ranges: ``frequency_hz`` [4, 15] Hz, ``amplitude_px`` [0, 2] px,
    ``drift_px`` [0, 3] px. Cross-check: ``amplitude_px[1] + drift_px <= 4``.
    """

    frequency_hz: tuple[float, float] = (8.0, 12.0)
    amplitude_px: tuple[float, float] = (0.3, 1.2)
    drift_px: float = 1.0

    def __post_init__(self) -> None:
        _store(
            self,
            "frequency_hz",
            _span("Tremor.frequency_hz", self.frequency_hz, 4.0, 15.0, " Hz"),
        )
        _store(
            self,
            "amplitude_px",
            _span("Tremor.amplitude_px", self.amplitude_px, 0.0, 2.0, " px"),
        )
        _store(
            self, "drift_px", _number("Tremor.drift_px", self.drift_px, 0.0, 3.0, " px")
        )
        if self.amplitude_px[1] + self.drift_px > _MAX_JITTER_PX:
            raise InvalidArgumentError(
                f"Tremor.amplitude_px[1] + drift_px must be <= {_MAX_JITTER_PX} px, "
                f"got {self.amplitude_px[1] + self.drift_px}"
            )

    def offsets(
        self,
        rng: random.Random,
        duration: float,
        target_size: float,
        sample_hz: float = 125.0,
    ) -> Callable[[float], _FloatPoint]:
        """``t -> (dx, dy)`` for one move of ``duration`` seconds."""
        fade_share = 0.0
        if duration > 0.0:
            fade = max(_TREMOR_FADE_POLLS / sample_hz, _TREMOR_FADE_SHARE * duration)
            fade_share = min(fade / duration, 0.25)
        peak = self.amplitude_px[1] + self.drift_px
        scale = 1.0
        if peak > 0.0:
            scale = min(1.0, _JITTER_TARGET_SHARE * target_size / peak)
        axes = [
            (
                rng.uniform(*self.amplitude_px) * scale,
                rng.uniform(*self.frequency_hz),
                rng.uniform(0.0, 2.0 * math.pi),
                rng.uniform(0.5, 1.0) * self.drift_px * scale,
                rng.uniform(0.2, 1.0),
                rng.uniform(0.0, 2.0 * math.pi),
            )
            for _ in range(2)
        ]

        def offset(t: float) -> _FloatPoint:
            if duration <= 0.0:
                return 0.0, 0.0
            gain = tremor_envelope(t / duration, fade_share)
            values = [
                gain
                * (
                    amplitude * math.sin(2.0 * math.pi * frequency * t + phase)
                    + drift * math.sin(2.0 * math.pi * drift_hz * t + drift_phase)
                )
                for amplitude, frequency, phase, drift, drift_hz, drift_phase in axes
            ]
            return values[0], values[1]

        return offset


@dataclass(frozen=True)
class Overshoot:
    """How far an aimed move may shoot past its target before correcting.

    With probability ``probability`` (and only when ``D >= 3 W``) the primary
    movement aims past the target along the direction of travel by
    ``clamp(U(share) * D, px[0], px[1])``, capped at ``W / 2 + slack_px``
    (it may leave the target by a few px); a corrective sub-stroke then lands
    exactly on the target.

    Ranges: ``probability`` [0, 1], ``share`` [0, 0.15], ``px`` [0, 80] px,
    ``slack_px`` [0, 10] px.
    """

    probability: float = 0.25
    share: tuple[float, float] = (0.02, 0.06)
    px: tuple[float, float] = (4.0, 40.0)
    slack_px: float = 4.0

    def __post_init__(self) -> None:
        _store(
            self,
            "probability",
            _number("Overshoot.probability", self.probability, 0.0, 1.0),
        )
        _store(self, "share", _span("Overshoot.share", self.share, 0.0, 0.15))
        _store(self, "px", _span("Overshoot.px", self.px, 0.0, 80.0, " px"))
        _store(
            self,
            "slack_px",
            _number("Overshoot.slack_px", self.slack_px, 0.0, 10.0, " px"),
        )

    def cap(self, target_size: float) -> float:
        """Largest overshoot past the target centre, in px."""
        return min(self.px[1], target_size / 2.0 + self.slack_px)

    def amount(self, rng: random.Random, distance: float, target_size: float) -> float:
        """Overshoot in px for one move; 0.0 when it does not overshoot."""
        roll = rng.random()
        if roll >= self.probability or distance < _OVERSHOOT_MIN_RATIO * target_size:
            return 0.0
        wanted = min(max(rng.uniform(*self.share) * distance, self.px[0]), self.px[1])
        return min(wanted, self.cap(target_size))


@dataclass(frozen=True)
class LognormalStroke:
    """One sigma-lognormal stroke (Plamondon's kinematic theory).

    Speed: ``v(t) = D / (sigma sqrt(2 pi) (t - t0)) *
    exp(-(ln(t - t0) - mu)^2 / (2 sigma^2))`` for ``t > t0``. The stroke is
    cut to ``ln(t - t0)`` in ``[mu - 3.2 sigma, mu + 3 sigma]`` and starts at
    ``t = 0``, so with duration ``T``: ``mu = ln(T / (exp(3 sigma) -
    exp(-3.2 sigma)))`` and ``t0 = -exp(mu - 3.2 sigma)``. ``progress(t)``
    is the lognormal CDF ``Phi((ln(t - t0) - mu) / sigma)`` renormalised to
    run exactly from 0 at ``t = 0`` to 1 at ``t = T``.

    The speed peaks at ``t - t0 = exp(mu - sigma^2)``; for sigma in [0.08,
    0.16] that is 37-44 % into the stroke (``peak_share``), and the speed at
    both cut points is below 1 % of the peak.
    """

    distance: float
    duration: float
    sigma: float

    @property
    def mu(self) -> float:
        span = math.exp(_SIGMAS_AFTER * self.sigma) - math.exp(
            -_SIGMAS_BEFORE * self.sigma
        )
        return math.log(self.duration / span)

    @property
    def t0(self) -> float:
        return -math.exp(self.mu - _SIGMAS_BEFORE * self.sigma)

    @property
    def peak_share(self) -> float:
        """Share of the duration at which the speed peaks."""
        return (math.exp(self.mu - self.sigma * self.sigma) + self.t0) / self.duration

    def time_at(self, sigmas: float) -> float:
        """Stroke time at which ``ln(t - t0) = mu + sigmas * sigma``."""
        return math.exp(self.mu + sigmas * self.sigma) + self.t0

    @staticmethod
    def share_at(sigma: float, sigmas: float) -> float:
        """``time_at(sigmas) / duration`` for any stroke with this ``sigma``."""
        low = math.exp(-_SIGMAS_BEFORE * sigma)
        return (math.exp(sigmas * sigma) - low) / (
            math.exp(_SIGMAS_AFTER * sigma) - low
        )

    def speed(self, t: float) -> float:
        """Unnormalised ``v(t)`` in px/s (the formula in the class docstring)."""
        elapsed = t - self.t0
        if elapsed <= 0.0:
            return 0.0
        z = (math.log(elapsed) - self.mu) / self.sigma
        return (
            self.distance
            / (self.sigma * math.sqrt(2.0 * math.pi) * elapsed)
            * math.exp(-0.5 * z * z)
        )

    def progress(self, t: float) -> float:
        """Share of ``distance`` covered at ``t`` (0 at ``t <= 0``, 1 at ``t >= T``)."""
        if t <= 0.0:
            return 0.0
        if t >= self.duration:
            return 1.0
        z = (math.log(t - self.t0) - self.mu) / self.sigma
        low = _phi(-_SIGMAS_BEFORE)
        return (_phi(z) - low) / (_phi(_SIGMAS_AFTER) - low)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class _Superposed:
    """One stroke of a trajectory: starts at ``onset``, moves ``displacement(share)``."""

    onset: float
    stroke: LognormalStroke
    displacement: Callable[[float], _FloatPoint]


@dataclass(frozen=True)
class Trajectory:
    """Noise-free SigmaDrift path: the vector sum of overlapping strokes.

    ``position(t) = origin + sum_i displacement_i(progress_i(t - onset_i))``.
    ``kind`` is ``single``, ``undershoot`` or ``overshoot``; ``junctions``
    are the stroke start and end times.
    """

    origin: _FloatPoint
    duration: float
    kind: str
    strokes: tuple[_Superposed, ...]

    def position(self, t: float) -> _FloatPoint:
        x, y = self.origin
        for part in self.strokes:
            dx, dy = part.displacement(part.stroke.progress(t - part.onset))
            x += dx
            y += dy
        return x, y

    @property
    def junctions(self) -> tuple[float, ...]:
        times = {p.onset for p in self.strokes}
        times |= {p.onset + p.stroke.duration for p in self.strokes}
        return tuple(sorted(times))

    def stroke(self, index: int) -> tuple[float, LognormalStroke]:
        """``(onset, stroke)`` of stroke ``index`` (0 is the primary)."""
        part = self.strokes[index]
        return part.onset, part.stroke


def _minimum_jerk(tau: float) -> float:
    """``s(tau) = 10 tau^3 - 15 tau^4 + 6 tau^5`` on ``[0, 1]``."""
    tau = min(max(tau, 0.0), 1.0)
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


class _Polyline:
    """Arc-length parametrisation of a densely sampled curve."""

    def __init__(self, points: Sequence[_FloatPoint]) -> None:
        self._points = list(points)
        self._lengths = [0.0]
        for (ax, ay), (bx, by) in zip(self._points, self._points[1:]):
            self._lengths.append(self._lengths[-1] + math.hypot(bx - ax, by - ay))

    @property
    def length(self) -> float:
        return self._lengths[-1]

    def at(self, share: float) -> _FloatPoint:
        """Point after ``share`` (0..1) of the arc length."""
        if self.length <= 0.0 or share <= 0.0:
            return self._points[0]
        if share >= 1.0:
            return self._points[-1]
        wanted = share * self.length
        low, high = 0, len(self._lengths) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self._lengths[middle] < wanted:
                low = middle
            else:
                high = middle
        segment = self._lengths[high] - self._lengths[low]
        part = 0.0 if segment <= 0.0 else (wanted - self._lengths[low]) / segment
        (ax, ay), (bx, by) = self._points[low], self._points[high]
        return ax + (bx - ax) * part, ay + (by - ay) * part


def _sample_times(duration: float, sample_hz: float, rng: random.Random) -> list[float]:
    """Polling instants in ``(0, duration]``; the last one is ``duration``."""
    period = 1.0 / sample_hz
    times: list[float] = []
    t = 0.0
    while True:
        t += period * rng.uniform(0.9, 1.1)
        if t >= duration - 0.25 * period:
            break
        times.append(t)
    times.append(duration)
    return times


def _finalize(
    samples: Sequence[_Sample],
    start: Point,
    end: Point,
    duration: float,
    bounds: ScreenBounds | None,
) -> list[MotionStep]:
    """Round, keep inside ``bounds``, merge repeats and land exactly on ``end``.

    The ``dt`` of a merged repeat moves to the next step, so the step delays
    still add up to ``duration``.
    """
    steps: list[MotionStep] = []
    last = start
    emitted_time = 0.0
    for t, x, y in samples:
        point = (round(x), round(y))
        if bounds is not None:
            point = bounds.settle(point)
        if point == last:
            continue
        steps.append(MotionStep(point[0], point[1], max(0.0, t - emitted_time)))
        emitted_time = t
        last = point
    # An intermediate that already reached ``end`` stays: the cursor arrives
    # when it arrives, and the landing step then confirms it in place.
    steps.append(MotionStep(end[0], end[1], max(0.0, duration - emitted_time)))
    return steps


def _unit(start: Point, end: Point) -> tuple[float, float, float]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance <= 0.0:
        return 0.0, 0.0, 0.0
    return dx / distance, dy / distance, distance


def _bezier(p0: float, p1: float, p2: float, p3: float, u: float) -> float:
    v = 1.0 - u
    return v * v * v * p0 + 3 * v * v * u * p1 + 3 * v * u * u * p2 + u * u * u * p3


# -- planners -----------------------------------------------------------------


@runtime_checkable
class MotionPlanner(Protocol):
    """Plans one pointer move; see the module docstring for the guarantees."""

    def plan(
        self,
        start: Point,
        end: Point,
        *,
        target_size: float,
        rng: random.Random,
        bounds: ScreenBounds | None = None,
    ) -> list[MotionStep]: ...


@dataclass(frozen=True)
class LinearPlanner:
    """One exact step onto ``end`` with no delay (no humanization)."""

    def plan(
        self,
        start: Point,
        end: Point,
        *,
        target_size: float,
        rng: random.Random,
        bounds: ScreenBounds | None = None,
    ) -> list[MotionStep]:
        _target_width(target_size)
        return [MotionStep(end[0], end[1], 0.0)]


@dataclass(frozen=True)
class SigmaDriftPlanner:
    """Sigma-lognormal strokes along a gently curved path (the default planner).

    1. Total time ``MT`` comes from ``timing`` (Fitts' law).
    2. The move is one of:
       - overshoot (``overshoot``): the primary stroke aims at a point past
         the target; a corrective stroke brings it back onto the target;
       - undershoot (``correction_probability`` of the rest): the primary
         stroke covers ``primary_fraction`` (93-98 %) of the path; a
         corrective stroke covers the rest;
       - otherwise one stroke lasting ``MT``.
    3. Each stroke follows ``LognormalStroke`` with ``sigma`` drawn from
       ``sigma``: it starts and ends below 1 % of its peak speed, which falls
       37-44 % into the stroke.
    4. Strokes superpose (Plamondon): the corrective stroke starts while the
       primary still decelerates, at the primary's ``mu + k sigma`` with
       ``k`` from ``correction_onset``, and lasts ``r`` primary durations
       (``r`` from ``correction_ratio``, raised so the primary is over by
       ``MT``; for an overshoot also raised so the returning stroke peaks no
       earlier than the primary ends, which makes the motion turn back
       exactly once). The position is the vector sum of both displacements,
       so the speed never jumps and an overshoot turns back smoothly.
       Durations are solved so the corrective stroke ends exactly at ``MT``.
    5. The primary follows a path bent to a random side by ``curvature *
       D`` at its middle (parabolic offset ``4 u (1 - u)``), walked by arc
       length; the corrective displacement is straight.
    6. ``tremor`` adds tremor and drift; samples come at ``sample_hz``
       (+-10 % polling jitter), ``dt`` being the time between samples.

    Ranges: ``sigma`` [0.08, 0.16], ``primary_fraction`` [0.85, 1.0],
    ``correction_probability`` [0, 1], ``correction_onset`` [0.5, 2.5]
    sigma, ``correction_ratio`` [0.3, 1.0], ``curvature`` [0, 0.3],
    ``sample_hz`` [60, 1000] Hz.
    """

    timing: FittsTiming = field(default_factory=FittsTiming)
    sigma: tuple[float, float] = (0.10, 0.15)
    primary_fraction: tuple[float, float] = (0.93, 0.98)
    correction_probability: float = 0.65
    correction_onset: tuple[float, float] = (1.0, 1.5)
    correction_ratio: tuple[float, float] = (0.45, 0.65)
    curvature: tuple[float, float] = (0.02, 0.08)
    overshoot: Overshoot = field(default_factory=Overshoot)
    tremor: Tremor = field(default_factory=Tremor)
    sample_hz: float = 125.0

    def __post_init__(self) -> None:
        name = "SigmaDriftPlanner"
        _require_type(f"{name}.timing", self.timing, FittsTiming)
        _require_type(f"{name}.overshoot", self.overshoot, Overshoot)
        _require_type(f"{name}.tremor", self.tremor, Tremor)
        _store(self, "sigma", _span(f"{name}.sigma", self.sigma, *_SIGMA_RANGE))
        _store(
            self,
            "primary_fraction",
            _span(f"{name}.primary_fraction", self.primary_fraction, 0.85, 1.0),
        )
        _store(
            self,
            "correction_probability",
            _number(
                f"{name}.correction_probability", self.correction_probability, 0.0, 1.0
            ),
        )
        _store(
            self,
            "correction_onset",
            _span(f"{name}.correction_onset", self.correction_onset, 0.5, 2.5),
        )
        _store(
            self,
            "correction_ratio",
            _span(f"{name}.correction_ratio", self.correction_ratio, 0.3, 1.0),
        )
        _store(self, "curvature", _span(f"{name}.curvature", self.curvature, 0.0, 0.3))
        _store(self, "sample_hz", _sample_hz(f"{name}.sample_hz", self.sample_hz))

    @classmethod
    def preset(cls, name: str) -> SigmaDriftPlanner:
        """``natural`` (default), ``careful`` (slower, more corrections), ``brisk``."""
        name = _preset_name(name, "SigmaDriftPlanner")
        if name == "careful":
            return cls(
                timing=FittsTiming(
                    a=0.12, b=0.15, variability=0.10, min_duration=0.2, max_duration=1.6
                ),
                sigma=(0.11, 0.16),
                primary_fraction=(0.93, 0.96),
                correction_probability=0.9,
                correction_onset=(1.0, 1.4),
                correction_ratio=(0.5, 0.75),
                curvature=(0.02, 0.06),
                overshoot=Overshoot(probability=0.35),
                tremor=Tremor(amplitude_px=(0.3, 0.8), drift_px=0.8),
                sample_hz=150.0,
            )
        if name == "brisk":
            return cls(
                timing=FittsTiming(
                    a=0.08,
                    b=0.11,
                    variability=0.15,
                    min_duration=0.12,
                    max_duration=0.9,
                ),
                sigma=(0.08, 0.13),
                primary_fraction=(0.95, 0.98),
                correction_probability=0.4,
                correction_onset=(1.2, 1.5),
                correction_ratio=(0.4, 0.55),
                curvature=(0.03, 0.1),
                overshoot=Overshoot(probability=0.15),
                tremor=Tremor(amplitude_px=(0.4, 1.2), drift_px=1.2),
                sample_hz=125.0,
            )
        return cls()

    def plan(
        self,
        start: Point,
        end: Point,
        *,
        target_size: float,
        rng: random.Random,
        bounds: ScreenBounds | None = None,
    ) -> list[MotionStep]:
        width = _target_width(target_size)
        _, _, distance = _unit(start, end)
        if distance < 1.0:
            return [MotionStep(end[0], end[1], 0.0)]
        trajectory = self.trajectory(start, end, target_size=width, rng=rng)
        total = trajectory.duration
        jitter = self.tremor.offsets(rng, total, width, self.sample_hz)
        samples: list[_Sample] = []
        for t in _sample_times(total, self.sample_hz, rng):
            x, y = trajectory.position(t)
            dx, dy = jitter(t)
            samples.append((t, x + dx, y + dy))
        return _finalize(samples, start, end, total, bounds)

    def trajectory(
        self, start: Point, end: Point, *, target_size: float, rng: random.Random
    ) -> Trajectory:
        """The noise-free stroke superposition ``plan`` samples (steps 1-4)."""
        width = _target_width(target_size)
        ux, uy, distance = _unit(start, end)
        total = self.timing.duration(distance, width, rng)
        overshoot = self.overshoot.amount(rng, distance, width)
        if overshoot > 0.0:
            kind, fraction = "overshoot", 1.0
        elif rng.random() < self.correction_probability:
            kind, fraction = "undershoot", rng.uniform(*self.primary_fraction)
        else:
            kind, fraction = "single", 1.0
        reach = distance + overshoot
        bend = reach * rng.uniform(*self.curvature) * rng.choice((-1.0, 1.0))
        path = _Polyline(
            [
                (
                    start[0] + ux * reach * u - uy * bend * 4.0 * u * (1.0 - u),
                    start[1] + uy * reach * u + ux * bend * 4.0 * u * (1.0 - u),
                )
                for u in (i / _PATH_SAMPLES for i in range(_PATH_SAMPLES + 1))
            ]
        )
        origin = (float(start[0]), float(start[1]))

        def along_path(share: float) -> _FloatPoint:
            x, y = path.at(fraction * share)
            return x - origin[0], y - origin[1]

        sigma = rng.uniform(*self.sigma)
        if kind == "single":
            primary = LognormalStroke(reach, total, sigma)
            return Trajectory(
                origin, total, kind, (_Superposed(0.0, primary, along_path),)
            )
        # The corrective stroke starts while the primary decelerates (at its
        # mu + k sigma) and lasts ``ratio`` primary durations; the primary
        # must be over by the end: onset_share + ratio >= 1.
        onset_share = LognormalStroke.share_at(
            sigma, rng.uniform(*self.correction_onset)
        )
        ratio = max(rng.uniform(*self.correction_ratio), 1.0 - onset_share)
        corrective_sigma = rng.uniform(*self.sigma)
        if kind == "overshoot":
            # The returning stroke peaks no earlier than the primary ends:
            # until then it speeds up while the primary slows down, so the
            # net motion along the path turns back exactly once.
            peak_share = LognormalStroke.share_at(corrective_sigma, -corrective_sigma)
            ratio = max(ratio, (1.0 - onset_share) / peak_share)
        primary_time = total / (onset_share + ratio)
        primary = LognormalStroke(reach * fraction, primary_time, sigma)
        reached = path.at(fraction)
        gap = (end[0] - reached[0], end[1] - reached[1])
        corrective = LognormalStroke(
            math.hypot(*gap), ratio * primary_time, corrective_sigma
        )

        def closing(share: float) -> _FloatPoint:
            return gap[0] * share, gap[1] * share

        return Trajectory(
            origin,
            total,
            kind,
            (
                _Superposed(0.0, primary, along_path),
                _Superposed(onset_share * primary_time, corrective, closing),
            ),
        )


@dataclass(frozen=True)
class BezierTremorPlanner:
    """Cubic Bezier with minimum-jerk speed along its arc length.

    1. Total time ``MT`` comes from ``timing`` (Fitts' law).
    2. Control points sit at 30 % and 70 % of the chord, offset perpendicular
       by ``curvature * D`` (the second by 30-100 % of the first, mirrored
       with probability ``s_curve_probability``) to a random side.
    3. Position along the arc length follows ``s(tau) = 10 tau^3 - 15 tau^4 +
       6 tau^5``.
    4. ``overshoot`` may aim the curve past the target; a straight
       minimum-jerk approach of ``c MT`` (``c`` from ``correction_share``)
       then comes back onto it.
    5. ``tremor`` adds tremor and drift; samples come at ``sample_hz``.

    Ranges: ``curvature`` [0, 0.3], ``s_curve_probability`` [0, 1],
    ``correction_share`` [0.05, 0.4], ``sample_hz`` [60, 1000] Hz.
    """

    timing: FittsTiming = field(default_factory=FittsTiming)
    curvature: tuple[float, float] = (0.05, 0.15)
    s_curve_probability: float = 0.2
    overshoot: Overshoot = field(default_factory=Overshoot)
    correction_share: tuple[float, float] = (0.15, 0.25)
    tremor: Tremor = field(
        default_factory=lambda: Tremor(amplitude_px=(0.5, 1.5), drift_px=1.0)
    )
    sample_hz: float = 125.0

    def __post_init__(self) -> None:
        name = "BezierTremorPlanner"
        _require_type(f"{name}.timing", self.timing, FittsTiming)
        _require_type(f"{name}.overshoot", self.overshoot, Overshoot)
        _require_type(f"{name}.tremor", self.tremor, Tremor)
        _store(self, "curvature", _span(f"{name}.curvature", self.curvature, 0.0, 0.3))
        _store(
            self,
            "s_curve_probability",
            _number(f"{name}.s_curve_probability", self.s_curve_probability, 0.0, 1.0),
        )
        _store(
            self,
            "correction_share",
            _span(f"{name}.correction_share", self.correction_share, 0.05, 0.4),
        )
        _store(self, "sample_hz", _sample_hz(f"{name}.sample_hz", self.sample_hz))

    @classmethod
    def preset(cls, name: str) -> BezierTremorPlanner:
        """``natural`` (default), ``careful`` (flatter, more corrections), ``brisk``."""
        name = _preset_name(name, "BezierTremorPlanner")
        if name == "careful":
            return cls(
                timing=FittsTiming(
                    a=0.12, b=0.15, variability=0.10, min_duration=0.2, max_duration=1.6
                ),
                curvature=(0.03, 0.1),
                s_curve_probability=0.1,
                overshoot=Overshoot(probability=0.35),
                correction_share=(0.2, 0.3),
                tremor=Tremor(amplitude_px=(0.5, 1.0), drift_px=0.8),
                sample_hz=150.0,
            )
        if name == "brisk":
            return cls(
                timing=FittsTiming(
                    a=0.08,
                    b=0.11,
                    variability=0.15,
                    min_duration=0.12,
                    max_duration=0.9,
                ),
                curvature=(0.05, 0.18),
                s_curve_probability=0.25,
                overshoot=Overshoot(probability=0.15),
                correction_share=(0.12, 0.2),
                tremor=Tremor(amplitude_px=(0.6, 1.5), drift_px=1.2),
                sample_hz=125.0,
            )
        return cls()

    def plan(
        self,
        start: Point,
        end: Point,
        *,
        target_size: float,
        rng: random.Random,
        bounds: ScreenBounds | None = None,
    ) -> list[MotionStep]:
        width = _target_width(target_size)
        ux, uy, distance = _unit(start, end)
        if distance < 1.0:
            return [MotionStep(end[0], end[1], 0.0)]
        total = self.timing.duration(distance, width, rng)
        overshoot = self.overshoot.amount(rng, distance, width)
        aim = (end[0] + ux * overshoot, end[1] + uy * overshoot)
        main_time = total
        if overshoot > 0.0:
            main_time = total * (1.0 - rng.uniform(*self.correction_share))
        side = rng.choice((-1.0, 1.0))
        bend = distance * rng.uniform(*self.curvature)
        bend2 = bend * rng.uniform(0.3, 1.0)
        if rng.random() < self.s_curve_probability:
            bend2 = -bend2
        sx, sy = float(start[0]), float(start[1])
        ax, ay = aim
        nx, ny = -uy * side, ux * side
        c1 = (sx + (ax - sx) * 0.3 + nx * bend, sy + (ay - sy) * 0.3 + ny * bend)
        c2 = (sx + (ax - sx) * 0.7 + nx * bend2, sy + (ay - sy) * 0.7 + ny * bend2)
        main = _Polyline(
            [
                (_bezier(sx, c1[0], c2[0], ax, u), _bezier(sy, c1[1], c2[1], ay, u))
                for u in (i / _PATH_SAMPLES for i in range(_PATH_SAMPLES + 1))
            ]
        )
        approach = _Polyline([aim, (float(end[0]), float(end[1]))])
        jitter = self.tremor.offsets(rng, total, width, self.sample_hz)
        samples: list[_Sample] = []
        for t in _sample_times(total, self.sample_hz, rng):
            if t <= main_time:
                x, y = main.at(_minimum_jerk(t / main_time))
            else:
                x, y = approach.at(
                    _minimum_jerk((t - main_time) / max(total - main_time, 1e-9))
                )
            dx, dy = jitter(t)
            samples.append((t, x + dx, y + dy))
        return _finalize(samples, start, end, total, bounds)


@dataclass(frozen=True)
class WindMousePlanner:
    """Benjamin Land's WindMouse: gravity pulls, wind wanders, steps are capped.

    Per iteration, with ``dist`` the remaining distance: outside
    ``target_area`` the wind becomes ``w / sqrt(3) + U(-1, 1) * min(wind,
    dist) / sqrt(5)``; inside it the wind is damped by ``sqrt(3)`` and the
    step cap shrinks by ``sqrt(5)`` (re-drawn in [3, 6) px below 3 px).
    Velocity gains ``wind + gravity * direction``; above the cap it is cut to
    ``U(cap / 2, cap)``. The walk stops within 1 px and lands on the target.

    Timing: ``MT`` comes from ``timing`` (Fitts' law); walk step ``i`` of
    length ``l_i`` is reached at ``MT * sum(sqrt(l_1..l_i)) / sum(sqrt(l))``
    (speed rises with step length). Steps are then emitted at the mouse
    polling rate ``sample_hz``: each poll reports the latest walk position,
    so no two steps come closer than one polling period.

    Ranges: ``gravity`` [1, 20], ``wind`` [0, 10], ``max_step`` [3, 50] px,
    ``target_area`` [1, 50] px, ``sample_hz`` [60, 1000] Hz. Cross-check:
    ``wind <= gravity`` (the walk must converge).
    """

    timing: FittsTiming = field(default_factory=FittsTiming)
    gravity: float = 9.0
    wind: float = 3.0
    max_step: float = 15.0
    target_area: float = 12.0
    sample_hz: float = 125.0

    def __post_init__(self) -> None:
        name = "WindMousePlanner"
        _require_type(f"{name}.timing", self.timing, FittsTiming)
        _store(self, "gravity", _number(f"{name}.gravity", self.gravity, 1.0, 20.0))
        _store(self, "wind", _number(f"{name}.wind", self.wind, 0.0, 10.0))
        _store(
            self,
            "max_step",
            _number(f"{name}.max_step", self.max_step, 3.0, 50.0, " px"),
        )
        _store(
            self,
            "target_area",
            _number(f"{name}.target_area", self.target_area, 1.0, 50.0, " px"),
        )
        _store(self, "sample_hz", _sample_hz(f"{name}.sample_hz", self.sample_hz))
        if self.wind > self.gravity:
            raise InvalidArgumentError(
                f"{name}.wind must not exceed gravity ({self.gravity}), got {self.wind}"
            )

    @classmethod
    def preset(cls, name: str) -> WindMousePlanner:
        """``natural`` (Land's defaults), ``careful`` (calmer), ``brisk`` (longer steps)."""
        name = _preset_name(name, "WindMousePlanner")
        if name == "careful":
            return cls(
                timing=FittsTiming(
                    a=0.12, b=0.15, variability=0.10, min_duration=0.2, max_duration=1.6
                ),
                gravity=9.0,
                wind=2.0,
                max_step=10.0,
                target_area=14.0,
            )
        if name == "brisk":
            return cls(
                timing=FittsTiming(
                    a=0.08,
                    b=0.11,
                    variability=0.15,
                    min_duration=0.12,
                    max_duration=0.9,
                ),
                gravity=11.0,
                wind=3.0,
                max_step=22.0,
                target_area=10.0,
            )
        return cls()

    def _walk(self, start: Point, end: Point, rng: random.Random) -> list[_FloatPoint]:
        sqrt3, sqrt5 = math.sqrt(3.0), math.sqrt(5.0)
        x, y = float(start[0]), float(start[1])
        vx = vy = wx = wy = 0.0
        step_cap = self.max_step
        points: list[_FloatPoint] = []
        for _ in range(_WIND_MAX_ITERATIONS):
            dist = math.hypot(end[0] - x, end[1] - y)
            if dist < 1.0:
                break
            wind = min(self.wind, dist)
            if dist >= self.target_area:
                wx = wx / sqrt3 + (2.0 * rng.random() - 1.0) * wind / sqrt5
                wy = wy / sqrt3 + (2.0 * rng.random() - 1.0) * wind / sqrt5
            else:
                wx /= sqrt3
                wy /= sqrt3
                if step_cap < 3.0:
                    step_cap = rng.random() * 3.0 + 3.0
                else:
                    step_cap /= sqrt5
            vx += wx + self.gravity * (end[0] - x) / dist
            vy += wy + self.gravity * (end[1] - y) / dist
            speed = math.hypot(vx, vy)
            if speed > step_cap:
                clipped = step_cap / 2.0 + rng.random() * step_cap / 2.0
                vx, vy = vx / speed * clipped, vy / speed * clipped
            x += vx
            y += vy
            points.append((x, y))
        return points

    def plan(
        self,
        start: Point,
        end: Point,
        *,
        target_size: float,
        rng: random.Random,
        bounds: ScreenBounds | None = None,
    ) -> list[MotionStep]:
        width = _target_width(target_size)
        _, _, distance = _unit(start, end)
        if distance < 1.0:
            return [MotionStep(end[0], end[1], 0.0)]
        total = self.timing.duration(distance, width, rng)
        points = [*self._walk(start, end, rng), (float(end[0]), float(end[1]))]
        weights: list[float] = []
        previous: _FloatPoint = (float(start[0]), float(start[1]))
        for point in points:
            length = math.hypot(point[0] - previous[0], point[1] - previous[1])
            weights.append(math.sqrt(max(length, 0.01)))
            previous = point
        scale = total / sum(weights)
        period = 1.0 / self.sample_hz
        # poll index -> latest walk position reported at that poll
        polls: dict[int, _FloatPoint] = {}
        elapsed = 0.0
        for weight, point in zip(weights[:-1], points[:-1]):
            elapsed += weight * scale
            poll = math.ceil(elapsed / period - 1e-9)
            if poll * period >= total:
                break  # reported together with the landing
            polls[poll] = point
        samples: list[_Sample] = [
            (poll * period, x, y) for poll, (x, y) in sorted(polls.items())
        ]
        return _finalize(samples, start, end, total, bounds)


def resolve_motion(motion: MotionPlanner | str | None) -> MotionPlanner:
    """The planner for a ``motion=`` argument.

    ``None`` -> ``SigmaDriftPlanner.preset("natural")``; a preset name ->
    that ``SigmaDriftPlanner`` preset; a ``MotionPlanner`` -> itself.
    """
    if motion is None:
        return SigmaDriftPlanner.preset("natural")
    if isinstance(motion, str):
        if motion not in PRESET_NAMES:
            raise InvalidArgumentError(
                f"motion must be one of {PRESET_NAMES}, a MotionPlanner or None, "
                f"got {motion!r}"
            )
        return SigmaDriftPlanner.preset(motion)
    if isinstance(motion, MotionPlanner) and callable(getattr(motion, "plan", None)):
        return motion
    raise InvalidArgumentError(
        f"motion must be one of {PRESET_NAMES}, a MotionPlanner or None, "
        f"got {type(motion).__name__}"
    )
