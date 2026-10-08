"""Turn the real workflows into test copies.

Keeps triggers, run-name, permissions, job `if:`s, needs, concurrency,
environment, summary steps, the SHA check, checkout and artifact uploads
exactly as written. Swaps the self-hosted runner for ubuntu-latest and
replaces every step that needs GCP/dbt with a stub whose behaviour is driven
by files in test-control/ at the checked-out commit:

  test-control/<key>-fail    -> that stub exits 1
  test-control/<key>-sleep   -> that stub sleeps N seconds first

Stubs log STUB_START/STUB_END <key> <epoch> lines so overlaps between runs can
be checked from the logs.
"""

import sys

import yaml


def stub(key, extra=""):
    return f"""set -euo pipefail
echo "STUB_START {key} $(date -u +%s.%N) run=$GITHUB_RUN_ID attempt=$GITHUB_RUN_ATTEMPT job=$GITHUB_JOB"
if [ -f test-control/{key}-sleep ]; then sleep "$(cat test-control/{key}-sleep)"; fi
{extra}
if [ -f test-control/{key}-fail ]; then
  echo "STUB_FAIL {key}"
  echo "STUB_END {key} $(date -u +%s.%N)"
  exit 1
fi
echo "STUB_END {key} $(date -u +%s.%N)"
"""


def noop(name):
    return {"name": name, "shell": "bash", "run": f'echo "stubbed: {name}"'}


def transform_step(step, job):
    name = step.get("name", "")
    if name in (
        "Install dependencies",
        "Authenticate to Google Cloud",
        "Set up Cloud SDK",
        "Sweep Stale Candidate Datasets",
        "Cleanup Candidate Datasets",
    ):
        new = noop(name)
        for k in ("if", "continue-on-error"):
            if k in step:
                new[k] = step[k]
        return new
    if name == "dbt Run":
        return {
            **step,
            "run": stub(
                "staging-run",
                "mkdir -p target && echo staging > target/run_results.json",
            ),
        }
    if name == "dbt Test (Warning Only)":
        # Keep the real `|| echo ...` + continue-on-error shape
        return {
            **step,
            "run": "( "
            + stub("staging-test").replace("\n", "\n  ")
            + ' ) || echo "Warning: Some dbt tests failed or warned but continuing deployment"\n',
        }
    if name == "Test in Candidate Dataset":
        key = "sched-run" if job == "scheduled-production" else "prod-run"
        return {
            **step,
            "run": 'echo "candidate: $DBT_CANDIDATE_DATASET"\n'
            + stub(key, "mkdir -p target && echo prod > target/run_results.json"),
        }
    if name == "Deploy to Production (Copy Validated Tables)":
        return {
            **step,
            "run": stub(
                "copy",
                'echo "Copied \`$DBT_CANDIDATE_DATASET\` into production (stub)" >> "$GITHUB_STEP_SUMMARY"',
            ),
        }
    return step


def main(src, dst):
    with open(src) as f:
        wf = yaml.safe_load(f)
    # PyYAML reads the `on:` key as boolean True
    if True in wf:
        wf = {("on" if k is True else k): v for k, v in wf.items()}
    for job_id, job in wf["jobs"].items():
        assert job["runs-on"] == "Runner-2", job_id
        job["runs-on"] = "ubuntu-latest"
        job["steps"] = [transform_step(s, job_id) for s in job["steps"]]
        names = [s.get("name") for s in job["steps"]]
        assert "Checkout code" in names, job_id
    with open(dst, "w") as f:
        f.write(f"# GENERATED from {src} by transform.py - do not edit\n")
        yaml.safe_dump(wf, f, sort_keys=False, width=1000)


if __name__ == "__main__":
    main(*sys.argv[1:])
