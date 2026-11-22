# 실물 시험 현장 절차 (Windows 핫스팟·Ubuntu NAS·아이패드)

작성일: 2026-10-25(KST), 2026-11-21 전면 개정, 같은 날 세션 교체·무수신 종료 반영  
현재 실행 코드: [nctr_lab_cli.py](../src/nctr_lab_cli.py), [nctr_lab 패키지](../src/nctr_lab/__init__.py), [tools/nctr_field.sh](../tools/nctr_field.sh), [tools/nctr_windows_field.ps1](../tools/nctr_windows_field.ps1)  
회차별 조작·marker: [현장 체크리스트](NCTR200_Field_Marker_Checklist.md)  
메시지 형식: [메시지 참조](NCTR200_Message_Reference.md)  
범위·판정: [실험 계획](NCTR200_Local_Control_Experiment_Plan.md)

이 문서 하나로 준비부터 회수까지 진행합니다. 회차 안에서 입력할 marker와 물리 조작만 체크리스트에 있습니다.

## 1. 구성

조절기를 AP 초기화한 뒤 Windows 모바일 핫스팟으로 다시 프로비저닝합니다. Windows가 조절기 통신 경로에 들어오므로 핫스팟 인터페이스의 Wireshark로 DHCP·DNS·HTTP·TCP를 모두 볼 수 있습니다. 집 공유기 설정은 바꾸지 않습니다.

```text
NCTR-200 ── Wi-Fi(2.4GHz) ──> Windows 모바일 핫스팟 (Wireshark 캡처)
                                  │ ICS NAT, DNS는 Windows 이더넷 DNS 설정을 따름
                                  ▼
                     Ubuntu NAS 192.168.1.8
                       ├─ dnsmasq: igs4.krb.co.kr만 192.168.1.8로 응답
                       ├─ 수집 서버 TCP/20000
                       └─ tcpdump (TCP/20000)
아이패드 ── SSH ──> NAS tmux (시험 중 입력)
igis.krb.co.kr HTTP는 제조사 경로 그대로 유지
```

### 2026-11-21 확인값

| 항목 | 값 | 비고 |
|---|---|---|
| Ubuntu NAS | `192.168.1.8`, 인터페이스 `enp3s0`, SSH 별칭 `nas` | `NCTR_CAPTURE_IFACE`에는 IP가 아니라 인터페이스 이름을 넣습니다. |
| Windows 이더넷 | 어댑터 이름 `이더넷`, `192.168.1.2` | DHCP 주소라 바뀔 수 있습니다. 시험 당일 확인합니다. 핫스팟 구성에서는 이 주소가 NAS에서 보이는 접속원(`NCTR_PEER_IP`)입니다. |
| Windows 원래 DNS | `168.126.63.1`, `168.126.63.2` 고정 | `DnsSet`이 자동 백업하고 `DnsRestore`가 되돌립니다. |
| ipTIME DHCP DNS | `1.1.1.1`, `1.0.0.1` | 이번 구성에서는 바꾸지 않습니다. |
| 핫스팟 인터페이스 | 과거 기록상 `Wi-Fi 4` | 시험 당일 Wireshark 목록에서 확인합니다. |
| NAS 셸 alias | `cat='bat --color=always'`, `ls='eza --icons'` | 보조 스크립트는 source할 때 alias 치환을 끄므로 영향이 없습니다. 예전 판을 source한 셸은 새로 source해야 합니다. |

### 이번 시험에서 하지 않는 것

- 제조사 앱의 계정 등록. 프로비저닝은 등록 코드 입력 화면에서 멈춥니다.
- 인터넷 차단·자체 프로비저닝(E14 후반), 오류 유발(E12), HA 노출(E15).
- 수집 회차의 원격 명령 입력. 명령은 기본으로 켜져 있지만 `SEND`를 입력하지 않으면 전송되지 않습니다. 수집만 할 회차는 `nctr_collect 회차 --no-control`로 띄우면 실수로 보낼 일이 없습니다. 원격 명령은 10절에서 별도 회차로 시험합니다.

## 2. 구현 기능과 남은 범위

