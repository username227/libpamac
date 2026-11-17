import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from submodule_metadata import (find_submodule_matches, gitmodules_urls,
                                github_metadata_for_primary, normalized_https_repo)


class SubmoduleMetadataTests(unittest.TestCase):
    def test_github_wrapped_base64_response_is_decoded(self):
        text = ('[submodule "third_party/ViPERDSP"]\n'
                '\tpath = third_party/ViPERDSP\n'
                '\turl = https://github.com/likelikeslike/ViPERDSP.git\n')
        doc = {'encoding': 'base64',
               'content': base64.encodebytes(text.encode()).decode()}
        response = io.BytesIO(json.dumps(doc).encode())
        with patch('submodule_metadata.urllib.request.urlopen',
                   return_value=response) as opened:
            fetched = github_metadata_for_primary(
                'https://github.com/username227/Viper4Linux-RE.git')
        self.assertEqual(fetched, text)
        request = opened.call_args.args[0]
        self.assertIn('/repos/username227/Viper4Linux-RE/contents/.gitmodules',
                      request.full_url)

    def test_custom_branch_is_used_in_metadata_request(self):
        text = '[submodule "one"]\n url = https://github.com/example/one.git\n'
        doc = {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        with patch('submodule_metadata.urllib.request.urlopen',
                   return_value=io.BytesIO(json.dumps(doc).encode())) as opened:
            self.assertEqual(github_metadata_for_primary(
                'https://github.com/example/project.git', 'branch=next'), text)
        self.assertIn('ref=next', opened.call_args.args[0].full_url)

    def test_unknown_remote_and_userinfo_do_not_make_network_requests(self):
        with patch('submodule_metadata.urllib.request.urlopen') as opened:
            self.assertIsNone(github_metadata_for_primary(
                'https://evil.example.org/example/project.git'))
            self.assertIsNone(github_metadata_for_primary(
                'https://user:password@github.com/example/project.git'))
            self.assertIsNone(github_metadata_for_primary(
                'https://github.com/example/project.git', 'unknown=flag'))
            opened.assert_not_called()

    def test_excludes_main_and_requires_secondary_url_match(self):
        urls = ('[submodule "deps/ViPERDSP"]\n'
                '  path = deps/ViPERDSP\n'
                '  url = https://github.com/likelikeslike/ViPERDSP.git\n')
        sources = [
            {'alias': 'main', 'url': 'https://github.com/example/main.git',
             'supported': True},
            {'alias': 'ViPERDSP', 'url': 'https://github.com/likelikeslike/ViPERDSP.git',
             'supported': True},
            {'alias': 'other', 'url': 'https://github.com/example/other.git',
             'supported': True},
        ]
        self.assertEqual(find_submodule_matches('main', sources, urls),
                         {'ViPERDSP'})
        self.assertEqual(gitmodules_urls(urls),
                         ['https://github.com/likelikeslike/ViPERDSP.git'])

    def test_exact_host_path_and_case_required(self):
        self.assertEqual(normalized_https_repo(
            'https://github.com/Test/Foo.git'), ('github.com', '/Test/Foo'))
        self.assertNotEqual(normalized_https_repo(
            'https://github.com/Test/Foo'), normalized_https_repo(
                'https://github.com/Test/foo.git'))
        self.assertIsNone(normalized_https_repo('https://github.com.invalid/x/y'))
        self.assertIsNone(normalized_https_repo('https://user@github.com/x/y'))


if __name__ == '__main__':
    unittest.main()
