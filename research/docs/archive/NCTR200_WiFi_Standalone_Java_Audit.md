# 귀뚜라미 IoT 매니저 Standalone Java 전체 정적 분석

- 분석 입력: 사용자 제공 `standalone.zip`
- 소스 구성: ZIP 항목 224개(디렉터리 포함), Java 소스 201개, 약 1.5 MB 비압축
- APK 패키지: `kr.openfloor.kituramiplatform.standalone`
- 앱 빌드 정보: `BuildConfig.java:8-9` — 버전 코드 99, 버전 이름 2.8.9
- 분석 방법: 전 Java 파일에 대한 정적 검색, 제어·초기 설정·등록·메시지·예약·상태 파일 교차 검토. 실제 단말/제조사 서버에 접속하거나 패킷을 수집한 검증은 수행하지 않음.
- 신뢰도 표시: **확인** = APK 코드에서 확인 / **추정** = 하드웨어·서버 구현에 대한 추론 / **미확인** = 이 소스만으로 판단 불가능

## 요약

**확인**: Android 앱은 HTTPS REST로 제어를 발행하고, MQTT를 통해 상태를 수신하며, 기기 초기 설정에서는 `krb_set` AP에 접속해 UDP `192.168.200.1:20000`로 WizFi250 Air Command 텍스트를 전송한다. NCTR-200 WIFI가 해당 기기 설정 분기에 명시된다.

**확인**: `POST https://igis.krb.co.kr/modelUri`가 모델별 서버 목적지 `ip`, `uri`를 내려줄 수 있는 API 인터페이스다. `Setup.java`는 응답을 읽어 선택 제품의 `productIP`, `productURI`로 보관한다. `Setup_Step06.java`는 `AT+SCON=S,TCN,<productIP>,20000,,1` 및 `AT+ABCD=S,TCN,<productURI>,20000,,1`로 이를 설정한다. `AT+ABCD`의 의미는 미확인이다.

**확인**: 실제 제어 `messageId` 및 `messageBody`의 인코더/디코더가 존재한다. `0x0101` 전원, `0x0102` 실내난방, `0x0106` 외출, `0x0107` 24시간 예약, `0x0108` 반복 예약, `0x010A` 실내상태, `0x0109` 오류, `0x0115` 온수 온도 등이 확인된다.

**미확인**: NCTR-200 실물에 WizFi250 SoC가 실제 탑재되는지, 원격 서버의 20000/TCP 프로토콜 프레이밍·식별·인증·하트비트가 무엇인지, 클라우드 없이 장치가 안정적으로 작동하는지.

## 통신 구조와 데이터 경로

1. 초기 설정: `Setup.java:203-204`, `Setup_Step04.java:88-106`, `Setup_Step06.java:116-135`, `UDPCommForRC.java:10-39`.
2. 주소 조회: `DeviceAPIService.java:61-62`, `KituramiAPIController.java:91-96,260-262`, `modelUri.java:8-30`, `Setup.java:216-234`.
3. 평상시 제어: `Control.java:1709-1763`, `Converter.java:29-36`, `ControlModel.java:7-27`, `DeviceAPIService.java:31-32`, `KituramiAPIController.java:276-277`.
4. 상태 수신: `MQTTController.java:42-73,190-202,302-308`, `MQTTReceiverService.java:96-127`, `Control.java:553-578`, `MessageParser.java:42-194`.
5. 앱의 MQTT 사용은 subscribe 및 callback 수신 중심이다. 별도 MQTT publish 호출은 조사한 201개 Java 코드에서 발견되지 않았다.
6. 앱 내부에서 raw device TCP client/server (`java.net.Socket`, `ServerSocket` 등)를 구현한 부분은 발견되지 않았으며, 초기 설정용 `DatagramSocket`이 보이는 유일한 직접 소켓 경로다. 장치-클라우드 TCP는 앱 코드의 바깥 영역이다.

### 정확한 초기 설정 메시지 (`Setup_Step06.java:129`)