| 항목 | 현재 상태 |
|---|---|
| 원본·사건 수집 | RX 청크·TX 요청 전문, 모든 프레임 body·미해독·오류·잔여 버퍼, 접속·대기 명령·조작 표시 저장 |
| 프로토콜 | TCP 재조립, 103바이트 기본 상태 프로파일, ACK와 body 이벤트 구분, ID pinning, 초기 응답 정책 선택 |
| ACK | 알려진 이벤트 규칙 + 규칙에 없는 장치 이벤트(`0x0100~0x01FE`)도 ACK. `--no-ack-unlisted`로 끔 |
| 연결 확인 | 조절기가 접속 60분 8초 뒤부터 그 주기로 10초마다 빈 `0003`을 보냄. 시작은 접속 시각에 고정. 서버 응답으로는 멈추지 않고, 조절기 이벤트나 서버 명령이 ACK로 오가면 멈춤. 해는 없음. 응답 방식은 `--keepalive-reply echo|none|time|time6|identity|probe`(기본 echo). 기록은 `keepalive`, `keepalive_run_end`, `status`의 `keepalives`. Message Reference 5-1절 |
| 세션 | 같은 허용 접속원에서 새 연결이 오면 예전 세션을 닫고 교체(`session_replaced`). 900초 동안 수신이 없으면 종료(`idle_timeout`, `--idle-timeout`). 접속 후 180초 동안 **아무 데이터도** 없으면 서버가 연결을 닫음(`silent_timeout`, `--silent-timeout`, 0이면 끔). 연속 3번이면 `silent_reconnect_loop` 경고, `status`의 `silent_closes`에 횟수 표시. `0001` 없이 `01FF`로 이어 붙은 연결은 재개된 세션으로 인정(`session_resumed`, `status`의 `resumed`, 끄려면 `--no-resumed-session`). 이전 동작(새 연결 거부)은 `--no-replace-session` |
| 상태 해석 | 확인된 메시지와 `01FF`를 이름 붙은 값으로 해석(`state_decoded`). CLI `status`의 `device`, `device` 명령으로 확인 |
| 신규 메시지 | 기록된 형식과 다른 메시지를 `novel.*.jsonl`에 따로 기록(사유·직전 marker·최근 프레임). `nctr_novel 회차` |
| 제어 | 온도(10~45)·전원·온수(35~60)·목욕·외출, 온돌(45~85) 명령이 CLI 기본값으로 켜져 있음. `--no-control`(전부 끔), `--disable 기능`(하나씩 끔). observe 모드에서는 항상 꺼짐. 대기 명령은 하나. ACK를 받은 명령은 다음 명령으로 대체(`command_superseded`). 다음 `01FF` 저장값으로 적용 대조. 충돌 없는 세션 토큰, 자동 재전송 없음 |
| 적용 판정 | ACK와 실제 적용 구분. `lcd`는 사용자 관측, 이벤트의 목표 일치는 후보로 기록 |
| 저장 | 기본 8 MiB 단위 회전, 실행당 1 GiB 예산, 64 MiB 여유. 오류 시 제어 차단·연결 종료 |
| 분석 | RX 원본 재생, 프레임 비교·ID/길이 목록·상태 차분·명령/ACK 지연, 무결성 검사 |
| 기존 PCAP | TShark로 추출 후 TCP seq 재조립·재전송 중복 제거·gap/conflict 표시 |
| 현장 보조 | `nctr_field.sh`: 설정 파일 하나로 캡처·수집·종료 정리. `nctr_windows_field.ps1`: Windows DNS 백업·변경·원복, 회수, Wireshark 첨부 |
| 미구현 | 예약 원격 명령, 실제 보안 페어링·HA, DNS/HTTP 에뮬레이션·프로비저닝 자동화 |

조절기는 ACK를 받지 못한 이벤트를 3~5초마다 다시 보내고, 그동안 다음 이벤트를 보내지 않습니다(2026-11-20·21 관측: `0104`·`0108`·`010A`). 그래서 기본 ACK 규칙에 `0101`·`0102`·`0104`·`0105`·`0106`·`0108`·`010A`·`0115`를 두고, 규칙에 없는 장치 이벤트(`0x0100~0x01FE`)도 ACK합니다. 규칙에 없던 이벤트는 `unknown_event`(`acknowledged=true`, `policy=ack_unlisted_events`)로 원본과 함께 남습니다. 이전 동작은 `--no-ack-unlisted`입니다. 기본 상태 TTL 900초, ACK 기한 10초, 보고 기한 720초는 실험값입니다.

## 3. 준비물

원본 자료 폴더의 `dist/field_kit_20261122/`에 모아 두었습니다(저장소에는 없음).

| 파일 | 위치 | 용도 |
|---|---|---|
| `nctr_lab_preflight_20261122l.zip` | NAS로 전송 | 코드·테스트·문서·`tools/nctr_field.sh`. SHA-256은 `SHA256SUMS.txt` |
| `node_id.hex` | NAS `~/nctr-lab/private/node_id.hex` (600) | 10월 17일 캡처 6110번 프레임의 8바이트 ID. `0000` 시작일 때만 사용. 비공개 |
| `ipad/` | 아이패드 '파일' 앱 | 이 문서, 체크리스트, 실험 계획 |
| `tools/nctr_windows_field.ps1` | Windows 저장소에 그대로 | 관리자 PowerShell에서 실행 |

NAS에 배치한 ZIP 안의 문서는 배포 당시 판입니다. 현장에서는 아이패드로 옮긴 최신 문서를 봅니다. 코드는 같습니다.

배포 묶음을 다시 만들 때는 Windows `research`에서 다음을 실행합니다. 같은 파일명은 덮어쓰지 않으므로 이름 끝 글자를 바꿉니다.

```powershell
python -m unittest discover -s tests -v
python src/nctr_lab_cli.py bundle --output dist/field_kit_20261122/nctr_lab_preflight_20261122m.zip
```

## 4. NAS 준비

### 4.1 전송 (Windows PowerShell, `research`)

```powershell
scp .\dist\field_kit_20261122\nctr_lab_preflight_20261122l.zip "nas:"
scp .\dist\field_kit_20261122\node_id.hex "nas:nctr-lab/private/node_id.hex"
```

