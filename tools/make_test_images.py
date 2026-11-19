# -*- coding: utf-8 -*-
"""
Генерує синтетичні тестові зображення (ті самі, що будує майстер PARCS
з параметрами size/seed) і зберігає у PGM (+PNG, якщо є Pillow).

  python tools/make_test_images.py                      # 1024 2048 4096
  python tools/make_test_images.py --sizes 512 --out data
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'solution'))
import solution as s  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sizes', type=int, nargs='+', default=[1024, 2048, 4096])
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--out', default=os.path.join(ROOT, 'data'))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for n in args.sizes:
        img = s.synth_image(n, n, args.seed)
        path = os.path.join(args.out, 'synthetic_%d.pgm' % n)
        with open(path, 'wb') as f:
            f.write(s.make_pgm(n, n, img))
        msg = path
        try:
            from PIL import Image
            Image.open(path).save(path[:-4] + '.png')
            msg += ' (+png)'
        except ImportError:
            pass
        print('%s  md5=%s' % (msg, s.md5(img)))


if __name__ == '__main__':
    main()
