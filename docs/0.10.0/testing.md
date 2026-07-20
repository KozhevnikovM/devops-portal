# v0.10.0 Manual Testing Plan

Run through each section in order. Each step lists the action and the expected result.
A ✓ means the check passed; record any deviation as a bug.

---

## 1. Stack startup

**Prerequisites:** fresh `docker compose up -d` on the v0.10.0 image with a valid `.env`.

1. Run `docker compose ps`.
   **Expected:** all services (`app`, `worker`, `beat`, `postgres`, `redis`) show `(healthy)` within 60 s.

2. Run `curl -f http://localhost:8000/health`.
   **Expected:** `{"status": "ok"}` with HTTP 200.

3. Check `docker compose logs init | tail -20`.
   **Expected:** migration `0030` applied; no errors.

---

## 2. Auth — login & default password enforcement

4. Navigate to the portal login page. Enter `admin` / `changeme`.
   **Expected (stub/dev mode):** redirected to the change-password page; cannot skip it.
   **Expected (production mode):** server refuses to start without `ADMIN_PASSWORD` set — never reaches login.

5. On the change-password page, enter a new password shorter than 8 characters.
   **Expected:** inline error "Password must be at least 8 characters".

6. Enter a valid new password (≥ 8 chars) and submit.
   **Expected:** redirected to the main page; logged in successfully.

---

## 3. Auth — self-service password change

7. Log in as a regular user. Open **top-right menu → Profile**.
   **Expected:** Change Password form is visible.

8. Enter the wrong current password and a valid new password.
   **Expected:** error "Current password is incorrect" (or similar); password unchanged.

9. Enter the correct current password and a new password ≥ 8 chars.
   **Expected:** success message; session stays alive.

10. Open a second browser / incognito tab. Try to log in with the **old** password.
    **Expected:** login fails — old session invalidated.

---

## 4. Auth — admin password reset

11. As admin, navigate to **Admin → Users**. Click **Reset pw** on any user row.
    **Expected:** inline form appears with a password field.

12. Enter a password shorter than 8 characters and submit.
    **Expected:** error "Password must be at least 8 characters".

13. Enter a valid new password and submit.
    **Expected:** success; the user's sessions are invalidated.

14. Via the API, create a user with a 3-character password:
    ```bash
    curl -s -X POST http://localhost:8000/api/users \
      -H "Authorization: Bearer dp_<admin-key>" \
      -H "Content-Type: application/json" \
      -d '{"username": "testuser", "password": "abc", "role": "user"}'
    ```
    **Expected:** HTTP 422 with message about minimum password length.

15. Repeat with a password ≥ 8 chars.
    **Expected:** HTTP 201; user created.

---

## 5. CSRF protection

16. With `BASE_URL` set correctly in `.env` (e.g. `http://localhost:8000`), submit any
    form (login, booking, catalog edit).
    **Expected:** form submits normally; no 403.

17. *(If you have a reverse proxy)* With `BASE_URL` **not** set (defaulting to
    `http://localhost:8000`) but the browser reaching the app at a different host/port,
    submit a form.
    **Expected:** HTTP 403 Forbidden.

18. Set `BASE_URL` to the correct public origin and retry.
    **Expected:** form submits normally.

---

## 6. Ansible catalog — YAML default vars

19. Navigate to **Admin → Catalog**. Add a new Ansible role with **Default vars**:
    ```yaml
    version: latest
    debug: false
    ```
    **Expected:** role created without error; vars displayed as YAML in the role card.

20. Edit the role. The Default vars textarea should show the YAML form (not JSON).
    **Expected:** multi-line YAML displayed; `rows="4"` textarea.

21. Enter a YAML list instead of a mapping:
    ```yaml
    - item1
    - item2
    ```
    **Expected:** error "must be a YAML mapping".

22. Enter existing JSON syntax: `{"version": "latest"}`.
    **Expected:** accepted (JSON is valid YAML); saved and displayed as YAML on next view.

---

## 7. Ansible catalog — YAML secret vars

23. With `SECRET_VARS_ENABLED=true`, edit a role. In the **Secret vars** textarea enter:
    ```yaml
    db_password: s3cr3t
    api_token: abc123
    ```
    **Expected:** saved without error; role card shows `db_password=●●●  api_token=●●●`.

24. Enter a YAML list in the secret vars field:
    ```yaml
    - bad
    ```
    **Expected:** error "must be a YAML mapping".

