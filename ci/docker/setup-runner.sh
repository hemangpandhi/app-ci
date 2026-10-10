#!/usr/bin/env bash
# Register a docker-executor GitLab runner for one platform on this machine.
#
#   sudo ci/docker/setup-runner.sh --url https://gitlab.example.com --token glrt-... \
#        --platform tangorpro --volume /srv/appci [--kvm] [--name buildhost01-tangorpro] \
#        [--proxy http://proxy.example.com:8080 --no-proxy localhost,127.0.0.1,.example.com]
#
# Create the runner first in GitLab (group or project > Settings > CI/CD > Runners > New runner)
# with the tags appci-<platform> (and appci-<platform>-kvm when it runs device tests), then pass
# its glrt- token here. Safe to re-run: an existing runner with the same name is left alone.
set -euo pipefail

url="" token="" platform="" volume="/srv/appci" kvm=0 name="" proxy="" no_proxy=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) url="$2"; shift 2 ;;
        --token) token="$2"; shift 2 ;;
        --platform) platform="$2"; shift 2 ;;
        --volume) volume="$2"; shift 2 ;;
        --kvm) kvm=1; shift ;;
        --name) name="$2"; shift 2 ;;
        --proxy) proxy="$2"; shift 2 ;;
        --no-proxy) no_proxy="$2"; shift 2 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$url" && -n "$token" && -n "$platform" ]] || { echo "need --url, --token and --platform" >&2; exit 2; }
name="${name:-$(hostname -s)-appci-$platform}"
config="${GITLAB_RUNNER_CONFIG:-/etc/gitlab-runner/config.toml}"

command -v docker >/dev/null || { echo "install Docker first (docs.docker.com/engine/install)" >&2; exit 1; }
command -v gitlab-runner >/dev/null || {
    echo "install gitlab-runner first: https://docs.gitlab.com/runner/install/linux-repository/" >&2; exit 1; }

root_dev="$(df --output=source / | tail -1)"
mkdir -p "$volume/.tools"
if [[ "$(df --output=source "$volume" | tail -1)" == "$root_dev" ]]; then
    echo "warning: $volume is on the root filesystem; a platform workspace needs ~300 GB+" >&2
fi
if [[ $kvm -eq 1 && ! -e /dev/kvm ]]; then
    echo "warning: --kvm given but /dev/kvm is missing on this host" >&2
fi

if [[ -f "$config" ]] && grep -q "name = \"$name\"" "$config"; then
    echo "runner '$name' already in $config; nothing to do"
    exit 0
fi

args=(register --non-interactive --config "$config" --url "$url" --token "$token" --name "$name"
      --executor docker --docker-image ubuntu:20.04 --docker-pull-policy if-not-present
      --docker-volumes "$volume:/appci" --docker-shm-size 8589934592 --docker-disable-cache
      --limit 1)
[[ $kvm -eq 1 ]] && args+=(--docker-devices /dev/kvm)
if [[ -n "$proxy" ]]; then
    for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do args+=(--env "$v=$proxy"); done
    [[ -n "$no_proxy" ]] && args+=(--env "no_proxy=$no_proxy" --env "NO_PROXY=$no_proxy")
fi
gitlab-runner "${args[@]}"
gitlab-runner verify --config "$config" >/dev/null
echo "registered '$name' (platform $platform, volume $volume -> /appci$([[ $kvm -eq 1 ]] && echo ', /dev/kvm'))"
echo "next: in the app project set APPCI_PLATFORM=$platform and APPCI_IMAGE=<platform image>, and allow"
echo "      the project in aosp-app-ci > Settings > CI/CD > Job token permissions."
