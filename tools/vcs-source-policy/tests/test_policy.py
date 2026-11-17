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

    def test_dynamic_external_pkgbuild_source_known_but_not_approved(self):
        text = SIMPLE + '\nprepare() {\n  source "$srcdir/dependency/PKGBUILD"\n}\n'
        result = package_policy({
            'srcinfo': srcinfo(), 'pkgbuild': text,
            'package': 'emulator-git', 'installed_version': 'r5.x-1'})
        self.assertEqual(result['classification'], 'single-source')
        self.assertEqual(result['source_identification_method'], 'sole-moving-git-source')
        self.assertTrue(result['review_required'])
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



    def test_arch_guideline_relative_pkgname_checkout(self):
        text = '''pkgname=bluez-git
source=("bluez-git::git://git.kernel.org/pub/scm/bluetooth/bluez.git")
pkgver() {
  cd "$pkgname"
  git describe --long
}
'''
        item = classify(srcinfo(name='bluez-git',
                        sources=('bluez-git::git://git.kernel.org/pub/scm/bluetooth/bluez.git',)),
                        text, 'bluez-git', 'r42.a-1')
        self.assertEqual(item['classification'], 'single-source')
        self.assertEqual(item['suggested_monitor'], ['bluez-git'])

    def test_literal_private_pkgname_checkout(self):
        text = '''pkgname=paru-git
_pkgname=paru
pkgver() {
  cd "$srcdir/$_pkgname"
  git describe --long
}
'''
        item = classify(srcinfo(name='paru-git',
                        sources=('git+https://github.com/morganamilo/paru',)),
                        text, 'paru-git', 'r42.a-1')
        self.assertEqual(item['classification'], 'single-source')
        self.assertEqual(item['suggested_monitor'], ['paru'])

    def test_direct_literal_git_c_checkout(self):
        text = '''pkgver() {
  git -C "myapp" rev-parse HEAD
}
'''
        item = classify(srcinfo(name='myapp-git',
                        sources=('myapp::git+https://example.org/myapp.git',)),
                        text, 'myapp-git', 'r42.a-1')
        self.assertEqual(item['classification'], 'single-source')

    def test_unknown_or_reassigned_variable_is_inconclusive(self):
        original = srcinfo(name='myapp-git',
                           sources=('git+https://example.org/myapp.git',))
        unknown = '''pkgver() {
  cd "$_dynamic"
  git rev-parse HEAD
}
'''
        item = classify(original, unknown, 'myapp-git', 'r42.a-1')
        self.assertEqual(item['classification'], 'single-source')
        self.assertEqual(item['source_identification_method'], 'sole-moving-git-source')
        self.assertFalse(item['pkgver_source_reference_confirmed'])
        reassigned = '''_repo=myapp
_repo=other
pkgver() {
  cd "$_repo"
  git rev-parse HEAD
}
'''
        item = classify(original, reassigned, 'myapp-git', 'r42.a-1')
        self.assertEqual(item['classification'], 'single-source')
        self.assertFalse(item['pkgver_source_reference_confirmed'])



    def test_gitmodules_links_three_real_multi_git_cases_as_advisory(self):
        """These URLs are found in the upstream .gitmodules of real AUR sources."""
        fixtures = [
            ("viper4linux-re-git", "viper4linux",
             "https://github.com/username227/viper4linux-re.git",
             ["https://github.com/likelikeslike/ViPERDSP.git"]),
            ("jamesdsp-git", "JDSP4Linux",
             "https://github.com/Audio4Linux/JDSP4Linux.git",
             ["https://github.com/ThePBone/GraphicEQWidget.git",
              "https://github.com/ThePBone/FlatTabWidget.git",
              "https://github.com/ThePBone/LiquidEqualizerWidget.git",
              "https://github.com/ThePBone/LiveprogIDE.git"]),
            ("shadps4-qtlauncher-git", "shadps4-qtlauncher",
             "https://github.com/shadps4-emu/shadps4-qtlauncher.git",
             ["https://github.com/nlohmann/json.git",
              "https://github.com/gabime/spdlog.git",
              "https://github.com/zeux/volk.git"]),
        ]
        for pkg, main, main_url, secondary_urls in fixtures:
            with self.subTest(package=pkg):
                sources = [main + "::git+" + main_url]
                sources += ["git+" + url for url in secondary_urls]
                script = 'pkgver() {\n  cd "$srcdir/' + main + '"\n  git describe --long\n}\n'
                # Matching URL evidence remains advisory: the test does NOT
                # claim the installed PKGBUILD uses submodule gitlink pins.
                modules = ''.join(
                    '[submodule "deps/' + str(i) + '"]\n'
                    '    path = deps/' + str(i) + '\n'
                    '    url = ' + url.removesuffix(".git") + '\n'
                    for i, url in enumerate(secondary_urls))
                result = package_policy({
                    'package': pkg, 'installed_version': 'r42.abc-1',
                    'srcinfo': srcinfo(name=pkg, sources=sources),
                    'pkgbuild': script, 'gitmodules': modules,
                })
                self.assertEqual(result['classification'],
                                 'submodule-metadata-candidate')
                self.assertTrue(result['review_required'])
                self.assertFalse(result['lightweight_check_enabled'])
                self.assertIsNone(result['installed_revision'])
                self.assertEqual(
                    [x['rebuild'] for x in result['sources']],
                    ['Y'] + ['N'] * len(secondary_urls))
                self.assertEqual(
                    [x['role'] for x in result['sources'][1:]],
                    ['submodule-candidate'] * len(secondary_urls))

    def test_gitmodules_unrelated_url_never_marks_n(self):
        text = 'pkgver() {\n cd "$srcdir/main"\n git describe\n}\n'
        sources = ('main::git+https://github.com/owner/main.git',
                   'other::git+https://github.com/owner/unrelated.git')
        row = {'package': 'example-git', 'installed_version': 'r1.a-1',
               'pkgbuild': text, 'srcinfo': srcinfo(name='example-git', sources=sources),
               'gitmodules': ('[submodule "deps/other"]\n'
                              '    path = deps/other\n'
                              '    url = https://github.com/owner/something-else.git\n')}
        result = package_policy(row)
        self.assertEqual([x['rebuild'] for x in result['sources']], ['Y', 'Y'])
        self.assertEqual(result['classification'], 'inconclusive')

    def test_gitmodules_metadata_change_invalidates_kept_policy(self):
        text = 'pkgver() {\n cd "$srcdir/main"\n git describe\n}\n'
        sources = ('main::git+https://github.com/owner/main.git',
                   'other::git+https://github.com/owner/other.git')
        modules = ('[submodule "deps/other"]\n'
                   '  path = deps/other\n'
                   '  url = https://github.com/owner/other.git\n')
        row = {'package': 'example-git', 'installed_version': 'r1.a-1',
               'pkgbuild': text, 'srcinfo': srcinfo(name='example-git', sources=sources),
               'gitmodules': modules}
        old = package_policy(row)
        changed = dict(row, gitmodules=modules + '\n# remote metadata changed\n')
        new = package_policy(changed)
        self.assertIn('Packaging metadata changed', validate_kept_policy(old, new))



    def test_one_git_source_identified_even_when_pkgver_path_is_unparsed(self):
        cases = [
            ('verifytapn-git',
             'verifytapn-git::git+https://github.com/TAPAAL/verifytapn.git',
             'pkgname="verifytapn-git"\npkgver() {\n cd "$srcdir/$pkgname/"\n git rev-parse --short HEAD\n}\n'),
            ('mmdvmcal-git',
             'git+https://github.com/g4klx/MMDVMCal.git',
             'pkgname=mmdvmcal-git\npkgver() {\n cd "$srcdir/${pkgname%-git}"\n git rev-parse HEAD\n}\n'),
            ('mingw-w64-mesa-demos-git',
             'git+https://gitlab.freedesktop.org/mesa/demos.git',
             'pkgver() {\n cd "${srcdir}"/demos\n git rev-parse HEAD\n}\n'),
            ('missing-pkgver-git',
             'git+https://example.org/missing-pkgver.git',
             'pkgname=missing-pkgver-git\nprepare() { :; }\n'),
            ('date-pkgver-git',
             'git+https://example.org/date-pkgver.git',
             'pkgver() { date +%Y%m%d; }\n'),
        ]
        for name, source, script in cases:
            with self.subTest(package=name):
                info = classify(srcinfo(name=name, sources=(source,)),
                                script, name, 'r1.abcdef0-1')
                self.assertEqual(info['classification'], 'single-source')
                self.assertEqual(info['source_identification_method'],
                                 'sole-moving-git-source')
                self.assertEqual(len(info['suggested_monitor']), 1)
                self.assertFalse(info['pkgver_source_reference_confirmed'])
                row = package_policy({'package': name, 'installed_version': 'r1.abcdef0-1',
                                      'pkgbuild': script, 'srcinfo': srcinfo(
                                          name=name, sources=(source,))})
                self.assertTrue(row['review_required'])
                self.assertFalse(row['lightweight_check_enabled'])

    def test_known_pkgver_binding_keeps_single_source_review_clear(self):
        row = package_policy({'package': 'emulator-git',
                              'installed_version': 'r14.aabbcc-1',
                              'pkgbuild': SIMPLE, 'srcinfo': srcinfo()})
        self.assertEqual(row['classification'], 'single-source')
        self.assertTrue(row['pkgver_source_reference_confirmed'])
        self.assertFalse(row['review_required'])
        self.assertFalse(row['lightweight_check_enabled'])

    def test_fixed_git_commit_and_tag_not_misidentified_as_moving(self):
        for fragment in ('commit=141648b', 'tag=v1.0'):
            with self.subTest(fragment=fragment):
                source = 'repo::git+https://github.com/example/repo.git#' + fragment
                script = 'pkgver() {\n cd repo\n git describe --long\n}\n'
                row = package_policy({
                    'package': 'repo-git', 'installed_version': '1.0-1',
                    'pkgbuild': script, 'srcinfo': srcinfo(
                        name='repo-git', sources=(source,))})
                self.assertEqual(row['classification'], 'fixed-source')
                self.assertEqual(row['source_identification_method'],
                                 'sole-fixed-git-source')
                self.assertTrue(row['review_required'])
                self.assertFalse(row['lightweight_check_enabled'])

    def test_unknown_git_fragment_is_not_a_confident_moving_source(self):
        source = 'repo::git+https://example.org/repo.git#something=unrecognized'
        row = classify(srcinfo(name='repo-git', sources=(source,)),
                       'pkgver() { cd repo; git describe; }',
                       'repo-git', 'r1.abc-1')
        self.assertEqual(row['classification'], 'inconclusive')
        self.assertEqual(row['suggested_monitor'], [])
        self.assertFalse(row['lightweight_check_enabled'])

    def test_architecture_qualified_source_requires_review(self):
        text = srcinfo() + '\tsource_x86_64 = somearch-specific.patch\n'
        row = package_policy({'package': 'emulator-git',
                              'installed_version': 'r14.aabbcc-1',
                              'pkgbuild': SIMPLE, 'srcinfo': text})
        self.assertEqual(row['classification'], 'single-source')
        self.assertTrue(row['pkgver_source_reference_confirmed'])
        self.assertTrue(row['review_required'])
        self.assertFalse(row['lightweight_check_enabled'])


    def test_one_git_plus_other_vcs_does_not_allow_unreviewed_probe(self):
        text = srcinfo(sources=(
            'emulator::git+https://example.org/emulator.git',
            'hg+https://example.org/second-hg-source',
        ))
        row = package_policy({'package': 'emulator-git',
                              'installed_version': 'r14.aabbcc-1',
                              'pkgbuild': SIMPLE, 'srcinfo': text})
        self.assertEqual(row['classification'], 'single-source')
        self.assertEqual(row['source_identification_method'],
                         'sole-moving-git-source')
        self.assertTrue(row['review_required'])
        self.assertIn('Additional non-Git VCS source needs independent review',
                      row['reasons'])

    def test_single_git_with_floating_submodules_requires_review(self):
        script = SIMPLE + '''
prepare() {
  cd "$srcdir/emulator"
  git submodule update --init --remote
}
'''
        row = package_policy({'package': 'emulator-git',
                              'installed_version': 'r14.aabbcc-1',
                              'pkgbuild': script, 'srcinfo': srcinfo()})
        self.assertEqual(row['classification'], 'single-source')
        self.assertTrue(row['pkgver_source_reference_confirmed'])
        self.assertTrue(row['review_required'])
        self.assertFalse(row['lightweight_check_enabled'])


    def test_old_saved_policy_without_new_analysis_fields_is_stale(self):
        row = {'package': 'emulator-git', 'installed_version': 'r14.aabbcc-1',
               'pkgbuild': SIMPLE, 'srcinfo': srcinfo()}
        proposed = package_policy(row)
        old = dict(proposed)
        old.pop('source_identification_method')
        old.pop('pkgver_source_reference_confirmed')
        self.assertIn('Source classification changed; recreate or review policy',
                      validate_kept_policy(old, proposed))
        current = dict(proposed)
        self.assertEqual(validate_kept_policy(current, proposed), [])


if __name__ == '__main__':
    unittest.main()
