"""The Stage 5 case set (evaluation/protocol.md §2): 7 categories x 10 templates, 5 development and
5 held-out. Held-out templates differ in substance (durations, intensities, onset times, window
lengths and distractors not used in development), not only in seed. FIXTURE CASES.

Times are seconds from the window start. Rates are transfers per second; capacity is about 8/s,
and the healthy profile is 3.2/s, so every rate here stays under 50 % of capacity."""

from __future__ import annotations

from dataclasses import dataclass, field

LATE = "Customers say that transfer notifications are arriving late."
FAILING = "Some customers say their transfers are failing or taking several seconds."

TRUTH = {
    "consumer_unavailable": "consumer_unavailable",
    "slow_consumer": "slow_consumer_handler",
    "credential_rejection": "consumer_credential_rejected",
    "lock_contention": "lock_contention",
    "lock_pool_exhaustion": "lock_contention+pool_saturation",
    "healthy": "none",
    "missing_evidence": "insufficient_evidence",
}


@dataclass(frozen=True)
class Template:
    template_id: str
    category: str
    split: str  # "development" or "held_out"
    window_seconds: int
    rate: float
    symptom: str
    fault: str | None = None  # unavailable, slow, reject, locks, locks_small_pool
    fault_start: int = 0
    fault_seconds: int = 0
    delay_ms: int = 0
    locked_fraction: float = 0.0
    pool_size: int = 0
    distractor: str = "none"  # none, overspend, replays, load_bump, overspend_and_replays
    distractor_start: int = 0
    distractor_seconds: int = 0
    stop: tuple[str, ...] = field(default_factory=tuple)  # missing evidence: collector, prometheus
    deciding_source: str | None = None  # missing evidence: logs or blockers


def t(template_id, category, split, window, rate, symptom, **kw) -> Template:
    return Template(template_id, category, split, window, rate, symptom, **kw)


D, H = "development", "held_out"

