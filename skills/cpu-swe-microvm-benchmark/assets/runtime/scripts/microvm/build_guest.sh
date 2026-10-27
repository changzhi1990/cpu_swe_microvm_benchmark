#!/usr/bin/env bash
# Build a local ext4 guest from an existing Docker image. Requires sudo for mkfs.
set -euo pipefail
IMAGE="${1:?usage: build_guest.sh IMAGE OUTPUT_DIR}"
OUT="$(realpath -m "${2:?output directory required}")"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$OUT"
test ! -e "$OUT/rootfs.ext4" || { echo 'Refusing to overwrite existing rootfs.ext4' >&2; exit 1; }
STAGE="$(mktemp -d "$OUT/build.XXXXXX")"
CID="$(docker create "$IMAGE" /bin/true)"
trap 'docker rm "$CID" >/dev/null 2>&1 || true' EXIT
docker export "$CID" | sudo tar --warning=no-timestamp -xf - -C "$STAGE"
sudo mkdir -p "$STAGE/opt/cpu-swe" "$STAGE/workspace" "$STAGE/proc" "$STAGE/sys" "$STAGE/dev" "$STAGE/tmp"
sudo cp "$ROOT/scripts/microvm/guest_service.py" "$STAGE/opt/cpu-swe/guest_service.py"
sudo cp "$ROOT/scripts/microvm/guest-init.sh" "$STAGE/sbin/cpu-swe-init"
sudo chmod 755 "$STAGE/sbin/cpu-swe-init"
: "${MINI_SWE_AGENT_SRC:?set MINI_SWE_AGENT_SRC to the source directory containing minisweagent}"
sudo mkdir -p "$STAGE/opt/cpu-swe-benchmark" "$STAGE/opt/mini-swe-agent/src"
sudo cp -a "$ROOT/src" "$ROOT/repo_templates" "$STAGE/opt/cpu-swe-benchmark/"
sudo cp -a "$MINI_SWE_AGENT_SRC/minisweagent" "$STAGE/opt/mini-swe-agent/src/"
if [[ -f "$MINI_SWE_AGENT_SRC/../LICENSE.md" ]]; then sudo cp "$MINI_SWE_AGENT_SRC/../LICENSE.md" "$STAGE/opt/mini-swe-agent/LICENSE.md"; fi
sudo cp "$ROOT/scripts/microvm/guest_task.py" "$STAGE/opt/cpu-swe/guest_task.py"

# Optional preinstalled Python environment in the source container.
if [[ -n "${GUEST_VENV:-}" ]]; then sudo ln -s "$GUEST_VENV" "$STAGE/opt/benchmark-venv"; fi
sudo chroot "$STAGE" /bin/bash -c 'export PATH=/opt/benchmark-venv/bin:/usr/bin:/bin; python3 -c "import pytest,numpy; print(pytest.__version__,numpy.__version__)"'
sudo chroot "$STAGE" /usr/bin/env PYTHONPATH=/opt/cpu-swe-benchmark/src:/opt/mini-swe-agent/src MSWEA_SILENT_STARTUP=1 python3 -c 'from minisweagent.agents.default import DefaultAgent; from cpu_swe_benchmark.worker import run_worker; print("Full agent imports OK")'
truncate -s "${GUEST_DISK_SIZE:-3G}" "$OUT/rootfs.ext4"
sudo mkfs.ext4 -q -F -d "$STAGE" "$OUT/rootfs.ext4"
sudo chown "$(id -u):$(id -g)" "$OUT/rootfs.ext4"
docker image inspect "$IMAGE" --format '{{.Id}}' > "$OUT/source-image.txt"
sha256sum "$OUT/rootfs.ext4" > "$OUT/rootfs.sha256"
echo "Guest image: $OUT/rootfs.ext4 (staging tree retained at $STAGE)"
