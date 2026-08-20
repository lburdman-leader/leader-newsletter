---
name: prune-tests
description: >
  Reduce a bloated test suite to the tests that earn their place — boundaries, invariants
  and real regressions — by merging near-duplicates and deleting tests that restate the
  implementation. Use when a suite has grown faster than the behaviour it covers, when the
  user says there are too many tests, or after a burst of feature work has left repetitive
  coverage behind.
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(python -m pytest:*), Bash(pytest:*), Bash(python -m coverage:*), Bash(coverage:*), Bash(ruff check:*), Bash(ruff format:*), Bash(python scripts/validate_repo.py:*), Bash(wc:*), Bash(ls:*), Bash(git diff:*), Bash(git status:*)
---

# Prune a test suite

## The governing rule

**A test earns its place only if it can fail for a reason no other test would catch.**

Everything below is that one rule applied. Test count is not a quality metric; a suite is
good when each test pins a distinct way the system could break. Two tests that fail
together for the same cause are one test wearing two hats.

Deleting a test is a real risk, so the burden of proof runs one way: **when you cannot
articulate the distinct failure a test catches, it goes — but when you are unsure whether
it is distinct, it stays.**

## 1. Establish the baseline before touching anything

```bash
python -m pytest -q                       # must be green before you start
python -m coverage run -m pytest -q && python -m coverage report   # only if installed
```

Record the pass count, and per-module coverage **if coverage tooling is available**. If it
is not, do not install it — dependency installs are gated in this repo. Say so in the
report and rely on the break-it check in step 5, which is the stronger signal regardless:
line coverage proves a line ran, while breaking the line proves a test was watching it.

If the suite is not green at the start, stop — pruning a broken suite hides which failures
you caused.

## 2. Inventory

For each test file, list every test as one line: name, the function or branch it exercises,
and the specific input that makes it distinct. Do this before judging anything — the
duplicates only become visible side by side.

Group tests that touch the same production function. Bloat is almost always *within* a
group, not across groups.

## 3. Classify every test: KEEP, MERGE or DELETE

### KEEP — these are the point of the suite

- **Boundaries.** Off-by-one, empty, exactly-at-threshold, one-below, one-above, zero, the
  maximum, `None`. If a test sits on an edge where behaviour changes, it stays.
- **Invariants and contracts.** Determinism, idempotence, ordering guarantees, "this must
  never appear in output", schema and type enforcement, purity of a function documented as
  pure.
- **Regressions pinning a real past bug.** The most valuable tests in any suite. See
  the protected list below.
- **Security and correctness boundaries.** Anything asserting untrusted input cannot become
  trusted, that a secret is absent, or that a value cannot be forged.
- **Failure paths.** Error branches, timeouts, retries, refusals, fail-open vs fail-closed
  behaviour. These are under-tested far more often than they are over-tested.

### MERGE — same behaviour, different data

The dominant form of bloat. Signals:

- Several tests differing only in the input value, all asserting the same shape of outcome.
- Names that differ by one noun (`test_handles_a_dict`, `test_handles_a_list`).
- Bodies you could diff and see one changed literal.

Collapse them with `pytest.mark.parametrize`, one case per row, with the edge cases kept as
explicit rows. A parametrized test with eight rows is **one** test that documents eight
inputs — that is the goal, not a trick to lower the count.

Also merge: multi-step setup repeated across tests that then assert on different fields of
the same result. One test, several assertions, is honest when the setup is the expensive
part and the assertions describe one behaviour.

### DELETE — these cost maintenance and buy nothing

- **Restating the implementation.** The test would have to be rewritten by anyone who
  refactors the function, and it asserts the mechanism rather than the behaviour.
- **Testing the language or a third-party library.** Pydantic validates, `sorted` sorts,
  the ORM persists. Test *your* use of them at the boundary, once.
- **Trivial accessors** with no logic — getters, passthroughs, `__repr__` with no contract.
- **Duplicate branch coverage.** Two tests entering the same branch by different doors.
  Keep the one whose failure message names the problem most clearly.
- **Tests asserting only that no exception was raised**, unless not-raising IS the
  documented behaviour.
- **Scaffolding left from development** — tests written to explore an API rather than to
  pin a requirement.

## 4. Protected — never delete, even if they look redundant

1. **Any test tied to a documented decision** (in this repo, an ADR in `docs/decisions.md`).
   If a test enforces a recorded decision, it is that decision's enforcement mechanism.
2. **Any test whose name, comment or git history marks it as a regression** for a specific
   bug. Check `git log -S` on the assertion before deleting anything suspicious.
3. **Any test that is the only one exercising a module, an error path, or a config key.**
4. **Golden-file and byte-identical-output tests** — they catch whole classes of change at
   once, which is exactly why they look redundant.

When in doubt about any of these, keep it and say so in the report.

## 5. Verify — the part that makes this safe

After every batch of changes:

```bash
python -m pytest -q
ruff check . && ruff format --check .
python -m coverage run -m pytest -q && python -m coverage report   # only if installed
```

If coverage tooling is available, **per-module line coverage must not drop**. If it does,
you deleted behaviour coverage, not duplication — restore the test and reclassify it.

**The break-it check is mandatory, with or without coverage.** It is what actually proves
the remaining tests bite. For each production function you pruned around, **temporarily**
break it — invert a condition, drop a guard, move a threshold by one — run the suite, and
confirm it fails. Then revert and confirm green again. A suite that stays green when you
break the code did not get leaner, it got weaker.

Do at least one break per file you touched, and target the specific behaviour whose tests
you merged or deleted, not some unrelated line in the same module. Report each break, what
you changed, and which test caught it — naming the test is the evidence. If a break goes
undetected, you removed real coverage: restore what you deleted around it.

Revert every mutation before reporting. Verify with `git diff` that no production file is
left modified.

## 6. Report

Give a table: file, tests before, tests after, and the single reason for the reduction.
Then, explicitly:

- every DELETE, with the distinct failure it could not catch (one line each);
- every MERGE, naming what was collapsed into what;
- anything kept despite looking redundant, and why;
- coverage before and after, per module;
- the break-it sanity check results;
- anything you were unsure about and left alone.

Never report a reduced count as the achievement. The achievement is that each surviving
test names a distinct way the system can break — say that, and show it.