TEMPLATES: list[Template] = [
    # consumer unavailable
    t("cu-d1", "consumer_unavailable", D, 150, 3.2, LATE, fault="unavailable", fault_start=30, fault_seconds=90),
    t("cu-d2", "consumer_unavailable", D, 180, 3.0, LATE, fault="unavailable", fault_start=40, fault_seconds=60,
      distractor="overspend", distractor_start=20, distractor_seconds=120),
    t("cu-d3", "consumer_unavailable", D, 150, 2.6, LATE, fault="unavailable", fault_start=20, fault_seconds=100,
      distractor="replays", distractor_start=10, distractor_seconds=120),
    t("cu-d4", "consumer_unavailable", D, 200, 3.4, LATE, fault="unavailable", fault_start=60, fault_seconds=80),
    t("cu-d5", "consumer_unavailable", D, 150, 3.2, LATE, fault="unavailable", fault_start=30, fault_seconds=45,
      distractor="overspend", distractor_start=60, distractor_seconds=60),
    t("cu-h1", "consumer_unavailable", H, 240, 2.2, LATE, fault="unavailable", fault_start=90, fault_seconds=120,
      distractor="load_bump", distractor_start=20, distractor_seconds=50),
    t("cu-h2", "consumer_unavailable", H, 180, 3.6, LATE, fault="unavailable", fault_start=15, fault_seconds=150,
      distractor="overspend_and_replays", distractor_start=30, distractor_seconds=100),
    t("cu-h3", "consumer_unavailable", H, 210, 3.0, LATE, fault="unavailable", fault_start=125, fault_seconds=60,
      distractor="load_bump", distractor_start=40, distractor_seconds=40),
    t("cu-h4", "consumer_unavailable", H, 150, 2.8, LATE, fault="unavailable", fault_start=10, fault_seconds=35),
    t("cu-h5", "consumer_unavailable", H, 240, 3.0, LATE, fault="unavailable", fault_start=30, fault_seconds=185,
      distractor="overspend_and_replays", distractor_start=100, distractor_seconds=80),
    # slow consumer (handler delay always below LedgerCore's 3 s delivery timeout)
    t("sc-d1", "slow_consumer", D, 150, 3.2, LATE, fault="slow", fault_start=30, fault_seconds=90, delay_ms=400),
    t("sc-d2", "slow_consumer", D, 180, 3.0, LATE, fault="slow", fault_start=40, fault_seconds=100, delay_ms=300,
      distractor="overspend", distractor_start=20, distractor_seconds=120),
    t("sc-d3", "slow_consumer", D, 150, 2.6, LATE, fault="slow", fault_start=20, fault_seconds=110, delay_ms=500,
      distractor="replays", distractor_start=30, distractor_seconds=90),
    t("sc-d4", "slow_consumer", D, 200, 3.4, LATE, fault="slow", fault_start=50, fault_seconds=120, delay_ms=600),
    t("sc-d5", "slow_consumer", D, 150, 3.2, LATE, fault="slow", fault_start=30, fault_seconds=80, delay_ms=450,
      distractor="overspend", distractor_start=50, distractor_seconds=60),
    t("sc-h1", "slow_consumer", H, 240, 2.2, LATE, fault="slow", fault_start=60, fault_seconds=160, delay_ms=900,
      distractor="load_bump", distractor_start=20, distractor_seconds=40),
    t("sc-h2", "slow_consumer", H, 180, 3.6, LATE, fault="slow", fault_start=15, fault_seconds=150, delay_ms=250,
      distractor="overspend_and_replays", distractor_start=40, distractor_seconds=100),
    t("sc-h3", "slow_consumer", H, 210, 3.0, LATE, fault="slow", fault_start=110, fault_seconds=90, delay_ms=1200,
      distractor="load_bump", distractor_start=30, distractor_seconds=50),
    t("sc-h4", "slow_consumer", H, 150, 2.8, LATE, fault="slow", fault_start=10, fault_seconds=60, delay_ms=1600),
    t("sc-h5", "slow_consumer", H, 240, 3.0, LATE, fault="slow", fault_start=30, fault_seconds=200, delay_ms=2400,
      distractor="overspend_and_replays", distractor_start=90, distractor_seconds=90),
    # credential rejection
    t("cr-d1", "credential_rejection", D, 150, 3.2, LATE, fault="reject", fault_start=30, fault_seconds=90),
    t("cr-d2", "credential_rejection", D, 180, 3.0, LATE, fault="reject", fault_start=40, fault_seconds=70,
      distractor="overspend", distractor_start=20, distractor_seconds=120),
    t("cr-d3", "credential_rejection", D, 150, 2.6, LATE, fault="reject", fault_start=20, fault_seconds=100,
      distractor="replays", distractor_start=10, distractor_seconds=120),
    t("cr-d4", "credential_rejection", D, 200, 3.4, LATE, fault="reject", fault_start=60, fault_seconds=90),
    t("cr-d5", "credential_rejection", D, 150, 3.2, LATE, fault="reject", fault_start=30, fault_seconds=50,
      distractor="overspend", distractor_start=60, distractor_seconds=60),
    t("cr-h1", "credential_rejection", H, 240, 2.2, LATE, fault="reject", fault_start=80, fault_seconds=130,
      distractor="load_bump", distractor_start=20, distractor_seconds=50),
    t("cr-h2", "credential_rejection", H, 180, 3.6, LATE, fault="reject", fault_start=15, fault_seconds=150,
      distractor="overspend_and_replays", distractor_start=30, distractor_seconds=100),
    t("cr-h3", "credential_rejection", H, 210, 3.0, LATE, fault="reject", fault_start=130, fault_seconds=55,
      distractor="load_bump", distractor_start=40, distractor_seconds=40),
    t("cr-h4", "credential_rejection", H, 150, 2.8, LATE, fault="reject", fault_start=10, fault_seconds=30),
    t("cr-h5", "credential_rejection", H, 240, 3.0, LATE, fault="reject", fault_start=30, fault_seconds=190,
      distractor="overspend_and_replays", distractor_start=100, distractor_seconds=80),
    # lock contention (locks held by a separate owner session)
    t("lc-d1", "lock_contention", D, 150, 3.2, FAILING, fault="locks", fault_start=30, fault_seconds=90, locked_fraction=0.3),
    t("lc-d2", "lock_contention", D, 180, 3.0, FAILING, fault="locks", fault_start=40, fault_seconds=70, locked_fraction=0.2,
      distractor="overspend", distractor_start=20, distractor_seconds=120),
    t("lc-d3", "lock_contention", D, 150, 2.6, FAILING, fault="locks", fault_start=20, fault_seconds=100, locked_fraction=0.4,
      distractor="replays", distractor_start=10, distractor_seconds=120),
    t("lc-d4", "lock_contention", D, 200, 3.4, FAILING, fault="locks", fault_start=60, fault_seconds=90, locked_fraction=0.3),
    t("lc-d5", "lock_contention", D, 150, 3.2, FAILING, fault="locks", fault_start=30, fault_seconds=50, locked_fraction=0.25,
      distractor="overspend", distractor_start=60, distractor_seconds=60),
    t("lc-h1", "lock_contention", H, 240, 2.2, FAILING, fault="locks", fault_start=80, fault_seconds=130, locked_fraction=0.15,
      distractor="load_bump", distractor_start=20, distractor_seconds=50),
    t("lc-h2", "lock_contention", H, 180, 3.6, FAILING, fault="locks", fault_start=15, fault_seconds=150, locked_fraction=0.5,
      distractor="overspend_and_replays", distractor_start=30, distractor_seconds=100),
    t("lc-h3", "lock_contention", H, 210, 3.0, FAILING, fault="locks", fault_start=130, fault_seconds=55, locked_fraction=0.6,
      distractor="load_bump", distractor_start=40, distractor_seconds=40),
    t("lc-h4", "lock_contention", H, 150, 2.8, FAILING, fault="locks", fault_start=10, fault_seconds=30, locked_fraction=0.35),
    t("lc-h5", "lock_contention", H, 240, 3.0, FAILING, fault="locks", fault_start=30, fault_seconds=190, locked_fraction=0.2,
      distractor="overspend_and_replays", distractor_start=100, distractor_seconds=80),
    # locks exhausting a smaller pool (joint cause, protocol §5.2)
    t("lp-d1", "lock_pool_exhaustion", D, 150, 3.2, FAILING, fault="locks_small_pool", fault_start=30, fault_seconds=90,
      locked_fraction=0.3, pool_size=2),
    t("lp-d2", "lock_pool_exhaustion", D, 180, 3.0, FAILING, fault="locks_small_pool", fault_start=40, fault_seconds=80,
      locked_fraction=0.25, pool_size=2, distractor="overspend", distractor_start=20, distractor_seconds=120),
    t("lp-d3", "lock_pool_exhaustion", D, 150, 2.6, FAILING, fault="locks_small_pool", fault_start=20, fault_seconds=100,
      locked_fraction=0.4, pool_size=3, distractor="replays", distractor_start=10, distractor_seconds=120),
    t("lp-d4", "lock_pool_exhaustion", D, 200, 3.4, FAILING, fault="locks_small_pool", fault_start=60, fault_seconds=90,
      locked_fraction=0.3, pool_size=2),
    t("lp-d5", "lock_pool_exhaustion", D, 150, 3.2, FAILING, fault="locks_small_pool", fault_start=30, fault_seconds=60,
      locked_fraction=0.3, pool_size=2, distractor="overspend", distractor_start=60, distractor_seconds=60),
    t("lp-h1", "lock_pool_exhaustion", H, 240, 2.2, FAILING, fault="locks_small_pool", fault_start=80, fault_seconds=130,
      locked_fraction=0.5, pool_size=1, distractor="load_bump", distractor_start=20, distractor_seconds=50),
    t("lp-h2", "lock_pool_exhaustion", H, 180, 3.6, FAILING, fault="locks_small_pool", fault_start=15, fault_seconds=150,
      locked_fraction=0.2, pool_size=3, distractor="overspend_and_replays", distractor_start=30, distractor_seconds=100),
    t("lp-h3", "lock_pool_exhaustion", H, 210, 3.0, FAILING, fault="locks_small_pool", fault_start=120, fault_seconds=70,
      locked_fraction=0.6, pool_size=2, distractor="load_bump", distractor_start=40, distractor_seconds=40),
    t("lp-h4", "lock_pool_exhaustion", H, 150, 2.8, FAILING, fault="locks_small_pool", fault_start=10, fault_seconds=60,
      locked_fraction=0.35, pool_size=1),
    t("lp-h5", "lock_pool_exhaustion", H, 240, 3.0, FAILING, fault="locks_small_pool", fault_start=30, fault_seconds=190,
      locked_fraction=0.15, pool_size=3, distractor="overspend_and_replays", distractor_start=100, distractor_seconds=80),
    # healthy (no fault; benign distractors only)
    t("he-d1", "healthy", D, 150, 3.2, LATE),
    t("he-d2", "healthy", D, 180, 2.0, FAILING, distractor="overspend", distractor_start=20, distractor_seconds=140),
    t("he-d3", "healthy", D, 150, 3.6, LATE, distractor="replays", distractor_start=10, distractor_seconds=120),
    t("he-d4", "healthy", D, 200, 2.8, FAILING),
    t("he-d5", "healthy", D, 150, 3.0, LATE, distractor="overspend", distractor_start=30, distractor_seconds=90),
    t("he-h1", "healthy", H, 240, 2.4, LATE, distractor="load_bump", distractor_start=60, distractor_seconds=60),
    t("he-h2", "healthy", H, 180, 3.0, FAILING, distractor="overspend_and_replays", distractor_start=20, distractor_seconds=140),
    t("he-h3", "healthy", H, 210, 3.4, LATE, distractor="load_bump", distractor_start=100, distractor_seconds=45),
    t("he-h4", "healthy", H, 150, 2.2, FAILING, distractor="overspend_and_replays", distractor_start=10, distractor_seconds=120),
    t("he-h5", "healthy", H, 240, 3.0, LATE),
    # missing evidence: a real fault, with the source that decides its cause removed
    t("me-d1", "missing_evidence", D, 150, 3.2, LATE, fault="unavailable", fault_start=30, fault_seconds=90,
      stop=("collector",), deciding_source="logs"),
    t("me-d2", "missing_evidence", D, 180, 3.0, LATE, fault="slow", fault_start=40, fault_seconds=100, delay_ms=500,
      stop=("collector",), deciding_source="logs"),
    t("me-d3", "missing_evidence", D, 150, 2.6, LATE, fault="reject", fault_start=20, fault_seconds=100,
      stop=("collector",), deciding_source="logs"),
    t("me-d4", "missing_evidence", D, 200, 3.4, FAILING, fault="locks", fault_start=60, fault_seconds=90, locked_fraction=0.3,
      stop=("collector",), deciding_source="blockers"),
    t("me-d5", "missing_evidence", D, 150, 3.2, LATE, fault="unavailable", fault_start=30, fault_seconds=80,
      stop=("collector", "prometheus"), deciding_source="logs"),
    t("me-h1", "missing_evidence", H, 240, 2.2, LATE, fault="slow", fault_start=60, fault_seconds=160, delay_ms=1200,
      stop=("collector",), deciding_source="logs", distractor="load_bump", distractor_start=20, distractor_seconds=40),
    t("me-h2", "missing_evidence", H, 180, 3.6, LATE, fault="reject", fault_start=15, fault_seconds=150,
      stop=("collector", "prometheus"), deciding_source="logs"),
    t("me-h3", "missing_evidence", H, 210, 3.0, FAILING, fault="locks", fault_start=110, fault_seconds=80, locked_fraction=0.5,
      stop=("collector",), deciding_source="blockers", distractor="overspend_and_replays",
      distractor_start=30, distractor_seconds=100),
    t("me-h4", "missing_evidence", H, 150, 2.8, LATE, fault="slow", fault_start=10, fault_seconds=120, delay_ms=900,
      stop=("collector", "prometheus"), deciding_source="logs"),
    t("me-h5", "missing_evidence", H, 240, 3.0, LATE, fault="unavailable", fault_start=30, fault_seconds=185,
      stop=("collector",), deciding_source="logs", distractor="load_bump", distractor_start=100, distractor_seconds=50),
]

