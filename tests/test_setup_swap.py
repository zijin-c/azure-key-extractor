import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else None)

@unittest.skipUnless(BASH and Path(BASH).is_file(), 'Bash required')
class SwapSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fstab = self.root / 'fstab'
        self.original = '# mounts\nUUID=synthetic / ext4 defaults 0 1\n'
        self.fstab.write_text(self.original)

    def run_swap(self, initial=0, free=20000, fs='ext4', before='', fail=''):
        script = r'''
set -euo pipefail
source "$SOURCE"
cd "$CASE"
printf 'SwapTotal: %s kB\n' "$INITIAL" > meminfo
touch active calls
swapon() {
    if [[ "${1:-}" == '--show' ]]; then cat active; return 0; fi
    local file="${@: -1}"
    echo "swapon $file" >> calls
    [[ "$FAIL" != swapon ]] || return 1
    grep -Fxq -- "$file" active && return 1
    printf '%s\n' "$file" >> active
    local size=8193 previous
    if [[ -f allocation ]]; then size="$(cat allocation)"; fi
    previous="$(swap_total_kib meminfo)"
    printf 'SwapTotal: %s kB\n' "$((previous + size * 1024 - 4))" > meminfo
}
df() { printf 'Filesystem Blocks Used Available Capacity Mount\n/dev/fake 30000 1000 %s 5%% /\n' "$FREE"; }
findmnt() { echo "$FILESYSTEM"; }
chattr() { echo chattr >> calls; }
dd() {
    echo dd >> calls
    [[ "$FAIL" != dd ]] || return 1
    local arg file='' count=''
    for arg in "$@"; do
        case "$arg" in of=*) file="${arg#of=}";; count=*) count="${arg#count=}";; esac
    done
    printf '%s' "$count" > allocation
    printf 'synthetic swap' > "$file"
}
mkswap() { echo mkswap >> calls; [[ "$FAIL" != mkswap ]]; }
'''
        script += '\n' + before + '\nsetup_swap "$CASE/swapfile-azure-key" "$CASE/fstab" "$CASE/meminfo"\n'
        path = self.root / 'scenario.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        env = {**os.environ, 'SOURCE':(ROOT/'setup_swap.sh').as_posix(), 'CASE':self.root.as_posix(),
               'INITIAL':str(initial), 'FREE':str(free), 'FILESYSTEM':fs, 'FAIL':fail}
        if os.name == 'nt':
            for key in ('SOURCE','CASE'):
                p = env[key]
                env[key] = '/' + p[0].lower() + p[2:]
        return subprocess.run([BASH, path.as_posix()], env=env, capture_output=True, text=True,
                              encoding='utf-8', timeout=20)

    def test_new_server_creates_8g_and_persists_it(self):
        r=self.run_swap()
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'allocation').read_text(),'8193')
        self.assertIn('none swap sw,nofail 0 0',self.fstab.read_text())
        self.assertEqual((self.root/'fstab.azure-key.bak').read_text(),self.original)

    def test_existing_8g_or_more_is_skipped(self):
        for initial in (8388604,8388608,16777216):
            r=self.run_swap(initial=initial)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertEqual((self.root/'calls').read_text(),'')
            self.assertEqual(self.fstab.read_text(),self.original)

    def test_existing_4g_is_only_topped_up(self):
        r=self.run_swap(initial=4194304)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'allocation').read_text(),'4097')

    def test_repeat_run_does_not_duplicate_files_or_fstab(self):
        r=self.run_swap(before='setup_swap "$CASE/swapfile-azure-key" "$CASE/fstab" "$CASE/meminfo"')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'calls').read_text().count('dd\n'),1)
        self.assertEqual(self.fstab.read_text().count('none swap sw,nofail'),1)

    def test_disk_space_check_runs_before_allocation(self):
        r=self.run_swap(free=9000)
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(list(self.root.glob('swapfile-azure-key.*')),[])
        self.assertEqual(self.fstab.read_text(),self.original)

    def test_failed_allocation_format_or_activation_preserves_other_files(self):
        existing=self.root/'swapfile-azure-key'
        existing.write_text('KEEP')
        for stage in ('dd','mkswap','swapon'):
            r=self.run_swap(fail=stage)
            self.assertNotEqual(r.returncode,0)
            self.assertEqual(existing.read_text(),'KEEP')
            self.assertEqual(list(self.root.glob('swapfile-azure-key.*')),[])
            self.assertEqual(self.fstab.read_text(),self.original)

    def test_inactive_managed_file_is_reused(self):
        before='touch "$CASE/swapfile-azure-key.saved"; printf "%s none swap sw,nofail 0 0\\n" "$CASE/swapfile-azure-key.saved" >> "$CASE/fstab"'
        r=self.run_swap(before=before)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        calls=(self.root/'calls').read_text()
        self.assertNotIn('dd\n',calls)
        self.assertNotIn('mkswap',calls)
        self.assertEqual(calls.count('swapon '),1)

    def test_active_file_missing_fstab_entry_is_persisted(self):
        before='touch "$CASE/swapfile-azure-key.saved"; printf "%s\\n" "$CASE/swapfile-azure-key.saved" > active'
        r=self.run_swap(initial=8388608,before=before)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'calls').read_text(),'')
        self.assertEqual(self.fstab.read_text().count('none swap sw,nofail'),1)

    def test_unsupported_filesystem_is_rejected_without_files(self):
        r=self.run_swap(fs='overlay')
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(list(self.root.glob('swapfile-azure-key.*')),[])
        self.assertEqual(self.fstab.read_text(),self.original)

    def test_btrfs_sets_nocow_before_writing(self):
        r=self.run_swap(fs='btrfs')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual((self.root/'calls').read_text().splitlines()[:3],['chattr','dd','mkswap'])
