#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

set -Eeuo pipefail
umask 077

transaction_failure() {
  local rc=$?
  trap - ERR
  printf 'transaction-inner: rc=%d line=%d command=%q\n' \
    "$rc" "$LINENO" "$BASH_COMMAND" >&2
  exit "$rc"
}
trap transaction_failure ERR

readonly OUTPUT=/outputs
readonly INPUT=/inputs
readonly DATABASE=/transaction-db
readonly TRACE="$OUTPUT/registration-trace.txt"
readonly UPGRADE_STDOUT="$OUTPUT/upgrade.stdout"
readonly UPGRADE_STDERR="$OUTPUT/upgrade.stderr"
readonly ROLLBACK_STDOUT="$OUTPUT/rollback.stdout"
readonly ROLLBACK_STDERR="$OUTPUT/rollback.stderr"
readonly BASELINE_HFX_DKMS_VERSION=0.0.0_dev.1
readonly CANDIDATE_HFX_DKMS_VERSION=2.1.2
readonly CANDIDATE_PACKAGE_VERSION=2.1.2-1

capture_dkms_log() {
  local module=$1
  local version=$2
  local destination=$3
  local source="/var/lib/dkms/$module/$version/build/make.log"
  if [[ -f "$source" && ! -L "$source" ]]; then
    [[ $(stat -c %s "$source") -gt 0 && $(stat -c %s "$source") -le 1048576 ]]
    install -m 0600 "$source" "$destination"
  else
    printf 'make_log=unavailable-after-dkms-install\n' >"$destination"
    chmod 0600 "$destination"
  fi
}

