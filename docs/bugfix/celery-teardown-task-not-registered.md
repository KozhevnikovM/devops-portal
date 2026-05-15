# Bugfix: Celery KeyError on teardown task (Issue #40)

## Root Cause

Celery workers only load task modules listed in the `include` parameter of the
`Celery(...)` constructor. `app/tasks/teardown.py` was created for the booking
release feature but never added to that list, so the worker had no registration
for `app.tasks.teardown.teardown_vm_task`. When the route queued the task, the
worker raised:

```
KeyError: 'app.tasks.teardown.teardown_vm_task'
```

## What Changes

**`app/infrastructure/celery_app.py`** — add `"app.tasks.teardown"` to the
`include` list:

```python
celery_app = Celery(
    "devops_portal",
    ...
    include=["app.tasks.provision", "app.tasks.teardown"],
)
```

## Expected Behaviour After Fix

Workers import `app.tasks.teardown` on startup and successfully handle
`teardown_vm_task` messages.

## No DB migrations required