`nctr-lab/private`가 없으면 두 번째 명령이 실패합니다. 4.2의 `mkdir`를 먼저 실행한 뒤 다시 보냅니다.

### 4.2 릴리스 배치 (NAS)

대화형 셸에서는 `set -e`를 쓰지 않습니다. 명령 하나가 실패하면 SSH 세션이 닫힙니다. ZIP은 홈이 아니라 릴리스 폴더 안에서 풉니다.

```bash
sha256sum ~/nctr_lab_preflight_20261122l.zip
mkdir -p ~/nctr-lab/releases ~/nctr-lab/private && chmod 700 ~/nctr-lab ~/nctr-lab/private
mkdir ~/nctr-lab/releases/preflight-20261122l && cd ~/nctr-lab/releases/preflight-20261122l
python3 -m zipfile -e ~/nctr_lab_preflight_20261122l.zip .
python3 src/nctr_lab_cli.py check-bundle --root .
python3 -m unittest discover -s tests
chmod 600 ~/nctr-lab/private/node_id.hex
```

`sha256sum` 값은 `SHA256SUMS.txt`와 같아야 합니다. `check-bundle`은 `deployment_files_verified: 27`, 자동 시험은 `OK`가 정상입니다. 테스트가 실패하면 실물 시험으로 진행하지 않습니다.

### 4.3 설정 파일 (NAS)

```bash
source ~/nctr-lab/releases/preflight-20261122l/tools/nctr_field.sh
type nctr_setup | grep -n 'cat\|bat'
nctr_setup
ip -4 -o addr show | grep 192.168.1.8
vi ~/nctr-lab/field.env
```

`type` 결과에 `bat`이 보이면 예전 판 함수가 남은 셸입니다. 새 터미널을 열고 다시 source합니다. `ip` 결과의 두 번째 칸이 인터페이스 이름입니다(현재 `enp3s0`). IP 주소를 넣으면 tcpdump가 `No such device exists`로 종료합니다.

```text
NCTR_DATE='시험일 YYYYMMDD'
NCTR_SERVER_IP='192.168.1.8'
NCTR_DNS_IP='192.168.1.8'
NCTR_PEER_IP='192.168.1.2'
NCTR_CAPTURE_IFACE='enp3s0'
```

`NCTR_DATE`는 자정을 넘겨도 바꾸지 않습니다. `NCTR_TEMP_MIN`·`NCTR_TEMP_MAX`는 10절 회차에서만 채웁니다. 저장 후 `nctr_show`로 확인합니다.

### 4.4 보조 함수 목록

`source ~/nctr-lab/releases/preflight-20261122l/tools/nctr_field.sh` 후 사용합니다. 새 터미널·tmux 창마다 source합니다.

| 함수 | 하는 일 |
|---|---|
| `nctr_setup` | `~/nctr-lab/field.env` 틀과 `experiments/`·`pcaps/`·`private/` 생성 |
| `nctr_show` | 설정값 표시 |
| `nctr_preflight` | IPv4 주소, 53·20000 포트 사용 프로세스, 도구, 노드 ID 파일, `doctor`(바인딩·여유 공간·DNS 응답) |
| `nctr_dnsmasq` | 임시 DNS. `igs4.krb.co.kr`만 `NCTR_SERVER_IP`로, 나머지는 1.1.1.1로 전달. 질의 로그를 화면에 출력 |
| `nctr_watch_syn` | TCP/20000 SYN 관찰(접속원 확인용) |
| `nctr_capture 회차` | `~/nctr-lab/pcaps/field-<날짜>-<회차>/server_tcp.pcap`에 TCP/20000 캡처 |
| `nctr_collect 회차 [옵션]` | 수집 서버. `--mode handshake --time-tail 0123`, 접속원 제한 포함. 원격 명령은 모두 켜진 상태. 추가 옵션은 그대로 전달 |
| `nctr_collect_node 회차` | `--node-id-file`을 붙인 수집 서버 |
| `nctr_collect_temp 회차` | 실내 온도 범위를 field.env의 `NCTR_TEMP_MIN`·`MAX`로 좁힌 수집 서버 |
| `nctr_finish 회차` | PCAP 권한 정리 → verify → attach → analyze → export → verify |
| `nctr_runs` | 해당 날짜의 실행 폴더 목록 |
| `nctr_novel 회차` | 신규 메시지 로그 요약(시각·ID·본문·사유·직전 marker) |
| `nctr_watch 회차` | 실시간: 해석된 값, 신규 메시지, 세션·명령 사건 |

실행 폴더는 `~/nctr-lab/experiments/field-<날짜>-<회차>`입니다. 같은 이름이 있으면 시작을 거부합니다.

## 5. DNS와 사전 점검 (NAS)

NAS에서 별도 DNS를 운영하지 않으므로 시험 동안 `nctr_dnsmasq`를 띄웁니다. tmux 창 0번을 DNS 전용으로 씁니다.

```bash
tmux new -s nctr
source ~/nctr-lab/releases/preflight-20261122l/tools/nctr_field.sh
nctr_dnsmasq
```

