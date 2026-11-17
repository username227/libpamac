#!/usr/bin/env python3
"""Reproducible, read-only AUR development-PKGBUILD classification audit.

Fetch packaging text only; NEVER source or execute PKGBUILDs, call makepkg,
clone application sources, touch pacman/pamac state, or install anything.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import random
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

from vcs_policy import package_policy, parse_srcinfo, source_record, sha256
import submodule_metadata

AUR = 'https://aur.archlinux.org'
VCS_SUFFIXES = ('-git', '-svn', '-hg', '-bzr', '-cvs', '-fossil', '-darcs')
SAFE_NAME = re.compile(r'^[A-Za-z0-9@._+\-]+$')
MAX_INDEX_COMPRESSED = 16_000_000
MAX_INDEX_UNCOMPRESSED = 40_000_000
MAX_ARCHIVE = 10_000_000
MAX_FILE = 1_500_000
MAX_TAR_MEMBERS = 256
MAX_TAR_TOTAL = 20_000_000
USER_AGENT = 'pamac-fix-vcs-research-audit/1.0 (read-only metadata)'


def safe_name(value: str) -> bool:
    return bool(SAFE_NAME.fullmatch(value)) and value not in ('.', '..')


def fetch_limited(url: str, max_bytes: int, timeout: float = 18.0) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f'Remote response exceeded {max_bytes} bytes')
    return data


def load_aur_index(path: Path | None = None) -> tuple[list[str], str]:
    """Return valid unique package names and SHA256 of raw compressed index."""
    raw = path.read_bytes() if path else fetch_limited(AUR + '/packages.gz', MAX_INDEX_COMPRESSED)
    if len(raw) > MAX_INDEX_COMPRESSED:
        raise ValueError('Compressed AUR index exceeded limit')
    # Limit decompression to avoid zip bombs in untrusted remote data.
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        text = stream.read(MAX_INDEX_UNCOMPRESSED + 1)
    if len(text) > MAX_INDEX_UNCOMPRESSED:
        raise ValueError('AUR index decompressed past limit')
    names = []
    seen = set()
    for raw_line in text.decode('utf-8').splitlines():
        name = raw_line.strip()
        if name and not name.startswith('#') and safe_name(name) and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        raise ValueError('AUR index is empty or malformed')
    return sorted(names), hashlib.sha256(raw).hexdigest()


def select_samples(names: list[str], rounds: int, size: int, seed: int,
                   negative_controls: int = 0) -> dict:
    """Uniform random sample, without replacement across rounds; recorded seed.

    Controls use unsuffixed names and are separately labeled; they are NOT
    presumed nondevelopment merely because they lack a conventional suffix.
    """
    if not (1 <= rounds <= 20 and 1 <= size <= 500 and 0 <= negative_controls <= 500):
        raise ValueError('Rounds, sample size or controls outside bounded range')
    vcs = [name for name in names if name.endswith(VCS_SUFFIXES)]
    others = [name for name in names if not name.endswith(VCS_SUFFIXES)]
    if len(vcs) < rounds * size or len(others) < negative_controls:
        raise ValueError('Insufficient population for requested unique sample')
    rng = random.Random(seed)
    picks = rng.sample(vcs, rounds * size)
    groups = [picks[i * size:(i + 1) * size] for i in range(rounds)]
    controls = rng.sample(others, negative_controls)
    return {'schema': 1, 'seed': seed, 'population_size': len(names),
            'vcs_suffix_population_size': len(vcs),
            'selection': 'uniform-package-name-without-replacement',
            'rounds': [{'round': i + 1, 'packages': group} for i, group in enumerate(groups)],
            'negative_controls': controls}


def rpc_bases(package_names: list[str], rate_delay: float = 0.2) -> dict[str, str]:
    """Resolve split package names to the PKGBUILD-owning AUR package base."""
    result: dict[str, str] = {}
    for i in range(0, len(package_names), 40):
        names = package_names[i:i + 40]
        query = urllib.parse.urlencode([('arg[]', n) for n in names])
        raw = fetch_limited(AUR + '/rpc/v5/info?' + query, 1_500_000)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('Unexpected AUR RPC response')
        for item in payload['results']:
            if not isinstance(item, dict):
                continue
            name = item.get('Name')
            base = item.get('PackageBase')
            if name in names and isinstance(base, str) and safe_name(base):
                result[name] = base
        if i + 40 < len(package_names):
            time.sleep(rate_delay)
    return result


def snapshot_packagebase(packagebase: str) -> tuple[str, str, str]:
    """Read PKGBUILD and .SRCINFO from the SAME compressed AUR snapshot."""
    if not safe_name(packagebase):
        raise ValueError('Invalid package base')
    escaped = urllib.parse.quote(packagebase, safe='')
    url = f'{AUR}/cgit/aur.git/snapshot/{escaped}.tar.gz'
    raw = fetch_limited(url, MAX_ARCHIVE)
    result: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        # Never extract archive paths; read just two strictly named text members.
        member_count = 0
        total_declared = 0
        for member in archive:
            member_count += 1
            total_declared += max(0, member.size)
            if member_count > MAX_TAR_MEMBERS or total_declared > MAX_TAR_TOTAL:
                raise ValueError('AUR snapshot exceeds safe member count/size limits')
            if member.name not in (packagebase + '/PKGBUILD', packagebase + '/.SRCINFO'):
                continue
            if not member.isfile() or member.size > MAX_FILE:
                raise ValueError('Unsafe or oversized metadata member')
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError('Could not read metadata member')
            result[member.name.rsplit('/', 1)[-1]] = stream.read(MAX_FILE + 1).decode('utf-8')
    if set(result) != {'PKGBUILD', '.SRCINFO'}:
        raise ValueError('AUR snapshot missing PKGBUILD or .SRCINFO')
    return result['PKGBUILD'], result['.SRCINFO'], hashlib.sha256(raw).hexdigest()


def audit_one(package_name: str, packagebase: str, pkgbuild: str, srcinfo: str,
              *, round_num: int, control: bool = False,
              gitmodules: str | None = None) -> dict:
    """Read-only existing project classifier plus independent safety indicators."""
    declared_base, sources = parse_srcinfo(srcinfo)
    if declared_base != packagebase:
        raise ValueError(f'.SRCINFO packagebase mismatch: {declared_base!r} != {packagebase!r}')
    # The AUR index and RPC can change while the sample is being fetched.
    # Fail closed if a selected split sibling has disappeared from this base.
    if not re.search(r'(?m)^\s*pkgname\s*=\s*' + re.escape(package_name) + r'\s*$',
                     srcinfo):
        raise ValueError('Selected package is absent from current .SRCINFO')
    # This version is AUR packaging metadata, not the installed version!
    # Never populate installed_revision or treat it as actual build provenance.
    policy_row = {'package': package_name, 'installed_version': 'AUDIT-NOT-INSTALLED',
                  'srcinfo': srcinfo, 'pkgbuild': pkgbuild}
    if gitmodules is not None:
        policy_row['gitmodules'] = gitmodules
    p = package_policy(policy_row)
    git_sources = [source_record(s) for s in sources]
    git_sources = [s for s in git_sources if s is not None]
    fixed = [s for s in git_sources if s.get('fragment', '').startswith(('tag=', 'commit='))]
    moving = [s for s in git_sources if not s.get('fragment', '') or
              s.get('fragment', '').startswith('branch=')]
    other_fragments = [s for s in git_sources if s not in moving and s not in fixed]
    n_sources = [s['source'] for s in p['sources'] if s['rebuild'] == 'N']
    red_flags = []
    if n_sources:
        red_flags.append('proposed-N-requires-independent-human-review')
    if '--remote' in pkgbuild and 'submodule' in pkgbuild:
        red_flags.append('PKGBUILD-mentions-submodule-remote')
    if fixed and not moving:
        red_flags.append('no-moving-git-source')
    if other_fragments:
        red_flags.append('unsupported-or-ambiguous-git-fragment')
    if not git_sources:
        red_flags.append('no-git-source-or-unhandled-VCS')
    if re.search(r'(?m)^\s*source_[A-Za-z0-9_]+\s*=', srcinfo):
        red_flags.append('architecture-qualified-sources')
    if p['lightweight_check_enabled']:
        raise ValueError('Safety violation: classifier enabled lightweight check')
    if n_sources and not p['review_required']:
        raise ValueError('Safety violation: N source without required review')
    return {'package': package_name, 'packagebase': packagebase,
            'round': round_num, 'control': control, 'status': 'classified',
            'classification': p['classification'], 'review_required': p['review_required'],
            'source_identification_method': p.get('source_identification_method', 'unresolved'),
            'pkgver_source_reference_confirmed': p.get('pkgver_source_reference_confirmed', False),
            'lightweight_check_enabled': p['lightweight_check_enabled'],
            'source_count': len(sources), 'git_sources': len(git_sources),
            'fixed_git_sources': len(fixed), 'moving_git_sources': len(moving),
            'ambiguous_git_sources': len(other_fragments),
            'n_sources': len(n_sources), 'n_source_names': n_sources,
            'red_flags': red_flags, 'reasons': p['reasons'],
            'pkgbuild_sha256': sha256(pkgbuild), 'srcinfo_sha256': sha256(srcinfo),
            'gitmodules_inspected': gitmodules is not None}


def summarize(rows: list[dict]) -> dict:
    by_round = {}
    for round_num in sorted({r['round'] for r in rows}):
        subset = [r for r in rows if r['round'] == round_num]
        counts = Counter(r.get('classification', 'not-classified') for r in subset)
        status = Counter(r['status'] for r in subset)
        flags = Counter(f for r in subset for f in r.get('red_flags', []))
        by_round[str(round_num)] = {'attempted': len(subset),
                                   'status': dict(sorted(status.items())),
                                   'classification': dict(sorted(counts.items())),
                                   'red_flags': dict(sorted(flags.items())),
                                   'proposed_n_source_count': sum(r.get('n_sources', 0) for r in subset),
                                   'review_required': sum(r.get('review_required', False) for r in subset),
                                   'sole_moving_git_source_identified': sum(
                                       r.get('source_identification_method') == 'sole-moving-git-source'
                                       for r in subset),
                                   'pkgver_source_reference_confirmed': sum(
                                       r.get('pkgver_source_reference_confirmed', False) for r in subset)}
    return {'overall': {'attempted': len(rows),
                        'classified': sum(r['status'] == 'classified' for r in rows),
                        'errors': sum(r['status'] != 'classified' for r in rows),
                        'unique_packagebases': len({r.get('packagebase') for r in rows if r.get('packagebase')}),
                        'auto_authorized_lightweight_checks': sum(r.get('lightweight_check_enabled', False) for r in rows),
                        'sole_moving_git_source_identified': sum(
                            r.get('source_identification_method') == 'sole-moving-git-source'
                            for r in rows),
                        'pkgver_source_reference_confirmed': sum(
                            r.get('pkgver_source_reference_confirmed', False) for r in rows),
                        'ground_truth_validated': False},
            'rounds': by_round}


def run(sample: dict, *, output_dir: Path, delay: float = 0.2,
        offline_dir: Path | None = None,
        inspect_gitmodules: bool = False, max_gitmodules: int = 40) -> dict:
    """Materialize frozen metadata and preserve each selected package outcome."""
    if delay < 0 or delay > 30:
        raise ValueError('Invalid delay')
    if not (0 <= max_gitmodules <= 200):
        raise ValueError('Metadata-check budget outside accepted range')
    if offline_dir and output_dir.resolve() == offline_dir.resolve():
        raise ValueError('Replay output directory must differ from frozen input')
    if output_dir.exists() and any(p.name != 'packages.gz' for p in output_dir.iterdir()):
        raise ValueError('Output directory must be new or empty (except frozen packages.gz index)')
    selections = [(round_info['round'], pkg, False)
                  for round_info in sample['rounds'] for pkg in round_info['packages']]
    selections += [(0, pkg, True) for pkg in sample['negative_controls']]
    names = [pkg for _, pkg, _ in selections]
    if len(set(names)) != len(names):
        raise ValueError('Sample must contain unique names')
    if offline_dir is None:
        bases = rpc_bases(names, rate_delay=delay)
    else:
        base_manifest = json.loads((offline_dir / 'packagebases.json').read_text())
        bases = base_manifest
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'sample.json').write_text(json.dumps(sample, indent=2) + '\n')
    (output_dir / 'packagebases.json').write_text(json.dumps(bases, indent=2, sort_keys=True) + '\n')
    result_rows = []
    metadata_cache = {}
    submodule_cache: dict[str, str | None] = {}
    metadata_checks = 0
    for n, (round_num, name, control) in enumerate(selections, 1):
        base = bases.get(name)
        if not isinstance(base, str) or not safe_name(base):
            result_rows.append({'package': name, 'round': round_num, 'control': control,
                                'status': 'missing-rpc-packagebase',
                                'error': 'Package not present in sampled RPC response'})
            continue
        if base not in metadata_cache:
            try:
                if offline_dir is None:
                    try:
                        pkgbuild, srcinfo, archive_sha = snapshot_packagebase(base)
                    finally:
                        # A missing/blocked snapshot must not cause a burst of retries.
                        time.sleep(delay)
                    directory = output_dir / 'packaging' / base
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / 'PKGBUILD').write_text(pkgbuild)
                    (directory / '.SRCINFO').write_text(srcinfo)
                    (directory / 'archive.sha256').write_text(archive_sha + '\n')
                    (directory / 'text-hashes.json').write_text(json.dumps({
                        'pkgbuild': sha256(pkgbuild), 'srcinfo': sha256(srcinfo)},
                        sort_keys=True) + '\n')
                else:
                    directory = offline_dir / 'packaging' / base
                    pkgbuild = (directory / 'PKGBUILD').read_text()
                    srcinfo = (directory / '.SRCINFO').read_text()
                    archive_sha = (directory / 'archive.sha256').read_text().strip()
                    fingerprints = json.loads((directory / 'text-hashes.json').read_text())
                    if fingerprints != {'pkgbuild': sha256(pkgbuild),
                                         'srcinfo': sha256(srcinfo)}:
                        raise ValueError('Frozen packaging text has changed since capture')
                metadata_cache[base] = (pkgbuild, srcinfo, archive_sha)
            except (OSError, ValueError, UnicodeError, tarfile.TarError) as exc:
                metadata_cache[base] = exc
        data = metadata_cache[base]
        if isinstance(data, Exception):
            result_rows.append({'package': name, 'packagebase': base, 'round': round_num,
                                'control': control, 'status': 'fetch-error',
                                'error': f'{type(data).__name__}: {data}'})
            continue
        pkgbuild, srcinfo, archive_sha = data
        module_text = None
        metadata_status = 'not-requested'
        if inspect_gitmodules:
            try:
                preliminary = package_policy({
                    'package': name, 'installed_version': 'AUDIT-NOT-INSTALLED',
                    'pkgbuild': pkgbuild, 'srcinfo': srcinfo})
                parent = next((x for x in preliminary['sources']
                               if x['role'] == 'primary-candidate' and x['kind'] == 'git'), None)
                secondary = sum(x['kind'] == 'git' and x['role'] != 'primary-candidate'
                                for x in preliminary['sources'])
                if parent and secondary:
                    source = source_record(parent['source'])
                    if source and source.get('supported'):
                        key = source['url'] + '#' + source.get('fragment', '')
                        if key not in submodule_cache:
                            if offline_dir:
                                path = offline_dir / 'packaging' / base / '.gitmodules'
                                submodule_cache[key] = path.read_text() if path.exists() else None
                            elif metadata_checks < max_gitmodules:
                                metadata_checks += 1
                                submodule_cache[key] = submodule_metadata.github_metadata_for_primary(
                                    source['url'], source.get('fragment', ''))
                            else:
                                metadata_status = 'skipped-budget'
                        if key in submodule_cache:
                            module_text = submodule_cache[key]
                            metadata_status = 'retrieved-unreviewed' if module_text else 'not-available'
                            if module_text and offline_dir is None:
                                (output_dir / 'packaging' / base / '.gitmodules').write_text(module_text)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                metadata_status = 'metadata-error:' + str(exc)
        try:
            row = audit_one(name, base, pkgbuild, srcinfo,
                            round_num=round_num, control=control,
                            gitmodules=module_text)
            row['gitmodules_status'] = metadata_status
            row['archive_sha256'] = archive_sha
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row = {'package': name, 'packagebase': base, 'round': round_num,
                   'control': control, 'status': 'classification-error',
                   'error': f'{type(exc).__name__}: {exc}'}
        result_rows.append(row)
        if n % 25 == 0:
            print(f'Audited {n}/{len(selections)} AUR package names', file=sys.stderr)
    report = {'schema': 1, 'purpose': 'classification-research-only',
              'generated_utc': datetime.now(timezone.utc).isoformat(),
              'metadata_inspection_attempts': metadata_checks,
              'index_sha256': sample.get('index_sha256'),
              'sampling': sample, 'summary': summarize(result_rows), 'packages': result_rows,
              'interpretation': 'No ground-truth certification or live A/B update decisions'}
    (output_dir / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    with (output_dir / 'results.csv').open('w', newline='', encoding='utf-8') as stream:
        keys = ('round', 'package', 'packagebase', 'control', 'status',
                'classification', 'source_identification_method',
                'pkgver_source_reference_confirmed', 'review_required',
                'source_count', 'git_sources',
                'moving_git_sources', 'fixed_git_sources', 'n_sources', 'error')
        writer = csv.DictWriter(stream, fieldnames=keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(result_rows)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--index-file', type=Path,
                        help='Previously downloaded packages.gz (offline index)')
    source.add_argument('--replay', type=Path,
                        help='Directory from previous audit: use frozen sample and PKGBUILDs')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--sample-size', type=int, default=200)
    parser.add_argument('--negative-controls', type=int, default=0)
    parser.add_argument('--seed', type=int, default=20261117)
    parser.add_argument('--inspect-gitmodules', action='store_true',
                        help='Optional bounded advisory GitHub metadata inspection')
    parser.add_argument('--max-gitmodules', type=int, default=40,
                        help='At most 40 additional GitHub metadata requests by default')
    parser.add_argument('--delay', type=float, default=0.3,
                        help='Delay between archive downloads (seconds)')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.replay:
            sample = json.loads((args.replay / 'sample.json').read_text())
        else:
            if args.output_dir.exists() and any(args.output_dir.iterdir()):
                raise ValueError('Output directory must be new or empty')
            # Always freeze the complete original index, even when supplied
            # by the caller, so audit selection can be independently rerun.
            args.output_dir.mkdir(parents=True, exist_ok=True)
            frozen_index = args.output_dir / 'packages.gz'
            if args.index_file is None:
                frozen_index.write_bytes(fetch_limited(AUR + '/packages.gz', MAX_INDEX_COMPRESSED))
            else:
                shutil.copyfile(args.index_file, frozen_index)
            names, index_sha = load_aur_index(frozen_index)
            sample = select_samples(names, args.rounds, args.sample_size,
                                    args.seed, args.negative_controls)
            sample['index_sha256'] = index_sha
            sample['index_retrieved_utc'] = datetime.now(timezone.utc).isoformat()
        report = run(sample, output_dir=args.output_dir, delay=args.delay,
                     offline_dir=args.replay,
                     inspect_gitmodules=args.inspect_gitmodules,
                     max_gitmodules=args.max_gitmodules)
        print(json.dumps(report['summary'], indent=2, sort_keys=True))
        # A complete classification does not mean correctness is proven.
        return 0 if report['summary']['overall']['errors'] == 0 else 2
    except (OSError, ValueError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        print(f'AUR random audit failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())