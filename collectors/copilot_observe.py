#!/usr/bin/env python3
"""Manual observation entry — the one signal no log records.

Usage (5 seconds, from any terminal on the box):

    observe 4 "good plan, slow finish"     # quality rating 1-5 + optional note
    observe --stall                        # agent went silent (dead worker, no prompt)
    observe --continue-prompt              # benign iteration-cap "continue?" appeared
    observe 2 --model claude-haiku-4.5     # rating with a model hover/fingerprint note

Model identity and tokens come from the chat-log collector; this records the human
judgment (quality) and the continue-vs-stall distinction the harness must not
conflate. Local-only; stdlib-only; Python 3.11-compatible.
"""
import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402


def _connect_now():
    """Connect using the CURRENT env (ledger.DB_PATH is frozen at import time)."""
    home = os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer"))
    con = sqlite3.connect(os.path.join(home, "ledger.db"), timeout=30)
    con.execute("PRAGMA busy_timeout = 30000;")
    return con


def record(con, quality=None, kind=None, model=None, note=None):
    """Insert one manual observation. quality must be 1-5 when given."""
    if quality is not None and not 1 <= int(quality) <= 5:
        raise ValueError(f"quality must be 1-5, got {quality}")
    if kind is None:
        kind = "rating"
    if kind == "rating" and quality is None:
        raise ValueError("a rating needs a quality value 1-5")
    con.execute(
        "INSERT INTO manual_obs (ts, kind, quality, model, note) VALUES (?, ?, ?, ?, ?)",
        (ledger.iso_utc(), kind, quality, model, note))
    con.commit()


def main(argv=None):
    p = argparse.ArgumentParser(prog="observe", description=__doc__.splitlines()[0])
    p.add_argument("quality", nargs="?", type=int, help="quality rating 1-5")
    p.add_argument("note", nargs="?", help="optional free-text note")
    p.add_argument("--model", help="model seen on hover / by style fingerprint")
    p.add_argument("--stall", action="store_true",
                   help="record a silent stall (no prompt, agent dead)")
    p.add_argument("--continue-prompt", dest="continue_prompt", action="store_true",
                   help="record a benign iteration-cap 'continue?' prompt")
    args = p.parse_args(argv)

    kind = "stall" if args.stall else (
        "continue_prompt" if args.continue_prompt else "rating")
    con = _connect_now()
    try:
        record(con, quality=args.quality, kind=kind,
               model=args.model, note=args.note)
    except ValueError as e:
        print(f"observe: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()
    print(f"observe: recorded {kind}"
          + (f" quality={args.quality}" if args.quality else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
