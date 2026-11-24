# 네일샷 AI API 명세 v1.7

손 사진(서버에 준비된 8장 중 하나) + 사용자가 올린 아무 네일 사진 → 그 디자인을 입힌 손 사진.

- 기본 주소: `http://localhost:8000` (settings.yaml `server.port`)
- 형식 정의(코드): `api_schema.py` — 실제 응답은 이 파일의 형식을 따라요
- 서버를 켜면 **`/docs`** 에서 같은 명세를 보고 바로 시험해 볼 수 있어요 (settings.yaml `server.api_docs`)
- 형식을 바꾸면 `api_schema.py` 의 `API_VERSION` 을 올리고 이 문서도 같이 고치세요

## 목차
| 메서드 | 주소 | 하는 일 |
|---|---|---|
| GET | `/api/health` | 서버 상태와 설정 |
| GET | `/api/usage` | 오늘/전체 사용량과 비용 |
| GET | `/api/hands` | 고를 수 있는 손 사진 목록 |
| GET | `/hands/{name}` | 손 사진 파일 |
| POST | `/api/analyze` | 디자인 사진 미리 인식 |
| POST | `/api/generate` | 네일아트 입히기 (한 번에 결과) |
| POST | `/api/generate_stream` | 네일아트 입히기 (중간 그림 받기, SSE) |
| POST | `/api/banner_stream` | **네일 사진 1~5장 → 손 자동 선택·입히기·배너 계획서·배너 (SSE)** |

## 공통 규칙
- 문자 인코딩 UTF-8. JSON 응답의 금액은 달러(`usd`, 소수), 원화(`krw`)는 대략값
- 생성/인식 요청은 `multipart/form-data` (파일 업로드 때문)
- 이미지는 **data URL** 문자열로 줘요: `data:image/jpeg;base64,....` (`<img src>` 에 그대로 넣으면 보여요)
- 비용 숨김(settings.yaml `server.hide_cost: true`)이면 모든 비용 항목이 `null`, `/api/usage` 는 404
- 같은 디자인 사진은 서버가 기억해서(최근 200장) 인식을 다시 하지 않아요 → 인식 비용 0

## 오류 형식 (모든 주소 공통)
HTTP 상태 코드 + 아래 JSON. 화면에는 `detail` 을 그대로 보여 주고, 프로그램 분기는 `code` 로 하세요.
```json
{ "detail": "디자인 사진은 10MB 이하로 올려 주세요", "code": "FILE_TOO_LARGE" }
```
입력 형식 오류(INVALID_REQUEST)만 `errors` 가 붙어요:
```json
{ "detail": "요청 형식이 올바르지 않아요: quality", "code": "INVALID_REQUEST",
  "errors": [ { "field": "quality", "message": "Input should be 'fast', 'normal' or 'high'" } ] }
```

| HTTP | code | 언제 | 사용자 조치 |
|---|---|---|---|
| 422 | `INVALID_REQUEST` | 필수 항목 누락, 허용되지 않은 값 | 입력 수정 |
| 400 | `HAND_NOT_FOUND` | `hand` 가 `/api/hands` 목록에 없음 | 손 다시 선택 |
| 400 | `INVALID_IMAGE` | JPG/PNG/WebP 가 아니거나 깨진 파일 | 다른 사진 |
| 413 | `FILE_TOO_LARGE` | 10MB 초과 | 작은 사진 |
| 422 | `NO_NAILS_DETECTED` | 업로드 사진에서 네일 디자인을 못 찾음 (이미지 생성 안 함) | 손톱이 잘 보이는 사진 |
| 400 | `CONTENT_POLICY` | OpenAI 안전 필터 | 다른 사진 |
| 400 | `UPSTREAM_BAD_REQUEST` | OpenAI 가 요청 거절 | 관리자 확인 |
| 400 | `MASK_MISSING` | 손 사진의 손톱 마스크 없음/비어 있음 | 관리자: mask_editor.html |
| 429 | `RATE_LIMITED` | OpenAI 사용량/요금 한도 | 잠시 후 다시 |
| 500 | `UPSTREAM_AUTH` | OpenAI API 키 문제 | 관리자: .env |
| 504 | `UPSTREAM_TIMEOUT` | OpenAI 연결 실패/시간 초과 | 다시 시도 |
| 404 | `NOT_FOUND` | 없는 주소/파일 | - |
| 500 | `INTERNAL_ERROR` | 그 밖의 서버 오류 | 관리자: 서버 창 로그 |

