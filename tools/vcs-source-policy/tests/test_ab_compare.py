import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_compare import compare, load_capture, InvalidCapture, CONTEXT_FIELDS


STATE = dict.fromkeys(CONTEXT_FIELDS, 'a' * 64)


def capture(method, statuses, state=None):
    return {'schema': 1, 'method': method, 'state': state or STATE,
            'packages': [dict(package=name, packagebase='test-base',
                              installed_version='r1-1', status=status,
                              **({'candidate_version': 'r2-1'} if status == 'update' else {}))
                         for name, status in statuses.items()]}


def indexes(old, new):
    return {'state': old['state'], 'packages': {x['package']: x for x in old['packages']}}, \
           {'state': new['state'], 'packages': {x['package']: x for x in new['packages']}}


class ABTests(unittest.TestCase):
    def test_identical_update_and_current(self):
        a, b = indexes(capture('legacy', {'x-git': 'update', 'y-git': 'current'}),
                       capture('lightweight', {'x-git': 'update', 'y-git': 'current'}))
        result = compare(a, b)
        self.assertTrue(result['matched_without_discrepancies'])
        self.assertEqual(result['counts'], {'match-current': 1, 'match-update': 1})

    def test_missing_upgrade_is_not_pass(self):
        a, b = indexes(capture('legacy', {'x-git': 'update'}),
                       capture('lightweight', {'x-git': 'current'}))
        result = compare(a, b)
        self.assertFalse(result['matched_without_discrepancies'])
        self.assertEqual(result['counts'], {'potential-missed-upgrade': 1})

    def test_extra_rebuild_is_not_pass(self):
        a, b = indexes(capture('legacy', {'x-git': 'current'}),
                       capture('lightweight', {'x-git': 'update'}))
        self.assertEqual(compare(a, b)['counts'], {'potential-extra-rebuild': 1})

    def test_version_disagreement(self):
        a, b = indexes(capture('legacy', {'x-git': 'update'}),
                       capture('lightweight', {'x-git': 'update'}))
        b['packages']['x-git']['candidate_version'] = 'r3-1'
        self.assertEqual(compare(a, b)['counts'], {'version-mismatch': 1})

    def test_remote_snapshots_mismatch_makes_all_incomparable(self):
        newer = dict(STATE, remote_state_sha256='b' * 64)
        a, b = indexes(capture('legacy', {'x-git': 'current'}),
                       capture('lightweight', {'x-git': 'current'}, newer))
        result = compare(a, b)
        self.assertFalse(result['comparable'])
        self.assertEqual(result['counts'], {'inconclusive': 1})

    def test_incomplete_result_is_never_a_pass(self):
        a, b = indexes(capture('legacy', {'x-git': 'incomplete'}),
                       capture('lightweight', {'x-git': 'incomplete'}))
        self.assertFalse(compare(a, b)['matched_without_discrepancies'])

    def test_split_package_member_missing_from_scan_is_inconclusive(self):
        a, b = indexes(capture('legacy', {'x-git': 'update'}),
                       capture('lightweight', {}))
        self.assertEqual(compare(a, b)['counts'], {'inconclusive': 1})

    def test_invalid_capture_and_duplicate_rejected(self):
        record = capture('legacy', {'x-git': 'current'})
        record['packages'].append(record['packages'][0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'old.json'
            path.write_text(json.dumps(record))
            with self.assertRaises(InvalidCapture):
                load_capture(path, 'legacy')

    def test_missing_state_evidence_rejected(self):
        record = capture('legacy', {'x-git': 'current'})
        del record['state']['remote_state_sha256']
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'old.json'
            path.write_text(json.dumps(record))
            with self.assertRaises(InvalidCapture):
                load_capture(path, 'legacy')


if __name__ == '__main__':
    unittest.main()