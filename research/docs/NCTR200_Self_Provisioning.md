# NCTR-200 자체 프로비저닝 시험

작성일: 2026-11-22(KST)  
도구: [src/nctr_lab/provision.py](../src/nctr_lab/provision.py), `python src/nctr_lab_cli.py provision`  
관련: [메시지 참조](NCTR200_Message_Reference.md), [현장 절차](NCTR200_Ubuntu_Field_Test_Runbook.md)

목표는 DNS를 바꾸지 않고 조절기가 처음부터 로컬 서버로 접속하게 하는 것입니다. 이것이 되면 AdGuard·공유기 DNS 설정과 상관없이 동작하고, Home Assistant 통합(HACS)이 조절기를 직접 받는 구성이 가능해집니다.

## 1. 근거

제조사 앱은 조절기 설정 AP(`krb_set`, `192.168.200.1`)에 UDP/20000으로 데이터그램 하나를 보냅니다(APK `Setup_Step06.java`). **줄 구분은 CR(`\r`)입니다.** 11-22 03:48:57 휴대폰 캡처(PCAPdroid)에서 앱의 실제 바이트를 확인했습니다. 출발 포트는 임의(37411), 170바이트, 한 번만 보냄.

```text
WizFi250AirCmd:AT+WSET=0,<SSID>
AT+WSEC=0,,<암호>
AT+WNET=1
AT+SCON=S,TCN,121.141.70.84,20000,,1
AT+ABCD=S,TCN,http://igs4.krb.co.kr,20000,,1
AT+FSOCK=6,15
AT+MPROF=S
```

APK 코드는 응답이 `ERROR`가 아니면 `AT+MRESET`을 보내지만, 실제 캡처에서는 **조절기 응답이 없었고 앱도 `AT+MRESET`을 보내지 않았습니다.** 조절기는 데이터그램을 받으면 스스로 재시작하고, 설정 AP는 1초 안팎에 사라집니다.

### 11-22 시도 기록

| 시각 | 구분 | 암호 길이 | 결과 |
|---|---|---|---|
| 03:29 | CR | 11자 | `krb_set` 즉시 사라짐(받아들이고 재시작). 그러나 ipTIME에 붙지 못함 → 암호 오타로 판단 |
| 03:37, 03:40 | LF | 10자 | `krb_set` 그대로(무시됨) |
| 03:48 | 앱(CR) | 10자 | 정상 |

ADB 로그에서 명령이 줄마다 나뉘어 보였던 것은 앱 로그 출력기가 바꿔 찍은 결과였고, 이를 LF로 잘못 판단했었습니다. 조회 명령(`AT+MINFO`)은 CR·LF·CRLF, 출발 포트 임의·20000 모두 응답이 없습니다.

도구는 CR을 기본으로 하고(`--line-ending`으로 변경 가능), 응답이 없으면 앱처럼 `AT+MRESET`을 보내지 않으며(`--reset auto|always|never`), 암호를 두 번 입력받아 일치하는지 확인합니다.

- 조절기는 `SCON`의 `121.141.70.84`가 아니라 `igs4`를 DNS로 풀어 `121.141.70.70`에 접속했습니다. 그래서 지금은 `ABCD`(도메인) 쪽 주소를 쓰는 것으로 보입니다.
- 부팅할 때마다 받는 `igis` HTTP 응답 `{"uri":"D"}`도 "Domain" 쪽을 고르라는 뜻으로 추정됩니다.
- `AT+ABCD`는 공개 WizFi250 명령이 아닌 제조사 확장입니다. IP를 넣어도 되는지는 확인되지 않았습니다.

### 성공(11-22 04:16, `field-20261122-30`)

조절기는 프로비저닝 뒤 `igis` 조회(`{"uri":"D"}`)를 거쳐 **`ABCD`의 호스트를 항상 DNS로 조회**합니다. IP를 넣어도 이름으로 묻고, `SCON`의 IP로 넘어가지 않습니다. 또 **호스트의 앞 3글자 뒤에 `4`를 끼워** 묻습니다.

