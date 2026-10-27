#!/bin/bash
set -eu
mountpoint -q /proc || mount -t proc proc /proc
mountpoint -q /sys || mount -t sysfs sysfs /sys
mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /workspace /tmp
mount -t devpts devpts /dev/pts
export PATH=/opt/benchmark-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root PYTHONUNBUFFERED=1 MSWEA_SILENT_STARTUP=1
export PYTHONPATH=/opt/cpu-swe-benchmark/src:/opt/mini-swe-agent/src
ip link set lo up
exec /usr/bin/python3 /opt/cpu-swe/guest_service.py
