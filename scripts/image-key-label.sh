#!/bin/sh
# CI `image-key` job: add the rebuilds-images label to the PR if an image-key input
# changed, remove it if not. Needs GH_TOKEN (pull-requests: write), GH_REPO, PR, CHANGED.
set -eu
if [ "$CHANGED" = true ]; then
  gh label create rebuilds-images --color d93f0b \
    --description "changes a dev-image or executor-layer input" 2>/dev/null || true
  gh pr edit "$PR" --add-label rebuilds-images
else
  gh pr edit "$PR" --remove-label rebuilds-images 2>/dev/null || true
fi
