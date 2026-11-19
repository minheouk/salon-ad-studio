# src/pipelines/service (서비스 개발)

사장님이 실제로 쓰는 **화면과 그 뒤의 처리**를 둡니다.

담당: 김도영 ([@diokim17](https://github.com/diokim17))
리뷰: PM ([@minheouk](https://github.com/minheouk))

| 폴더 | 하는 일 |
|---|---|
| `ui/` | 화면 (입력, 결과 확인, 다운로드) |
| `backend/` | 사용자 요청 처리, 서빙 API 호출, 결과 저장 |

## 다른 역할과 주고받는 것

- **서빙에게서 받는 것**: API 주소와 요청·응답 형식
- 모델이 완성되기 전에는 서빙의 목업 API(`serving/mock/`)로 화면을 먼저 만들 수 있습니다.
- 주고받는 데이터 형식은 `src/common/`에 정의합니다.

## 알아둘 점

- 화면 개발은 GPU가 필요 없어서 내 PC에서 작업해도 됩니다.
- 팀 VM에서 띄울 때는 내 포트(`8506`), 팀 데모는 `8500` 포트를 씁니다.
- 테스트는 `tests/service/`에 둡니다.
