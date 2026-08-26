#!/usr/bin/env bash
# Run the exact release candidate in a disposable Ubuntu 24.04 systemd host.
set -euo pipefail

usage() {
    echo "usage: $0 --profile {release|deployment-canary} --artifacts DIR --candidate-sha SHA --wheel-sha256 SHA --bundle-sha256 SHA --output DIR" >&2
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
profile=""

while (($#)); do
    case "$1" in
        --profile) profile=${2-}; shift 2 ;;
        --artifacts) artifact_dir=${2-}; shift 2 ;;
        --candidate-sha) candidate_sha=${2-}; shift 2 ;;
        --wheel-sha256) expected_wheel_sha=${2-}; shift 2 ;;
        --bundle-sha256) expected_bundle_sha=${2-}; shift 2 ;;
        --output) output_dir=${2-}; shift 2 ;;
        *) usage ;;
    esac
done

[[ -d "$artifact_dir" && -n "$output_dir" ]] || usage
[[ "$profile" == release || "$profile" == deployment-canary ]] || usage
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
output_volume_name="cortex-qualification-output-${candidate_sha:0:12}-$$"

cleanup() {
    docker rm --force "$container_name" >/dev/null 2>&1 || true
    docker volume rm "$volume_name" >/dev/null 2>&1 || true
    docker volume rm "$output_volume_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build --pull --tag "$image_tag" --file "$script_dir/Dockerfile" "$script_dir"
image_digest=$(docker image inspect --format '{{.Id}}' "$image_tag")
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Docker image has no content digest"

docker volume create "$volume_name" >/dev/null
docker volume create "$output_volume_name" >/dev/null
network_args=()
if [[ "$profile" == release ]]; then
    # The release profile must be incapable of contacting providers or remotes,
    # even if an installed service unexpectedly attempts a network operation.
    network_args=(--network none)
fi
docker run --detach \
    --name "$container_name" \
    --hostname cortex-qualification \
    --privileged \
    --cgroupns=host \
    --tmpfs /run:rw,nosuid,nodev,mode=755 \
    --tmpfs /run/lock:rw,nosuid,nodev,noexec,mode=755 \
    --mount "type=bind,source=$artifact_dir,target=/artifacts,readonly" \
    --volume "/sys/fs/cgroup:/sys/fs/cgroup:rw" \
    --mount "type=volume,source=$volume_name,target=/var/lib/cortex" \
    --mount "type=volume,source=$output_volume_name,target=/qualification-output" \
    "${network_args[@]}" \
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
docker exec "$container_name" sh -eu -c \
    'python3 -m pip install --break-system-packages --no-index --no-deps /artifacts/wheelhouse/*.whl'

plan_path=/run/cortex-install/install-plan.json
receipt_path=/run/cortex-install/install-receipt.json
qualification_root=/qualification-output
qualification_path=$qualification_root/qualification.json

docker exec "$container_name" install -d -o root -g root -m 0700 /run/cortex-install
docker exec "$container_name" install -d -o root -g root -m 0700 "$qualification_root"
docker exec "$container_name" cortex install trust-root plan \
    --config /artifacts/install-config.yaml \
    --bundle /artifacts/bundle.json \
    --output "$plan_path"
plan_sha=$(docker exec "$container_name" sha256sum "$plan_path" | awk '{print $1}')

# Apply once for a fresh install and once more to prove idempotency. The public
# installer owns receipt replay; the harness never translates its plan to shell.
docker exec "$container_name" cortex install trust-root apply \
    --plan "$plan_path" --confirm-sha256 "$plan_sha" --receipt "$receipt_path"
# The real template unit has read-only bindings for the deployment-owned Codex
# controls. A clean reference image has no operator HOME, so seed only a
# non-secret, root-owned legacy control fixture after the installer creates the
# four accounts, then execute the production scaffold helper. Credentials are
# still imported exclusively through the protected stdin path below.
docker exec "$container_name" sh -eu -c '
    for account in cortex-builder cortex-reviewer-planner; do
        install -d -o root -g root -m 0755 "/var/lib/$account/.codex/plugins" "/var/lib/$account/.codex/skills"
        printf "%s\n" "# qualification control fixture" > "/var/lib/$account/.codex/config.toml"
        test -f "/var/lib/$account/.codex/hooks.json"
        printf "%s\n" "{}" > "/var/lib/$account/.codex/auth.json"
    done
    python3 -m paulsha_cortex.trust_root scaffold | sh -eu
    for principal in builder reviewer; do
        rm -f "/var/lib/cortex/config/codex-credentials/$principal/auth.json"
    done
    rm -f /var/lib/cortex-builder/.codex/auth.json /var/lib/cortex-reviewer-planner/.codex/auth.json
'
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

# The reference image never fetches mutable runtime tools.  Every executor and
# qualification helper must resolve through the root-owned installed wrappers.
for provider_tool in codex claude copilot agy srt openspec; do
    docker exec "$container_name" sh -eu -c \
        'test -x "/opt/cortex/toolchain/bin/$1" || { echo "missing hash-locked tool: $1" >&2; exit 1; }' \
        sh "$provider_tool"
done

# Credential inputs are deliberately not discovered from any HOME. Canary
# secrets and release-profile non-secret fixtures both travel over stdin into
# container tmpfs, pass through the production adapter, and are then removed.
# Secret values are never arguments, receipts, or log output.
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

import_fixture() {
    local principal=$1 provider=$2 source_path=$3
    printf '%s' '{}' | docker exec -i "$container_name" \
        sh -eu -c 'umask 077; cat > "$1"' sh "$source_path"
    docker exec "$container_name" cortex install trust-root credentials import \
        --receipt "$receipt_path" \
        --principal "$principal" \
        --provider "$provider" \
        --source "$source_path"
    docker exec "$container_name" sh -eu -c 'rm -f -- "$1"' sh "$source_path"
}

if [[ "$profile" == deployment-canary ]]; then
    import_secret CORTEX_RC_CODEX_AUTH builder codex /run/auth.json
    import_secret CORTEX_RC_AGY_AUTH reviewer-planner agy /run/oauth_creds.json
    import_secret CORTEX_RC_COPILOT_AUTH reviewer-planner copilot /run/hosts.json
    import_secret CORTEX_RC_MANAGER_GITHUB_AUTH manager github /run/hosts.yml
else
    # Exercise the production import/activation path without introducing a
    # credential or external authority into release qualification.
    import_fixture builder codex /run/auth.json
    import_fixture reviewer-planner agy /run/oauth_creds.json
    import_fixture reviewer-planner copilot /run/hosts.json
    import_fixture manager github /run/hosts.yml
fi

# Imported Codex material lands in the account's legacy ``~/.codex`` path. Run
# the same production scaffold once more so the canary credential or release
# fixture traverses the Manager-owned canonical projection used by
# ``spool_slot.provision_runtime_surfaces``. Existing controls and generated
# hooks are already present, so the scaffold remains idempotent.
docker exec "$container_name" sh -eu -c \
    'printf "%s\n" "{}" > /var/lib/cortex-reviewer-planner/.codex/auth.json
     python3 -m paulsha_cortex.trust_root scaffold | sh -eu
     rm -f /var/lib/cortex/config/codex-credentials/reviewer/auth.json
     rm -f /var/lib/cortex-reviewer-planner/.codex/auth.json'

docker exec "$container_name" cortex install trust-root activate --receipt "$receipt_path"
install_evidence_path=$qualification_root/install-verification.json
docker exec \
    --env "CORTEX_QUALIFICATION_CANDIDATE_SHA=$candidate_sha" \
    --env "CORTEX_QUALIFICATION_WHEEL_SHA256=$expected_wheel_sha" \
    --env "CORTEX_QUALIFICATION_BUNDLE_SHA256=$expected_bundle_sha" \
    --env "CORTEX_QUALIFICATION_IMAGE_DIGEST=$image_digest" \
    "$container_name" cortex install trust-root verify \
    --receipt "$receipt_path" --json --evidence "$install_evidence_path"

# A fixed harness installed in the reference image always runs the five attack
# families and negative controls. Only deployment-canary mode adds provider
# smokes/runtime identity, Manager auth dry-run, and full intake-to-closeout;
# protected repository identity is never inferred from HOME or candidate JSON.
qualification_driver=/usr/local/libexec/cortex-release-qualification
driver_profile_args=(--profile "$profile")
validator_profile_args=(--require-release-profile)
if [[ "$profile" == deployment-canary ]]; then
    [[ -n ${CORTEX_RC_PROBE_REPOSITORY:-} ]] || die "protected probe repository is unavailable"
    [[ -n ${CORTEX_RC_PROBE_WORK_ID:-} ]] || die "protected probe work id is unavailable"
    [[ ${CORTEX_RC_PROBE_ISSUE:-} =~ ^[1-9][0-9]*$ ]] || die "protected probe issue is unavailable"
    driver_profile_args+=(
        --probe-repository "$CORTEX_RC_PROBE_REPOSITORY"
        --probe-work-id "$CORTEX_RC_PROBE_WORK_ID"
        --probe-issue "$CORTEX_RC_PROBE_ISSUE"
    )
    validator_profile_args=(--require-canary-profile)
fi
wheel_filename=$(basename "$wheel_path")
docker exec "$container_name" "$qualification_driver" \
    --receipt "$receipt_path" \
    --install-evidence "$install_evidence_path" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --image-digest "$image_digest" \
    --wheel-filename "$wheel_filename" \
    "${driver_profile_args[@]}" \
    --output "$qualification_path" \
    --evidence-dir "$qualification_root/evidence"

docker exec "$container_name" /usr/local/libexec/cortex-qualification-validate \
    --qualification "$qualification_path" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --evidence-root "$qualification_root" \
    "${validator_profile_args[@]}"

# Evidence lives on a dedicated disposable Docker volume because Docker's archive
# API cannot read the /run tmpfs. docker cp still avoids a writable host bind; the
# only host input bind is the read-only artifact directory above.
docker cp "$container_name:$qualification_path" "$output_dir/qualification.json"
docker cp "$container_name:$qualification_root/evidence" "$output_dir/evidence"

# The evidence must remain valid after crossing the container boundary.
python3 "$script_dir/validate.py" \
    --qualification "$output_dir/qualification.json" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha" \
    --evidence-root "$output_dir" \
    "${validator_profile_args[@]}"
