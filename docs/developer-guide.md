# appci developer guide

What happens when you open a merge request on a system app (CarSettings, CarLauncher, ...), what
can block it, and how to fix it before you push. Machine and GitLab setup is in the
[admin guide](admin-guide.md).

## What runs on your MR

Every MR pipeline builds your commit **inside the full platform tree** (the same Soong build as the
product) and runs:

| Job | What it checks | Blocks the merge when |
|---|---|---|
| `preupload` | the project's `PREUPLOAD.cfg` hooks (as `repo upload` would) | a hook fails |
| `build` | the app (as `<Module>-debug.apk` on the userdebug target and `<Module>-release.apk` on the user target), its test modules and apps that depend on a changed library | a build fails |
| `risk` | manifest, permissions, `Android.bp`, aconfig flags, secrets, APK size, API files | a blocker (see below) |
| `lint` | Android Lint (Soong) | a **new error on a line you changed** |
| `checkstyle` | Java style (AOSP `android-style.xml`) | a **new error on a line you changed** |
| `ktlint` | Kotlin style (AOSP `.editorconfig`) | never (style findings are warnings) |
| `detekt` | Kotlin code smells / bugs | a **new error on a line you changed** |
| `unit_test` | Robolectric and host tests affected by the change | any test fails |
| `device_test` | instrumented tests on an emulator (TEST_MAPPING presubmit) | any test fails |
| `coverage` | merged coverage of all suites | less than 50 % of your changed lines are covered (only when you changed 10+ executable lines) |
| `docs` | Javadoc, class diagrams, Dokka (manual on MRs) | never |
| `sonar` | SonarQube analysis (default branch) | never on MRs |
| `report` | the dashboard | — |

**Warnings never block, and neither do findings in code you did not touch.** Legacy code with
hundreds of existing findings can be changed safely: only what your MR adds or modifies counts.

Tests are selected by impact: a change to app code runs all its unit suites; a test-only change
runs just those tests; docs-only changes run no tests; build files, manifests and `TEST_MAPPING`
run everything. Labels on the MR override this: **`full-ci`** runs everything,
**`device-test`** runs all instrumented tests.

## Reading the result

Open the `report` job, *Browse* the artifacts, `reports/app/index.html` (or follow the
*dashboard* link on the MR). From top to bottom:

1. **Verdict**: *All quality gates passed*, *Passed with warnings* or *Merge blocked*, with each
   blocking gate in one sentence,
   e.g. "Checkstyle: 1 new error on changed lines (allowed at most 0)".
2. **Key numbers**: new findings, tests passed, coverage, changed-line coverage, run time.
3. **Introduced by this change**: every new finding from every tool in one table, filterable by
   tool; each file name links to that line in your MR.
4. Tests (failed tests with their message), coverage per suite, static analysis (new vs existing),
   change risk, and every tool report and log, grouped by stage.

GitLab also shows the results directly on the MR: the **Test summary** (JUnit), **Code Quality**
(findings on your lines), and **coverage** in the *Changes* tab (green/red line markers).

Each job's log ends with its metrics, the gate result and the findings, new ones first and
marked `NEW`.

## Running appci on your own machine

The same checks as the MR pipeline run on your own AOSP tree, with the same gates, in seconds to
minutes. Nothing is pushed or committed, and no GitLab is needed.

### One-time setup

1. **A synced tree that builds** (`m <your app>` works). appci sets up its own build environment;
   no `lunch` needed.
2. **Get appci** anywhere (inside or outside the tree), at the release your projects use:

   ```bash
   git clone --branch v1.0.0 https://gitlab.example.com/tools/aosp-app-ci.git ~/aosp-app-ci
   ```

   It needs Python 3.8+ with PyYAML (`sudo apt install python3-yaml`).
3. **Check the machine and tree**, from the AOSP root:

   ```bash
   cd <aosp-root>
   ~/aosp-app-ci/bin/appci doctor
   ```

   The first run downloads the pinned tools (Detekt, Dokka, ...) into `~/.cache/appci/tools` and
   verifies them; behind a proxy, set `https_proxy` / `no_proxy` in your shell first.

### Which settings apply

appci reads `appci.yaml` from its checkout (the same file CI uses; `--config` or `$APPCI_CONFIG`
for another one). Tell it which platform your tree is, so the right lunch targets and JDK apply:

```bash
A="$HOME/aosp-app-ci/bin/appci --platform a14"     # your tree's platform in appci.yaml
```

On your machine the tree is the one you run from (or `--aosp-root`); in CI it is the runner's
workspace. Without a platform section, `--device-lunch <target>` sets the lunch target directly.

### Commands

