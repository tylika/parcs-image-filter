# -*- coding: utf-8 -*-
"""
Драйвер замірів для PARCS (Python 3, лише стандартна бібліотека).

Через REST API майстра PARCS (порт 8080) надсилає задачі з solution/solution.py,
чекає результату, збирає час і будує зведену таблицю.

Приклади:
  python bench/run_benchmark.py --master http://20.1.2.3:8080
  python bench/run_benchmark.py --master http://localhost:8080 --sizes 256 512 --runs 1
  python bench/run_benchmark.py --master http://20.1.2.3:8080 --photo photo.pgm

Результати (папка --out, за замовчуванням results/):
  raw.jsonl     - повний звіт кожного запуску (дописується після кожної задачі,
                  тож після збою можна продовжити з --resume)
  raw.csv       - те саме у вигляді таблиці
  summary.csv   - середні по 3 запусках, прискорення, ефективність, перевірка md5
  images/       - оригінал / розмиття / контури (PGM, і PNG, якщо є Pillow)
"""
from __future__ import print_function

import argparse
import base64
import csv
import json
import math
import os
import re
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SOLUTION = os.path.join(ROOT, 'solution', 'solution.py')


# ---------------------------------------------------------------- HTTP ----
def http(method, url, body=None, headers=None, timeout=60):
    req = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                    % (boundary, name, value)).encode('utf-8'))
    for name, (filename, data) in files.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n'
                    % (boundary, name, filename)).encode('utf-8'))
        out.append(data)
        out.append(b'\r\n')
    out.append(('--%s--\r\n' % boundary).encode('utf-8'))
    return b''.join(out), 'multipart/form-data; boundary=%s' % boundary


class Parcs:
    def __init__(self, master, solution_path):
        self.master = master.rstrip('/')
        with open(solution_path, 'rb') as f:
            self.solution = f.read()

    def workers(self):
        code, body = http('GET', self.master + '/api/worker', timeout=20)
        if code != 200:
            raise RuntimeError('PARCS master returned HTTP %d on /api/worker' % code)
        return [w for w in json.loads(body.decode('utf-8'))['workers'] if w.get('enabled', True)]

    def job_ids(self):
        code, body = http('GET', self.master + '/jobs', timeout=30)
        if code != 200:
            raise RuntimeError('PARCS master returned HTTP %d on /jobs' % code)
        return [int(x) for x in re.findall(r'data-job-id="(\d+)"', body.decode('utf-8', 'replace'))]

    def job_title(self, job_id):
        code, body = http('GET', self.master + '/jobs', timeout=30)
        html = body.decode('utf-8', 'replace')
        m = re.search(r'data-job-id="%d".*?<span[^>]*>(.*?)</span>' % job_id, html, re.S)
        return re.sub(r'\s+', ' ', m.group(1)).strip() if m else '?'

    def submit(self, name, input_bytes):
        before = set(self.job_ids())
        body, ctype = multipart({'job_name': name},
                                {'solution_file': ('solution.py', self.solution),
                                 'input_file': ('input.txt', input_bytes)})
        code, _ = http('POST', self.master + '/api/job', body, {'Content-Type': ctype}, timeout=600)
        if code != 200:
            raise RuntimeError('Job submission failed: HTTP %d' % code)
        for _ in range(30):
            new = sorted(set(self.job_ids()) - before)
            if new:
                return new[-1]
            time.sleep(1)
        raise RuntimeError('Submitted job did not appear in /jobs')

    def wait(self, job_id, timeout):
        t0 = time.time()
        delay = 0.5
        while time.time() - t0 < timeout:
            code, body = http('GET', '%s/api/job/%d/output' % (self.master, job_id), timeout=300)
            if code == 200:
                return json.loads(body.decode('utf-8'))
            if code == 404:
                raise RuntimeError('Job %d failed: %s' % (job_id, self.job_title(job_id)))
            time.sleep(delay)
            delay = min(3.0, delay * 1.5)
        raise RuntimeError('Job %d timed out after %d s' % (job_id, timeout))

    def run(self, name, input_bytes, timeout):
        job_id = self.submit(name, input_bytes)
        t0 = time.time()
        rep = self.wait(job_id, timeout)
        rep['job_id'] = job_id
        rep['job_name'] = name
        rep['t_wall_client'] = time.time() - t0
        return rep


# ------------------------------------------------------------- helpers ----
def params_input(**kw):
    return ''.join('%s=%s\n' % (k, v) for k, v in kw.items()).encode('ascii')


def pgm_with_params(path, **kw):
    """Вставляє '# parcs: k=.. mode=..' у заголовок PGM."""
    with open(path, 'rb') as f:
        raw = f.read()
    if not raw.startswith(b'P5'):
        raise SystemExit('%s: expected binary PGM (P5). Convert with tools/photo_to_pgm.py' % path)
    comment = ('# parcs: ' + ' '.join('%s=%s' % (k, v) for k, v in kw.items()) + '\n').encode('ascii')
    return b'P5\n' + comment + raw[2:].lstrip(b' \t\r\n')


