# src/pipelines/serving (모델 서빙)

모델을 **서비스에서 호출할 수 있는 API로** 띄우고, 빠르고 가볍게 돌아가게 만드는 코드를 둡니다.

담당: 김연주 ([@aiengineeung-del](https://github.com/aiengineeung-del)), 김시현 ([@dypower1559-cell](https://github.com/dypower1559-cell))
팀 VM 관리도 서빙 담당 중 1명이 맡습니다.

| 폴더 | 하는 일 |
|---|---|
| `api/` | 추론 API 서버 |
| `optimization/` | 추론 속도·메모리 최적화 |
| `mock/` | 진짜 모델 대신 바로 응답하는 목업 API. 모델이 완성되기 전에도 서비스 개발이 화면을 만들 수 있게 함 |

## 다른 역할과 주고받는 것

- **모델 개발에게서 받는 것**: 생성 함수와 모델·설정
- **서비스 개발에게 넘기는 것**: API 주소와 요청·응답 형식
- 주고받는 데이터 형식은 `src/common/`에 정의합니다.

## 알아둘 점

- 팀 VM의 GPU(L4, 24GB) 한 장을 모두가 함께 씁니다. 서버를 띄울 때와 내릴 때 디스코드 `#gpu` 채널에 알려 주세요.
- 개인 테스트는 내 포트(`800N`), 팀 데모는 `8000` 포트를 씁니다. 자세한 내용은 [VM 접속 가이드](../../../docs/vm_member_guide.md)에 있습니다.
- 테스트는 `tests/serving/`에 둡니다.
