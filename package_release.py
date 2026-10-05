"""Assemble a clean deliverable without local articles, credentials or sessions."""
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from publisher.version import VERSION
from package_source import source_files

root=Path(__file__).resolve().parent
# Check public inputs before copying anything into a distributable release.
source_files(root)
if not (root/'dist'/'墨笺公众号助手.exe').is_file():
    raise FileNotFoundError('Run python build.py before python package_release.py')
if not (root/'日常记录整理-示例图文包.zip').is_file():
    subprocess.run([sys.executable,'-m','samples.build_example'],cwd=root,check=True)
release=root/'release'/f'墨笺公众号助手-v{VERSION}'
release.mkdir(parents=True,exist_ok=True)
executable=root/'dist'/'墨笺公众号助手.exe'
destination=release/executable.name
if not destination.is_file() or hashlib.sha256(destination.read_bytes()).digest()!=hashlib.sha256(executable.read_bytes()).digest():
    shutil.copy2(executable,destination)
shutil.copy2(root/'README.md',release/'使用说明.md')
shutil.copy2(root/'VALIDATION.md',release/'验证记录.md')
shutil.copy2(root/'日常记录整理-示例图文包.zip',release/'日常记录整理-示例图文包.zip')
shutil.copy2(root/'requirements-lock.txt',release/'requirements-lock.txt')
for name in ('LICENSE','NOTICE.md'):
    shutil.copy2(root/name,release/name)
licenses=[]
for dist in sorted(importlib.metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
    licenses.append('\n'+'='*70+'\n'+dist.metadata['Name']+' '+dist.version+'\n')
    files=[f for f in dist.files or [] if any(t in f.name.lower() for t in ('license','copying','notice')) and f.suffix.lower() in {'.txt','.md','.rst',''}]
    for file in files:
        path=Path(dist.locate_file(file))
        if path.is_file() and path.stat().st_size<2_000_000:
            try:licenses.append(str(file)+'\n'+path.read_text(encoding='utf-8',errors='replace'))
            except OSError:pass
    if not files:
        licenses.append('License metadata: '+str(dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See upstream project.'))
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if python_license.is_file():licenses.append('\nPython runtime license\n'+python_license.read_text(encoding='utf-8',errors='replace'))
(release/'THIRD_PARTY_LICENSES.txt').write_text('\n'.join(licenses),encoding='utf-8')
source_zip=release/'源码与构建脚本.zip'
subprocess.run([sys.executable,str(root/'package_source.py'),'--output',str(source_zip)],cwd=root,check=True)
public_files=[release/name for name in (
    '墨笺公众号助手.exe','使用说明.md','验证记录.md','日常记录整理-示例图文包.zip',
    'requirements-lock.txt','LICENSE','NOTICE.md','THIRD_PARTY_LICENSES.txt',
    '源码与构建脚本.zip','源码与构建脚本.zip.sha256')]
hashes=[hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name for path in public_files]
(release/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='utf-8')
archive=release.parent/f'墨笺公众号助手-v{VERSION}-Windows-x64.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for path in [*public_files,release/'SHA256SUMS.txt']:
        z.write(path,release.name+'/'+path.name)
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
print(json.dumps({'exe':str(release/executable.name),'zip':str(archive),'exe_bytes':executable.stat().st_size,'sha256':hashlib.sha256(executable.read_bytes()).hexdigest()},ensure_ascii=False))
