# E01~E15 현장 조작·marker 체크리스트

작성일: 2026-11-09(KST), 2026-11-21 개정(0회차·보조 스크립트·핫스팟 구성 반영)  
기준: [실험 계획](NCTR200_Local_Control_Experiment_Plan.md) 5·6절, [현장 절차](NCTR200_Ubuntu_Field_Test_Runbook.md) 7절  
대상 코드: [nctr_lab_cli.py](../src/nctr_lab_cli.py)의 `collect` 대화형 명령, [tools/nctr_field.sh](../tools/nctr_field.sh)

이 문서는 회차 안에서 수집 서버 화면에 입력할 marker와 그 시점의 물리 조작만 정리합니다. NAS·DNS·Windows 준비, 종료 후 원복·회수는 [현장 절차](NCTR200_Ubuntu_Field_Test_Runbook.md) 4~9절을 따릅니다.

## 사용 규칙

- `mark` 뒤의 내용은 자유 텍스트로 저장됩니다. 장치로 송신하지 않습니다. `E01` 같은 번호는 실험 계획 5절의 시험 번호이며, 분석 시 검색용입니다.
- **한 줄씩 입력합니다.** 여러 줄을 한 번에 붙여넣으면 같은 시각으로 기록돼 조작 전후를 구분할 수 없습니다. "직전" 줄은 조작 바로 전에, "직후" 줄은 조작 후 LCD를 확인한 다음 입력합니다.
- `?`는 실제 LCD 값으로 바꿔 입력합니다. 예시 숫자(26, 27 등)도 실제 화면과 다르면 고칩니다.
- 조작 후 최소 60초는 다음 조작을 하지 않습니다. 가능하면 다음 `01FF` 상태 보고(과거 기록상 약 5분 간격)까지 기다립니다.
- 입력할 때마다 출력되는 `action_id`를 사진 파일명과 함께 적어 둡니다.
- 회차 이름은 `00`, `01`처럼 짧게 씁니다. 스크립트가 `field-<NCTR_DATE>-<회차>`로 실행 폴더를 만듭니다. 같은 이름은 덮어쓰지 않고 거부합니다.

| 회차 | 시험 | 서버 옵션 |
|---|---|---|
| 0 | E14 일부: AP 초기화 후 Windows 핫스팟으로 프로비저닝, 첫 접속 | 수집만. `0000` 시작이면 `00b`로 재시작 |
| 1 | E01, E02, E03, E04 | 수집만(제어 없음) |
| 2 | E06, E07, E08, E09 | 수집만 |
| 3 | E10 | 수집만 |
| 4 | E11 | `--pair-code` 추가 |
| 5 | E13, E14(관찰만) | 수집만, 중간에 서버 재시작 |
| 6 | E05 | `--enable-temperature` |
| 해당 없음 | E12, E15 | 마지막 절 참고 |

## 회차 공통 명령

시작 전에 현장 절차 5·6절이 끝나 있어야 합니다. NAS tmux 창 0에서 `nctr_dnsmasq` 실행 중, `nctr_preflight` 통과, Windows `DnsCheck`에서 `igs4`가 `192.168.1.8`, Wireshark 캡처 중인 상태입니다.

tmux 창 1(창 A)·창 2(창 B)에서 각각 `source ~/nctr-lab/releases/preflight-20261122l/tools/nctr_field.sh`를 먼저 실행합니다. 회차 이름이 `01`이라면 다음 순서입니다.

1. 창 A: `nctr_capture 01` (TCP 캡처 시작)
2. 창 B: `nctr_collect 01` (수집 서버 시작, 아래 marker 입력)
3. 창 B: 마지막 marker 후 `quit`
4. 창 A: `Ctrl+C` (캡처 종료)
5. 창 A 또는 B: `nctr_finish 01` (권한 정리·verify·attach·analyze·export)

Windows Wireshark 캡처는 0회차 전에 시작해 마지막 회차가 끝날 때까지 한 파일로 계속 받습니다.

---

## 회차 0: AP 초기화·핫스팟 프로비저닝 (E14 일부)

준비: 위 '회차 공통 명령'의 시작 조건. 조절기 설정(목표·모드·온수·외출·예약) 사진. 휴대폰에 제조사 앱.

