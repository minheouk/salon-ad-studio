"""
네일샷 AI API 명세 (코드 버전).  사람이 읽는 설명서: API_SPEC.md
여기 정의한 형식이 실제 응답 형식이에요. 서버를 켜면 http://localhost:8000/docs 에서 같은 내용을 볼 수 있어요.
형식을 바꾸면 API_VERSION 을 올리고 API_SPEC.md 도 함께 고치세요.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

API_VERSION = "1.7"

Quality = Literal["fast", "normal", "high"]
QUALITY_TO_LEVEL = {"fast": 512, "normal": 768, "high": 1024}     # 서버 내부 품질 단계 (settings.yaml quality.*)
LEGACY_SIZE_TO_QUALITY = {512: "fast", 640: "normal", 768: "normal", 1024: "high"}
Effort = Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]     # "" = settings.yaml 값
NailLength = Literal["auto", "keep", "medium", "long", "extra_long"]    # auto = 인식 결과대로, keep = 손 사진 원래 길이
NailShape = Literal["auto", "almond", "oval", "round", "square", "squoval", "coffin", "stiletto"]

ErrorCode = Literal[
    "INVALID_REQUEST",      # 422 필수 항목 누락/형식 오류
    "HAND_NOT_FOUND",       # 400 hand 가 /api/hands 목록에 없음
    "INVALID_IMAGE",        # 400 이미지가 아니거나 열 수 없음
    "FILE_TOO_LARGE",       # 413 10MB 초과
    "NO_NAILS_DETECTED",    # 422 업로드 사진에서 네일 디자인을 못 찾음
    "MASK_MISSING",         # 400 손 사진의 손톱 마스크 없음/비어 있음 (서버 설정 문제)
    "CONTENT_POLICY",       # 400 OpenAI 안전 필터에 걸림
    "UPSTREAM_BAD_REQUEST", # 400 OpenAI 가 요청을 거절
    "UPSTREAM_AUTH",        # 500 OpenAI API 키 문제 (서버 설정 문제)
    "RATE_LIMITED",         # 429 OpenAI 사용량/요금 한도
    "UPSTREAM_TIMEOUT",     # 504 OpenAI 연결 실패/시간 초과
    "NOT_FOUND",            # 404
    "INTERNAL_ERROR",       # 500 그 밖의 서버 오류
]


# ------------------------------------------------------------------ 공통
class ErrorResponse(BaseModel):
    detail: str = Field(..., description="사용자에게 그대로 보여 줄 한국어 메시지", examples=["디자인 사진은 10MB 이하로 올려 주세요"])
    code: ErrorCode = Field(..., description="프로그램이 분기할 때 쓰는 오류 코드")
    errors: Optional[List[dict]] = Field(None, description="INVALID_REQUEST 일 때만: 어떤 항목이 틀렸는지")


class CostTotals(BaseModel):
    count: int = Field(..., description="만든 결과 수 (인식만 한 것은 제외)")
    usd: float = Field(..., description="달러 (인식 + 생성 합계)")
    krw: Optional[int] = Field(None, description="원화 대략값 (prices.json usd_krw 기준). 환율 미설정 시 null")


# ------------------------------------------------------------------ GET /api/health
class HealthResponse(BaseModel):
    ok: bool = True
    api_version: str = Field(API_VERSION)
    model: str = Field(..., description="이미지 생성 모델 (예: gpt-image-2). --fake 모드면 'fake'")
    complexity: Optional[Literal["low", "medium", "high"]] = Field(None, description="구상 강도. null = 모델 기본값")
    analysis_model: Optional[str] = Field(None, description="디자인 인식 모델. 인식 단계를 끄면 null")
    fake: bool = Field(..., description="true 면 OpenAI 를 부르지 않는 화면 확인 모드")
    show_cost: bool = Field(..., description="false 면 비용 정보를 응답에 넣지 않음 (server.hide_cost)")
    qualities: List[Quality] = Field(default_factory=lambda: ["fast", "normal", "high"])
    banner_size: str = Field("1536x1024", description="배너 크기 (settings.yaml banner.size)")
    max_designs: int = Field(5, description="배너 요청 한 번에 올릴 수 있는 사진 수")
    pose: str = Field("auto", description="settings.yaml banner.pose (auto | keep)")
    apply_mode: str = Field("mask", description="settings.yaml banner.apply_mode (mask | free | direct | scene)")
    scene_banner: bool = Field(False, description="settings.yaml banner.scene_banner (scene 방식에서 ③④ 배너까지 만들지)")
    scene_count: int = Field(0, description="nailhand 폴더의 장면 사진 수")
    max_sets: int = Field(12, description="세트 나누기(split_sets=on)로 한 번에 만드는 최대 장수 (settings.yaml sheet.max_sets)")
    brand: str = Field("", description="settings.yaml banner.brand (화면 입력칸 기본값)")
    analysis_effort: str = Field("", description="settings.yaml design_analysis.reasoning_effort (빈 값으로 보내면 이 값)")
    plan_effort: str = Field("", description="settings.yaml banner.plan_reasoning_effort")
    plan_model: Optional[str] = Field(None, description="배너 계획서 모델")


# ------------------------------------------------------------------ GET /api/usage
class UsageResponse(BaseModel):
    today: CostTotals
    total: CostTotals
    usd_krw: Optional[float] = Field(None, description="원화 환산 환율")
    model: str
    complexity: Optional[str] = None


# ------------------------------------------------------------------ GET /api/hands
class Hand(BaseModel):
    id: str = Field(..., description="생성 요청의 hand 값으로 그대로 보내요", examples=["hand1_blue_front.jpg"])
    label: str = Field(..., examples=["hand1_blue_front"])
    url: str = Field(..., description="손 사진 주소 (GET 하면 이미지)", examples=["/hands/hand1_blue_front.jpg"])


# ------------------------------------------------------------------ 디자인 인식 결과
class DesignInfo(BaseModel):
    summary_ko: str = Field(..., description="인식한 디자인 한 줄 요약 (한국어)", examples=["투명 핑크 베이스에 은색 글리터"])
    has_nails: bool = Field(..., description="사진에서 손톱/네일 디자인을 찾았는지")
    confidence: float = Field(..., ge=0, le=1, description="인식 확신도 0~1")
    accent_nails: str = Field("", description="포인트 손톱 설명. 모두 같으면 빈 문자열")
    model: str = Field(..., description="인식에 쓴 모델")
    cached_result: bool = Field(False, description="true = 같은 사진의 이전 인식 결과 재사용 (인식 비용 0)")
    usd: Optional[float] = Field(None, description="이 인식에 든 비용. show_cost=false 면 null")
    suggested_hand: Optional[str] = Field(None, description="인식 모델이 이 디자인에 맞다고 고른 손 사진 id. 못 고르면 null")
    hand_reason_ko: str = Field("", description="그 손을 고른 이유 (한국어)")
    nail_length: str = Field("", description="인식한 손톱 길이 short|medium|long|extra_long|unknown")
    nail_shape: str = Field("", description="인식한 손톱 모양")
    reasoning_effort: str = Field("", description="인식에 쓴 Reasoning effort. 빈 문자열 = 모델 기본값")
    scene_ko: str = Field("", description="scene 방식: 바꿀 음료 색 · 배경 요약 (한국어). 그 외 빈 문자열")
    hand_side: str = Field("", description="고른 손 사진이 왼손(left)인지 오른손(right)인지 양손(both)인지")
    fingers_ko: str = Field("", description="팁 세트일 때 손가락 배정 (예: '엄지=1번 · 검지=2번 ...'). 팁 번호는 줄의 왼쪽부터")


class AnalyzeResponse(BaseModel):
    enabled: bool = Field(..., description="false 면 서버에서 인식 단계를 꺼 둔 상태 (design 은 null)")
    design: Optional[DesignInfo] = Field(None, description="인식 결과. 인식이 실패해도 null (생성은 사진 그대로 진행)")


# ------------------------------------------------------------------ 생성 결과
class NailCheck(BaseModel):
    total: int = Field(..., description="검사한 손톱 수")
    ok: int = Field(..., description="정상")
    bg: int = Field(..., description="배경색으로 칠해진 손톱 (실패)")
    unchanged: int = Field(..., description="디자인이 안 들어간 손톱 (실패)")


class AlignInfo(BaseModel):
    used: bool = Field(..., description="위치 맞춤을 시도했는지 (request.align)")
    ok: Optional[bool] = Field(None, description="맞춤 성공 여부")
    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    cc: Optional[float] = Field(None, description="맞춤 정확도(상관계수)")


class NailExtend(BaseModel):
    length: str = Field(..., description="적용한 길이 keep|short|medium|long|extra_long")
    shape: str = Field(..., description="적용한 모양 (unknown = 모름)")
    ratio: float = Field(..., description="손톱 영역을 손톱 길이의 몇 배 늘렸는지. 0 = 원래 길이")


class GenerationUsage(BaseModel):
    model: str
    quality: str = Field(..., description="OpenAI 에 보낸 품질 (low/medium/high/...)")
    complexity: str = Field("", description="보낸 구상 강도. 빈 문자열 = 모델 기본값")
    text_in: int = Field(0, description="입력 텍스트 토큰")
    image_in: int = Field(0, description="입력 이미지 토큰")
    text_out: int = Field(0, description="출력 텍스트 토큰")
    image_out: int = Field(0, description="출력 이미지 토큰")
    total_tokens: int = 0
    image_usd: Optional[float] = Field(None, description="이미지 생성 비용")
    analysis_usd: Optional[float] = Field(None, description="이번 결과에 포함된 인식 비용 (같은 사진 두 번째부터 0)")
    usd: Optional[float] = Field(None, description="이번 결과 비용 = image_usd + analysis_usd. 가격표에 없는 모델이면 null")
    krw: Optional[float] = None
    priced: bool = Field(..., description="prices.json 에 모델 가격이 있는지")
    today: CostTotals


class GenerateResult(BaseModel):
    image: str = Field(..., description="결과 이미지 data URL (data:image/jpeg;base64,... 또는 image/png)")
    mime: Literal["image/jpeg", "image/png"]
    width: int
    height: int
    ms: int = Field(..., description="OpenAI 생성에 걸린 시간(ms)")
    hand: str = Field(..., description="사용한 손 사진 id")
    quality: Quality
    variation: int
    design: Optional[DesignInfo] = None
    nails: Optional[NailCheck] = Field(None, description="손톱별 검사. 실패(bg/unchanged)가 있으면 다시 만들기 권장")
    align: Optional[AlignInfo] = None
    nail_extend: Optional[NailExtend] = Field(None, description="긴 손톱 처리. 늘렸으면 결과가 원본 손 사진보다 클 수 있어요")
    usage: Optional[GenerationUsage] = Field(None, description="토큰/비용. show_cost=false 면 null")


# ------------------------------------------------------------------ SSE 이벤트 (POST /api/generate_stream)
class StageEvent(BaseModel):
    step: Literal["analyzing", "generating"]
    text: str = Field(..., examples=["디자인 확인 중..."])


class PartialEvent(BaseModel):
    index: int = Field(..., description="0부터. 최대 request.partial_images 개")
    image: str = Field(..., description="중간 그림 data URL (JPEG, 절반 크기, 미리보기용)")


class ErrorEvent(BaseModel):
    status: int = Field(..., description="HTTP 상태 코드에 해당하는 값")
    code: ErrorCode
    message: str


# ------------------------------------------------------------------ 배너 (POST /api/banner_stream, SSE)
#  사진 1~5장 -> 장마다: 디자인 인식 + 손 고르기 -> 손에 네일 입히기 -> 배너 계획서 -> 배너
#  모든 이벤트에 item(0부터, 올린 사진 순서)이 붙어요. 사진끼리는 동시에 진행돼서 이벤트가 섞여 와요.
BannerStep = Literal["analyzing", "applying", "planning", "banner"]


class PaletteColor(BaseModel):
    name: str
    hex: str = Field(..., examples=["#F4C2C2"])


class BannerPlan(BaseModel):
    concept_ko: str = Field(..., description="한 줄 콘셉트")
    mood_ko: str
    headline_ko: str = Field(..., description="큰 제목 (14자 이내)")
    subcopy_ko: str = Field(..., description="작은 문구 (30자 이내)")
    cta_ko: str = Field(..., description="버튼 문구 (10자 이내)")
    palette: List[PaletteColor]
    background_ko: str
    layout_ko: str
    typography_ko: str
    image_prompt_en: str = Field(..., description="이미지 모델에 보낸 연출 지시 (영어)")
    brand: str = Field("", description="배너에 넣은 상호명. 없으면 빈 문자열")
    model: str = Field(..., description="계획서를 만든 모델")
    reasoning_effort: str = Field("", description="계획서에 쓴 Reasoning effort. 빈 문자열 = 모델 기본값")
    pose_change: bool = Field(False, description="true = 배너에서 손 자세를 바꿈")
    pose_ko: str = Field("", description="고른 자세 설명 (한국어)")
    pose_en: str = Field("", description="이미지 모델에 보낸 자세 지시 (영어). 자세 유지면 빈 문자열")
    usd: Optional[float] = Field(None, description="계획서 비용. show_cost=false 면 null")


class BannerSetItem(BaseModel):
    item: int = Field(..., description="이후 item_* 이벤트의 item 번호")
    source: int = Field(..., description="몇 번째로 올린 사진에서 나온 세트인지 (0부터)")
    label: str = Field(..., description="예: '1번 사진 · 3세트 · 핑크 리본'")
    thumb: str = Field(..., description="잘라낸 세트 사진 (data URL)")
    is_set: bool = Field(True, description="true = 세트를 잘라낸 사진, false = 세트를 못 찾아 사진 전체")


class BannerSetsEvent(BaseModel):
    """split_sets=on 일 때 맨 처음: 사진에서 찾은 세트 목록 (이 순서대로 item 0, 1, 2 ...)"""
    total: int
    dropped: int = Field(0, description="sheet.max_sets 를 넘어 만들지 않는 세트 수")
    items: List[BannerSetItem]


class BannerStageEvent(BaseModel):
    item: int
    step: BannerStep
    text: str


class BannerDesignEvent(BaseModel):
    item: int
    design: Optional[DesignInfo] = Field(None, description="인식 결과. 인식 끔/실패면 null")
    hand: Hand = Field(..., description="네일을 입힐 손 사진")
    hand_auto: bool = Field(..., description="true = 인식 모델이 디자인을 보고 고름, false = 고르지 못해 순서대로 배정")
    hand_reason_ko: str = ""


class BannerPartialEvent(BaseModel):
    item: int
    phase: Literal["nail", "banner"]
    index: int
    image: str = Field(..., description="중간 그림 data URL (JPEG, 절반 크기)")


class BannerNailEvent(BaseModel):
    item: int
    image: str = Field(..., description="네일 입힌 손 사진 data URL")
    width: int
    height: int
    ms: int
    hand: str
    nails: Optional[NailCheck] = None
    nail_extend: Optional[NailExtend] = None


class BannerPlanEvent(BaseModel):
    item: int
    plan: BannerPlan


class BannerResultEvent(BaseModel):
    item: int
    image: str = Field(..., description="완성 배너 data URL")
    mime: Literal["image/jpeg", "image/png"]
    width: int
    height: int
    ms: int = Field(..., description="배너 생성 시간(ms)")
    item_usd: Optional[float] = Field(None, description="이 사진 한 장에 든 비용 합계 (인식+입히기+계획서+배너)")


class BannerErrorEvent(BaseModel):
    item: int
    status: int
    code: ErrorCode
    message: str


class BannerDoneEvent(BaseModel):
    total: int
    ok: int
    failed: int
    usd: Optional[float] = Field(None, description="이번 요청 전체 비용")
    krw: Optional[float] = None
    today: Optional[CostTotals] = None
