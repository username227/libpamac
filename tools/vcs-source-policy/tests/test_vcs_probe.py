from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vcs_probe import (reported_commit, source_target, parse_remote_output,
                       ls_remote, assess_package, parse_approvals)


def policy(package='demo-git', classification='single-source', sources=None):
    if sources is None:
        sources = [{'source': 'demo::git+https://github.com/owner/demo.git',
                    'role': 'primary-candidate', 'rebuild': 'Y'}]
    return {'packagebase': package, 'pkgbuild_sha256': 'c'*64,
            'installed_version': 'r4.abc0123-1',
            'classification': classification, 'sources': sources}

ROW = {'package': 'demo-git', 'installed_version': 'r4.abc0123-1'}
SHA = 'abc0123'+'f'*33
NEW = '1234567'+'e'*33


class ProbeTests(unittest.TestCase):
    def test_approval_must_match_current_pkgbuild_hash(self):
        p = policy(classification='submodule-metadata-candidate', sources=[
            {'source': 'demo::git+https://github.com/owner/demo.git',
             'role': 'primary-candidate', 'rebuild': 'Y'},
            {'source': 'other::git+https://github.com/owner/other.git',
             'role': 'submodule-candidate', 'rebuild': 'N'}])
        blocked = assess_package(ROW, p, approve={'demo-git': 'a'*64},
                                 query=lambda _: self.fail('Unreviewed build contacted remote'))
        self.assertEqual(blocked['status'], 'incomplete')
        self.assertEqual(parse_approvals(['demo-git=' + 'c'*64]), {'demo-git': 'c'*64})
        for v in ('demo-git=short', 'demo-git', '../x=' + 'c'*64):
            with self.assertRaises(ValueError):
                parse_approvals([v])

    def test_version_hints(self):
        examples = [('1:1.7.0.r16077.g8d9fda86576f-1', '8d9fda86576f'),
                    ('r4874.6af327bdd-1', '6af327bdd'),
                    ('r3431.0518441-1', '0518441'),
                    ('3.5.1+r33+gb496406df-1', 'b496406df'),
                    ('3.5.1-1', None)]
        for v, want in examples:
            self.assertEqual(reported_commit(v), want)

    def test_branch_and_default(self):
        default = source_target('demo::git+https://github.com/owner/demo.git')
        self.assertEqual(default['ref_kind'], 'default-branch')
        branch = source_target('git+https://github.com/owner/demo.git#branch=main')
        self.assertEqual(branch['ref'], 'refs/heads/main')
        self.assertEqual(source_target('git+https://x.example/a.git#tag=v1')['ref_kind'],
                         'pinned-tag')
        self.assertEqual(source_target('git+https://x.example/a.git#commit=abcdef')['ref_kind'],
                         'pinned-commit')

    def test_malicious_urls_and_branch_not_used(self):
        for value in ('git+file:///tmp/x', 'git+ssh://github.com/x/y',
                      'git+https://a@github.com/x/y',
                      'git+https://github.com/x/y#branch=../bad',
                      'git+https://github.com/x/y#branch=-bad',
                      'git+https://github.com/x/y#branch=main;rm',
                      'git+https://github.com/x/y#tag=',
                      'git+https://github.com/x/y#unknown=test'):
            with self.subTest(source=value), self.assertRaises(ValueError):
                source_target(value)

    def test_exact_symref_and_head_required(self):
        target = source_target('git+https://example.org/demo.git')
        result = parse_remote_output('ref: refs/heads/main\tHEAD\n'+SHA+'\tHEAD\n', target)
        self.assertEqual(result, {'commit': SHA, 'ref': 'refs/heads/main'})
        for output in (SHA+'\tHEAD\n',
                       'ref: refs/heads/main\tHEAD\n',
                       'ref: refs/tags/foo\tHEAD\n'+SHA+'\tHEAD\n',
                       'ref: refs/heads/main\tHEAD\n'+SHA+'\tHEAD\n'+SHA+'\tHEAD\n'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_remote_output(output, target)

    def test_named_branch(self):
        target = source_target('git+https://example.org/demo.git#branch=stable')
        result = parse_remote_output(NEW+'\trefs/heads/stable\n', target)
        self.assertEqual(result['commit'], NEW)
        with self.assertRaises(ValueError):
            parse_remote_output(SHA+'\trefs/heads/main\n', target)

    def test_only_ls_remote_and_no_prompts(self):
        target = source_target('demo::git+https://github.com/owner/demo.git')
        fake = type('Result', (), {'returncode': 0,
                                   'stdout': 'ref: refs/heads/main\tHEAD\n'+SHA+'\tHEAD\n'})()
        with patch('vcs_probe.subprocess.run', return_value=fake) as command:
            observed = ls_remote(target)
        self.assertEqual(observed['commit'], SHA)
        args, kwargs = command.call_args
        self.assertEqual(args[0][0], 'git')
        self.assertIn('ls-remote', args[0])
        self.assertNotIn('fetch', args[0])
        self.assertNotIn('clone', args[0])
        self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_pkgver_matching_ref_is_remote_match_only(self):
        result = assess_package(ROW, policy(), approve={},
                                query=lambda _: {'commit': SHA, 'ref': 'refs/heads/main'})
        self.assertEqual(result['status'], 'remote-match')
        self.assertEqual(result['installed_revision_provenance'], 'unknown')

    def test_difference_does_not_claim_upgrade(self):
        result = assess_package(ROW, policy(), approve={},
                                query=lambda _: {'commit': NEW, 'ref': 'refs/heads/main'})
        self.assertEqual(result['status'], 'remote-difference')
        self.assertNotIn('candidate_version', result)

    def test_unreviewed_submodules_block_remote_query(self):
        p = policy(classification='submodule-metadata-candidate', sources=[
            {'source': 'demo::git+https://github.com/owner/demo.git',
             'role': 'primary-candidate', 'rebuild': 'Y'},
            {'source': 'other::git+https://github.com/owner/other.git',
             'role': 'submodule-candidate', 'rebuild': 'N'}])
        def should_not_run(_):
            self.fail('unreviewed candidate contacted remote')
        blocked = assess_package(ROW, p, approve={}, query=should_not_run)
        self.assertEqual(blocked['status'], 'incomplete')
        reviewed = assess_package(ROW, p, approve={'demo-git': 'c'*64},
                                   query=lambda _: {'commit': SHA, 'ref': 'refs/heads/main'})
        self.assertEqual(reviewed['status'], 'remote-match')

    def test_unresolved_secondary_blocks_remote_query(self):
        p = policy(classification='submodule-metadata-candidate', sources=[
            {'source': 'demo::git+https://github.com/owner/demo.git',
             'role': 'primary-candidate', 'rebuild': 'Y'},
            {'source': 'other::git+https://github.com/owner/other.git',
             'role': 'unresolved-git-source', 'rebuild': 'Y'}])
        def should_not_run(_):
            self.fail('unresolved source contacted remote')
        blocked = assess_package(ROW, p, approve={'demo-git': 'c'*64}, query=should_not_run)
        self.assertEqual(blocked['status'], 'incomplete')

    def test_pinned_ref_does_not_query_remote(self):
        p = policy(sources=[{'source': 'git+https://example.org/demo.git#tag=v1',
                             'role': 'primary-candidate', 'rebuild': 'Y'}])
        def should_not_run(_):
            self.fail('pinned ref contacted remote')
        result = assess_package(ROW, p, approve={}, query=should_not_run)
        self.assertEqual(result['status'], 'pinned-source')

    def test_without_installed_hash_incomplete(self):
        p = policy()
        p['installed_version'] = '2.0.0-1'
        row = dict(ROW, installed_version='2.0.0-1')
        result = assess_package(row, p, approve={},
                                query=lambda _: {'commit': SHA, 'ref': 'refs/heads/main'})
        self.assertEqual(result['status'], 'incomplete')

    def test_git_query_failure_is_error_not_current(self):
        def failure(_):
            raise RuntimeError('offline')
        result = assess_package(ROW, policy(), approve={}, query=failure)
        self.assertEqual(result['status'], 'error')


if __name__ == '__main__':
    unittest.main()