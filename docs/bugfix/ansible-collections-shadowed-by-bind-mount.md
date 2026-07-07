# Bugfix: Ansible collections shadowed by bind-mount (#286)

## Root cause

Collections are installed into `/app/ansible/collections/ansible_collections/` during the Docker
image build (`ansible-galaxy collection install -p /app/ansible/collections`).

At runtime `docker-compose.yml` mounts the local checkout over the container's `/app`:

```yaml
volumes:
  - .:/app
```

This bind-mount replaces the entire `/app` tree, including
`/app/ansible/collections/ansible_collections/`. The local `./ansible/collections/` directory
contains only source tarballs (or is empty), not the installed namespace tree — so
`ansible-galaxy collection list` finds nothing and `ansible-playbook` fails with
`No module named 'ansible_collections.community'`.

## What changes

Move the install target to `/opt/ansible/collections` (outside `/app`) so the bind-mount cannot
shadow it, and add a worker entrypoint that re-runs the tarball install on each container start so
new tarballs can be picked up without a full image rebuild.

**`Dockerfile`**
- `ENV ANSIBLE_COLLECTIONS_PATH` → `/opt/ansible/collections`
- Both `-p` install flags → `/opt/ansible/collections`
- The `find /app/ansible/collections …` tarball step stays — tarballs are still read from the
  bind-mounted source path; they are just installed to the new location.
- `RUN groupadd/useradd` step gains `mkdir -p /opt/ansible/collections && chown -R portal:portal`
  so the `portal` user can write there at runtime (entrypoint runs as `portal`).

**`docker/entrypoint-worker.sh`** (new)
- Runs before Celery starts.
- `find ansible/collections/ | xargs ansible-galaxy collection install -p $ANSIBLE_COLLECTIONS_PATH`
- Allows dropping a new tarball into `./ansible/collections/` and `docker compose restart worker`
  to pick it up with no rebuild and no internet access.

**`docker-compose.yml`** worker service
- Adds `entrypoint: ["/app/docker/entrypoint-worker.sh"]`.

**`app/config.py`**
- Default `ANSIBLE_COLLECTIONS_PATH` → `"/opt/ansible/collections"`.

**`docs/admin-guide.md`**
- Updates the default path reference.
- Adds "Adding a collection without rebuilding the image" section.

## Expected behaviour after fix

- Collections installed at build time are present inside the running container even with the
  `- .:/app` bind-mount in effect.
- New collection tarballs dropped into `./ansible/collections/` are picked up on
  `docker compose restart worker` — no internet access or image rebuild required.

## Edge cases / notes

- Deployments that set `ANSIBLE_COLLECTIONS_PATH` explicitly in `.env` are unaffected.
- `/opt/ansible/collections` is created by `ansible-galaxy` at build time; the `mkdir -p` in the
  Dockerfile handles the edge case where no collections were installed at build.
- `beat` service does not run ansible and does not need the entrypoint.
