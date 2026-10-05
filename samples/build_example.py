"""Build the portable Markdown sample with a local, software-rendered cover."""
from pathlib import Path
import tempfile
import zipfile

from publisher.rendering import make_cover

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as temporary:
    assets = Path(temporary)
    image = make_cover('日常记录整理：给一条笔记留好三个位置', 'jade', assets)
    with zipfile.ZipFile(root/'日常记录整理-示例图文包.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(root/'samples'/'日常记录整理.md', '日常记录整理.md')
        archive.write(assets / image.rsplit('/', 1)[-1], 'images/cover.jpg')
