from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.matcher import MatchResult, best_match

cv2.setNumThreads(1)

ENGINE_SETTINGS = dict(
    use_cls=False,
    det_limit_type="max",
    det_limit_side_len=416,
    intra_op_num_threads=6,
)

_ENGINE = None


def get_engine(**overrides):
    global _ENGINE
    if _ENGINE is None or overrides:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR(**{**ENGINE_SETTINGS, **overrides})
        if overrides:
            return engine
        _ENGINE = engine
    return _ENGINE


BRIGHT_LIFT = 40


@dataclass
class GateSettings:
    min_sharpness: float = 12.0
    max_consecutive_drops: int = 8
    enabled: bool = True


def gate_metrics(frame: np.ndarray) -> tuple[float, float]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    small = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
    sharp = float(cv2.Laplacian(small, cv2.CV_64F).var())
    bright = float((small >= float(np.median(small)) + BRIGHT_LIFT).mean())
    return sharp, bright


def gate(frame: np.ndarray, s: GateSettings) -> tuple[bool, str, tuple[float, float]]:
    sharp, bright = gate_metrics(frame)
    if not s.enabled:
        return True, "gate off", (sharp, bright)
    if sharp < s.min_sharpness:
        return False, f"blurred (sharpness {sharp:.1f} < {s.min_sharpness})", (sharp, bright)
    return True, "ok", (sharp, bright)


def _raw(frame: np.ndarray) -> np.ndarray:
    return frame


def _clahe(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    out = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)