창 A `nctr_capture 00`, 창 B `nctr_collect 00`

```text
mark E14-0 시작: AP 초기화 전, LCD 목표 ?, 모드 ?, 온수 설정 ?, 예약 사진 촬영 완료
```
```text
mark E14-1 직전: AP 초기화 후 Windows 핫스팟으로 프로비저닝 예정
```
→ 조절기 AP 초기화, 휴대폰을 `krb_set`에 연결, 제조사 앱에서 핫스팟 SSID 지정. **등록 코드 입력 화면에서 멈추고 계정 등록은 하지 않습니다.**
```text
mark E14-1 직후: 앱 Wi-Fi 설정 전송 완료, 등록 단계 진행 안 함
```
1~3분 동안 `status`를 반복합니다.
```text
status
```
- `connected=true`, `identity=true`, 이후 `ready=true`: 성공. 아래를 입력하고 그대로 회차 1로 넘어갑니다.
  ```text
  mark E14-2 첫 접속 확인: 0001 시작, ready 도달, 프로비저닝 후 ?분
  ```
- `connected=true`인데 몇 분이 지나도 `identity=false`: `0000` 시작일 가능성이 큽니다.
  ```text
  mark E14-2 첫 접속: identity 미확보, 0000 시작 추정, 00b로 재시작 예정
  ```
  `quit` → 창 A `Ctrl+C` → 창 A `nctr_capture 00b` → 창 B `nctr_collect_node 00b`. 조절기가 다시 접속하면 `status`로 `ready=true`를 확인합니다.
  ```text
  mark E14-3 노드 ID 공급 후 접속 확인: ready ?
  ```
- `connected=false`가 계속되면 DNS 경로 문제입니다. TCP 시험으로 넘어가지 않고 Wireshark에서 조절기가 받은 `igs4` 응답을 확인합니다.

종료: `quit` → 창 A `Ctrl+C` → `nctr_finish 00` (재시작했다면 `nctr_finish 00b`도)

---

## 회차 1: E01·E02·E03·E04

창 A `nctr_capture 01`, 창 B `nctr_collect 01`

### E01 초기 연결·무조작 기준선

조작하지 않고 상태 보고 3개(약 15분)를 받습니다.

```text
mark E01 시작: 실내난방 모드, LCD 현재 ?, 목표 ?, 외출 해제, 온수 설정 ?
```
```text
status
```
`ready=true` 확인 후:
```text
mark E01 준비 확인: 조작 없이 기준선 대기 시작
```
상태 보고 3개 수신 후:
```text
mark E01 기준선 종료: 무조작 유지, LCD 현재 ?, 목표 ?
```

### E02 실내 목표온도 (다이얼 1단위 왕복)

```text
mark E02-1 직전: 목표 26에서 27로 올릴 예정, LCD 현재 ?
```
→ 다이얼로 27
```text
mark E02-1 직후: LCD 목표 27, 현재 ?
```
→ 60초 이상, 가능하면 다음 상태 보고까지 대기
```text
mark E02-2 직전: 목표 27에서 26으로 내릴 예정
```
→ 다이얼로 26
```text
mark E02-2 직후: LCD 목표 26, 현재 ?
```

### E03 외출 진입·해제

```text
mark E03-1 직전: 외출 진입 예정, LCD 목표 26, 모드 실내난방
```
→ 외출 버튼
```text
mark E03-1 직후: LCD 외출 표시 ?, 목표 표시 ?
```
→ 60초 이상 대기
```text
mark E03-2 직전: 외출 해제 예정
```
→ 외출 버튼
```text
mark E03-2 직후: LCD 외출 해제 ?, 목표 ? (외출 전 26 유지 여부)
```

### E04 현재온도 자연 변화

조작하지 않습니다. LCD 현재온도가 바뀌는 것을 봤을 때만 입력합니다. 변화가 없으면 생략하고 미판정으로 둡니다.

```text
mark E04 관측: LCD 현재온도 ?에서 ?로 변화, 목표 ?
```

### 종료

```text
mark 복구 완료: 목표 ?, 외출 해제, 모드 실내난방
```
```text
quit
```

---

## 회차 2: E06·E07·E08·E09

창 A `nctr_capture 02`, 창 B `nctr_collect 02`

