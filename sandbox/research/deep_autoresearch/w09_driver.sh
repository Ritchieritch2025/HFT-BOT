#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

RUN_ID="20260715T112538Z__c21a79a8cff__deep01"
RUN_DIR="/srv/w09-research/runs/${RUN_ID}"
CACHE_ROOT="/srv/w09-research/cache"
SOURCE_DIR="${RUN_DIR}/source"
DRIVER_START_EPOCH="$(date +%s)"

mkdir -p "${RUN_DIR}/logs/driver" "${RUN_DIR}/logs/resources"

test "$(id -un)" = "ubuntu"
test -f "${RUN_DIR}/RUN_MANIFEST.json"
test -f "${RUN_DIR}/SOURCE_MANIFEST.json"
test ! -e "$HOME/.kalshi"
test ! -e "$HOME/.aws/credentials"
test ! -e "$HOME/.aws/config"
test -z "${AWS_PROFILE:-}"
test -z "${AWS_DEFAULT_PROFILE:-}"
test -z "${AWS_SHARED_CREDENTIALS_FILE:-}"
test -z "${AWS_ACCESS_KEY_ID:-}"
test -z "${AWS_SECRET_ACCESS_KEY:-}"
test -z "${AWS_SESSION_TOKEN:-}"
test -z "${KALSHI_API_KEY:-}"
test -z "${KALSHI_PRIVATE_KEY:-}"

# Refuse every ambient AWS/Kalshi credential/provider override and bind the
# root-owned W09 read-only tooling before the first release verification.
/opt/w09/venv/bin/python - <<'PY'
import hashlib, os, pathlib, stat
expected = {
    "/usr/local/bin/research_data": "68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f",
    "/opt/w09/research/tools/research_data_instance_profile.py": "af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf",
    "/usr/local/bin/w09-run": "6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321",
    "/etc/w09/cost-contract.json": "bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352",
}
ambient = sorted(k for k in os.environ if k.startswith(("AWS_", "KALSHI_")))
if ambient:
    raise SystemExit("ambient AWS/Kalshi variables refused: " + ",".join(ambient))
