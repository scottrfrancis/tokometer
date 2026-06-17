"""Repo labels must be the leaf directory name, not a full path.

Regression: on Windows, repo paths come through with mixed separators
(e.g. ``C:\\Users\\me/workspace\\my-repo``). The label helpers split on "/"
only, so the "workspace" segment never matched and the whole tail
(``workspace\\my-repo``) leaked into the report as the "repo name".

Both helpers must normalize backslashes so they yield just ``my-repo``.
``repo_of`` additionally receives deep working dirs (not repo roots), so it
must still anchor on the ``workspace`` segment rather than naively take a
basename.
"""

from __future__ import annotations

from collectors.git_metrics import repo_label
from report_html import repo_of


# --- repo_label: given a repo ROOT path, return the leaf dir name ------------

def test_repo_label_windows_mixed_separators():
    assert repo_label(r"C:\Users\scott/workspace\Catalyst-RCM") == "Catalyst-RCM"


def test_repo_label_windows_all_backslashes():
    assert repo_label(r"C:\Users\scott\workspace\brightsign-engagement") == "brightsign-engagement"


def test_repo_label_posix_under_workspace():
    assert repo_label("/home/scott/workspace/dot-opencode") == "dot-opencode"


def test_repo_label_no_workspace_falls_back_to_leaf():
    assert repo_label(r"C:\Users\scott\repos\tokometer") == "tokometer"


# --- repo_of: given a (possibly deep) working dir, map it back to the repo ---

def test_repo_of_deep_windows_cwd_anchors_on_workspace():
    assert repo_of(r"C:\Users\scott/workspace\Catalyst-RCM\src\app") == "Catalyst-RCM"


def test_repo_of_deep_posix_cwd_anchors_on_workspace():
    assert repo_of("/home/scott/workspace/dot-opencode/sub/dir") == "dot-opencode"


def test_repo_of_none_returns_none():
    assert repo_of(None) is None