def save_images(rep, out_dir, prefix):
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for key, suffix in (('original_pgm_b64', 'original'), ('blur_pgm_b64', 'blur'),
                        ('edges_pgm_b64', 'edges')):
        if rep.get(key):
            path = os.path.join(out_dir, '%s_%s.pgm' % (prefix, suffix))
            with open(path, 'wb') as f:
                f.write(base64.b64decode(rep[key]))
            saved.append(path)
            try:
                from PIL import Image
                Image.open(path).save(path[:-4] + '.png')
            except Exception:
                pass
    return saved


RAW_FIELDS = ['run', 'width', 'height', 'k', 'job_id', 't_total', 't_compute_max',
              't_compute_sum', 't_comm', 't_split', 't_map', 't_merge', 't_load',
              't_wall_client', 'md5_edges', 'available_workers', 'colocated_worker_used']


def flat(rep):
    row = {f: rep.get(f, '') for f in RAW_FIELDS}
    row['colocated_worker_used'] = any(d.get('same_vm_as_master') for d in rep.get('workers_detail', []))
    return row


def mean(xs):
    return sum(xs) / len(xs) if xs else float('nan')


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def summarize(records, out_dir):
    by = {}
    for r in records:
        by.setdefault((r['width'], r['k']), []).append(r)
    sizes = sorted({s for s, _ in by})
    rows = []
    for size in sizes:
        seq = by.get((size, 0), [])
        t_seq = mean([r['t_total'] for r in seq]) if seq else None
        md5_ref = seq[0]['md5_edges'] if seq else None
        for k in sorted(k for s, k in by if s == size):
            rs = by[(size, k)]
            tt = [r['t_total'] for r in rs]
            tc = [r['t_compute_max'] for r in rs]
            row = {
                'size': '%dx%d' % (size, rs[0]['height']), 'k': k,
                'config': 'sequential' if k == 0 else '%d worker%s' % (k, '' if k == 1 else 's'),
                'runs': len(rs),
                't_total_mean': mean(tt), 't_total_std': stdev(tt),
                't_compute_mean': mean(tc),
                't_comm_mean': mean([r['t_comm'] for r in rs]),
                't_split_mean': mean([r['t_split'] for r in rs]),
                't_map_mean': mean([r['t_map'] for r in rs]),
                't_merge_mean': mean([r['t_merge'] for r in rs]),
                'speedup': (t_seq / mean(tt)) if t_seq else '',
                'efficiency': (t_seq / mean(tt) / k) if (t_seq and k) else '',
                'speedup_compute_only': (t_seq / mean(tc)) if t_seq else '',
                'md5_matches_sequential': (all(r['md5_edges'] == md5_ref for r in rs)
                                           if md5_ref else 'no sequential run'),
            }
            rows.append(row)
    path = os.path.join(out_dir, 'summary.csv')
    with open(path, 'w', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ['size'])
        wr.writeheader()
        for row in rows:
            wr.writerow({k: ('%.4f' % v if isinstance(v, float) else v) for k, v in row.items()})
    return rows, path