이전에 수동으로 실행한 dnsmasq가 있으면 먼저 종료합니다. 직접 실행할 때는 `--host-record=igs4.krb.co.kr,192.168.1.8`처럼 쉼표 뒤에 공백을 넣지 않습니다(`junk found in command line` 오류).

`Ctrl+b c`로 새 창을 열고 점검합니다.

```bash
source ~/nctr-lab/releases/preflight-20261122l/tools/nctr_field.sh
nctr_preflight
```

| 확인 항목 | 정상 |
|---|---|
| 53번 포트 | dnsmasq만 사용 |
| 20000번 포트 | 사용 프로세스 없음(이전 v4 서버 종료) |
| 도구 | `python3`·`tcpdump`·`tmux` 있음 |
| doctor DNS | `igs4.krb.co.kr` → `192.168.1.8`, `igis.krb.co.kr` → 제조사 IP |

`doctor`는 NAS DNS의 응답만 확인합니다. 조절기가 실제로 그 응답을 받았는지는 Wireshark로 확인합니다(6절·7절). NAS 방화벽을 쓰고 있다면 `192.168.1.2`에서 오는 DNS(UDP/TCP 53)와 TCP/20000만 허용합니다.

## 6. Windows 준비 (관리자 PowerShell, `research`)

1. 설정 → 네트워크 및 인터넷 → 모바일 핫스팟
   - 대역을 **2.4GHz**로 고정합니다. 조절기 Wi-Fi 모듈(WizFi250)은 2.4GHz만 지원합니다.
   - SSID·암호는 영문·숫자만 사용합니다.
   - 절전(연결된 장치가 없으면 자동으로 끄기)을 끕니다.
2. PC 절전 모드를 끕니다. 시험 동안 PC는 켜 둡니다.
3. Windows 이더넷 DNS를 NAS로 바꿉니다. 처음 실행할 때만 원래 값을 `%LOCALAPPDATA%\nctr-lab`에 백업합니다.
   ```powershell
   .\tools\nctr_windows_field.ps1 -Action DnsSet -InterfaceAlias '이더넷' -UbuntuDns 192.168.1.8
   ```
4. 핫스팟을 껐다 켠 뒤 확인합니다.
   ```powershell
   .\tools\nctr_windows_field.ps1 -Action DnsCheck -UbuntuDns 192.168.1.8
   ```
   "Windows 기본 경로"의 `igs4`가 `192.168.1.8`, `igis`가 제조사 IP여야 합니다. 핫스팟 클라이언트의 DNS는 Windows를 거쳐 나가므로 이 결과가 조절기가 받을 응답에 가장 가깝습니다. IPv6 DNS 경고가 나오고 `igs4`가 다르게 나오면 진행하지 않습니다.
5. Wireshark에서 핫스팟 인터페이스를 선택해 캡처를 시작합니다. 마지막 회차가 끝날 때까지 한 파일로 계속 받습니다. 확인용 표시 필터:
   ```text
   dns.qry.name contains "krb"
   ip.addr == <조절기IP> && tcp.port == 20000
   ip.addr == <조절기IP> && http
   ```
   조절기 IP는 핫스팟 설정 화면의 연결된 장치 목록에서 확인합니다(과거 `192.168.137.x`).

## 7. 현장 시험 (아이패드 → NAS tmux)

### 7.1 아이패드 준비

- 자동 잠금을 '안 함'으로 두고 충전기를 연결합니다.
- 터미널 앱이 백그라운드로 가면 SSH가 끊길 수 있습니다. 모든 작업은 tmux 안에서 합니다. 끊기면 `tmux attach -t nctr`로 다시 붙습니다.
- marker는 **한 줄씩** 붙여넣습니다.
- 사진은 휴대폰으로 찍고, 출력된 `action_id`와 사진 파일명을 함께 적습니다.

### 7.2 tmux 창

| 창 | 용도 | 만들기 |
|---|---|---|
| 0 | `nctr_dnsmasq` (5절) | 이미 실행 중 |
| 1 = 창 A | `nctr_capture` | `Ctrl+b c` 후 source |
| 2 = 창 B | `nctr_collect`, marker 입력 | `Ctrl+b c` 후 source |

창 이동은 `Ctrl+b 0`/`1`/`2`, 세션에서 잠시 빠지는 것은 `Ctrl+b d`입니다.

### 7.3 회차 진행

조절기 설정(목표온도·모드·온수·외출·예약)을 사진으로 남기고 시작합니다. 회차별 조작과 marker는 [체크리스트](NCTR200_Field_Marker_Checklist.md)를 따릅니다.

| 회차 | 시험 | 창 A | 창 B | 끝난 뒤 |
|---|---|---|---|---|
| 0 | AP 초기화·핫스팟 프로비저닝·첫 접속 | `nctr_capture 00` | `nctr_collect 00` | `nctr_finish 00` |
| 1 | E01~E04 | `nctr_capture 01` | `nctr_collect 01` | `nctr_finish 01` |
| 2 | E06~E09 | `nctr_capture 02` | `nctr_collect 02` | `nctr_finish 02` |
| 3 | E10 예약 | `nctr_capture 03` | `nctr_collect 03` | `nctr_finish 03` |
| 4 | E11 등록 코드 | `nctr_capture 04` | `nctr_collect 04 --pair-code 4321` | `nctr_finish 04` |
| 5 | E13 서버 재시작, E14 관찰 | `nctr_capture 05a` (05b 동안 유지) | `nctr_collect 05a` → `quit` → `nctr_collect 05b` | `nctr_finish 05a`, `nctr_finish 05b` |

