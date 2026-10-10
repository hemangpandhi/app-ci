# appci

CI for AOSP / AAOS system apps (CarSettings, CarSystemUI, vendor apps, ...). appci builds an app
inside the full platform tree, runs static analysis and tests, and blocks a merge request only
for what the change itself breaks. The same command runs on a developer's machine and in GitLab.

## What it checks

| Stage | Tool |
|---|---|
| preupload | the project's `PREUPLOAD.cfg` repo hooks |
| build | Soong (`m`), like the product build |
| risk | permissions, privapp allowlist, `Android.bp`, aconfig flags, secrets, APK size |
| lint, checkstyle, ktlint, detekt | Android Lint, Checkstyle, ktlint, Detekt |
| unit_test, device_test | `atest`: Robolectric and host tests; instrumented tests on an emulator |
| coverage | JaCoCo, merged over all suites, with coverage of the changed lines |
| docs, sonar | Javadoc with class diagrams, Dokka; SonarQube |
| deploy | on release tags: user and userdebug APKs, permission allowlist and `Android.bp` as an MR to a prebuilts repository |

An MR is blocked by a new error on a line it changed, a failing test, a failed build or hook, a
risk blocker, or too little coverage of its changed lines. Warnings and findings in untouched code
never block. Modules, tests and classpaths come from Soong's metadata; nothing is configured per app.

## Quick start on your own tree

Needs Python 3.8+ with PyYAML (`sudo apt install python3-yaml`) and a synced tree that builds.

```bash
git clone <appci repository URL> ~/aosp-app-ci
cd <aosp-root>
~/aosp-app-ci/bin/appci doctor
~/aosp-app-ci/bin/appci run --app packages/apps/Car/Settings \
    --stages checkstyle,ktlint,detekt,build,lint,unit_test,coverage --changed-since @{upstream}
xdg-open out/appci/latest/index.html
```

## In GitLab

The app project's `.gitlab-ci.yml` includes appci's template; jobs run in the platform's Docker
image on runners with a persistent AOSP tree per platform:

```yaml
include:
  - project: 'tools/aosp-app-ci'
    ref: v1.0.0
    file: '/ci/gitlab/app-pipeline.yml'
variables:
  APPCI_APP: packages/apps/Car/Settings
  APPCI_PLATFORM: tangorpro
  APPCI_IMAGE: platforms/tangorpro:latest
  APPCI_VERSION: v1.0.0
```

Results appear on the MR (tests, Code Quality, coverage) and in a dashboard in the job artifacts.

## Documentation

* [Developer guide](docs/developer-guide.md): what runs on an MR, what blocks it, how to fix it,
  running appci on your own machine.
* [Admin guide](docs/admin-guide.md): setup step by step, configuration, several platforms and
  apps, prebuilt delivery, device tests, operations, security, troubleshooting.
* [Architecture](docs/architecture.md): components, pipeline, stages, tools and licenses.
