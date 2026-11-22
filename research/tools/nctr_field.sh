# shellcheck shell=bash
# NCTR-200 현장 시험 보조 함수입니다. 실행하지 말고 source로 불러옵니다.
#
#   source ~/nctr-lab/releases/<릴리스>/tools/nctr_field.sh
#   nctr_help
#
# 설정값은 ~/nctr-lab/field.env 한 곳에만 둡니다(nctr_setup이 틀을 만듭니다).
# 이 파일은 DNS·방화벽·조절기 설정을 바꾸지 않습니다.

if [ -z "${BASH_VERSION:-}" ]; then
  echo "bash에서 source 하십시오." >&2
  return 1 2>/dev/null || exit 1
fi
if ! (return 0 2>/dev/null); then
  echo "실행하지 말고 source 하십시오: source $0" >&2
  exit 1
fi

# 사용자 alias(예: cat='bat --color=always', ls='eza')가 함수 정의에 섞이지 않도록
# 이 파일을 읽는 동안 alias 치환을 끄고, 끝에서 원래 상태로 되돌립니다.
_nctr_alias_state=off
shopt -q expand_aliases && _nctr_alias_state=on
shopt -u expand_aliases

NCTR_RELEASE="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCTR_HOME="${NCTR_HOME:-$HOME/nctr-lab}"
NCTR_ENV_FILE="$NCTR_HOME/field.env"
NCTR_OUTPUT_ROOT="$NCTR_HOME/experiments"
NCTR_PCAP_ROOT="$NCTR_HOME/pcaps"
export NCTR_RELEASE NCTR_HOME NCTR_ENV_FILE NCTR_OUTPUT_ROOT NCTR_PCAP_ROOT

_nctr_cli() {
  (builtin cd "$NCTR_RELEASE" && command python3 src/nctr_lab_cli.py "$@")
}

nctr_help() {
  cat <<'EOF'
준비
  nctr_setup                 설정 파일 틀 생성 (~/nctr-lab/field.env)
  nctr_show                  현재 설정값 표시
  nctr_preflight             주소·포트·도구·DNS 응답 점검 (doctor)
  nctr_dnsmasq               기존 DNS가 없을 때만: 임시 dnsmasq (igs4만 로컬)
  nctr_watch_syn             TCP/20000 SYN 관찰 (접속원 IP 확인용, Ctrl+C 종료)

회차 (회차 이름 예: 00, 01, 05a)
  nctr_capture 이름          창 A: TCP/20000 캡처 시작 (Ctrl+C 종료)
  nctr_collect 이름 [옵션]   창 B: 수집 서버 시작. 원격 명령은 모두 켜진 상태
                             (수집만: --no-control, 일부 끄기: --disable ondol)
  nctr_collect_node 이름     0000 시작일 때: 노드 ID 파일을 붙여 수집 서버 시작
  nctr_collect_temp 이름     실내 온도 범위를 field.env의 TEMP_MIN/MAX로 좁혀 시작
  nctr_finish 이름           종료 후: 권한 정리·verify·attach·analyze·export
  nctr_runs                  오늘 날짜 회차 목록
  nctr_novel 이름            그 회차의 신규 메시지(novel.*.jsonl) 요약
  nctr_watch 이름            실시간: 해석된 값·신규 메시지·세션 사건 보기 (Ctrl+C)
EOF
}

nctr_setup() {
  (umask 077 && mkdir -p "$NCTR_HOME/private" "$NCTR_OUTPUT_ROOT" "$NCTR_PCAP_ROOT") || return 1
  chmod 700 "$NCTR_HOME" "$NCTR_HOME/private"
  if [ -e "$NCTR_ENV_FILE" ]; then
    echo "이미 있습니다: $NCTR_ENV_FILE"
  else
    (umask 077 && cat > "$NCTR_ENV_FILE" <<'EOF'
# NCTR-200 현장 시험 설정. 값만 바꾸고 따옴표는 유지합니다.

# 시험 날짜(YYYYMMDD). 회차 이름 field-<날짜>-<회차>에 쓰입니다.
# 자정을 넘겨도 바꾸지 않습니다(창마다 값이 달라지지 않게 고정).
NCTR_DATE='REPLACE_YYYYMMDD'

# Ubuntu LAN 주소 (수집 서버가 바인딩할 주소)
NCTR_SERVER_IP='REPLACE_UBUNTU_LAN_IP'

# 조절기 쪽이 사용하는 DNS 서버 주소. Ubuntu DNS면 보통 NCTR_SERVER_IP와 같습니다.
NCTR_DNS_IP='REPLACE_DNS_IP'

# Ubuntu에서 보이는 접속원. 한 IP 또는 범위(예: 192.168.1.0/24).
# Windows 핫스팟 구성은 Windows Ethernet IP, 자체 프로비저닝으로 ipTIME에 직접 붙이면
# 조절기 IP를 미리 모르므로 LAN 범위를 씁니다.
NCTR_PEER_IP='REPLACE_PEER_IP'

# 캡처할 Ubuntu LAN 인터페이스 (ip -4 -o addr show 로 확인)
NCTR_CAPTURE_IFACE='REPLACE_IFACE'

# 0000 시작일 때만 쓰는 8바이트 노드 ID 파일 (hex 16자, chmod 600)
NCTR_NODE_ID_FILE="$HOME/nctr-lab/private/node_id.hex"

# 회차 6 원격 온도 범위. 조절기에서 실제로 확인한 값으로 채웁니다.
NCTR_TEMP_MIN=''
NCTR_TEMP_MAX=''
EOF
    ) || return 1
    echo "만들었습니다: $NCTR_ENV_FILE"
  fi
  echo "값 입력: ${EDITOR:-vi} $NCTR_ENV_FILE"
}

