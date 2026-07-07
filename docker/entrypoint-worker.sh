#!/bin/sh
# Worker entrypoint: re-install any collection tarballs found in the bind-mounted
# ./ansible/collections/ before handing off to the celery command.
# This picks up tarballs added after the image was built (e.g. a new role's dependency)
# without requiring a full image rebuild.
set -e

COLLECTIONS_SRC=/app/ansible/collections
COLLECTIONS_DEST="${ANSIBLE_COLLECTIONS_PATH:-/opt/ansible/collections}"

find "$COLLECTIONS_SRC" -maxdepth 1 \( -name '*.tar.gz' -o -name '*.tar' \) \
    | xargs -r ansible-galaxy collection install -p "$COLLECTIONS_DEST" || true

exec "$@"