---

## GET `/api/health`
입력 없음.
```json
{
  "ok": true,
  "api_version": "1.0",
  "model": "gpt-image-2",          // 이미지 생성 모델. --fake 모드면 "fake"
  "complexity": null,              // "low"|"medium"|"high"|null(모델 기본값)
  "analysis_model": "gpt-6-luna",  // 디자인 인식 모델. 인식 끔이면 null
  "fake": false,
  "show_cost": true,               // false 면 비용 항목이 전부 null
  "qualities": ["fast", "normal", "high"]
}
```

## GET `/api/usage`
입력 없음. 비용 숨김이면 404 `NOT_FOUND`.
```json
{
  "today": { "count": 3, "usd": 0.3114, "krw": 436 },   // count = 만든 결과 수 (인식만 한 건 제외)
  "total": { "count": 12, "usd": 1.0721, "krw": 1501 },  // usd = 인식 + 생성 합계
  "usd_krw": 1400.0,
  "model": "gpt-image-2",
  "complexity": null
}
```

## GET `/api/hands`
입력 없음. 배열.
```json
[ { "id": "hand1_blue_front.jpg", "label": "hand1_blue_front", "url": "/hands/hand1_blue_front.jpg" } ]
```
- `id` → 생성 요청의 `hand` 에 그대로 보내요
- `url` → GET 하면 손 사진 이미지

## GET `/hands/{name}`
손 사진 파일 (`image/jpeg` 또는 `image/png`). 없으면 404 `NOT_FOUND`.

## GET `/api/scenes`  *(v1.5)*
`nailhand/<분위기(키워드,…)>/<사진>` 의 장면 사진 목록. `/api/hands` 와 같은 형식.
```json
[ { "id": "레드와인(빨간색,섹시)/블랙체리 네일, 와인과 촛불빛-2.png", "label": "레드와인 · 블랙체리 네일, 와인과 촛불빛",
    "url": "/nailhand/%EB%A0%88..." } ]
```
## GET `/nailhand/{폴더}/{파일}`  *(v1.5)*
장면 사진 파일. nailhand 폴더 밖 경로나 없는 파일이면 404 `NOT_FOUND`.

---

## POST `/api/analyze` — 디자인 사진 미리 인식
사진을 고르는 순간 호출해 두면, 생성할 때 인식을 건너뛰어 빨라져요. (웹 화면이 이렇게 해요)

**입력** (`multipart/form-data`)
| 이름 | 형식 | 필수 | 설명 |
|---|---|---|---|
| `design` | 파일 | O | JPG/PNG/WebP, 10MB 이하. 인스타 캡처, 손 사진 등 아무 사진 |

**출력 200**
```json
{
  "enabled": true,                 // false = 서버에서 인식 단계를 꺼 둠 (design 은 null)
  "design": {                      // 인식이 실패해도 null (생성은 사진 그대로 진행)
    "summary_ko": "투명 핑크 베이스에 은색 글리터",
    "has_nails": true,
    "confidence": 0.9,             // 0~1
    "accent_nails": "",            // 포인트 손톱 설명. 모두 같으면 ""
    "model": "gpt-6-luna",
    "cached_result": false,        // true = 같은 사진 이전 결과 재사용 (비용 0)
    "usd": 0.00022,                // 이 인식 비용. 비용 숨김이면 null
    "suggested_hand": "hand6_white_side.jpg",  // (v1.1) 인식 모델이 디자인에 맞다고 고른 손. 못 고르면 null
    "hand_reason_ko": "흰 배경이 핑크를 살려요"
  }
}
```
**오류**: 400 `INVALID_IMAGE`, 413 `FILE_TOO_LARGE`, 422 `NO_NAILS_DETECTED` / `INVALID_REQUEST`

