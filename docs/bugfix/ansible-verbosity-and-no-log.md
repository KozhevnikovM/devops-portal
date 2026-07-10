# Bugfix: Ansible playbook failures are opaque when secrets are configured (#283)

## Root cause

Two compounding problems make `ansible-playbook` failures impossible to diagnose:

1. **Play-level `no_log: true`** — `_render_playbook` in
   `app/infrastructure/config/ansible.py` sets `no_log: true` at the play level whenever
   `secret_vars` are present. Ansible's play-level `no_log` censors *every* task's output,
   not just the ones that reference secret variables. A task that generates an SSH key and
   has nothing to do with secrets still produces:
   ```
   {"censored": "the output has been hidden due to the fact that 'no_log: true' was specified"}
   ```

2. **8-line tail truncation** — `_run` only keeps the last 8 lines of ansible output in
   the `AnsibleConfigError` message. Even without censoring, a play that runs many tasks
   may have its useful failure context truncated.

There is also no mechanism to request more verbose ansible output (e.g. `-v` / `-vv`).

## What changes

### `app/infrastructure/config/ansible.py`

- **Remove play-level `no_log`**: instead, set `no_log: true` only on the synthetic
  `include_vars` task that loads the secrets file. This keeps secrets out of the
  `include_vars` output while leaving all other task results visible.
- **Log full output at DEBUG level**: write every captured line to
  `logger.debug("ansible: %s", line)` so operators can see the complete run with
  `CELERY_LOG_LEVEL=DEBUG` (or the equivalent `--loglevel=debug` on the worker).
- **Increase error tail** from 8 to 20 lines in the `AnsibleConfigError` message.
- **Add verbosity support**: read `settings.ANSIBLE_VERBOSITY` (int, default `0`) and
  append `-v` repeated that many times (1 → `-v`, 2 → `-vv`, 3 → `-vvv`) to the
  `ansible-playbook` command.

### `app/config.py`

Add `ANSIBLE_VERBOSITY: int = 0` (range 0–3).

### `docs/admin-guide.md`

Add a "Debugging ansible failures" section explaining:
- How to set `ANSIBLE_VERBOSITY=1` (or higher) in the portal's `.env`
- How to get full ansible output via `CELERY_LOG_LEVEL=DEBUG`
- Note that tasks which directly handle secret values should still carry their own
  `no_log: true` in the role definition

## Expected behaviour after fix

- A failing task's real error is visible in the worker log even when `secret_vars` are
  configured.
- The `include_vars` step that loads the secrets file still says `(censored)` — secrets
  don't leak.
- Setting `ANSIBLE_VERBOSITY=1` in `.env` adds `-v` to every `ansible-playbook`
  invocation, enabling task-level timing and more detailed module output.
- The `AnsibleConfigError` message shown in the booking's `config_log` field now includes
  the last 20 lines of output (up from 8).

## Regression test

No automated pytest test — this is a subprocess / external tool boundary. Manual
verification: configure a booking with a role whose task deliberately fails and
`secret_vars` set; confirm the worker log now shows the actual failure message rather
than the censored placeholder.
