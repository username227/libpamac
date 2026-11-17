import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vcs_policy import (classify, atomic_write_json,
                        package_policy, package_policies, write_package_policies,
                        validate_kept_policy)


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

    def test_all_sources_receive_editable_flags_even_for_verified_package(self):
        info = srcinfo(sources=('emulator::git+https://example.org/emulator.git',
                                'patch.diff'))
        item = package_policy({'package': 'emulator-git', 'installed_version': 'r2.a-1',
                               'srcinfo': info, 'pkgbuild': SIMPLE})
        self.assertEqual([x['rebuild'] for x in item['sources']], ['Y', 'Y'])
        self.assertEqual([x['kind'] for x in item['sources']], ['git', 'other'])
        self.assertIsNone(item['installed_revision'])
        self.assertFalse(item['lightweight_check_enabled'])

    def test_submodules_have_n_flags_and_uncertain_sources_stay_y(self):
        deps = [f'dep{i}' for i in range(25)]
        sources = ['emulator::git+https://example.org/emulator.git'] + [
            f'{name}::git+https://example.org/{name}.git' for name in deps] + [
            'uncertain::git+https://example.org/uncertain.git']
        config = '\n'.join(
            f'  git config submodule.{name}.url "$srcdir/{name}"' for name in deps)
        script = '''pkgver() {
  cd "$srcdir/emulator"
  git rev-list --count HEAD
}
prepare() {
  setup_modules
}
setup_modules() {
''' + config + '''
  git submodule update --init --recursive
}
'''
        item = package_policy({'package': 'emulator-git', 'installed_version': 'r2.a-1',
                               'srcinfo': srcinfo(sources=sources), 'pkgbuild': script})
        flags = {x['alias']: x['rebuild'] for x in item['sources']}
        self.assertEqual(flags['emulator'], 'Y')
        self.assertEqual(flags['uncertain'], 'Y')
        self.assertEqual(sum(x == 'N' for x in flags.values()), 25)
        self.assertFalse(item['lightweight_check_enabled'])

    def test_one_file_for_each_split_package(self):
        info = srcinfo(name='shared-base')
        def row(name):
            return {'package': name, 'installed_version': 'r2.a-1',
                    'srcinfo': info, 'pkgbuild': SIMPLE}
        policies = package_policies([row('shared-foo-git'), row('shared-bar-git')])
        self.assertEqual(set(policies), {'shared-foo-git', 'shared-bar-git'})
        with tempfile.TemporaryDirectory() as directory:
            report = write_package_policies(policies, Path(directory))
            self.assertEqual(len(report['actions']), 2)
            self.assertEqual({p.name for p in Path(directory).glob('*.json')},
                             {'shared-foo-git.json', 'shared-bar-git.json'})
            self.assertEqual(json.loads((Path(directory) / 'shared-foo-git.json').read_text())
                             ['packagebase'], 'shared-base')

    def test_keep_user_edited_n_even_when_primary_is_confident(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}
        policies = package_policies([row])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(policies, folder)
            path = folder / 'emulator-git.json'
            saved = json.loads(path.read_text())
            saved['sources'][0]['rebuild'] = 'N'
            atomic_write_json(path, saved)
            report = write_package_policies(policies, folder, existing='keep')
            self.assertEqual(report['actions'][0]['action'], 'keep')
            self.assertEqual(report['actions'][0]['warnings'], [])
            self.assertEqual(json.loads(path.read_text())['sources'][0]['rebuild'], 'N')

    def test_keep_changed_metadata_marks_stale_without_overwriting(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(package_policies([row]), folder)
            path = folder / 'emulator-git.json'
            old = path.read_bytes()
            row['srcinfo'] = srcinfo(sources=(
                'emulator::git+https://example.org/emulator.git#branch=testing',
                'new-submodule::git+https://example.org/new.git'))
            report = write_package_policies(package_policies([row]), folder,
                                             existing='keep')
            self.assertEqual(report['actions'][0]['action'], 'keep-stale')
            self.assertIn('Source list changed, added, or removed',
                          report['actions'][0]['warnings'])
            self.assertEqual(path.read_bytes(), old)

    def test_overwrite_resets_all_flags_including_user_edits(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}
        policies = package_policies([row])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(policies, folder)
            path = folder / 'emulator-git.json'
            saved = json.loads(path.read_text())
            saved['sources'][0]['rebuild'] = 'N'
            atomic_write_json(path, saved)
            report = write_package_policies(policies, folder, existing='overwrite')
            self.assertEqual(report['actions'][0]['action'], 'overwrite')
            self.assertEqual(json.loads(path.read_text())['sources'][0]['rebuild'], 'Y')

    def test_backup_before_overwrite_keeps_exact_old_data_in_same_folder(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}
        policies = package_policies([row])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(policies, folder)
            path = folder / 'emulator-git.json'
            old = json.loads(path.read_text())
            old['sources'][0]['rebuild'] = 'N'
            atomic_write_json(path, old)
            original_bytes = path.read_bytes()
            report = write_package_policies(policies, folder, existing='backup')
            self.assertEqual(report['actions'][0]['action'], 'backup-and-overwrite')
            backup = Path(report['actions'][0]['backup'])
            self.assertEqual(backup.parent, folder)
            self.assertTrue(backup.name.startswith('emulator-git.json.bak-'))
            self.assertEqual(backup.read_bytes(), original_bytes)
            self.assertEqual(json.loads(path.read_text())['sources'][0]['rebuild'], 'Y')

    def test_dry_run_does_not_create_folder_or_backup(self):
        row = {'package': 'emulator-git', 'installed_version': 'r2.a-1',
               'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'new'
            report = write_package_policies(package_policies([row]), output,
                                             existing='backup', dry_run=True)
            self.assertEqual(report['actions'][0]['action'], 'write')
            self.assertFalse(output.exists())

    def test_keep_invalid_y_n_is_stale(self):
        policies = package_policies([{'package': 'emulator-git',
                                     'installed_version': 'r2.a-1',
                                     'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}])
        old = json.loads(json.dumps(policies['emulator-git']))
        old['sources'][0]['rebuild'] = 'MAYBE'
        self.assertIn('Invalid rebuild flag (must be Y or N)',
                      validate_kept_policy(old, policies['emulator-git']))

    def test_refuse_symlink_output(self):
        policies = package_policies([{'package': 'emulator-git',
                                     'installed_version': 'r2.a-1',
                                     'srcinfo': srcinfo(), 'pkgbuild': SIMPLE}])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / 'untouched'
            target.write_text('marker')
            output = folder / 'policies'
            output.mkdir()
            (output / 'emulator-git.json').symlink_to(target)
            with self.assertRaises(ValueError):
                write_package_policies(policies, output, existing='backup')
            self.assertEqual(target.read_text(), 'marker')


    def test_one_installed_package_from_twenty_way_split_base(self):
        """Never fabricate policy files for uninstalled split siblings."""
        info = srcinfo(name='emulator-suite')
        selected = {'package': 'emulator-gui-git', 'installed_version': 'r8.abc-1',
                    'srcinfo': info, 'pkgbuild': SIMPLE}
        policies = package_policies([selected])
        self.assertEqual(list(policies), ['emulator-gui-git'])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(policies, folder)
            self.assertEqual([p.name for p in folder.glob('*.json')],
                             ['emulator-gui-git.json'])
            saved = json.loads((folder / 'emulator-gui-git.json').read_text())
            self.assertEqual(saved['package'], 'emulator-gui-git')
            self.assertEqual(saved['packagebase'], 'emulator-suite')

    def test_regeneration_inventory_does_not_include_removed_split_siblings(self):
        """A stale old file is inert, but should eventually be pruned safely."""
        info = srcinfo(name='emulator-suite')
        def row(name):
            return {'package': name, 'installed_version': 'r8.abc-1',
                    'srcinfo': info, 'pkgbuild': SIMPLE}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_package_policies(package_policies(
                [row('emulator-gui-git'), row('emulator-cli-git')]), folder)
            next_policies = package_policies([row('emulator-gui-git')])
            report = write_package_policies(next_policies, folder, existing='keep')
            self.assertEqual([a['package'] for a in report['actions']],
                             ['emulator-gui-git'])
            self.assertEqual(list(next_policies), ['emulator-gui-git'])
            self.assertTrue((folder / 'emulator-cli-git.json').exists())
            # Prototype intentionally does not delete old user-edited policy
            # files; the eventual installed-package resolver must ignore them.



if __name__ == '__main__':
    unittest.main()
