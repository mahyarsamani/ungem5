"""How much memory a job will use, learned from the jobs that ran before.

Every run that ends leaves a record in a SQLite file next to the scheduler's
state: the job's features (`Job.features`), its group (`Job.memory_group`),
the host, how busy the host was when it launched, the user's guess, what
was reserved, the measured peak, and the memory samples over time. The
records are kept, so they can be studied or fed to a better model later.

A job's estimate is the first of these that applies:

1. history: runs with the same features finished; their largest peak,
   plus HISTORY_MARGIN.
2. model: enough runs finished to learn from (see `model.py`); what the
   model, refit every REFIT_EVERY new runs, reserves for jobs like it.
3. corrected guess: the user gave a guess, and at least MIN_GROUP_RUNS runs
   of the job's group with guesses ended; the guess times the largest
   peak/guess ratio among the last GROUP_WINDOW of them, plus GROUP_MARGIN.
   So a guess is trusted only as far as the group's guesses were right.
4. group: no guess, but runs of the group ended; their largest peak, plus
   GROUP_MARGIN.
5. guess: the user's guess, plus GUESS_MARGIN.
6. default: the scheduler's default.

Runs that failed or were killed may have stopped early, so their peaks never
count as a full run; they only set a floor for jobs with the same features.
"""

import hashlib
import json
import sqlite3
import time

from dataclasses import dataclass
from pathlib import Path

from .model import PeakModel, Run
from .resources import format_size
from .work import Job

