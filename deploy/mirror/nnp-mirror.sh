#!/usr/bin/env bash
# Push the static export to the GitHub Pages mirror as ONE parentless commit
# on gh-pages, so content taken down on the live site leaves the branch (and
# fresh clones) on the next push. Run as user nnp by nnp-mirror.service:
#
#   nnp-mirror.sh EXPORT_DIR [--allow-shrink]
#
# Config (systemd EnvironmentFile /etc/nnp/mirror.env, see mirror.env.example):
#   NNP_MIRROR_REMOTE    required; the DEDICATED mirror repo
#   GIT_SSH_COMMAND      pins the deploy key and known_hosts
#   NNP_MIRROR_PING_URL  optional healthchecks.io URL (/start, "" or /fail)
#   NNP_MIRROR_GITDIR    local bare repo, default /var/lib/nnp/mirror.git
#
# Change gate: the git tree id of the export IS its content hash; it is
# compared with the remote gh-pages tree, so there is no local state to lose.
# A remote that cannot be listed aborts (never mistaken for a first push), and
# an export that shrank to under half the remote's file count is refused
# unless --allow-shrink is given (an empty export never overwrites the mirror).
#
# Tested only against a local bare repo (tests/test_mirror.py). The GitHub
# side (deploy key, ssh, Pages) is NOT verified where this was written; drill
# D4 in deploy/RUNBOOK.md is the check.
set -euo pipefail
umask 022

src=${1:?usage: nnp-mirror.sh EXPORT_DIR [--allow-shrink]}
: "${NNP_MIRROR_REMOTE:?set NNP_MIRROR_REMOTE (the mirror repo URL)}"

ping() { [ -n "${NNP_MIRROR_PING_URL:-}" ] || return 0; curl -fsS -m 10 --retry 3 -o /dev/null "${NNP_MIRROR_PING_URL}$1" || true; }
trap 'rc=$?; [ "$rc" -eq 0 ] || ping /fail' EXIT
ping /start

[ -d "$src" ] || { echo "export dir $src does not exist" >&2; exit 1; }
# A compromised app user could plant a symlink to the deploy key in the export.
# (No pipe into grep -q: under pipefail a SIGPIPE'd find would skip the refusal.)
bad=$(find "$src" ! -type f ! -type d)
if [ -n "$bad" ]; then
    echo "refusing: non-regular file in export:" >&2
    printf '%s\n' "$bad" >&2
    exit 1
fi

export GIT_DIR=${NNP_MIRROR_GITDIR:-/var/lib/nnp/mirror.git}
[ -d "$GIT_DIR" ] || git init -q --bare "$GIT_DIR"
export GIT_WORK_TREE=$src
export GIT_INDEX_FILE=$GIT_DIR/index.tmp
rm -f "$GIT_INDEX_FILE"
git add -A
new=$(git write-tree)          # the tree id IS the content hash

if git ls-remote --exit-code --heads "$NNP_MIRROR_REMOTE" gh-pages >/dev/null; then
    git fetch -q --depth=1 "$NNP_MIRROR_REMOTE" gh-pages
    old=$(git rev-parse 'FETCH_HEAD^{tree}')
    if [ "$new" = "$old" ]; then
        echo "mirror unchanged (tree $new)"
        ping ""
        exit 0
    fi
    n_new=$(git ls-tree -r "$new" | wc -l | tr -d " ")
    n_old=$(git ls-tree -r "$old" | wc -l | tr -d " ")
    if [ "${2:-}" != "--allow-shrink" ] && [ "$n_new" -lt $((n_old / 2)) ]; then
        echo "refusing: export shrank from $n_old to $n_new files (pass --allow-shrink)" >&2
        exit 1
    fi
else
    rc=$?
    # --exit-code returns 2 only when the remote answered and has no gh-pages.
    [ "$rc" -eq 2 ] || { echo "ls-remote failed (rc=$rc): network or auth, not a first push" >&2; exit 1; }
fi

commit=$(GIT_AUTHOR_NAME='NNP mirror' GIT_AUTHOR_EMAIL=mirror@nonnewtonian.org \
         GIT_COMMITTER_NAME='NNP mirror' GIT_COMMITTER_EMAIL=mirror@nonnewtonian.org \
         git commit-tree "$new" -m "NonNewtonian Physicists static mirror")   # no -p: parentless
git push -q --force "$NNP_MIRROR_REMOTE" "$commit:refs/heads/gh-pages"
# Keep no taken-down objects locally: nothing here is referenced by a ref.
rm -f "$GIT_INDEX_FILE"
git reflog expire --expire=now --all
git gc -q --prune=now
echo "pushed $commit (tree $new)"
ping ""
