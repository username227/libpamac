import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vcs_policy import classify, regenerate, atomic_write_json


def srcinfo(name='emulator-git', sources=('emulator::git+https://example.org/emulator.git#branch=main',)):
    return 'pkgbase = ' + name + '\n' + ''.join('\tsource = ' + x + '\n' for x in sources) + '\npkgname = ' + name + '\n'


SIMPLE = '''pkgname=emulator-git
source=('emulator::git+https://example.org/emulator.git#branch=main')
pkgver() {
  cd "$srcdir/emulator"
  git describe --long
}
'''


class PolicyTests(unittest.TestCase):
    def test_single_git_source(self):
        record = classify(srcinfo(), SIMPLE, 'emulator-git', 'r14.aabbcc-1')
        self.assertEqual(record['classification'], 'single-source')
        self.assertEqual(record['suggested_monitor'], ['emulator'])
        self.assertFalse(record['lightweight_check_enabled'])
        self.assertIsNone(record['installed_revision'])

    def test_twenty_five_submodules_are_candidates_not_triggers(self):
        deps = [f'dep{i}' for i in range(25)]
        sources = ['emulator::git+https://example.org/emulator.git'] + [
            f'{name}::git+https://example.org/{name}.git' for name in deps]
        config = '\n'.join(f'  git config submodule.{name}.url "$srcdir/{name}"' for name in deps)
        pkgbuild = '''pkgver() {
    cd "${srcdir}/emulator"
    git rev-list --count HEAD
}
prepare() {
  configure_modules
}
configure_modules() {
''' + config + '''
  git submodule update --init --recursive
}
'''
        result = classify(srcinfo(sources=sources), pkgbuild, 'emulator-git', 'r5.old-1')
        self.assertEqual(result['classification'], 'submodule-candidate')
        self.assertEqual(result['suggested_monitor'], ['emulator'])
        self.assertEqual(len(result['identified_submodules']), 25)
        self.assertEqual(result['monitor'], [])
        self.assertFalse(result['lightweight_check_enabled'])

    def test_dynamic_external_pkgbuild_is_inconclusive(self):
        text = SIMPLE + '\nprepare() {\n  source "$srcdir/dependency/PKGBUILD"\n}\n'
        result = classify(srcinfo(), text, 'emulator-git', 'r5.x-1')
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertFalse(result['lightweight_check_enabled'])

    def test_multiple_vcs_without_proven_submodule_relationship(self):
        sources = ('emulator::git+https://example.org/emulator.git',
                   'external::git+https://example.org/external.git')
        record = classify(srcinfo(sources=sources), SIMPLE, 'emulator-git', 'r4.a-1')
        self.assertEqual(record['classification'], 'inconclusive')
        self.assertEqual(record['suggested_monitor'], ['emulator'])
        self.assertEqual(record['monitor'], [])

    def test_overrides_only_if_exact_alias_and_url_match(self):
        info = srcinfo(sources=('emulator::git+https://example.org/emulator.git',
                                'other::git+https://example.org/other.git'))
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': info, 'pkgbuild': SIMPLE}
        overrides = {'packages': {'emulator-git': {'monitor': [
            {'alias': 'emulator', 'url': 'https://example.org/emulator.git'}]}}}
        result = regenerate([row], overrides)['packages']['emulator-git']
        self.assertEqual(result['classification'], 'user-policy')
        self.assertEqual(result['monitor'], ['emulator'])
        self.assertFalse(result['lightweight_check_enabled'])
        changed = row.copy()
        changed['srcinfo'] = info.replace('https://example.org/emulator.git',
                                          'https://evil.invalid/emulator.git')
        result = regenerate([changed], overrides)['packages']['emulator-git']
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertEqual(result['monitor'], [])
        self.assertTrue(any('Stale override' in x for x in result['reasons']))

    def test_override_invalidated_if_git_branch_changes(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(sources=(
                   'emulator::git+https://example.org/emulator.git#branch=main',
                   'other::git+https://example.org/other.git')),
               'pkgbuild': SIMPLE}
        overrides = {'packages': {'emulator-git': {'monitor': [
            {'alias': 'emulator', 'url': 'https://example.org/emulator.git',
             'fragment': 'branch=main'}]}}}
        accepted = regenerate([row], overrides)['packages']['emulator-git']
        self.assertEqual(accepted['monitor'], ['emulator'])
        row['srcinfo'] = row['srcinfo'].replace('branch=main', 'branch=testing')
        rejected = regenerate([row], overrides)['packages']['emulator-git']
        self.assertEqual(rejected['monitor'], [])
        self.assertEqual(rejected['classification'], 'inconclusive')

    def test_rebuild_fresh_without_preserving_old_package_entries(self):
        rows = [{'package': 'emulator-git', 'installed_version': 'r2.a-1',
                 'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}]
        first = regenerate(rows)
        second = regenerate([])
        self.assertIn('emulator-git', first['packages'])
        self.assertEqual(second['packages'], {})

    def test_pkgbuild_changes_are_detectable(self):
        first = classify(srcinfo(), SIMPLE, 'emulator-git', 'r2.a-1')
        second = classify(srcinfo(), SIMPLE + '\nmakedepends=(cmake)\n', 'emulator-git', 'r2.a-1')
        self.assertNotEqual(first['pkgbuild_sha256'], second['pkgbuild_sha256'])

    def test_not_execute_pkgbuild(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'DID_EXECUTE'
            hostile = SIMPLE + f'\ntrigger=$(touch "{marker}")\n'
            classify(srcinfo(), hostile, 'emulator-git', 'r2.a-1')
            self.assertFalse(marker.exists())

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            atomic_write_json(path, {'schema': 1, 'packages': {'a': {}}})
            self.assertEqual(json.loads(path.read_text())['packages'], {'a': {}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_split_packages_share_base(self):
        info = srcinfo(name='common-base')
        row = lambda name: {'package': name, 'installed_version': 'r2.a-1',
                            'srcinfo': info, 'pkgbuild': SIMPLE}
        result = regenerate([row('common-foo-git'), row('common-bar-git')])
        self.assertEqual(len(result['packages']), 1)
        self.assertEqual(set(result['packages']['common-base']['installed_packages']),
                         {'common-foo-git', 'common-bar-git'})


if __name__ == '__main__':
    unittest.main()