HISTORY_MARGIN = 0.10
GROUP_MARGIN = 0.15
GUESS_MARGIN = 0.25
MIN_GROUP_RUNS = 3
GROUP_WINDOW = 50
REFIT_EVERY = 20
COMPLETE = "exited"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    experiment TEXT NOT NULL,
    grp TEXT NOT NULL,
    key TEXT NOT NULL,
    features TEXT NOT NULL,
    host TEXT,
    outcome TEXT NOT NULL,
    start_time REAL,
    end_time REAL,
    guess INTEGER,
    reserved INTEGER,
    peak INTEGER,
    co_jobs INTEGER,
    co_reserved INTEGER,
    trace TEXT,
    recorded_at REAL NOT NULL,
    UNIQUE (job_id, start_time)
);
CREATE INDEX IF NOT EXISTS runs_key ON runs (key);
CREATE INDEX IF NOT EXISTS runs_grp ON runs (grp);
"""


@dataclass(frozen=True)
class Estimate:
    memory: int
    source: str
    # NOTE: What the estimate rests on, for people to judge it.
    detail: str = ""


def feature_key(features: dict) -> str:
    text = json.dumps(features, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class MemoryHistory:
    """Records of finished runs, and estimates made from them.

    Used only from the scheduler thread; `path` None keeps the records in
    memory (tests).
    """

    def __init__(self, path: Path | None, default_memory: int) -> None:
        self._default = default_memory
        self._db = sqlite3.connect(
            str(path) if path is not None else ":memory:",
            # NOTE: Made on one thread, used on the scheduler's.
            check_same_thread=False,
        )
        self._db.executescript(SCHEMA)
        # NOTE: In-memory summaries of the table: per feature key, the
        # peaks of complete runs and of cut-short ones; per group, the
        # (peak, guess) of complete runs, oldest first.
        self._complete: dict[str, list[int]] = {}
        self._partial: dict[str, list[int]] = {}
        self._group: dict[str, list[tuple[int, int | None]]] = {}
        for key, grp, outcome, peak, guess in self._db.execute(
            "SELECT key, grp, outcome, peak, guess FROM runs "
            "WHERE peak IS NOT NULL ORDER BY id"
        ):
            self._remember(key, grp, outcome, peak, guess)
        # NOTE: Job id -> (key, group, features); features may be costly
        # to compute and never change for a loaded job.
        self._described: dict[str, tuple[str, str, dict]] = {}
        # NOTE: The model, complete runs since it was fit, and its
        # predictions per job id (until the next fit).
        self._model: PeakModel | None = None
        self._since_fit = 0
        self._predicted: dict[str, int] = {}
        self._refit()

    def _refit(self) -> None:
        runs = [
            Run(json.loads(features), grp, guess, peak)
            for features, grp, guess, peak in self._db.execute(
                "SELECT features, grp, guess, peak FROM runs WHERE outcome = ? "
                "AND peak IS NOT NULL ORDER BY id",
                (COMPLETE,),
            )
        ]
        self._model = PeakModel.fit(runs)
        self._since_fit = 0
        self._predicted.clear()

    def model(self) -> PeakModel | None:
        return self._model

    def close(self) -> None:
        self._db.close()

    def default_memory(self) -> int:
        return self._default

    def _remember(self, key, grp, outcome, peak, guess) -> None:
        if outcome == COMPLETE:
            self._complete.setdefault(key, []).append(peak)
            self._group.setdefault(grp, []).append((peak, guess))
        else:
            self._partial.setdefault(key, []).append(peak)

    def describe(self, job: Job) -> tuple[str, str, dict]:
        """(feature key, group, features) of `job`."""
        described = self._described.get(job.id())
        if described is None:
            try:
                features = job.features()
            except Exception as e:
                # NOTE: A broken override must not stop scheduling; the
                # job then only learns from its own reruns.
                features = {"id": job.id(), "features_error": str(e)}
            described = (feature_key(features), job.memory_group(), features)
            self._described[job.id()] = described
        return described

    def forget(self, job_id: str) -> None:
        self._described.pop(job_id, None)
        self._predicted.pop(job_id, None)

    def estimate(self, job: Job) -> Estimate:
        key, group, features = self.describe(job)
        guess = job.memory_guess()
        floor = max(self._partial.get(key, []), default=0)

        def done(memory: float, source: str, detail: str) -> Estimate:
            memory = int(memory)
            if floor > memory:
                return Estimate(
                    floor,
                    f"{source}, raised",
                    f"{detail}; a cut-short run used {format_size(floor)}",
                )
            return Estimate(memory, source, detail)

        complete = self._complete.get(key)
        if complete:
            return done(
                max(complete) * (1 + HISTORY_MARGIN),
                "history",
                f"{len(complete)} identical run(s), peak "
                f"{format_size(max(complete))}",
            )
        if self._model is not None:
            predicted = self._predicted.get(job.id())
            if predicted is None:
                predicted = self._model.predict(features, group, guess)
                self._predicted[job.id()] = predicted
            return done(predicted, "model", self._model.describe())
        runs = self._group.get(group, [])
        guessed = [(peak, g) for peak, g in runs if g][-GROUP_WINDOW:]
        if guess and len(guessed) >= MIN_GROUP_RUNS:
            ratio = max(peak / g for peak, g in guessed)
            return done(
                guess * ratio * (1 + GROUP_MARGIN),
                "corrected guess",
                f"guess {format_size(guess)} x {ratio:.2f}, the worst "
                f"peak/guess of {len(guessed)} run(s) in {group}",
            )
        if not guess and runs:
            peak = max(peak for peak, _ in runs)
            return done(
                peak * (1 + GROUP_MARGIN),
                "group",
                f"largest peak of {len(runs)} run(s) in {group}: "
                f"{format_size(peak)}",
            )
        if guess:
            return done(
                guess * (1 + GUESS_MARGIN),
                "guess",
                f"your guess {format_size(guess)}, unchecked",
            )
        return done(self._default, "default", "nothing known about this job")

    def record(
        self,
        job: Job,
        host: str | None,
        outcome: str,
        trace: list[dict],
        co_jobs: int | None,
        co_reserved: int | None,
    ) -> bool:
        """Keep a finished run. Returns False if it was already kept or
        has nothing to learn from (no peak)."""
        peak = job.memory_peak()
        if peak is None:
            return False
        key, group, features = self.describe(job)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO runs (job_id, experiment, grp, key, "
                    "features, host, outcome, start_time, end_time, guess, "
                    "reserved, peak, co_jobs, co_reserved, trace, "
                    "recorded_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id(),
                        job.experiment(),
                        group,
                        key,
                        json.dumps(features, sort_keys=True, default=str),
                        host,
                        outcome,
                        job.start_time(),
                        job.end_time(),
                        job.memory_guess(),
                        job.reserved(),
                        peak,
                        co_jobs,
                        co_reserved,
                        json.dumps(trace),
                        time.time(),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        self._remember(key, group, outcome, peak, job.memory_guess())
        if outcome == COMPLETE:
            self._since_fit += 1
            if self._since_fit >= REFIT_EVERY:
                self._refit()
        return True

    def runs(self, job_id: str | None = None) -> list[dict]:
        """Recorded runs, newest first (all, or those of one job)."""
        query = (
            "SELECT job_id, experiment, grp, host, outcome, start_time, "
            "end_time, guess, reserved, peak, co_jobs, co_reserved "
            "FROM runs"
        )
        args: tuple = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            args = (job_id,)
        cursor = self._db.execute(query + " ORDER BY id DESC", args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]
