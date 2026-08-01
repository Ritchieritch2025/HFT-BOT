#!/bin/sh
# mmctl — operator console for the live MM engine (hot-reload, no restart).
#
#   ./mmctl.sh status              show current limits + engine health
#   ./mmctl.sh cap 20              set max open exposure to $20
#   ./mmctl.sh clip 5              set order size to 5 contracts
#   ./mmctl.sh net 10              set max net inventory per market
#   ./mmctl.sh pause               stop quoting + cancel all (reversible)
#   ./mmctl.sh resume              resume quoting
#   ./mmctl.sh kill                emergency: cancel all + halt engine
#
# Changes take effect within 1 second. Engine keeps running.
set -e
SSH="ssh -o ConnectTimeout=10 -i $HOME/.ssh/kalshi-key.pem ubuntu@3.130.232.109"
CTRL=/home/ubuntu/h6b_inputs/mm_control.json
PY=/home/ubuntu/hft-bot/.venv/bin/python

set_key() {
  $SSH "$PY -c \"
import json,os
p='$CTRL'
d=json.load(open(p)) if os.path.exists(p) else {}
v='$2'
try: v=json.loads(v)
except Exception: pass
d['$1']=v
json.dump(d,open(p,'w'),indent=1)
print('SET $1 =',v)
print(json.dumps(d))\""
}

case "$1" in
  status)
    $SSH "cat $CTRL 2>/dev/null || echo '(no control file yet — defaults in effect)';
      pgrep -f 'mm_engine.p[y]' >/dev/null && echo ENGINE:RUNNING || echo ENGINE:STOPPED;
      f=\$(ls -t /home/ubuntu/h6b_inputs/mm_engine/mm_*.ndjson 2>/dev/null | head -1);
      [ -n \"\$f\" ] && grep -h '\"ev\":\"HEALTH\"' \$f | tail -1;
      [ -n \"\$f\" ] && grep -h 'CONTROL_APPLIED' \$f | tail -1" ;;
  cap)    set_key max_open_cost "$2" ;;
  clip)   set_key clip "\"$2.00\"" ;;
  net)    set_key max_net "$2" ;;
  pause)  set_key paused true ;;
  resume) set_key paused false ;;
  kill)   set_key kill true ;;
  unkill) set_key kill false ;;
  *) sed -n '2,14p' "$0" ;;
esac
