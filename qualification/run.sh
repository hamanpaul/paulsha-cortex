#!/usr/bin/env bash
# Run the exact release candidate in a disposable Ubuntu 24.04 systemd host.
set -euo pipefail

usage() {
    echo "usage: $0 --artifacts DIR --candidate-sha SHA --wheel-sha256 SHA --bundle-sha256 SHA --output DIR" >&2
    exit 2
}

die() {
    echo "qualification: $*" >&2
    exit 1
}

artifact_dir=""
candidate_sha=""
expected_wheel_sha=""
expected_bundle_sha=""
output_dir=""

while (($#)); do
    case "$1" in
        --artifacts) artifact_dir=${2-}; shift 2 ;;
        --candidate-sha) candidate_sha=${2-}; shift 2 ;;
        --wheel-sha256) expected_wheel_sha=${2-}; shift 2 ;;
        --bundle-sha256) expected_bundle_sha=${2-}; shift 2 ;;
        --output) output_dir=${2-}; shift 2 ;;
        *) usage ;;
    esac
done

[[ -d "$artifact_dir" && -n "$output_dir" ]] || usage
[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]] || die "candidate SHA must be 40 lowercase hex characters"
[[ "$expected_wheel_sha" =~ ^[0-9a-f]{64}$ ]] || die "wheel SHA-256 must be 64 lowercase hex characters"
[[ "$expected_bundle_sha" =~ ^[0-9a-f]{64}$ ]] || die "bundle SHA-256 must be 64 lowercase hex characters"
command -v docker >/dev/null || die "docker is required"
[[ -f /sys/fs/cgroup/cgroup.controllers ]] || die "the host must use cgroup v2"

artifact_dir=$(realpath "$artifact_dir")
mkdir -p "$output_dir"
output_dir=$(realpath "$output_dir")
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
[[ -f "$artifact_dir/bundle.json" && ! -L "$artifact_dir/bundle.json" ]] || die "bundle.json must be a regular file"

