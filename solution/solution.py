# -*- coding: utf-8 -*-
"""
PARCS-розв'язок: паралельна фільтрація зображень
(Гауссове розмиття 5x5 + модуль градієнта Собеля).

Файл самодостатній: без сторонніх бібліотек (у Docker-образі PARCS немає Pillow
та numpy), працює і в Python 2.7 (вузли PARCS), і в Python 3 (локальні тести).

Схема (map-reduce):
  * майстер ділить зображення на k горизонтальних смуг; до кожної смуги додає
    перекриття (halo) по HALO=3 рядки зверху і знизу;
  * воркер (mymap) фільтрує свою смугу як окреме зображення;
  * майстер (myreduce) відкидає рядки перекриття і склеює результат.

Halo = 3, бо Гаусс 5x5 бере +-2 рядки, а Собель 3x3 по розмитому - ще +-1.

Формат вхідного файла PARCS (input.txt) - або текст key=value:
    mode=bench   # bench - лише заміри; visual - ще й повернути картинки
    size=2048    # розмір синтетичного зображення size x size
    k=4          # кількість воркерів; 0 = послідовна версія на майстрі
    seed=42
    halo=3
  або бінарний PGM (P5) з параметрами в коментарі заголовка:
    P5
    # parcs: mode=visual k=4
    1024 768
    255
    <пікселі>
"""
from __future__ import print_function, division

import base64
import binascii
import hashlib
import json
import random
import socket
import threading
import time
import traceback

try:  # Python 2: ледачий zip, щоб не будувати зайві списки
    from itertools import izip as zip  # noqa: A001
except ImportError:  # Python 3
    pass

try:
    from Pyro4 import expose
except ImportError:  # локальний запуск без PARCS
    def expose(func):
        return func

HALO = 3

# --------------------------------------------------------------------------
#  Таблиці
# --------------------------------------------------------------------------
# SQ[v] = min(255, floor(sqrt(v))) для v = Gx^2 + Gy^2 (|Gx|,|Gy| <= 1020)
_SQ_N = 2 * 1020 * 1020 + 1
SQ = bytearray(b'\xff' * _SQ_N)
for _k in range(255):
    SQ[_k * _k:(_k + 1) * (_k + 1)] = bytearray([_k]) * (2 * _k + 1)

# Обмеження шуму для генератора: CLIP16[v] = clamp(v - 16, 0, 255)
CLIP16 = bytearray([min(255, max(0, _v - 16)) for _v in range(256 + 32)])


# --------------------------------------------------------------------------
#  Ядра фільтрів (рядкові, без циклів по пікселях на рівні Python)
# --------------------------------------------------------------------------
def hblur(row):
    """Горизонтальний прохід Гаусса [1 4 6 4 1], край - повтор пікселя.
    Повертає ненормовані суми (x16)."""
    p = [row[0], row[0]] + list(row) + [row[-1], row[-1]]
    return [a + e + 4 * (b + d) + 6 * c
            for a, b, c, d, e in zip(p, p[1:], p[2:], p[3:], p[4:])]


def vblur(h0, h1, h2, h3, h4):
    """Вертикальний прохід Гаусса + нормування на 256 з округленням."""
    return bytearray([(a + e + 4 * (b + d) + 6 * c + 128) >> 8
                      for a, b, c, d, e in zip(h0, h1, h2, h3, h4)])


def sobel_row(u, m, d):
    """Модуль градієнта Собеля для рядка m (u - рядок вище, d - нижче).
    Gx = [-1 0 1; -2 0 2; -1 0 1],  Gy = [-1 -2 -1; 0 0 0; 1 2 1].
    Ядра розкладені на вертикальну та горизонтальну частини."""
    c = [a + 2 * b + e for a, b, e in zip(u, m, d)]   # згладжування по вертикалі
    t = [e - a for a, e in zip(u, d)]                 # різниця по вертикалі
    c = [c[0]] + c + [c[-1]]
    t = [t[0]] + t + [t[-1]]
    gx = [r - l for l, r in zip(c, c[2:])]
    gy = [l + 2 * s + r for l, s, r in zip(t, t[1:], t[2:])]
    return bytearray([SQ[x * x + y * y] for x, y in zip(gx, gy)])


def _clamp(i, n):
    return 0 if i < 0 else (n - 1 if i >= n else i)


