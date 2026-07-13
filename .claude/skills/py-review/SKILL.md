---
name: py-review
description: Review Python code quality using ruff (lint + format), mypy (types), and bandit (security). Run on changed files, a path, or the whole project. Invoke when the user asks to review, check, lint, or audit Python code quality.
---

# Python Code Quality Review

Review Python code for style, type correctness, and security issues. Report findings grouped by severity and category, then summarise what to fix.

## Tools used

| Tool    | Checks                                    |
|---------|-------------------------------------------|
| ruff    | PEP8, imports, complexity, unused code    |
| mypy    | Static type checking                      |
| bandit  | Security vulnerabilities (OWASP / CWE)   |

## How to invoke

```
/py-review              # review all .py files changed vs main
/py-review app/         # review a specific directory
/py-review app/main.py  # review a single file
```

## Steps

1. Determine the target:
   - If the user passed a path argument, use it.
   - Otherwise, use `git diff --name-only main...HEAD -- '*.py'` to get changed Python files. If that list is empty, fall back to `app/` (the main source tree).

2. Ensure tools are available. If any are missing, install them with pip into the active environment:
   ```bash
   pip install ruff mypy bandit --quiet
   ```

3. Run each tool and capture output. Use these flags for clean, actionable output:
   ```bash
   # Ruff — lint
   ruff check <target> --output-format=concise

   # Ruff — format check (no changes)
   ruff format <target> --check --diff

   # Mypy — type check
   mypy <target> --ignore-missing-imports --no-error-summary

   # Bandit — security (medium+ severity only, skip tests/)
   bandit -r <target> --severity-level medium --exclude tests/ -q
   ```

4. Parse and present findings:
   - Group by tool, then by file.
   - For each finding show: file:line, rule/code, short message.
   - Highlight HIGH severity bandit findings prominently.
   - If a tool produced no findings, say "✓ No issues" for that tool.

5. End with a **Summary** section:
   - Count of issues per tool.
   - One-line verdict: "All clear", "Minor issues", or "Action required".
   - List the 3 most important things to fix (if any), ordered by impact.

## Output format example

```
### Ruff (lint)
app/presentation/routes.py:45  E501  line too long (103 > 99)
app/tasks/provision.py:12      F401  'os' imported but unused

### Ruff (format)
✓ No issues

### Mypy
app/domain/entities.py:18  error  Argument 1 to "get" has incompatible type "str | None"; expected "str"

### Bandit (security)
[HIGH] app/infrastructure/terraform/adapter.py:34
  B603 subprocess_without_shell_equals_true — subprocess call with shell=True is a security risk

---
**Summary**
- Ruff: 2 issues
- Mypy: 1 issue
- Bandit: 1 HIGH

**Verdict: Action required**

Top fixes:
1. [HIGH] Remove shell=True from subprocess call in terraform/adapter.py:34
2. Fix type mismatch in entities.py:18 — unwrap Optional before passing to get()
3. Remove unused import 'os' in tasks/provision.py:12
```

## Notes

- Never auto-fix files during a review — only report. The user decides what to change.
- If the target path doesn't exist, say so clearly and stop.
- If mypy reports "no .pyi stub" warnings only, suppress them and note "type stubs not installed".
- For ruff format diff output, show only the first 20 lines per file to keep output readable.