```text
mark E00 회차 시작: LCD 현재 ?, 목표 ?, 모드 ?, 온수 설정 ?
```

### E06 전원 OFF→ON (조절기 전원 버튼)

보일러 전원선은 건드리지 않습니다. OFF 중 TCP 연결 유지 여부를 확인합니다.

```text
mark E06-1 직전: 조절기 전원 버튼 OFF 예정
```
→ 전원 OFF
```text
mark E06-1 직후: LCD OFF 표시 ?
```
```text
status
```
→ 1~2분 대기, 연결 끊김 여부 확인
```text
mark E06-2 직전: 전원 ON 예정, OFF 중 연결 상태 ?
```
→ 전원 ON
```text
mark E06-2 직후: LCD ON, 목표 ? 모드 ? (OFF 전 값 유지 여부)
```

### E07 난방 모드 전환 (실내 ↔ 온돌)

조절기에 온돌 모드가 있을 때만 합니다. 모드와 목표를 따로 바꿉니다.

```text
mark E07-1 직전: 실내난방에서 온돌 모드로 전환 예정, 목표 ?
```
→ 모드 전환
```text
mark E07-1 직후: LCD 온돌 모드, 난방수 목표 ?
```
```text
mark E07-2 직전: 온돌 난방수 목표 ?에서 ?로 1단계 변경 예정
```
→ 목표 1단계 변경
```text
mark E07-2 직후: LCD 난방수 목표 ?
```
```text
mark E07-3 직전: 난방수 목표 원래 값 ?으로 복귀 후 실내난방 전환 예정
```
→ 목표 복귀, 실내난방 전환
```text
mark E07-3 직후: LCD 실내난방, 목표 ? (전환 전 값 유지 여부)
```

### E08 온수 온도·온수 전용

```text
mark E08-1 직전: 온수 설정 ?에서 ?로 1단계 변경 예정
```
→ 온수 1단계 변경
```text
mark E08-1 직후: LCD 온수 설정 ?
```
```text
mark E08-2 직전: 온수 설정 원래 값 ?으로 복귀 예정
```
→ 원래 값으로 복귀
```text
mark E08-2 직후: LCD 온수 설정 ?
```

온수 전용 모드가 있을 때:
```text
mark E08-3 직전: 온수 전용 모드 진입 예정
```
```text
mark E08-3 직후: LCD 온수 전용 표시 ?
```
```text
mark E08-4 직전: 온수 전용 해제, 실내난방 복귀 예정
```
```text
mark E08-4 직후: LCD 실내난방, 목표 ?
```

### E09 목욕·HOT·절약

조절기에 있는 기능만 합니다. 기능 이름을 바꿔 같은 형식으로 입력합니다.

```text
mark E09-1 직전: 목욕 기능 진입 예정
```
```text
mark E09-1 직후: LCD 목욕 표시 ?
```
```text
mark E09-2 직전: 목욕 기능 해제 예정
```
```text
mark E09-2 직후: LCD 해제, 모드 ? 목표 ?
```

기능이 없을 때:
```text
mark E09 해당 없음: 조절기에 목욕/HOT/절약 버튼 없음
```

### 종료

```text
mark 복구 완료: 전원 ON, 실내난방, 목표 ?, 온수 ?
```
```text
quit
```

---

## 회차 3: E10 예약

창 A `nctr_capture 03`, 창 B `nctr_collect 03`

기존 예약을 사진으로 백업하고, 몇 분 뒤 시작하는 짧은 예약 하나만 만듭니다.

```text
mark E10-0 기존 예약 백업: 사진 촬영 완료, 기존 예약 ?
```
```text
mark E10-1 직전: 예약 생성 예정, 시작 ?시?분, 종료 ?시?분
```
→ 예약 생성
```text
mark E10-1 직후: LCD 예약 표시 ?
```
예약 시작 시각:
```text
mark E10-2 예약 시작 관측: LCD 운전 상태 ?
```
예약 종료 시각:
```text
mark E10-3 예약 종료 관측: LCD 운전 상태 ?
```
```text
mark E10-4 직전: 시험 예약 삭제, 기존 예약 복구 예정
```
→ 시험 예약 삭제, 기존 예약 복구
```text
mark E10-4 직후: LCD 예약 ? (사진과 일치 여부)
```
```text
quit
```