---

## POST `/api/generate` — 네일아트 입히기 (한 번에 결과)

**입력** (`multipart/form-data`)
| 이름 | 형식 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `hand` | 문자열 | O | | `/api/hands` 의 `id` |
| `design` | 파일 | O | | JPG/PNG/WebP, 10MB 이하 |
| `quality` | `fast` \| `normal` \| `high` | | `normal` | 빠름·저렴 / 기본 / 고화질·느림 (실제 OpenAI 품질은 settings.yaml `quality.*`) |
| `variation` | 정수 0~9999 | | `0` | 0 = 기본, 1·2·3… = '다른 느낌으로 다시' |
| `desc` | 문자열 ≤300자 | | `""` | 추가 요청 (영어 권장). 인식 결과보다 우선 |
| `response_format` | `json` \| `image` | | `json` | 아래 참고 |
| `size` | 512\|768\|1024 | | | **예전 호환용** → fast/normal/high. `quality` 를 쓰세요 |
| `seed` | 정수 | | | **예전 호환용** → `variation` |

**출력 200 (`response_format=json`, 기본)** — `GenerateResult`
```json
{
  "image": "data:image/jpeg;base64,/9j/4AAQ...",   // 결과 이미지 (server.result_format: jpeg|png)
  "mime": "image/jpeg",
  "width": 1122, "height": 1402,                    // 원본 손 사진과 같은 크기
  "ms": 31250,                                      // OpenAI 생성 시간
  "hand": "hand1_blue_front.jpg",
  "quality": "high",
  "variation": 0,
  "design": { ...DesignInfo... },                   // 위 /api/analyze 의 design 과 같은 형식, 인식 끔이면 null
  "nails": { "total": 5, "ok": 5, "bg": 0, "unchanged": 0 },   // 손톱별 검사
  "align": { "used": true, "ok": true, "dx": -2.0, "dy": -4.0, "scale": 1.002, "cc": 0.98 },
  "usage": {                                        // 비용 숨김이면 null
    "model": "gpt-image-2", "quality": "high", "complexity": "",
    "text_in": 383, "image_in": 2117, "text_out": 0, "image_out": 4300, "total_tokens": 6800,
    "image_usd": 0.1479,                            // 이미지 생성 비용
    "analysis_usd": 0.0002,                         // 이번 결과에 포함된 인식 비용 (같은 사진 두 번째부터 0)
    "usd": 0.1481, "krw": 207.3,                    // 이번 결과 비용 합계
    "priced": true,                                 // prices.json 에 모델 가격이 있는지 (false 면 usd null)
    "today": { "count": 4, "usd": 0.4595, "krw": 643 }
  }
}
```
- `nails.bg`(배경색으로 칠해짐) 또는 `nails.unchanged`(디자인 안 들어감)가 1 이상이면 실패한 손톱이 있는 거예요 → `variation` 을 올려 다시 요청 권장

**출력 200 (`response_format=image`)** — 이미지 파일 그대로
- 본문: `image/jpeg` 또는 `image/png`
- 헤더 `X-Result`: 위 JSON 에서 `image` 만 뺀 것 (ASCII JSON, 한글은 `\uXXXX`)
- 헤더 `X-Elapsed-Ms`: 생성 시간

**오류**: 400 / 413 / 422 / 429 / 500 / 504 (위 오류 표)

---

## POST `/api/generate_stream` — 중간 그림 받기 (Server-Sent Events)
입력은 `/api/generate` 와 같아요 (`response_format` 없음).

