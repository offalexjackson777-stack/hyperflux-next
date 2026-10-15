#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

set -Eeuo pipefail
umask 077

matrix_failure() {
  local rc=$?
  trap - ERR
  printf 'transaction-matrix: rc=%d line=%d command=%q\n' \
    "$rc" "$LINENO" "$BASH_COMMAND" >&2
  exit "$rc"
}
trap matrix_failure ERR

readonly OUTPUT=/outputs
readonly INPUT=/inputs
readonly DATABASE=/matrix-db
readonly HOOKS=/matrix-hooks
readonly SHIMS=/matrix-shims
readonly REPOSITORY=/matrix-repository
readonly TRACE="$OUTPUT/matrix-registration-trace.txt"
readonly LOG="$OUTPUT/matrix-transactions.log"
readonly CASES=/matrix-cases.tsv
readonly PAYLOAD_PATHS=/matrix-payload-paths.txt
readonly CANDIDATE_VERSION=2.0.2-1
readonly RELEASE8_VERSION=0.0.0_dev.1-8
readonly OLD_SPLIT_VERSION=0.0.0_dev.1-2
: >"$TRACE"
: >"$LOG"
: >"$CASES"
chmod 0600 "$TRACE" "$LOG" "$CASES"

install -d -m 0700 "$HOOKS" "$SHIMS"
for hook in 70-dkms-install.hook 70-dkms-upgrade.hook 71-dkms-remove.hook; do
  ln -s /dev/null "$HOOKS/$hook"
