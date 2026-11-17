import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_observe import legacy_lines, compare_observations


def probe(status, version='r4.abc0123-1'):
    return {'schema': 1, 'purpose': 'research-remote-observation-only',
            'packages': [{'package': 'demo-git', 'installed_version': version,
                          'status': status}]}


class ObservationalTests(unittest.TestCase):
    def test_quiet_output_format_from_pamac_fork(self):
        records, unknown = legacy_lines('demo-git  r4.abc0123-1 -> r5.fffffff-1\n')
        self.assertEqual(records['demo-git']['candidate_version'], 'r5.fffffff-1')
        self.assertEqual(unknown, [])

    def test_remote_difference_with_legacy_update_is_diagnostic_not_pass(self):
        result = compare_observations(probe('remote-difference'),
                                     'demo-git  r4.abc0123-1 -> r5.fffffff-1\n', 100)
        self.assertEqual(result['results'][0]['category'], 'both-signal-possible-update')
        self.assertFalse(result['strictly_comparable'])
        self.assertFalse(result['validated_ab_pass'])

    def test_no_update_still_not_strict_pass(self):
        result = compare_observations(probe('remote-match'), '', 0)
        self.assertEqual(result['results'][0]['category'], 'both-show-no-remote-update')
        self.assertFalse(result['validated_ab_pass'])

    def test_vcs_incomplete_exit_cannot_pass(self):
        result = compare_observations(probe('remote-match'), '', 102)
        self.assertEqual(result['results'][0]['category'], 'inconclusive')

    def test_unlisted_new_remote_flagged(self):
        result = compare_observations(probe('remote-difference'), '', 0)
        self.assertEqual(result['results'][0]['category'], 'possible-missed-update')

    def test_legacy_version_drift_inconclusive(self):
        result = compare_observations(probe('remote-difference'),
                                     'demo-git  r3.old-1 -> r5.new-1', 100)
        self.assertEqual(result['results'][0]['category'], 'inconclusive')

    def test_duplicate_legacy_rows_rejected(self):
        with self.assertRaises(ValueError):
            legacy_lines('demo-git  old -> new\ndemo-git  old -> new\n')


if __name__ == '__main__':
    unittest.main()