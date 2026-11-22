# NCTR-200 WiFi: 미등록 + AP 설정 초기화 상태에서 시작하는 DNS/로컬 TCP 시험

작성: 2026-10-17. 실험실용 계획. 제조사 공식 서버나 조절기 펌웨어를 변경하지 않는다.

## 0. 출발 상태 및 핵심 교정

- 조절기는 제조사 계정에서 **등록 해제된 상태**.
- 조절기에는 Wi-Fi/AP 설정 초기화가 적용돼 **일반 가정용 Wi-Fi/Windows 핫스팟에 연결되지 않은 상태**.
- 따라서 먼저 **제조사 앱에서 Wi-Fi 연결까지만 프로비저닝**해야 한다. 기기 등록 코드를 발급받거나 앱 계정에 등록하면 안 된다.
- `nodeId`는 제조사 앱의 4자리 등록 코드와 별개인 **8바이트 장치 식별자**다. 2026-10-17 최초 프로비저닝 캡처에서 계정 등록 전 `0001` 요청이 이 식별자 전체를 전송했다. 따라서 **실험 시작 시 `nodeId` 입력은 필요 없다**. 임시 수신 서버가 `0001`에서 자동 학습하면 된다.
- 단, 2026-10-15 캡처에서는 `0000`으로 시작했고, 그 요청은 장치 유형처럼 보이는 **4바이트만** 포함한다. 이 경로는 로컬 서버가 최초 요청만으로 전체 식별자를 알 수 없다. 과거 사용자 본인의 PCAP `0001` 본문으로만 보조 식별자를 공급한다. 새 계정 등록은 필요 없다.

## 1. 목표 / 엄밀한 검증 기준

1. 미등록 상태에서 제조사 공식 앱으로 **Wi-Fi만 설정**한다.
2. DNS는 `igs4.krb.co.kr`만 홈서버 LAN IP로 응답한다. `igis.krb.co.kr` HTTP 서버 조회는 제조사 그대로 유지한다.
3. 조절기가 사용자 로컬 TCP/20000에 접속하는지 직접 관측한다.
4. 수신 전용으로 장치가 전송하는 `0001`과 ID를 확인한다(또는 `0000` 시작 여부 확인).
5. 수신 서버를 응답 모드로 다시 실행해 `0001` → `01FF` 상태 보고 성공을 검증한다.
6. 사람의 감시하에서 허용된 기존 온도 제어를 한 번 시험한다. **앱 계정 미등록 상태에서 LCD와 상태 보고에 변경이 반영**되면, 클라우드 계정 등록이 물리 TCP 제어에 필수라는 가설은 반증된다. 실패는 미해독 서버 프로토콜 때문일 수 있어 등록 필수라는 증거가 아니다.

## 2. 네트워크 선택

### A. 기존 Windows 모바일 핫스팟 활용

- 조절기 → Windows 모바일 핫스팟(기존 게이트웨이 `192.168.137.1`) → 상위 Ethernet → Ubuntu 홈서버(LAN 주소는 실제 확인 필요).
- Ubuntu DNS/53과 TCP/20000 준비. Windows Ethernet DNS에 Ubuntu DNS를 우선 설정한 뒤 모바일 핫스팟을 다시 시작해본다. **Windows ICS가 그 DNS로 질의를 전달한다는 보장은 없다.**
- Wireshark에서 조절기가 **실제로 받은** `igs4.krb.co.kr` A 응답과 이후 TCP SYN 목적지를 확인한다. `nslookup`으로 Ubuntu DNS 자체를 시험하는 것만으로는 충분하지 않다.
- **주의:** Ubuntu 서버에서 관측하는 TCP 소스 IP는 Windows ICS NAT로 인해 Windows PC의 Ethernet IP일 수 있다. 예전 핫스팟 내 조절기 `192.168.137.181`이 서버 측 소스 IP로 보인다고 가정하지 않는다.

### B. Windows ICS DNS 변경이 통하지 않을 경우 — 테스트용 일반 Wi-Fi AP (더 재현성 높음)

- ipTIME 2.4GHz 일반 LAN SSID(게스트/클라이언트 격리 기능은 사용하지 않음)에서 조절기를 프로비저닝한다.
- 공유기의 DHCP가 조절기에게 **Ubuntu DNS IP를 배포하는지** 확인한다. 공유기 자신의 상위 DNS 설정을 바꿔도 DHCP 제공 DNS가 그대로일 수 있다.
- 공유기 전체 DHCP DNS를 바꿔야 한다면 다른 가정용 기기에도 영향이 있을 수 있으므로 이전 값을 저장하고 실험 직후 원복한다. 가능하다면 분리된 실험용 LAN/DHCP를 사용한다.
- Ubuntu에서 질의 로그와 로컬 TCP 수신 로그로 서버 전환을 확인한다. 완전한 외부 패킷 캡처는 이 구성에서 필요하지 않다.