| 넣은 `ABCD` | 조절기가 물은 이름 | 결과 |
|---|---|---|
| `http://192.168.1.8` | `1924.168.1.8` | 풀리지 않아 TCP 시도 없음 |
| `http://192.168.1.8.nip.io` | `1924.168.1.8.nip.io` | `No such name` |
| `http://nas.192.168.1.8.nip.io` | `nas4.192.168.1.8.nip.io` | `192.168.1.8` → **접속·식별·`ready` 성공** |

제조사 값 `igs4.krb.co.kr`도 `igs` + `4` 형태라 같은 규칙으로 보입니다. 와일드카드 DNS(nip.io)에 버려도 되는 3글자 첫 마디를 붙이면 로컬 DNS를 바꾸지 않고 서버 IP로 풀립니다. 도구는 `--abcd`를 주지 않으면 이 형식(`http://nas.<server-ip>.nip.io`)을 쓰고, 조절기가 물을 이름을 미리 보여 줍니다.

남는 의존성은 인터넷입니다. 부팅 때 `igis` 조회와 nip.io 이름 풀이가 필요합니다. 직접 가진 도메인을 쓰려면 첫 마디를 3글자로 정하고(예: `nas.boiler.example`) `nas4.boiler.example`에 A 레코드를 둡니다.

참고: 첫 성공 전에 Windows 핫스팟의 DHCP가 Offer를 보내지 않는 상태가 있었습니다(조절기 `Discover`에 ARP 확인만 하고 멈춤). PC 무선 어댑터가 `krb_set`에 붙었다 떨어진 뒤 생겼고, 핫스팟을 껐다 켜서 풀렸습니다.

## 2. 도구

```powershell
& "$env:LOCALAPPDATA\Python\bin\python.exe" src/nctr_lab_cli.py provision --ssid <Wi-Fi 이름> --server-ip 192.168.1.8 --bind 192.168.200.2
```

PowerShell의 `python`은 Windows 스토어 별칭으로 연결돼 "Python"만 출력하고 끝날 수 있습니다. 위처럼 설치 경로를 지정하거나, 설정 → 앱 → 고급 앱 설정 → 앱 실행 별칭에서 `python.exe`를 끕니다.

| 옵션 | 기본값·뜻 |
|---|---|
| `--server-ip` | 수집 서버(NAS) IP. `SCON`과 `ABCD`의 기본값 |
| `--scon IP` | `AT+SCON`만 따로 지정 |
| `--abcd 값` | `AT+ABCD`만 따로 지정. 기본 `http://nas.<server-ip>.nip.io`, 앱 값은 `http://igs4.krb.co.kr` |
| `--password-file` | Wi-Fi 암호 파일. 없으면 입력 프롬프트(화면에 안 보임). 명령줄 인자로는 받지 않음 |
| `--bind` | `krb_set`에 붙은 PC 어댑터 IP(보통 `192.168.200.2`) |
| `--query AT+MINFO` | 설정 대신 조회 명령 하나만 보냄 |
| `--line-ending` | 명령 구분 문자. 기본 `cr`(앱과 같음) |
| `--reset` | `auto`(기본, 응답이 있고 `ERROR`가 아닐 때만 `AT+MRESET`), `always`, `never` |

진행 순서는 다음과 같습니다.
1. 암호를 두 번 입력받아 같은지 확인합니다. 보낼 내용과 암호 글자 수를 보여 줍니다(암호는 `*`).
2. `SEND`를 입력하면 한 번만 보냅니다.
3. 5초 동안 응답을 받아 보여 줍니다. 응답에 `ERROR`가 있으면 재시작하지 않고 멈춥니다.
4. 응답이 없으면(정상) 재시작 명령은 보내지 않습니다. 조절기가 받아들였다면 1초 안팎에 `krb_set`이 사라지고 PC의 Wi-Fi 연결이 끊깁니다. 응답이 있을 때만 `RESET` 확인 후 `AT+MRESET`을 보냅니다.
5. 기록은 `provision-records/`에 JSON으로 남깁니다. 암호는 길이만 남고 내용은 남지 않습니다. 이 폴더는 `.gitignore` 대상입니다.