회차마다 종료 순서는 같습니다.

1. 창 B: 마지막 marker 후 `quit`
2. 창 A: `Ctrl+C`
3. `nctr_finish 회차`

`nctr_finish`의 첫 verify가 종료 코드 2(`collection_clean=false`)면 원본을 보존한 채 계속 진행하고, 다른 오류면 멈춥니다. 출력은 Windows에서 다시 확인합니다.

### 7.4 회차 0의 분기

AP 초기화 → 휴대폰을 `krb_set`에 연결 → 제조사 앱에서 핫스팟 SSID 지정 → **등록 코드 입력 화면에서 중단**. 이후 창 B에서 `status`를 1~3분 반복합니다.

| `status` | 판단 | 조치 |
|---|---|---|
| `connected=true`, `identity=true`, `ready=true` | `0001` 시작, 성공 | 회차 1로 진행 |
| `connected=true`, 몇 분 지나도 `identity=false` | `0000` 시작 추정 | 창 B `quit` → 창 A `Ctrl+C` → 창 A `nctr_capture 00b` → 창 B `nctr_collect_node 00b` |
| `connected=false` 계속 | DNS 경로 문제 | TCP 시험 중단. Wireshark에서 조절기가 받은 `igs4` 응답과 SYN 목적지 확인 |

노드 ID 파일을 지정하면 그 ID로 고정(pinning)됩니다. 조절기가 다른 ID로 `0001`을 보내면 연결을 끊으므로, `0000`이 확인된 경우에만 씁니다.

## 8. 종료·원복

1. 조절기 설정을 시작 전 사진과 같게 복구하고, 마지막 회차의 `quit` 전에 `mark 복구 완료: ...`를 남깁니다.
2. NAS 창 0에서 `Ctrl+C`로 dnsmasq를 종료합니다.
3. Windows DNS를 원래 값으로 되돌리고 핫스팟을 껐다 켭니다.
   ```powershell
   .\tools\nctr_windows_field.ps1 -Action DnsRestore -InterfaceAlias '이더넷'
   ```
4. Wireshark 캡처를 멈추고 원본 자료 폴더의 `captures\hotspot_<날짜>.pcapng`로 저장합니다.

원복 후에도 조절기는 Windows 핫스팟에 연결된 상태입니다. PC가 꺼져 있으면 제조사 앱 연결이 끊깁니다. 조절기 버튼·다이얼은 영향이 없습니다. 평소 운영으로 돌아가려면 조절기를 ipTIME으로 다시 프로비저닝합니다.

## 9. 회수·첨부 (Windows PowerShell, `research`)

```powershell
.\tools\nctr_windows_field.ps1 -Action Retrieve -SshTarget nas -Date <날짜>
.\tools\nctr_windows_field.ps1 -Action AttachWireshark -RunId field-<날짜>-00 -Path .\captures\hotspot_<날짜>.pcapng
```