## 3. Ubuntu 준비 — DNS와 수신 서버를 먼저 켜둠

Ubuntu LAN IP 확인:

```bash
ip -4 -o addr show dev enp3s0
sudo ss -lntup | grep -E ':53\\b|:20000\\b' || true
```

예시 IP는 사용자의 실제 LAN IP가 **아니다**. 실측 주소로 설정:

```bash
export NCTR_LAN_IP='192.168.1.50'  # 실제 주소로 교체
```

임시 DNS: 인터넷에 공개하지 않고 LAN에서만 접근하게 한다.

```bash
sudo apt install dnsmasq-base
sudo dnsmasq --keep-in-foreground --bind-interfaces \
  --listen-address="$NCTR_LAN_IP" --no-resolv \
  --server=1.1.1.1 \
  --host-record=igs4.krb.co.kr,"$NCTR_LAN_IP" \
  --log-queries
```

`igis.krb.co.kr`을 변경하지 말 것. 이 DNS 프로세스를 첫 번째 터미널에서 실행한 상태로 유지한다.

두 번째 터미널에서 기존 스크립트가 아닌 **수정본** `nctr_local_autoid_server.py`를 수신 전용으로 실행한다. **`nodeId` 인수가 없다.**

```bash
python3 -u nctr_local_autoid_server.py \
  --bind 0.0.0.0 --port 20000 --mode observe \
  | tee nctr_observe.log
```

TCP 포트를 Traefik에 연결하거나 인터넷으로 포트포워딩하지 않는다. Ubuntu 방화벽이 있다면 시험 LAN에서 오는 DNS(UDP/TCP 53) 및 TCP 20000만 허용한다.

Windows PC에서 Ubuntu DNS 자체 검증(`192.168.1.50`은 예시):

```powershell
nslookup igs4.krb.co.kr 192.168.1.50
nslookup igis.krb.co.kr 192.168.1.50
```

첫 번째는 로컬 IP, 두 번째는 제조사 원본 IP. **조절기 자신이 이 응답을 받았는지는 별도로 캡처로 검증**해야 한다.

## 4. 제조사 공식 앱의 Wi-Fi 프로비저닝만 수행

1. Android 휴대전화에서 제조사 앱을 열고 `NCTR-200 WIFI` 모델을 선택한다. 모델 정보 조회에는 휴대전화의 정상 인터넷 접속이 필요할 수 있다.
2. 조절기가 AP 설정 초기화된 상태라면 정식 안내대로 `krb_set` AP를 열어, 휴대전화를 연결한다.
3. 앱에서 대상 Wi-Fi로 Windows 테스트 핫스팟(또는 위 B의 일반 AP)을 선택하고 암호를 입력한다.
4. 설정 전송 완료 후 휴대전화가 AP를 벗어나더라도 즉시 장치 등록을 진행하지 않는다. **등록 코드 입력/추가 정보 등록 화면에서 중단**한다.
5. Wireshark 또는 Ubuntu DNS 로그에서 조절기 DHCP → `igis.krb.co.kr` HTTP → `igs4.krb.co.kr` DNS → TCP/20000 순서를 확인한다.
6. 이번 실험에서 원본 앱이 `AT+SCON`, `AT+ABCD`에 제조사 주소를 쓰는 것은 **의도한 동작**이다. DNS만 별도로 바꾸므로 설정 명령을 변조할 필요는 없다.

## 5. 첫 결과의 분기

| 결과 | 해석 / 조치 |
|---|---|
| `CONNECTED` 와 `RX 0001 ... node=...` | **ID 자동 확보 성공**. 계정 등록하지 않은 채 다음 단계 진행 |
| `CONNECTED` 와 `RX 0000 ... prefix=...` 만 | 전체 ID는 이 프레임에 없음. 기존 10월 17일 PCAP의 `0001` 본문(마지막 8바이트)을 로컬에서 추출해 `--bootstrap-node-id`에 **선택적으로** 전달. 제조사 등록 필요 없음 |
| DNS 응답이 제조사 IP | Windows ICS/라우터 DHCP DNS가 override되지 않음. TCP 서버가 아닌 DNS 전달 문제부터 해결 |
| DNS 응답이 로컬 IP인데 TCP는 제조사 IP | 서버 우선순위/HTTP 선택 로직이 DNS override를 무시하는지 추가 조사 |
| TCP 연결 자체 없음 | 서버 방화벽, 라우팅, IP/53 설정, 공유기 격리 확인 |

