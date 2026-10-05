"""Reproducible local Windows packaging entrypoint."""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

root=Path(__file__).resolve().parent
out=root/'build-assets'
out.mkdir(exist_ok=True)
image=Image.new('RGBA',(256,256),(0,0,0,0))
draw=ImageDraw.Draw(image)
draw.rounded_rectangle((0,0,255,255),60,fill='#286d54')
draw.rounded_rectangle((74,48,181,207),12,fill='#f1eee3')
for y,width in [(87,61),(121,61),(155,42)]:
    draw.line((97,y,97+width,y),fill='#286d54',width=11)
image.save(out/'mojian.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
image.save(out/'mojian.png')
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
         '--name','墨笺公众号助手','--icon',str(out/'mojian.ico'),
         '--add-data',str(root/'web')+';web','--collect-all','playwright',
         '--add-data',str(out/'mojian.ico')+';.',
         '--hidden-import','webview.platforms.winforms','--hidden-import','uvicorn.loops.asyncio',
         '--hidden-import','uvicorn.protocols.http.h11_impl',
         '--hidden-import','uvicorn.protocols.websockets.websockets_impl',
         '--hidden-import','uvicorn.lifespan.on',str(root/'launcher.py')]
subprocess.run(command,cwd=root,check=True)