`Retrieve`는 `field-<날짜>-*` 실행 폴더를 `experiments\`로 복사하고 각각 verify합니다. 이미 있는 폴더는 건너뜁니다. Wireshark 파일은 하루치 한 파일이므로 0회차 실행 폴더에 첨부합니다.

분석 결과(`analysis/`)는 비공개입니다. `analysis/shared_metadata.jsonl`은 payload·raw·marker 설명·IP를 뺀 공유용 메타데이터입니다. 사진·관측 메모는 실행 폴더 밖에 작성한 뒤 `attach --category media` 또는 `--category observations`로 첨부합니다.

## 10. 원격 명령 시험 (별도 회차)

수집 회차와 원래 설정 복구를 확인한 뒤 별도 회차에서 한 기능씩 진행합니다. 명령 형식과 근거는 [메시지 참조](NCTR200_Message_Reference.md) 4절입니다.

모든 명령이 기본으로 켜져 있습니다. 끄려면 `--disable 기능`(temperature, power, hot_water, bath, away, ondol, hot_water_only, reservation, heating_water, schedule), 전부 끄려면 `--no-control`을 씁니다.

| 기능 | 기본 범위(바꾸는 옵션) | CLI | 형식 근거 |
|---|---|---|---|
| 실내 희망온도 | 10~45(`--temp-min`·`--temp-max`, 또는 `nctr_collect_temp`) | `temp 27` | 제조사 캡처 |
| 전원 | - | `power off` / `power on` | 제조사 캡처 |
| 온수 설정 | 35~60(`--hot-water-min`·`--hot-water-max`) | `hotwater 55` | 제조사 캡처 |
| 목욕 | - | `bath` | 제조사 캡처 |
| 외출 | - | `away` | 제조사 캡처 |
| 온돌 희망온도 | 45~85(`--ondol-min`·`--ondol-max`) | `ondol 46` | 실물 확인(`field-20261122-20`) |
| 온수 전용 | 35~60(온수 범위) | `hotwateronly` 또는 `hotwateronly 60` | 실물 확인(11-22) |
| 난방 예약 | 정지 1~24시간, 가동 10~90분 | `reserve 3 20`(정지 3시간, 가동 20분) | 제조사 캡처, 실물 확인(11-22). 가동·정지 순서는 조절기에서만 변경 |
| 난방수 온도 | 45~85, 1도 단위 | `heatwater 50` (`0103`만 보냄. 온수는 `hotwater`로 따로) | 제조사 캡처, 실물 확인(11-22) |
| 24시간 예약 | 30분 단위 | `schedule 00:00-09:00,18:00-24:00` | 제조사 캡처, 실물 확인(11-22) |

예: `nctr_collect 20` (모두 켜짐), `nctr_collect 20 --disable ondol` (온돌만 끔)

1. `status`로 `ready=true`와 `device`의 현재 값을 확인하고 복구값을 적어 둡니다.
2. 명령을 입력하면 보낼 ID·본문이 표시됩니다. `SEND`를 입력해야 한 번 전송하고 `command_id`가 출력됩니다.
3. LCD를 확인하고 `lcd <command_id> 값`을 입력합니다. `bath`·`away`는 적용됐으면 `1`, 전원은 ON=1·OFF=2, 난방 예약은 `3:20`(정지 시간:가동 분), 24시간 예약은 `00:00-09:00,18:00-24:00`입니다. 미검증 명령은 보내기 전에 경고가 표시됩니다.
4. 조절기는 원격 명령에 ACK만 보내고 값을 다시 보고하지 않습니다. 다음 상태 보고(약 5분 이내)의 저장값이 요청값과 같으면 `command_report_candidate`(`source=01FF[n]`)가 기록되고, LCD 관측도 맞으면 `command_observed_match`가 기록됩니다.
5. ACK를 받은 뒤라면 `cancel` 없이 바로 다음 명령(예: 원복)을 보낼 수 있습니다. 앞 명령은 `command_superseded`로 남습니다.
6. 외출 해제는 `temp N`처럼 원하는 모드의 명령으로 합니다.

ACK만으로 적용 완료로 판정하지 않습니다. 자동 재전송하지 않습니다. ACK 전에는 다음 명령을 막습니다. `cancel`은 추적만 끝내며 보낸 설정을 되돌리지 않습니다. LCD·상태 보고가 다르면 불일치로 기록하고 물리 조절기로 복구합니다.

## 11. 진단·복구

| 현상 | 먼저 확인할 것 |
|---|---|
| `field.env`에 색상 코드·줄 번호가 섞임 | 예전 판 함수가 남은 셸. `rm ~/nctr-lab/field.env` 후 새 터미널에서 source, `type nctr_setup`에 `bat`이 없는지 확인 |
| dnsmasq `junk found in command line` | `--host-record` 값의 쉼표 뒤 공백. `nctr_dnsmasq` 사용 |
| dnsmasq가 53번 포트를 열지 못함 | `sudo ss -lntup \| grep ':53\b'`로 기존 DNS 확인 |
| `DnsCheck`의 `igs4`가 제조사 IP | 핫스팟 재시작 여부, IPv6 DNS, `DnsSet` 대상 어댑터 |
| doctor는 통과했는데 TCP가 없음 | Wireshark에서 조절기가 받은 DNS 응답·SYN 목적지. 서버의 DNS 조회만으로 경로 성공을 판단하지 않음 |
| 연결이 거부됨(`client_ip`) | NAS에서 보이는 접속원과 `NCTR_PEER_IP`(`nctr_watch_syn`), 다른 리스너 |
| 조절기가 DHCP → igis HTTP → 접속을 반복하고 수집기는 `concurrent_session`으로 거부 | 2026-11-20d 이전 판 수집기. 조절기가 Wi-Fi를 재연결하며 예전 TCP 세션을 FIN 없이 버린 상태. 20261121 판은 새 연결로 교체하므로 `session_replaced`가 기록되는지 확인 |
| 수집기를 다시 띄운 뒤 연결은 됐는데 `identity=false`가 계속됨 | `resumed=true`이고 `ready=true`면 정상(재개된 세션). 데이터가 전혀 없으면 조용한 연결 상태로, 보통 130초 안팎에 조절기가 스스로 Wi-Fi를 재연결하지만 7분 넘게 멈춘 적도 있음(11-22 23 회차). 180초 뒤 서버가 닫음. `silent_closes`가 3 이상이거나 재접속이 없으면 핫스팟·AP를 껐다 켬 |
| `0001` 응답 직후 조절기가 DHCP부터 다시 시작 | 2026-11-21 23:11·23:12 관측, 원인 미확정. 교체 후에도 반복되는지와 직전 실패 이력을 기록 |
| `0001` 반복·ready 미도달 | RX·TX 원본, 시간 응답 정책·상태 길이. 제어를 켜지 않음 |
| `identity_mismatch`로 끊김 | 노드 ID 파일의 ID와 실제 ID가 다름. `nctr_collect`로 다시 시작 |
| 미해독 이벤트 이후 재전송·단절 | ID·body·제조사 ACK 규칙 비교. 근거와 함께 ACK 추가 |
| 명령 ACK와 LCD·보고 불일치 | 모드·owner 필드·조작 시각·보고 지연·다른 명령 분리 |
| 파일·PCAP 누락, 해시 불일치 | 원본 보존, 시험 미판정, 저장·캡처 오류와 종료 상태 확인 |
| manifest가 `running`에 남음 | 강제 종료된 회차. 분석 도구가 거부하며, 표시만 바꾸지 않음 |

정상 운영 복구가 필요하면 시험 명령·예약을 중단하고 물리 설정을 복구합니다. 8절대로 DNS를 원복한 뒤 조절기의 제조사 접속을 확인합니다. 공유기 전체나 보일러 전원선을 끄는 조작을 진단의 기본 단계로 삼지 않습니다.

## 12. 수동 명령 (보조 스크립트를 쓰지 않을 때)

보조 함수가 실행하는 명령과 같습니다. 변수는 field.env 값입니다.

```bash
# 창 A
mkdir -p ~/nctr-lab/pcaps && mkdir -m 700 ~/nctr-lab/pcaps/$RUN_ID
sudo tcpdump -i "$NCTR_CAPTURE_IFACE" -s 0 -U -Z "$(id -un)" \
  -w ~/nctr-lab/pcaps/$RUN_ID/server_tcp.pcap \
  "host $NCTR_PEER_IP and tcp port 20000" 2> ~/nctr-lab/pcaps/$RUN_ID/tcpdump_stats.txt