# --------------------------------------------------------------------------
#  Послідовний алгоритм: повне розмиття, потім Собель по всьому зображенню
# --------------------------------------------------------------------------
def gaussian_blur(img, w, h):
    """Гауссове розмиття 5x5 усього зображення (bytearray w*h -> bytearray)."""
    out = bytearray(w * h)

    def hrow(i):
        i = _clamp(i, h)
        return hblur(img[i * w:(i + 1) * w])

    win = [hrow(-2), hrow(-1), hrow(0), hrow(1), hrow(2)]   # ковзне вікно 5 рядків
    for y in range(h):
        out[y * w:(y + 1) * w] = vblur(win[0], win[1], win[2], win[3], win[4])
        if y + 1 < h:
            win.pop(0)
            win.append(hrow(y + 3))
    return out


def sobel(img, w, h):
    """Модуль градієнта Собеля усього зображення."""
    out = bytearray(w * h)

    def row(i):
        i = _clamp(i, h)
        return img[i * w:(i + 1) * w]

    for y in range(h):
        out[y * w:(y + 1) * w] = sobel_row(row(y - 1), row(y), row(y + 1))
    return out


def filter_image(img, w, h):
    """Послідовна версія: розмиття, потім контури. Повертає (blur, edges)."""
    blurred = gaussian_blur(img, w, h)
    edges = sobel(blurred, w, h)
    return blurred, edges


# --------------------------------------------------------------------------
#  Розбиття на смуги з перекриттям і склеювання
# --------------------------------------------------------------------------
def split_bands(h, k, halo=HALO):
    """Повертає список (y0, y1, top, bottom): власні рядки смуги [y0, y1)
    і рядки, що передаються воркеру, [top, bottom) з перекриттям halo."""
    bands = []
    for i in range(k):
        y0 = (i * h) // k
        y1 = ((i + 1) * h) // k
        bands.append((y0, y1, max(0, y0 - halo), min(h, y1 + halo)))
    return bands


def merge_bands(w, h, bands, results):
    """Відкидає рядки перекриття і склеює смуги в одне зображення."""
    out = bytearray(w * h)
    for (y0, y1, top, _bottom), part in zip(bands, results):
        skip = (y0 - top) * w                 # рядки halo зверху
        out[y0 * w:y1 * w] = part[skip:skip + (y1 - y0) * w]
    return out


def parallel_local(img, w, h, k, halo=HALO):
    """Паралельна схема без мережі (для перевірки коректності)."""
    bands = split_bands(h, k, halo)
    blur_parts, edge_parts = [], []
    for (_y0, _y1, top, bottom) in bands:
        b, e = filter_image(img[top * w:bottom * w], w, bottom - top)
        blur_parts.append(b)
        edge_parts.append(e)
    return merge_bands(w, h, bands, blur_parts), merge_bands(w, h, bands, edge_parts)


# --------------------------------------------------------------------------
#  Синтетичне тестове зображення (детерміноване, швидке)
# --------------------------------------------------------------------------
def _randbytes(rnd, n):
    hx = '%0*x' % (2 * n, rnd.getrandbits(8 * n))
    return bytearray(binascii.unhexlify(hx))


def _pick(rnd, seq):
    # однаково в Python 2 і 3 (random.choice у них реалізований по-різному)
    return seq[int(rnd.random() * len(seq))]


