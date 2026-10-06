#!/usr/bin/env bash
# Run the exact release candidate in a disposable Ubuntu 24.04 systemd host.
set -euo pipefail

usage() {
    echo "usage: $0 --profile {release|deployment-canary|legacy-adoption} --artifacts DIR --candidate-sha SHA --wheel-sha256 SHA --bundle-sha256 SHA --output DIR" >&2
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
[[ "$profile" == release || "$profile" == deployment-canary || "$profile" == legacy-adoption ]] || usage
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
if [[ "$profile" == release || "$profile" == legacy-adoption ]]; then
    # The release and legacy-adoption profiles must be incapable of contacting
    # providers or remotes, even if an installed service unexpectedly attempts
    # a network operation.
    network_args=(--network none)
fi
state_mount_args=(--mount "type=volume,source=$volume_name,target=/var/lib/cortex")
if [[ "$profile" == legacy-adoption ]]; then
    # A Phase 2b host keeps /var/lib/cortex on the same filesystem as /etc and
    # /opt; legacy-quarantine moves objects with rename(2) and never copies, so
    # the legacy fixture lives on the container root filesystem.
    state_mount_args=()
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
    "${state_mount_args[@]}" \
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

# legacy-adoption (#1122): seed a Phase 2b-shaped host, then adopt, roll back,
# and re-adopt it through the installer CLI. It never imports a provider
# credential or runs a provider smoke; the release flow below is not used.
run_legacy_adoption_profile() {
    local legacy_output=$qualification_root/legacy
    docker exec "$container_name" install -d -o root -g root -m 0700 "$legacy_output"
    if ! docker exec "$container_name" /usr/local/libexec/cortex-legacy-adoption \
        --config /artifacts/install-config.yaml \
        --bundle /artifacts/bundle.json \
        --output-dir "$legacy_output" \
        --work-dir /run/cortex-install \
        --candidate-sha "$candidate_sha" \
        --wheel-sha256 "$expected_wheel_sha" \
        --bundle-sha256 "$expected_bundle_sha"; then
        # The step log names the failing step and the rollback report names any
        # retained object; neither holds credential material.
        docker exec "$container_name" cat "$legacy_output/legacy-adoption.json" >&2 || true
        docker exec "$container_name" cat "$legacy_output/legacy-rollback.json" >&2 || true
        die "legacy-adoption harness failed"
    fi
    docker exec "$container_name" /usr/local/libexec/cortex-release-qualification \
        --receipt "$receipt_path" \
        --install-evidence "$legacy_output/install-verification.json" \
        --legacy-evidence "$legacy_output" \
        --candidate-sha "$candidate_sha" \
        --wheel-sha256 "$expected_wheel_sha" \
        --bundle-sha256 "$expected_bundle_sha" \
        --image-digest "$image_digest" \
        --wheel-filename "$(basename "$wheel_path")" \
        --profile legacy-adoption \
        --output "$qualification_path" \
        --evidence-dir "$qualification_root/evidence"
    docker exec "$container_name" /usr/local/libexec/cortex-qualification-validate \
        --qualification "$qualification_path" \
        --candidate-sha "$candidate_sha" \
        --wheel-sha256 "$expected_wheel_sha" \
        --bundle-sha256 "$expected_bundle_sha" \
        --evidence-root "$qualification_root" \
        --require-legacy-profile
    docker cp "$container_name:$qualification_path" "$output_dir/qualification.json"
    docker cp "$container_name:$qualification_root/evidence" "$output_dir/evidence"
    python3 "$script_dir/validate.py" \
        --qualification "$output_dir/qualification.json" \
        --candidate-sha "$candidate_sha" \
        --wheel-sha256 "$expected_wheel_sha" \
        --bundle-sha256 "$expected_bundle_sha" \
        --evidence-root "$output_dir" \
        --require-legacy-profile
}

if [[ "$profile" == legacy-adoption ]]; then
    run_legacy_adoption_profile
    exit 0
fi
docker exec "$container_name" cortex install trust-root plan \
    --config /artifacts/install-config.yaml \
    --bundle /artifacts/bundle.json \
    --output "$plan_path"
plan_sha=$(docker exec "$container_name" sha256sum "$plan_path" | awk '{print $1}')

# Credential source basenames come from the installer's adapter allowlist
# (AGY is derived from the permgen credential row), never from harness literals.
# The candidate wheel is installed into the system interpreter above; the
# deployment venv does not exist until apply.
credential_source_basename() {
    docker exec "$container_name" python3 -c \
        'import sys; from paulsha_cortex.trust_root.install.core import credential_source_basename; print(credential_source_basename(sys.argv[1], sys.argv[2]))' \
        "$1" "$2"
}

agy_auth_leaf=$(credential_source_basename reviewer-planner agy)
builder_agy_auth_leaf=$(credential_source_basename builder agy)
copilot_auth_leaf=$(credential_source_basename reviewer-planner copilot)
manager_github_auth_leaf=$(credential_source_basename manager github)
for credential_leaf in "$agy_auth_leaf" "$builder_agy_auth_leaf" \
    "$copilot_auth_leaf" "$manager_github_auth_leaf"; do
    [[ "$credential_leaf" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || \
        die "credential adapter returned an unsafe source basename"
done

# The packaged release config keeps the builder on Codex. A host overlay may
# opt into AGY, in which case the plan is the authority for whether a separate
# builder credential must be supplied. Treat an unreadable/malformed plan as a
# harness failure instead of silently skipping the protected import.
builder_agy_required=0
if docker exec "$container_name" jq -e \
    'any(.required_credentials[]?; .principal == "builder" and .provider == "agy")' \
    "$plan_path" >/dev/null 2>&1; then
    builder_agy_required=1
else
    plan_query_status=$?
    [[ $plan_query_status -eq 1 ]] || die "install plan required_credentials could not be inspected"
fi

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

# The real template unit has read-only bindings for the deployment-owned Codex
# controls. Add this non-transactional runtime fixture only after the rollback
# proof and clean reinstall, so the rollback scanner never has to classify
# harness-authored state as installer-owned state. Credentials are still
# imported exclusively through the protected stdin path below.
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
    local fixture='{}'
    if [[ "$provider" == copilot ]]; then
        # The adapter validates the Copilot config.json shape; the fixture is
        # structurally valid but carries no usable credential.
        fixture='{"copilotTokens":{"qualification-fixture":"non-secret-qualification-fixture"}}'
    fi
    printf '%s' "$fixture" | docker exec -i "$container_name" \
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
    if (( builder_agy_required )); then
        import_secret CORTEX_RC_BUILDER_AGY_AUTH builder agy "/run/$builder_agy_auth_leaf"
    fi
    import_secret CORTEX_RC_AGY_AUTH reviewer-planner agy "/run/$agy_auth_leaf"
    import_secret CORTEX_RC_COPILOT_AUTH reviewer-planner copilot "/run/$copilot_auth_leaf"
    import_secret CORTEX_RC_MANAGER_GITHUB_AUTH manager github "/run/$manager_github_auth_leaf"
else
    # Exercise the production import/activation path without introducing a
    # credential or external authority into release qualification.
    import_fixture builder codex /run/auth.json
    if (( builder_agy_required )); then
        import_fixture builder agy "/run/$builder_agy_auth_leaf"
    fi
    import_fixture reviewer-planner agy "/run/$agy_auth_leaf"
    import_fixture reviewer-planner copilot "/run/$copilot_auth_leaf"
    import_fixture manager github "/run/$manager_github_auth_leaf"
fi

if [[ "$profile" == deployment-canary ]]; then
    # The packaged roster has neither the canary builder nor its independent
    # planner/reviewer. Install the operator overlay rendered by the same
    # qualification contract as the provider smokes and builder override, at the
    # PSC_PROJECT_CONFIG_ROOT the installed Manager actually reads.
    model_config_root=$(docker exec "$container_name" python3 -c '
import json, sys
for raw in open("/opt/cortex/etc/cortex-manager.env", encoding="utf-8"):
    key, _sep, value = raw.rstrip("\n").partition("=")
    if key == "PSC_PROJECT_CONFIG_ROOT":
        print(json.loads(value))
        break
else:
    sys.exit("PSC_PROJECT_CONFIG_ROOT is not installed")
')
    [[ "$model_config_root" == /var/lib/cortex/* ]] || die "model identity overlay root is outside qualification state"
    docker exec "$container_name" sh -eu -c '
        [ -d "$1" ] || install -d -o root -g root -m 0755 "$1"
        rm -f /run/cortex-install/model-identities.yaml
        python3 /usr/local/libexec/qualification/contract.py \
            --model-identity-overlay /run/cortex-install/model-identities.yaml
        install -o root -g root -m 0644 /run/cortex-install/model-identities.yaml "$1/model-identities.yaml"
        rm -f /run/cortex-install/model-identities.yaml
        python3 -c "import sys; from paulsha_cortex.coordinator.model_identities import load_model_identities; load_model_identities(sys.argv[1])" "$1"
    ' sh "$model_config_root"
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

# 一鍵升級演練（#1263）：上方已 qualified 的 receipt 是 prior，同一容器以已安裝的
# `/opt/cortex/venv/bin/cortex upgrade` 走完整流程。release profile 沒有網路，release
# 來源改用容器內與 GitHub REST 同形狀的本機目錄；同一 candidate 的 plan 只能靠
# host overlay digest 不同才成為新的 transaction，因此 overlay 重述 release 設定既有
# 的 `providers.builder`（有效設定不變）。三個僅限測試的參數只有在
# PSC_UPGRADE_QUALIFICATION=1 時才被接受。
upgrade_version=$(basename "$wheel_path")
upgrade_version=${upgrade_version#paulsha_cortex-}
upgrade_version=${upgrade_version%-py3-none-any.whl}
[[ "$upgrade_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    die "candidate wheel carries no MAJOR.MINOR.PATCH release version"
upgrade_release_source=/run/cortex-upgrade-release
upgrade_drill_report=/run/cortex-install/upgrade-drill-report.json
upgrade_report=/run/cortex-install/upgrade-report.json
upgrade_durable_report=/var/lib/cortex-installer/$upgrade_version/upgrade-report.json
upgrade_status_report=/run/cortex-install/upgrade-status.json
builder_codex_credential=/var/lib/cortex-builder/.codex/auth.json
upgrade_slot=/opt/cortex/venvs/$expected_wheel_sha
# `--json` 只把 report 寫進容器內的檔案，而 `trap cleanup EXIT` 會刪掉容器：每個失敗的
# `cortex upgrade` 檢查在 die 之前，先把 `--json` 輸出、durable report 與它指向的 verify
# evidence 印到 stderr，live RC 失敗時才留得下診斷。這次執行若在發布 report 之前就被拒
# （例如 preflight），`--json` 沒有輸出，同版本的 durable report 仍是先前（activate 前失敗
# 演練）留下的那一份：`started_at` 對不上時標示為可能過時，verify evidence 也只取這次的輸出。
upgrade_diagnostics() {
    local evidence evidence_report run_started durable_started
    echo "qualification: cortex upgrade --json output ($1):" >&2
    docker exec "$container_name" cat "$1" >&2 || true
    run_started=$(docker exec "$container_name" jq -r '.started_at // empty' \
        "$1" 2>/dev/null) || run_started=""
    durable_started=$(docker exec "$container_name" jq -r '.started_at // empty' \
        "$upgrade_durable_report" 2>/dev/null) || durable_started=""
    if [[ -n "$run_started" && "$run_started" == "$durable_started" ]]; then
        echo "qualification: durable upgrade report ($upgrade_durable_report):" >&2
        evidence_report=$upgrade_durable_report
    else
        echo "qualification: durable upgrade report ($upgrade_durable_report)," \
            "possibly stale: its started_at does not match this run's --json output" >&2
        evidence_report=$1
    fi
    docker exec "$container_name" cat "$upgrade_durable_report" >&2 || true
    evidence=$(docker exec "$container_name" jq -r '.verify_evidence // empty' \
        "$evidence_report" 2>/dev/null) || evidence=""
    if [[ "$evidence" == /* ]]; then
        echo "qualification: verify evidence ($evidence):" >&2
        docker exec "$container_name" cat "$evidence" >&2 || true
    fi
}
docker exec "$container_name" /usr/local/libexec/cortex-qualification-release-source \
    --artifacts /artifacts \
    --output "$upgrade_release_source" \
    --version "$upgrade_version" \
    --candidate-sha "$candidate_sha" \
    --wheel-sha256 "$expected_wheel_sha" \
    --bundle-sha256 "$expected_bundle_sha"
docker exec "$container_name" python3 -c '
import json, sys, yaml
config = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
overlay = {"providers": {"builder": list(config["providers"]["builder"])}}
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(overlay) + "\n")
' /artifacts/install-config.yaml /run/cortex-install/upgrade-host-overlay.json
docker exec "$container_name" install -o root -g root -m 0644 \
    /run/cortex-install/upgrade-host-overlay.json /var/lib/cortex-installer/host-overlay.yaml
upgrade_cli=(
    /opt/cortex/venv/bin/cortex upgrade "$upgrade_version"
    --release-source "$upgrade_release_source"
    --allow-same-version
    --prior-receipt "$receipt_path"
    --wait-idle 120
    --json
)
# 剛恢復的 Manager 可能正在改寫 durable jobs registry；`--wait-idle` 讓前置檢查重試而不是
# 立刻拒絕（讀不到 registry 一律視為忙碌）。
# activate 前注入失敗：prior 已記錄的 builder/codex 落點檔權限偏離 0600，credential
# handoff 拒絕繼承 → 工具自動 rollback 新 receipt，並把原本 active 的服務啟動回來。
docker exec "$container_name" chmod 0640 "$builder_codex_credential"
if docker exec --env PSC_UPGRADE_QUALIFICATION=1 "$container_name" \
    sh -eu -c 'umask 077; out=$1; shift; "$@" >"$out"' \
    sh "$upgrade_drill_report" "${upgrade_cli[@]}"; then
    upgrade_diagnostics "$upgrade_drill_report"
    die "one-command upgrade accepted a drifted inherited credential"
fi
docker exec "$container_name" chmod 0600 "$builder_codex_credential"
docker exec "$container_name" cat "$upgrade_drill_report"
if ! docker exec "$container_name" jq -e \
    '.result == "rolled-back" and .failed_step == "credentials"
     and .rollback.restore_safe == true
     and .rollback.prior_loaded_runtime.mismatch == ""' \
    "$upgrade_drill_report" >/dev/null; then
    upgrade_diagnostics "$upgrade_drill_report"
    die "pre-activate upgrade failure did not return to the prior receipt"
fi
for upgrade_service in cortex-egress-proxy.service cortex-manager.service cortex-monitor.service; do
    docker exec "$container_name" systemctl is-active --quiet "$upgrade_service" || \
        die "upgrade drill did not restore $upgrade_service"
done
# credentials 步驟前失敗的 drill 也會先跑 apply；rollback 只還原 active link，內容位址的
# candidate slot 仍留在磁碟上。完整升級要驗「新 wheel SHA 在 umask 077 下 fresh create
# 的 slot 可執行」，因此在 disposable container 先把 drill 留下的 slot 刪掉，再跑一次
# 同版升級。
docker exec "$container_name" test -d "$upgrade_slot" || \
    die "upgrade drill did not create the candidate venv slot"
docker exec "$container_name" rm -rf "$upgrade_slot"
docker exec "$container_name" test ! -e "$upgrade_slot" || \
    die "upgrade drill candidate venv slot was not removed"
# 模擬 executor 自行刷新登入檔（#1275）：codex 以 builder 帳號身分就地改寫 auth.json。
# 這裡同樣以 cortex-builder（driver 跑 job 帳號指令用的 `/usr/sbin/runuser -u`）在檔尾附加
# 一個換行（JSON 尾端空白，憑證語意不變，內容不讀出也不印出），inode、owner、group、mode、
# nlink 都不變，只有 sha256 改變。不能以 root 寫：`.codex` 是 sticky 目錄，kernel 的
# fs.protected_regular 會拒絕 root 以 O_CREAT 開啟（shell `>>`）別的帳號的檔。完整升級必須
# 照常接手，並在新 receipt 記錄改寫後的 sha。
builder_codex_prior_sha=$(docker exec "$container_name" jq -r \
    '[.credentials[] | select(.principal == "builder" and .provider == "codex")]
     | if length == 1 then .[0].sha256 else empty end' \
    "$receipt_path")
builder_codex_identity=$(docker exec "$container_name" \
    stat -c '%F %i %u %g %a %h' "$builder_codex_credential")
[[ "$builder_codex_identity" == "regular file "*" 600 1" ]] || \
    die "credential refresh drill changed the builder credential metadata"
if ! docker exec "$container_name" /usr/sbin/runuser -u cortex-builder -- \
    sh -eu -c 'printf "\n" >> "$1"' sh "$builder_codex_credential"; then
    die "credential refresh drill could not rewrite the builder credential as cortex-builder"
fi
[[ "$(docker exec "$container_name" \
    stat -c '%F %i %u %g %a %h' "$builder_codex_credential")" == "$builder_codex_identity" ]] || \
    die "credential refresh drill changed the builder credential metadata"
builder_codex_refreshed_sha=$(docker exec "$container_name" sha256sum "$builder_codex_credential")
builder_codex_refreshed_sha=${builder_codex_refreshed_sha%% *}
[[ "$builder_codex_prior_sha" =~ ^[0-9a-f]{64}$
   && "$builder_codex_refreshed_sha" =~ ^[0-9a-f]{64}$
   && "$builder_codex_refreshed_sha" != "$builder_codex_prior_sha" ]] || \
    die "credential refresh drill did not change the builder credential digest"
# 完整升級：apply → 繼承 prior 憑證 → activate → verify → loaded↔installed 一致。
if ! docker exec --env PSC_UPGRADE_QUALIFICATION=1 "$container_name" \
    sh -eu -c 'umask 077; out=$1; shift; "$@" >"$out"' \
    sh "$upgrade_report" "${upgrade_cli[@]}"; then
    upgrade_diagnostics "$upgrade_report"
    die "one-command upgrade failed"
fi
if ! docker exec "$container_name" jq -e '.result == "upgraded"' "$upgrade_report" >/dev/null; then
    upgrade_diagnostics "$upgrade_report"
    die "one-command upgrade did not complete"
fi
upgrade_receipt_path=$(docker exec "$container_name" jq -r '.receipt.path' "$upgrade_report")
upgrade_evidence_path=$(docker exec "$container_name" jq -r '.verify_evidence' "$upgrade_report")
[[ "$upgrade_receipt_path" == /* && "$upgrade_evidence_path" == /* ]] || \
    die "one-command upgrade report lacks the new receipt or verify evidence"
# 新 receipt 的 builder/codex 列記錄改寫後的 sha，並標示接手自 prior receipt；prior 匯入
# 時的 sha 仍在 prior receipt 自己那一列（#1275）。
if ! docker exec "$container_name" jq -e \
    --arg refreshed "$builder_codex_refreshed_sha" \
    --slurpfile prior "$receipt_path" \
    '[.credentials[] | select(.principal == "builder" and .provider == "codex")]
     | length == 1
       and .[0].sha256 == $refreshed
       and .[0].inherited_from == $prior[0].receipt_id' \
    "$upgrade_receipt_path" >/dev/null; then
    upgrade_diagnostics "$upgrade_report"
    die "upgrade did not record the refreshed builder credential digest"
fi
# `--status` 是只有正式部署才走的唯讀路徑（receipt chain 判定 effective receipt、維護
# 狀態）；它不吃任何僅限測試的參數。升級後生效中的必須就是剛升級的 receipt、loaded
# runtime 一致，而且沒有留下待 `--recover` 的 snapshot 或 lease marker。
if ! docker exec "$container_name" sh -eu -c 'out=$1; shift; "$@" >"$out"' \
    sh "$upgrade_status_report" /opt/cortex/venv/bin/cortex upgrade --status --json; then
    docker exec "$container_name" cat "$upgrade_status_report" >&2 || true
    die "cortex upgrade --status does not show the upgraded receipt in force"
fi
docker exec "$container_name" cat "$upgrade_status_report"
if ! docker exec "$container_name" jq -e --slurpfile report "$upgrade_report" \
    '.effective_receipt.path == $report[0].receipt.path
     and .effective_receipt.receipt_id == $report[0].receipt.receipt_id
     and .loaded_runtime == "match"
     and .maintenance_pending == false' \
    "$upgrade_status_report" >/dev/null; then
    docker exec "$container_name" cat "$upgrade_status_report" >&2 || true
    die "cortex upgrade --status does not show the upgraded receipt in force"
fi
for upgrade_account in cortex-egress-proxy cortex-manager; do
    if ! docker exec "$container_name" /usr/sbin/runuser -u "$upgrade_account" -- \
        sh -eu -c 'test -x /opt/cortex/venv/bin/cortex; /opt/cortex/venv/bin/cortex --help >/dev/null'; then
        die "upgraded venv is not executable as $upgrade_account"
    fi
done

# A fixed harness installed in the reference image always runs the five attack
# families and negative controls. Only deployment-canary mode adds provider
# smokes/runtime identity, Manager auth dry-run, and full intake-to-closeout;
# protected repository identity is never inferred from HOME or candidate JSON.
qualification_driver=/usr/local/libexec/cortex-release-qualification
driver_profile_args=(--profile "$profile")
driver_profile_args+=(
    --prior-receipt "$receipt_path"
    --upgrade-drill-report "$upgrade_drill_report"
    --upgrade-report "$upgrade_report"
)
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
    validator_profile_args+=(
        --canary-repository "$CORTEX_RC_PROBE_REPOSITORY"
        --canary-work-id "$CORTEX_RC_PROBE_WORK_ID"
        --canary-issue "$CORTEX_RC_PROBE_ISSUE"
    )
fi
wheel_filename=$(basename "$wheel_path")
docker exec "$container_name" "$qualification_driver" \
    --receipt "$upgrade_receipt_path" \
    --install-evidence "$upgrade_evidence_path" \
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