## 3. 시험 준비

1. **NAS**: 최신 판 수집기를 띄워 둡니다(`nctr_collect 회차`). 조절기가 ipTIME에 직접 붙으면 IP를 미리 모르므로 `field.env`의 `NCTR_PEER_IP`를 `192.168.1.0/24`처럼 LAN 범위로 둡니다. 핫스팟 NAT 주소(`192.168.1.2`)도 이 범위에 들어갑니다.
2. **DNS 우회 끄기**: AdGuard의 `igs4` 규칙을 끕니다. 확인 명령에서 `121.141.70.70`이 나와야 합니다.
   ```powershell
   Clear-DnsClientCache; Resolve-DnsName igs4.krb.co.kr -Type A -DnsOnly
   ```
   이 상태에서 조절기가 NAS에 붙으면 DNS와 상관없이 붙은 것입니다.
3. **Wireshark**: 핫스팟 인터페이스(`Wi-Fi 4`)에서 캡처를 시작합니다. 조절기가 `igis` 조회와 `igs4` DNS 질의를 계속하는지 봅니다.
4. **조절기를 설정 AP 모드로**: 전원 OFF 상태에서 외출 버튼 5초(AP 초기화). 조절기가 `krb_set`을 엽니다. APK 리소스상 기본 암호는 `krb12345`입니다.
5. **PC를 `krb_set`에 연결**: 핫스팟이 아닌 무선 어댑터(`Wi-Fi`)로 붙습니다. IP가 `192.168.200.2`인지 확인합니다. PC 무선 칩 하나를 같이 쓰므로 **이 동안 Windows 핫스팟은 꺼집니다.** 그래서 `--ssid`에 핫스팟을 넣으면 재시작 직후 조절기가 붙을 곳이 없습니다. 첫 시험은 ipTIME 2.4GHz SSID를 권합니다.

## 4. 시험

| 단계 | 설정 | 확인할 것 |
|---|---|---|
| P0 | `--query AT+MINFO` | 조회에 응답하는지(10월에는 응답 없음). 응답이 없어도 다음 단계는 진행 |
| P1 | 기본값: `SCON`·`ABCD` 모두 NAS IP, CR 구분, SSID는 ipTIME 2.4GHz | 재시작 뒤 ipTIME 접속 목록에 조절기 MAC(`XX:XX:XX:XX:XX:XX`)이 보이는지, NAS 수집기에 `connected`·`0001`이 오는지 |
| P2 | P1이 안 되면 `--abcd http://igs4.krb.co.kr`(앱 값), `SCON`만 NAS IP | 조절기가 어느 쪽을 쓰는지 가르기 |
| P3 | P1 성공 후 조절기 전원 OFF·ON, 핫스팟 재시작 | 설정이 유지되고 다시 NAS로 붙는지 |
| P4 | P1 성공 후 인터넷 차단(공유기 차단 등) | `igis` 조회 실패 시에도 NAS로 붙는지. 완전 오프라인 가능 여부 |
| P5 | SSID를 Windows 핫스팟으로 바꿔 P1 반복(PC가 `krb_set`에서 빠진 뒤 핫스팟을 다시 켬) | Wireshark로 `igis` 조회·`igs4` DNS 질의가 남는지 관찰 |

각 단계의 시각을 기록해 주시면 NAS 기록·Wireshark와 맞춰 분석합니다.

## 5. 복구

- 언제든 제조사 앱으로 Wi-Fi를 다시 설정하면 `SCON`·`ABCD`가 제조사 값으로 돌아갑니다.
- 자체 프로비저닝 중 조절기가 어디에도 붙지 않으면, AP 초기화 후 도구를 다시 실행하거나 앱으로 설정합니다.
- 실패한 설정이 조절기 기본 동작(버튼·다이얼·보일러 제어)에는 영향을 주지 않습니다. Wi-Fi 통신 설정만 바뀝니다.