def print_summary(rows):
    print()
    print('%-11s %-12s %9s %9s %9s %8s %8s  %s' % ('size', 'config', 'total,s', 'compute,s',
                                                  'comm,s', 'speedup', 'effic.', 'md5'))
    for r in rows:
        sp = '%.2f' % r['speedup'] if r['speedup'] != '' else '-'
        ef = '%.2f' % r['efficiency'] if r['efficiency'] != '' else '-'
        print('%-11s %-12s %9.3f %9.3f %9.3f %8s %8s  %s' % (
            r['size'], r['config'], r['t_total_mean'], r['t_compute_mean'], r['t_comm_mean'],
            sp, ef, 'OK' if r['md5_matches_sequential'] is True else r['md5_matches_sequential']))


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description='PARCS image filter benchmark')
    ap.add_argument('--master', required=True, help='http://<master-ip>:8080')
    ap.add_argument('--sizes', type=int, nargs='+', default=[1024, 2048, 4096])
    ap.add_argument('--configs', type=int, nargs='+', default=[0, 1, 2, 3, 4],
                    help='0 = sequential, N = N workers')
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--visual-size', type=int, default=1024,
                    help='size of the synthetic image saved for the report (0 = skip)')
    ap.add_argument('--photo', help='your photo as PGM (tools/photo_to_pgm.py) for the report')
    ap.add_argument('--out', default=os.path.join(ROOT, 'results'))
    ap.add_argument('--solution', default=DEFAULT_SOLUTION)
    ap.add_argument('--timeout', type=int, default=3600, help='per job, seconds')
    ap.add_argument('--resume', action='store_true', help='skip runs already in raw.jsonl')
    ap.add_argument('--wait-workers', type=int, default=300,
                    help='seconds to wait for the required number of workers')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    parcs = Parcs(args.master, args.solution)

    need = max(args.configs)
    t0 = time.time()
    while True:
        try:
            n = len(parcs.workers())
        except (URLError, OSError, RuntimeError) as e:
            n = -1
            print('Master not reachable yet: %s' % e)
        if n >= need or time.time() - t0 > args.wait_workers:
            break
        print('Connected workers: %s, need %d. Waiting...' % (max(n, 0), need))
        time.sleep(10)
    if n < 0:
        sys.exit('PARCS master %s is not reachable.' % args.master)
    configs = [k for k in args.configs if k <= n]
    if len(configs) < len(args.configs):
        print('WARNING: only %d workers connected, skipping configs %s'
              % (n, sorted(set(args.configs) - set(configs))))
    print('Workers connected: %d. Configs: %s. Sizes: %s. Runs: %d'
          % (n, configs, args.sizes, args.runs))

    raw_jsonl = os.path.join(args.out, 'raw.jsonl')
    done = set()
    records = []
    if args.resume and os.path.exists(raw_jsonl):
        with open(raw_jsonl, encoding='utf-8') as f:
            for line in f:
                r = json.loads(line)
                records.append(r)
                done.add((r['run'], r['width'], r['k']))
        print('Resuming: %d runs already done' % len(done))
    elif os.path.exists(raw_jsonl):
        os.replace(raw_jsonl, raw_jsonl + '.%d.bak' % int(time.time()))

    # прогрів: завантаження модуля на воркерах, з'єднання (у статистику не йде)
    print('Warm-up job...')
    parcs.run('warmup', params_input(mode='bench', size=256, k=max(configs), seed=args.seed),
              args.timeout)

    plan = [(run, size, k) for run in range(1, args.runs + 1)
            for size in args.sizes for k in configs]
    t_start = time.time()
    for i, (run, size, k) in enumerate(plan, 1):
        if (run, size, k) in done:
            continue
        name = 'filter_%d_k%d_run%d' % (size, k, run)
        rep = parcs.run(name, params_input(mode='bench', size=size, k=k, seed=args.seed),
                        args.timeout)
        rep['run'] = run
        records.append(rep)
        with open(raw_jsonl, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rep) + '\n')
        print('[%2d/%d] %4dx%-4d %-12s total %8.3f s  compute %8.3f s  comm %7.3f s  md5 %s'
              % (i, len(plan), size, size, 'sequential' if k == 0 else 'k=%d' % k,
                 rep['t_total'], rep['t_compute_max'], rep['t_comm'], rep['md5_edges'][:8]))
    print('Benchmark time: %.1f min' % ((time.time() - t_start) / 60))

    with open(os.path.join(args.out, 'raw.csv'), 'w', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        wr.writeheader()
        for r in records:
            wr.writerow(flat(r))
    with open(os.path.join(args.out, 'workers_detail.csv'), 'w', newline='', encoding='utf-8') as f:
        fields = ['run', 'width', 'k', 'worker', 'host', 'same_vm_as_master', 'rows', 'rows_sent',
                  'bytes_sent', 'bytes_received', 't_call', 't_decode', 't_compute', 't_encode',
                  't_network_and_rpc']
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in records:
            for d in r.get('workers_detail', []):
                row = dict(d)
                row.update(run=r['run'], width=r['width'], k=r['k'])
                wr.writerow({k: row.get(k, '') for k in fields})

    rows, path = summarize(records, args.out)
    print_summary(rows)
    print('\nSummary: %s' % path)

    # картинки для звіту
    img_dir = os.path.join(args.out, 'images')
    visual = []
    if args.visual_size:
        rep = parcs.run('visual_synthetic', params_input(mode='visual', size=args.visual_size,
                                                         k=max(configs), seed=args.seed),
                        args.timeout)
        visual.append(rep)
        print('Saved: %s' % ', '.join(save_images(rep, img_dir, 'synthetic_%d' % args.visual_size)))
    if args.photo:
        reps = {}
        for k in sorted({0, max(configs)}):
            reps[k] = parcs.run('visual_photo_k%d' % k,
                                pgm_with_params(args.photo, mode='visual', k=k), args.timeout)
        rep = reps[max(configs)]
        visual.append(rep)
        same = reps[0]['md5_edges'] == rep['md5_edges']
        print('Photo: parallel (k=%d) == sequential: %s' % (max(configs), same))
        print('Saved: %s' % ', '.join(save_images(rep, img_dir, 'photo')))
    if visual:
        with open(os.path.join(args.out, 'visual_runs.json'), 'w', encoding='utf-8') as f:
            json.dump([{k: v for k, v in r.items() if not k.endswith('_b64')} for r in visual],
                      f, indent=2)

    bad = [r for r in rows if r['md5_matches_sequential'] is not True]
    print('\nCORRECTNESS: %s' % ('all parallel results match the sequential one'
                                 if not bad else 'MISMATCH in %d configs!' % len(bad)))


if __name__ == '__main__':
    main()