nctr_load() {
  if [ ! -r "$NCTR_ENV_FILE" ]; then
    echo "설정 파일이 없습니다. nctr_setup을 먼저 실행하십시오." >&2
    return 1
  fi
  # shellcheck disable=SC1090
  . "$NCTR_ENV_FILE"
  local name missing=0
  for name in NCTR_DATE NCTR_SERVER_IP NCTR_DNS_IP NCTR_PEER_IP NCTR_CAPTURE_IFACE; do
    case "${!name:-}" in
      ''|*REPLACE*) echo "설정값이 비어 있습니다: $name ($NCTR_ENV_FILE)" >&2; missing=1 ;;
    esac
  done
  [ "$missing" = 0 ] || return 1
  case "$NCTR_DATE" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) echo "NCTR_DATE는 YYYYMMDD 형식이어야 합니다: $NCTR_DATE" >&2; return 1 ;;
  esac
  if command -v ip >/dev/null 2>&1 && ! ip link show dev "$NCTR_CAPTURE_IFACE" >/dev/null 2>&1; then
    echo "NCTR_CAPTURE_IFACE가 인터페이스 이름이 아닙니다: $NCTR_CAPTURE_IFACE (예: enp3s0)" >&2
    return 1
  fi
  export NCTR_DATE NCTR_SERVER_IP NCTR_DNS_IP NCTR_PEER_IP NCTR_CAPTURE_IFACE \
         NCTR_NODE_ID_FILE NCTR_TEMP_MIN NCTR_TEMP_MAX
}

_nctr_run_id() {
  case "${1:-}" in
    ''|*[!A-Za-z0-9-]*)
      echo "회차 이름은 영문·숫자·-만 사용합니다. 예: 00, 01, 05a" >&2
      return 1 ;;
  esac
  printf 'field-%s-%s' "$NCTR_DATE" "$1"
}

nctr_show() {
  nctr_load || return 1
  cat <<EOF
릴리스        $NCTR_RELEASE
설정 파일     $NCTR_ENV_FILE
날짜          $NCTR_DATE
서버 IP       $NCTR_SERVER_IP
DNS IP        $NCTR_DNS_IP
접속원 IP     $NCTR_PEER_IP
인터페이스    $NCTR_CAPTURE_IFACE
노드 ID 파일  $NCTR_NODE_ID_FILE
온도 범위     ${NCTR_TEMP_MIN:-미설정} ~ ${NCTR_TEMP_MAX:-미설정}
수집 폴더     $NCTR_OUTPUT_ROOT
캡처 폴더     $NCTR_PCAP_ROOT
EOF
}

nctr_preflight() {
  nctr_load || return 1
  echo "== IPv4 주소"
  ip -4 -o addr show
  echo "== 53·20000 포트 사용 프로세스"
  sudo ss -lntup | grep -E ':(53|20000)\b' || echo "(없음)"
  echo "== 도구"
  local tool
  for tool in python3 tcpdump tmux; do
    command -v "$tool" >/dev/null && echo "있음: $tool" || echo "없음: $tool"
  done
  python3 --version
  echo "== 노드 ID 파일"
  if [ -f "$NCTR_NODE_ID_FILE" ]; then
    stat -c '%a %n' "$NCTR_NODE_ID_FILE"
  else
    echo "없음 (0000 시작일 때만 필요)"
  fi
  echo "== doctor (바인딩·여유 공간·DNS 응답)"
  _nctr_cli doctor --bind "$NCTR_SERVER_IP" --port 20000 \
    --output-root "$NCTR_OUTPUT_ROOT" \
    --dns-server "$NCTR_DNS_IP" --expected-ip "$NCTR_SERVER_IP"
}