# 창 B (릴리스 폴더에서)
python3 src/nctr_lab_cli.py collect \
  --bind "$NCTR_SERVER_IP" --allowed-client-ip "$NCTR_PEER_IP" \
  --mode handshake --time-tail 0123 \
  --output-root ~/nctr-lab/experiments --run-id "$RUN_ID"

# 종료 후
RUN_DIR=~/nctr-lab/experiments/$RUN_ID
python3 src/nctr_lab_cli.py verify --run-dir "$RUN_DIR"
python3 src/nctr_lab_cli.py attach --run-dir "$RUN_DIR" --input ~/nctr-lab/pcaps/$RUN_ID/server_tcp.pcap --category network
python3 src/nctr_lab_cli.py attach --run-dir "$RUN_DIR" --input ~/nctr-lab/pcaps/$RUN_ID/tcpdump_stats.txt --category network
python3 src/nctr_lab_cli.py analyze --run-dir "$RUN_DIR" --output "$RUN_DIR/analysis"
python3 src/nctr_lab_cli.py export --run-dir "$RUN_DIR" --output "$RUN_DIR/analysis/shared_metadata.jsonl"
python3 src/nctr_lab_cli.py verify --run-dir "$RUN_DIR"
```

`export` 출력은 반드시 `analysis/` 아래에 둡니다. 실행 폴더 바로 아래에 `.jsonl`을 만들면 verify가 파일 목록 불일치로 실패합니다.

## 13. 제조사 서버 장시간 캡처 (`0003` 응답 확인)

목적: 조절기가 접속 60분 8초 뒤부터 보내는 `0003`에 제조사 서버가 무엇으로 답하는지, 그 응답으로 반복이 멈추는지 확인합니다. 지금까지의 제조사 캡처(261017 약 151초, boiler6 약 164초)는 1시간보다 짧아서 `0003`이 나오지 않았습니다. 수집기는 쓰지 않습니다.

### 13.1 확인값 (2026-11-22 22:55)

| 항목 | 값 |
|---|---|
| Windows 이더넷 DNS | 원래 값(KT). `igs4`·`igis` 모두 제조사 IP `121.141.70.70`으로 풀림 |
| 핫스팟 인터페이스 | `Wi-Fi 4`, `192.168.137.1` |
| TShark | `C:\Program Files\Wireshark\tshark.exe` |
| PC 절전 | 전원 연결 시 절전 안 함 |

DNS가 NAS로 바뀌어 있으면 조절기가 NAS로 붙습니다. 시작 전에 확인합니다.

```powershell
Clear-DnsClientCache; Resolve-DnsName igs4.krb.co.kr -Type A -DnsOnly
```

### 13.2 절차

1. **NAS 수집기 정리**: 수집기가 돌고 있으면 창 B `quit` → 창 A `Ctrl+C` → `nctr_finish 회차`.
2. **Windows 점검**
   - 모바일 핫스팟의 "연결된 장치가 없으면 자동으로 끄기"를 끕니다. AP 초기화 동안 연결된 장치가 없습니다.
   - 밤사이 재시작되지 않도록 Windows Update를 일시 중지합니다.
3. **캡처 시작 (PowerShell, 프로비저닝 전)**: DHCP·DNS·첫 `0001` 교환까지 받도록 먼저 시작합니다. 1시간마다 파일을 나눕니다. 핫스팟에는 조절기만 붙으므로 필터를 두지 않습니다. 창은 아침까지 닫지 않습니다.
   ```powershell
   $ts = Get-Date -Format yyMMdd_HHmm
   & 'C:\Program Files\Wireshark\tshark.exe' -i 'Wi-Fi 4' -q -b duration:3600 -w "<원본 자료 폴더>\captures\maker_night_$ts.pcapng"
   ```
4. **제조사 앱으로 프로비저닝**
   1. 조절기 AP 초기화(전원 OFF 상태에서 외출 버튼 5초).
   2. 휴대폰을 `krb_set`에 연결합니다.
   3. 앱에서 Wi-Fi로 Windows 핫스팟 SSID를 지정합니다.
   4. 등록을 요구하면 평소대로 진행합니다. 이미 등록된 기기라면 그대로 둡니다.
   5. 핫스팟 설정 화면의 연결된 장치 목록에서 조절기 IP(`192.168.137.x`)를 적어 둡니다.
   6. 앱에 상태가 보이면 연결된 것입니다. 시각을 적어 둡니다.
5. **밤사이**
   - 접속 후 70분 동안은 앱과 조절기를 건드리지 않습니다. 이벤트나 명령이 오가면 `0003` 반복이 멈춰서 제조사 서버의 반응을 가리기 어렵습니다.
   - `0003`은 접속 약 60분 8초 뒤 시작될 것으로 예상합니다.
   - 05:05쯤 공유기가 재시작합니다. 이때의 재접속도 함께 기록됩니다.
6. **아침**: 캡처 창에서 `Ctrl+C`.

### 13.3 볼 것

| 항목 | 표시 필터·방법 |
|---|---|
| `0003` 시작 시각과 간격 | `tcp.port == 20000 && tcp.payload[0:2] == 00:03` |
| 제조사 서버의 `0003` 응답(내용, 지연) | 같은 필터에서 서버 → 조절기 방향 |
| 응답 뒤 반복이 멈추는지 | 첫 `0003`과 다음 시작(60분 8초 뒤) 사이의 개수 |
| 1시간 안에 서버가 먼저 보내는 메시지 | `tcp.port == 20000 && ip.src == 121.141.70.70 && tcp.len > 0` |
| 05:05 재접속이 `0001`부터인지 | `tcp.flags.syn == 1 && tcp.port == 20000`과 그 뒤 첫 프레임 |

### 13.4 끝난 뒤

조절기는 제조사 서버에 붙은 상태로 남습니다. NAS나 Home Assistant 통합으로 돌아가려면 [자체 프로비저닝](NCTR200_Self_Provisioning.md)으로 다시 설정합니다.

## 14. 확인한 결과

- 2026-10-25: Windows Python 3.13.15에서 모의·loopback 자동 시험 52개 통과. 기존 PCAP 3개의 payload 21·30·12개를 TShark 4.6.9로 재조립했고 RX 원본 재생이 프레임 로그와 일치했습니다.
- 2026-11-20~21: 보조 스크립트를 Git Bash 루프백(127.0.0.1)으로 설정 → 수집 → `nctr_finish`까지 실행해 최종 verify 통과. alias가 걸린 대화형 셸(NAS 포함)에서 `nctr_setup`이 정상 파일을 만드는 것을 확인했습니다. 이 과정에서 이전 절차의 export 경로 오류를 찾아 고쳤습니다.
- NAS: 배포 묶음 `check-bundle` 23개 파일 통과.
- 2026-11-21: 실물 회차 00~05 수행(03 미실행). 조절기가 Wi-Fi 재연결 시 예전 세션을 버려 수집기가 새 연결을 거부하는 문제를 확인하고, 세션 교체·무수신 종료·종료 시 `Bad file descriptor` 오기록을 고쳤습니다. 자동 시험 55개 통과. 이어서 11 회차에서 ACK가 없는 이벤트의 재전송·후속 이벤트 차단을 확인하고, 규칙에 없는 장치 이벤트도 ACK하도록 바꿨습니다(자동 시험 58개 통과).
- 2026-11-22: 15 회차에서 온돌(`0104`)·온수(`0115`)·목욕(`0105`)·예약(`0108`) 형식을 LCD와 대조해 확인했습니다. 상태 해석, 신규 메시지 별도 로그, 온수·목욕·외출·온돌(미검증) 원격 명령을 추가했습니다(자동 시험 63개 통과). 새 원격 명령은 실물에서 아직 시험하지 않았습니다.
- 2026-11-22 `field-20261122-20`: `ondol 50` → `ondol 45` 원격 명령이 ACK되고 LCD와 `01FF[14]`에 반영됨을 확인했습니다. 조절기가 원격 명령 뒤 값을 보고하지 않아 앞 명령이 대기로 남고 원복 명령이 막히는 문제를 찾아, ACK된 명령은 다음 명령으로 대체하고 `01FF` 저장값으로 대조하도록 고쳤습니다(자동 시험 66개 통과).
- 아직 하지 않은 것: NAS에서의 자동 시험 결과 기록, Windows `DnsSet`·`DnsRestore`·`Retrieve`·`AttachWireshark` 실행, 실물 접속. 새 코드로 실물 시험을 하기 전까지 10월 18~19일 기록을 새 결과로 취급하지 않습니다.