def _unsharp(frame: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(frame, (3, 3), 0)
    return cv2.addWeighted(frame, 1.6, blur, -0.6, 0)


VARIANTS = {"raw": _raw, "clahe": _clahe, "unsharp": _unsharp}

ROTATION = ("raw", "clahe", "unsharp")


class TemporalVoter:

    def __init__(self, window: int = 3, needed: int = 2):
        self.window, self.needed = window, needed
        self.recent: deque[str | None] = deque(maxlen=window)
        self.last_accepted: str | None = None

    def push(self, matric: str | None) -> str | None:
        self.recent.append(matric)
        votes = Counter(m for m in self.recent if m)
        if not votes:
            return None
        winner, n = votes.most_common(1)[0]
        return winner if n >= self.needed else None

    def reset(self) -> None:
        self.recent.clear()

    @property
    def tally(self) -> dict[str, int]:
        return dict(Counter(m for m in self.recent if m))


@dataclass
class FrameResult:
    passed_gate: bool = True
    gate_reason: str = ""
    sharpness: float = 0.0
    brightness: float = 0.0
    variant: str = ""
    texts: list[str] = field(default_factory=list)
    match: MatchResult | None = None
    stable: str | None = None
    unmatched: str | None = None
    ocr_ms: float = 0.0
    total_ms: float = 0.0

    @property
    def accepted(self) -> bool:
        return bool(self.match and self.match.accepted)

    def as_dict(self) -> dict:
        m = self.match
        return {
            "passed_gate": self.passed_gate, "gate_reason": self.gate_reason,
            "sharpness": round(self.sharpness, 1), "brightness": round(self.brightness, 4),
            "variant": self.variant, "texts": self.texts[:12],
            "ocr_ms": round(self.ocr_ms), "total_ms": round(self.total_ms),
            "stable": self.stable, "unmatched": self.unmatched,
            "match": None if not m else {
                "token": m.token, "best": m.best,
                "distance": round(m.best_distance, 2), "margin": round(m.margin, 2),
                "accepted": m.accepted, "reason": m.reason,
                "ranked": [{"matric": r, "distance": round(d, 2)} for r, d in m.ranked],
            },
        }


class Pipeline:

    def __init__(self, gate_settings: GateSettings | None = None,
                 rotate_variants: bool = False, window: int = 3, needed: int = 2,
                 engine=None):
        self.gate_settings = gate_settings or GateSettings()
        self.rotate = rotate_variants
        self.voter = TemporalVoter(window, needed)
        self.unmatched_voter = TemporalVoter(window, needed)
        self.frame_index = 0
        self._drops = 0
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    def reset(self) -> None:
        self.voter.reset()
        self.unmatched_voter.reset()
        self._drops = 0

    def process(self, frame: np.ndarray, roster: list[str]) -> FrameResult:
        t0 = time.perf_counter()
        res = FrameResult()
        self.frame_index += 1

        ok, reason, (sharp, bright) = gate(frame, self.gate_settings)

        if not ok:
            self._drops += 1
            if self._drops >= self.gate_settings.max_consecutive_drops:
                ok, reason, self._drops = True, f"{reason}; forced through", 0
        else:
            self._drops = 0

        res.passed_gate, res.gate_reason = ok, reason
        res.sharpness, res.brightness = sharp, bright
        if not ok:
            res.total_ms = (time.perf_counter() - t0) * 1000
            return res

        name = ROTATION[self.frame_index % len(ROTATION)] if self.rotate else "raw"
        res.variant = name
        prepared = VARIANTS[name](frame)

        t1 = time.perf_counter()
        raw = self.engine(prepared)
        ocr = raw[0] if isinstance(raw, tuple) else raw
        res.ocr_ms = (time.perf_counter() - t1) * 1000
        res.texts = [text for _box, text, _score in (ocr or [])]

        res.match = best_match(res.texts, roster)
        res.stable = self.voter.push(res.match.best if res.accepted else None)
        res.unmatched = self.unmatched_voter.push(
            res.match.token if res.match and not res.match.accepted else None)
        res.total_ms = (time.perf_counter() - t0) * 1000
        return res


def _open_camera(index: int):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    return cap


def main() -> int:
    import argparse
    import statistics

    from app.config import load_config
    from app.roster import load_roster

    ap = argparse.ArgumentParser(description="Run the live pipeline over the camera")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--session", type=int, default=0, help="0 = whatever session.yaml says")
    ap.add_argument("--truth", default="", help="the correct matric, to measure wrong-accepts")
    ap.add_argument("--step", type=int, default=10, help="sample every Nth frame")
    ap.add_argument("--limit", type=int, default=200, help="stop after this many sampled frames")
    ap.add_argument("--rotate", action="store_true", help="rotate preprocessing variants")
    ap.add_argument("--gate", choices=["on", "off", "report"], default="report")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    roster_all = load_roster(cfg.roster)
    session = args.session or cfg.session.number
    roster = list(roster_all.for_session(session).matrics)
    if args.truth and args.truth not in roster:
        roster.append(args.truth)
        print(f"note: added --truth {args.truth} to the session {session} roster")

    gs = GateSettings(enabled=(args.gate == "on"))
    pipe = Pipeline(gate_settings=gs, rotate_variants=args.rotate)
    print(f"camera {args.camera} | session {session} | roster {len(roster)} | "
          f"variants {'rotate' if args.rotate else 'raw only'} | gate {args.gate}")
    print(f"engine {ENGINE_SETTINGS}")

    cap = _open_camera(args.camera)
    if not cap.isOpened():
        print(f"cannot open camera {args.camera} -- is /operator still holding it?")
        return 1
    fps_in = cap.get(cv2.CAP_PROP_FPS) or 25.0

    t_warm = time.perf_counter()
    pipe.engine(np.zeros((720, 1280, 3), dtype=np.uint8))
    print(f"engine warm in {(time.perf_counter() - t_warm):.1f}s\n")

    times, idx, sampled = [], 0, 0
    correct = wrong = refused = nothing = gated = 0
    stable_events: list[tuple[float, str]] = []
    last_stable = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % args.step:
            idx += 1
            continue
        idx += 1
        sampled += 1
        if args.limit and sampled > args.limit:
            break

        r = pipe.process(frame, roster)
        t = (idx - 1) / fps_in
        times.append(r.total_ms)

        would_gate = not gate(frame, GateSettings(enabled=True))[0]
        if not r.passed_gate:
            gated += 1
            continue

        if r.accepted:
            if args.truth and r.match.best != args.truth:
                wrong += 1
                print(f"  t={t:6.2f}s  *** WRONG ACCEPT *** {r.match.token} -> {r.match.best}")
            else:
                correct += 1
                if not args.quiet:
                    flag = "  [gate would have dropped this]" if would_gate else ""
                    print(f"  t={t:6.2f}s  {r.match.best:<12} d={r.match.best_distance:4.2f} "
                          f"margin={r.match.margin:5.2f} conf-ok  {r.total_ms:5.0f} ms "
                          f"[{r.variant}]{flag}")
        elif r.match:
            refused += 1
            if not args.quiet:
                print(f"  t={t:6.2f}s  refused: {r.match.reason}  [{r.variant}]")
        else:
            nothing += 1

        if r.stable and r.stable != last_stable:
            last_stable = r.stable
            stable_events.append((t, r.stable))
            print(f"  t={t:6.2f}s  >>> IDENTIFIED {r.stable} "
                  f"(2 of last 3 frames) -- this is where check-in fires")
        if not r.stable:
            last_stable = None

    cap.release()

    print("\n" + "=" * 70)
    print(f"frames sampled   : {sampled} (every {args.step}th of {idx})")
    print(f"gated out        : {gated}")
    print(f"auto-accepted    : {correct}"
          + (f"   ({100*correct/max(sampled-gated,1):.0f}% of OCR'd frames)" if sampled else ""))
    print(f"WRONG ACCEPTS    : {wrong}   <-- must be 0")
    print(f"refused (ambig.) : {refused}")
    print(f"no ID-like text  : {nothing}")
    if times:
        print(f"latency          : mean {statistics.mean(times):.0f} ms | "
              f"median {statistics.median(times):.0f} ms | max {max(times):.0f} ms "
              f"-> {1000/statistics.mean(times):.1f} fps")
    print(f"identifications  : {len(stable_events)}  "
          f"{[(round(t, 1), m) for t, m in stable_events[:8]]}")
    return 1 if wrong else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
