#!/bin/bash
# Decides which version of Mello this speaker should run. Used by the nightly
# auto-update and by "Check for updates" in the settings menu, so both agree.
#
# Update channel, set per speaker in .env:
#   MELLO_UPDATE_CHANNEL=beta  follows main: every merged change, next night
#   anything else (stable)     follows the newest release tag (v*), made with
#                              the "Release" workflow on GitHub. Until the
#                              first release exists, stable follows main too.
#
# Prints one line: <status> <channel> <target-name> <target-commit>
#   update   a newer version is waiting
#   current  already running it
#   older    the target is older than what runs (e.g. moved from beta to
#            stable); never go backwards, wait until the channel catches up
#   failed   the target failed its health check on this speaker before
#            (see BAD_UPDATE_FILE); wait for a newer one
# A speaker still on go-librespot never goes past the version before the one
# that added pi/needs-soloist (see below).
# Exits 2 when GitHub cannot be reached.
set -euo pipefail

cd "$(dirname "$0")/.."

BAD_UPDATE_FILE="$HOME/.mello-bad-update"

CHANNEL=stable
if grep -qE "^MELLO_UPDATE_CHANNEL=[\"']?beta[\"']?[[:space:]]*$" .env 2>/dev/null; then
  CHANNEL=beta
fi

# git's own words (DNS, TLS, timeout...) go to stderr for the log.
if ! FETCH_ERR=$(git fetch --quiet --force --tags origin main 2>&1); then
  echo "git fetch: $FETCH_ERR" >&2
  exit 2
fi

TARGET_NAME=origin/main
if [ "$CHANNEL" = stable ]; then
  RELEASE_TAG=$(git for-each-ref --count=1 --sort=-v:refname \
    --format='%(refname:short)' 'refs/tags/v*')
  if [ -n "$RELEASE_TAG" ]; then
    TARGET_NAME="$RELEASE_TAG"
  fi
fi

LOCAL=$(git rev-parse HEAD)
TARGET=$(git rev-parse "$TARGET_NAME^{commit}")

# Versions with pi/needs-soloist no longer have go-librespot. A speaker that
# has not switched to Spotify Soloist stays on the version just before that
# file was added, and moves on by itself once it has switched. --first-parent:
# a PR merged with a merge commit must land on main's commit before the merge,
# not on the branch's own history.
if ! grep -qE "^MELLO_SPOTIFY_ENGINE=[\"']?soloist[\"']?[[:space:]]*$" .env 2>/dev/null \
   && git cat-file -e "$TARGET:pi/needs-soloist" 2>/dev/null; then
  ADDED=$(git log -1 --first-parent --diff-filter=A --format=%H "$TARGET" -- pi/needs-soloist)
  TARGET=$(git rev-parse "$ADDED^")
  TARGET_NAME=last-librespot-version
fi
# For the settings menu (mello/update_status.py): updates wait for Soloist.
if [ "$TARGET_NAME" = last-librespot-version ]; then
  touch "$HOME/.mello-needs-soloist" || echo "update-target: cannot write $HOME/.mello-needs-soloist" >&2
else
  rm -f "$HOME/.mello-needs-soloist"
fi

if [ "$LOCAL" = "$TARGET" ]; then
  STATUS=current
elif git merge-base --is-ancestor "$TARGET" "$LOCAL" 2>/dev/null; then
  STATUS=older
elif [ "$(cat "$BAD_UPDATE_FILE" 2>/dev/null)" = "$TARGET" ]; then
  STATUS=failed
else
  STATUS=update
fi

echo "$STATUS $CHANNEL $TARGET_NAME $TARGET"