25. Enter JSON syntax: `{"db_password": "s3cr3t"}`.
    **Expected:** accepted.

26. Leave the field blank and save.
    **Expected:** existing secrets are preserved (blank = keep existing).

---

## 8. Booking label

27. Open the booking form for a VM. Verify a **Label** field appears above Duration.

28. Enter a label (e.g. `PR #42 perf test`) and submit a booking.
    **Expected:** booking row shows the label above the resource name.

29. Submit a booking with no label.
    **Expected:** booking row shows no label line; no error.

30. Via the API, create a booking with a label:
    ```bash
    curl -s -X POST http://localhost:8000/api/bookings \
      -H "Authorization: Bearer dp_<key>" \
      -H "Content-Type: application/json" \
      -d '{"resource_type":"VM","ttl_minutes":60,"image_name":"...","hw_config_name":"...","label":"CI run #7"}'
    ```
    **Expected:** response includes `"label": "CI run #7"`.

31. `GET /api/bookings` — verify `"label"` key is present in every row (null for
    unlabelled bookings).

---

## 9. Environments — owner column & filters

32. Order two environments as different users (or use the dispatcher to order on behalf of
    another). Navigate to **Environments → All**.
    **Expected:** Owner column shows the correct username for each row.

33. For a dispatcher-ordered environment, verify the Owner cell shows the **owner's**
    username with a small `via <dispatcher>` subtitle below it.

34. Click **Mine**. **Expected:** only environments owned by the current user are shown.

35. Click **All**. **Expected:** all environments are shown regardless of owner.

36. Click **Show released**. **Expected:** released environments appear in the list.

37. Refresh the page. **Expected:** the active filter (Mine/All, Show released) is preserved
    in the URL and re-applied after reload.

---

## 10. Environments — QUEUED namespace adoption rejected

38. Find (or create) a namespace booking in `QUEUED` status. Attempt to order an
    environment that adopts it:
    ```bash
    curl -s -X POST http://localhost:8000/api/environments \
      -H "Authorization: Bearer dp_<key>" \
      -H "Content-Type: application/json" \
      -d '{"blueprint_id":"...","namespace_name":"<queued-ns>"}'
    ```
    **Expected:** HTTP 409 Conflict with a message explaining the namespace is queued.

39. Wait for the namespace to reach `READY`, then retry.
    **Expected:** environment created successfully.

---

## 11. Quota correctness

40. *(Requires stub or real adapter)* Book a VM with a 512 MB hardware config. Check the
    user's quota usage via **Admin → Users → quota**.
    **Expected:** 1 GB memory consumed (not 0 — `ceil(512/1024) = 1`).

41. While a booking is in `CONFIGURING` status, attempt to book a VM that would exceed
    the user's quota.
    **Expected:** booking is rejected (CONFIGURING now counts against quota).

---

## 12. Admin — force-release FAILED bookings

42. Locate a booking in `FAILED` status (or trigger one via the stub adapter by setting
    a very short retry count). Open the **⋮** menu on the booking row as admin.
    **Expected:** **Force release** option is visible.

43. Click **Force release**.
    **Expected:** booking transitions to `RELEASED`; row updates or disappears from active list.

---

## 13. Environment order rollback — no orphaned bookings

44. Order an environment with a blueprint that contains at least one VM resource. During
    ordering, simulate a failure mid-way (or configure the stub to fail after the first
    child booking is created).
    **Expected:** after the error response, no `PENDING` or `PROVISIONING` bookings remain
    in the database for that order. User quota is unchanged.

---

## 14. Docker healthcheck

45. Run `docker compose ps` after startup.
    **Expected:** `app` service shows `(healthy)`. No `/bin/sh: curl: not found` errors in
    `docker compose logs app`.

46. Stop the `app` container manually (`docker compose stop app`), wait 30 s, then restart.
    **Expected:** service returns to `(healthy)` within the configured `start_period`.

---

## 15. Regression — existing functionality

47. Book a VM (stub mode). Verify it transitions `PENDING → PROVISIONING → READY` and
    shows a fake IP.

48. Release the booking. **Expected:** status transitions to `RELEASED`.

49. Order an environment (stub mode). Verify all child bookings reach `READY` and the
    environment shows `READY`.

50. Release the environment. **Expected:** all children released; environment shows `RELEASED`.