---

## 회차 4: E11 등록 코드

현재 수집 서버는 `--pair-code`가 없으면 `0002` 요청을 기록만 하고 응답하지 않습니다([server.py](../src/nctr_lab/server.py)의 `0x0002` 처리). 이 회차는 시험 코드를 지정합니다. 고정 시험 응답이며 보안 페어링이 아닙니다. 제조사 앱에서 이 코드로 등록하지 않습니다.

창 A `nctr_capture 04`, 창 B `nctr_collect 04 --pair-code 4321`

```text
mark E11-1 직전: 전원 OFF 후 온수 5초로 등록 코드 표시 시도 예정, 서버 코드 4321
```
→ 전원 OFF 상태에서 온수 버튼 5초
```text
mark E11-1 직후: LCD 표시 코드 ? (4321 일치 여부)
```
```text
mark E11-2 직전: 등록 화면 종료, 전원 ON 예정
```
```text
mark E11-2 직후: LCD 정상 화면, 목표 ?
```
```text
quit
```

---

## 회차 5: E13 서버 재시작, E14 재접속 관찰

창 A `nctr_capture 05a`, 창 B `nctr_collect 05a`

```text
mark E13-1 직전: 수집 서버만 종료 예정, 조절기 조작 없음
```
```text
quit
```

1~2분 뒤 창 B에서 `nctr_collect 05b`로 서버를 다시 시작합니다. 창 A의 `05a` 캡처는 끄지 않고 유지합니다. 05b를 `quit`한 뒤 창 A를 `Ctrl+C`로 끝내고 `nctr_finish 05a`, `nctr_finish 05b` 순서로 정리합니다(05b는 캡처 파일 없음으로 표시됩니다).

```text
mark E13-2 서버 재시작: 이전 회차 05a, 재접속 대기 시작
```
재접속·`ready=true` 확인 후:
```text
mark E13-3 재접속 확인: 서버 재시작 후 ?분 ?초, LCD 목표 ?
```
재접속 시 조절기 DNS 질의를 Wireshark(`dns.qry.name contains "krb"`)와 NAS 창 0의 dnsmasq 로그에서 확인한 뒤:
```text
mark E14 관찰: 재접속 시 DNS 로그 igs4 질의 ?, igis 질의 ?
```
```text
quit
```

E14의 자체 프로비저닝·인터넷 차단 시험은 현재 도구 범위 밖이므로 이번에는 하지 않습니다.

---

## 회차 6: E05 원격 온도 비교

회차 1 분석 후 진행합니다. 같은 날 진행하지 않는 것을 권합니다. 범위는 조절기가 실제로 허용하는 값으로 채웁니다.

`field.env`의 `NCTR_TEMP_MIN`·`NCTR_TEMP_MAX`를 채운 뒤 창 A `nctr_capture 06`, 창 B `nctr_collect_temp 06`

```text
status
```
```text
mark E05-1 직전: 원격 목표 26에서 27 전송 예정, LCD 목표 26, 모드 실내난방
```
```text
temp 27
```
확인 화면에서 `SEND`를 입력하면 `command_id`가 출력됩니다. LCD 확인 후 `<command_id>`와 값을 실제 값으로 바꿔 입력합니다.
```text
lcd <command_id> 27
```
```text
mark E05-1 직후: LCD 목표 ?, 운전 상태 ?
```
→ 다음 상태 보고까지 대기
```text
mark E05-2 직전: 원격 목표 27에서 26 복귀 전송 예정
```
```text
temp 26
```
```text
lcd <command_id> 26
```
```text
mark E05-2 직후: LCD 목표 ?
```
```text
quit
```

ACK만으로 적용 성공으로 판정하지 않습니다. LCD·후속 보고가 다르면 불일치로 기록하고 물리 조절기로 복구합니다.

---

## 해당 없음

- **E12 오류:** 오류를 일부러 만들지 않습니다. 시험 중 오류가 저절로 나타났을 때만 입력합니다.
  ```text
  mark E12 관측: LCD 오류 코드 ? 표시, 발생 직전 조작 ?
  ```
- **E15 HA 노출:** Home Assistant 연동이 구현되지 않아 실물 시험 대상이 아닙니다.
