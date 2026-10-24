# SafeRepo Evaluation Post-Mortem

## Summary

Out of 20 fresh evaluation runs, 16 succeeded and 4 failed, yielding
a success rate of 80%. All runs used freshly created Celesto Cloud computers
with unique sandbox names. Failed runs are retained in the report and no results were
deleted or rerun to improve the score.

## Failure Categories

The 4 failures fell into these categories:

1. **Setup/dependency installation failures (2 runs):** The guest's package manager
   could not install a required dependency within the timeout window. The pytest
   collection phase failed with `ModuleNotFoundError` before the test could execute.
   These are environmental failures, not bugs in the agent logic.

2. **Agent test-path validation failure (1 run):** The LLM generated a test path
   that did not match the required `tests/` directory convention. The pipeline
   rejected it at step 6 without executing untrusted code.

3. **Gold check mismatch (1 run):** The agent wrote a test that failed on buggy
   code but also failed on the fixed code, indicating the test was not targeting
   the actual bug. The bimodal verdict correctly classified this as a failure.

## Environmental vs Genuine Failures

- Environmental: 2 runs (dependency timeouts)
- Genuine task failures: 2 runs (path validation, gold check)

## Changes After Failures

No code changes were made mid-evaluation. The same pipeline code ran for all
20 attempts. Dependency timeout failures suggest increasing the bootstrap
timeout or pre-warming guests with common packages.

## Remaining Reliability Concerns

- Cloud startup latency varies between 6-14 seconds, adding unpredictable cost
- LLM tool-call quality depends on model temperature and prompt adherence
- The bootstrap script installs packages on every fresh guest; a cached template
  would reduce both latency and failure risk
- Network-dependent steps (git clone, pip install) have no retry logic beyond
  the SDK built-in behavior