- 입력 확인 오류(HAND_NOT_FOUND, INVALID_IMAGE, FILE_TOO_LARGE, INVALID_REQUEST)는 **일반 JSON 오류**로 바로 응답
- 그 뒤는 `Content-Type: text/event-stream`. 각 이벤트는 `event: 이름` + `data: JSON` + 빈 줄
- `: keep-alive` 같은 `:` 로 시작하는 줄은 무시하세요 (연결 유지용, 15초마다)
- 브라우저의 `EventSource` 는 POST 를 못 보내서 `fetch` 로 받아 직접 읽어요 (`web/index.html` 의 `readEvents` 참고)

**이벤트 순서**: `stage` → (`design`) → `stage` → `partial` 0~3개 → `final`  
실패하면 그 시점에 `error` 하나로 끝나요.

| event | data 형식 | 설명 |
|---|---|---|
| `stage` | `{ "step": "analyzing"\|"generating", "text": "디자인 확인 중..." }` | 진행 단계 (화면 안내 문구) |
| `design` | `DesignInfo` | 인식 결과 (인식 끔/실패면 이 이벤트 없음) |
| `partial` | `{ "index": 0, "image": "data:image/jpeg;base64,..." }` | 그리는 중간 그림 (절반 크기 미리보기). 개수는 `request.partial_images` |
| `final` | `GenerateResult` | `/api/generate` json 응답과 같은 형식 |
| `error` | `{ "status": 422, "code": "NO_NAILS_DETECTED", "message": "..." }` | 실패 (오류 표의 code) |

예시
```
event: stage
data: {"step": "analyzing", "text": "디자인 확인 중..."}

event: design
data: {"summary_ko": "핑크 글리터", "has_nails": true, "confidence": 0.9, "accent_nails": "", "model": "gpt-6-luna", "cached_result": true, "usd": 0.00022}

event: stage
data: {"step": "generating", "text": "그리는 중..."}

event: partial
data: {"index": 0, "image": "data:image/jpeg;base64,..."}

event: final
data: {"image": "data:image/jpeg;base64,...", "mime": "image/jpeg", "width": 1122, ...}
```

---

## POST `/api/banner_stream` — 네일 사진 1~5장 → 배너 1~5장 (SSE)  *(v1.1)*
웹 첫 화면(`/`)이 쓰는 주소예요. 예전 1장 화면은 `/single`.

사진마다 아래 4단계를 거쳐요 (사진끼리는 동시에 진행, 이미지 생성은 `request.concurrency` 개씩):
1. **인식 모델**(`design_analysis.model`, 예: gpt-6-luna)이 디자인을 글로 정리 + `hands` 폴더의 손 사진 중 어울리는 손 선택 (한 번의 호출)
2. **이미지 모델**(`openai.model`, 예: gpt-image-2)이 그 손에 네일아트 입히기 (`/api/generate` 와 같은 방식)
3. **인식 모델**(`banner.plan_model`, 비우면 1번과 같음)이 완성 사진을 보고 배경·배너 디자인 계획서 작성
4. **이미지 모델**이 계획서대로 그 손 사진으로 배너 생성 (`banner.size`, 기본 1536x1024)