BY_ID = {template.template_id: template for template in TEMPLATES}


def development() -> list[Template]:
    return [template for template in TEMPLATES if template.split == "development"]


def held_out() -> list[Template]:
    return [template for template in TEMPLATES if template.split == "held_out"]


def check_templates() -> None:
    """Structural rules: 5 + 5 per category, faults end inside the window, delays below the
    delivery timeout, rates under 50 % of capacity, held-out parameters unseen in development."""
    categories = {template.category for template in TEMPLATES}
    assert len(categories) == 7
    for category in categories:
        for split in ("development", "held_out"):
            assert sum(1 for x in TEMPLATES if x.category == category and x.split == split) == 5, (category, split)
    for x in TEMPLATES:
        assert x.fault_start + x.fault_seconds <= x.window_seconds, x.template_id
        assert x.distractor_start + x.distractor_seconds <= x.window_seconds, x.template_id
        assert x.delay_ms < 3_000 and x.rate <= 3.8, x.template_id
        assert (x.category == "missing_evidence") == bool(x.stop), x.template_id
        # A lock fault's symptom is transfer errors, read from Prometheus: it must keep running.
        assert not (x.fault == "locks" and "prometheus" in x.stop), x.template_id
    dev_distractors = {x.distractor for x in development()}
    assert "load_bump" not in dev_distractors and "overspend_and_replays" not in dev_distractors
    dev_delays = {x.delay_ms for x in development() if x.delay_ms}
    assert not dev_delays & {x.delay_ms for x in held_out() if x.category == "slow_consumer"}
