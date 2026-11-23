# NCTR-200 프로토콜 연구 자료

이 폴더는 Home Assistant 통합을 만들기 전에 NCTR-200 조절기의 통신을 분석한 자료입니다. 2026-10-15부터 11-22까지 별도 저장소(Boiler_IoT, 비공개)에서 진행했고, 통합이 나온 뒤 코드와 문서만 이곳으로 옮겼습니다.

통합을 쓰는 데에는 이 폴더가 필요 없습니다. 프로토콜을 확인하거나 조절기를 새로 시험할 때 참고합니다.

## 지금 상태

| 항목 | 상태 |
|---|---|
| 프로토콜 | 프레임 형식, ACK 규칙, 식별·상태 보고, 원격 명령 10종을 실물로 확인. [메시지 참조](docs/NCTR200_Message_Reference.md) |
| 로컬 접속 | DNS를 바꾸지 않는 자체 프로비저닝 성공(11-22). [자체 프로비저닝](docs/NCTR200_Self_Provisioning.md) |
| Home Assistant | 이 저장소의 `custom_components/nctr200`. 수집 서버에서 확인한 동작을 옮겼고, 통합 자체의 실물 시험은 아직 |
| 남은 확인 | 제조사 형식의 `0003` 응답으로 반복이 멈추는지(통합 0.3.0에서 확인), 전원 재투입·인터넷 차단·공유기 재시작 뒤 재접속 등. [시험 기록](docs/NCTR200_Test_Log.md) 6절 |

## 읽는 순서

1. [시험 기록](docs/NCTR200_Test_Log.md): 10월~11-22의 회차별 시험, 캡처, 주제별 결론
2. [메시지 참조](docs/NCTR200_Message_Reference.md): 확인된 메시지·명령 형식과 근거 수준
3. [자체 프로비저닝](docs/NCTR200_Self_Provisioning.md): 조절기를 로컬 서버로 붙인 방법과 시도 기록
4. [현장 절차](docs/NCTR200_Ubuntu_Field_Test_Runbook.md): 수집 서버로 실물 시험을 하는 순서. 13절은 제조사 서버 장시간 캡처
5. [현장 체크리스트](docs/NCTR200_Field_Marker_Checklist.md): 회차별 조작과 marker
6. [실험 계획](docs/NCTR200_Local_Control_Experiment_Plan.md), [사전 검증](docs/NCTR200_Preflight_Validation_2026-10-25.md): 수집 도구를 만들 때의 계획과 모의 시험 결과
7. [10월 인수인계](docs/NCTR200_WiFi_Local_Gateway_Handoff_2026-10-19.md): 이전 도구(v4) 시기의 실기 기록

`docs/archive/`에는 10월의 분석 문서와 이전 판 계획·안내를 그대로 둡니다. 당시 판단이 지금과 다른 곳이 있고, 옮긴 뒤 위치가 달라져 깨진 링크도 있습니다.

## 구성

```text
research/
├─ src/
│  ├─ nctr_lab_cli.py     수집 서버·분석·프로비저닝 CLI
│  ├─ nctr_lab/           프로토콜, 기록, 분석, 프로비저닝
│  └─ archive/            10월의 단일 파일 서버 v1~v4와 AP 조회 스크립트
├─ tests/test_lab.py      모의·loopback 시험(실물·외부 서버에 접속하지 않음)
├─ tools/
│  ├─ nctr_field.sh       NAS 현장 시험 보조 함수
│  └─ nctr_windows_field.ps1  Windows 핫스팟 구성 보조 스크립트
└─ docs/
```

Python 3.11 이상, 표준 라이브러리만 씁니다.

```sh
cd research
python -m unittest discover -s tests
python src/nctr_lab_cli.py --help
```

## 옮기지 않은 것

아래 원본 자료는 집 네트워크 정보와 조절기 고유 정보가 들어 있거나 제조사 저작물이라 공개 저장소에 넣지 않았습니다. 문서에 파일 이름은 남아 있지만 링크는 없습니다.

- 패킷 캡처(`captures/*.pcapng`), 휴대폰 캡처
- Android 앱 로그(`ADB/`)
- 수집 서버 회차 기록(`experiments/`), 프로비저닝 기록, 배포 묶음(`dist/`)
- 제조사 사용설명서 PDF

## 옮기면서 바꾼 것

- 조절기 MAC은 `XX:XX:XX:XX:XX:XX`, 노드 ID는 `31 01 02 00 xx xx xx xx`로 가렸습니다. 앞 4바이트는 장치 유형(`0000` 본문)과 같습니다.
- 집 LAN 주소는 대역만 `192.168.1.x`로 바꿨습니다. 끝자리와 역할(NAS, Windows 이더넷 등)은 그대로라 문서 사이의 대응은 유지됩니다. 핫스팟(`192.168.137.x`)과 조절기 설정 AP(`192.168.200.x`)는 기기 기본값이라 그대로 뒀습니다.
- NAS 호스트 이름은 `nas`로 바꿨습니다.
- 작업 폴더 이름 `Boiler_IoT`는 `research`로 바꿨습니다. 10월 인수인계와 `docs/archive/`의 원문에는 옛 이름이 남아 있습니다.