```text
WizFi250AirCmd:AT+WSET=0,<SSID>\r
AT+WSEC=0,,<WiFiPassword>\r
AT+WNET=1\r
AT+SCON=S,TCN,<productIP>,20000,,1\r
AT+ABCD=S,TCN,<productURI>,20000,,1\r
AT+FSOCK=6,15\r
AT+MPROF=S\r
```

이후 UDP 응답이 `ERROR`가 아니면 `WizFi250AirCmd:AT+MRESET\r` 송신. 소스에는 2개의 목적지 IP/URI가 있으며, `AT+ABCD`는 공개 표준 WIZnet 명령 목록에서 확인되지 않아 의미는 미확정. 데이터 모드에서 UART 데이터를 TCP로 투명 전달하는 구조일 가능성은 있으나 실기기 동작 검증이 필요하다.

**주의**: `Setup.java:116-118`에서 `SendingInfo()`가 스레드를 시작한 이후 `SendingProInfo()`에서 `protype`을 기록하는 순서라 경쟁 상태가 가능하다. `UDPCommForRC.ReceiveMsg()`는 timeout이 없고 송신 DatagramPacket을 수신용으로 재사용하며 실제 수신 길이가 아닌 전체 버퍼를 해석한다. 앱 로직에서 성공 판정은 장치의 설정 저장 확인과 구별할 것.

## 제어·상태 패킷 사양(앱 단계)

통상 `messageBody`는 hex 문자열 길이 12(6 byte). 단 예약은 길이가 더 길다. 앱 MQTT 수신 문자열은 앞 4글자를 ID, 12번째 글자부터 나머지를 body로 해석한다. 4~11번째 글자의 의미는 미확인. 이 구조가 원시 TCP와 동일하다고 가정할 수 없다.

| ID | 상수 / 디코더 | 본문 특징 | 자료 |
|---|---|---|---|
| `0101` | 전원/MessagePower | `roomID + 00000000 + powerState`; on=01 off=02 | `MessagePower.java:11-28` |
| `0102` | 실내난방/MessageInner | room, owner, current, unit, set, fractional | `MessageInner.java:15-36` |
| `0103` | 난방/MessageHeating | 현온도(byte2), 설정온도(byte4) | `MessageHeating.java:12-30` |
| `0104` | 온돌/MessageWater | 현온도(byte2), 설정온도(byte4) | `MessageWater.java:12-30` |
| `0105` | 목욕/MessageBath | 설정온도(byte4) | `MessageBath.java` |
| `0106` | 외출/MessageOut | roomID + ownerState + 00000000 | `MessageOut.java` |
| `0107` | 24h 예약/MessageRsvHour | 1바이트 room mask + 48바이트(30분 슬롯별 8실 mask), 총 49 bytes | `MessageRsvHour.java:36-100` |
| `0108` | 반복 예약/MessageRsvRepeat | roomID + 000000 + stopHour + activeMinute | `MessageRsvRepeat.java` |
| `0109` | 오류/MessageError | 마지막 2바이트 errorFirst/errorSecond | `MessageError.java` |
| `010A` | 실내상태/MessageMQTTInner | room(byte0), tempMode(byte3), temp(byte4), fractional(byte5) | `MessageMQTTInner.java` |
| `010B` | 방 상태/MessageRoom | byte5 = changedCount | `MessageRoom.java` |
| `010C` | 주간 예약/MessageRsvWeekly | 49바이트 패킷 처리 함수 존재, 사용 시 검증 필요 | `MessageRsvWeekly.java` |
| `010F` | 예약 유형 요청 | 짧은 요청 `01+roomID` | `MessageRsvRepeatRequest.java` |
| `0110` | 난방 온도/MessageMQTTHeating | mode(byte0), temperature(byte4), temperatureB(byte5) | `MessageMQTTHeating.java` |
| `0111` | 최소 제어 | 6바이트 전부 0 | `MessageMinimumControl.java` |
| `0113` | 절약/MessageSaving | room/owner/current/unit/set/fractional | `MessageSaving.java` |
| `0114` | 온수 전용/MessageWarmWater | unit(byte3), set(byte4) | `MessageWarmWater.java` |
| `0115` | 온수 온도/MessageWarmWaterSetting | unit(byte3), set(byte4) | `MessageWarmWaterSetting.java` |
| `0122` | HOT/MessageHot | unit(byte3), set(byte4) | `MessageHot.java` |
| `0006` | 타입 변경/MessageTypeChange | body byte1=determinedMode | `MessageTypeChange.java` |
| `01DD` | 연결 끊김 UI 이벤트 | `ProcessMessage()`에서 연결 끊김 화면 선택 | `Control.java:563-567` |
| `01FA` | 강제 새로고침 UI 이벤트 | `ProcessMessage()`에서 GetDeviceInfo 요청 | `Control.java:563-567` |

