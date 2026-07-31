"""Tests for monthly.py's archive_and_prune() -- guards against the
archive/prune race where a row inserted by a concurrently-running collector
between the archive snapshot and the delete could be deleted from the live
table without ever having been captured in the archive.
"""

from __future__ import annotations

import sqlite3

import monthly


def _point_at(monkeypatch, home):
    """Redirect monthly's DB/ARCHIVE_DIR at the tmp_tokometer ledger."""
    monkeypatch.setattr(monthly, "DB", str(home / "ledger.db"))
    monkeypatch.setattr(monthly, "ARCHIVE_DIR", str(home / "archive"))


def _connect(home):
    return sqlite3.connect(str(home / "ledger.db"))


def _insert_usage(con, uid, ts):
    con.execute(
        "INSERT INTO usage (uid, ts, harness, source) VALUES (?, ?, 'claude-code', 'test')",
        (uid, ts),
    )


def _insert_commit(con, sha, ts):
    con.execute(
        "INSERT INTO commit_metric (repo, repo_path, sha, ts) VALUES ('r', '/r', ?, ?)",
        (sha, ts),
    )


def _dump_text(home):
    """Concatenate every archive .sql dump written under ARCHIVE_DIR."""
    archive_dir = home / "archive"
    if not archive_dir.exists():
        return ""
    text = ""
    for p in archive_dir.glob("pruned-before-*.sql"):
        text += p.read_text()
    return text


def test_archive_and_prune_never_loses_a_boundary_row(tmp_tokometer, monkeypatch):
    """A row with ts < cutoff must end up in exactly one place -- live or
    archive -- and never in neither. This is the direct repro for the
    reported bug: a row that satisfied the DELETE predicate but was never
    captured by the archive snapshot used to vanish silently.
    """
    _point_at(monkeypatch, tmp_tokometer)
    con = _connect(tmp_tokometer)
    _insert_usage(con, "old-1", "2026-01-15T12:00:00Z")
    con.commit()
    con.close()

    monthly.archive_and_prune("2026-02-01")

    con = _connect(tmp_tokometer)
    live = con.execute("SELECT COUNT(*) FROM usage WHERE uid = 'old-1'").fetchone()[0]
    con.close()
    archived = 1 if "old-1" in _dump_text(tmp_tokometer) else 0

    assert live + archived == 1, (
        f"row 'old-1' must exist in exactly one place (live={live}, archived={archived})"
    )


def test_archive_and_prune_invariant_all_cutoff_rows_archived_none_lost(tmp_tokometer, monkeypatch):
    """Regression test for the archive+prune invariant on the normal
    (non-racy) path: every row matching the cutoff predicate, across every
    pruned table, ends up copied into the archive dump, the live table is
    left holding only rows on/after the cutoff, and nothing is lost. This is
    what would regress if the single-transaction archive+delete in
    archive_and_prune() were carelessly split back into two connections.
    """
    _point_at(monkeypatch, tmp_tokometer)
    con = _connect(tmp_tokometer)

    old_usage_uids = [f"old-u{i}" for i in range(5)]
    new_usage_uids = [f"new-u{i}" for i in range(3)]
    for uid in old_usage_uids:
        _insert_usage(con, uid, "2025-11-20T09:00:00Z")
    for uid in new_usage_uids:
        _insert_usage(con, uid, "2026-03-10T09:00:00Z")

    old_shas = [f"old-sha-{i}" for i in range(2)]
    new_shas = [f"new-sha-{i}" for i in range(2)]
    for sha in old_shas:
        _insert_commit(con, sha, "2025-11-20T09:00:00Z")
    for sha in new_shas:
        _insert_commit(con, sha, "2026-03-10T09:00:00Z")

    con.commit()
    con.close()

    cutoff = "2026-02-01"
    monthly.archive_and_prune(cutoff)

    con = _connect(tmp_tokometer)
    live_usage_uids = {row[0] for row in con.execute("SELECT uid FROM usage")}
    live_shas = {row[0] for row in con.execute("SELECT sha FROM commit_metric")}
    con.close()

    dump = _dump_text(tmp_tokometer)

    # New rows (on/after cutoff) must stay live and must NOT be archived.
    assert set(new_usage_uids) == live_usage_uids
    assert set(new_shas) == live_shas
    for uid in new_usage_uids:
        assert uid not in dump

    # Old rows (before cutoff) must be gone from live and present in the
    # archive dump -- archived-count + still-live-count == inserted-count,
    # with still-live-count == 0.
    for uid in old_usage_uids:
        assert uid not in live_usage_uids, f"{uid} should have been pruned from live usage"
        assert uid in dump, f"{uid} should have been archived before pruning"
    for sha in old_shas:
        assert sha not in live_shas, f"{sha} should have been pruned from live commit_metric"
        assert sha in dump, f"{sha} should have been archived before pruning"