capture_module_hashes() {
  local destination=$1
  local module
  local -a matches=()
  : >"$destination"
  for module in \
    hid-hyperflux-next razeraccessory razerkbd razerkraken razermouse; do
    mapfile -t matches < <(
      find "$FIXTURE_KERNEL_ROOT/updates/dkms" -maxdepth 1 -type f \
        -name "$module.ko*" -print | sort
    )
    [[ ${#matches[@]} -eq 1 ]]
    printf '%s  %s\n' "$(sha256sum "${matches[0]}" | cut -d ' ' -f 1)" \
      "${matches[0]##*/}" >>"$destination"
  done
  chmod 0600 "$destination"
}

assert_both_dkms_modules_installed() {
  local status=$1
  local hfx_version=$2
  grep -F "hid-hyperflux-next/$hfx_version, $FIXTURE_KERNEL_VERSION, x86_64: installed" \
    "$status" >/dev/null
  grep -F "openrazer-driver/3.12.4, $FIXTURE_KERNEL_VERSION, x86_64: installed" \
    "$status" >/dev/null
}

mkdir -p /hfx-shims "$DATABASE/local" "$DATABASE/sync"
: >"$TRACE"
chmod 0600 "$TRACE"

mapfile -t fixture_kernels < <(
  find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -print0 \
    | while IFS= read -r -d '' candidate; do
        [[ -d "$candidate/build/include" ]] && printf '%s\n' "$candidate"
      done \
    | sort
)
[[ ${#fixture_kernels[@]} -eq 1 ]]
readonly FIXTURE_KERNEL_ROOT=${fixture_kernels[0]}
readonly FIXTURE_KERNEL_VERSION=${FIXTURE_KERNEL_ROOT##*/}
readonly KERNEL_MAKEFILE="$FIXTURE_KERNEL_ROOT/build/Makefile"
readonly BASELINE_DKMS=/baseline-dkms
readonly ROLLBACK_HOOKS=/rollback-hooks
fixture_kernel_tree=existing
if [[ ! -d "$FIXTURE_KERNEL_ROOT/kernel" ]]; then
  mkdir -m 0755 "$FIXTURE_KERNEL_ROOT/kernel"
  fixture_kernel_tree=synthetic-empty
fi
printf 'kernel_version=%s\nbuild_headers=existing\nmodule_tree=%s\n' \
  "$FIXTURE_KERNEL_VERSION" "$fixture_kernel_tree" \
  >"$OUTPUT/dkms-kernel-fixture.txt"
chmod 0600 "$OUTPUT/dkms-kernel-fixture.txt"

python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

source = Path('/seed-local')
entries = []
total_bytes = 0
for path in sorted(source.rglob('*')):
    relative = path.relative_to(source).as_posix()
    stat = path.lstat()
    if path.is_symlink() or not (path.is_dir() or path.is_file()):
        raise SystemExit(f'unsupported package database entry: {relative}')
    if path.is_file():
        if stat.st_size > 8 * 1024 * 1024:
            raise SystemExit(f'oversized package database entry: {relative}')
        total_bytes += stat.st_size
        if total_bytes > 128 * 1024 * 1024:
            raise SystemExit('package database seed exceeds its aggregate bound')
        entries.append(
            {
                'path': relative,
                'size': stat.st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
if not entries or len(entries) > 8192:
    raise SystemExit('package database inventory is empty or oversized')

def package_field(path, field):
    lines = path.read_text(encoding='utf-8').splitlines()
    marker = f'%{field}%'
    matches = [index for index, value in enumerate(lines) if value == marker]
    if len(matches) != 1 or matches[0] + 1 >= len(lines):
        raise SystemExit(f'malformed package database descriptor: {path.parent.name}')
    return lines[matches[0] + 1]

targets = {
    'hyperflux-next-linux',
    'hyperflux-next-full',
    'hyperflux-next-openrgb',
    'openrazer-driver-dkms',
    'openrazer-daemon',
    'python-openrazer',
    'polychromatic',
    'openrgb',
}
removed = []
for candidate in sorted(source.iterdir()):
    descriptor = candidate / 'desc'
    if candidate.is_dir() and descriptor.is_file():
        name = package_field(descriptor, 'NAME')
        if name in targets:
            removed.append((name, package_field(descriptor, 'VERSION')))
if len({name for name, _version in removed}) != len(removed):
    raise SystemExit('package database seed contains ambiguous target records')
polychromatic = [version for name, version in removed if name == 'polychromatic']
if polychromatic and polychromatic != ['0.9.7-1']:
    raise SystemExit('package database seed contains an unexpected Polychromatic version')
payload = json.dumps(entries, separators=(',', ':'), sort_keys=True).encode()
result = {
    'schema': 'hyperflux-read-only-package-db-seed-v1',
    'bytes': total_bytes,
    'files': len(entries),
    'inventory_sha256': hashlib.sha256(payload).hexdigest(),
    'private_copy_normalization': {
        'removed_package_records': [f'{name} {version}' for name, version in removed],
    },
}
target = Path('/outputs/seed-package-db.json')
target.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
target.chmod(0o600)
PY

cp -a /seed-local/. "$DATABASE/local/"
cp -a /var/lib/pacman/sync/. "$DATABASE/sync/"
python - <<'PY'
import shutil
from pathlib import Path

root = Path('/transaction-db/local')

def package_field(path, field):
    lines = path.read_text(encoding='utf-8').splitlines()
    marker = f'%{field}%'
    matches = [index for index, value in enumerate(lines) if value == marker]
    if len(matches) != 1 or matches[0] + 1 >= len(lines):
        raise SystemExit(f'malformed private package descriptor: {path.parent.name}')
    return lines[matches[0] + 1]

targets = {
    'hyperflux-next-linux',
    'hyperflux-next-full',
    'hyperflux-next-openrgb',
    'openrazer-driver-dkms',
    'openrazer-daemon',
    'python-openrazer',
    'polychromatic',
    'openrgb',
}
for candidate in sorted(root.iterdir()):
    descriptor = candidate / 'desc'
    if candidate.is_dir() and descriptor.is_file():
        if package_field(descriptor, 'NAME') in targets:
            shutil.rmtree(candidate)
PY
readonly -a PACMAN=(pacman --dbpath "$DATABASE")

install -d -m 0700 /seed-hooks
for hook in 70-dkms-install.hook 70-dkms-upgrade.hook 71-dkms-remove.hook; do
  ln -s /dev/null "/seed-hooks/$hook"
done
timeout --kill-after=10s 300s "${PACMAN[@]}" --hookdir /seed-hooks \
  -U --asdeps --noconfirm --noscriptlet \
  "$INPUT/openrazer-daemon.pkg" \
  "$INPUT/python-openrazer.pkg" \
  "$INPUT/old.pkg" >/dev/null
"${PACMAN[@]}" -D --asexplicit hyperflux-next-linux >/dev/null

[[ $("${PACMAN[@]}" -Q hyperflux-next-linux) == \
  'hyperflux-next-linux 0.0.0_dev.1-1' ]]
if "${PACMAN[@]}" -Qq | grep -Fx polychromatic >/dev/null; then
  echo 'seed package database unexpectedly contains Polychromatic' >&2
  exit 94
fi
"${PACMAN[@]}" -Q >"$OUTPUT/packages-before-upgrade.txt"

grep -Fx 'pkgname = hyperflux-next-linux' \
  <(bsdtar -xOf "$INPUT/old.pkg" .PKGINFO) >/dev/null
grep -Fx 'pkgver = 0.0.0_dev.1-1' \
  <(bsdtar -xOf "$INPUT/old.pkg" .PKGINFO) >/dev/null

[[ -f "$KERNEL_MAKEFILE" && ! -L "$KERNEL_MAKEFILE" ]]
install -d -m 0700 "$BASELINE_DKMS" "$BASELINE_DKMS/var-lib-dkms" \
  "$BASELINE_DKMS/modules"
install -m 0600 /usr/src/hid-hyperflux-next-0.0.0_dev.1/dkms.conf \
  "$BASELINE_DKMS/hid-dkms.conf"
sha256sum "$KERNEL_MAKEFILE" >"$OUTPUT/kernel-makefile-before-upgrade.sha256"
chmod 0600 "$OUTPUT/kernel-makefile-before-upgrade.sha256"

# Seed the disposable root with the exact pre-upgrade installed module state.
# The legacy core recipe is replaced only while creating this fixture because it
# is incompatible with the fixture kernel's Kbuild MO behavior. The package-owned
# source is restored byte-for-byte before the transaction begins.
dkms add -m openrazer-driver -v 3.12.4
dkms build -m openrazer-driver -v 3.12.4 -k "$FIXTURE_KERNEL_VERSION"
dkms install -m openrazer-driver -v 3.12.4 -k "$FIXTURE_KERNEL_VERSION"
cat >/usr/src/hid-hyperflux-next-0.0.0_dev.1/dkms.conf <<'DKMS'
PACKAGE_NAME="hid-hyperflux-next"
PACKAGE_VERSION="0.0.0_dev.1"
BUILT_MODULE_NAME[0]="hid-hyperflux-next"
BUILT_MODULE_LOCATION[0]="."
DEST_MODULE_LOCATION[0]="/updates/dkms"
MAKE[0]="make -C /lib/modules/${kernelver}/build M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
AUTOINSTALL="yes"
DKMS
dkms add -m hid-hyperflux-next -v 0.0.0_dev.1
dkms build -m hid-hyperflux-next -v 0.0.0_dev.1 -k "$FIXTURE_KERNEL_VERSION"
dkms install -m hid-hyperflux-next -v 0.0.0_dev.1 -k "$FIXTURE_KERNEL_VERSION"
install -m 0644 "$BASELINE_DKMS/hid-dkms.conf" \
  /usr/src/hid-hyperflux-next-0.0.0_dev.1/dkms.conf
cmp "$BASELINE_DKMS/hid-dkms.conf" \
  /usr/src/hid-hyperflux-next-0.0.0_dev.1/dkms.conf
sha256sum -c "$OUTPUT/kernel-makefile-before-upgrade.sha256"

dkms status >"$OUTPUT/dkms-before-upgrade.txt"
assert_both_dkms_modules_installed \
  "$OUTPUT/dkms-before-upgrade.txt" "$BASELINE_HFX_DKMS_VERSION"
capture_dkms_log hid-hyperflux-next 0.0.0_dev.1 \
  "$OUTPUT/dkms-build-hid-before-upgrade.log"
capture_dkms_log openrazer-driver 3.12.4 \
  "$OUTPUT/dkms-build-openrazer-before-upgrade.log"
capture_module_hashes "$OUTPUT/dkms-module-hashes-before-upgrade.txt"
cp -a /var/lib/dkms/hid-hyperflux-next "$BASELINE_DKMS/var-lib-dkms/"
cp -a /var/lib/dkms/openrazer-driver "$BASELINE_DKMS/var-lib-dkms/"
while IFS= read -r module; do
  cp -a "$FIXTURE_KERNEL_ROOT/updates/dkms/$module" "$BASELINE_DKMS/modules/"
done < <(cut -d ' ' -f 3 "$OUTPUT/dkms-module-hashes-before-upgrade.txt")

cat >/hfx-shims/hfx-registration-shim <<'SHIM'
#!/usr/bin/env bash
set -Eeuo pipefail
tool=${0##*/}
{
  printf '%s' "$tool"
  printf ' %q' "$@"
  printf '\n'
} >>/outputs/registration-trace.txt
case "$tool" in
  systemctl)
    [[ $# -eq 1 && $1 == daemon-reload ]] || exit 97
    ;;
  udevadm)
    [[ $# -eq 2 && $1 == control && $2 == --reload ]] || exit 98
    ;;
  systemd-sysusers)
    [[ $# -eq 1 && ($1 == hyperflux-next.conf || $1 == openrazer.conf) ]] || exit 99
    exec /usr/bin/systemd-sysusers "$@"
    ;;
  systemd-tmpfiles)
    [[ $# -eq 2 && $1 == --create && $2 == hyperflux-next.conf ]] || exit 100
    exec /usr/bin/systemd-tmpfiles "$@"
    ;;
  *)
    exit 101
    ;;
esac
SHIM
chmod 0755 /hfx-shims/hfx-registration-shim
for tool in systemctl udevadm systemd-sysusers systemd-tmpfiles; do
  ln -s hfx-registration-shim "/hfx-shims/$tool"
done
export PATH="/hfx-shims:$PATH"
export MAKEFLAGS=-j2

[[ $("${PACMAN[@]}" -Qoq /usr/lib/python3.14/site-packages/polychromatic/common.py) == \
  hyperflux-next-linux ]]

python - <<'PY'
import json
from pathlib import Path

path = Path('/etc/hyperflux-next/bridge.json')
value = json.loads(path.read_text())
value['restoration']['enabled'] = True
path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
PY
sha256sum /etc/hyperflux-next/bridge.json >"$OUTPUT/config-before-upgrade.sha256"

timeout --kill-after=10s 900s "${PACMAN[@]}" -U --noconfirm \
  "$INPUT/openrazer-daemon.pkg" \
  "$INPUT/python-openrazer.pkg" \
  "$INPUT/polychromatic.pkg" \
  "$INPUT/openrgb.pkg" \
  "$INPUT/linux.pkg" \
  "$INPUT/full.pkg" \
  >"$UPGRADE_STDOUT" 2>"$UPGRADE_STDERR"

[[ $("${PACMAN[@]}" -Q hyperflux-next-linux) == \
  "hyperflux-next-linux $CANDIDATE_PACKAGE_VERSION" ]]
[[ $("${PACMAN[@]}" -Q hyperflux-next-full) == \
  "hyperflux-next-full $CANDIDATE_PACKAGE_VERSION" ]]
if "${PACMAN[@]}" -Qq hyperflux-next-openrgb >/dev/null 2>&1; then
  echo 'retired OpenRGB connector survived the 2.0 migration' >&2
  exit 105
fi
[[ $("${PACMAN[@]}" -Q polychromatic) == 'polychromatic 0.9.7-1' ]]
"${PACMAN[@]}" -Q openrazer-daemon python-openrazer openrgb \
  >"$OUTPUT/stock-package-versions.txt"

[[ $("${PACMAN[@]}" -Qoq /usr/lib/python3.14/site-packages/polychromatic/common.py) == \
  polychromatic ]]
[[ $("${PACMAN[@]}" -Qoq /usr/lib/openrgb/plugins/hyperflux-next-openrgb.so) == \
  hyperflux-next-full ]]
[[ ! -e /usr/share/licenses/hyperflux-next-openrgb/LICENSE ]]
[[ $("${PACMAN[@]}" -Qoq /usr/lib/sysusers.d/openrazer.conf) == \
  hyperflux-next-full ]]
[[ $("${PACMAN[@]}" -Qoq /usr/lib/hyperflux-next/hyperflux-next-uhid-relay) == \
  hyperflux-next-full ]]
[[ $("${PACMAN[@]}" -Qoq /usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf) == \
  hyperflux-next-full ]]
[[ $("${PACMAN[@]}" -Qoq /usr/share/hyperflux-next/components/full.json) == \
  hyperflux-next-full ]]

for obsolete in \
  /usr/bin/hyperflux-openrazer-capabilities \
  /usr/bin/hyperflux-openrazer-compat \
  /usr/bin/hyperflux-openrazer-session \
  /usr/lib/systemd/system/hyperflux-next-activation.service \
  /usr/lib/systemd/system/hyperflux-next-confirmation.service \
  /usr/lib/systemd/system/hyperflux-next-openrazer-session@.service \
  /usr/lib/systemd/system/hyperflux-next-openrazer-session@.path \
  /usr/lib/systemd/user/hyperflux-next-openrazer-session.service \
  /usr/lib/systemd/user/hyperflux-next-openrazer-session.path; do
  [[ ! -e "$obsolete" && ! -L "$obsolete" ]]
done

python - <<'PY'
import json
from pathlib import Path
import subprocess

def package_config(package: str) -> bytes:
    result = subprocess.run(
        [
            'bsdtar',
            '-xOf',
            package,
            'etc/hyperflux-next/bridge.json',
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not result.stdout or len(result.stdout) > 65536:
        raise SystemExit(f'invalid packaged bridge configuration: {package}')
    return result.stdout

active = json.loads(Path('/etc/hyperflux-next/bridge.json').read_text())
old_default_bytes = package_config('/inputs/old.pkg')
candidate_default_bytes = package_config('/inputs/linux.pkg')
candidate_default = json.loads(candidate_default_bytes)
if active['restoration']['enabled'] is not True:
    raise SystemExit('modified active configuration was not preserved')
if old_default_bytes != candidate_default_bytes:
    raise SystemExit('legacy and candidate packaged defaults unexpectedly differ')
if Path('/etc/hyperflux-next/bridge.json.pacnew').exists():
    raise SystemExit('unchanged packaged default unexpectedly emitted as .pacnew')
if candidate_default['restoration']['enabled'] is not False:
    raise SystemExit('packaged safe default enables restoration')
if candidate_default['mode'] != 'qualified-live':
    raise SystemExit('packaged operational mode is not qualified-live')
PY

sort -u "$TRACE" >"$OUTPUT/registration-trace.unique.txt"
if grep -Eq ' (start|stop|restart|enable|disable|mask|unmask|trigger|settle)( |$)' "$TRACE"; then
  echo 'unsafe package lifecycle command observed' >&2
  exit 102
fi
if grep -Eiq '(remove|removing)[^[:cntrl:]]*polychromatic|polychromatic[^[:cntrl:]]*conflict' \
  "$UPGRADE_STDOUT" "$UPGRADE_STDERR"; then
  echo 'transaction attempted to remove or conflict with stock Polychromatic' >&2
  exit 104
fi
for expected in \
  'systemd-sysusers hyperflux-next.conf' \
  'systemd-sysusers openrazer.conf' \
  'systemd-tmpfiles --create hyperflux-next.conf' \
  'systemctl daemon-reload' \
  'udevadm control --reload'; do
  grep -Fx "$expected" "$TRACE" >/dev/null
done

dkms status >"$OUTPUT/dkms-after-upgrade.txt"
capture_dkms_log hid-hyperflux-next "$CANDIDATE_HFX_DKMS_VERSION" \
  "$OUTPUT/dkms-build-hid-after-upgrade.log"
capture_dkms_log openrazer-driver 3.12.4 \
  "$OUTPUT/dkms-build-openrazer-after-upgrade.log"
assert_both_dkms_modules_installed \
  "$OUTPUT/dkms-after-upgrade.txt" "$CANDIDATE_HFX_DKMS_VERSION"
sha256sum "$KERNEL_MAKEFILE" >"$OUTPUT/kernel-makefile-after-upgrade.sha256"
chmod 0600 "$OUTPUT/kernel-makefile-after-upgrade.sha256"
cmp "$OUTPUT/kernel-makefile-before-upgrade.sha256" \
  "$OUTPUT/kernel-makefile-after-upgrade.sha256"

"${PACMAN[@]}" -Q >"$OUTPUT/packages-after-upgrade.txt"
find /usr/src -maxdepth 2 -type f -name dkms.conf -print | sort \
  >"$OUTPUT/dkms-sources-after-upgrade.txt"
grep -Fx '/usr/src/hid-hyperflux-next-2.1.2/dkms.conf' \
  "$OUTPUT/dkms-sources-after-upgrade.txt" >/dev/null
grep -Fx '/usr/src/openrazer-driver-3.12.4/dkms.conf' \
  "$OUTPUT/dkms-sources-after-upgrade.txt" >/dev/null

install -d -m 0700 "$ROLLBACK_HOOKS"
for hook in 70-dkms-install.hook 70-dkms-upgrade.hook 71-dkms-remove.hook; do
  ln -s /dev/null "$ROLLBACK_HOOKS/$hook"
done
# The known rollback package predates the safe core DKMS recipe. Preserve the
# exact pre-upgrade DKMS/module bytes instead of executing that legacy hook.
{
  dkms remove -m hid-hyperflux-next -v "$CANDIDATE_HFX_DKMS_VERSION" --all
  dkms remove -m openrazer-driver -v 3.12.4 --all
  timeout --kill-after=10s 300s "${PACMAN[@]}" --hookdir "$ROLLBACK_HOOKS" \
    -R --noconfirm hyperflux-next-full polychromatic \
    openrazer-daemon python-openrazer
  timeout --kill-after=10s 300s "${PACMAN[@]}" --hookdir "$ROLLBACK_HOOKS" \
    -U --asdeps --noconfirm --noscriptlet \
    "$INPUT/openrazer-daemon.pkg" \
    "$INPUT/python-openrazer.pkg" \
    "$INPUT/old.pkg"
  "${PACMAN[@]}" -D --asexplicit hyperflux-next-linux
} >>"$ROLLBACK_STDOUT" 2>>"$ROLLBACK_STDERR"

[[ ! -e /var/lib/dkms/hid-hyperflux-next ]]
[[ ! -e /var/lib/dkms/openrazer-driver ]]
cp -a "$BASELINE_DKMS/var-lib-dkms/hid-hyperflux-next" /var/lib/dkms/
cp -a "$BASELINE_DKMS/var-lib-dkms/openrazer-driver" /var/lib/dkms/
install -d -m 0755 "$FIXTURE_KERNEL_ROOT/updates/dkms"
while IFS= read -r module; do
  install -m 0644 "$BASELINE_DKMS/modules/$module" \
    "$FIXTURE_KERNEL_ROOT/updates/dkms/$module"
done < <(cut -d ' ' -f 3 "$OUTPUT/dkms-module-hashes-before-upgrade.txt")
depmod -a "$FIXTURE_KERNEL_VERSION"

[[ $("${PACMAN[@]}" -Q hyperflux-next-linux) == 'hyperflux-next-linux 0.0.0_dev.1-1' ]]
[[ $("${PACMAN[@]}" -Q openrazer-daemon) == 'openrazer-daemon 3.12.4-0.1' ]]
[[ $("${PACMAN[@]}" -Q python-openrazer) == 'python-openrazer 3.12.4-0.1' ]]
[[ $("${PACMAN[@]}" -Qe hyperflux-next-linux) == \
  'hyperflux-next-linux 0.0.0_dev.1-1' ]]
[[ $("${PACMAN[@]}" -Qd openrazer-daemon) == \
  'openrazer-daemon 3.12.4-0.1' ]]
[[ $("${PACMAN[@]}" -Qd python-openrazer) == \
  'python-openrazer 3.12.4-0.1' ]]
if "${PACMAN[@]}" -Qq | grep -Fx polychromatic >/dev/null; then
  echo 'stock Polychromatic unexpectedly survived the old monolith rollback' >&2
  exit 103
fi
[[ $("${PACMAN[@]}" -Qoq /usr/lib/python3.14/site-packages/polychromatic/common.py) == \
  hyperflux-next-linux ]]
dkms status >"$OUTPUT/dkms-after-rollback.txt"
capture_dkms_log hid-hyperflux-next 0.0.0_dev.1 \
  "$OUTPUT/dkms-build-hid-after-rollback.log"
capture_dkms_log openrazer-driver 3.12.4 \
  "$OUTPUT/dkms-build-openrazer-after-rollback.log"
assert_both_dkms_modules_installed \
  "$OUTPUT/dkms-after-rollback.txt" "$BASELINE_HFX_DKMS_VERSION"
capture_module_hashes "$OUTPUT/dkms-module-hashes-after-rollback.txt"
cmp "$OUTPUT/dkms-module-hashes-before-upgrade.txt" \
  "$OUTPUT/dkms-module-hashes-after-rollback.txt"
cmp "$BASELINE_DKMS/hid-dkms.conf" \
  /usr/src/hid-hyperflux-next-0.0.0_dev.1/dkms.conf
sha256sum "$KERNEL_MAKEFILE" >"$OUTPUT/kernel-makefile-after-rollback.sha256"
chmod 0600 "$OUTPUT/kernel-makefile-after-rollback.sha256"
cmp "$OUTPUT/kernel-makefile-before-upgrade.sha256" \
  "$OUTPUT/kernel-makefile-after-rollback.sha256"

for output in \
  "$TRACE" "$UPGRADE_STDOUT" "$UPGRADE_STDERR" \
  "$ROLLBACK_STDOUT" "$ROLLBACK_STDERR"; do
  [[ $(stat -c %s "$output") -le 1048576 ]]
done

python - <<'PY'
import hashlib
import json
from pathlib import Path

output = Path('/outputs')

def digest(name):
    return hashlib.sha256((output / name).read_bytes()).hexdigest()

result = {
    'schema': 'hyperflux-arch-split-transaction-v1',
    'upgrade': {
        'resolved': True,
        'seeded_from_read_only_package_database': True,
        'stock_polychromatic_installed': True,
        'stock_polychromatic_owns_takeover_paths': True,
        'obsolete_private_runtime_absent': True,
        'dkms_modules_installed': ['hid-hyperflux-next/2.1.2', 'openrazer-driver/3.12.4'],
        'active_restoration_enabled_preserved': True,
        'safe_disabled_default_preserved_in_package': True,
        'unchanged_package_default_requires_no_pacnew': True,
        'kernel_header_tree_preserved': True,
    },
    'hook': {
        'registration_only': True,
        'trace_sha256': digest('registration-trace.txt'),
    },
    'rollback': {
        'resolved': True,
        'old_monolith_restored': True,
        'old_scriptlet_suppressed': True,
        'legacy_dkms_hooks_suppressed': True,
        'pretransaction_dkms_state_restored': True,
        'module_bytes_restored': True,
        'kernel_header_tree_preserved': True,
        'dkms_modules_installed': ['hid-hyperflux-next/0.0.0_dev.1', 'openrazer-driver/3.12.4'],
    },
    'logs': {
        name: {'sha256': digest(name), 'size': (output / name).stat().st_size}
        for name in ('upgrade.stdout', 'upgrade.stderr', 'rollback.stdout', 'rollback.stderr')
    },
    'verdict': 'passed',
}
path = output / 'inner-result.json'
path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
path.chmod(0o600)
PY
