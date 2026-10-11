# appci admin guide

For the people who set up and run appci: GitLab, build machines, platforms and apps.
Developers read the [developer guide](developer-guide.md); how appci works inside is in
[architecture](architecture.md).

1. [How it fits together](#1-how-it-fits-together)
2. [Setup, step by step](#2-setup-step-by-step)
3. [Configuration](#3-configuration)
4. [Several platforms and apps](#4-several-platforms-and-apps)
5. [Prebuilt delivery](#5-prebuilt-delivery)
6. [Device tests](#6-device-tests)
7. [Routine operations](#7-routine-operations)
8. [Security](#8-security)
9. [Troubleshooting](#9-troubleshooting)

## 1. How it fits together

```
 App project (e.g. CarSettings)              appci project (this repository)
 .gitlab-ci.yml includes the template ──►    ci/gitlab/app-pipeline.yml, appci.yaml
        │ merge request / tag
        ▼
 GitLab runner (Docker executor) on a build machine, tags appci-<platform>[-kvm]
        │ every job:
        │  1. starts the platform image ($APPCI_IMAGE)
        │  2. clones appci at $APPCI_VERSION
        │  3. `appci workspace`: repo init/sync of /appci/<platform> on the runner volume
        │  4. checks the commit out into that tree and runs one stage
        ▼
 Artifacts: dashboard, JUnit, Code Quality, coverage → shown on the MR
```

Every setting lives in one of these places:

| Place | What | Changed by |
|---|---|---|
| `appci.yaml` in the appci repository | platforms (manifest, lunch targets, JDK), gates, apps, prebuilt delivery, emulator image URLs, tool mirrors | CI admins, by MR and a new release tag |
| GitLab CI/CD variables | `APPCI_PROJECT`, `APPCI_VERSION`, credentials and tokens | GitLab maintainers |
| App project `.gitlab-ci.yml` | app path, platform, image | app maintainers |
| Runner config on each build machine | volume, `/dev/kvm`, tags, proxy | machine owner |

`ci/gitlab/app-pipeline.yml` is generated from `appci.yaml` (`bin/appci gitlab --out
ci/gitlab/app-pipeline.yml`); never edit it by hand.

**Platform image requirements.** appci uses your existing platform image. It needs Python 3.8+
with PyYAML (`python3-yaml`), `git`, `unzip`, a `.repo` folder at `/build_env/.repo` (or set
`workspace.reference`), and for device tests the emulator's libraries. Check it:

```bash
docker run --rm <image> sh -c 'python3 -c "import yaml, sys; print(sys.version)"; git --version; \
  which unzip; ls -d /build_env/.repo; ldconfig -p | grep -c -E "libpulse|libnss3|libGL|libgbm|libXss"'
```

The last number should be 5 or more if the machine runs device tests.

## 2. Setup, step by step

Placeholders: `gitlab.example.com` (your GitLab), `tools/aosp-app-ci` (where appci lives),
`proxy.example.com:8080` (your proxy), `platforms/tangorpro:latest` (your platform image).

### 2.1 Put appci on your GitLab

Create an empty project, then:

```bash
git clone <appci repository URL> aosp-app-ci && cd aosp-app-ci
git remote set-url origin https://gitlab.example.com/tools/aosp-app-ci.git
git push -u origin main
```

### 2.2 Edit `appci.yaml`

At minimum: the platform's manifest, the lunch targets, the apps, and where jobs clone appci
from. Section 3 lists every setting.

```yaml
platforms:
  tangorpro:
    manifest: {url: https://git.example.com/platform/manifest.git, branch: main, file: default.xml}
gitlab: {appci_project: tools/aosp-app-ci, appci_version: v1.0.0}
apps:
  - path: packages/apps/Car/Settings
```

The manifest must reach AOSP with an absolute URL (`fetch="https://android.googlesource.com/"`
or your mirror), not `fetch=".."`; otherwise a fresh `repo sync` fails with `remote: Not Found`.

Check the result: `bin/appci workspace --platform tangorpro --print`.

### 2.3 Regenerate the template and tag a release

```bash
python3 -m unittest discover -s tests
bin/appci gitlab --out ci/gitlab/app-pipeline.yml
git commit -am "Configure appci" && git push
git tag v1.0.0 && git push origin v1.0.0
```

App projects always use a tag, never `main`.

### 2.4 GitLab settings

1. Group with the app projects > *Settings > CI/CD > Variables*:

   | Key | Value | Flags | Needed |
   |---|---|---|---|
   | `APPCI_PROJECT` | `tools/aosp-app-ci` | | always |
   | `APPCI_VERSION` | `v1.0.0` | | always |
   | `APPCI_GIT_CREDENTIALS` | `https://bot:<token>@git.example.com` (several separated by spaces) | masked | private manifest or projects |
   | `APPCI_ARTIFACTORY_TOKEN` | read-only token | masked | Artifactory with auth |
   | `SONAR_HOST_URL`, `SONAR_TOKEN` | server, analysis token | token masked + protected | SonarQube |

2. appci project > *Settings > CI/CD > Job token permissions*: add the app projects or their
   group (otherwise jobs fail cloning appci with `403`).
3. Group > *Build > Runners > New group runner*: tags `appci-<platform>` and, with KVM,
   `appci-<platform>-kvm`; maximum job timeout at least 10 h. Keep the `glrt-…` token.

### 2.5 Build machine

Needs Linux x86_64, 16+ cores (32 recommended), 64+ GB RAM (128 recommended), a large disk
(about 500 GB per platform) and `/dev/kvm` for device tests.

```bash
# large disk at /srv, mounted at boot (nofail: the machine still boots without it)
sudo blkid /dev/nvme1n1
echo 'UUID=<uuid>  /srv  ext4  defaults,nofail  0  2' | sudo tee -a /etc/fstab
sudo mount -a && findmnt /srv
```

Docker's data belongs on the large disk too: add `"data-root": "/srv/docker"` to
`/etc/docker/daemon.json` and restart Docker. Snap Docker ignores `data-root`; bind-mount a
directory of the large disk over `/var/snap/docker/common/var-lib-docker` with a systemd mount
unit that Docker depends on, and run `sudo snap connect docker:removable-media` if the disk is
under `/mnt` or `/media`.

Install gitlab-runner (docs.gitlab.com/runner/install), then register the runner:

```bash
sudo mkdir -p /srv/appci
sudo ci/docker/setup-runner.sh --url https://gitlab.example.com --token glrt-... \
     --platform tangorpro --volume /srv/appci --kvm \
     --proxy http://proxy.example.com:8080 --no-proxy localhost,127.0.0.1,.example.com
sudo gitlab-runner verify
```

Leave out `--proxy`/`--no-proxy` without a proxy and `--kvm` without KVM. For several platforms
on one machine, run it once per platform and raise `concurrent` in
`/etc/gitlab-runner/config.toml`. Behind a TLS-intercepting proxy, add the proxy's CA certificate
to the platform image.

### 2.6 App project

`.gitlab-ci.yml` in the app's repository:

```yaml
include:
  - project: 'tools/aosp-app-ci'
    ref: v1.0.0
    file: '/ci/gitlab/app-pipeline.yml'
variables:
  APPCI_APP: packages/apps/Car/Settings     # path of the app in the tree
  APPCI_PLATFORM: tangorpro                 # section under platforms: in appci.yaml
  APPCI_IMAGE: platforms/tangorpro:latest   # image name as `docker images` shows it on the runner
  APPCI_VERSION: v1.0.0
```

In the app project: *Settings > Merge requests > Pipelines must succeed*; *Settings > CI/CD >
General pipelines > Timeout* at least 3 h; *Settings > Repository > Protected tags*: `v*`
(needed for prebuilt delivery).

### 2.7 First pipeline

Open a small MR. The first job copies `.repo` from the image (15–30 min), syncs and builds the
tree (a few hours); later jobs sync at most every 12 h and build incrementally. Check:

* the job log starts with `appci v1.0.0 on Python …`;
* all jobs pass; `device_test` shows *not run* (a warning) until an emulator image is configured;
* the dashboard is in any job's artifacts: `reports/app/index.html`;
* a commit that breaks a unit test turns the MR red and blocks the merge.

## 3. Configuration

`appci.yaml` overrides the defaults in `appci/config.py`. Precedence, later wins: defaults →
`appci.yaml` → the selected `platforms.<name>` section → per-app `apps[].stages` → CI variables
for workspace, emulator and deploy settings → `--set key=value`.

| Key | Meaning |
|---|---|
| `platforms.<name>.manifest` | `url`, `branch`, `file`, `groups`, `depth` of the platform manifest |
| `platforms.<name>.<any key>` | overrides for that platform (targets, `java_home`, stages, apps) |
| `targets.device` / `targets.emulator` | lunch target and `out_dir`; the emulator target builds with coverage |
| `build_env_secrets` | secret-looking CI variables (e.g. `SECRET_PASS`) the build may see; all others are hidden from build, test and hook processes |
| `targets.release` | the user build for prebuilt delivery (default: device target with `-user`, `out_user`) |
| `java_home` | JDK for host tools (default `prebuilts/jdk/jdk21/linux-x86`; needs 17+) |
| `workspace` | `path` (`/appci/{platform}`), `tree` (subdirectory holding the AOSP tree when the manifest nests it, e.g. `android`), `reference` (`/build_env`), `seed` (`copy`, `reference`, `none`), `adopt` (use a tree appci did not create, see 4.1), `sync.policy` (`if-older`, `always`, `never`), `sync.max_age_hours` (12) |
| `discovery.extra_prod_types` | vendor module types (`soong_config_module_type`, e.g. `xxx_car_app`) treated like `android_app`; when nothing under an app path counts as production code, the job log lists what Soong recorded there and why it was skipped |
| `stages.default` | stages run by `appci run` without `--stages` |
| `stages.<stage>.gate` | `max_*` / `min_*` limits; `new_*` metrics count only changed lines |
| `stages.preupload` | `skip_hooks`, `python`, `python_modules` |
| `stages.unit_test`, `stages.device_test` | `atest_args`, `timeout`; device: `mode` (`emulator`, `device`), `missing_image` (`warn`, `error`) |
| `stages.coverage` | `min_new_lines_for_gate` (10) and the gate |
| `stages.sonar` | `project_key_prefix`, `project_key_suffix`, `pull_request_analysis` |
| `stages.deploy` | section 5 |
| `emulator` | `image` and `program` `{url, pattern}`, `require_checksum` |
| `discovery.scan_roots` | where `appci discover --scan` looks for apps |
| `gitlab` | `appci_project`, `appci_version`, `volume`, `build_tags`, `emulator_tags`, `timeouts`, `allow_failure` |
| `tools.<name>` | `url` (internal copy) or `path` (installed tool) for a pinned tool |
| `downloads` | `verify_tls`, `wheel_base_url` |
| `apps[]` | `path`, `enabled`, per-app `stages` overrides |

**CI variables**

| Variable | Where | Meaning |
|---|---|---|
| `APPCI_APP`, `APPCI_PLATFORM`, `APPCI_IMAGE` | app project | app path, platform, image |
| `APPCI_VERSION`, `APPCI_PROJECT` | group | appci tag and project path |
| `APPCI_SOURCE` | group (optional) | another clone URL for appci |
| `APPCI_MANIFEST_URL`, `_BRANCH`, `_FILE`, `_GROUPS`, `_DEPTH` | optional | manifest without editing `appci.yaml` |
| `APPCI_EMULATOR_IMAGE_URL`, `_PATTERN`, `APPCI_EMULATOR_PROGRAM_URL` | optional | emulator sources |
| `APPCI_EMULATOR_IMAGE=true` | schedule | runs the `emulator_image` job |
| `APPCI_GIT_CREDENTIALS` | group, masked | credentials for private git servers |
| `APPCI_ARTIFACTORY_TOKEN` (`_HEADER`, `_USER`, `_PASSWORD`) | group, masked | Artifactory auth |
| `SONAR_HOST_URL`, `SONAR_TOKEN` | group | SonarQube |
| `APPCI_DEPLOY_TOKEN` | app project, masked + protected | prebuilt delivery |
| `APPCI_DEPLOY_REPO`, `_BRANCH`, `_MODE` | app project (optional) | override `stages.deploy` |
| `APPCI_PROGRESS_INTERVAL` | optional | seconds between progress lines (0 = off) |

**Network.** git, `repo` and appci's downloads use `http_proxy` / `https_proxy` / `no_proxy` from
the runner config. By default appci downloads its pinned tools from `github.com`,
`raw.githubusercontent.com`, `repo1.maven.org`, `binaries.sonarsource.com`, `pypi.org` and
`files.pythonhosted.org`. Without internet access, copy the files listed by `bin/appci tools`
(`bin/appci tools --fetch` downloads them) to an internal server and set `tools.<name>.url` and
`downloads.wheel_base_url`; the SHA-256 pins still apply, so a wrong file is refused.

## 4. Several platforms and apps

One appci repository serves every platform and app. Each platform gets a section in
`appci.yaml`, its own runners (`appci-a12`, `appci-a14`, …) and its own workspace on the volume.
Each app project's `.gitlab-ci.yml` (per branch, when an app has one branch per platform) names
its platform.

```yaml
platforms:
  a12:
    manifest: {url: https://git.example.com/a12/manifest.git, branch: a12-main, file: source.xml}
    targets:
      device:   {lunch: xxx_car-userdebug}
      emulator: {lunch: sdk_car_x86_64-userdebug}
    java_home: /usr/lib/jvm/java-17-openjdk-amd64    # A12 ships JDK 11; host tools need 17+
    stages:
      deploy: {repo: https://gitlab.example.com/prebuilts/a12-apps.git, branch: a12-main}
      sonar:  {project_key_suffix: "-a12"}
  a15:
    manifest: {url: https://git.example.com/a15/manifest.git, branch: a15-main, file: source.xml}
    targets:
      device:   {lunch: xxx_car-ap3a-userdebug}      # with the release config, where the branch has one
      emulator: {lunch: sdk_car_x86_64-ap3a-userdebug}
    stages:
      deploy: {repo: https://gitlab.example.com/prebuilts/a15-apps.git, branch: a15-main}
      sonar:  {project_key_suffix: "-a15"}

apps:
  - path: vendor/xxx/xxxCarSettings
    stages: {deploy: {path: CarSettings}}
  - path: vendor/xxx/xxxSystemUI
    stages: {deploy: {path: SystemUI}}
```

* An MR pipeline needs only the app project's `.gitlab-ci.yml`; an `apps:` entry is needed for
  per-app settings (gates, deploy folder).
* `platforms.<name>.apps` replaces the top-level list for that platform, for app settings that
  differ per platform.
* appci has been validated end to end on Android 17 (AAOS). On other releases, run one pilot MR
  per platform first; lunch target names and `java_home` are the usual differences. Android 14
  trees are handled: no `module-info` target, and module types read from Android.bp when Soong
  does not record them.

### 4.1 Tree baked into the image (no runner volume)

The runner volume (2.5) keeps one synced tree and a warm `out/` per platform on each machine, so
only the first job on a machine pays for sync and a full build. The alternative is to bake that
state into the platform image: the image pipeline syncs the tree and builds the apps once, and
every job starts from the finished state. Nothing is configured on the runner, any machine runs
any platform, and the disk holds one copy per platform per machine (the image), bounded and
cleaned up by Docker. Each job writes into the container's copy-on-write layer, which is
discarded with the job, so concurrent jobs never touch each other.

In the image pipeline (Dockerfile, after `.repo` and the credentials are in place):

```dockerfile
ENV APPCI_WORKSPACE=/build_env APPCI_PLATFORM=a14
# repo init + sync into /build_env, then a build of the apps so out/ is warm
RUN git clone --depth 1 -b v1.0.0 https://gitlab.example.com/tools/aosp-app-ci /opt/appci \
 && /opt/appci/bin/appci workspace \
 && /opt/appci/bin/appci run --app vendor/xxx/apps/Media --stages build
```

`appci run --stages build` is optional: without it the tree is ready but build jobs start cold.
With it, build jobs are incremental, at the cost of `out/` in the image (100 to 300 GB per
platform). The lunch target, `OUT_DIR` and the environment must be the ones appci uses, which is
why the build runs through appci; the same `appci.yaml` version is used in the image and in CI.

Then, per platform in `appci.yaml`:

```yaml
platforms:
  a14:
    workspace:
      tree: android              # when the manifest nests the tree
      adopt: true                # the tree is already there; manage it as it is (no repo init)
      sync: {policy: never}      # the image pipeline syncs; jobs only reset the app projects
```

and in the app project (or group) CI/CD variables `APPCI_WORKSPACE = /build_env`, which
overrides the template's default `/appci/{platform}`. `appci workspace` then finishes in seconds:
it resets the app projects and the stage checks the merge request commit out. The platform tree
is as old as the image; rebuild the image on the schedule the platform needs (daily is typical).
`appci workspace --sync` inside a container still forces a sync for a one-off check.

If the image carries only `.repo` (no checked-out tree) and you do not want a volume, use
`workspace: {seed: reference, sync: {jobs: 16}}` instead: `repo init --reference` borrows the
image's objects without copying them, which removes the copy from every job; the checkout and a
cold build remain.

## 5. Prebuilt delivery

For apps that products take as prebuilts. On a release tag (e.g. `v1.2.0`), after every other
job has passed, the `deploy` job:

1. builds the app as `user` (the device lunch target with `-user`, `OUT_DIR out_user`, or
   `targets.release`) and as `userdebug`;
2. reads the privileged permissions each APK requests (`aapt2`) and writes the privapp allowlist;
3. writes `Android.bp`: `android_app_import` (APK chosen by `debuggable`), `certificate:
   "platform"` or `presigned: true`, the partition and `privileged` as in the product, the source
   module's `overrides` and `<uses-library>` tags, and a `prebuilt_etc` installing the allowlist
   as `<package>-prebuilt.xml` (Android merges it with the platform's `<package>.xml`); plus
   `VERSION.json` and `SHA256SUMS`;
4. replaces the app's folder in the prebuilts repository and opens a merge request there (or
   pushes, `mode: push`). The commit message lists the source commits since the previous tag,
   changed areas, privileged-permission changes, variants and checksums.

```yaml
stages:
  deploy:
    repo: https://gitlab.example.com/prebuilts/apps.git
    branch: main
    path: "{name}"            # folder per app: CarSettings/, ...
    mode: mr                  # mr or push
    certificate: platform     # or presigned: true
    author_name: CI Bot
    author_email: ci-bot@example.com
    # overrides: [CarSettings]   # default: read from the app's Android.bp / Android.mk
```

Setup per prebuilts repository and app project:

1. Prebuilts project > *Settings > Access tokens*: role Developer, scopes `api` and
   `write_repository`.
2. App project (or group) > *Settings > CI/CD > Variables*: `APPCI_DEPLOY_TOKEN` = that token,
   masked and protected.
3. App project > *Settings > Repository > Protected tags*: `v*`. Protected variables reach only
   protected refs; without this the job stops with "no deploy token".
4. Release: `git tag v1.2.0 && git push origin v1.2.0`. The job log and the dashboard link the MR.

**Source and prebuilts manifests.** When products build from a manifest that has the prebuilts
repository instead of the app's sources, CI still uses the source manifest. The prebuilt keeps
the source module's name, so product makefiles work with either manifest; leave `prefer: false`.
The deploy job warns (summary, commit message, `VERSION.json`) about what a build without the
source project would lose:

* modules defined in the app's project that the product installs but that are not delivered
  (e.g. a permissions XML next to the app): deliver them another way or move them to the device tree;
* auto-generated product overlays (`<app>__<product>__auto_generated_rro_*`): Soong generates
  them for source apps only. If the product customises the app's resources with
  `PRODUCT_PACKAGE_OVERLAYS`, provide those overlays as `runtime_resource_overlay` modules in the
  device tree.

Check the first delivery of each app with a product build from the prebuilts manifest and compare
`installed-files.txt` with a source build.

## 6. Device tests

Device tests boot the platform's userdebug, test-keys emulator image and install the MR's build
over the system app.

```yaml
platforms:
  tangorpro:
    emulator:
      image:   {url: https://artifactory.example.com/artifactory/aaos-images/tangorpro/latest/, pattern: "RELEASE_*.zip"}
      program: {url: https://artifactory.example.com/artifactory/tools/emulator/latest/, pattern: "emulator*.zip"}
```

* `url` is a folder: the newest file matching `pattern` is used. A URL without a trailing `/`
  names one file.
* Each release is downloaded once per machine to `/appci/emulator/<platform>/`, checked against
  the SHA-256 the server publishes (Artifactory's `X-Checksum-Sha256` or a `<file>.sha256`) on
  every use, and the previous release is deleted. Releases without a published checksum are
  refused unless `emulator.require_checksum: false`.
* `program` is optional; without it the tree's `prebuilts/android-emulator` is used.
* No image: device tests are reported *not run* (warning, MR not blocked);
  `stages.device_test.missing_image: error` makes it a failure.
* Without Artifactory, a schedule with `APPCI_EMULATOR_IMAGE=true` runs the `emulator_image` job,
  which builds the image in the tree (up to 10 h).

## 7. Routine operations

| Task | How |
|---|---|
| Platform image updated | Nothing to do: the workspace was copied from the image once. If the image name changes, update `APPCI_IMAGE`. |
| Upgrade appci | Tag a release, then change `APPCI_VERSION` and the `include` `ref` together. |
| Force a sync | `appci workspace --sync` inside the image (see below), or `workspace.sync.policy: always` for a while. |
| Repair a workspace | `appci workspace --repair`: removes stale git locks, syncs with `--force-sync`, restores every project's files. |
| Rebuild a workspace | Stop the runner, `rm -rf /srv/appci/<platform>`, start the runner. The next job seeds again. |
| Disk full | Delete `out_appci_emu/` or `out/` (next build is a full build); `docker system prune`. Old emulator releases are removed automatically. |
| Run appci by hand | `docker run --rm -it -v /srv/appci:/appci <image> bash`, then `git clone <appci URL> /tmp/appci && /tmp/appci/bin/appci workspace --platform <platform> --print` |
| Rotate a credential | Update the masked variable; nothing is stored on the machines. |
| Check a runner | `sudo gitlab-runner verify`, `journalctl -u gitlab-runner` |

## 8. Security

What appci does:

* Build, test, lint and hook processes do not inherit CI secrets (`*TOKEN*`, `*PASSWORD*`,
  `*SECRET*`, `*CREDENTIAL*`, `CI_REPOSITORY_URL`, …); only sonar-scanner gets `SONAR_TOKEN`, plus the names
  listed in `build_env_secrets` (a vendor build step that unpacks resources with a password, for example;
  the job log warns that merge-request code can read them). Git
  credentials exist only during `repo sync`. Credentials in URLs are shown as `***` in logs.
* Cached tools, wheels and emulator releases are checked against their pinned or published
  SHA-256 on every use and run from a private copy per job. Archives with unsafe paths are refused.
* appci is cloned fresh for every job, outside the MR's checkout; an MR cannot supply its own.
  Credentials are not forwarded on redirects to another host.
* Reports escape MR text; XML with entity declarations is refused.

What you decide:

1. **One runner volume per trust level.** Jobs on a machine share the platform tree and run MR
   code with write access to it. Do not let forks or external contributors run on these runners.
2. **Least-privilege tokens.** Read-only `APPCI_GIT_CREDENTIALS` and `APPCI_ARTIFACTORY_TOKEN`;
   `SONAR_TOKEN` and `APPCI_DEPLOY_TOKEN` protected. Code in a job can read that job's own
   environment, so treat every secret visible to MR pipelines as readable by MR authors.
3. **Proxy without personal credentials** for CI.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Jobs *pending* or *waiting for resource* | No online runner with tag `appci-<platform>`, or another pipeline holds the platform tree (one job at a time per tree, by design). |
| `fatal: unable to access ... 403` cloning appci | The app project is not in appci's job token allowlist (2.4). |
| `appci needs PyYAML` | Install `python3-yaml` in the platform image (or on the developer machine). |
| `set APPCI_PLATFORM and APPCI_IMAGE` | Variables missing in the app project or group. |
| `no manifest URL` | Platform missing in `appci.yaml` and no `APPCI_MANIFEST_URL`. |
| `... is a repo tree appci did not create` | The workspace path holds another tree. Use another path, or `appci workspace --adopt`. |
| `WARN workspace: the previous sync did not finish` | Expected after a cancelled job; appci recovers on its own. If that fails too, rebuild the workspace. |
| Build fails on missing files in the tree (e.g. `ckati: no such file`) | A checkout was killed half-way: `appci workspace --repair`. |
| `repo sync`: `remote: Not Found` | The manifest uses `fetch=".."`; use an absolute URL (2.2). |
| `repo sync` authentication errors | `APPCI_GIT_CREDENTIALS` missing or expired for that host. |
| Job killed after 1 h | Project or runner timeout too low (2.4, 2.6). |
| `cannot compute the change vs <base>` | The MR base commit could not be fetched; check the app project's git access and `GIT_DEPTH`. |
| Device tests *not run* | No emulator image: check the URL and token in the job log. |
| `checksum mismatch` | Download corrupted or the file changed during upload; re-run the job. |
| `the server publishes no SHA-256` | Use an Artifactory repository, add `<file>.sha256`, or set `emulator.require_checksum: false`. |
| `no deploy token ... not a protected tag` | Protect tags `v*` (section 5). |
| Docker does not start after a reboot | The large disk is not mounted: `findmnt /srv`. |
| Sonar job skipped | `SONAR_HOST_URL` / `SONAR_TOKEN` not set. |

Every job log starts with the appci version, Python, git and uid, prints a progress line every
minute during long steps, and ends with metrics, gate results and findings. The dashboard links
every tool report and log.