`0x010A`의 온도는 현재 온도이며 목표 온도는 `0x0102`의 설정 필드를 쓴다. 코드 UI는 목표온도 > 현재온도 조건만으로 불꽃 아이콘을 바꾸므로 실제 연소 중 상태로 신뢰하면 안 된다 (`Control.java:774-803`).

### 보일러 분류 및 ID

- 기기 등록 성공 시 `nodeId`는 **18문자**인지 검사한다 (`Register.java:243-259`).
- `nodeId` 첫 2문자는 제품 계열. `MessageDefine.TYPE_NEW_200_BOILER = "36"` (`MessageDefine.java:89-106`). `KituramiDefine.java`의 `TYPE_NEW_BOILER_200WF=905969664=0x36000000`, 세부 코드 0x36010001/0x36020001/0x36040001도 확인된다. 따라서 36계열은 200WF 제품 계열과 강하게 연관되지만, NCTR-200 WiFi의 실기기 식별값과 동일한지 확인 필요.
- `MessageDefine.GetTankMode(topic)`는 `topic.substring(2,4)`를 반환한다. 01/02/04 값에 따라 탱크리스·저장형 구성 분기가 달라진다 (`MessageDefine.java:314-316`, `HeatingAndWarmWater.java:198-221`).
- 방 ID는 01,02,04,08,10,20,40,80 비트마스크 (`MessageDefine.java:59-66`).
- 전원 01=ON 02=OFF (`MessageDefine.java:49-50`).
- 온도는 16진 두 자리(`%02X`)로 인코딩됨. 온도 범위는 클라이언트에서 모델별로 제한되며, 하드웨어 허용 범위와 같다고 단정하지 말 것.

### 예약·스케줄

- `MessageRsvHour.java`는 48개의 30분 슬롯을 각 방 8비트 마스크로 합성하여 총 49바이트를 만든다. 장치 전송 방식의 예약 기능 단서.
- `MessageRsvRepeat.java`는 휴지시간과 가동시간을 각각 1바이트씩 표현.
- `ConfigAPIService.java:11-17`과 `ScheduleModel.java`는 `configure/getSchedule`, `registerSchedule`, `updateDeviceSchedule` 등 서버 저장/관리 API를 제공한다. **클라우드 서버 기반 예약**은 인터넷 독립성 요구를 충족하지 못할 수 있다.
- `MessageRsvWeekly.java`의 루프는 i=2부터 i+=2로 증가하여 선언된 48슬롯 중 일부만 채우는 형태이므로 원본을 그대로 이식하지 말고 샘플과 대조 검증해야 한다.
- `MessageRsvHour`와 `MessageRsvWeekly`의 `Byte.parseByte(hex,16)`은 0x80 이상을 표현하는 16진 두 자리에서 예외가 발생할 수 있다. Java decompile 결과 그대로를 정확한 프로토콜로 취급하지 않을 것.

## API 아키텍처