**입력** (`multipart/form-data`)
| 이름 | 형식 | 필수 | 설명 |
|---|---|---|---|
| `designs` | 파일 (여러 번) | O | 1~5장 (`banner.max_designs`). 각 JPG/PNG/WebP, 10MB 이하 |
| `quality` | `fast`\|`normal`\|`high` | | 입히기·배너 둘 다에 적용. 기본 normal |
| `brand` | 문자열 ≤30자 | | 배너에 넣을 상호명. 비우면 `banner.brand` |
| `desc` | 문자열 ≤300자 | | 추가 요청 (네일·배너 모두 반영) |
| `analysis_effort` | `""`\|none\|minimal\|low\|medium\|high\|xhigh\|max | | ① 인식·손 고르기의 Reasoning effort. 빈 값 = `design_analysis.reasoning_effort` *(v1.2)* |
| `plan_effort` | 위와 같음 | | ③ 배너 계획서의 Reasoning effort. 빈 값 = `banner.plan_reasoning_effort` *(v1.2)* |
| `nail_length` | auto\|keep\|medium\|long\|extra_long | | 손톱 길이. auto = 인식 결과대로, keep = 손 사진 원래 길이 *(v1.2)* |
| `pose` | `""`\|auto\|keep | | ④ 배너의 손 자세. auto = 계획서가 디자인에 맞는 자세를 정해 바꿈, keep = 원래 자세. 빈 값 = `banner.pose` *(v1.3)* |
| `nail_shape` | auto\|almond\|oval\|round\|square\|squoval\|coffin\|stiletto | | 손톱 모양. auto = 인식 결과대로 *(v1.2)* |
| `apply_mode` | `""`\|mask\|free\|direct\|scene | | ② 적용 방식. mask = 손톱 마스크 · free = 마스크 없이 통째로 · direct = ② 없이 배너에서 한 번에 *(v1.4)* · scene = nailhand 장면 사진에 입히고 음료 색 · 배경도 바꾸기 *(v1.5)*. 빈 값 = `banner.apply_mode` |
| `split_sets` | `""`\|on\|off | | on = 사진 1장에 네일 팁 세트(5개씩)가 여러 개 → ⓪ 인식 모델이 세트를 찾아 세트마다 1장씩 만들어요 (최대 `sheet.max_sets`). 맨 먼저 `sets` 이벤트가 와요 *(v1.6)* |
| `scene_banner` | `""`\|on\|off | | scene 방식에서 ③④ 배너까지 만들지. off = 바꾼 장면 사진이 완성품(`item_banner` 로 옴, `item_nail`·`item_plan` 없음). 빈 값 = `banner.scene_banner` *(v1.5)* |

입력 확인 오류(장수, INVALID_IMAGE, FILE_TOO_LARGE)는 일반 JSON 오류로 바로 응답 (`detail` 에 "N번째 사진:" 이 붙어요).

**이벤트** — `done` 을 뺀 모든 이벤트에 `item`(0부터, 올린 순서)이 있어요. 여러 사진의 이벤트가 섞여 와요.
| event | data | 설명 |
|---|---|---|
| `sets_stage` | `{item: 0, step, text}` | split_sets=on: 세트 찾는 중 *(v1.6)* |
| `sets` | `{total, dropped, items: [{item, source, label, thumb, is_set}]}` | split_sets=on: 찾은 세트 목록. 이후 item 번호 = 이 순서. `thumb` = 잘라낸 세트 사진, `is_set=false` 면 세트를 못 찾아 사진 전체 *(v1.6)* |
| `item_stage` | `{item, step: analyzing\|applying\|planning\|banner, text}` | 단계 시작 |
| `item_design` | `{item, design: DesignInfo\|null, hand: Hand, hand_auto, hand_reason_ko}` | 인식 결과 + 고른 손. `hand_auto=false` 면 고르지 못해 순서대로 배정 |
| `item_partial` | `{item, phase: nail\|banner, index, image}` | 중간 그림 (절반 크기) |
| `item_nail` | `{item, image, width, height, ms, hand, nails}` | 네일 입힌 손 사진 |
| `item_plan` | `{item, plan: BannerPlan}` | 계획서: concept_ko, mood_ko, headline_ko, subcopy_ko, cta_ko, palette[{name,hex}], background_ko, layout_ko, typography_ko, image_prompt_en, brand, model, usd |
| `item_banner` | `{item, image, mime, width, height, ms, item_usd}` | 완성 배너. `item_usd` = 이 사진 한 장 비용 합계 |
| `item_error` | `{item, status, code, message}` | 이 사진만 실패 (나머지는 계속) |
| `done` | `{total, ok, failed, usd, krw, today}` | 전부 끝남 |

