#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The two shell scripts' dangerous steps, run for real: publish.sh replaces everything in its
destination and pushes to the public repository, and install_cdk.sh sources the deploy.env it writes.

publish.sh runs against a clean clone of this repository, and a local bare repository stands in
for the public one (PUBLISH_REMOTE)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(cwd, *args) -> str:
    return subprocess.run([*GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _publish(clone, remote, *args):
    return subprocess.run(["bash", str(ROOT / "publish.sh"), *map(str, args)], cwd=clone,
                          env={**os.environ, "PUBLISH_REMOTE": str(remote)},
                          capture_output=True, text=True, timeout=300, check=False)


@pytest.fixture
def clone(tmp_path):
    # A clean clone: publish.sh releases only a committed HEAD.
    path = tmp_path / "repo"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(path)], check=True)
    return path


@pytest.fixture
def remote(tmp_path):
    """The public repository as it starts out: one template commit, a file of its own and a
    README that ours replaces."""
    bare, seed = tmp_path / "public.git", tmp_path / "seed"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("template\n")
    (seed / "TEMPLATE_ONLY.md").write_text("from the template\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "Initial commit")
    _git(seed, "push", "-q", str(bare), "main")
    return bare


def _exported(clone) -> list[str]:
    """What publish.sh should ship: the .publish-paths at HEAD, as git archive expands them."""
    paths = [ln.split("#")[0].strip() for ln in _git(clone, "show", "HEAD:.publish-paths").splitlines()]
    return sorted(_git(clone, "ls-tree", "-r", "--name-only", "HEAD", "--", *filter(None, paths)).splitlines())


def test_publish_replaces_nothing_but_the_public_worktree(tmp_path, clone, remote):
    """Pointed at any other directory, it must not delete what is there."""
    target = tmp_path / "not-the-public-worktree"
    target.mkdir()
    (target / "precious.txt").write_text("keep me")
    result = _publish(clone, remote, target)
    assert result.returncode != 0
    assert "is not this repository's worktree of the 'public' branch" in result.stderr
    assert (target / "precious.txt").read_text() == "keep me"


def test_a_dry_run_shows_the_files_and_changes_and_changes_nothing(clone, remote):
    tip = _git(remote, "rev-parse", "main")
    result = _publish(clone, remote, "--dry-run")
    assert result.returncode == 0, result.stderr
    # Only gitleaks' report: the dry run ends at its summary, before any step that needs a worktree.
    assert all("INF" in ln for ln in result.stderr.splitlines() if ln.strip()), result.stderr
    out = result.stdout.splitlines()
    assert all(f"  {f}" in out for f in _exported(clone))
    assert "  changed  README.md" in out and "  removed  TEMPLATE_ONLY.md" in out
    assert "  added    publish.sh" not in out  # not in .publish-paths, so never shipped
    assert _git(remote, "rev-parse", "main") == tip
    assert _git(clone, "branch", "--list", "public") == ""
    assert len(_git(clone, "worktree", "list").splitlines()) == 1


def test_an_unreadable_public_repository_stops_the_run(tmp_path, clone):
    """Not "it has no commits yet": that would make the release a root commit."""
    result = _publish(clone, tmp_path / "no-such-repo.git", "--dry-run")
    assert result.returncode != 0
    assert "could not read" in result.stderr


def test_a_release_is_one_commit_on_the_public_tip_with_exactly_the_exported_tree(tmp_path, clone, remote):
    tip, dest = _git(remote, "rev-parse", "main"), tmp_path / "public"
    result = _publish(clone, remote, dest)
    assert result.returncode == 0, result.stderr
    assert _git(dest, "rev-parse", "HEAD^") == tip
    assert _git(dest, "rev-list", "--count", f"{tip}..HEAD") == "1"
    assert sorted(_git(dest, "ls-files").splitlines()) == _exported(clone)
    assert _git(remote, "rev-parse", "main") == tip  # not pushed without --push


def test_push_moves_the_public_repository_forward_by_one_commit_and_no_internal_history(tmp_path, clone, remote):
    tip, dest = _git(remote, "rev-parse", "main"), tmp_path / "public"
    # A stale local 'public' branch with a commit of its own: the squash must leave it behind.
    _git(clone, "worktree", "add", "--quiet", "--orphan", "-b", "public", str(dest))
    (dest / "old.txt").write_text("an earlier, unpublished release\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "old release")
    stale = _git(dest, "rev-parse", "HEAD")

    result = _publish(clone, remote, "--push", dest)
    assert result.returncode == 0, result.stderr
    released = _git(remote, "rev-parse", "main")
    assert released == _git(dest, "rev-parse", "HEAD")
    assert _git(remote, "rev-list", "--count", f"{tip}..main") == "1"
    assert _git(remote, "rev-parse", "main^") == tip
    for internal in (_git(clone, "rev-parse", "HEAD"), stale):
        assert subprocess.run(["git", "-C", str(remote), "cat-file", "-e", internal], check=False).returncode != 0

    again = _publish(clone, remote, "--push", dest)
    assert again.returncode == 0 and "Nothing to publish" in again.stdout
    assert _git(remote, "rev-parse", "main") == released


def test_a_typed_deploy_setting_is_kept_as_data(tmp_path):
    """install_cdk.sh appends what it asked for to deploy.env and sources the file next run.
    A value with a space, a semicolon or a $(...) must come back as typed and run nothing."""
    line = next(ln.strip() for ln in (ROOT / "install_cdk.sh").read_text().splitlines()
                if ln.strip().startswith("printf '%s=%q") and "DEPLOY_ENV" in ln)
    env_file, marker = tmp_path / "deploy.env", tmp_path / "ran"
    typed = f"subnet-a, subnet-b; touch {marker} $(touch {marker})"
    script = f'DEPLOY_ENV={env_file}; v=PRIVATE_SUBNET_IDS; value="$1"; {line}\n' \
             f'set -a; source {env_file}; set +a; printf %s "$PRIVATE_SUBNET_IDS"'
    result = subprocess.run(["bash", "-c", script, "bash", typed], capture_output=True, text=True, check=True)
    assert result.stdout == typed
    assert not marker.exists()
