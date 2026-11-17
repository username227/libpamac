import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import vcs_policy
except ModuleNotFoundError:
    # Local CI fixture when testing this standalone file apart from libpamac repo.
    vcs_policy = types.ModuleType('vcs_policy')
    vcs_policy.sha256 = lambda value: hashlib.sha256(value.encode()).hexdigest()
    def parse_srcinfo(text):
        lines = [line.strip() for line in text.splitlines()]
        return next((l[10:] for l in lines if l.startswith('pkgbase = ')), None), \
               [l[9:] for l in lines if l.startswith('source = ')]
    vcs_policy.parse_srcinfo = parse_srcinfo
    def source_record(raw):
        source = raw.split('::')[-1]
        if not source.startswith(('git+', 'git://')):
            return None
        source, _, fragment = source.partition('#')
        return {'fragment': fragment, 'url': source.removeprefix('git+'), 'supported': True}
    vcs_policy.source_record = source_record
    vcs_policy.package_policy = lambda row: {
        'classification': 'single-source', 'sources': [
            {'source': s, 'rebuild': 'Y'}
            for s in parse_srcinfo(row['srcinfo'])[1]
        ], 'review_required': False, 'lightweight_check_enabled': False, 'reasons': []}
    sys.modules['vcs_policy'] = vcs_policy
try:
    import submodule_metadata
except ModuleNotFoundError:
    submodule_metadata = types.ModuleType('submodule_metadata')
    submodule_metadata.github_metadata_for_primary = lambda url, fragment='': None
    sys.modules['submodule_metadata'] = submodule_metadata
import aur_random_audit as audit