```bash
curl -N -F designs=@a.jpg -F designs=@b.jpg -F brand=네일샷 http://localhost:8000/api/banner_stream
```

## 호출 예시

**curl**
```bash
curl http://localhost:8000/api/hands
curl -F design=@nail.jpg http://localhost:8000/api/analyze
curl -F hand=hand1_blue_front.jpg -F design=@nail.jpg -F quality=high http://localhost:8000/api/generate
curl -N -F hand=hand1_blue_front.jpg -F design=@nail.jpg http://localhost:8000/api/generate_stream
```

**JavaScript (한 번에 결과)**
```js
const fd = new FormData();
fd.append('hand', 'hand1_blue_front.jpg');
fd.append('design', fileInput.files[0]);
fd.append('quality', 'normal');
const r = await fetch('/api/generate', { method: 'POST', body: fd });
const j = await r.json();
if (!r.ok) { alert(j.detail); /* j.code 로 분기 */ }
else { img.src = j.image; console.log(j.usage?.usd, j.nails); }
```

**Python**
```python
import base64, requests
with open("nail.jpg", "rb") as f:
    r = requests.post("http://localhost:8000/api/generate",
                      data={"hand": "hand1_blue_front.jpg", "quality": "normal"},
                      files={"design": f})
j = r.json()
if r.ok:
    open("result.jpg", "wb").write(base64.b64decode(j["image"].split(",", 1)[1]))
else:
    print(j["code"], j["detail"])
```

## 바뀐 점 (v1.7)
- **팁 세트 손가락 순서 통일**: 한 줄의 팁을 사진에서 왼쪽부터 1~5번이라 하면
  왼손·오른손 모두 1번 = 엄지, 2번 = 검지, 3번 = 중지, 4번 = 약지, 5번 = 새끼. 팁의 긴 쪽 위 끝 = 손톱 바깥쪽(끝).
  인식 모델은 팁을 왼쪽부터 하나씩 설명(`tips_en`)만 하고, 손가락 배정은 서버가 이 규칙대로 정해 이미지 모델에 보내요.
- 손 사진이 왼손인지 오른손인지는 처음 켤 때 한 번 인식해서 `hands/hand_side.json`, `nailhand/hand_side.json` 에 저장해요.
  틀리면 그 파일의 "left"/"right" 를 직접 고치면 돼요. 새 사진을 넣으면 그 사진만 다시 확인해요.
- `DesignInfo.hand_side` (`left` | `right` | `both`), `DesignInfo.fingers_ko` (예: "엄지=1번 · 검지=2번 ...")
- 양손(`both`): 한 줄의 팁을 양손에 각각 전부 적용해요 (왼손·오른손 모두 1번=엄지 ... 5번=새끼).

## 바뀐 점 (v1.6)
- **팁 모음 사진 세트 나누기** `split_sets=on`: 사진 1장에 네일 팁 세트(5개씩)가 여러 줄 있으면 인식 모델이 세트마다 팁 위치를
  찾고(`sheet.reasoning_effort`, 기본 medium), 그 줄만 또렷하게 잘라 세트 하나 = 결과 1장으로 만들어요. 팁은 줄 순서대로
  엄지~새끼 손가락에 입혀요. 잘린 세트는 빼요(`sheet.skip_incomplete`). 최대 `sheet.max_sets`장
- 새 이벤트 `sets_stage`, `sets`. `/api/health` 에 `max_sets`

## 바뀐 점 (v1.5)
- **장면 사진 방식** `apply_mode=scene`: `nailhand` 폴더의 장면 사진(손 + 와인/커피/칵테일/향수 등) 중
  ① 인식 모델이 디자인 분위기에 맞는 폴더(폴더 이름의 분위기·키워드 기준) → 그 안에서 사진 1장을 고르고, 같은 호출에서
  바꿀 음료 색(`scene_drink_en`)과 배경(`scene_background_en`)을 정해요. ② 이미지 모델이 마스크 없이 그 사진의 손톱을
  새 디자인으로, 음료 색과 배경도 바꿔요 (생성 1번). `scene_banner=on` 이면 그 사진으로 ③④ 배너까지 (자세 유지).
