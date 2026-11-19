# -*- coding: utf-8 -*-
"""
Локальний запуск без хмари (Python 3, Pillow не обов'язковий).

  python tools/run_local.py check
      Перевірка коректності: паралельна схема (k = 1..6 смуг) попіксельно
      збігається з послідовною; заодно показує, що з перекриттям < 3 рядків
      результат уже неправильний.

  python tools/run_local.py solver --size 1024 --k 4
      Запускає справжній клас Solver із solution.py так, як це робить PARCS,
      але воркери - це локальні процеси (multiprocessing) замість ВМ.

  python tools/run_local.py image photo.pgm --k 4 --out results/local
      Фільтрує ваше PGM-зображення, перевіряє збіг послідовної й паралельної
      версій і зберігає оригінал / розмиття / контури.
"""
import argparse
import json
import multiprocessing
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'solution'))
import solution as s  # noqa: E402


def naive_reference(img, w, h):
    """Пряма реалізація за формулами (повільна, 25 множень на піксель) - для звірки."""
    def px(a, y, x):
        return a[min(max(y, 0), h - 1) * w + min(max(x, 0), w - 1)]
    g = [1, 4, 6, 4, 1]
    blur = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            acc = sum(g[i] * g[j] * px(img, y + i - 2, x + j - 2) for i in range(5) for j in range(5))
            blur[y * w + x] = (acc + 128) >> 8
    kx = ((-1, 0, 1), (-2, 0, 2), (-1, 0, 1))
    ky = ((-1, -2, -1), (0, 0, 0), (1, 2, 1))
    edges = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            gx = sum(kx[i][j] * px(blur, y + i - 1, x + j - 1) for i in range(3) for j in range(3))
            gy = sum(ky[i][j] * px(blur, y + i - 1, x + j - 1) for i in range(3) for j in range(3))
            edges[y * w + x] = min(255, int((gx * gx + gy * gy) ** 0.5 + 1e-9))
    return blur, edges


def cmd_check(_args):
    ok_all = True
    w, h = 61, 47
    img = s.synth_image(w, h, 3)
    ok = s.filter_image(img, w, h) == naive_reference(img, w, h)
    ok_all &= ok
    print('Optimized filter == naive formula (61x47): %s' % ('OK' if ok else 'MISMATCH'))
    for (w, h) in [(64, 48), (257, 131), (512, 512)]:
        img = s.synth_image(w, h, 7)
        blur, edges = s.filter_image(img, w, h)
        for k in range(1, 7):
            pb, pe = s.parallel_local(img, w, h, k)
            ok = (pb == blur and pe == edges)
            ok_all &= ok
            print('%4dx%-4d k=%d halo=3: %s' % (w, h, k, 'OK' if ok else 'MISMATCH'))
    print('\nWhy halo = 3 (512x512, k=4):')
    img = s.synth_image(512, 512, 7)
    _b, edges = s.filter_image(img, 512, 512)
    for halo in range(0, 5):
        _pb, pe = s.parallel_local(img, 512, 512, 4, halo)
        diff = sum(1 for a, b in zip(pe, edges) if a != b)
        print('  halo=%d: %6d different pixels %s' % (halo, diff, '(correct)' if diff == 0 else ''))
    print('\nRESULT: %s' % ('all checks passed' if ok_all else 'FAILED'))


# ---------- імітація PARCS: воркери = процеси ----------
def _remote_mymap(args):
    return s.Solver.mymap(*args)


class _LocalWorker(object):
    """Поводиться як Pyro4-проксі воркера: proxy.mymap(...) -> результат."""

    def __init__(self, pool, idx):
        self.pool, self.idx = pool, idx
        self._pyroUri = type('U', (), {'host': 'local-process-%d' % idx, 'port': idx})()

    def mymap(self, *args):
        return self.pool.apply(_remote_mymap, (args,))


def cmd_solver(args):
    tmp = tempfile.mkdtemp()
    inp, out = os.path.join(tmp, 'input.txt'), os.path.join(tmp, 'output.txt')
    pool = multiprocessing.Pool(max(1, args.k))
    workers = [_LocalWorker(pool, i) for i in range(max(1, args.k))]
    results = {}
    for k in sorted({0, args.k}):
        with open(inp, 'w') as f:
            f.write('mode=bench\nsize=%d\nk=%d\nseed=%d\n' % (args.size, k, args.seed))
        s.Solver(workers, inp, out).solve()
        with open(out) as f:
            results[k] = json.load(f)
    pool.close()
    for k, r in sorted(results.items()):
        print('k=%d: total %.3f s, compute %.3f s, comm %.3f s, md5 %s'
              % (k, r['t_total'], r['t_compute_max'], r['t_comm'], r['md5_edges']))
    same = results[0]['md5_edges'] == results[args.k]['md5_edges']
    print('Parallel == sequential: %s' % same)


def cmd_image(args):
    with open(args.path, 'rb') as f:
        w, h, img, _c = s.parse_pgm(f.read())
    t0 = time.time()
    blur, edges = s.filter_image(img, w, h)
    t_seq = time.time() - t0
    pb, pe = s.parallel_local(img, w, h, args.k)
    print('%s: %dx%d, sequential %.2f s, parallel(k=%d) == sequential: %s'
          % (args.path, w, h, t_seq, args.k, pb == blur and pe == edges))
    os.makedirs(args.out, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.path))[0]
    for name, data in (('original', img), ('blur', blur), ('edges', edges)):
        path = os.path.join(args.out, '%s_%s.pgm' % (base, name))
        with open(path, 'wb') as f:
            f.write(s.make_pgm(w, h, data))
        try:
            from PIL import Image
            Image.open(path).save(path[:-4] + '.png')
        except ImportError:
            pass
        print('  saved %s' % path)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('check')
    p = sub.add_parser('solver')
    p.add_argument('--size', type=int, default=1024)
    p.add_argument('--k', type=int, default=4)
    p.add_argument('--seed', type=int, default=42)
    p = sub.add_parser('image')
    p.add_argument('path')
    p.add_argument('--k', type=int, default=4)
    p.add_argument('--out', default=os.path.join(ROOT, 'results', 'local'))
    args = ap.parse_args()
    {'check': cmd_check, 'solver': cmd_solver, 'image': cmd_image}.get(
        args.cmd or 'check', cmd_check)(args)


if __name__ == '__main__':
    main()
