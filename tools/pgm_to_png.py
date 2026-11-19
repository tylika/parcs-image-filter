# -*- coding: utf-8 -*-
"""
Перетворює всі .pgm у папці (рекурсивно) на .png поруч. Потрібен Pillow.

  python tools/pgm_to_png.py results
"""
import os
import sys

try:
    from PIL import Image
except ImportError:
    raise SystemExit('Pillow is not installed. Run:  python -m pip install pillow')

root = sys.argv[1] if len(sys.argv) > 1 else 'results'
n = 0
for dirpath, _dirs, files in os.walk(root):
    for name in files:
        if name.lower().endswith('.pgm'):
            src = os.path.join(dirpath, name)
            Image.open(src).save(src[:-4] + '.png')
            n += 1
print('Converted %d file(s) in %s' % (n, root))