- `DesignInfo.scene_ko` (음료 · 배경 요약), `/api/health` 에 `scene_banner`, `scene_count`
- 새 주소 `GET /api/scenes`, `GET /nailhand/{폴더}/{파일}`. 설정: settings.yaml `banner.apply_mode`, `banner.scene_banner`

## 바뀐 점 (v1.4)
- `/api/banner_stream` 에 `apply_mode` (mask | free | direct). `/api/health` 에 `apply_mode`

## 바뀐 점 (v1.3)
- **배너 손 자세**: `/api/banner_stream` 에 `pose` 입력. auto 면 ③ 계획서가 디자인에 맞는 자세를 정하고
  (`BannerPlan.pose_change`, `pose_ko`, `pose_en`), ④ 배너에서 그 자세로 다시 그려요. 이때 원본 디자인 사진을
  2번째 참고 이미지로 같이 보내 네일 디자인을 지켜요. ② 네일 입히기는 원래 자세 그대로 (마스크가 그 자세 기준)
- `/api/health` 에 `pose` 추가. 설정: settings.yaml `banner.pose`

## 바뀐 점 (v1.2)
- **긴 손톱**: `nail_length`, `nail_shape` 입력 추가 (`/api/generate`, `/api/generate_stream`, `/api/banner_stream`).
  손 사진의 짧은 손톱 마스크를 손가락 방향으로 늘이고 모양대로 끝을 좁혀서 길고 뾰족한 디자인이 잘리지 않아요.
  손톱이 사진 밖으로 나가면 배경을 넓혀서 **결과 사진이 원본 손 사진보다 클 수 있어요** (`width`/`height` 확인).
  결과에 `nail_extend: {length, shape, ratio}` (ratio 0 = 늘리지 않음). 설정: settings.yaml `nail_extend`
- **Reasoning effort 단계별 지정**: `analysis_effort` (①, `/api/analyze`·`/api/generate*` 도 받음), `plan_effort` (③).
  같은 사진이라도 effort 가 다르면 다시 인식해요 (인식 결과 저장은 사진+effort 기준)
- `DesignInfo` 에 `nail_length`, `nail_shape`, `reasoning_effort`, `BannerPlan` 에 `reasoning_effort` 추가
- 인식 결과 `nail_length` 에 `extra_long` 추가. 참고 사진 자를 때 긴 손톱 끝까지 포함
- `/api/health` 에 `analysis_effort`, `plan_effort`, `plan_model` 추가

## 바뀐 점 (v1.1)
- `POST /api/banner_stream` 추가, 웹 첫 화면이 배너 화면으로 (예전 화면은 `/single`)
- `DesignInfo` 에 `suggested_hand`, `hand_reason_ko` 추가 (`design_analysis.choose_hand`)
- `/api/health` 에 `banner_size`, `max_designs`, `brand` 추가

## 바뀐 점 (v1.0 확정 시)
- 품질 입력: `size`(512/768/1024) → **`quality`(fast/normal/high)**. `size` 는 예전 호환으로 계속 받아요
- 다시 만들기 번호: `seed` → **`variation`**. `seed` 도 계속 받아요
- `/api/generate` 기본 응답: 이미지 파일 + `X-Usage` 헤더 → **`GenerateResult` JSON**. 예전 방식은 `response_format=image` (헤더 이름은 `X-Result`)
- 모든 오류에 `code` 추가. 입력 형식 오류도 `{detail, code, errors}` 로 통일
- SSE `error` 이벤트에 `code` 추가, `stage` 이벤트에 `step` 추가
