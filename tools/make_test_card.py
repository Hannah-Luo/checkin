from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FONT = cv2.FONT_HERSHEY_SIMPLEX


def render_card(matric: str, name: str = "TEST STUDENT",
                width: int = 1280, height: int = 720,
                dark_surround: bool = True) -> np.ndarray:
    frame = np.full((height, width, 3), 28 if dark_surround else 255, dtype=np.uint8)

    x0, y0 = int(width * 0.16), int(height * 0.17)
    x1, y1 = int(width * 0.84), int(height * 0.83)
    cv2.rectangle(frame, (x0, y0), (x1, y1), (250, 250, 250), -1)
    cv2.rectangle(frame, (x0, y0), (x1, y1), (180, 180, 180), 2)

    cv2.putText(frame, "NANYANG TECHNOLOGICAL UNIVERSITY", (x0 + 30, y0 + 60),
                FONT, 0.7, (60, 60, 60), 2, cv2.LINE_AA)
    cv2.putText(frame, "STUDENT", (x0 + 30, y0 + 110),
                FONT, 0.9, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.putText(frame, name.upper(), (x0 + 30, y1 - 150),
                FONT, 1.0, (20, 20, 20), 2, cv2.LINE_AA)
    cv2.putText(frame, matric.upper(), (x0 + 30, y1 - 60),
                FONT, 2.0, (10, 10, 10), 5, cv2.LINE_AA)
    return frame


def encode_jpeg(frame: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("could not encode the frame")
    return buf.tobytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("matric", nargs="?", default="")
    ap.add_argument("--name", default="TEST STUDENT")
    ap.add_argument("--roster", action="store_true",
                    help="one card per student in the configured session")
    ap.add_argument("--full-names", action="store_true",
                    help="print real names on roster cards (default: initials)")
    ap.add_argument("--out", default="sample/cards")
    args = ap.parse_args()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    if args.roster:
        import os

        from app.config import load_config
        from app.roster import load_roster, mask_name
        cfg = load_config(os.environ.get("CHECKIN_CONFIG"))
        sr = load_roster(cfg.roster).for_session(cfg.session.number)
        people = [(p.matric,
                   (p.name if args.full_names else mask_name(p.name)) or "TEST STUDENT")
                  for p in sr.participants]
        print(f"session {cfg.session.number}: {len(people)} cards"
              f"{' with real names' if args.full_names else ' (names as initials)'}")
    elif args.matric:
        people = [(args.matric, args.name)]
    else:
        ap.error("give a matric number or --roster")

    for matric, name in people:
        path = out / f"card_{matric}.jpg"
        cv2.imwrite(str(path), render_card(matric, name))
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