def synth_image(w, h, seed=42, noise=True):
    """Градієнт + прямокутники, кола, кільця, смуги + рівномірний шум +-16.
    Фігури задані у відносних координатах, тож картинка однакова для всіх розмірів."""
    rnd = random.Random(seed)
    base = bytearray([40 + (x * 120) // max(1, w - 1) for x in range(w)])
    shades = [bytearray([min(255, v + dy) for v in range(256)]) for dy in range(61)]

    rects, circles, rings = [], [], []
    for _ in range(6):
        x0, y0 = rnd.uniform(0.0, 0.8), rnd.uniform(0.0, 0.8)
        rects.append((x0, y0, x0 + rnd.uniform(0.08, 0.2), y0 + rnd.uniform(0.08, 0.2),
                      _pick(rnd, [10, 30, 200, 230, 250])))
    for _ in range(6):
        circles.append((rnd.uniform(0.1, 0.9), rnd.uniform(0.1, 0.9),
                        rnd.uniform(0.04, 0.12), _pick(rnd, [0, 20, 215, 245])))
    for _ in range(2):
        rings.append((rnd.uniform(0.3, 0.7), rnd.uniform(0.3, 0.7),
                      rnd.uniform(0.15, 0.3), 0.012, 255))
    stripes = (0.05, 0.75, 0.45, 0.95)  # блок вертикальних смуг (x0, y0, x1, y1)

    img = bytearray(w * h)
    for y in range(h):
        fy = (y + 0.5) / h
        row = base.translate(shades[(y * 60) // max(1, h - 1)])
        for (x0, y0, x1, y1, val) in rects:
            if y0 <= fy < y1:
                a, b = int(x0 * w), int(x1 * w)
                row[a:b] = bytearray([val]) * (b - a)
        if stripes[1] <= fy < stripes[3]:
            period = max(4, w // 64)
            for xs in range(int(stripes[0] * w), int(stripes[2] * w), 2 * period):
                xe = min(xs + period, int(stripes[2] * w))
                row[xs:xe] = bytearray([235]) * (xe - xs)
        for (cx, cy, r, val) in circles:
            dy = abs(fy - cy) * h
            rr = r * w
            if dy < rr:
                half = (rr * rr - dy * dy) ** 0.5
                a = max(0, int(cx * w - half))
                b = min(w, int(cx * w + half))
                if b > a:
                    row[a:b] = bytearray([val]) * (b - a)
        for (cx, cy, r, thick, val) in rings:
            dy = abs(fy - cy) * h
            ro, ri = r * w, (r - thick) * w
            if dy < ro:
                ho = (ro * ro - dy * dy) ** 0.5
                hi = (ri * ri - dy * dy) ** 0.5 if dy < ri else 0.0
                for a, b in ((cx * w - ho, cx * w - hi), (cx * w + hi, cx * w + ho)):
                    a, b = max(0, int(a)), min(w, int(b))
                    if b > a:
                        row[a:b] = bytearray([val]) * (b - a)
        if noise:
            nz = _randbytes(rnd, w)
            row = bytearray([CLIP16[a + (b & 31)] for a, b in zip(row, nz)])
        img[y * w:(y + 1) * w] = row
    return img


# --------------------------------------------------------------------------
#  PGM (P5)
# --------------------------------------------------------------------------
def parse_pgm(raw):
    """Повертає (w, h, pixels: bytearray, comments: list[str])."""
    buf = bytearray(raw)
    pos, n = 0, len(buf)
    tokens, comments = [], []
    ws = (9, 10, 13, 32)
    while len(tokens) < 4:
        if pos >= n:
            raise ValueError('Bad PGM header')
        c = buf[pos]
        if c == 35:  # '#'
            end = buf.find(b'\n', pos)
            end = n if end < 0 else end
            comments.append(bytes(buf[pos + 1:end]).decode('ascii', 'replace').strip())
            pos = end + 1
        elif c in ws:
            pos += 1
        else:
            start = pos
            while pos < n and buf[pos] not in ws and buf[pos] != 35:
                pos += 1
            tokens.append(bytes(buf[start:pos]).decode('ascii'))
    if tokens[0] != 'P5':
        raise ValueError('Only binary PGM (P5) is supported')
    w, h, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval > 255:
        raise ValueError('Only 8-bit PGM is supported')
    pos += 1  # один пробільний символ після maxval
    pixels = buf[pos:pos + w * h]
    if len(pixels) != w * h:
        raise ValueError('PGM is truncated')
    return w, h, pixels, comments


def make_pgm(w, h, pixels, comment=None):
    header = 'P5\n'
    if comment:
        header += '# %s\n' % comment
    header += '%d %d\n255\n' % (w, h)
    return header.encode('ascii') + bytes(pixels)


def md5(data):
    return hashlib.md5(bytes(data)).hexdigest()


def _b64(data):
    return base64.b64encode(bytes(data)).decode('ascii')


def _unb64(text):
    return bytearray(base64.b64decode(str(text)))


def pack(data, transport):
    """raw - сирі байти (серіалізатор marshal), b64 - текст base64 (serpent)."""
    return bytes(data) if transport == 'raw' else _b64(data)


def unpack(data, transport):
    return bytearray(data) if transport == 'raw' else _unb64(data)


def _set_serializer(proxies, name):
    try:
        import Pyro4
        Pyro4.config.SERIALIZER = name
    except Exception:  # noqa: BLE001 - локальний запуск без Pyro4
        pass
    for p in proxies:
        try:
            p._pyroSerializer = name
        except Exception:  # noqa: BLE001
            pass


def parse_params(text):
    params = {}
    for chunk in text.replace(';', '\n').replace(' ', '\n').split('\n'):
        chunk = chunk.split('#', 1)[0].strip()
        if '=' in chunk:
            key, val = chunk.split('=', 1)
            params[key.strip().lower()] = val.strip()
    return params


def load_input(raw):
    """raw - байти input.txt. Повертає (w, h, img, params, source)."""
    head = bytes(bytearray(raw)[:2])
    if head == b'P5':
        w, h, img, comments = parse_pgm(raw)
        params = {}
        for c in comments:
            if c.lower().startswith('parcs:'):
                params.update(parse_params(c[6:]))
        return w, h, img, params, 'pgm'
    params = parse_params(bytes(raw).decode('utf-8', 'replace'))
    size = int(params.get('size', 1024))
    w = int(params.get('width', size))
    h = int(params.get('height', size))
    if w * h > 8192 * 8192:
        raise ValueError('Image %dx%d is too large' % (w, h))
    img = synth_image(w, h, int(params.get('seed', 42)))
    return w, h, img, params, 'synthetic'


# --------------------------------------------------------------------------
#  Виклик воркерів PARCS (Pyro4) у паралельних потоках
# --------------------------------------------------------------------------
def _local_ip_towards(host):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _proxy_host(proxy):
    try:
        uri = proxy._pyroUri
        return str(uri.host), int(uri.port)
    except Exception:
        return str(proxy), 0


def order_workers(workers):
    """Стабільний порядок воркерів; воркер на тій самій ВМ, що й майстер
    (якщо такий є), ставимо в кінець."""
    hosts = [_proxy_host(p) for p in workers]
    my_ip = _local_ip_towards(hosts[0][0]) if hosts else None
    items = sorted(zip(hosts, workers), key=lambda hw: (hw[0][0] == my_ip, hw[0]))
    return [p for _h, p in items], [h for h, _p in items], my_ip


class _Call(threading.Thread):
    """Синхронний виклик віддаленого методу в окремому потоці.
    Потоки дають паралельність незалежно від того, async-проксі чи ні."""

    def __init__(self, proxy, method, args):
        threading.Thread.__init__(self)
        self.daemon = True
        self.proxy, self.method, self.args = proxy, method, args
        self.result, self.error, self.elapsed = None, None, 0.0

    def run(self):
        t0 = time.time()
        try:
            p = self.proxy
            for name, arg in (('_pyroClaimOwnership', ()), ('_pyroAsync', (False,))):
                fn = getattr(p, name, None)
                if fn is not None:
                    try:
                        fn(*arg)
                    except Exception:
                        pass
            res = getattr(p, self.method)(*self.args)
            if not isinstance(res, dict) and hasattr(res, 'value'):
                res = res.value          # FutureResult асинхронного проксі
            self.result = res
        except Exception as e:  # noqa: BLE001
            self.error = '%s: %s' % (type(e).__name__, e)
        self.elapsed = time.time() - t0


# --------------------------------------------------------------------------
#  Клас для PARCS
# --------------------------------------------------------------------------
class Solver:
    def __init__(self, workers=None, input_file_name=None, output_file_name=None):
        self.input_file_name = input_file_name
        self.output_file_name = output_file_name
        self.workers = workers or []
        print('Inited')

    # --- майстер ---
    def solve(self):
        try:
            self._solve()
        except Exception as e:  # PARCS показує лише str(e) - додаємо тип помилки
            traceback.print_exc()
            raise Exception('%s: %s' % (type(e).__name__, e))

    def _solve(self):
        print('Job Started')
        t_job = time.time()
        with open(self.input_file_name, 'rb') as f:
            raw = f.read()
        t0 = time.time()
        w, h, img, params, source = load_input(raw)
        t_load = time.time() - t0
        mode = params.get('mode', 'bench').lower()
        halo = int(params.get('halo', HALO))
        k = int(params.get('k', len(self.workers)))
        print('Image %dx%d (%s), mode=%s, k=%d, available workers=%d'
              % (w, h, source, mode, k, len(self.workers)))

        report = {
            'width': w, 'height': h, 'pixels': w * h, 'source': source,
            'mode': mode, 'k': k, 'halo': halo, 'seed': params.get('seed', '42'),
            'available_workers': len(self.workers), 't_load': t_load,
        }
        if k <= 0:
            blurred, edges = self.sequential(img, w, h, report)
        else:
            if k > len(self.workers):
                raise Exception('Requested k=%d workers, only %d connected'
                                % (k, len(self.workers)))
            blurred, edges = self.parallel(img, w, h, k, halo, mode == 'visual', report,
                                           params.get('transport', 'raw'))

        report['md5_edges'] = md5(edges)
        report['md5_input'] = md5(img)
        if blurred is not None:
            report['md5_blur'] = md5(blurred)
        if mode == 'visual':
            report['original_pgm_b64'] = _b64(make_pgm(w, h, img))
            report['blur_pgm_b64'] = _b64(make_pgm(w, h, blurred))
            report['edges_pgm_b64'] = _b64(make_pgm(w, h, edges))
        report['t_job_inside_solver'] = time.time() - t_job
        self.write_output(report)
        print('Job Finished: total %.3f s' % report['t_total'])

    def sequential(self, img, w, h, report):
        t0 = time.time()
        blurred, edges = filter_image(img, w, h)
        t = time.time() - t0
        report.update({'t_split': 0.0, 't_map': t, 't_merge': 0.0, 't_total': t,
                       't_compute_max': t, 't_compute_sum': t, 't_comm': 0.0,
                       'workers_detail': []})
        return blurred, edges

    def parallel(self, img, w, h, k, halo, want_blur, report, transport='raw'):
        workers, hosts, my_ip = order_workers(self.workers)
        workers, hosts = workers[:k], hosts[:k]
        _set_serializer(workers, 'marshal' if transport == 'raw' else 'serpent')

        # 1) розбиття на смуги з перекриттям (map: підготовка даних)
        t0 = time.time()
        bands = split_bands(h, k, halo)
        payloads = [pack(img[top * w:bottom * w], transport) for (_y0, _y1, top, bottom) in bands]
        t_split = time.time() - t0

        # 2) розсилка, обчислення на воркерах, отримання результатів (паралельно)
        t1 = time.time()
        calls = [_Call(p, 'mymap', (payloads[i], w, bands[i][3] - bands[i][2], want_blur, transport))
                 for i, p in enumerate(workers)]
        for c in calls:
            c.start()
        for c in calls:
            c.join()
        t_map = time.time() - t1
        errors = [c.error for c in calls if c.error]
        if errors:
            if transport == 'raw':
                # серіалізатор marshal недоступний - повтор із base64 через serpent
                print('raw transport failed (%s), retrying with base64' % errors[0])
                return self.parallel(img, w, h, k, halo, want_blur, report, 'b64')
            raise Exception('Worker error: %s' % '; '.join(errors))

        # 3) reduce: відкинути перекриття, склеїти
        t2 = time.time()
        edges, blurred = self.myreduce(w, h, bands, [c.result for c in calls], want_blur, transport)
        t_merge = time.time() - t2

        detail = []
        for i, c in enumerate(calls):
            r = c.result
            work = r['t_decode'] + r['t_compute'] + r['t_encode']
            detail.append({
                'worker': i + 1, 'host': hosts[i][0], 'same_vm_as_master': hosts[i][0] == my_ip,
                'rows': bands[i][1] - bands[i][0], 'rows_sent': bands[i][3] - bands[i][2],
                'bytes_sent': len(payloads[i]), 'bytes_received': r['bytes_out'],
                't_call': c.elapsed, 't_decode': r['t_decode'], 't_compute': r['t_compute'],
                't_encode': r['t_encode'], 't_network_and_rpc': max(0.0, c.elapsed - work),
            })
        comp = [d['t_compute'] for d in detail]
        total = t_split + t_map + t_merge
        report.update({
            't_split': t_split, 't_map': t_map, 't_merge': t_merge, 't_total': total,
            't_compute_max': max(comp), 't_compute_sum': sum(comp),
            't_comm': total - max(comp), 'master_ip': my_ip, 'transport': transport,
            'workers_detail': detail,
        })
        return blurred, edges

    @staticmethod
    def myreduce(w, h, bands, results, want_blur, transport='raw'):
        edges = merge_bands(w, h, bands, [unpack(r['edges'], transport) for r in results])
        blurred = None
        if want_blur:
            blurred = merge_bands(w, h, bands, [unpack(r['blur'], transport) for r in results])
        return edges, blurred

    # --- воркер ---
    @staticmethod
    @expose
    def mymap(band, w, rows, want_blur=False, transport='b64'):
        """Фільтрує смугу (разом із рядками перекриття) як окреме зображення."""
        t0 = time.time()
        band = unpack(band, transport)
        t1 = time.time()
        blurred, edges = filter_image(band, w, rows)
        t2 = time.time()
        res = {'edges': pack(edges, transport),
               'blur': pack(blurred, transport) if want_blur else pack(b'', transport)}
        t3 = time.time()
        res.update({'t_decode': t1 - t0, 't_compute': t2 - t1, 't_encode': t3 - t2,
                    'bytes_out': len(res['edges']) + len(res['blur']),
                    'host': socket.gethostname()})
        return res

    def write_output(self, report):
        with open(self.output_file_name, 'w') as f:
            f.write(json.dumps(report, sort_keys=True))
            f.write('\n')
