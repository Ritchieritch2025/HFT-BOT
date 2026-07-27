#!/usr/bin/env python3
"""Direct-durability verify-then-prune for one raw date.

Same safety contract as prune_gate.py, minus the receipt layer (which is dead:
kalshi-research-v3-durable.timer never ran, so no receipts exist after 07-22).
Per file: local sha256 == cloud sha256, byte-for-byte, streamed through /dev/shm
so the verification itself needs no root-disk space. Deletes ONLY on match.
firehose_/l2_ only — RFQ is never touched. Writes a deletion receipt.

    python3 verify_prune.py <date> [--apply] [--limit N]
"""
import hashlib, json, os, subprocess, sys, time, concurrent.futures as cf

RAW = '/home/ubuntu/hft-bot/work/raw'
BUCKET = 'kalshi-vault-ritcardo'
PREFIX = 'ec2/raw'
AWS = os.environ.get('AWS_CLI', '/snap/aws-cli/current/bin/aws')
SHM = '/dev/shm/verify_prune'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for c in iter(lambda: fh.read(1 << 22), b''):
            h.update(c)
    return h.hexdigest()


def check(args):
    date, name, local = args
    key = '%s/date=%s/%s' % (PREFIX, date, name)
    tmp = os.path.join(SHM, name.replace('/', '_'))
    try:
        r = subprocess.run([AWS, 's3api', 'get-object', '--bucket', BUCKET,
                            '--key', key, tmp], capture_output=True, text=True)
        if r.returncode != 0:
            return dict(name=name, ok=False, why='s3_get_failed', err=r.stderr.strip()[:200])
        ls, cs = sha(local), sha(tmp)
        lb, cb = os.path.getsize(local), os.path.getsize(tmp)
        return dict(name=name, ok=(ls == cs and lb == cb), local_sha=ls, cloud_sha=cs,
                    local_bytes=lb, cloud_bytes=cb, why='' if ls == cs else 'sha_mismatch')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    date = sys.argv[1]
    apply = '--apply' in sys.argv
    limit = None
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    os.makedirs(SHM, exist_ok=True)
    d = os.path.join(RAW, 'date=%s' % date)
    names = [n for n in os.listdir(d)
             if (n.startswith('firehose_') or n.startswith('l2_')) and 'rfq' not in n]
    names.sort(key=lambda n: -os.path.getsize(os.path.join(d, n)))
    if limit:
        names = names[:limit]
    print('%s: %d firehose/l2 files, %.1f GB local'
          % (date, len(names), sum(os.path.getsize(os.path.join(d, n)) for n in names) / 1e9), flush=True)
    recs, freed, bad = [], 0, 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for r in ex.map(check, [(date, n, os.path.join(d, n)) for n in names]):
            recs.append(r)
            if r['ok']:
                if apply:
                    os.remove(os.path.join(d, r['name']))
                freed += r['local_bytes']
            else:
                bad += 1
                print('  HOLD %s (%s)' % (r['name'], r['why']), flush=True)
            if len(recs) % 20 == 0:
                print('  %d/%d verified, %.1f GB %s, %d held (%.0fs)'
                      % (len(recs), len(names), freed / 1e9,
                         'deleted' if apply else 'deletable', bad, time.time() - t0), flush=True)
    out = '/home/ubuntu/e15_scratch/prune_receipt_%s_%s.json' % (date, 'apply' if apply else 'dryrun')
    json.dump({'date': date, 'applied': apply, 'files': len(recs), 'verified_ok': len(recs) - bad,
               'held': bad, 'bytes': freed, 'records': recs}, open(out, 'w'), indent=1)
    print('%s: %d/%d verified, %.1f GB %s, receipt %s'
          % (date, len(recs) - bad, len(recs), freed / 1e9,
             'deleted' if apply else 'deletable', out), flush=True)


if __name__ == '__main__':
    main()