- 운영 REST: `KituramiAPIController.java:91-96`에서 `https://igis.krb.co.kr/api/`.
- 모델 주소: `KituramiAPIController.java:260-262`는 `getURL2()`를 사용하며 `DeviceAPIService.java:61-62`의 `POST modelUri` → `https://igis.krb.co.kr/modelUri`.
- 제조사 상태 수신: `MQTTController.java:42-73`은 TLS 없는 MQTT TCP 1883을 사용하고, 저장된 `nodeId` 토픽을 구독한다.
- MQTT 계정의 공유 비밀번호가 APK에 하드코딩되어 있다. 보고서에는 비밀번호를 복제하지 않음. 본인 소유 기기의 관찰 목적으로도 제조사 운영 MQTT에 임의 접속하거나 타인의 토픽을 구독하는 시험은 피할 것.
- `device/getServerAddress`는 인터페이스/컨트롤러 메서드만 있고 201개 Java 파일에서 `RequestServerInfo()`의 사용처가 발견되지 않음. 응답 모델 `ServerInfo.java`에는 `country`, `serverAddress` 2개 필드가 있으나 **실제 쓰는 API라고 보기는 어렵다**.
- `device/getWifiInfo` 역시 인터페이스/모델 정의만 관찰됨. 앱에서 직접 호출하는 코드 없음.
- `HttpLoggingInterceptor.Level.BODY`, Logcat에 인증 헤더·Wi-Fi 암호 등이 출력될 수 있는 로깅이 있다 (`KituramiAPIController.java:121-132`, `Setup_Step06.java:129-130`). 네트워크 덤프/스크린샷 공유시 민감값 마스킹.

## 펌웨어/OTA 기능 흔적

201개 `.java` 전체에서 기기 펌웨어 이미지 다운로드, Flash 쓰기, MCU 디버그, 앱이 수행하는 장치 OTA 제어 로직을 발견하지 못함. `BuildConfig`의 2.8.9는 스마트폰 앱 버전이지 보일러 펌웨어 버전이 아님. 다만 WizFi250 공식 프로그래머 가이드에는 모듈 차원의 `AT+FOTA` 명령이 존재하므로 제조사 장치 내부에 OTA 기능이 **절대 없다**고 판단해서는 안 됨. APK가 그 기능을 노출하지 않는다는 결론까지만 가능.

## 정적 분석의 한계

- 이 압축은 앱 자체 패키지 201개 Java이며, 의존 라이브러리·리소스·네이티브 바이너리·제조사 서버 소스·NCTR-200 기기 펌웨어를 포함한다고 확인되지 않음. 실제 컴파일 가능한 전체 원본 프로젝트가 아님.
- JADX 디컴파일 경고와 타입 추론 스택 트레이스 주석이 여러 파일에 포함되어 있으므로 프로그램 로직 세부는 Smali/DEX 비교가 필요할 수 있다.
- `0x0101` 등 메시지는 **앱 REST 계층 형식**. 장치 TCP의 프레임이라고 확인되지 않음.
- `AT+ABCD`는 공개 WIZnet 프로그래머 가이드에서 확인되지 않는 명령이다. 커스텀 명령으로 추정할 수 있지만 다른 기능일 수도 있음.
- Air Command 기본 포트 50001이 공식 문서 사례에 있지만 제조사 앱은 `192.168.200.1:20000` 사용. 기기별 설정 변경 가능성 때문에 소스의 값 그대로를 실기기에 적용 전 검증 필요.

## 다음 검증 순서 (위험 최소화)

1. **구입 전**: 가능하면 현재 출시 앱인 IoT 멀티 매니저 `integrated` APK와 이 Standalone v2.8.9를 비교해 동일 초기 설정 코드인지 확인.
2. **구입 후 분해 없이**: 별도 테스트용 AP/라우터를 이용해 기기가 어느 IP의 어떤 TCP/UDP 포트로 접속하는지 관찰. 본인 소유 기기 트래픽만 수집.
3. **PCB 분석**: 별도 예비 조절기에서 Wi-Fi 모듈(실제 WizFi250 여부), 제어 MCU, UART 연결 확인. 보일러 가동 중 전기적 접속·신호 주입 금지.
4. **읽기 전용 로컬 리스너**: 테스트용 기기에 로컬 목적지를 설정할 수 있는지 확인한 다음 컨테이너 TCP 20000 포트에서 **수신만** 하며 최초 접속 데이터, 주기, 장치 식별자, 프레임 구분 방식을 파악.
5. **프로토콜 대조**: 수신 데이터를 앱 `Message*` 파서와 비교해 4자리 ID/헤더/바디와 일치하는지 확인. 인증·하트비트·재전송 여부를 명확히 하기 전 제어 패킷 전송 금지.
6. **HA**: 검증 후 MQTT Climate / custom integration; Matter는 필요 시 서버 측 Matter Bridge 또는 별도 ESP32 펌웨어로 확장.

