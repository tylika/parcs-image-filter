# -*- coding: utf-8 -*-
"""
Конвертує фото (JPG/PNG/...) у відтінки сірого у форматі PGM (P5).
Потрібен Pillow:  pip install pillow

  python tools/photo_to_pgm.py photo.jpg photo.pgm
  python tools/photo_to_pgm.py photo.jpg photo.pgm --max-side 2048
  python tools/photo_to_pgm.py photo.jpg photo_1024.pgm --square 1024
"""
import argparse

try:
    from PIL import Image, ImageOps
except ImportError:
    raise SystemExit('Pillow is not installed. Run:  python -m pip install pillow')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--max-side', type=int, default=0, help='downscale so that the longer side <= N')
    ap.add_argument('--square', type=int, default=0, help='center-crop to a square and resize to NxN')
    args = ap.parse_args()

    im = Image.open(args.src)
    im = ImageOps.exif_transpose(im).convert('L')
    if args.square:
        im = ImageOps.fit(im, (args.square, args.square), Image.LANCZOS)
    elif args.max_side and max(im.size) > args.max_side:
        im.thumbnail((args.max_side, args.max_side), Image.LANCZOS)
    im.save(args.dst, format='PPM')  # для режиму 'L' Pillow пише бінарний PGM (P5)
    print('%s -> %s (%dx%d, grayscale)' % (args.src, args.dst, im.width, im.height))


if __name__ == '__main__':
    main()