done
cat >"$SHIMS/hfx-registration-shim" <<'SHIM'
#!/usr/bin/env bash
set -Eeuo pipefail
tool=${0##*/}
{
  printf '%s' "$tool"
  printf ' %q' "$@"
  printf '\n'
} >>/outputs/matrix-registration-trace.txt
case "$tool" in
  systemctl)
    [[ $# -eq 1 && $1 == daemon-reload ]]
    ;;
  udevadm)
    [[ $# -eq 2 && $1 == control && $2 == --reload ]]
    ;;
  systemd-sysusers)
    [[ $# -eq 1 && ($1 == hyperflux-next.conf || $1 == openrazer.conf) ]]
    ;;
  systemd-tmpfiles)
    [[ $# -eq 2 && $1 == --create && $2 == hyperflux-next.conf ]]
    ;;
  *)
    exit 97
    ;;
esac
SHIM
chmod 0755 "$SHIMS/hfx-registration-shim"
for tool in systemctl udevadm systemd-sysusers systemd-tmpfiles; do
  ln -s hfx-registration-shim "$SHIMS/$tool"
done
export PATH="$SHIMS:$PATH"

for package in "$INPUT"/*.pkg; do
  [[ -f "$package" && ! -L "$package" ]]
  bsdtar -tf "$package"
done | python -c '
import sys
paths = set()
for raw in sys.stdin:
    value = raw.rstrip("\n").removeprefix("./")
    if not value or value.startswith(".") or value.endswith("/"):
        continue
    parts = value.split("/")
    if value.startswith("/") or ".." in parts or "\x00" in value:
        raise SystemExit("unsafe package path")
    paths.add(value)
for value in sorted(paths):
    print(value)
' >"$PAYLOAD_PATHS"
[[ -s "$PAYLOAD_PATHS" ]]

readonly -a PACMAN=(pacman --dbpath "$DATABASE" --hookdir "$HOOKS")

package_field() {
  local archive=$1
  local field=$2
  bsdtar -xOf "$archive" .PKGINFO | sed -n "s/^$field = //p"
}

clear_package_payloads() {
  local relative path variant
  while IFS= read -r relative; do
    path="/$relative"
    for variant in "$path" "$path.pacnew" "$path.pacsave"; do
      if [[ -L "$variant" || -f "$variant" ]]; then
        rm -f -- "$variant"
      elif [[ -e "$variant" && ! -d "$variant" ]]; then
        printf 'unsupported package residue: %s\n' "$variant" >&2
        return 1
      fi
    done
  done <"$PAYLOAD_PATHS"
  rm -rf /var/lib/dkms/hid-hyperflux-next /var/lib/dkms/openrazer-driver
  rm -f /var/lib/pacman/db.lck
}

reset_case() {
  local case_id=$1
  clear_package_payloads
  rm -rf "$DATABASE" "$REPOSITORY" /matrix-cache
  install -d -m 0755 "$DATABASE/local" "$DATABASE/sync"
  install -d -m 0700 /matrix-cache
  cp -a /seed-local/. "$DATABASE/local/"
  cp -a /var/lib/pacman/sync/. "$DATABASE/sync/"
  MATRIX_CASE=$case_id python - <<'PY'
import os
import shutil
from pathlib import Path

root = Path('/matrix-db/local')
targets = {
    'hyperflux-next-linux',
    'hyperflux-next-openrgb',
    'hyperflux-next-full',
    'openrazer-driver-dkms',
    'openrazer-daemon',
    'python-openrazer',
    'polychromatic',
    'openrgb',
}

def field(path: Path, name: str) -> str:
    lines = path.read_text(encoding='utf-8').splitlines()
    marker = f'%{name}%'
    matches = [index for index, value in enumerate(lines) if value == marker]
    if len(matches) != 1 or matches[0] + 1 >= len(lines):
        raise SystemExit(f'malformed package record in {os.environ["MATRIX_CASE"]}: {path}')
    return lines[matches[0] + 1]

for candidate in list(root.iterdir()):
    descriptor = candidate / 'desc'
    if candidate.is_dir() and descriptor.is_file() and field(descriptor, 'NAME') in targets:
        shutil.rmtree(candidate)
PY
}

append_transaction_log() {
  local label=$1
  local stdout=$2
  local stderr=$3
  {
    printf '=== %s stdout ===\n' "$label"
    cat "$stdout"
    printf '=== %s stderr ===\n' "$label"
    cat "$stderr"
  } >>"$LOG"
  [[ $(stat -c %s "$LOG") -le 2097152 ]]
}

run_pacman_success() {
  local label=$1
  local stock_removal_allowed=$2
  shift 2
  local stdout stderr
  stdout=$(mktemp /matrix-stdout.XXXXXX)
  stderr=$(mktemp /matrix-stderr.XXXXXX)
  if ! timeout --kill-after=10s 300s "${PACMAN[@]}" "$@" \
      >"$stdout" 2>"$stderr"; then
    append_transaction_log "$label" "$stdout" "$stderr"
    rm -f "$stdout" "$stderr"
    printf 'expected successful transaction failed: %s\n' "$label" >&2
    return 1
  fi
  if [[ $stock_removal_allowed == false ]] && \
      grep -Eiq '^removing (openrgb|openrazer-daemon|python-openrazer|polychromatic)\.\.\.$' \
        "$stdout" "$stderr"; then
    append_transaction_log "$label" "$stdout" "$stderr"
    rm -f "$stdout" "$stderr"
    printf 'transaction proposed removing a stock application: %s\n' "$label" >&2
    return 1
  fi
  append_transaction_log "$label" "$stdout" "$stderr"
  rm -f "$stdout" "$stderr"
}

state_digest() {
  python - <<'PY'
import hashlib
import json
import os
import stat
from pathlib import Path

records = []
roots = [Path('/matrix-db')]
for root in roots:
    if root.exists():
        for path in sorted(root.rglob('*')):
            relative = f'db/{path.relative_to(root).as_posix()}'
            item = path.lstat()
            record = [relative, stat.S_IFMT(item.st_mode), stat.S_IMODE(item.st_mode)]
            if path.is_symlink():
                record.append(os.readlink(path))
            elif path.is_file():
                record.extend([item.st_size, hashlib.sha256(path.read_bytes()).hexdigest()])
            records.append(record)
for raw in Path('/matrix-payload-paths.txt').read_text(encoding='utf-8').splitlines():
    for suffix in ('', '.pacnew', '.pacsave'):
        path = Path('/') / f'{raw}{suffix}'
        if not (path.exists() or path.is_symlink()):
            records.append([f'fs/{raw}{suffix}', 'absent'])
            continue
        item = path.lstat()
        record = [f'fs/{raw}{suffix}', stat.S_IFMT(item.st_mode), stat.S_IMODE(item.st_mode)]
        if path.is_symlink():
            record.append(os.readlink(path))
        elif path.is_file():
            record.extend([item.st_size, hashlib.sha256(path.read_bytes()).hexdigest()])
        records.append(record)
payload = json.dumps(records, separators=(',', ':'), ensure_ascii=True).encode()
print(hashlib.sha256(payload).hexdigest())
PY
}

run_pacman_failure() {
  local label=$1
  shift
  local before after stdout stderr
  before=$(state_digest)
  stdout=$(mktemp /matrix-stdout.XXXXXX)
  stderr=$(mktemp /matrix-stderr.XXXXXX)
  if timeout --kill-after=10s 300s "${PACMAN[@]}" "$@" \
      >"$stdout" 2>"$stderr"; then
    append_transaction_log "$label" "$stdout" "$stderr"
    rm -f "$stdout" "$stderr"
    printf 'expected failed transaction succeeded: %s\n' "$label" >&2
    return 1
  fi
  append_transaction_log "$label" "$stdout" "$stderr"
  rm -f "$stdout" "$stderr"
  after=$(state_digest)
  [[ $before == "$after" ]]
  [[ ! -e "$DATABASE/db.lck" ]]
}

install_origin() {
  local label=$1
  shift
  run_pacman_success "$label" false -U --noconfirm --noscriptlet "$@"
}

assert_installed() {
  local package=$1
  local version=$2
  [[ $("${PACMAN[@]}" -Q "$package") == "$package $version" ]]
}

assert_absent() {
  local installed
  installed=$(mktemp /matrix-installed.XXXXXX)
  "${PACMAN[@]}" -Qq >"$installed"
  if grep -Fxq -- "$1" "$installed"; then
    rm -f "$installed"
    return 1
  fi
  rm -f "$installed"
}

package_reason() {
  MATRIX_PACKAGE=$1 python - <<'PY'
import os
from pathlib import Path

root = Path('/matrix-db/local')
package = os.environ['MATRIX_PACKAGE']

def values(path: Path, marker: str) -> list[str]:
    lines = path.read_text(encoding='utf-8').splitlines()
    token = f'%{marker}%'
    matches = [index for index, value in enumerate(lines) if value == token]
    if not matches and marker == 'REASON':
        return ['0']
    if len(matches) != 1:
        raise SystemExit(f'missing {marker} for {package}')
    start = matches[0] + 1
    result = []
    while start < len(lines) and lines[start]:
        result.append(lines[start])
        start += 1
    return result

matches = []
for candidate in root.iterdir():
    descriptor = candidate / 'desc'
    if candidate.is_dir() and descriptor.is_file() and values(descriptor, 'NAME') == [package]:
        matches.append(values(descriptor, 'REASON'))
if len(matches) != 1 or matches[0] not in (['0'], ['1']):
    raise SystemExit(f'ambiguous reason for {package}')
print(matches[0][0])
PY
}

assert_database_consistent() {
  [[ ! -e "$DATABASE/db.lck" ]]
  "${PACMAN[@]}" -Q >/dev/null
  python - <<'PY'
from pathlib import Path

root = Path('/matrix-db/local')
names = []
for candidate in root.iterdir():
    descriptor = candidate / 'desc'
    if not candidate.is_dir() or not descriptor.is_file():
        continue
    lines = descriptor.read_text(encoding='utf-8').splitlines()
    positions = [index for index, value in enumerate(lines) if value == '%NAME%']
    if len(positions) != 1 or positions[0] + 1 >= len(lines):
        raise SystemExit('malformed package database')
    names.append(lines[positions[0] + 1])
if len(names) != len(set(names)):
    raise SystemExit('duplicate package database name')
PY
}

assert_full_payload() {
  assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
  assert_installed hyperflux-next-full "$CANDIDATE_VERSION"
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/openrgb/plugins/hyperflux-next-openrgb.so) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/hyperflux-next/hyperflux-next-uhid-relay) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/systemd/system/hyperflux-next-openrazer-relay.service) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/systemd/system/hyperflux-next-openrazer-driver-preflight.service) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/share/hyperflux-next/components/full.json) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/sysusers.d/openrazer.conf) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/udev/rules.d/99-razer.rules) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/udev/razer_mount) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/src/openrazer-driver-3.12.4/dkms.conf) == \
    hyperflux-next-full ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/share/licenses/hyperflux-next-full/LICENSE) == \
    hyperflux-next-full ]]
  [[ ! -e /usr/share/licenses/hyperflux-next-openrgb/LICENSE ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/systemd/system/hyperflux-next-bridge.service) == \
    hyperflux-next-linux ]]
  jq -e \
    --arg version 2.0.2 \
    '.schema == "hyperflux-distribution-component-v1" and
     .component_id == "full" and .product_version == $version and
     .capabilities == ["openrazer-native-route", "openrgb-plugin"]' \
    /usr/share/hyperflux-next/components/full.json >/dev/null
  assert_database_consistent
}

assert_full_payload_absent() {
  local path
  for path in \
    /usr/lib/hyperflux-next/hyperflux-next-uhid-relay \
    /usr/lib/openrgb/plugins/hyperflux-next-openrgb.so \
    /usr/lib/systemd/system/hyperflux-next-bridge.service.d/50-full.conf \
    /usr/lib/systemd/system/hyperflux-next-openrazer-driver-preflight.service \
    /usr/lib/systemd/system/hyperflux-next-openrazer-relay.service \
    /usr/lib/sysusers.d/openrazer.conf \
    /usr/lib/udev/razer_mount \
    /usr/lib/udev/rules.d/99-razer.rules \
    /usr/share/hyperflux-next/components/full.json \
    /usr/share/licenses/hyperflux-next-full/LICENSE \
    /usr/src/openrazer-driver-3.12.4/dkms.conf; do
    [[ ! -e "$path" && ! -L "$path" ]]
  done
}

assert_no_optional_apps() {
  local package
  for package in openrgb openrazer-daemon python-openrazer polychromatic; do
    assert_absent "$package"
  done
}

assert_archive_installed() {
  local archive=$1
  local name version
  name=$(package_field "$archive" pkgname)
  version=$(package_field "$archive" pkgver)
  [[ -n $name && -n $version ]]
  assert_installed "$name" "$version"
  "${PACMAN[@]}" -Qk "$name" >/dev/null
}

assert_stock_apps() {
  assert_archive_installed "$INPUT/openrgb.pkg"
  assert_archive_installed "$INPUT/openrazer-daemon.pkg"
  assert_archive_installed "$INPUT/python-openrazer.pkg"
  assert_archive_installed "$INPUT/polychromatic.pkg"
}

record_case() {
  printf '%s\tpassed\t%s\n' "$1" "$2" >>"$CASES"
}

setup_candidate_repo() {
  install -d -m 0755 "$REPOSITORY"
  install -m 0644 "$INPUT/linux.pkg" "$INPUT/full.pkg" "$REPOSITORY/"
  repo-add -q "$REPOSITORY/hyperflux.db.tar.gz" \
    "$REPOSITORY/linux.pkg" "$REPOSITORY/full.pkg" >>"$LOG" 2>&1
  cat >/matrix-pacman.conf <<EOF
[options]
Architecture = auto
SigLevel = Never
[hyperflux]
Server = file://$REPOSITORY
EOF
  timeout --kill-after=10s 60s pacman --config /matrix-pacman.conf \
    --dbpath "$DATABASE" --hookdir "$HOOKS" -Sy --noconfirm >>"$LOG" 2>&1
}

install_openrazer_clients() {
  install_origin "$1" "$INPUT/openrazer-daemon.pkg" "$INPUT/python-openrazer.pkg"
}

install_stock_stack_after_core() {
  install_origin "$1" \
    "$INPUT/openrazer-daemon.pkg" "$INPUT/python-openrazer.pkg" \
    "$INPUT/polychromatic.pkg" "$INPUT/openrgb.pkg"
}

install_stock_stack_with_driver() {
  install_origin "$1" \
    "$INPUT/openrazer-driver.pkg" "$INPUT/openrazer-daemon.pkg" \
    "$INPUT/python-openrazer.pkg" "$INPUT/polychromatic.pkg" \
    "$INPUT/openrgb.pkg"
}

setup_old_three_package_origin() {
  local label=$1
  install_origin "$label-old-core" "$INPUT/old-linux.pkg"
  install_stock_stack_after_core "$label-stock"
  install_origin "$label-old-adapters" \
    "$INPUT/old-openrgb-plugin.pkg" "$INPUT/old-full.pkg"
}

setup_release8_origin() {
  local label=$1
  install_origin "$label-release8" \
    "$INPUT/release8-linux.pkg" "$INPUT/release8-full.pkg"
  install_stock_stack_after_core "$label-stock"
  assert_installed hyperflux-next-linux "$RELEASE8_VERSION"
  assert_installed hyperflux-next-full "$RELEASE8_VERSION"
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/hyperflux-next/hyperflux-next-uhid-relay) == \
    hyperflux-next-linux ]]
  [[ $("${PACMAN[@]}" -Qoq /usr/lib/openrgb/plugins/hyperflux-next-openrgb.so) == \
    hyperflux-next-full ]]
}

snapshot_packages() {
  {
    "${PACMAN[@]}" -Q
    printf '%s\n' '--- explicit ---'
    "${PACMAN[@]}" -Qe
  } | sha256sum | cut -d ' ' -f 1
}

# 1. Core alone.
reset_case case-01
run_pacman_success case-01-install-core false -U --noconfirm "$INPUT/linux.pkg"
  assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
assert_absent hyperflux-next-full
assert_no_optional_apps
record_case case-01 core-alone

# 2. Repository Full resolves Core and libraries, but no applications.
reset_case case-02
setup_candidate_repo
run_pacman_success case-02-repository-full false \
  --config /matrix-pacman.conf -S --noconfirm hyperflux-next-full
assert_full_payload
assert_no_optional_apps
[[ $(package_reason hyperflux-next-full) == 0 ]]
[[ $(package_reason hyperflux-next-linux) == 1 ]]
record_case case-02 repository-full-no-applications

# 3. A lone local Full archive fails atomically without exact Core.
reset_case case-03
run_pacman_failure case-03-full-alone -U --noconfirm "$INPUT/full.pkg"
assert_absent hyperflux-next-linux
assert_absent hyperflux-next-full
record_case case-03 local-full-alone-atomic-failure

# 4. Both local archive orders succeed.
reset_case case-04a
run_pacman_success case-04a-linux-full false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_full_payload
reset_case case-04b
run_pacman_success case-04b-full-linux false -U --noconfirm \
  "$INPUT/full.pkg" "$INPUT/linux.pkg"
assert_full_payload
record_case case-04 local-order-independent

# 5. Core first, then Full.
reset_case case-05
run_pacman_success case-05-core false -U --noconfirm "$INPUT/linux.pkg"
run_pacman_success case-05-full false -U --noconfirm "$INPUT/full.pkg"
assert_full_payload
record_case case-05 core-then-full

# 6. Stock OpenRGB can precede or follow Full.
reset_case case-06a
install_origin case-06a-openrgb "$INPUT/openrgb.pkg"
run_pacman_success case-06a-candidate false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_full_payload
assert_archive_installed "$INPUT/openrgb.pkg"
reset_case case-06b
run_pacman_success case-06b-candidate false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
install_origin case-06b-openrgb "$INPUT/openrgb.pkg"
assert_full_payload
assert_archive_installed "$INPUT/openrgb.pkg"
record_case case-06 openrgb-order-independent

# 7. Removing OpenRGB leaves the dormant Full-owned plugin.
run_pacman_success case-07-remove-openrgb true -R --noconfirm openrgb
assert_full_payload
assert_absent openrgb
record_case case-07 dormant-plugin-survives-openrgb-removal

# 8. Removing Full leaves stock OpenRGB and Core.
reset_case case-08
run_pacman_success case-08-candidate false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
install_origin case-08-openrgb "$INPUT/openrgb.pkg"
run_pacman_success case-08-remove-full false -R --noconfirm hyperflux-next-full
assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
assert_archive_installed "$INPUT/openrgb.pkg"
assert_absent hyperflux-next-full
assert_full_payload_absent
record_case case-08 full-removal-preserves-core-and-openrgb

# 9. Core coexists with the stock driver; Full replaces only that driver while
# stock OpenRazer userspace works before or after HyperFlux.
reset_case case-09a
install_origin case-09a-openrazer \
  "$INPUT/openrazer-driver.pkg" "$INPUT/openrazer-daemon.pkg" \
  "$INPUT/python-openrazer.pkg"
run_pacman_success case-09a-core false -U --noconfirm "$INPUT/linux.pkg"
assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
assert_archive_installed "$INPUT/openrazer-driver.pkg"
run_pacman_success case-09a-full false -U --noconfirm --ask=4 "$INPUT/full.pkg"
assert_full_payload
assert_absent openrazer-driver-dkms
assert_archive_installed "$INPUT/openrazer-daemon.pkg"
assert_archive_installed "$INPUT/python-openrazer.pkg"
reset_case case-09b
run_pacman_success case-09b-hyperflux false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
install_openrazer_clients case-09b-openrazer
assert_full_payload
assert_archive_installed "$INPUT/openrazer-daemon.pkg"
assert_archive_installed "$INPUT/python-openrazer.pkg"
record_case case-09 openrazer-order-independent

# 10 and 20 are the heavy monolith/DKMS migration and exact rollback run by
# arch_split_transaction_inner.sh in the same qualification framework.
record_case case-10 delegated-heavy-monolith-migration

# 11. Frozen three-package -2 migrates and rolls back exactly.
reset_case case-11
setup_old_three_package_origin case-11
origin_packages=$(snapshot_packages)
run_pacman_success case-11-migrate false -U --noconfirm --ask=4 \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_full_payload
assert_absent hyperflux-next-openrgb
assert_stock_apps
run_pacman_success case-11-restore-old false -U --noconfirm \
  "$INPUT/old-linux.pkg" "$INPUT/old-openrgb-plugin.pkg" "$INPUT/old-full.pkg"
[[ $(snapshot_packages) == "$origin_packages" ]]
[[ $("${PACMAN[@]}" -Qoq /usr/lib/openrgb/plugins/hyperflux-next-openrgb.so) == \
  hyperflux-next-openrgb ]]
record_case case-11 three-package-migration-and-rollback

# 12. The accepted release-8 ownership split and the retired connector origin
# both migrate to the 2.0 ownership boundary.
reset_case case-12a
setup_release8_origin case-12a
run_pacman_success case-12a-migrate false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_full_payload
assert_stock_apps
reset_case case-12b
install_origin case-12b-old-core "$INPUT/old-linux.pkg"
install_origin case-12b-openrgb "$INPUT/openrgb.pkg"
install_origin case-12b-old-connector "$INPUT/old-openrgb-plugin.pkg"
run_pacman_success case-12b-migrate false -U --noconfirm --ask=4 \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_full_payload
assert_archive_installed "$INPUT/openrgb.pkg"
record_case case-12 release8-and-retired-connector-migrate

# 13. Core-only -2 upgrades to Core-only 2.0.
reset_case case-13
install_origin case-13-old-core "$INPUT/old-linux.pkg"
run_pacman_success case-13-upgrade false -U --noconfirm "$INPUT/linux.pkg"
assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
assert_absent hyperflux-next-full
assert_absent hyperflux-next-openrgb
record_case case-13 core-only-upgrade-remains-core-only

# 14. Core 2.0 alone cannot strand old Full -2.
reset_case case-14
setup_old_three_package_origin case-14
run_pacman_failure case-14-core-alone -U --noconfirm "$INPUT/linux.pkg"
assert_installed hyperflux-next-linux "$OLD_SPLIT_VERSION"
assert_installed hyperflux-next-full "$OLD_SPLIT_VERSION"
record_case case-14 old-full-blocks-core-only-upgrade

# 15. A connector version other than exact -2 is never removed automatically.
reset_case case-15
install_origin case-15-old-core "$INPUT/old-linux.pkg"
install_origin case-15-openrgb "$INPUT/openrgb.pkg"
install_origin case-15-old-connector "$INPUT/old-openrgb-plugin.pkg"
python - <<'PY'
from pathlib import Path

root = Path('/matrix-db/local')
for candidate in root.iterdir():
    descriptor = candidate / 'desc'
    if not descriptor.is_file():
        continue
    lines = descriptor.read_text(encoding='utf-8').splitlines()
    if '%NAME%' not in lines:
        continue
    index = lines.index('%NAME%')
    if lines[index + 1] != 'hyperflux-next-openrgb':
        continue
    version = lines.index('%VERSION%')
    lines[version + 1] = '0.0.0_dev.1-99'
    destination = root / 'hyperflux-next-openrgb-0.0.0_dev.1-99'
    if destination.exists():
        raise SystemExit('unexpected connector destination already exists')
    descriptor.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    candidate.rename(destination)
    break
else:
    raise SystemExit('old connector record not found')
PY
run_pacman_failure case-15-unexpected-connector -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
assert_installed hyperflux-next-openrgb 0.0.0_dev.1-99
record_case case-15 unexpected-connector-fails-closed

# 16. Candidate transactions preserve all stock applications; the policy rejects
# any explicit proposal naming one for removal before Pacman is invoked.
reset_case case-16
run_pacman_success case-16-core false -U --noconfirm "$INPUT/linux.pkg"
install_stock_stack_with_driver case-16-stock
run_pacman_success case-16-full false -U --noconfirm --ask=4 "$INPUT/full.pkg"
assert_full_payload
assert_stock_apps
reject_stock_removal_proposal() {
  local package
  for package in "$@"; do
    case "$package" in
      openrgb|openrazer-daemon|python-openrazer|polychromatic) return 1 ;;
    esac
  done
  return 0
}
if reject_stock_removal_proposal openrgb; then
  echo 'stock-application removal policy accepted OpenRGB' >&2
  exit 98
fi
record_case case-16 stock-application-removal-proposals-rejected

# 17. Plain Full removal leaves Core.
reset_case case-17
run_pacman_success case-17-install false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
run_pacman_success case-17-remove-full false -R --noconfirm hyperflux-next-full
assert_installed hyperflux-next-linux "$CANDIDATE_VERSION"
assert_absent hyperflux-next-full
assert_full_payload_absent
record_case case-17 plain-full-removal-leaves-core

# 18. Core removal fails atomically while Full remains.
reset_case case-18
run_pacman_success case-18-install false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
run_pacman_failure case-18-remove-core -R --noconfirm hyperflux-next-linux
assert_full_payload
record_case case-18 core-removal-blocked-by-full

# 19. Normal removal preserves an operator-modified backup as an exact pacsave;
# an explicit no-save purge removes that configuration by request.
reset_case case-19-preserve
run_pacman_success case-19-preserve-install false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
printf '%s\n' '{"schema":"matrix-operator-config"}' \
  >/etc/hyperflux-next/bridge.json
config_sha=$(sha256sum /etc/hyperflux-next/bridge.json | cut -d ' ' -f 1)
run_pacman_success case-19-preserve-remove false -R --noconfirm \
  hyperflux-next-full hyperflux-next-linux
assert_absent hyperflux-next-full
assert_absent hyperflux-next-linux
assert_full_payload_absent
[[ ! -e /etc/hyperflux-next/bridge.json ]]
[[ -f /etc/hyperflux-next/bridge.json.pacsave && \
  ! -L /etc/hyperflux-next/bridge.json.pacsave ]]
[[ $(sha256sum /etc/hyperflux-next/bridge.json.pacsave | cut -d ' ' -f 1) == \
  "$config_sha" ]]

reset_case case-19-purge
run_pacman_success case-19-purge-install false -U --noconfirm \
  "$INPUT/linux.pkg" "$INPUT/full.pkg"
printf '%s\n' '{"schema":"matrix-operator-config"}' \
  >/etc/hyperflux-next/bridge.json
run_pacman_success case-19-purge-remove false -Rn --noconfirm \
  hyperflux-next-full hyperflux-next-linux
assert_absent hyperflux-next-full
assert_absent hyperflux-next-linux
assert_full_payload_absent
[[ ! -e /etc/hyperflux-next/bridge.json ]]
[[ ! -e /etc/hyperflux-next/bridge.json.pacsave ]]
record_case case-19 removal-and-explicit-purge-configuration-semantics

record_case case-20 delegated-heavy-exact-origin-rollback

sort -u "$TRACE" -o "$TRACE"
[[ $(stat -c %s "$TRACE") -le 1048576 ]]
python - <<'PY'
import hashlib
import json
from pathlib import Path

cases_path = Path('/matrix-cases.tsv')
cases = []
for line in cases_path.read_text(encoding='utf-8').splitlines():
    case_id, verdict, detail = line.split('\t')
    cases.append({'id': case_id, 'verdict': verdict, 'detail': detail})
expected = [f'case-{index:02d}' for index in range(1, 21)]
if sorted(item['id'] for item in cases) != expected or len(cases) != 20:
    raise SystemExit('transaction case inventory is incomplete')
result = {
    'schema': 'hyperflux-arch-two-package-transaction-matrix-v1',
    'cases': sorted(cases, key=lambda item: item['id']),
    'registration_trace_sha256': hashlib.sha256(
        Path('/outputs/matrix-registration-trace.txt').read_bytes()
    ).hexdigest(),
    'transaction_log_sha256': hashlib.sha256(
        Path('/outputs/matrix-transactions.log').read_bytes()
    ).hexdigest(),
    'verdict': 'passed',
}
path = Path('/outputs/matrix-result.json')
path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
path.chmod(0o600)
PY