for raw, wanted in expected.items():
    path = pathlib.Path(raw)
    info = path.stat()
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit(f"untrusted W09 installation ownership/mode: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != wanted:
        raise SystemExit(f"W09 installation SHA-256 mismatch: {path}")
PY

TOKEN="$(curl -fsS --max-time 2 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token)"
HDR="X-aws-ec2-metadata-token: ${TOKEN}"
INSTANCE_ID="$(curl -fsS --max-time 2 -H "${HDR}" \
  http://169.254.169.254/latest/meta-data/instance-id)"
REGION="$(curl -fsS --max-time 2 -H "${HDR}" \
  http://169.254.169.254/latest/dynamic/instance-identity/document | \
  /opt/w09/venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["region"])')"
ROLE="$(curl -fsS --max-time 2 -H "${HDR}" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/)"
PROFILE="$(curl -fsS --max-time 2 -H "${HDR}" \
  http://169.254.169.254/latest/meta-data/iam/info | \
  /opt/w09/venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["InstanceProfileArn"].rsplit("/",1)[-1])')"
test "${INSTANCE_ID}" = "i-0e53d134dceffe166"
test "${REGION}" = "us-east-2"
test "${ROLE}" = "w09-research-runner"
test "${PROFILE}" = "w09-research-runner"
test "$(uname -m)" = "aarch64"

cd "${SOURCE_DIR}"
sha256sum -c "${RUN_DIR}/SOURCE_SHA256SUMS.txt"
/opt/w09/venv/bin/python -m pytest -q -p no:cacheprovider "${SOURCE_DIR}"

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label gate_verify_2026_07_12 -- \
  /usr/local/bin/research_data verify --release 2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03
/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label gate_verify_2026_07_13 -- \
  /usr/local/bin/research_data verify --release 2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5

/opt/w09/venv/bin/python - "${RUN_DIR}" "${INSTANCE_ID}" "${REGION}" "${ROLE}" "${PROFILE}" <<'PY'
import datetime, hashlib, json, os, platform, re, subprocess, sys
run_dir, iid, region, role, profile = sys.argv[1:]
def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
inhibitors=subprocess.check_output(['systemd-inhibit','--list','--no-pager'],text=True)
match=re.search(r'^w09-workload\s+\d+\s+\S+\s+(\d+)\s+systemd-inhibit\s+shutdown\b', inhibitors, re.MULTILINE)
if not match:
    raise SystemExit('w09-run inhibitor proof missing')
inhibitor_pid=int(match.group(1))
ancestors=[]
pid=os.getpid()
while pid > 1 and pid not in ancestors:
    ancestors.append(pid)
    with open(f'/proc/{pid}/status',encoding='utf-8') as f:
        ppid_line=next(line for line in f if line.startswith('PPid:'))
    pid=int(ppid_line.split(':',1)[1].strip())
if inhibitor_pid not in ancestors:
    raise SystemExit('w09-run inhibitor is not an ancestor of this workload')
att={
  'attested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'instance_id':iid,'region':region,'role':role,'instance_profile':profile,
  'architecture':platform.machine(),
  'python':platform.python_version(),'hostname':platform.node(),
  'duckdb':__import__('duckdb').__version__,
  'w09_run_inhibitor_present':True,'w09_run_inhibitor_is_ancestor':True,
  'w09_run_inhibitor_pid':inhibitor_pid,'static_credentials_present':False,
  'trading_credentials_present':False,'s3_access':'READ_ONLY_RESEARCH_PREFIX',
  'ambient_aws_or_kalshi_variables':[],
  'static_credential_paths_present':[],
  'installation_sha256':{
    '/usr/local/bin/research_data':'68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f',
    '/opt/w09/research/tools/research_data_instance_profile.py':'af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf',
    '/usr/local/bin/w09-run':'6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321',
    '/etc/w09/cost-contract.json':'bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352',
  },
}
path=os.path.join(run_dir,'DATA_INTEGRITY','W09_ATTESTATION.json')
tmp_path=path+'.tmp'
with open(tmp_path,'w',encoding='utf-8') as f:
    json.dump(att,f,indent=2,sort_keys=True); f.write('\n')
os.replace(tmp_path,path)
m_path=os.path.join(run_dir,'RUN_MANIFEST.json')
with open(m_path,encoding='utf-8') as f: m=json.load(f)
if m.get('status')!='REGISTRATION_FROZEN_NO_RESULTS' or m.get('analysis_started'):
    raise SystemExit('manifest not frozen/no-results')
m['gates']['gate_a']={'status':'PASS_SAME_RUN_EXACT_VERSION_REVERIFY'}
m['gates']['gate_b']={
  'status':'PASS_CURRENT_W09_ATTESTED_PINNED_IMDSV2_READ_ONLY_ANCESTOR',
  'attestation_sha256':digest(path),
}
m['analysis_started']=True
m['analysis_started_at_utc']=att['attested_at_utc']
m['status']='CYCLE1_CORE_RUNNING'
tmp=m_path+'.tmp'
with open(tmp,'w',encoding='utf-8') as f: json.dump(m,f,indent=2,sort_keys=True); f.write('\n')
os.replace(tmp,m_path)
PY

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label rfq_trigger_prefix -- \
  /opt/w09/venv/bin/python "${SOURCE_DIR}/rfq_trigger.py" \
    --run-dir "${RUN_DIR}" --cache-root "${CACHE_ROOT}" --max-rows 2000000

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label cycle1_core -- \
  /opt/w09/venv/bin/python "${SOURCE_DIR}/run_cycle1.py" \
    --run-dir "${RUN_DIR}" --cache-root "${CACHE_ROOT}" \
    --memory-limit 40GB --max-temp-directory-size 120GB --threads 8

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label core_hypothesis_tests -- \
  /opt/w09/venv/bin/python "${SOURCE_DIR}/core_hypothesis_tests.py" \
    --run-dir "${RUN_DIR}" \
    --memory-limit 40GB --max-temp-directory-size 120GB --threads 8

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label rfq_full_stage -- \
  /opt/w09/venv/bin/python "${SOURCE_DIR}/rfq_full_stage.py" \
    --run-dir "${RUN_DIR}" --cache-root "${CACHE_ROOT}" \
    --memory-limit 40GB --max-temp-size 120GB --threads 8 \
    --min-free-gib 120 --clob-max-per-root 50

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_runner.py" \
  --run-dir "${RUN_DIR}" --label l2_hypothesis_stage -- \
  /opt/w09/venv/bin/python "${SOURCE_DIR}/l2_hypothesis_stage.py" \
    --run-dir "${RUN_DIR}" --cache-root "${CACHE_ROOT}" \
    --memory-limit 40GB --max-temp-directory-size 120GB --threads 8 \
    --bootstrap-replicates 2000

/opt/w09/venv/bin/python "${SOURCE_DIR}/resource_finalize.py" \
  --run-dir "${RUN_DIR}" --driver-start-epoch "${DRIVER_START_EPOCH}"

# The finalizer is intentionally not wrapped by resource_runner: RESOURCE_USAGE.json
# is already closed and becomes part of the final immutable artifact inventory.
/opt/w09/venv/bin/python "${SOURCE_DIR}/finalize_mission.py" \
  --run-dir "${RUN_DIR}"
