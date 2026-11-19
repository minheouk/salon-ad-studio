# src/pipelines/model (모델 개발)

광고 이미지와 광고 문구를 **만들어 내는** 코드를 둡니다.

담당: 이재웅 ([@mitguchin](https://github.com/mitguchin)), 서정철 ([@SeoJungCheol](https://github.com/SeoJungCheol))

| 폴더 | 하는 일 |
|---|---|
| `generation/` | 광고 이미지 생성 |
| `copywriting/` | 광고 문구 생성 |
| `finetune/` | 파인튜닝 (LoRA 등) |
| `evaluation/` | 생성 품질 평가 |
| `composition/` | 생성한 이미지 위에 문구·가게 이름·로고를 합성하고, 게시 비율에 맞게 배치 |

## 다른 역할과 주고받는 것

- **서빙에게 넘기는 것**: 생성 함수와 그 함수가 쓰는 모델·설정
- 주고받는 데이터 형식은 `src/common/`에 정의합니다.

## 알아둘 점

- 이미지 생성 모델은 한글 글자를 제대로 그리지 못하는 경우가 많습니다. 그래서 문구는 `composition/`에서 이미지 위에 합성합니다.
- 실험은 `exp/model-<주제>` 브랜치에서 하고, 결과는 `experiments/`에 기록합니다.
- 테스트는 `tests/model/`에 둡니다.