From the AOSP root (`APP` = your app's path in the tree):

```bash
APP=vendor/xxx/xxxCarSettings

# style and static analysis on the lines you changed since upstream (~15 s)
$A run --app $APP --stages checkstyle,ktlint,detekt --changed-since @{upstream}
# plus the repo hooks, the build and Android Lint
$A run --app $APP --stages preupload,build,lint --changed-since @{upstream}
# unit tests with coverage (minutes)
$A run --app $APP --stages build,unit_test,coverage --changed-since @{upstream}
# everything the MR pipeline runs, except device tests and Sonar
$A run --app $APP --skip device_test,sonar --changed-since @{upstream}

xdg-open out/appci/latest/index.html                # the same dashboard as in CI
```

* Uncommitted edits are included. `--changed-since` takes any ref (`HEAD~3`, `aosp/main`, ...);
  without it appci reports every finding but cannot apply the changed-line gates.
* The exit code is 0 when every gate passes, 1 when a gate fails: usable in your own scripts or
  a git `pre-push` hook.
* `$A impact --app $APP` shows which tests your change selects and why.
* `$A discover --app $APP` shows the modules and test suites appci finds for the app.

### Device tests on your machine

* **Connected device or running emulator:**
  `$A run --app $APP --stages build,device_test --set stages.device_test.mode=device`
  (`ANDROID_SERIAL` picks one when several are connected).
* **appci's emulator:** set `APPCI_EMULATOR_IMAGE_URL` to the platform's image in Artifactory (the
  same one CI uses), or build one with `$A image` (several hours, once).

### What it touches

* Builds go into the tree's normal `out/` (incremental, like your own `m`); emulator builds into
  `out_appci_emu/`. Do not run `m` in the same tree at the same time.
* Reports and logs: `out/appci/` (`latest/` = the last run).
* Your sources and git state are never changed. Sonar runs only when `SONAR_HOST_URL` and
  `SONAR_TOKEN` are set; delivery of prebuilts (`deploy`) runs only in CI on release tags.

## Fixing what blocks you

| Blocked by | What to do |
|---|---|
| **Checkstyle** | Fix the reported line (common: line longer than 100 characters, import order, missing Javadoc on public API). The finding shows the rule name. |
| **Lint / Detekt error** | Fix it, or, if it is a false positive, suppress it at the narrowest scope (`@SuppressLint("Id")`, `@Suppress("RuleName")`) with a comment explaining why. |
| **Failed test** | The dashboard and the JUnit report show the assertion and stack trace. Reproduce locally with the command above or `atest <TestClass>`. |
| **Changed-line coverage** | Add or extend tests for the new code. The *Changes* tab marks uncovered lines in red. |
| **Risk blocker** | *Privileged permission not in the privapp allowlist*: add it to the app's `privapp-permissions` XML. *New aconfig flag without namespace/description/bug*: complete the flag. *Secret on an added line*: remove it (if it is not a real secret, add `appci:allow-secret` in a comment on that line). *API check failed*: update the API files (`m <module>-update-current-api`, or `m update-api`). |
| **Pre-upload hook** | Run `repo upload --dry-run` or the hook it names; usually formatting (`google-java-format`, `ktfmt`) or a commit message rule. |
| **Build** | The job log shows the first compiler errors; `reports/app/build/` has the full log. |

Review items in the *Change risk* section (new permissions, exported components, dependency
changes, production code without tests, ...) never block; they tell reviewers what to look at.

## Statuses

| Status | Meaning |
|---|---|
| passed | ran, gates OK, no findings |
| warning | ran, gates OK, but there are findings (existing code or warnings), or tests were *not run* |
| failed | a gate failed: this blocks the merge |
| error | the tool could not run (infrastructure, not your code); re-run the job, then tell the CI admins |
| skipped | not needed for this change (e.g. no tests impacted) |

A **device_test** with a yellow `!` and *not run* means no emulator image was available on the
runner; it does not block your MR, but your instrumented tests did not run.

## Releasing a prebuilt

Apps delivered as prebuilts are released with a tag on the default branch, e.g. `v1.2.0` (only
maintainers can create release tags). The tag pipeline runs every check again and then the
`deploy` job, which opens a merge request in the prebuilts repository with the user and userdebug
APKs, the updated privileged-permission allowlist and a summary of every commit since the
previous tag. Write good commit titles: they become the release notes.

## FAQ

**My MR is blocked by a finding in a file I only moved / reformatted.** Reformatted lines count as
changed. Fix the findings on those lines, or split the reformat into its own MR.

**The pipeline sits in *pending* / *waiting for resource*.** Jobs on one platform run one at a
time on the shared tree, so pipelines queue. If it stays pending with no other pipeline running,
tell the CI admins (no runner online).

**The first job takes very long.** It syncs the platform tree (at most every 12 h) and the first
build after a sync recompiles more. Later jobs in the pipeline are faster.

**Can I see Sonar results?** On the default branch, in SonarQube project `aaos-<app>` (e.g.
`aaos-car-settings`).

**My branch is built for several platforms.** The MR then shows one child pipeline per platform,
each with its own dashboard; every one of them has to pass. The checks and gates are the same,
only the tree, the lunch targets and the JDK differ.

**Do I have to add a lint module to `Android.bp`?** No. `<module>-lint` is a target Soong
generates for every app and library module; the `lint` job just asks for it. Only
`lint: { enabled: false }` in a module turns the report off.

**Where are the docs?** The `docs` job (manual on MRs, automatic on the default branch) builds
Javadoc, class diagrams and Kotlin API docs; on the default branch they are published to GitLab
Pages with the dashboard.
