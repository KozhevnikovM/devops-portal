# Bugfix: Ansible collections tarballs not installed in worker image (#280)

## Root cause

`ansible-galaxy collection install` in the Dockerfile uses an `|| echo "WARNING:"` fallback that
silently swallows failures. When the build host has no internet access and
`ANSIBLE_COLLECTIONS_REQUIREMENTS` is not overridden, the install fails but the image build
continues.

Users who vendor tarballs by running:

```bash
ansible-galaxy collection download -r ansible/requirements.yml -p ansible/collections/
```

(downloading directly into `ansible/collections/` instead of the documented
`ansible/collections/vendor/`) end up with raw tarballs in that directory. `COPY . .` copies them
into the image, so `ls /app/ansible/collections/` shows the tarballs — but `ansible-galaxy
collection list` finds nothing because Ansible expects the collections to be extracted into
`ansible_collections/<namespace>/<name>/`, not stored as archives.

## What changes

### `Dockerfile`

After the primary `ansible-galaxy collection install -r "${ANSIBLE_COLLECTIONS_REQUIREMENTS}"` step
(which handles both online and vendored-requirements-yml installs), add a fallback that detects any
`.tar.gz` / `.tar` archives already in `ansible/collections/` and installs each one directly:

```dockerfile
RUN ansible-galaxy collection install -r "${ANSIBLE_COLLECTIONS_REQUIREMENTS}" \
        -p /app/ansible/collections || \
    find /app/ansible/collections -maxdepth 1 \( -name '*.tar.gz' -o -name '*.tar' \) | \
        xargs -r ansible-galaxy collection install -p /app/ansible/collections || \
    echo "WARNING: ansible-galaxy collection install failed (no internet, no vendor tarballs?)"
```

This handles all three scenarios without breaking existing workflows:

| Scenario | What happens |
|---|---|
| Online build (default) | Primary step succeeds; tarball fallback has nothing to do |
| Vendor via `requirements.yml` (documented path) | Primary step succeeds; tarball fallback has nothing to do |
| Tarballs placed directly in `ansible/collections/` | Primary step fails; tarball fallback installs each archive |

### `docs/admin-guide.md`

Add a note under the offline/vendored-install section clarifying that `ansible-galaxy collection
download` must target a **subdirectory** (e.g., `ansible/collections/vendor/`) — not the
`ansible/collections/` root — so it doesn't mix tarballs with the installed collection tree.

## Expected behaviour after fix

Building the image with tarballs in `ansible/collections/` (no internet, no vendor
`requirements.yml`) will properly install the collections. `ansible-galaxy collection list` inside
the worker will show all namespaces from the tarballs, and role tasks that depend on those
collections will succeed.

## Regression test

No automated test — this is a Dockerfile build-time behaviour that cannot be exercised by `pytest`.
Manual verification: copy tarballs to `ansible/collections/`, build the image offline, and confirm
`docker compose exec worker ansible-galaxy collection list` shows the expected collections.
