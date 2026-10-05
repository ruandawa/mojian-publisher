"""Package repository sources using an explicit allowlist, never workspace data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from publisher.version import VERSION

ROOT_FILES = (
    'launcher.py', 'build.py', 'package_release.py', 'package_source.py',
    'requirements.txt', 'requirements-lock.txt', 'README.md', 'LICENSE',
    'NOTICE.md', 'THIRD_PARTY_LICENSES.txt', 'CONTRIBUTING.md', 'SECURITY.md',
    'VALIDATION.md', '.gitignore', '.gitattributes',
)
SOURCE_DIRS = ('publisher', 'web', 'tests', 'samples', 'docs', '.github')
SOURCE_SUFFIXES = {'.py', '.js', '.css', '.html', '.svg', '.md', '.yml', '.yaml'}
FORBIDDEN_PARTS = {
    'data', 'assets', 'imports', 'evidence', 'validation', 'release', 'dist',
    '.venv', 'venv', 'build', 'build-assets', 'wechat-profile', 'workbench-profile',
    '__pycache__', '.git',
}


def source_files(root: Path) -> list[Path]:
    files = []
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f'Required public source file is missing: {name}')
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Required file is a link or outside repository: {name}')
        files.append(path)
    for directory in SOURCE_DIRS:
        folder = root / directory
        if not folder.is_dir():
            raise FileNotFoundError(f'Required source directory is missing: {directory}')
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(root)
            if any(part.casefold() in FORBIDDEN_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'Symlinks are not packaged: {relative}')
            if path.is_file() and path.suffix.casefold() in SOURCE_SUFFIXES:
                if not path.resolve().is_relative_to(root):
                    raise ValueError(f'File is outside repository: {relative}')
                files.append(path)
    # License texts may include upstream contact addresses; scan project files.
    private_patterns = (
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
        re.compile(r'\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{20,}'),
        re.compile(r'(?i)[A-Z]:[\\/]Users[\\/](?!Public\b)[^\\/\s\x22\x27]+'),
    )
    for path in files:
        if path.name == 'THIRD_PARTY_LICENSES.txt':
            continue
        value = path.read_text(encoding='utf-8-sig')
        if any(pattern.search(value) for pattern in private_patterns):
            raise ValueError(f'Potential private value in {path.relative_to(root)}; review before packaging')
    return sorted(set(files))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Destination ZIP (default: release directory)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    files = source_files(root)
    output = args.output or root / 'release' / f'mojian-publisher-v{VERSION}-github-source.zip'
    output = output.resolve()
    if output in files:
        raise ValueError('Output must not replace an input file')
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = f'mojian-publisher-v{VERSION}/'
    checksums = []
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            archive.writestr(prefix + relative, data)
            checksums.append(hashlib.sha256(data).hexdigest() + '  ' + relative)
        archive.writestr(prefix + 'SHA256SUMS.txt', '\n'.join(checksums) + '\n')
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP integrity check failed')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    print(json.dumps({'zip': str(output), 'source_files': len(files),
                      'bytes': output.stat().st_size, 'sha256': digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