mapfile -t wheel_files < <(find "$artifact_dir/dist" -maxdepth 1 -type f -name '*.whl' -print 2>/dev/null)
((${#wheel_files[@]} == 1)) || die "exactly one candidate wheel is required"
wheel_path=${wheel_files[0]}
wheel_name=${wheel_path#"$artifact_dir/"}

(
    cd "$artifact_dir"
    printf '%s  %s\n' "$expected_wheel_sha" "$wheel_name" | sha256sum --check --strict -
    printf '%s  %s\n' "$expected_bundle_sha" bundle.json | sha256sum --check --strict -
)
python3 "$script_dir/verify_bundle.py" \
    --bundle "$artifact_dir/bundle.json" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha"

image_tag="cortex-qualification:${candidate_sha}"
container_name="cortex-qualification-${candidate_sha:0:12}-$$"
volume_name="cortex-qualification-data-${candidate_sha:0:12}-$$"

cleanup() {
    docker rm --force "$container_name" >/dev/null 2>&1 || true
    docker volume rm "$volume_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build --pull --tag "$image_tag" --file "$script_dir/Dockerfile" "$script_dir"
image_digest=$(docker image inspect --format '{{.Id}}' "$image_tag")
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Docker image has no content digest"

docker volume create "$volume_name" >/dev/null
docker run --detach \
    --name "$container_name" \
    --hostname cortex-qualification \
    --privileged \
    --cgroupns=host \
    --tmpfs /run:rw,nosuid,nodev,mode=755 \
    --tmpfs /run/lock:rw,nosuid,nodev,noexec,mode=755 \
    --mount "type=bind,source=$artifact_dir,target=/artifacts,readonly" \
    --mount "type=bind,source=/sys/fs/cgroup,target=/sys/fs/cgroup,rw" \
    --mount "type=volume,source=$volume_name,target=/var/lib/cortex" \
    "$image_tag" >/dev/null

for _attempt in $(seq 1 60); do
    if docker exec "$container_name" systemctl is-system-running --wait >/dev/null 2>&1; then
        break
    fi
    [[ $_attempt -lt 60 ]] || die "systemd did not become ready"
    sleep 1
done

# Recheck immutable inputs inside the qualification host, then install only the
# candidate wheel from its hash-locked wheelhouse. No checkout is available.
docker exec "$container_name" sh -eu -c \
    'printf "%s  %s\n" "$1" "$2" | sha256sum --check --strict -
     printf "%s  %s\n" "$3" /artifacts/bundle.json | sha256sum --check --strict -' \
    sh "$expected_wheel_sha" "/artifacts/$wheel_name" "$expected_bundle_sha"
docker exec "$container_name" /usr/local/libexec/cortex-qualification-verify-bundle \
    --bundle /artifacts/bundle.json \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha"
docker exec "$container_name" python3 -m pip install \
    --break-system-packages \
    --no-index \
    --find-links /artifacts/wheelhouse \
    "/artifacts/$wheel_name"

plan_path=/var/lib/cortex/qualification/install-plan.json
receipt_path=/var/lib/cortex/qualification/install-receipt.json
qualification_path=/var/lib/cortex/qualification/qualification.json

docker exec "$container_name" cortex install trust-root plan \
    --config /artifacts/install-config.yaml \
    --bundle /artifacts/bundle.json \
    --output "$plan_path"
plan_sha=$(docker exec "$container_name" sha256sum "$plan_path" | awk '{print $1}')

# Apply once for a fresh install and once more to prove idempotency. The public
# installer owns receipt replay; the harness never translates its plan to shell.
docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"
docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"

# A functional unit drift must be rejected. Rollback then proves that the
# interrupted/adopted transaction is recoverable before the exact plan is
# installed again. A comment-only mutation is intentionally not used because
# generated-vs-installed attestation classifies comments as WARN.
manager_unit=/etc/systemd/system/cortex-manager.service
docker exec "$container_name" test -f "$manager_unit"
docker exec "$container_name" cp --preserve=all "$manager_unit" /run/cortex-manager.service.before-drift
docker exec "$container_name" sh -eu -c \
    'printf "\n[Service]\nNoNewPrivileges=false\n" >> "$1"' sh "$manager_unit"
if docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"; then
    die "installer accepted functional drift"
fi
# Restore the harness-owned drift before asking rollback to act. Safe rollback
# is required to reject unknown current bytes, not overwrite them.
docker exec "$container_name" cp --preserve=all /run/cortex-manager.service.before-drift "$manager_unit"
docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"
docker exec "$container_name" cortex install trust-root rollback --receipt "$receipt_path"
docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"

# The reference image deliberately does not fetch mutable toolchains from the
# network. The exact install bundle must deliver all three provider executables.
# Until it does, RC qualification is intentionally impossible to pass.
for provider_tool in codex agy copilot; do
    docker exec "$container_name" sh -eu -c \
        'command -v "$1" >/dev/null || { echo "missing hash-locked provider tool: $1" >&2; exit 1; }' \
        sh "$provider_tool"
done

# Credentials are deliberately not discovered from any HOME. Each protected
# environment secret is streamed over stdin into container tmpfs, imported by
# the explicit provider adapter, and then removed. Secret values are never
# arguments, receipts, or log output.
import_secret() {
    local variable_name=$1 principal=$2 provider=$3 source_path=$4
    [[ -n ${!variable_name:-} ]] || die "required protected secret $variable_name is unavailable"
    printf '%s' "${!variable_name}" | docker exec -i "$container_name" \
        sh -eu -c 'umask 077; cat > "$1"' sh "$source_path"
    docker exec "$container_name" cortex install trust-root credentials import \
        --receipt "$receipt_path" \
        --principal "$principal" \
        --provider "$provider" \
        --source "$source_path"
    docker exec "$container_name" sh -eu -c 'rm -f -- "$1"' sh "$source_path"
    unset "$variable_name"
}

import_secret CORTEX_RC_CODEX_AUTH cortex-builder codex /run/auth.json
import_secret CORTEX_RC_AGY_AUTH cortex-reviewer-planner agy /run/oauth_creds.json
import_secret CORTEX_RC_COPILOT_AUTH cortex-reviewer-planner copilot /run/hosts.json
import_secret CORTEX_RC_MANAGER_GITHUB_AUTH cortex-manager github /run/hosts.yml

docker exec "$container_name" cortex install trust-root activate --receipt "$receipt_path"
install_evidence_path=/var/lib/cortex/qualification/install-verification.json
docker exec \
    --env "CORTEX_QUALIFICATION_CANDIDATE_SHA=$candidate_sha" \
    --env "CORTEX_QUALIFICATION_WHEEL_SHA256=$expected_wheel_sha" \
    --env "CORTEX_QUALIFICATION_BUNDLE_SHA256=$expected_bundle_sha" \
    --env "CORTEX_QUALIFICATION_IMAGE_DIGEST=$image_digest" \
    "$container_name" cortex install trust-root verify \
    --receipt "$receipt_path" --json --evidence "$install_evidence_path"

# Provider smokes, runtime-model metadata, all five attack families (including
# negative controls), Manager auth dry-run, and full intake-to-closeout must be
# run by a fixed harness installed in the reference image, not by candidate JSON.
# That driver is intentionally absent until those executable probes land; this
# guard makes the current workflow fail closed and therefore unable to bless an RC.
qualification_driver=/usr/local/libexec/cortex-release-qualification
docker exec "$container_name" test -x "$qualification_driver" || \
    die "trusted provider/attack/full-dispatch qualification driver is not implemented"
docker exec "$container_name" "$qualification_driver" \
    --receipt "$receipt_path" \
    --install-evidence "$install_evidence_path" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --image-digest "$image_digest" \
    --output "$qualification_path" \
    --evidence-dir /var/lib/cortex/qualification/evidence

docker exec "$container_name" /usr/local/libexec/cortex-qualification-validate \
    --qualification "$qualification_path" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --evidence-root /var/lib/cortex/qualification \
    --require-full-suite

# docker cp is used instead of binding a host output directory. The only host
# input bind is the read-only artifact directory above.
docker cp "$container_name:$qualification_path" "$output_dir/qualification.json"
docker cp "$container_name:/var/lib/cortex/qualification/evidence" "$output_dir/evidence"

# The evidence must remain valid after crossing the container boundary.
python3 "$script_dir/validate.py" \
    --qualification "$output_dir/qualification.json" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --evidence-root "$output_dir" \
    --require-full-suite