class RandomAuditTests(unittest.TestCase):
    def test_random_three_rounds_two_hundred_reproducible_and_no_replacement(self):
        vcs = [f'app{i:04d}-git' for i in range(1500)]
        nonsuffix = [f'release{i:04d}' for i in range(100)]
        a = audit.select_samples(vcs + nonsuffix, 3, 200, 20261117, 50)
        b = audit.select_samples(vcs + nonsuffix, 3, 200, 20261117, 50)
        c = audit.select_samples(vcs + nonsuffix, 3, 200, 20261118, 50)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        sampled = [p for group in a['rounds'] for p in group['packages']]
        self.assertEqual(len(sampled), 600)
        self.assertEqual(len(set(sampled)), 600)
        self.assertTrue(all(p.endswith('-git') for p in sampled))
        self.assertTrue(all(not p.endswith('-git') for p in a['negative_controls']))
        self.assertEqual(a['vcs_suffix_population_size'], 1500)

    def test_insufficient_population_fails_instead_of_resampling(self):
        with self.assertRaises(ValueError):
            audit.select_samples(['a-git', 'b-git'], 2, 2, 1)

    def test_compressed_index_is_hashed_and_sorted(self):
        gz = gzip.compress(b'# AUR packages\nb-git\na-git\nb-git\nordinary\n')
        with tempfile.TemporaryDirectory() as d:
            index = Path(d) / 'packages.gz'
            index.write_bytes(gz)
            names, sha = audit.load_aur_index(index)
        self.assertEqual(names, ['a-git', 'b-git', 'ordinary'])
        self.assertEqual(sha, hashlib.sha256(gz).hexdigest())

    def test_rpcs_resolve_split_packagebase(self):
        payload = {'results': [{'Name': 'one-git', 'PackageBase': 'large-suite-git'},
                               {'Name': 'two-git', 'PackageBase': 'large-suite-git'}]}
        with patch.object(audit, 'fetch_limited', return_value=json.dumps(payload).encode()) as fetch:
            result = audit.rpc_bases(['one-git', 'two-git'], rate_delay=0)
        self.assertEqual(result, {'one-git': 'large-suite-git',
                                  'two-git': 'large-suite-git'})
        self.assertIn('arg%5B%5D=one-git', fetch.call_args.args[0])

    def test_snapshot_reads_only_metadata_not_arbitrary_script(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            for name, content in {'suite/PKGBUILD': b'pkgver() { true; }\n',
                                  'suite/.SRCINFO': b'pkgbase = suite\n',
                                  'suite/evil.sh': b'echo executed\n',
                                  '../outside': b'bad\n'}.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        with patch.object(audit, 'fetch_limited', return_value=data.getvalue()):
            pkgbuild, srcinfo, digest = audit.snapshot_packagebase('suite')
        self.assertIn('pkgver()', pkgbuild)
        self.assertIn('pkgbase = suite', srcinfo)
        self.assertEqual(len(digest), 64)
        self.assertNotIn('executed', pkgbuild)

    def test_classifier_rejects_unreviewed_n_and_enabled_policy(self):
        row = {'classification': 'submodule-candidate',
               'sources': [{'source': 'parent', 'rebuild': 'Y'},
                           {'source': 'child', 'rebuild': 'N'}],
               'review_required': False, 'lightweight_check_enabled': False,
               'reasons': []}
        src = 'pkgbase = suite\npkgname = suite-git\n\tsource = parent::git+https://example.org/parent.git\n'
        with patch.object(audit, 'package_policy', return_value=row):
            with self.assertRaisesRegex(ValueError, 'N source without required review'):
                audit.audit_one('suite-git', 'suite', '', src, round_num=1)
        row['review_required'] = True
        row['lightweight_check_enabled'] = True
        with patch.object(audit, 'package_policy', return_value=row):
            with self.assertRaisesRegex(ValueError, 'enabled lightweight'):
                audit.audit_one('suite-git', 'suite', '', src, round_num=1)

    def test_ambiguous_tag_and_remote_are_red_flags(self):
        src = ('pkgbase = suite\npkgname = suite-git\n'
               '\tsource = pinned::git+https://example.org/pinned.git#tag=v2\n')
        row = audit.audit_one('suite-git', 'suite',
                              'prepare() { git submodule update --remote; }\n',
                              src, round_num=2)
        self.assertEqual(row['fixed_git_sources'], 1)
        self.assertIn('no-moving-git-source', row['red_flags'])
        self.assertIn('PKGBUILD-mentions-submodule-remote', row['red_flags'])

    def test_exact_split_member_is_required(self):
        with self.assertRaisesRegex(ValueError, 'absent from current'):
            audit.audit_one('gone-git', 'suite', '',
                            'pkgbase = suite\npkgname = remaining-git\n', round_num=1)

    def test_optional_metadata_is_bounded_and_remains_advisory(self):
        sample = {'schema': 1, 'rounds': [{'round': 1, 'packages': ['suite-git']}],
                  'negative_controls': [], 'index_sha256': 'a' * 64}
        src = ('pkgbase = suite\npkgname = suite-git\n'
               'source = main::git+https://github.com/org/main.git\n'
               'source = child::git+https://github.com/org/child.git\n')
        modules = ('[submodule "dep/child"]\n path = dep/child\n'
                   ' url = https://github.com/org/child.git\n')
        original_policy = audit.package_policy

        def classified(row):
            source_entries = [{'source': 'main::git+https://github.com/org/main.git',
                               'alias': 'main', 'kind': 'git', 'role': 'primary-candidate',
                               'rebuild': 'Y'},
                              {'source': 'child::git+https://github.com/org/child.git',
                               'alias': 'child', 'kind': 'git', 'role': 'submodule-candidate'
                               if 'gitmodules' in row else 'unresolved-git-source',
                               'rebuild': 'N' if 'gitmodules' in row else 'Y'}]
            return {'sources': source_entries, 'classification': 'submodule-metadata-candidate'
                    if 'gitmodules' in row else 'inconclusive',
                    'review_required': True, 'lightweight_check_enabled': False,
                    'reasons': ['advisory review']}

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            with patch.object(audit, 'rpc_bases', return_value={'suite-git': 'suite'}), \
                 patch.object(audit, 'snapshot_packagebase', return_value=('pkgver() { cd main; }', src, 'b' * 64)), \
                 patch.object(audit, 'package_policy', side_effect=classified), \
                 patch.object(audit.submodule_metadata, 'github_metadata_for_primary', return_value=modules) as remote, \
                 patch.object(audit.time, 'sleep'):
                report = audit.run(sample, output_dir=base / 'first',
                                   inspect_gitmodules=True, max_gitmodules=1)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(report['packages'][0]['n_sources'], 1)
            self.assertTrue(report['packages'][0]['review_required'])
            self.assertFalse(report['packages'][0]['lightweight_check_enabled'])
            self.assertEqual(report['metadata_inspection_attempts'], 1)
            self.assertEqual((base / 'first/packaging/suite/.gitmodules').read_text(), modules)
            with patch.object(audit, 'package_policy', side_effect=classified), \
                 patch.object(audit.submodule_metadata, 'github_metadata_for_primary') as remote:
                replay = audit.run(sample, output_dir=base / 'replay', offline_dir=base / 'first',
                                   inspect_gitmodules=True)
                remote.assert_not_called()
            self.assertEqual(replay['packages'][0]['n_sources'], 1)

        with tempfile.TemporaryDirectory() as d:
            with patch.object(audit, 'rpc_bases', return_value={'suite-git': 'suite'}), \
                 patch.object(audit, 'snapshot_packagebase', return_value=('pkgver() { cd main; }', src, 'b' * 64)), \
                 patch.object(audit, 'package_policy', side_effect=classified), \
                 patch.object(audit.submodule_metadata, 'github_metadata_for_primary') as remote, \
                 patch.object(audit.time, 'sleep'):
                capped = audit.run(sample, output_dir=Path(d) / 'capped',
                                   inspect_gitmodules=True, max_gitmodules=0)
            remote.assert_not_called()
            self.assertEqual(capped['packages'][0]['n_sources'], 0)
            self.assertEqual(capped['packages'][0]['gitmodules_status'], 'skipped-budget')

    def test_main_freezes_supplied_index_and_writes_results(self):
        gz = gzip.compress(b'a-git\nb-git\nordinary\n')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            index = base / 'input-packages.gz'
            index.write_bytes(gz)
            destination = base / 'results'
            pkgtext = 'pkgver() { true; }\n'
            srctext = 'pkgbase = suite\npkgname = a-git\npkgname = b-git\n'
            with patch.object(audit, 'rpc_bases', return_value={
                'a-git': 'suite', 'b-git': 'suite'}), \
                patch.object(audit, 'snapshot_packagebase', return_value=(pkgtext, srctext, 'c' * 64)), \
                patch.object(audit.time, 'sleep'), \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = audit.main([
                    '--index-file', str(index), '--rounds', '1',
                    '--sample-size', '1', '--seed', '7',
                    '--output-dir', str(destination)])
            self.assertEqual(exit_code, 0)
            self.assertEqual((destination / 'packages.gz').read_bytes(), gz)
            self.assertEqual(len(json.loads((destination / 'results.json').read_text())['packages']), 1)
            self.assertIn('ground_truth_validated', stdout.getvalue())

    def test_failed_download_not_hidden_or_resampled_and_split_cache(self):
        sample = {'schema': 1, 'rounds': [{'round': 1, 'packages': ['good-git', 'bad-git', 'split-git']}],
                  'negative_controls': [], 'index_sha256': 'a' * 64}
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            def fake_snapshot(base):
                if base == 'broken':
                    raise ValueError('broken snapshot')
                return ('pkgver() { true; }\n', 'pkgbase = shared\npkgname = good-git\npkgname = split-git\n', 'a' * 64)
            with patch.object(audit, 'rpc_bases', return_value={
                'good-git': 'shared', 'bad-git': 'broken', 'split-git': 'shared'}), \
                patch.object(audit, 'snapshot_packagebase', side_effect=fake_snapshot) as fetched, \
                patch.object(audit.time, 'sleep'):
                result = audit.run(sample, output_dir=d)
            self.assertEqual(len(result['packages']), 3)
            self.assertEqual([p['status'] for p in result['packages']],
                             ['classified', 'fetch-error', 'classified'])
            self.assertEqual(fetched.call_count, 2)  # split packages share one snapshot
            self.assertEqual(result['summary']['overall']['errors'], 1)
            self.assertFalse(result['summary']['overall']['ground_truth_validated'])
            self.assertEqual(len(json.loads((d / 'results.json').read_text())['packages']), 3)
            self.assertTrue((d / 'results.csv').exists())
            replay = audit.run(sample, output_dir=d / 'replay', offline_dir=d)
            self.assertEqual([p['status'] for p in replay['packages']],
                             ['classified', 'fetch-error', 'classified'])


if __name__ == '__main__':
    unittest.main()