`0000` 보조 ID 확보용으로는 Wireshark에서 이전 `packet_bolier.pcapng`의 `frame.number == 6110`을 열어 TCP Payload 앞 6바이트를 제외한 마지막 8바이트를 확인한다. 이 장치 고유 ID는 계정 등록 해제와 AP 초기화로 변경된다는 증거가 없다.

## 6. 핸드셰이크 시험

수신 전용 서버를 중단한 다음 다음과 같이 실행한다. `--time-tail`의 `0123`은 과거 PCAP에서 관측한 **미해독 2바이트**로, 유효한 최신 값을 의미하지 않는다. 이로 인해 동작이 실패할 수 있으므로 원인을 등록 여부로 단정하지 않는다.

```bash
python3 -u nctr_local_autoid_server.py \
  --bind 0.0.0.0 --port 20000 \
  --mode handshake --time-tail 0123 \
  | tee nctr_handshake.log
```

조절기의 Wi-Fi를 재연결해 새 TCP 세션을 만들고, 서버 로그에서 `RX 0001`, `STATE REPORT`, `TX 01 FF`를 관찰한다. **계정 미등록 상태를 유지한다.**

`0000`으로만 시작하는 경우:

```bash
python3 -u nctr_local_autoid_server.py \
  --bind 0.0.0.0 --port 20000 \
  --mode handshake --time-tail 0123 \
  --bootstrap-node-id 'XXXXXXXXXXXXYYYY' \
  | tee nctr_handshake.log
```

`XXXXXXXXXXXXYYYY`는 **자리 표시자**이므로 실제 8바이트 장치 ID로 교체해야 한다. 노드 ID는 비밀 공유하지 않는다.

## 7. 미등록 제어 시험은 마지막에

핸드셰이크 성공, 1회 이상 `01FF` 수신, 조절기 물리 상태 확인 후에만:

```bash
python3 -u nctr_local_autoid_server.py \
  --bind 0.0.0.0 --port 20000 \
  --mode handshake --time-tail 0123 \
  --enable-temperature \
  | tee nctr_control.log
```

TCP가 다시 연결되어 상태 보고한 다음 단일 명령 `temp 22` 등 실제 기기에서 안전하게 확인할 목표온도(18~26°C 범위)를 입력하고 `SEND`로 최종 확인한다. 실험 중 실제 보일러 점화·설정온도를 사람이 확인하고, 필요하면 직접 조절기로 즉시 원래 설정을 복구한다.

성공 판정은 **응용 ACK + 실제 LCD 목표온도 변경 + 후속 상태 보고**를 대조하는 것이다. `0001` 응답의 미해독 필드 등 때문에 실패하면 등록 의존성은 판단할 수 없다.

## 8. 정상 상태 복구

1. TCP 테스트 서버 종료. DNS 테스트 서버 종료 및 Windows Ethernet/공유기 DHCP DNS 원복.
2. 조절기 Wi-Fi를 재연결하여 `igs4.krb.co.kr`이 원래 제조사 IP로 반환되는지 확인.
3. 필요할 경우 정식 앱에서 등록 코드를 다시 발급받아 계정에 정상 등록. 현재 테스트가 끝날 때까지 **사전 등록은 하지 말 것**.
4. 실제 조절기 전원·목표온도·외출 모드를 확인하고 실험 전 값으로 복구.

## 검증 범위와 제약

- 이 스크립트는 로컬 모의 소켓 테스트만 통과했으며 **실제 조절기에서의 핸드셰이크 성공은 아직 미검증**.
- 로컬 TCP 서버의 0001 응답은 PCAP에서 확인된 날짜·시간 형식을 부분 모방하지만, 길이 필드가 실제 데이터보다 1바이트 큰 예외와 시간 본문 마지막 2바이트의 의미는 미해결.
- 장치의 4자리 등록 코드 `0002`는 기본적으로 응답하지 않음. 이 상태에서 정상 동작하는지 확인 후 필요하면 별도 구현.
- 이 실험은 `igis.krb.co.kr` HTTP 요청을 계속 제조사에 의존하므로 완전 오프라인 운용의 검증이 아님.
- 보일러 실제 제어 관련 임의 명령, 인증 우회 또는 안전 로직 변경은 수행하지 않는다. 제조사 서버 접근을 사칭하지 않고 오직 **자신이 소유한 조절기와 테스트 네트워크**에서 실행한다.
