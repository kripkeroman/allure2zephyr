# allure2zephyr_dc.py — upload Allure results to Zephyr Scale (Jira DC)

Reads `target/allure-results`, finds cases via `@TmsLink`, puts them into a Test Cycle,
and sets statuses (Pass / Fail / Blocked / Not Executed).

## What it does
1. Reads `*-result.json`, collapses retries (keeps the latest attempt per case).
2. Takes case keys from `@TmsLink` (`PROJ-T123`) and resolves them to numeric ids.
3. Creates a new cycle **or** uses an existing one by key.
4. Adds missing cases to the cycle (already present cases are not duplicated).
5. Sets each case status from the worst run.

## Before you run

1. Set the Jira Personal Access Token in the environment:

```bash
export JIRA_PAT=<Personal Access Token>
```

2. Pass `--base-url` on every run — the Jira instance where results will be uploaded
   (no trailing slash), for example `https://jira.example.com`.

3. Every test that should appear in Zephyr **must** have a TmsLink with the Zephyr
   case key (`PROJ-T123`). Tests without it are skipped (`Tests without a link` in the output).

   Allure exposes TmsLink differently per language — use the API of the Allure adapter
   for your stack. The value must be (or contain) the Zephyr key.

   | Language | Typical Allure API |
   |----------|--------------------|
   | Java / Kotlin | `@TmsLink("PROJ-T123")` / `@TmsLinks({…})` |
   | Python (pytest) | `@allure.tms("PROJ-T123")` or `allure.dynamic.tms(...)` |
   | JavaScript / TypeScript | `allure.tms("PROJ-T123")` |
   | C# | `[TmsLink("PROJ-T123")]` |

   See Allure docs for your language if the adapter uses another name (`link` with type `tms`, etc.).
   The uploader reads `links` with type `tms` or `test_case` from `*-result.json`.

## Flags
| Flag | What it does | Default |
|------|----------------|---------|
| `--base-url URL` | Jira base URL to upload results to | **required** |
| `--allure-dir PATH` | Directory with Allure results | `target/allure-results` |
| `--cycle-key SM-C148` | Upload into an **existing** cycle (key from URL `/testCycle/{key}`). No new cycle is created | — |
| `--cycle-name "…"` | Name of a **new** cycle. Ignored if `--cycle-key` is set | `Automated run <date>` |
| `--description "…"` | Description of a new cycle (e.g. Allure report URL in CI) | — |
| `--project-id N` | Jira/Zephyr project id | required unless `--dry-run` |
| `--folder-id N` | Cycle folder id | required when creating a new cycle |
| `--jira-version-id ID` | Project version id | required unless `--dry-run` |
| `--cycle-status-id N` | Status id of a newly created cycle | required when creating a new cycle |
| `--owner-key JIRAUSER…` | Cycle owner / assignedTo | required unless `--dry-run` |
| `--status-passed N` | Zephyr status id for Allure `passed` | `22` |
| `--status-failed N` | Zephyr status id for Allure `failed` | `23` |
| `--status-broken N` | Zephyr status id for Allure `broken` | `24` |
| `--status-skipped N` | Zephyr status id for Allure `skipped` | `20` |
| `--dry-run` | Sends nothing; prints what would be uploaded and with which statuses | — |

Rule: `--cycle-key` and `--cycle-name` are mutually exclusive. If `--cycle-key` is set → append to that cycle.
Otherwise → create a new cycle named `--cycle-name`.

Status ids come from `GET /rest/tests/1.0/project/{projectId}/testresultstatus` on your instance.
Override the defaults if they do not match.

## Recipes

**See what would be uploaded, no API calls:**
```bash
python allure2zephyr_dc.py --base-url https://jira.example.com --dry-run
```

**Create a new cycle with a name:**
```bash
python allure2zephyr_dc.py \
  --base-url https://jira.example.com \
  --project-id 10200 \
  --folder-id 580 \
  --jira-version-id 10822 \
  --cycle-status-id 32 \
  --owner-key JIRAUSER11726 \
  --cycle-name "E2E #42 (develop)"
```

**Create a new cycle + description with a CI report link:**
```bash
python allure2zephyr_dc.py \
  --base-url https://jira.example.com \
  --project-id 10200 \
  --folder-id 580 \
  --jira-version-id 10822 \
  --cycle-status-id 32 \
  --owner-key JIRAUSER11726 \
  --cycle-name "E2E #42 (develop)" \
  --description "Allure: https://ci/job/42/allure"
```

**Append to an existing cycle (main scenario):**
```bash
python allure2zephyr_dc.py \
  --base-url https://jira.example.com \
  --project-id 10200 \
  --jira-version-id 10822 \
  --owner-key JIRAUSER11726 \
  --allure-dir ~/project/.../target/allure-results \
  --cycle-key SM-C148
```

**Results from another directory + dry-run:**
```bash
python allure2zephyr_dc.py \
  --base-url https://jira.example.com \
  --allure-dir /path/to/results \
  --cycle-key SM-C148 \
  --dry-run
```

## How to read the output
```
Cases with @TmsLink:    99        # tests bound to cases
Tests without a link:   10        # no @TmsLink — will not go into the cycle
Added new cases: 8 (already present: 91)
Statuses set: 99/99               # want 99/99
```
- `API returned testRunItems: ?` — normal: on success `bulk/save` does not return a list. Cases are still added.
- `Cases not found in Zephyr` + exit code `2` — a key in `@TmsLink` does not exist in Zephyr; check the case.