def test_archive_and_prune_holds_writer_lock_across_snapshot_and_delete(tmp_tokometer, monkeypatch):
    """Structural proof that the fix closes the race window.

    We can't deterministically simulate the original timing-dependent race
    (a second connection inserting a cutoff-matching row between the
    snapshot read and the delete) with real threads/sleeps in a unit test --
    that's inherently timing-order dependent and flaky. Instead, this test
    proves the mechanism that makes the race impossible: archive_and_prune()
    takes a BEGIN IMMEDIATE writer lock before its snapshot query and holds
    it through the delete, so any *other* connection attempting to write
    while that transaction is open must be blocked by SQLite's locking --
    there is no instant at which a second writer could sneak a
    cutoff-matching insert in between the snapshot and the delete.

    This is done by subclassing sqlite3.Connection (the C type can't be
    monkeypatched directly) and intercepting the exact CREATE TABLE arch...
    snapshot statement archive_and_prune() issues, from inside its own
    still-open transaction, to attempt a concurrent insert from a second,
    independent connection and confirm SQLite rejects it as locked.
    """
    _point_at(monkeypatch, tmp_tokometer)
    con = _connect(tmp_tokometer)
    _insert_usage(con, "old-1", "2026-01-15T12:00:00Z")
    con.commit()
    con.close()

    db_path = str(tmp_tokometer / "ledger.db")
    real_connect = sqlite3.connect
    probe = {"triggered": False, "blocked": None}

    class SpyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.startswith("CREATE TABLE arch.") and not probe["triggered"]:
                probe["triggered"] = True
                racer = real_connect(db_path, timeout=0.2)
                try:
                    racer.execute(
                        "INSERT INTO usage (uid, ts, harness, source)"
                        " VALUES ('racer', '2020-01-01T00:00:00Z', 'test', 'test')"
                    )
                    racer.commit()
                    probe["blocked"] = False
                except sqlite3.OperationalError:
                    probe["blocked"] = True
                finally:
                    racer.close()
            return result

    first_call = {"seen": False}

    def fake_connect(database, *args, **kwargs):
        if not first_call["seen"] and database == db_path:
            first_call["seen"] = True
            return real_connect(database, *args, factory=SpyConnection, **kwargs)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(monthly.sqlite3, "connect", fake_connect)

    monthly.archive_and_prune("2026-02-01")

    assert probe["triggered"], "expected the CREATE TABLE arch. snapshot statement to fire"
    assert probe["blocked"] is True, (
        "a second connection was able to write while archive_and_prune() held its"
        " snapshot-to-delete transaction open -- the race window is not closed"
    )

    # And the archived row is still correctly accounted for despite the
    # contention against it.
    con = _connect(tmp_tokometer)
    live = con.execute("SELECT COUNT(*) FROM usage WHERE uid = 'old-1'").fetchone()[0]
    con.close()
    assert live == 0
    assert "old-1" in _dump_text(tmp_tokometer)