## 부록 A. 확인된 API 인터페이스 목록

일부 메서드는 *정의만 존재하고 호출되지 않을 수 있음*. 서버 실제 작동을 검증한 목록이 아니라 Retrofit 어노테이션 목록이다.

| 소스 위치 | REST 상대경로 |
|---|---|
| `CSAPIService.java:14` | `IOT/postinfo.asp?makecode=K` |
| `CSAPIService.java:18` | `IOT/acceptSet.asp` |
| `ConfigAPIService.java:11` | `configure/getSchedule` |
| `ConfigAPIService.java:14` | `configure/registerSchedule` |
| `ConfigAPIService.java:17` | `configure/updateDeviceSchedule` |
| `DeviceAPIService.java:28` | `device/deviceAllControl` |
| `DeviceAPIService.java:31` | `device/deviceControl` |
| `DeviceAPIService.java:34` | `device/getErrorInfo` |
| `DeviceAPIService.java:37` | `gas/yearMonthDayGas` |
| `DeviceAPIService.java:40` | `gas/yearMonthGas` |
| `DeviceAPIService.java:43` | `gas/yearGas` |
| `DeviceAPIService.java:46` | `device/getDeviceInfo` |
| `DeviceAPIService.java:49` | `device/isAliveNormal` |
| `DeviceAPIService.java:52` | `device/getDeviceLastFirstAction` |
| `DeviceAPIService.java:55` | `device/getDeviceLastNextAction` |
| `DeviceAPIService.java:58` | `device/getDeviceModeInfo` |
| `DeviceAPIService.java:61` | `modelUri` |
| `DeviceAPIService.java:64` | `device/getServerAddress` |
| `DeviceAPIService.java:67` | `device/getStudyData` |
| `DeviceAPIService.java:70` | `device/getWifiInfo` |
| `DeviceAPIService.java:73` | `device/registerDevice` |
| `DeviceAPIService.java:76` | `device/sendQrCode` |
| `DeviceAPIService.java:79` | `device/unregisterDevices` |
| `DeviceAPIService.java:82` | `device/updateAddress` |
| `DeviceAPIService.java:85` | `device/updateDeviceAlias` |
| `DeviceAPIService.java:88` | `device/updateQrCode` |
| `DeviceAPIService.java:91` | `device/updateSlaveAlias` |
| `MemberAPIService.java:20` | `member/checkDupId` |
| `MemberAPIService.java:23` | `member/getAddressList` |
| `MemberAPIService.java:26` | `member/getAddressNormalList` |
| `MemberAPIService.java:29` | `member/checkAuthKey` |
| `MemberAPIService.java:32` | `member/getMemberDeviceList` |
| `MemberAPIService.java:35` | `member/getMemberDeviceList` |
| `MemberAPIService.java:38` | `member/getMemberId` |
| `MemberAPIService.java:41` | `member/getMemberNormalDeviceList` |
| `MemberAPIService.java:44` | `member/getMemberDeviceCount` |
| `MemberAPIService.java:47` | `member/getSurveyAnswer` |
| `MemberAPIService.java:50` | `member/getTerms` |
| `MemberAPIService.java:53` | `member/login` |
| `MemberAPIService.java:56` | `member/registerMember` |
| `MemberAPIService.java:59` | `member/requestMemberInfo` |
| `MemberAPIService.java:62` | `member/initializePassword` |
| `MemberAPIService.java:65` | `member/setSurveyAnswer` |
| `MemberAPIService.java:68` | `member/unregisterMember` |
| `MemberAPIService.java:71` | `member/updateMember` |
| `MemberAPIService.java:74` | `member/updatePassword` |
| `NoticeAPIService.java:9` | `notice/getNoticeAndroid` |

## 부록 B. 참고 공식 문서

- [WizFi250 Programmer's Guide](https://docs.wiznet.io/Product/Modules/Wi-Fi-Module/WizFi250/wizfi250_programmersguide) — `AT+SCON`, `AT+MPROF`, `AT+FSOCK`, `AT+FOTA`, Air Command.