nctr_dnsmasq() {
  nctr_load || return 1
  echo "임시 DNS: igs4.krb.co.kr -> $NCTR_SERVER_IP, 나머지는 1.1.1.1로 전달 (Ctrl+C 종료)"
  echo "기존 DNS가 53번 포트를 쓰고 있으면 실행하지 마십시오."
  sudo dnsmasq --keep-in-foreground --bind-interfaces \
    --listen-address="$NCTR_SERVER_IP" \
    --no-resolv --no-hosts --server=1.1.1.1 \
    --host-record=igs4.krb.co.kr,"$NCTR_SERVER_IP" \
    --log-queries --log-facility=-
}

nctr_watch_syn() {
  nctr_load || return 1
  echo "TCP/20000 SYN 관찰 중 (Ctrl+C 종료). 출발지 IP가 NCTR_PEER_IP입니다."
  sudo tcpdump -ni "$NCTR_CAPTURE_IFACE" 'tcp dst port 20000 and tcp[tcpflags] & tcp-syn != 0'
}

nctr_capture() {
  nctr_load || return 1
  local run_id dir
  run_id="$(_nctr_run_id "${1:-}")" || return 1
  dir="$NCTR_PCAP_ROOT/$run_id"
  if [ -e "$dir" ]; then
    echo "이미 있습니다: $dir — 다른 회차 이름을 쓰십시오." >&2
    return 1
  fi
  mkdir -p "$NCTR_PCAP_ROOT" && mkdir -m 700 "$dir" || return 1
  sudo -v || return 1
  local peer_filter="host $NCTR_PEER_IP"
  case "$NCTR_PEER_IP" in */*) peer_filter="net $NCTR_PEER_IP" ;; esac
  echo "캡처 시작: $run_id ($peer_filter, tcp 20000). 종료는 Ctrl+C."
  sudo tcpdump -i "$NCTR_CAPTURE_IFACE" -s 0 -U -Z "$(id -un)" \
    -w "$dir/server_tcp.pcap" \
    "$peer_filter and tcp port 20000" \
    2> >(tee "$dir/tcpdump_stats.txt" >&2)
  echo "캡처 종료: $dir"
}

nctr_collect() {
  nctr_load || return 1
  local suffix="${1:-}" run_id
  run_id="$(_nctr_run_id "$suffix")" || return 1
  shift
  if [ -e "$NCTR_OUTPUT_ROOT/$run_id" ]; then
    echo "이미 있는 실행 폴더입니다: $NCTR_OUTPUT_ROOT/$run_id — 다른 회차 이름을 쓰십시오." >&2
    return 1
  fi
  if [ ! -d "$NCTR_PCAP_ROOT/$run_id" ]; then
    echo "주의: 이 회차의 캡처 폴더가 없습니다. 다른 창에서 nctr_capture $suffix 를 먼저 실행했는지 확인하십시오." >&2
  fi
  echo "수집 서버: $run_id $*"
  _nctr_cli collect \
    --bind "$NCTR_SERVER_IP" --allowed-client-ip "$NCTR_PEER_IP" \
    --mode handshake --time-tail 0123 \
    --output-root "$NCTR_OUTPUT_ROOT" --run-id "$run_id" "$@"
}

nctr_collect_node() {
  nctr_load || return 1
  if [ ! -f "$NCTR_NODE_ID_FILE" ]; then
    echo "노드 ID 파일이 없습니다: $NCTR_NODE_ID_FILE" >&2
    return 1
  fi
  if ! grep -Eqx '[0-9a-fA-F]{16}' "$NCTR_NODE_ID_FILE"; then
    echo "노드 ID 파일은 hex 16자 한 줄이어야 합니다: $NCTR_NODE_ID_FILE" >&2
    return 1
  fi
  chmod 600 "$NCTR_NODE_ID_FILE"
  local suffix="${1:-}"
  shift
  nctr_collect "$suffix" --node-id-file "$NCTR_NODE_ID_FILE" "$@"
}

nctr_collect_temp() {
  nctr_load || return 1
  if [ -z "${NCTR_TEMP_MIN:-}" ] || [ -z "${NCTR_TEMP_MAX:-}" ]; then
    echo "field.env의 NCTR_TEMP_MIN·NCTR_TEMP_MAX를 실제 범위로 채우십시오." >&2
    return 1
  fi
  local suffix="${1:-}"
  shift
  nctr_collect "$suffix" --temp-min "$NCTR_TEMP_MIN" --temp-max "$NCTR_TEMP_MAX" "$@"
}

nctr_finish() {
  nctr_load || return 1
  local run_id run_dir cap_dir name rc
  run_id="$(_nctr_run_id "${1:-}")" || return 1
  run_dir="$NCTR_OUTPUT_ROOT/$run_id"
  cap_dir="$NCTR_PCAP_ROOT/$run_id"
  if [ ! -d "$run_dir" ]; then
    echo "실행 폴더가 없습니다: $run_dir" >&2
    return 1
  fi
  if pgrep -f -- "$cap_dir/server_tcp.pcap" >/dev/null 2>&1; then
    echo "이 회차의 tcpdump가 아직 실행 중입니다. 캡처 창에서 Ctrl+C 후 다시 실행하십시오." >&2
    return 1
  fi

  for name in server_tcp.pcap tcpdump_stats.txt; do
    if [ -f "$cap_dir/$name" ]; then
      [ -O "$cap_dir/$name" ] || sudo chown "$(id -u):$(id -g)" "$cap_dir/$name"
      chmod 600 "$cap_dir/$name"
    fi
  done

  echo "== verify (첨부 전)"
  _nctr_cli verify --run-dir "$run_dir"
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    echo "verify 실패(종료 코드 $rc). 원본을 보존하고 여기서 멈춥니다." >&2
    return "$rc"
  fi
  [ "$rc" -eq 2 ] && echo "주의: collection_clean=false. 원본을 보존하고 계속 진행합니다." >&2

  for name in server_tcp.pcap tcpdump_stats.txt; do
    if [ -f "$cap_dir/$name" ]; then
      echo "== attach $name"
      _nctr_cli attach --run-dir "$run_dir" --input "$cap_dir/$name" --category network || return 1
    else
      echo "캡처 파일 없음, 건너뜀: $cap_dir/$name"
    fi
  done

  echo "== analyze"
  _nctr_cli analyze --run-dir "$run_dir" --output "$run_dir/analysis" || return 1
  echo "== export"
  _nctr_cli export --run-dir "$run_dir" --output "$run_dir/analysis/shared_metadata.jsonl" || return 1
  echo "== verify (최종)"
  _nctr_cli verify --run-dir "$run_dir"
  rc=$?
  echo "완료: $run_dir (verify 종료 코드 $rc)"
  return "$rc"
}

nctr_novel() {
  nctr_load || return 1
  local run_dir
  run_dir="$NCTR_OUTPUT_ROOT/$(_nctr_run_id "${1:-}")" || return 1
  if ! ls "$run_dir"/novel.*.jsonl >/dev/null 2>&1; then
    echo "신규 메시지 없음: $run_dir"
    return 0
  fi
  command python3 - "$run_dir" <<'PY'
import json, glob, sys
for path in sorted(glob.glob(sys.argv[1] + "/novel.*.jsonl")):
    for line in open(path, encoding="utf-8"):
        row = json.loads(line)
        marker = (row.get("last_marker") or {}).get("text") or "-"
        print(row["at"][11:19], row["mid"], row["body_hex"], ",".join(row["reasons"]), "| marker:", marker)
PY
}

nctr_watch() {
  nctr_load || return 1
  local run_dir
  run_dir="$NCTR_OUTPUT_ROOT/$(_nctr_run_id "${1:-}")" || return 1
  command tail -n 0 -F "$run_dir"/events.*.jsonl | command python3 -u -c '
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line)
    except ValueError:
        continue
    k, t = e.get("event_type"), e.get("at", "")[11:19]
    if k == "state_decoded":
        vals = {n: (v.get("label") or v["value"]) for n, v in e["values"].items()}
        print(t, e["mid"], vals, flush=True)
    elif k in ("novel_message_logged", "session_replaced", "connected", "disconnected",
               "idle_timeout", "operator_marker", "command_requested", "command_ack",
               "command_report_candidate", "command_observed_match"):
        print(t, k, {x: e[x] for x in ("mid", "reasons", "reason", "text", "feature",
                                       "requested", "reported", "matches") if x in e}, flush=True)
'
}

nctr_runs() {
  nctr_load || return 1
  ls -1d "$NCTR_OUTPUT_ROOT"/field-"$NCTR_DATE"-* 2>/dev/null || echo "(회차 없음)"
}

[ "$_nctr_alias_state" = on ] && shopt -s expand_aliases
unset _nctr_alias_state
