"""
네일샷 AI 체험 서버 (OpenAI 버전) - 손 사진 선택 + 디자인 사진 업로드 -> 네일아트 입힌 손 사진
start_server.bat 로 실행하세요. 그래픽카드도, 학습도 필요 없어요.

  http://localhost:8000          웹 화면 (web/index.html 그대로)
  GET  /api/hands                고를 수 있는 손 사진 목록
  POST /api/generate             form: hand=<파일이름>, design=<이미지 파일>, seed=<숫자, 선택>, desc=<영어 설명, 선택>,
                                       size=<512|768|1024: 품질 low|medium|high>
                                 -> image/png (헤더 X-Elapsed-Ms: 걸린 시간)

어떻게 만드나요
  1) 손 사진 + 손톱 마스크(hands/masks/<이름>.png) + 디자인 사진을 OpenAI 이미지 편집 API 에 보내요.
     마스크가 없으면 hands/nails.json 의 손톱 위치로 마스크를 만들어요.
  2) 돌아온 그림에서 손톱 부분만 잘라 원본 손 사진 위에 부드럽게 붙여요.
     -> 손·피부·배경은 원본 그대로 (API 가 마스크 밖을 조금 바꿔도 결과에는 안 나와요)

설정
  settings.yaml   모델, 구상 강도(complexity = 추론 강도 역할), 버튼별 품질, 포트 등 -> 여기서 고치세요
  .env            OPENAI_API_KEY=sk-...  (비밀 키만)
  적용 순서: 실행 옵션(--model, --effort ...) > settings.yaml > .env > 기본값

비용 표시
  요청마다 서버 창에 토큰/비용, 웹 화면 결과 아래에 이번 비용과 오늘 누적을 보여 줘요.
  기록: usage_log.csv (엑셀로 열 수 있어요)   요약: GET /api/usage
  가격표: prices.json (OpenAI 가격이 바뀌면 숫자만 고치세요)
"""
import argparse, base64, csv, glob, io, json, os, re, sys, threading, time
import urllib.parse
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
HANDS = os.path.join(ROOT, "hands")
MASKS = os.path.join(HANDS, "masks")
NAILHAND = os.path.join(ROOT, "nailhand")     # 장면 사진: nailhand/<분위기(키워드)>/<사진> (마스크 없이 생으로)
WEB = os.path.join(ROOT, "web")
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}   # 브라우저가 예전 화면을 쓰지 않게
PRICES_FILE = os.path.join(ROOT, "prices.json")
USAGE_LOG = os.path.join(ROOT, "usage_log.csv")
MAX_UPLOAD = 10 * 1024 * 1024
EXTS = (".jpg", ".jpeg", ".png", ".webp")
QUALITY = {512: "low", 640: "medium", 768: "medium", 1024: "high"}   # settings.yaml 의 quality 로 바뀌어요
COMPLEXITY = ("low", "medium", "high")
QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
SETTINGS_FILE = os.path.join(ROOT, "settings.yaml")
DEFAULTS = {
    "openai": {"model": "gpt-image-1.5", "complexity": "", "input_fidelity": "auto"},
    "quality": {"fast": "low", "normal": "medium", "high": "high"},
    "request": {"design_max_size": 1024, "concurrency": 2, "timeout_sec": 240,
                "zoom_to_nails": True, "zoom_padding": 0.25, "align": True,
                "partial_images": 2, "canvas_long_side": 1024, "api_output_format": "webp"},
    "server": {"port": 8000, "lan": False, "hide_cost": False, "result_format": "jpeg", "api_docs": True},
    "debug": {"save_images": True},
    "design_analysis": {"enabled": True, "model": "gpt-5.4-mini", "reasoning_effort": "low",
                        "crop_reference": True, "reject_if_no_nails": True, "choose_hand": True},
    "nail_extend": {"enabled": True, "medium": 0.4, "long": 0.95, "extra_long": 1.5},
    "banner": {"size": "1024x1280", "max_designs": 5, "plan_model": "", "plan_reasoning_effort": "low",
               "text_on_image": True, "brand": "", "pose": "auto", "apply_mode": "mask",
               "scene_banner": False, "tip_paste": False, "tip_paste_after": True, "tip_guide": False, "guide_inset": True, "guide_model": True,
               "guide_quality": "medium", "original_ratio": 70, "natural_level": 50,
               "tip_change_drink": True, "guide_left": "", "guide_right": ""},
    "sheet": {"enabled": True, "max_sets": 12, "reasoning_effort": "medium", "skip_incomplete": True},
}
REASONING = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# 투명(클리어) 팁: 배경이 비쳐 보여서 놓치기 쉬워요 -> 인식 · 세트 찾기 · 그리기 모두에 같은 규칙
CLEAR_SEE_EN = (
    "CLEAR / TRANSPARENT parts: many nails and tips have parts that are clear jelly with no color of their own - the "
    "background shows through them, so they are visible only by their outline, edges, gloss and highlights. They ARE "
    "part of the nail and of the design: never skip them, never treat them as background, and never describe the "
    "background color or texture seen through them as a nail color or pattern."
)
CLEAR_PAINT_EN = (
    " Where the design (or a tip description) says a part is clear / transparent, paint that part as glossy clear "
    "gel through which this hand's own natural nail shows (pinkish nail bed, natural free edge) - not the background "
    "color of image 2, not white, not grey and not the old polish (remove the old color there too). Decorations on a "
    "clear part (bows, lace, charms, dots, stickers) sit on top of the clear gel exactly where described."
)
# '글자 · 로고 · 워터마크 금지'는 새로 생기는 것만 막아요. 손톱 디자인 안의 캐릭터 · 글자 · 로고 · 모티프는 그대로 옮겨요
#   (올리는 팁 · 디자인 사진은 사용 허가를 받은 것 - 디자인은 바꾸지 않고 그대로)
NAIL_ART_KEEP_EN = (
    " This ban never applies to the nail design itself: characters, letters, logos and motifs that are part of the "
    "nail art in the design images are copied onto the nails exactly as they are - never removed, blurred, "
    "simplified, replaced or changed."
)
ANALYSIS_INSTRUCTIONS = (
    "You are a professional nail art analyst. The user uploads an arbitrary photo (a salon photo, an Instagram "
    "screenshot, a product shot, a close-up of a hand, possibly with text, props or several hands). "
    "Find the fingernails and describe the nail design so that a painter can reproduce it exactly on another hand: "
    "base color(s) with specific color names, gradient direction, pattern and where it sits on the nail, decorations "
    "(glitter, foil, chrome, rhinestones, pearls, stickers, 3D charms) with their size and position, and the finish "
    "(glossy, matte, jelly/sheer, cat-eye, chrome). " + CLEAR_SEE_EN + " Say explicitly which part of the nail is "
    "clear (e.g. 'clear free edge with a 3D bow'). If nails differ, describe the main design in design_en and the "
    "accent nails in accent_nails (e.g. 'ring finger: silver glitter'). Ignore skin, rings, background and props. "
    "nail_shape and nail_length describe the design's nails, not the hand: nail_length compared to the fingertip - "
    "short = ends at or just past the fingertip, medium = clearly past it, long = free edge about as long as the "
    "nail bed, extra_long = longer than the nail bed (dramatic stiletto/coffin extensions). "
    "best_region: normalized 0-1 box (x0,y0 = top-left, x1,y1 = bottom-right) around the nails that show the "
    "design most clearly (may include several nails), always including the FULL length of every nail up to its "
    "very tip - never cut off long or pointy tips. summary_ko: one short Korean sentence for the customer "
    "(max 60 characters). design_en: English, 40-90 words. tips_en: [] (empty) unless told otherwise. If no fingernails or nail design are visible, set "
    "has_nails=false, confidence low, and explain briefly in summary_ko."
)
# 2번째 이미지(손 사진 후보 모음)를 같이 보낼 때만 붙이는 설명 -> 같은 호출에서 손 사진도 골라요 (호출 1번 = 빠르고 저렴)
HAND_CHOICE_INSTRUCTIONS = (
    " Image 2 is a contact sheet of {n} numbered hand photos (1 to {n}) that are available as models for a try-on and "
    "an advertising banner: {labels}. Choose hand_index (1-{n}): the hand whose skin tone, pose (front/side), "
    "nail shape and length, and background color would show THIS nail design most attractively in an ad (for "
    "example: contrast with the nail colors, a side pose for 3D decorations, a front pose for patterns across all "
    "nails). hand_reason_ko: one short Korean reason (max 40 characters). Image 2 is never the design - analyze "
    "the design only from image 1."
)
ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["has_nails", "confidence", "summary_ko", "design_en", "accent_nails", "finish",
                 "nail_shape", "nail_length", "best_region", "hand_index", "hand_reason_ko", "tips_en"],
    "properties": {
        "tips_en": {"type": "array", "items": {"type": "string"},
                    "description": "one description per tip, left to right; empty unless the photo is one tip set"},
        "hand_index": {"type": "integer", "description": "1-based number on the hand contact sheet; 0 if none"},
        "hand_reason_ko": {"type": "string"},
        "has_nails": {"type": "boolean"},
        "confidence": {"type": "number", "description": "0 to 1"},
        "summary_ko": {"type": "string"},
        "design_en": {"type": "string"},
        "accent_nails": {"type": "string", "description": "empty string if all nails are the same"},
        "finish": {"type": "string"},
        "nail_shape": {"type": "string", "enum": ["almond", "oval", "round", "square", "squoval", "coffin",
                                                  "stiletto", "unknown"]},
        "nail_length": {"type": "string", "enum": ["short", "medium", "long", "extra_long", "unknown"]},
        "best_region": {"type": "object", "additionalProperties": False, "required": ["x0", "y0", "x1", "y1"],
                        "properties": {k: {"type": "number"} for k in ("x0", "y0", "x1", "y1")}},
    },
}


def load_settings(path=SETTINGS_FILE):
    """settings.yaml 을 읽어 기본값 위에 덮어요. 파일이 없거나 틀리면 이유를 알려 줘요."""
    import copy
    s = copy.deepcopy(DEFAULTS)
    if not os.path.isfile(path):
        print("settings.yaml 이 없어서 기본 설정을 써요.")
        return s, False
    try:
        import yaml
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
    except ImportError:
        sys.exit("settings.yaml 을 읽으려면 pyyaml 이 필요해요:  venv_api\\Scripts\\python.exe -m pip install pyyaml")
    except Exception as e:
        sys.exit(f"settings.yaml 형식이 잘못됐어요 (들여쓰기/콜론 확인):\n  {e}")
    if not isinstance(data, dict):
        sys.exit("settings.yaml 형식이 잘못됐어요 (맨 위가 'openai:' 같은 항목이어야 해요)")
    for sec, vals in data.items():
        if sec not in s:
            print(f"! settings.yaml: 모르는 항목 '{sec}' 는 무시해요")
            continue
        if not isinstance(vals, dict):
            sys.exit(f"settings.yaml: '{sec}:' 아래 항목은 빈칸 2개로 들여써 주세요")
        for k, v in vals.items():
            if k not in s[sec]:
                print(f"! settings.yaml: 모르는 항목 '{sec}.{k}' 는 무시해요")
                continue
            s[sec][k] = "" if v is None else v
    # 값 검사
    o, q = s["openai"], s["quality"]
    o["model"] = str(o["model"]).strip()
    o["complexity"] = str(o["complexity"]).strip().lower()
    o["input_fidelity"] = str(o["input_fidelity"]).strip().lower()
    if o["complexity"] not in ("",) + COMPLEXITY:
        sys.exit(f"settings.yaml: openai.complexity 는 low / medium / high / \"\" 중 하나예요 (지금: {o['complexity']})")
    if o["input_fidelity"] not in ("auto", "high", "low", "off"):
        sys.exit(f"settings.yaml: openai.input_fidelity 는 auto / high / low / off 중 하나예요 (지금: {o['input_fidelity']})")
    for k in q:
        q[k] = str(q[k]).strip().lower()
        if q[k] not in QUALITIES:
            sys.exit(f"settings.yaml: quality.{k} 는 {' / '.join(QUALITIES)} 중 하나예요 (지금: {q[k]})")
    d = s["design_analysis"]
    d["model"] = str(d["model"]).strip()
    d["reasoning_effort"] = str(d["reasoning_effort"]).strip().lower()
    if d["reasoning_effort"] not in ("",) + REASONING:
        sys.exit(f"settings.yaml: design_analysis.reasoning_effort 는 {' / '.join(REASONING)} / \"\" 중 하나예요 "
                 f"(지금: {d['reasoning_effort']})")
    b = s["banner"]
    b["plan_model"] = str(b["plan_model"]).strip()
    b["plan_reasoning_effort"] = str(b["plan_reasoning_effort"]).strip().lower()
    b["brand"] = str(b["brand"]).strip()
    b["pose"] = str(b["pose"]).strip().lower()
    b["apply_mode"] = str(b["apply_mode"]).strip().lower()
    if b["apply_mode"] not in APPLY_MODES:
        sys.exit(f"settings.yaml: banner.apply_mode 는 mask / free / direct / scene 중 하나예요 (지금: {b['apply_mode']})")
    b["scene_banner"] = str(b["scene_banner"]).strip().lower() in ("true", "1", "yes", "on")
    if b["pose"] not in ("auto", "keep"):
        sys.exit(f"settings.yaml: banner.pose 는 auto / keep 중 하나예요 (지금: {b['pose']})")
    if b["plan_reasoning_effort"] not in ("",) + REASONING:
        sys.exit(f"settings.yaml: banner.plan_reasoning_effort 는 {' / '.join(REASONING)} / \"\" 중 하나예요 "
                 f"(지금: {b['plan_reasoning_effort']})")
    if parse_size(b["size"], None) is None:
        sys.exit(f"settings.yaml: banner.size 는 1536x1024 처럼 '가로x세로' 로 적어 주세요 (지금: {b['size']})")
    b["max_designs"] = max(1, min(5, int(b["max_designs"] or 5)))
    sh = s["sheet"]
    sh["max_sets"] = max(1, min(30, int(sh["max_sets"] or 12)))
    sh["reasoning_effort"] = str(sh["reasoning_effort"]).strip().lower()
    if sh["reasoning_effort"] not in ("",) + REASONING:
        sys.exit(f"settings.yaml: sheet.reasoning_effort 는 {' / '.join(REASONING)} / \"\" 중 하나예요")
    ne = s["nail_extend"]
    for k in ("medium", "long", "extra_long"):
        try:
            ne[k] = max(0.0, min(3.0, float(ne[k])))
        except (TypeError, ValueError):
            sys.exit(f"settings.yaml: nail_extend.{k} 는 0~3 사이 숫자예요 (지금: {ne[k]})")
    return s, True

PROMPT = (
    "Image 1 is a photo of a hand. Image 2 is a nail art design reference. "
    "Paint the nail design from image 2 onto every fingernail of the hand in image 1, so the result looks like a real, "
    "professional manicure photo. Match the base colors, patterns, finish (glossy, matte, chrome, glitter), decorations "
    "and small details of image 2, adapted naturally to each nail's shape, angle, curvature and the photo's lighting, "
    "with realistic gloss highlights and clean edges along the cuticles and sidewalls. "
    "Only change the fingernails (the transparent area of the mask). The transparent areas are the nail plates: "
    "fill every one of them with the nail design - never with background color or skin. "
    "Keep the hand, skin tone, fingers, pose, framing, lighting and background exactly the same, with every finger "
    "in exactly the same position (do not move, zoom or crop the hand). "
    "No text, no watermark, no extra fingers, no rings or jewelry." + NAIL_ART_KEEP_EN
)

# ---------------------------------------------------------------------------------------------- 배너
NAIL_KEEP_SHAPE = " Keep each nail's original size and shape; only paint the design on it."
NAIL_EXTEND_SHAPE = (
    " IMPORTANT - nail length and shape: the design has {length} {shape} nails, longer than this hand's natural "
    "nails. For this, the transparent area of the mask has been extended beyond every fingertip along the finger's "
    "direction, already shaped like the target nail. Grow every nail out from its fingertip into that extended area "
    "as a realistic {shape} nail extension that reaches all the way to the tip of the transparent area, with a "
    "{tip} free edge, consistent thickness, gloss and a soft shadow on the background. Show the WHOLE design along "
    "the full length of each nail, including the patterns and decorations near the tip - do not shorten, round off "
    "or crop the nails. Any part of the transparent area outside the new nails must match the original background."
)
NAIL_TIP_EN = {"stiletto": "sharp pointed", "almond": "softly pointed", "coffin": "flat, tapered (ballerina)",
               "square": "straight, square", "squoval": "squared with soft corners", "oval": "rounded oval",
               "round": "round", "unknown": "natural"}

# 팁 끝 모양 통일: 세트 팁의 손톱 끝 모양을 이름 + 설명으로 Image-2 에 못 박아요 (장면 사진 · 동그란 사진 모두)
TIP_SHAPE_LOCK = True
TIP_SHAPE_EN = {
    "square": "SQUARE - perfectly straight, flat free edge with sharp 90-degree corners, straight parallel side walls",
    "squoval": "SQUOVAL - straight flat free edge with only slightly softened corners, straight side walls",
    "round": "ROUND - the free edge is an even semicircle following the fingertip, side walls straight",
    "oval": "OVAL - long side walls that curve evenly into an egg-shaped rounded free edge",
    "almond": "ALMOND - side walls taper gradually to a narrow, softly rounded point at the center",
    "coffin": "COFFIN (BALLERINA) - side walls taper inward and end in a straight, flat, narrow free edge with crisp corners",
    "stiletto": "STILETTO - side walls taper sharply all the way to a sharp point",
}


# 뾰족한 팁(스틸레토 · 아몬드): 길고 가늘어서 방향이 조금만 틀어져도 끝이 크게 빗나가요
#   -> luna 가 손가락마다 큐티클 · 방향을 다시 봐서 위치를 고치고, 길이 상한을 늘리고, Image-2 에 모양 · 위치 보정을 더 맡겨요
POINTED_SHAPES = ("stiletto", "almond")
POINTED_HELP = True
POINTED_LEN_RANGE = (0.6, 1.8)        # 뾰족한 팁은 레퍼런스 손톱의 1.8배까지 길게 (보통 1.35배)
POINTED_MAX_TURN = 25                 # luna 보정: 방향은 이 각도(도)까지만
POINTED_MAX_SHIFT = 0.35              # luna 보정: 큐티클 위치는 손톱 폭의 이 배까지만 옮겨요
POINTED_MIN_TURN = 8                  # luna 좌표는 손톱 폭의 0.1~0.3배쯤 흔들려요 -> 이보다 작은 회전과
POINTED_MIN_SHIFT = 0.3               #   이보다 작은 이동만 있으면 손으로 찍은 JSON 위치가 더 정확해서 그대로 둬요
POINTED_EN = (
    " POINTED NAILS ({shape}): these are long pointed press-on tips. Every nail must be ONE smooth tapered nail that "
    "starts at the cuticle of the real nail and narrows to a single point exactly on the finger's center line, "
    "pointing straight along the direction the finger extends (continue the line from the cuticle through the middle "
    "of the real nail). The laid cut-outs are only approximate: correct each one's angle, position and taper as "
    "needed so the point is centered and aligned with its own finger and the nail sits flush on the nail bed - never "
    "bend the point sideways, never cut it short, blunt or square it. Keep the tip's full length extending beyond the "
    "fingertip (repaint whatever is behind the extension) and keep every motif of the design at the same relative "
    "place along the nail (studs, dots, letters, charms stay at the same fraction from cuticle to point, letters run "
    "along the nail)."
)
LUNA_PLACE_INSTRUCTIONS = """You help place long POINTED press-on nails on a hand photo. Image 1 is the original
photo. Image 2 is the same photo with the program's current placement drawn for each finger: a colored line starting
at a dot (the nail's cuticle center) and going toward the fingertip, labeled T (thumb), I (index), M (middle),
R (ring), L (little). The pointed nail will be laid along that line and extend well beyond the fingertip, so even a
small angle error becomes a big mistake. For every listed finger look at the REAL nail and finger in image 1 and give:
cuticle = the center of that real nail's cuticle edge, direction_point = a point on the finger's center line at the
free edge (fingertip end) of the real nail, so the line cuticle -> direction_point follows exactly the direction the
finger extends. Both as fractions of the image width and height (x, y). needs_fix = false if the drawn line already
starts at the cuticle and follows the finger within a few degrees."""
LUNA_PLACE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fingers"],
                     "properties": {"fingers": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False,
                         "required": ["finger", "needs_fix", "cuticle", "direction_point"],
                         "properties": {"finger": {"type": "string",
                                                   "enum": ["thumb", "index", "middle", "ring", "little"]},
                                        "needs_fix": {"type": "boolean"},
                                        "cuticle": {"type": "array", "items": {"type": "number"}},
                                        "direction_point": {"type": "array", "items": {"type": "number"}}}}}}}


def is_pointed(analysis):
    return POINTED_HELP and (analysis or {}).get("nail_shape") in POINTED_SHAPES


POINTED_THUMB_EN = (
    " THUMB (pointed set): the thumb is usually seen at an angle or from the side, so its laid cut-out is only a hint "
    "for the design. Re-render the thumb nail much more freely than the other nails: give it the same pointed shape "
    "and a full length that suits the thumb, with the point aimed exactly along the thumb's direction, sitting flush "
    "on the thumb's own nail bed from its cuticle, with correct perspective, curvature, shading and highlights - "
    "change its outline, angle, size and position as much as needed so it looks naturally worn, while keeping its "
    "design (colors, motifs and their order from cuticle to point)."
)


THUMB_FREE = True            # 엄지는 모델이 다른 손가락보다 더 많이 관여해서 다시 그려요 (모든 팁 모양)
THUMB_PASTE_ALPHA = 0.45     # 모델에 보내는 대략 얹은 사진에서 엄지 팁의 진하기 (1 = 다른 손가락처럼 그대로, 낮을수록 '힌트'만)
THUMB_FREE_EN = (
    " THUMB - re-render it, do not copy the pasted cut-out: the thumb sits at a different angle from the other "
    "fingers (often turned toward the camera at an angle or seen partly from the side), so in image 1 its tip is "
    "pasted only FAINTLY as a rough hint of where it goes and which design it has. Paint the thumb nail anew, much "
    "more freely than the other nails: it must sit flush on the thumb's own nail bed, start at the thumb's cuticle, "
    "follow the thumb's direction, width and curvature, with correct perspective/foreshortening for this view, and "
    "the same lighting, shading, gloss and skin contact as the other nails - like a real press-on worn on that thumb, "
    "never a flat sticker or a cut-out pasted on top. Change its outline, angle, size and position as much as "
    "needed for that, while keeping its design (colors, motifs, charms and their order from cuticle to free edge) "
    "and the set's nail shape and length."
)


def thumb_soft(fingers):
    """모델이 다시 그리는 사진에 팁을 얹을 때 엄지만 흐리게 -> {1: 진하기} 또는 None."""
    return {1: THUMB_PASTE_ALPHA} if THUMB_FREE and 1 in (fingers or []) and THUMB_PASTE_ALPHA < 1 else None


def thumb_text(analysis, fingers=None):
    """프롬프트용: 엄지를 더 자유롭게 다시 그리라는 지시 (뾰족한 세트는 pointed_text 의 엄지 지시가 대신해요)."""
    if not THUMB_FREE or is_pointed(analysis) or not (fingers is None or 1 in fingers):
        return ""
    return THUMB_FREE_EN


def pointed_text(analysis, fingers=None):
    if not is_pointed(analysis):
        return thumb_text(analysis, fingers)
    s = POINTED_EN.format(shape=(analysis or {}).get("nail_shape", "").upper())
    return s + (POINTED_THUMB_EN if fingers is None or 1 in fingers else "")


def geo_redirect(g, W, H, new_base, new_dir):
    """손톱 자리 g 를 큐티클 new_base(픽셀) · 방향 new_dir 로 옮겨요 (길이 · 폭 그대로, 상자도 같이 돌리고 옮겨요)."""
    import math
    import numpy as np
    b = np.array([g["base"][0] * W, g["base"][1] * H])
    tp = np.array([g["tip"][0] * W, g["tip"][1] * H])
    L = float(np.hypot(*(tp - b)))
    if L < 2:
        return g
    a0 = math.atan2(*(tp - b)[::-1])
    a1 = math.atan2(new_dir[1], new_dir[0])
    c, s = math.cos(a1 - a0), math.sin(a1 - a0)
    R = np.array([[c, -s], [s, c]])
    nb = np.array(new_base, float)
    mv = lambda q: R @ (np.array(q, float) - b) + nb
    out = dict(g)
    out["base"] = tuple(float(v) for v in nb / [W, H])
    out["tip"] = tuple(float(v) for v in mv(tp) / [W, H])
    if g.get("box"):
        out["box"] = dict(g["box"], corners=tuple(tuple(float(v) for v in mv((x * W, y * H)) / [W, H])
                                                  for x, y in g["box"]["corners"]))
    return out


def tip_shape_local(tips):
    """팁 사진들(tip_mask) -> 손톱 끝 모양 (세트 다수결) 또는 None. 참고용 (시험해 보니 정확도가 낮아 프롬프트에는 안 써요)."""
    import numpy as np
    votes = []
    for t in tips or []:
        mk = getattr(t, "info", {}).get("tip_mask")
        if mk is None:
            continue
        ys, xs = np.nonzero(mk)
        if len(xs) < 200:
            continue
        y0, y1 = ys.min(), ys.max()
        L = float(y1 - y0 + 1)
        wid = lambda f: float((mk[int(y0 + f * L)] > 0).sum())
        wmax = max(1.0, max(wid(f) for f in np.linspace(0.2, 0.8, 13)))
        w3, w10, w30 = wid(0.03) / wmax, wid(0.10) / wmax, wid(0.30) / wmax
        asp = L / wmax
        if w10 < 0.35:
            s = "stiletto"
        elif w10 < 0.62:
            s = "almond" if w3 < 0.35 else ("coffin" if w30 < 0.9 and w3 > 0.45 else "oval")
        elif w3 > 0.8 and w10 > 0.88:
            s = "coffin" if w30 < 0.9 else "square"
        elif w3 > 0.6 and w10 > 0.8:
            s = "squoval"
        else:
            s = "oval" if asp > 1.6 else "round"
        votes.append(s)
    if not votes:
        return None
    return max(set(votes), key=votes.count)


def tip_shape_text(analysis, tips):
    """세트 팁의 끝 모양을 모든 손톱에 똑같이 - 프롬프트 문장. 모양을 모르면 ''."""
    if not TIP_SHAPE_LOCK:
        return ""
    s = (analysis or {}).get("nail_shape")         # luna 가 팁 사진을 보고 고른 모양 (프로그램 추정은 부정확해서 안 써요)
    if s not in TIP_SHAPE_EN:
        return (" FREE-EDGE SHAPE: copy the free-edge outline of each tip image exactly (the same corners, curve or "
                "point, and the same length) - do not round off, square off, point, file or shorten any nail "
                "differently from its tip.")
    return (f" FREE-EDGE SHAPE (read from the tip photos, the SAME on every nail): {TIP_SHAPE_EN[s]}. Every nail in "
            "this photo must end in exactly this free-edge shape and keep the tip's length - copy the outline of each "
            "tip image; do not round off, square off, point, file or shorten any nail differently from the tips, and "
            "do not mix shapes between fingers.")


# ---------------------------------------------------------------------------------------------- 적용 방식
# mask   : ② 손톱 마스크로 손톱만 그려 원본에 붙이기 (손·배경 원본 그대로, 가장 정확)
# free   : ② 마스크 없이 손 사진 + 디자인 사진만 주고 이미지 모델이 통째로 다시 그리기 (마스크 준비 불필요)
# direct : ② 없이 ④ 배너에서 맨손 사진 + 디자인 사진으로 한 번에 (이미지 생성 1번, 가장 저렴)
APPLY_MODES = ("mask", "free", "direct", "scene")
FREE_PROMPT = (
    "Image 1 is a photo of a hand. Image 2 is a nail art design reference. Paint the nail design from image 2 "
    "onto all five fingernails of the hand in image 1 (including the thumb and little finger), so the result looks "
    "like a real, professional manicure photo: match the base colors, patterns, finish, decorations, accent nails "
    "and small details of image 2, adapted to each nail's angle, curvature and the photo's lighting, with realistic "
    "gloss and clean cuticle lines. Keep the same hand: skin tone, finger shape, pose, framing, lighting and "
    "background as in image 1 - only the nails change. No text, no watermark, no extra fingers, no rings or jewelry."
    + NAIL_ART_KEEP_EN
)
NAILS_LEN_FREE = (
    " The design has {length} {shape} nails with a {tip} free edge: give every nail that length and shape, growing "
    "naturally beyond the fingertips with realistic thickness and a soft shadow, and show the WHOLE design along the "
    "full nail length. The nails must be fully inside the picture, never cut off by the edge."
)
DIRECT_NAILS = (
    "Image 1 is a model hand with plain nails and image 2 is a nail art design. First paint the nail design from "
    "image 2 onto all five fingernails of that hand (including the thumb and little finger) as a realistic, "
    "professional manicure: the same base colors, patterns, decorations, finish and accent nails as image 2. "
)

PLAN_INSTRUCTIONS = (
    "You are the art director of a nail salon, planning one advertising web banner. Image 1 is the finished hero "
    "photo: a model hand wearing the nail art. The banner will be made by an image model that keeps this hand and "
    "its nails exactly as they are and builds a new background and layout around it. "
    "Plan a {w}x{h} pixel ({orient}) banner that sells THIS nail design: a background (colors, setting, textures, "
    "props such as fabric, flowers, stones, water, light) that complements the nail colors without competing with "
    "them, lighting, composition (where the hand sits and how big, where the negative space for the copy is), "
    "short Korean ad copy and the typography style. Keep it premium, clean and realistic; the nails must stay the "
    "focal point. palette: 3-5 colors with hex codes taken from or harmonizing with the nails. "
    "All copy (headline_ko, subcopy_ko, cta_ko) and every *_ko field MUST be written in Korean (Hangul) - never "
    "Japanese, Chinese or English, whatever the theme. "
    "headline_ko max 14 characters, subcopy_ko max 30 characters, cta_ko max 10 characters (e.g. '지금 예약하기'). "
    "{brand_rule} {text_rule} {pose_rule} "
    "background_ko, layout_ko, typography_ko, concept_ko, mood_ko: short Korean sentences for the salon owner. "
    "image_prompt_en: English, 80-160 words, a complete art-direction prompt for the image model describing the "
    "background, props, lighting, camera, composition, the hand's placement and scale, and exactly where each text "
    "element goes (quote the Korean text verbatim). Never ask to repaint or change the nails."
)
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["concept_ko", "mood_ko", "headline_ko", "subcopy_ko", "cta_ko", "palette", "background_ko",
                 "layout_ko", "typography_ko", "image_prompt_en", "pose_change", "pose_ko", "pose_en"],
    "properties": {
        "concept_ko": {"type": "string"}, "mood_ko": {"type": "string"},
        "headline_ko": {"type": "string"}, "subcopy_ko": {"type": "string"}, "cta_ko": {"type": "string"},
        "palette": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "hex"],
            "properties": {"name": {"type": "string"}, "hex": {"type": "string"}}}},
        "background_ko": {"type": "string"}, "layout_ko": {"type": "string"}, "typography_ko": {"type": "string"},
        "image_prompt_en": {"type": "string"},
        "pose_change": {"type": "boolean"}, "pose_ko": {"type": "string"}, "pose_en": {"type": "string"},
    },
}
POSE_RULE_AUTO = (
    "Hand pose: decide whether a different hand pose would show THIS nail design better than the current photo, "
    "and if so describe it. Pick the pose from the design: long stiletto/almond/coffin nails -> elegant, elongated "
    "fingers, slightly spread, side or three-quarter angle so the full length and taper are visible; 3D "
    "decorations, charms, rhinestones or chrome -> a three-quarter angle with fingers gently curved so the "
    "decorations catch the light; patterns or nail art across all five nails -> back of the hand toward the camera, "
    "fingers fanned out, all five nails facing the camera; one accent nail -> relaxed gesture with that finger "
    "slightly forward; short minimal nails -> natural relaxed gesture, e.g. fingertips resting on fabric or "
    "touching a product. Rules: one hand only, all five nails clearly visible and not hidden behind objects, no "
    "face, natural anatomy. pose_change: true if the hand should be re-posed, false if the current pose is "
    "already ideal. pose_ko: short Korean description of the chosen pose (and why). pose_en: precise English pose "
    "description for the image model (empty string if pose_change is false). Describe the pose in image_prompt_en "
    "too."
)
POSE_RULE_KEEP = ("Hand pose: keep the hand exactly in its current pose. Set pose_change=false, pose_en='' and "
                  "pose_ko='원래 자세 유지'.")
BANNER_REPOSE = (
    "Re-pose the hand into this pose: {pose}. Re-posing means redrawing the fingers, so you must carry every nail "
    "over exactly: each finger keeps its own nail design (thumb, index, middle, ring and little finger as in image "
    "1, including accent nails), with the same colors, patterns, decorations, finish, nail LENGTH and nail SHAPE - "
    "long or pointy nails stay just as long and pointy. Image 1 shows the hand, skin tone and exact nail art; "
    "image 2 is the original nail design reference - use it to keep every detail of the nail art correct. All "
    "five nails must be clearly visible. Same person's hand (skin tone, slenderness), natural anatomy, five "
    "fingers. "
)

BANNER_PROMPT = (
    "Create a polished, professional advertising web banner for a nail salon, {w}x{h} pixels ({orient}), using the "
    "hand in image 1 as the hero subject. Keep the hand, fingers, skin tone and especially every fingernail exactly "
    "as in image 1: the same nail colors, patterns, decorations, finish and nail shape - do not repaint, simplify or "
    "alter the nail art in any way. You may cut the hand out of its original background and move, scale or slightly "
    "rotate it to fit the composition, but it must stay photo-real with natural shadows that match the new scene. "
    "No extra hands or fingers, no rings or jewelry, no watermark, no logos." + NAIL_ART_KEEP_EN + " "
)


def parse_size(text, default=(1536, 1024)):
    """'1536x1024' -> (1536, 1024). 틀리면 기본값."""
    try:
        w, h = (int(v) for v in str(text).lower().replace(" ", "").split("x"))
        if 256 <= w <= 4096 and 256 <= h <= 4096:
            return w, h
    except Exception:
        pass
    return default


def orientation(w, h):
    return "landscape" if w > h * 1.1 else "portrait" if h > w * 1.1 else "square"


def hand_contact_sheet(hands):
    """손 사진 후보를 번호 붙인 한 장(4열)으로 -> 인식 모델이 한 번에 보고 골라요. (PIL 이미지 또는 None)"""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    if not hands:
        return None
    cw, ch, cols = 240, 300, 4
    rows = (len(hands) + cols - 1) // cols
    sheet = Image.new("RGB", (cw * min(cols, len(hands)), ch * rows), (255, 255, 255))
    try:
        font = ImageFont.load_default(size=40)
    except Exception:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(sheet)
    for i, h in enumerate(hands):
        im = ImageOps.exif_transpose(Image.open(os.path.join(HANDS, h["id"]))).convert("RGB")
        im = ImageOps.fit(im, (cw - 8, ch - 8), Image.LANCZOS)
        x, y = (i % cols) * cw, (i // cols) * ch
        sheet.paste(im, (x + 4, y + 4))
        d.ellipse((x + 10, y + 10, x + 66, y + 66), fill=(255, 255, 255), outline=(0, 0, 0), width=3)
        d.text((x + 38, y + 38), str(i + 1), fill=(0, 0, 0), font=font, anchor="mm")
    return sheet


# ---------------------------------------------------------------------------------------------- 장면 사진 (scene)
# nailhand/<분위기 이름(키워드,키워드)>/<사진 제목-번호>.png : 손이 음료·소품을 든 완성 사진. 마스크 없이 생으로 써요.
#   ① 인식 모델이 디자인 분위기에 맞는 폴더 -> 그 안의 사진 1장을 고르고, 음료 색 · 배경을 정해요 (같은 호출)
#   ② 이미지 모델이 그 사진의 손톱을 새 디자인으로 바꾸고, 음료 색과 배경도 바꿔요 (생성 1번)
def list_scenes():
    out = []
    if not os.path.isdir(NAILHAND):
        return out
    for d in sorted(os.listdir(NAILHAND)):
        dp = os.path.join(NAILHAND, d)
        if not os.path.isdir(dp):
            continue
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", d)
        cat, kws = (m.group(1).strip(), m.group(2).strip()) if m else (d.strip(), "")
        for p in sorted(glob.glob(os.path.join(dp, "*"))):
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in EXTS:
                n = os.path.basename(p)
                title = re.sub(r"\s*-\s*\d+$", "", os.path.splitext(n)[0]).strip()
                rel = d + "/" + n
                out.append({"id": rel, "label": f"{cat} · {title}", "url": "/nailhand/" + urllib.parse.quote(rel),
                            "category": cat, "keywords": kws, "title": title})
    return out


def scene_path(scene_id):
    """장면 사진 id('폴더/파일') -> 실제 경로. nailhand 밖이나 없는 파일이면 None."""
    p = os.path.realpath(os.path.join(NAILHAND, *str(scene_id or "").replace("\\", "/").split("/")))
    root = os.path.realpath(NAILHAND)
    if not p.startswith(root + os.sep) or not os.path.isfile(p) or os.path.splitext(p)[1].lower() not in EXTS:
        return None
    return p


# 레퍼런스(nailhand) 손 정보 파일 - 서버는 읽기만 하고 절대 쓰지 않아요. 틀리면 직접 고치고 서버를 다시 켜요.
#   hand_side.json     {"폴더/사진.png": "left" | "right"}        사진의 (주된) 손이 왼손인지 오른손인지
#   thumb_visible.json {"폴더/사진.png": true | false}            엄지 손톱이 보이는지
#   hand_fingers.json  {"폴더/사진.png": {"hands": [{"side", "fingers": [{"finger", "visible", "x", "y"}]}]}}
#                      손가락별 손톱 위치 (x, y = 사진 가로·세로 0~1). 첫 번째 손이 주된 손
#   nail_positions.json {"폴더/사진.png": {"thumb" ~ "little": {"visible", "tip": [x, y], "base": [x, y], "w"}}}
#                      팁 직접 붙이기용 손톱 끝(tip) · 손톱 뿌리(base) · 폭(w, 사진 가로 비율). 주된 손에만 써요
#   겹치면 hand_side.json · thumb_visible.json 이 이겨요 (주된 손에만 적용)
REF_SIDE_FILE, REF_THUMB_FILE, REF_FINGERS_FILE = "hand_side.json", "thumb_visible.json", "hand_fingers.json"
REF_POS_FILE = "nail_positions.json"


def _parse_box(b):
    """finger_point_editor.html 이 저장한 손톱 상자 -> {"corners": ((x, y) x4), "tip_round", "base_round", "persp"} 또는 None.
    corners 둘레 순서: 뿌리쪽1, 뿌리쪽2, 끝쪽2, 끝쪽1 (사진 비율 0~1). persp = 끝쪽 폭 / 뿌리쪽 폭 (원근)."""
    import math
    if not isinstance(b, dict):
        return None
    try:
        c = tuple((float(q[0]), float(q[1])) for q in b["corners"])
        if len(c) != 4:
            return None
        area = 0.5 * abs(sum(c[i][0] * c[(i + 1) % 4][1] - c[(i + 1) % 4][0] * c[i][1] for i in range(4)))
        if area < 1e-6:
            return None
        tr = min(1.0, max(0.0, float(b.get("tip_round", 0.8))))
        br = min(1.0, max(0.0, float(b.get("base_round", 0.5))))
        tw, bw = b.get("tip_w"), b.get("base_w")
        if not (isinstance(tw, (int, float)) and isinstance(bw, (int, float)) and bw > 0):
            tw, bw = math.dist(c[3], c[2]), math.dist(c[0], c[1]) or 1.0
        vis = b.get("visible", 1.0)
        vis = min(1.0, max(0.15, float(vis))) if isinstance(vis, (int, float)) else 1.0
        return {"corners": c, "tip_round": tr, "base_round": br, "persp": float(tw) / float(bw),
                "visible": vis, "hidden_side": 1 if b.get("hidden_side") == 1 else 2}
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _geo_entry(f, fallback=None):
    """nail_positions.json 한 손가락 (없으면 hand_fingers.json 의 같은 손가락) -> {"tip", "base", "w"[, "box"]} 또는 None.
    상자(box)는 nail_positions 쪽이 먼저, 없으면 hand_fingers 쪽. w 가 없으면 상자 폭(box.w)을 써요."""
    for src in (f, fallback):
        if not isinstance(src, dict) or src.get("visible") is False:
            continue
        try:
            g = {"tip": (float(src["tip"][0]), float(src["tip"][1])),
                 "base": (float(src["base"][0]), float(src["base"][1]))}
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        b = src.get("box") if isinstance(src.get("box"), dict) else (fallback or {}).get("box")
        w = src.get("w", b.get("w") if isinstance(b, dict) else None)
        try:
            g["w"] = float(w)
        except (TypeError, ValueError):
            continue
        box = _parse_box(b)
        if box:
            g["box"] = box
        return g
    return None


def _read_json_loose(path):
    """사람이 직접 고친 JSON 을 읽기만 해요. 빠진 쉼표 · 끝의 } 는 읽을 때만 보충 (파일은 그대로). 없거나 못 읽으면 {}."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        t = f.read().strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    fixed = re.sub(r'("|\d|\]|\}|true|false|null)(\s*\n\s*")', r"\1,\2", t)      # 줄 끝에 빠진 쉼표
    fixed += "\n}" * max(0, fixed.count("{") - fixed.count("}"))                  # 빠진 }
    try:
        d = json.loads(fixed)
        print(f"  ! {os.path.basename(path)}: JSON 문법 오류(빠진 쉼표나 괄호)를 보충해서 읽었어요 - 파일을 고쳐 주세요")
        return d
    except ValueError as e:
        print(f"  ! {os.path.basename(path)} 를 읽지 못해서 쓰지 않아요: {e}")
        return {}


# 참조용 손 사진 (거울 잡은 손): reference/ 폴더 + reference/nail_positions.json (읽기만)
#   레퍼런스 손이 왼손이면 banner.guide_left, 오른손이면 banner.guide_right 사진을 골라
#   손톱 자리 = reference/nail_positions.json (그 폴더의 hand_side · thumb_visible · hand_fingers.json 은 참고용)
#   ① 팁을 엄지~새끼 순서 · 방향대로 씌우고 (Image-2, 저렴한 품질) ② 결과 사진 빈 곳에 동그랗게 넣고
#   ③ 장면 생성 때 '낀 모습 예시'로 함께 보내요
GUIDE_DIR = os.path.join(ROOT, "reference")
GUIDE_POS_FILE = "nail_positions.json"       # reference/ 안 - nailhand 와 같은 형식 4개, 서버는 읽기만
GUIDE_SIDE_FILE, GUIDE_THUMB_FILE, GUIDE_FINGERS_FILE = "hand_side.json", "thumb_visible.json", "hand_fingers.json"
GUIDE_DEFAULT = {"left": "실버 콤팩트를 든 핑크 네일-1.png", "right": "은빛 거울을 든 누드 핑크 네일-2.png"}


def load_guide_hands(files=None, folder=GUIDE_DIR):
    """reference/ 의 hand_side · thumb_visible · nail_positions · hand_fingers.json 을 읽기만 해서
    -> {"left"|"right": {"path", "geo": {1~5: {"tip", "base", "w"}}}}.
    손 = hand_side.json (없으면 hand_fingers.json 의 side). 엄지 = thumb_visible.json 이 false 면 빼요.
    hand_fingers.json 에서 visible: false 인 손가락도 빼요. files {"left"/"right": 파일} 로 직접 고를 수도 있어요."""
    def rd(name):
        d = _read_json_loose(os.path.join(folder, name))
        return {str(k): v for k, v in d.items() if not str(k).startswith("_")} if isinstance(d, dict) else {}

    side_in, thumb_in, pos_in, fing_in = (rd(GUIDE_SIDE_FILE), rd(GUIDE_THUMB_FILE), rd(GUIDE_POS_FILE),
                                          rd(GUIDE_FINGERS_FILE))

    def find(d, name):
        if name in d:
            return d[name]
        return next((v for k, v in d.items() if os.path.splitext(k)[0] == os.path.splitext(name)[0]), None)

    names = list(dict.fromkeys(list(side_in) + list(pos_in) + list(fing_in)))
    photos = {}
    for name in names:
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            p = next((os.path.join(folder, f) for f in os.listdir(folder)
                      if os.path.splitext(f)[0] == os.path.splitext(name)[0] and os.path.splitext(f)[1].lower() in EXTS),
                     None)
            if not p:
                continue
        hf = find(fing_in, name)
        h0 = ((hf or {}).get("hands") or [{}])[0] if isinstance(hf, dict) else {}
        side = find(side_in, name) or h0.get("side")
        if side not in ("left", "right"):
            continue
        hidden = {f.get("finger") for f in (h0.get("fingers") or []) if isinstance(f, dict) and f.get("visible") is False}
        if find(thumb_in, name) is False:
            hidden.add("thumb")
        e = find(pos_in, name)
        hfs = {f.get("finger"): f for f in (h0.get("fingers") or []) if isinstance(f, dict)}
        geo = {}
        for n, key in enumerate(FINGER_KEYS, 1):
            if key in hidden:
                continue
            f = (e or {}).get(key) if isinstance(e, dict) else None
            f = f if isinstance(f, dict) and f.get("visible") else None
            ge = _geo_entry(f, hfs.get(key))
            if ge:
                geo[n] = ge
            elif f:
                print(f"  ! reference/{GUIDE_POS_FILE}: {name} {key} 값이 이상해서 쓰지 않아요")
        if geo:
            photos[os.path.basename(p)] = {"path": p, "geo": geo, "side": side}
    out = {}
    for side in ("left", "right"):
        want = str((files or {}).get(side) or "")
        g = photos.get(want) if want else None
        if want and not g:
            print(f"  ! 설정의 참조용 손({side}) {want} 를 reference 에서 찾지 못해서 hand_side.json 으로 골라요")
        g = g or next((v for v in photos.values() if v["side"] == side), None)
        if g:
            out[side] = {"path": g["path"], "geo": g["geo"]}
        else:
            print(f"  ! reference/{GUIDE_SIDE_FILE} 에 {'왼손' if side == 'left' else '오른손'} 사진이 없어요")
    return out


def _nail_box(img, geo, fingers, margin, square=False):
    W, H = img.size
    pts = [p for k in fingers if k in geo for p in geo_points(geo[k])]
    xs = [x * W for x, _ in pts]
    ys = [y * H for _, y in pts]
    if not xs:
        return img
    if square:
        side = min(min(W, H), int(max(max(xs) - min(xs), max(ys) - min(ys)) * (1 + 2 * margin)))
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        x0 = int(min(max(cx - side / 2, 0), W - side)); y0 = int(min(max(cy - side / 2, 0), H - side))
        return img.crop((x0, y0, x0 + side, y0 + side))
    mx, my = margin * W, margin * H
    return img.crop((int(max(0, min(xs) - mx)), int(max(0, min(ys) - my)),
                     int(min(W, max(xs) + mx)), int(min(H, max(ys) + my))))


def guide_paste(guide, tips, fingers, len_range=None, geo=None, soft=None):
    """참조용 손 사진에 팁을 그대로 붙인 사진 (Image-2 에 보낼 시작 그림 · 실패할 때 대신 쓰는 그림)."""
    from PIL import Image
    geo = geo or guide["geo"]
    ok = [(k, t) for k, t in zip(fingers, tips) if k in geo]
    img = Image.open(guide["path"]).convert("RGB")
    if not ok:
        return img
    return paste_tips(img, geo, [t for _, t in ok], [k for k, _ in ok], len_range=len_range, soft=soft)


GUIDE_INSET_SIZE = 0.31        # 동그란 삽입 지름 (결과 사진 가로에 대한 비율)


def add_guide_inset(img, inset, avoid=None, size=GUIDE_INSET_SIZE):
    """결과 사진 아래쪽 빈 곳(손톱을 가리지 않고 가장 밋밋한 자리)에 inset 을 동그랗게(크림색 테두리 + 그림자) 넣어요.
    avoid: 가리면 안 되는 점들 (사진 비율 0~1, 손톱 끝 · 뿌리)."""
    import numpy as np, cv2
    from PIL import Image, ImageDraw, ImageFilter
    img = img.convert("RGB")
    W, H = img.size
    D = int(W * size)
    ring = max(3, int(D * 0.022))
    m = int(W * 0.035)
    g = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY).astype(np.float32)
    mag = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    yy, xx = np.mgrid[0:H, 0:W]
    pts = [(x * W, y * H) for x, y in (avoid or [])]
    R = D / 2 + ring
    cands = []          # (선호도 벌점, 중심) - 아래쪽 먼저
    for fx, pref in ((1.0, 0.0), (0.0, 0.0), (0.5, 0.15)):
        cands.append((pref, (m + R + fx * (W - 2 * (m + R)), H - m - R)))
    for fy in (0.72, 0.6):
        for fx in (1.0, 0.0):
            cands.append((0.35 + (0.72 - fy), (m + R + fx * (W - 2 * (m + R)), fy * H)))
    best = None
    for pref, (cx, cy) in cands:
        hit = sum(1 for px, py in pts if (px - cx) ** 2 + (py - cy) ** 2 < (R * 1.12) ** 2)
        inside = (xx - cx) ** 2 + (yy - cy) ** 2 < R ** 2
        busy = float(mag[inside].mean()) / 255.0
        score = hit * 10 + busy + pref
        if best is None or score < best[0]:
            best = (score, cx, cy, hit)
    _, cx, cy, hit = best
    if hit:
        print(f"  ! 동그란 예시가 손톱 {hit}개를 조금 가려요 (빈 곳이 부족해요)")
    x0, y0 = int(cx - R), int(cy - R)
    S = 2 * int(R)
    # 그림자
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse((x0, y0 + ring, x0 + S, y0 + S + ring), fill=110)
    sh = sh.filter(ImageFilter.GaussianBlur(max(4, ring * 3)))
    out = Image.composite(Image.new("RGB", (W, H), (60, 50, 45)), img, sh)
    # 크림색 테두리 + 안쪽 사진 (4배로 그려 줄여서 가장자리를 매끈하게)
    k = 4
    big = Image.new("L", (S * k, S * k), 0)
    ImageDraw.Draw(big).ellipse((0, 0, S * k - 1, S * k - 1), fill=255)
    disc = big.resize((S, S), Image.LANCZOS)
    out.paste(Image.new("RGB", (S, S), (246, 240, 230)), (x0, y0), disc)
    d_in = S - 2 * ring
    big = Image.new("L", (d_in * k, d_in * k), 0)
    ImageDraw.Draw(big).ellipse((0, 0, d_in * k - 1, d_in * k - 1), fill=255)
    out.paste(inset.convert("RGB").resize((d_in, d_in), Image.LANCZOS), (x0 + ring, y0 + ring),
              big.resize((d_in, d_in), Image.LANCZOS))
    return out


def load_ref_hands(folder, scenes):
    """레퍼런스 손 정보 3개 파일을 읽어서 합쳐요 (읽기 전용, 인식 모델 호출 없음).
    돌려주는 값: (sides {사진 id: "left"|"right"}, fingers {사진 id: [{"side", "nails": {1~5: (x, y)}}]})
    nails 번호: 1=엄지, 2=검지, 3=중지, 4=약지, 5=새끼 - 팁 세트도 1번=엄지 ... 5번=새끼라서 같은 번호끼리 짝지어요."""
    ids = [s["id"] for s in scenes]
    idset = set(ids)
    by_stem = {os.path.splitext(i)[0]: i for i in ids}
    unknown = set()

    def norm(k):          # 키 이름 맞추기: 역슬래시 -> /, 확장자가 빠졌거나 달라도 같은 사진으로
        k = str(k).replace("\\", "/").strip()
        hit = k if k in idset else by_stem.get(k) or by_stem.get(os.path.splitext(k)[0])
        if not hit:
            unknown.add(k)
        return hit

    def read(name):
        raw = _read_json_loose(os.path.join(folder, name))
        if not isinstance(raw, dict):
            print(f"  ! {name}: {{ \"사진\": 값 }} 모양이 아니라서 쓰지 않아요")
            return {}
        return {n: v for n, v in ((norm(k), v) for k, v in raw.items() if not str(k).startswith("_")) if n}

    side_in = {k: v for k, v in read(REF_SIDE_FILE).items() if v in ("left", "right")}
    thumb_in = {k: v for k, v in read(REF_THUMB_FILE).items() if isinstance(v, bool)}
    pos_raw, fing_raw = read(REF_POS_FILE), read(REF_FINGERS_FILE)
    pos_in = {}                                   # 사진 -> {손가락 번호: {"tip", "base", "w"[, "box"]}} (보이는 손톱만)
    for k in list(dict.fromkeys(list(pos_raw) + list(fing_raw))):
        e, hf = pos_raw.get(k), fing_raw.get(k)
        h0 = ((hf or {}).get("hands") or [{}])[0] if isinstance(hf, dict) else {}
        hfs = {f.get("finger"): f for f in (h0.get("fingers") or []) if isinstance(f, dict)}
        g = {}
        for n, name in enumerate(FINGER_KEYS, 1):
            f = (e or {}).get(name) if isinstance(e, dict) else None
            f = f if isinstance(f, dict) and f.get("visible") else None
            ge = _geo_entry(f, hfs.get(name))
            if ge:
                g[n] = ge
            elif f:
                print(f"  ! {REF_POS_FILE}: {k} {name} 값이 이상해서 쓰지 않아요")
        pos_in[k] = g
    fingers, ko = {}, ("엄지", "검지", "중지", "약지", "새끼")
    for k, e in fing_raw.items():
        hands = []
        for i, h in enumerate((e.get("hands") or []) if isinstance(e, dict) else []):
            nails, geo = {}, {}
            for f in h.get("fingers") or []:
                if f.get("visible") and f.get("finger") in FINGER_KEYS:
                    try:
                        x, y = float(f["x"]), float(f["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    n = FINGER_KEYS.index(f["finger"]) + 1
                    nails[n] = (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
            side = h.get("side")                         # 손가락 = hand_fingers.json
            if i == 0:                                   # 주된 손: 손 = hand_side.json, 엄지 = thumb_visible.json,
                side = side_in.get(k, side)              #   손톱 끝 · 뿌리 방향 = nail_positions.json
                if thumb_in.get(k) is False:
                    nails.pop(1, None)
                geo = {n: g for n, g in pos_in.get(k, {}).items() if n in nails}
                miss = [ko[n - 1] for n in sorted(nails) if n not in geo]
                if miss:
                    print(f"  ! {k}: {REF_POS_FILE} 에 {' '.join(miss)} 손톱 끝 · 뿌리가 없어요 (그 손가락은 위치만 알려줘요)")
            if side in ("left", "right") and nails:
                hands.append({"side": side, "nails": nails, "geo": {n: g for n, g in geo.items() if n in nails}})
        if hands:
            fingers[k] = hands
    sides = dict(side_in)
    for k, hs in fingers.items():
        sides.setdefault(k, hs[0]["side"])
    if HANDS and os.path.isdir(HANDS):              # hands 폴더 손 사진의 왼손/오른손 (있으면, 역시 읽기만)
        for k, v in (_read_json_loose(os.path.join(HANDS, "hand_side.json")) or {}).items():
            if v in ("left", "right", "both"):
                sides.setdefault(str(k), v)
    print(f"레퍼런스 손 정보(읽기 전용): {REF_FINGERS_FILE} {len(fingers)}장 · {REF_SIDE_FILE} {len(side_in)}장 · "
          f"{REF_THUMB_FILE} {len(thumb_in)}장 / 장면 사진 {len(ids)}장")
    for k in ids:
        hs = fingers.get(k)
        if hs:
            print(f"  {k}: " + " / ".join(f"{'왼손' if h['side'] == 'left' else '오른손'} "
                                         + " ".join(ko[n - 1] for n in sorted(h["nails"])) for h in hs)
                  + (" · 팁 직접 붙이기 OK" if all(set(h["nails"]) <= set(h["geo"]) for h in hs[:1]) else
                     f" · ({REF_POS_FILE} 에 손톱 끝 · 뿌리가 없는 손가락이 있어서 팁은 이미지 모델이 그려요)"))
        else:
            print(f"  ! {k}: {REF_FINGERS_FILE} 에 손톱 위치가 없어요 -> "
                  + (f"{'왼손' if sides.get(k) == 'left' else '오른손'}으로만 알려 주고 손가락은 이미지 모델이 찾아요"
                     if k in sides else "손·손가락 모두 이미지 모델이 찾아요"))
    for k in sorted(unknown):
        print(f"  ! 파일에 있는데 nailhand 에 없는 사진이라 무시해요: {k}")
    return sides, fingers


def scene_contact_sheet(scenes):
    """장면 사진을 분위기(폴더)별 한 열씩, 번호를 붙여 한 장으로. (PIL 이미지 또는 None)"""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    if not scenes:
        return None
    cats = list(dict.fromkeys(s["category"] for s in scenes))
    cols = {c: [i for i, s in enumerate(scenes) if s["category"] == c] for c in cats}
    rows = max(len(v) for v in cols.values())
    cw, ch = 220, 280
    sheet = Image.new("RGB", (cw * len(cats), ch * rows), (255, 255, 255))
    try:
        font = ImageFont.load_default(size=36)
    except Exception:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(sheet)
    for c_i, c in enumerate(cats):
        for r_i, idx in enumerate(cols[c]):
            im = ImageOps.exif_transpose(Image.open(scene_path(scenes[idx]["id"]))).convert("RGB")
            im = ImageOps.fit(im, (cw - 8, ch - 8), Image.LANCZOS)
            x, y = c_i * cw, r_i * ch
            sheet.paste(im, (x + 4, y + 4))
            d.ellipse((x + 10, y + 10, x + 62, y + 62), fill=(255, 255, 255), outline=(0, 0, 0), width=3)
            d.text((x + 36, y + 36), str(idx + 1), fill=(0, 0, 0), font=font, anchor="mm")
    return sheet


SCENE_CHOICE_INSTRUCTIONS = (
    " Image 2 is a contact sheet of {n} numbered lifestyle photos (1 to {n}): a woman's hand with manicured nails "
    "holding a drink or an object. Each column is one mood category: {cats}. Photo numbers: {labels}. "
    "Step 1 - choose the category whose mood best matches THIS nail design (its colors, finish and feeling) using "
    "the category names and keywords (e.g. deep red/burgundy, sexy -> red wine; gold, glitter, glamorous -> "
    "sparkling wine; brown, beige, cozy, autumn/winter -> mug; dark brown/black, heavy, classic -> whisky; bright, "
    "fresh, summer, blue/green/neon -> cocktail; purple, pastel, other moods -> perfume). "
    "Step 2 - inside that category choose the ONE photo whose hand pose, nail length and shape, number of visible "
    "nails and composition would show this design best (long pointy design -> a photo with long pointed nails; "
    "art across all nails -> a photo where all five nails are clearly visible). hand_index = that photo's number. "
    "hand_reason_ko: one short Korean reason that names the category (max 50 characters). "
    "Step 3 - restyle that photo for this nail design: scene_drink_en = the new color and look of the drink / "
    "liquid / perfume in the chosen photo, harmonizing with the nail colors (keep the same kind of drink and "
    "vessel; it may become a variant, e.g. rose wine, a blue cocktail, a matcha latte), 15-40 English words. "
    "scene_background_en = the new background (colors, lighting, bokeh, setting and small props) that matches the "
    "nail design's mood and makes the nails stand out, staying believable for that drink, 25-60 English words. "
    "scene_ko = one short Korean sentence summarizing the new drink color and background (max 60 characters). "
    "Image 2 is never the design - analyze the design only from image 1."
)
SCENE_FIELDS = {"scene_drink_en": {"type": "string"}, "scene_background_en": {"type": "string"},
                "scene_ko": {"type": "string"}}
SCENE_ANALYSIS_SCHEMA = {**ANALYSIS_SCHEMA, "required": ANALYSIS_SCHEMA["required"] + list(SCENE_FIELDS),
                         "properties": {**ANALYSIS_SCHEMA["properties"], **SCENE_FIELDS}}
SCENE_PROMPT = (
    "Image 1 is a lifestyle photo of a woman's hand holding {obj}. Image 2 is a nail art design reference. "
    "Edit image 1 in three ways and keep everything else. "
    "(1) NAILS: completely replace the old nail polish on every visible fingernail with the nail design from image "
    "2 - the same base colors, patterns, decorations, finish and accent nails, adapted to each nail's angle, "
    "curvature and the photo's lighting, with realistic gloss and clean cuticle lines. No trace of the old nail "
    "color may remain. Copy the nail design EXACTLY as it is in image 2 - do not change, simplify, restyle, "
    "recolor, add to or invent any part of it, and never shift its colors toward the new drink or background "
    "(the drink and background are changed to suit the nails, never the other way round)." + CLEAR_PAINT_EN + " {nails} "
    "(2) DRINK: change the color of the {drink_word} to: {drink}. Keep the same glass/cup/bottle shape, liquid "
    "level, garnish position, bubbles, foam, reflections and refraction - only its color and look change. "
    "(3) BACKGROUND: replace the background with: {bg}. Keep a similar camera angle and shallow depth of field so "
    "the hand and drink stay sharp and the nails are the focal point. "
    "Keep the hand, finger positions, pose, skin tone, rings and bracelets, the held object and the framing exactly "
    "as in image 1. Photo-real, premium advertising photo. Absolutely no text, letters, brand or shop names, "
    "banners, captions, watermarks or logos anywhere in the image." + NAIL_ART_KEEP_EN + " No extra fingers."
)
OWN_SHAPE_TIP_EN = (
    "Each nail takes the outline, free-edge shape and length of its OWN tip image (the whole tip from its top end to "
    "its bottom end becomes the nail from the fingertip to the cuticle), so the WHOLE design is visible along the full "
    "nail; nails stay fully inside the picture.")
OWN_SHAPE_WORN_EN = (
    "Each nail takes the outline, free-edge shape and length of the SAME finger's nail in image 2, so the WHOLE design "
    "is visible along the full nail; nails stay fully inside the picture.")
# 팁 세트 장면: Image-2 는 배경 · 음료만. 손 · 손톱은 손대지 않아요 (팁은 나중에 프로그램이 그대로 붙여요)
SCENE_BG_ONLY_PROMPT = (
    "Image 1 is a lifestyle photo of a woman's hand holding {obj}. Edit ONLY these two things and keep everything else "
    "pixel-identical: {drink_part}(2) BACKGROUND: replace the background with: {bg}. Keep a similar camera angle and "
    "shallow depth of field so the hand and drink stay sharp. DO NOT touch the hand at all: the hand, every finger, "
    "every fingernail (including its current polish), the pose, skin, rings, bracelets, the held object and the "
    "framing must stay EXACTLY as in image 1 - same position, size, angle and shape; do not move, resize, redraw, "
    "repaint or restyle any nail. Photo-real, premium advertising photo. Absolutely no text, letters, logos, "
    "captions or watermarks anywhere in the image. No extra fingers."
)
# 팁 세트 장면: 프로그램이 팁을 레퍼런스 손톱 자리에 대략 얹어 둔 사진 -> Image-2 가 레퍼런스 손톱에 자연스럽게 녹여요
SET_ORIGINAL_REF = True       # Image-2 에 올린 팁 세트 원본 사진도 보내서, 잘라낸 팁 대신 원본을 보고 손톱을 다듬어 그리게 해요
TIP_ROUGH_ALPHA = 0.6         # 모델이 다시 그릴 사진에 팁을 얹는 진하기 (1 = 그대로, 낮을수록 '어느 손가락에 어떤 팁' 힌트만)
ORIGINAL_SET_EN = (
    " ORIGINAL TIP SET - THE TRUE SOURCE: image {n} is the ORIGINAL photo of this press-on tip set as the customer "
    "uploaded it, with the tips side by side from left to right = 1 thumb, 2 index, 3 middle, 4 ring, 5 little finger "
    "(free edge at the top). The program's cut-outs (laid FAINTLY in image 1 and shown one by one) were cut "
    "automatically and can be WRONG: cropped or clipped edges, wrong outline, missing or squashed parts, stretched or "
    "tilted design, background or neighbouring tips mixed in. Use the cut-outs ONLY to know which finger gets which "
    "tip. For every nail, look at that tip in the ORIGINAL photo and take its real design from there - the complete "
    "design with its true proportions, colors, translucency, glitter, patterns, characters and 3D charms (bows, "
    "rhinestones, pearls, raised figures) intact and in the same places from cuticle to free edge, and the tip's own "
    "nail shape and length. Then refine it so it sits naturally on this hand's own nail: correct perspective, "
    "curvature and lighting, clean outline, no cut-out edges, seams, halos or background bits. Do not copy the "
    "mistakes of the cut-outs."
)


def orig_ratio(settings):
    """settings.yaml banner.original_ratio (0~100) -> 0~1. 원본 팁 세트 사진 vs 잘라낸 팁 중 원본을 얼마나 따를지."""
    try:
        v = float((settings or {}).get("banner", {}).get("original_ratio", 70))
    except (TypeError, ValueError):
        v = 70.0
    return max(0.0, min(100.0, v)) / 100.0 if SET_ORIGINAL_REF else 0.0


ORIGINAL_MID_EN = (
    " ORIGINAL TIP SET: image {n} is the ORIGINAL photo of this press-on tip set as the customer uploaded it, with the "
    "tips side by side from left to right = 1 thumb, 2 index, 3 middle, 4 ring, 5 little finger (free edge at the "
    "top). Use BOTH sources: the program's cut-outs (laid in image 1 and shown one by one) for which finger gets which "
    "tip and how each design is laid out along the nail, and the ORIGINAL photo to see the true colors, translucency, "
    "glitter and 3D charms and to correct anything the automatic cut-outs got wrong (clipped edges, wrong outline, "
    "missing or squashed parts, tilted design, background or neighbouring tips mixed in). Then refine each nail so it "
    "sits naturally on this hand's own nail, with no cut-out edges, seams, halos or background bits."
)
ORIGINAL_LOW_EN = (
    " ORIGINAL TIP SET: image {n} is the ORIGINAL photo of this press-on tip set (left to right = 1 thumb ... 5 little "
    "finger, free edge at the top). The program's cut-outs are the main source of each design; look at the original "
    "photo only to fix obvious cutting mistakes (clipped edges, missing parts, background bits) and to check charms "
    "and colors."
)


def original_set_text(n, ratio):
    """원본 비중(0~1)에 맞는 프롬프트 문장 + 비중 안내. 0 이면 ''."""
    if ratio <= 0:
        return ""
    body = (ORIGINAL_SET_EN if ratio >= 0.67 else ORIGINAL_MID_EN if ratio >= 0.34 else ORIGINAL_LOW_EN).format(n=n)
    pct = int(round(ratio * 100))
    return body + (f" WEIGHTING: rely about {pct}% on the original photo (image {n}) and {100 - pct}% on the program's "
                   f"cut-outs for each nail's appearance.")


def rough_soft(fingers, ratio=None):
    """모델이 다시 그릴 사진에 팁을 얹을 때 손가락마다 진하기 -> {손가락: 진하기} (엄지는 더 흐리게).
    ratio(원본 비중 0~1)가 클수록 잘라낸 팁을 흐리게 얹어요: 0 -> 그대로(1.0), 100% -> 0.3. 원본을 안 보내면 엄지만."""
    r = 0.43 if ratio is None else ratio
    if not SET_ORIGINAL_REF or r <= 0:
        return thumb_soft(fingers)
    a = max(0.3, min(1.0, 1.0 - 0.7 * r))
    sf = {k: a for k in (fingers or [])}
    for k, v in (thumb_soft(fingers) or {}).items():
        sf[k] = min(sf.get(k, 1.0), v)
    return sf or None


def original_set_image(design, ratio=1.0):
    """Image-2 에 보낼 팁 세트 원본 사진 (긴 쪽 1536px 까지) -> (파일 이름, PNG 바이트, 형식) 또는 None (원본 비중 0 이면 안 보내요)."""
    if design is None or not SET_ORIGINAL_REF or ratio <= 0:
        return None
    from PIL import Image
    d = design.convert("RGB").copy()
    d.thumbnail((1536, 1536), Image.LANCZOS)
    return ("original_tip_set.png", png_bytes(d), "image/png")


SCENE_STRICT_EN = "never redesign, simplify or recolor it, and "     # 장면 프롬프트의 엄격한 문장 (자연스러움 높으면 빼요)
SCENE_NATURAL_MID_EN = (
    " NATURAL FIT (balanced): keep every design clearly recognizable - the same base color, finish, main motifs, "
    "charms and where they sit along the nail - but do NOT copy the laid cut-outs pixel by pixel. Re-draw each nail as "
    "a real press-on worn on this hand: its outline, curvature, side walls and perspective follow the hand's own nail "
    "and this camera angle, with the photo's light, gloss, shading and a soft contact shadow at the skin. Small "
    "details may be redrawn slightly so they wrap naturally around the curved nail. No hard cut lines, flat "
    "rectangles, halos, floating parts or mismatched lighting."
)
SCENE_NATURAL_HIGH_EN = (
    " PRIORITY - NATURAL LOOK FIRST: the most important thing is that the nails look like a real, freshly done "
    "manicure on this hand in this photo - never like stickers or cut-outs. Keep each nail's design recognizable (the "
    "same base color, finish, main motifs and where they sit along the nail), but tiny details may be softened or "
    "simplified when that makes the nail look more natural. Give every nail a real nail shape that grows from the "
    "cuticle and sits flush on the finger, smooth side walls, natural curvature and correct perspective for this "
    "view, a glossy gel surface with highlights and shading from the photo's light, and a soft contact shadow where "
    "it meets the skin. Blend all edges - no hard cut lines, flat rectangles, floating parts, halos or mismatched "
    "lighting."
)


def natural_level(settings):
    """settings.yaml banner.natural_level (0~100) -> 0~1. 레퍼런스 장면 사진에 네일을 얼마나 자연스럽게(vs 정확히 복사) 입힐지."""
    try:
        v = float((settings or {}).get("banner", {}).get("natural_level", 50))
    except (TypeError, ValueError):
        v = 50.0
    return max(0.0, min(100.0, v)) / 100.0


def scene_natural_prompt(prompt, level):
    """장면 프롬프트를 자연스러움 레벨에 맞게: 0~33 = 그대로(정확히 복사), 34~66 = 균형, 67~100 = 자연스러움 최우선
    (엄격한 '디자인을 단순화 · 변경 금지' 문장을 빼요)."""
    if level < 0.34:
        return prompt
    if level >= 0.67:
        prompt = prompt.replace(SCENE_STRICT_EN, "").replace(NAIL_ART_KEEP_EN, "")
        return prompt + SCENE_NATURAL_HIGH_EN
    return prompt + SCENE_NATURAL_MID_EN


def scene_soft(fingers, settings, with_original=True):
    """장면 사진에 팁을 얹는 진하기: 원본 비중(original_ratio)으로 정한 진하기 x 자연스러움(natural_level) -> {손가락: 진하기}.
    자연스러움 0 = 그대로, 50 = 0.75배, 100 = 0.5배 (최소 0.2). 엄지는 더 흐린 쪽."""
    base = (rough_soft(fingers, orig_ratio(settings)) if with_original else thumb_soft(fingers)) or {}
    f = 1.0 - 0.5 * natural_level(settings)
    out = {k: max(0.2, base.get(k, 1.0) * f) for k in (fingers or [])}
    return {k: v for k, v in out.items() if v < 0.999} or None


SCENE_REFINE_PROMPT = (
    "Image 1 is a lifestyle photo of a woman's hand holding {obj}. Press-on nail tips have been roughly laid on its "
    "fingernails as flat cut-outs by a program, one per finger in the right order and direction (the top of each tip "
    "at the fingertip): this placement only tells you WHICH design goes on WHICH nail and which way up. The tip "
    "designs are also shown one by one: {names}. Edit image 1 in three ways and keep everything else. "
    "(1) NAILS: re-render every one of these nails so the design looks naturally painted on / worn on the hand's OWN "
    "fingernail, not pasted: follow the real nail's outline, free edge, curvature, angle and perspective, match the "
    "photo's lighting, shadows, reflections and gloss, give clean cuticle and side walls, and remove every cut-out "
    "edge, rectangle, halo, seam, background patch from the tip photo and any trace of the old polish. Use the "
    "one-by-one tip images only as the source of each design (ignore their backgrounds): the same colors, "
    "translucency, patterns, decorations, 3D charms, characters and letters at the same places along the nail "
    "(fingertip part stays at the fingertip, cuticle part at the cuticle), the same finish. Every finger keeps exactly "
    "the design that is on it in image 1 - never move, swap, mirror or rotate a design, " + SCENE_STRICT_EN +
    "never shift its colors toward the new drink or background." + CLEAR_PAINT_EN + " "
    "{drink_part}(3) BACKGROUND: replace the background with: {bg}. Keep a similar camera angle and shallow depth of "
    "field so the hand and drink stay sharp and the nails are the focal point. "
    "Keep the hand, finger positions, pose, skin tone, rings and bracelets, the held object and the framing exactly "
    "as in image 1. Photo-real, premium advertising photo. Absolutely no text, letters, logos, captions or "
    "watermarks anywhere in the image." + NAIL_ART_KEEP_EN + " No extra fingers."
)
SCENE_DRINK_KEEP = ("(2) DRINK: keep the {drink_word} exactly as in image 1 - the same color, glass, liquid level, "
                    "garnish, bubbles, foam and reflections. ")
# 팁 비율대로 손톱 길이를 바꿨을 때 (TIP_KEEP_LENGTH) 장면 · 동그란 사진 프롬프트에서 바꿔 끼우는 문장
OUTLINE_FOLLOW_EN = "follow the real nail's outline, free edge, curvature, angle and perspective"
OUTLINE_KEEP_LEN_EN = ("keep each nail's LENGTH and free-edge shape exactly as laid in image 1 (they come from its "
                       "press-on tip) while following the real nail's cuticle, side walls, curvature, angle and perspective")
TIP_LENGTH_EN = (
    " NAIL LENGTH: the laid tips already have their final length. Where a tip is shorter than the hand's original nail, "
    "the rest of the original nail is gone - show natural bare fingertip skin there (no old polish, no gap, no grey, "
    "white or silver band). Where a tip reaches past the original nail, it extends beyond the fingertip like a long "
    "press-on nail: repaint whatever is behind it (skin, glass, drink, background) so it sits naturally in the scene."
)
# 참조용 손(거울 잡은 손)에 팁 씌우기 - Image-2 저렴한 품질. 팁은 프로그램이 이미 손톱 자리에 순서 · 방향대로 붙여 둠
WORN_GUIDE_PROMPT = (
    "Image 1 is a close-up photo of a woman's hand holding a round silver compact mirror. Press-on nail tips "
    "have been roughly laid on its nails as flat cut-outs by a program, one per finger in the right order and "
    "direction (thumb, index, middle, ring, little finger; the top of each tip at the fingertip): this placement only "
    "tells you WHICH design goes on WHICH nail and which way up. The same tips are shown one by one: {names}. "
    "Re-render every one of these nails so the design looks naturally painted on / worn on the hand's OWN fingernail, "
    "not pasted: follow the real nail's outline, free edge, curvature, angle and perspective, match the photo's soft "
    "lighting, shadows, reflections and gloss, give clean cuticle and side walls, and remove every cut-out edge, "
    "rectangle, halo, seam, whitened or background patch from the tip photos and any leftover old pink polish. Use "
    "the one-by-one tip images only as the source of each design (ignore their whitened backgrounds). Every finger "
    "keeps exactly the design that is on it now, in the same direction - never move, swap, mirror or rotate a design. "
    "Do not change the designs at all: the same colors, translucency, patterns, decorations, charms, characters, "
    "letters, positions along the nail and sizes, the same finish." + CLEAR_PAINT_EN + " "
    "Keep the hand, pose, skin, mirror, sweater, background and framing exactly as in image 1. "
    "No text, labels or watermarks. The tip designs are licensed originals: reproduce them exactly, never alter them."
)
# 동그란 사진(참조용 손)은 작게 들어가요 -> 디자인의 잔 디테일보다 '진짜 손에 한 네일처럼 자연스러움'이 먼저
GUIDE_NATURAL = True
GUIDE_STRICT_EN = (
    "Do not change the designs at all: the same colors, translucency, patterns, decorations, charms, characters, "
    "letters, positions along the nail and sizes, the same finish.",
    "The tip designs are licensed originals: reproduce them exactly, never alter them.",
)
GUIDE_NATURAL_EN = (
    " PRIORITY FOR THIS PHOTO: it will be shown small, so the most important thing is that the nails look like a "
    "real, freshly done manicure on this hand - not like stickers or cut-outs. Keep each nail's design recognizable "
    "(the same base color, finish, main motifs and where they sit along the nail), but tiny details may be softened "
    "or simplified when that makes the nail look more natural. Give every nail a real nail shape that grows from "
    "the cuticle and sits flush on the finger, smooth side walls, natural curvature, a glossy gel surface with "
    "highlights and shading from the photo's light, and a soft contact shadow where it meets the skin. Blend all "
    "edges - no hard cut lines, flat rectangles, floating parts, halos or mismatched lighting."
)
SCENE_OBJ = {"레드와인": ("a glass of red wine", "wine"), "스파클링": ("a flute of sparkling wine", "sparkling wine"),
             "머그": ("a cup of coffee latte", "coffee/latte"), "위스키": ("a glass of whisky", "whisky"),
             "칵테일": ("a cocktail glass", "cocktail"), "향수": ("a perfume bottle", "perfume liquid (and the glass bottle tint)")}


def scene_object(category):
    for k, v in SCENE_OBJ.items():
        if k in (category or ""):
            return v
    return ("a drink or an object", "drink or the liquid in the object")


# ---------------------------------------------------------------------------------------------- 팁 모음 (세트 나누기)
# 사진 1장에 네일 팁 세트(5개씩)가 여러 줄 -> ⓪ 인식 모델이 세트마다 팁 5개의 위치를 찾고 -> 세트마다 잘라 1장씩 만들어요
SPLIT_INSTRUCTIONS = (
    "The image shows press-on nail tips arranged in sets: one set = the tips for one hand (usually 5 tips in a "
    "row, thumb to little finger); rows may be diagonal and there may be several sets side by side in one row. "
    "Sets can look very different: straight rows, diagonal rows at any angle, tips that overlap each other like "
    "fanned cards, 4-6 tips per row, on a card, a holder, fabric or a plain backdrop, inside a styled poster or "
    "product photo with text. A row is any line of 4-6 neighbouring tips that touch or overlap. If 8 or more "
    "separate nail tips (not on a hand) are visible, this IS a tip sheet - find the rows and never answer "
    "is_tip_sheet=false. If a row has 6 tips, still return it with all 6 tips. "
    "Find every set. For each set list every tip with its CENTER (x, y), the point at the very end of its FREE "
    "EDGE (ex, ey): the outer end of the nail that sits at the fingertip, i.e. the end pointing the way the finger "
    "extends, and the middle of its CUTICLE BASE (bx, by): the opposite end that sits at the cuticle. The line from "
    "(bx, by) through (x, y) to (ex, ey) is the tip's length axis, cuticle -> fingertip. RULE: for a tip that stands "
    "upright in the photo (its length runs roughly up-down), the free edge is its TOP end (nearer the top of "
    "the image) and the cuticle base is its BOTTOM end - EXCEPT a clearly POINTED tip (stiletto, almond): its sharp, "
    "narrow point is ALWAYS the free edge, even when it points down. For a tip lying sideways, the "
    "free edge is the end where it is narrowest. All coordinates "
    "are normalized 0-1 of the whole image (x right, y down). List the tips in order along the row; the program "
    "orders them by finger itself. " + CLEAR_SEE_EN + " A tip's extent always includes its clear "
    "parts (e.g. a clear free edge with a bow on it), so the CENTER and tip_length must cover the whole tip from "
    "its very end to its base, clear parts included. tip_length = the average length of one tip (normalized to image "
    "height), complete = false only if a tip of the set is cut off by the image edge so badly that its design cannot be "
    "seen, or the set has fewer than 4 visible tips (slight overlap with neighbouring tips is fine). "
    "pointed = true if the tips of this set are clearly POINTED (stiletto or almond: they narrow to a sharp or "
    "soft point at one end); then (ex, ey) of every tip of the set MUST be at that narrow point, even when the "
    "points face DOWN in the photo (sets are often laid with the points down). "
    "shape = the free-edge shape shared by the tips of this set: 'pointed' (stiletto, almond - narrows to a point), "
    "'square' (straight sides, flat free edge with sharp or slightly rounded corners, incl. squoval), "
    "'coffin' (sides taper toward a flat, narrower free edge - ballerina), 'round' (round or oval free edge). "
    "length = 'short' (tip about as long as it is wide or shorter), 'long' (more than about twice as long as wide), "
    "otherwise 'medium'. "
    "summary_ko: short Korean description of the set (max 30 characters). Ignore text, logos, inset photos of "
    "hands, and stray single tips. Order the sets top to bottom, then left to right. If the image is not a "
    "collection of nail tip sets (e.g. one hand, one design), set is_tip_sheet=false and sets=[]."
)
_PT = {"type": "object", "additionalProperties": False, "required": ["x", "y", "ex", "ey", "bx", "by"],
       "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                      "ex": {"type": "number"}, "ey": {"type": "number"},
                      "bx": {"type": "number"}, "by": {"type": "number"}}}
SPLIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["is_tip_sheet", "sets"],
    "properties": {"is_tip_sheet": {"type": "boolean"},
                   "sets": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["tips", "tip_length", "complete", "pointed", "shape", "length", "summary_ko"],
                       "properties": {"tips": {"type": "array", "items": _PT}, "tip_length": {"type": "number"},
                                      "complete": {"type": "boolean"}, "pointed": {"type": "boolean"},
                                      "shape": {"type": "string", "enum": ["pointed", "square", "coffin", "round"]},
                                      "length": {"type": "string", "enum": ["short", "medium", "long"]},
                                      "summary_ko": {"type": "string"}}}}},
}

# 세트 하나(세로로 세워 자른 사진)를 luna 가 보고: 손톱 끝이 위/아래 · 팁 모양 · 팁마다 위치와 크기 · 기준 팁
SET_INSPECT_INSTRUCTIONS = (
    "The image shows ONE set of press-on nail tips (one hand), already rotated so that every tip stands upright: "
    "its length axis runs up-down and the tips sit side by side in a row from left to right. "
    "1) free_edge_at_top: true if the FREE EDGE of the tips (the outer end that sits past the fingertip) is at the TOP "
    "of the image and the CUTICLE end at the bottom; false if the free edges point DOWN. Clues: a pointed or "
    "narrower end is the free edge (stiletto, almond, coffin); a flat square end is the free edge of square tips; "
    "the cuticle end is a smooth rounded C-curve; clear/transparent ends are usually the free edge; characters, "
    "text and charms on the tips read upright when the free edge is at the top. Decide for the whole set "
    "(all tips of a set lie the same way). "
    "2) shape = shared free-edge shape: 'pointed' (stiletto, almond), 'square' (incl. squoval), 'coffin' (ballerina), "
    "'round' (round, oval). length = 'short' (about as long as wide or shorter), 'long' (more than about twice as "
    "long as wide), else 'medium'. "
    "3) tips: every tip of the set from LEFT to RIGHT. For each: x = horizontal center, top = y of its upper end, "
    "bottom = y of its lower end (cover the whole tip including clear parts), w = its width at the widest point. "
    "All values normalized 0-1: x and w of the image width, top and bottom of the image height. Ignore neighbouring "
    "sets, text and props that are only partly visible at the image border. "
    "4) reference = 1-based number (left to right) of the ONE tip with the cleanest, most typical outline: fully "
    "visible, not overlapped, no 3D parts or charms sticking out past its edge (prefer middle or ring finger). "
    "5) ref_sample: if a second image with numbered nail OUTLINE SAMPLES is given, the number (1-based, left to right) of the ONE sample whose outline shape is closest to the tips of this set (compare only the outline: pointed/almond/oval/square/coffin, how the free edge and sides narrow; ignore color, size and which way it points); 0 if no samples are given. "
    "note_ko: very short Korean reason for the free-edge decision (max 30 characters)."
)
SET_INSPECT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["free_edge_at_top", "shape", "length", "tips", "reference", "ref_sample", "note_ko"],
    "properties": {
        "free_edge_at_top": {"type": "boolean"},
        "shape": {"type": "string", "enum": ["pointed", "square", "coffin", "round"]},
        "length": {"type": "string", "enum": ["short", "medium", "long"]},
        "tips": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["x", "top", "bottom", "w"],
            "properties": {"x": {"type": "number"}, "top": {"type": "number"}, "bottom": {"type": "number"},
                           "w": {"type": "number"}}}},
        "reference": {"type": "integer"},
        "ref_sample": {"type": "integer"},
        "note_ko": {"type": "string"}},
}
SET_UPRIGHT = True        # 세트마다: 팁 축이 세로가 되게 돌려 자르고 -> luna 가 손톱 끝 위/아래 · 모양 · 크기 확인 -> 아래면 상하 거울 뒤집기
SET_ROTATE_MIN_DEG = 2.0  # 이보다 덜 기울었으면 돌리지 않아요
SET_CROP_ROTATE = False   # 세트마다 따로 돌리기 (끔: 올린 원본 사진을 통째로 한 번 돌린 뒤, 그 사진에서 세트를 그대로 잘라요)


def set_axis_turn(ends, bases):
    """팁마다 손톱 끝 · 뿌리 점 -> 팁 축이 세로가 되게 돌릴 각도 (rotate_keep 의 deg, -90~90) 또는 None.
    축은 방향이 없어서(위/아래는 나중에 luna 가) 2배 각도로 평균해요."""
    import math
    s = c = 0.0
    n = 0
    for e, b in zip(ends or [], bases or []):
        if not e or not b:
            continue
        vx, vy = e[0] - b[0], e[1] - b[1]
        if math.hypot(vx, vy) < 1:
            continue
        a = math.atan2(-vx, vy)                       # 이 각도만큼 돌리면 (vx, vy) 의 가로 성분이 0
        s += math.sin(2 * a); c += math.cos(2 * a); n += 1
    if not n or math.hypot(s, c) < 0.3 * n:          # 팁 축이 제각각이면 돌리지 않아요
        return None
    return math.degrees(math.atan2(s, c) / 2)


# 팁 방향 규칙 (고정): 팁 하나에서 긴 쪽 = 손톱의 위아래(길이) 축, 그 긴 축의 위쪽 끝 = 손톱 바깥쪽(끝, free edge)
TIP_ORIENT_EN = (
    "Orientation rule for every tip: the tips stand upright in the photo - the TOP end of each tip (nearer the top of "
    "the image) is its FREE EDGE (toward the fingertip, the way the finger extends) and its BOTTOM end is the "
    "cuticle/base, whatever their widths. Never turn a tip upside down."
)
# 잘라 보낸 팁 사진을 손톱에 옮기는 규칙: 팁 사진은 프로그램이 이미 세워 둠 (긴 쪽 = 세로, 세트 사진의 위쪽 끝 = 위)
#   디자인은 그대로 옮기기만 - 다시 그리기 · 색 바꾸기 · 단순화 · 추가 금지
TIP_TRANSFER_EN = (
    "THE TIP IMAGES ARE THE EXACT DESIGNS - TRANSFER THEM, DO NOT REDESIGN THEM. Every tip image has already been "
    "turned upright by the program: its LONG side runs along the nail's length (fingertip <-> cuticle), its TOP "
    "end (the end that was at the top of the customer's set photo) is the FREE EDGE at the fingertip and its "
    "BOTTOM end is the CUTICLE - whichever end is wider. "
    "Lay each tip image onto its nail like a press-on nail: the tip's long axis along the nail's length, its top at "
    "the fingertip and its bottom at the cuticle - never upside down, sideways or mirrored. Only fit it to the nail: "
    "scale it to the nail size, follow the nail's angle, perspective and curvature, and match the photo's lighting "
    "and gloss. Everything else stays exactly as in the tip image: the same colors and translucency (no recoloring, "
    "no color shift toward the drink or background), the same pattern, the same decorations (3D charms, bows, "
    "beads, stickers, dots, stars, lines) at the same positions along the nail and in the same sizes and "
    "proportions, the same finish, and the same clear parts. Do not add, remove, simplify, restyle, blend or "
    "reinterpret anything, and never move a detail to another place on the nail or to another nail. Ignore only "
    "the background around each tip (and any parts of neighbouring tips at the edges of a tip image)."
)
TIPSET_INSTRUCTIONS = (
    " This photo is ONE set of press-on nail tips cut out of a larger sheet: the sharp, non-faded row in the "
    "middle (the faded, whitened surroundings are other sets - ignore them). tips_en: describe EACH tip of the sharp row "
    "separately, in order from the LEFTMOST tip to the RIGHTMOST tip as seen in the photo (for a vertical row: top "
    "to bottom), one entry per tip (usually 5), each 8-30 English words with its colors, pattern, decorations and "
    "finish, so a painter can reproduce every tip exactly. " + TIP_ORIENT_EN + " Describe every tip from its "
    "free edge to its cuticle: say what is at the free edge and what is at the cuticle, e.g. 'free edge: gold "
    "glitter; cuticle half: nude'. " + CLEAR_SEE_EN + " For EVERY tip state its clear part explicitly with its "
    "position and rough share of the length, e.g. 'free edge 40%: clear transparent with a brown 3D bow at the "
    "boundary; cuticle 60%: milky pink with brown dots' - or write 'no clear part'. Do not name fingers. "
    "design_en = the common style of the "
    "set in a few sentences; accent_nails = ''. nail_shape and nail_length describe the tips. best_region = the "
    "whole sharp row."
)
# luna 에 팁을 하나씩 (둘레 하얗게) 보여줄 때 - 엄지 · 검지 · 중지 · 약지 · 새끼를 팁마다 따로 인식하게
TIP_ONE_BY_ONE_EN = (
    " The final image shows the same tips cut out ONE BY ONE, in the same left-to-right order as the row (labelled "
    "1 to 5): in each cut-out only the tip in the middle is sharp and its surroundings (neighbouring tips, the "
    "sheet) are whitened - ignore the whitened parts. Use these cut-outs to tell the tips apart: tips_en[0] "
    "describes cut-out 1 only, tips_en[1] cut-out 2 only, and so on - one entry per cut-out, never mixing details "
    "of neighbouring tips. In each cut-out the TOP is the free edge and the BOTTOM is the cuticle."
)
# 세트 규칙 (통일): 한 줄의 팁을 왼쪽부터 1~5 번이라 하면
#   오른손: 1번 = 엄지, 2번 = 검지, 3번 = 중지, 4번 = 약지, 5번 = 새끼
#   왼손  : 똑같이 1번 = 엄지 ... 5번 = 새끼 (뒤집지 않아요. 왼손은 화면에서 5 새끼 · 4 약지 · 3 중지 · 2 검지 · 1 엄지 로 놓여요)
FINGERS = ("thumb", "index finger", "middle finger", "ring finger", "little finger")


FINGER_KEYS = ("thumb", "index", "middle", "ring", "little")      # FINGERS 와 같은 순서 (1~5번)


def worn_text(tip_fingers, guide_geo, ref_hands=None, map_image=None):
    """장면 생성 지시 (팁 사진 없이): image 2 = 팁을 그대로 씌운 참조용 손. 같은 손가락끼리 그대로 옮겨요."""
    used = list(tip_fingers)
    shown = ", ".join(FINGERS[k - 1] for k in sorted(guide_geo))
    only = ", ".join(f"the {FINGERS[k - 1]} gets the {FINGERS[k - 1]}'s nail of image 2" for k in used)
    s = (" The nail designs are licensed originals: reproduce them exactly, never alter them. Image 2 is the WORN REFERENCE: another hand already wearing the exact press-on nails of this order, one "
         f"per finger ({shown}). It is the ONLY source of the nail designs. Transfer them finger to finger: {only}. "
         "Copy each nail EXACTLY as it is in image 2 - the same colors, translucency, patterns, decorations, 3D "
         "charms, characters and letters at the same places along the nail, the same finish - and keep its "
         "direction relative to the finger: what sits at the fingertip end in image 2 sits at the fingertip end in "
         "image 1, what sits at the cuticle stays at the cuticle. Only fit each nail to its new nail's angle, "
         "perspective, curvature and lighting. Never move a design to another finger, never swap neighbours, never "
         "mirror the order and never redesign, simplify or recolor anything. Take ONLY the nails from image 2 - never "
         "copy its hand, pose, skin, mirror, sweater or background. ")
    if ref_hands:
        parts = []
        for h in ref_hands:
            seen = []
            for k in range(1, 6):
                if k in h["nails"]:
                    x, y = h["nails"][k]
                    g = (h.get("geo") or {}).get(k)
                    where = (f"{FINGERS[k - 1]} nail from its cuticle at ({g['base'][0] * 100:.0f}% from left, "
                             f"{g['base'][1] * 100:.0f}% from top) to its fingertip end at ({g['tip'][0] * 100:.0f}%, "
                             f"{g['tip'][1] * 100:.0f}%)" + box_text(g) if g else
                             f"{FINGERS[k - 1]} nail at about ({x * 100:.0f}% from left, {y * 100:.0f}% from top)")
                    seen.append(where + (" -> its nail from image 2, its fingertip end at that fingertip end"
                                         if k in used else " -> keep as it is"))
                else:
                    seen.append(f"{FINGERS[k - 1]}: not visible")
            parts.append((f"hand {len(parts) + 1}: " if len(ref_hands) > 1 else "") + "; ".join(seen))
        s += "The fingers in image 1 have already been identified (anatomically): " + " | ".join(parts) + ". "
        if map_image:
            s += (f"Image {map_image} is a FINGER MAP: image 1 with a numbered marker on each of these nails (1 thumb, "
                  "2 index, 3 middle, 4 ring, 5 little) and an arrow from each nail's cuticle to its fingertip end - "
                  "only a guide, never draw the markers or arrows. ")
    return s


def finger_map(img, hands, use=None):
    """레퍼런스 사진 위에 손톱마다 손가락 번호(1=엄지 ... 5=새끼) 표시 -> 이미지 모델용 손가락 지도.
    hands: [{"side", "nails": {번호: (x, y) 0-1}}], use: 표시할 손가락 번호들 (None = 전부)."""
    from PIL import ImageDraw
    m = img.convert("RGB").copy()
    d = ImageDraw.Draw(m)
    W, H = m.size
    r = max(10, int(min(W, H) * 0.028))
    font = _label_font(int(r * 1.3))
    for h in hands or []:
        for num, g in sorted((h.get("geo") or {}).items()):      # 뿌리 -> 손톱 끝 화살표
            if use is not None and num not in use:
                continue
            (bx, by), (tx, ty) = (g["base"][0] * W, g["base"][1] * H), (g["tip"][0] * W, g["tip"][1] * H)
            if g.get("box"):                                      # 손톱 상자 윤곽 (모양 · 각도 안내)
                ol = geo_outline(g, W, H)
                d.line(ol + ol[:1], fill=(0, 200, 255), width=max(2, r // 6))
            d.line((bx, by, tx, ty), fill=(230, 0, 120), width=max(2, r // 4))
            import math
            a = math.atan2(ty - by, tx - bx)
            d.polygon([(tx, ty), (tx - r * math.cos(a - 0.5), ty - r * math.sin(a - 0.5)),
                       (tx - r * math.cos(a + 0.5), ty - r * math.sin(a + 0.5))], fill=(230, 0, 120))
        for num, (x, y) in sorted(h["nails"].items()):
            if use is not None and num not in use:
                continue
            cx, cy = x * W, y * H
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255), outline=(230, 0, 120), width=max(2, r // 4))
            t = str(num)
            tw = d.textlength(t, font=font)
            d.text((cx - tw / 2, cy - r * 0.8), t, fill=(230, 0, 120), font=font)
    return m


def finger_assign(analysis, side):
    """팁 세트 인식 결과(tips_en, 왼쪽부터) + 손(left/right) -> [(손가락, 팁 설명, 팁 번호)]. 세트가 아니면 [].
    side == 'both' 이면 오른손 배정을 돌려줘요 (양손은 finger_text/finger_ko 에서 손마다 따로 써요)."""
    tips = [str(t).strip() for t in ((analysis or {}).get("tips_en") or []) if str(t).strip()]
    if not (analysis or {}).get("tipset") or not tips:
        return []
    # 오른손 · 왼손 모두: 1번 = 엄지, 2번 = 검지, 3번 = 중지, 4번 = 약지, 5번 = 새끼 (왼손도 뒤집지 않아요)
    n = min(5, len(tips))
    idx = list(range(n))
    return [(FINGERS[k], tips[i], i + 1) for k, i in enumerate(idx)]


def tips_by_finger(tips, side):
    """세트에서 왼쪽 -> 오른쪽 순서인 팁 사진들 -> 손가락 순서 [엄지, 검지, 중지, 약지, 새끼].
    오른손 · 왼손 모두 그대로 (1번 = 엄지 ... 5번 = 새끼, 왼손도 뒤집지 않아요)."""
    return list(tips or [])[:5]


def _finger_list(fa):
    return "; ".join(f"{f} = tip {i}: {t.rstrip('.')}" for f, t, i in fa)


def finger_text(analysis, side, first_tip_image=None, n_tip_images=0, tip_fingers=None, ref_hands=None,
                map_image=None, mirror=False):
    """이미지 모델에 보낼 손가락별 지시 (세트가 아니면 빈 문자열). 양손(both)이면 같은 줄을 손마다 전부 적용.
    first_tip_image: 팁을 하나씩 잘라 함께 보낼 때 첫 팁 사진의 이미지 번호 (그 사진들이 정답, 글은 참고)
    tip_fingers: 보내는 팁 사진들이 각각 몇 번 손가락인지 [1=엄지 ... 5=새끼] (None = 1번부터 차례대로)
    ref_hands: 레퍼런스 사진에서 인식한 손들 [{"side", "nails": {번호: (x, y)}}] (None = 모름)
    map_image: 손가락 지도(레퍼런스 + 손톱 번호) 이미지 번호"""
    fa = finger_assign(analysis, "right" if side == "both" else side)
    if first_tip_image and (n_tip_images or tip_fingers):      # 팁 사진이 있으면 글 설명이 없어도 1번=엄지 ... 5번=새끼
        # 팁 사진이 정답 -> 인식 모델이 쓴 글 설명(tips_en)은 보내지 않아요 (글이 틀려 디자인이 바뀌는 일이 없게)
        nums = list(tip_fingers or range(1, min(5, n_tip_images) + 1))
        fa = [(FINGERS[i - 1], "", i) for i in nums]
        img_no = {i: first_tip_image + j for j, (_, _, i) in enumerate(fa)}
        imgs = ", ".join(f"image {img_no[i]} = tip {i} ({f})" for f, _, i in fa)
        lst = "; ".join(f"{f} = tip {i} = image {img_no[i]}" for f, _, i in fa)
        labels = ", ".join(FINGER_LABELS_EN[i - 1] for _, _, i in fa)
        only = ", ".join(f"the {f} gets only tip {i}" for f, _, i in fa)
        head = (f" Image 2 is a FINGER CHART: {len(fa)} press-on nail tips of one set, separated and placed side by "
                + ("side like the fingers of this LEFT hand - mirrored: little, ring, middle, index, thumb from left to "
                   "right (only the order is mirrored; every tip image itself is upright and NOT flipped), "
                   if mirror else "side in the set's own order (thumb, index, middle, ring, little from left to right), ")
                + f"each labelled under it with its number and finger ({labels}). The "
                "labels are instructions only - never draw any text, numbers or labels in the result. Each tip is "
                f"also shown alone, enlarged: {imgs}. Each tip design belongs to EXACTLY ONE finger: {only}. Never "
                "move a design to another finger, never swap neighbouring designs, never repeat one design on two "
                "fingers and never mirror the order. ")
        if ref_hands:      # 레퍼런스에서 인식한 손·손가락 위치 그대로
            used = set(nums)
            parts = []
            for h in ref_hands:
                seen = []
                for k in range(1, 6):
                    if k in h["nails"]:
                        x, y = h["nails"][k]
                        g = (h.get("geo") or {}).get(k)
                        where = (f"{FINGERS[k - 1]} nail from its cuticle at ({g['base'][0] * 100:.0f}% from left, "
                                 f"{g['base'][1] * 100:.0f}% from top) to its fingertip end at ({g['tip'][0] * 100:.0f}%, "
                                 f"{g['tip'][1] * 100:.0f}%)" + box_text(g) if g else
                                 f"{FINGERS[k - 1]} nail at about ({x * 100:.0f}% from left, {y * 100:.0f}% from top)")
                        seen.append(where + (f" -> tip {k} (image {img_no[k]}), the TOP of the tip image at that "
                                             "fingertip end and its BOTTOM at that cuticle" if k in used
                                             else " -> keep as it is"))
                    else:
                        seen.append(f"{FINGERS[k - 1]}: not visible")
                parts.append((f"hand {len(parts) + 1}: " if len(ref_hands) > 1 else "") + "; ".join(seen))
            head += ("The fingers in image 1 have already been identified (anatomically): " + " | ".join(parts) + ". "
                     + (f"Image {map_image} is a FINGER MAP: image 1 with a numbered marker on each of these nails "
                        "(1 thumb, 2 index, 3 middle, 4 ring, 5 little) and an arrow from each nail's cuticle to its "
                        "fingertip end - only a guide, never draw the markers or arrows. "
                        if map_image else "")
                     + "Paint each nail exactly at the position given for its finger. A finger that is not visible "
                     "stays hidden: do not add, reveal or invent it, and its tip is simply not used. ")
        head += TIP_TRANSFER_EN + CLEAR_PAINT_EN + " "
        if ref_hands:
            if len(ref_hands) > 1:
                return head + "Apply the set to EACH hand separately with the same finger mapping: " + lst + "."
            return head + f"Put the tips on exactly like this: " + lst + "."
        if side == "both":
            return (head + "Image 1 shows BOTH a left hand and a right hand. Apply the set to EACH hand separately "
                    "with the same finger mapping (identify each finger anatomically): " + lst + ".")
        return (head + f"The hand in image 1 is a {side or 'right'} hand. First identify each finger anatomically "
                "(thumb, index, middle, ring, little - by anatomy, not by its left/right position in the photo), "
                "then put the tips on exactly like this: " + lst + ".")
    if not fa:
        return ""
    n = len(fa)
    head = (f" Image 2 is a set of press-on nail tips; the tips are numbered 1 to {n} from left to right. "
            + TIP_ORIENT_EN + " So put each tip on its nail with the same orientation: the top end of the tip goes "
            "to the free edge at the fingertip and the bottom end to the cuticle - never upside down; keep each "
            "tip's pattern along the nail exactly as it runs from top to bottom in image 2." + CLEAR_PAINT_EN + " ")
    if side == "both":
        return (head + "Image 1 shows BOTH a left hand and a right hand. Apply the WHOLE set to EACH hand separately, "
                "with the same finger mapping on both hands (identify each finger anatomically, not by its position "
                "in the photo; every visible nail of both hands gets a tip; each finger has its own design - do not "
                "repeat one design on every nail): " + _finger_list(fa) + ".")
    return (head + f"The hand in image 1 is a {side or 'right'} hand. Identify each finger anatomically (not by its "
            "position in the photo) and put the tips on exactly like this (each finger has its own design - do not "
            "repeat one design on every nail): " + _finger_list(fa) + ".")


def finger_ko(analysis, side):
    ko = {"thumb": "엄지", "index finger": "검지", "middle finger": "중지", "ring finger": "약지", "little finger": "새끼"}
    one = lambda s: " · ".join(f"{ko[f]}={i}번" for f, _, i in finger_assign(analysis, s))
    return one(side)


# ---------------------------------------------------------------------------------------------- 가격표
# 추가 요청 칸에 적은 가격 -> luna 가 읽어서 세트 순서대로 "6.5" 같은 라벨로 (서버는 숫자를 해석하지 않아요)
PRICE_INSTRUCTIONS = """You read a Korean nail shop owner's free-text request that was typed under an uploaded photo of
press-on nail tip sets. The text may contain a list of prices (one per tip set, in order) mixed with other requests
(mood, background, drink ...). Extract the prices and write each one as a price label by these rules:

RULES FOR A LABEL
- Unit = 10,000 won (만원). Write the number only: no "원", no "만", no currency sign, no comma.
- Always at least one decimal place: 5만원 -> "5.0", 60000원 / 60,000원 / 6만 -> "6.0", 75000 -> "7.5",
  6만5천원 / 육만오천원 / 65,000원 -> "6.5", 1000원 / 천원 -> "0.1", 12만원 -> "12.0".
- Use a second decimal only when the price needs it: 45500원 -> "4.55". Never round a price.
- A bare small number with a decimal or below 100 (4.5, 7, 5.0) is already in 만원 -> "4.5", "7.0", "5.0".
- A bare number of 1000 or more without a unit is in won -> divide by 10,000 (75000 -> "7.5").
- Korean number words count too (오만원 -> "5.0", 칠만 오천원 -> "7.5").
- Keep the order the owner wrote them in. One label per price. Never invent, merge, sort or drop prices.

WHAT IS NOT A PRICE
- Numbers that describe the picture or the request: years (2026년), dates, counts (촛불 2개, 3세트), sizes (3D, 10cm),
  percentages, set numbers ("1번 세트") etc. If you are not sure a lone number is a price, it is not a price
  unless the owner wrote 가격 / 원 / 만 / 천 / ₩ near it or listed several numbers like a price list.

REST
- "rest" = the original text with every price (and words like "가격:" that only introduce prices) removed, keeping
  all other requests word for word in Korean. Clean up leftover commas / slashes. "" if nothing is left."""

PRICE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["prices", "rest"],
    "properties": {
        "prices": {"type": "array", "items": {"type": "string"}, "description": "price labels in 만원, e.g. 6.5"},
        "rest": {"type": "string"}}}


TIP_VERIFY_INSTRUCTIONS = """You check automatic crops of press-on nail tips. Image 1 shows ONE set of tips (usually 5,
thumb to little finger, left to right); each tip has a small red number label placed just beyond its base (cuticle
end), not on the tip. For upright tips the top of the photo is the free edge (fingertip end). The other images are
automatic crops of some tips; a crop can be wrong: cut off at the top, bottom or a side, shifted onto a neighbouring
tip, including part of a neighbour, or missing a clear/transparent free edge.
For EVERY tip number you are asked about:
- crop_ok = true only if its crop shows that whole tip (including clear parts and 3D charms) and nothing of another tip.
- Give that tip's precise position in image 1 as fractions of image 1's width and height (0..1, x then y):
  center = middle of the tip, free_edge = middle of the fingertip end at the very end (include clear parts),
  base = middle of the cuticle end.
- problem_ko = what is wrong with the crop in short Korean ("" if crop_ok).
Look carefully at image 1 itself to find the tip; never guess from the crop alone."""

TIP_MEASURE_INSTRUCTIONS = (
    "Each image is ONE press-on nail tip cut out of a set photo (neighbouring tips may show faded at the sides - ignore "
    "them, measure only the main tip in the middle). For every image give: free_edge = [x, y] the middle of the tip's "
    "FREE EDGE (the outer end that sits past the fingertip: the pointed/narrower/flat-cut end, where clear parts usually "
    "are; for a round or oval tip, the end the design reads upright toward), base = [x, y] the middle of its CUTICLE "
    "end (the smooth C-curve at the other end), width = the tip's width at its widest point across the free-edge-to-base "
    "axis. Cover the whole tip including clear parts and charms that sit ON the tip (not charms sticking far out past "
    "its edge). Coordinates normalized 0-1 of that image (x right, y down); width as a fraction of the image width. "
    "visible = false only if the image shows no complete tip at all."
)
TIP_MEASURE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tips"],
    "properties": {"tips": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["number", "free_edge", "base", "width", "visible"],
        "properties": {"number": {"type": "integer"},
                       "free_edge": {"type": "array", "items": {"type": "number"}},
                       "base": {"type": "array", "items": {"type": "number"}},
                       "width": {"type": "number"}, "visible": {"type": "boolean"}}}}},
}
TIP_LUNA_AXIS = True          # 팁마다 luna 가 손톱 끝 · 뿌리 · 폭을 재서 그 축대로 돌려 nail_ref 표본 틀에 똑바로 넣어요 (세트당 1번 호출)


TIP_VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tips"],
    "properties": {"tips": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["number", "crop_ok", "center", "free_edge", "base", "problem_ko"],
        "properties": {
            "number": {"type": "integer"}, "crop_ok": {"type": "boolean"},
            "center": {"type": "array", "items": {"type": "number"}},
            "free_edge": {"type": "array", "items": {"type": "number"}},
            "base": {"type": "array", "items": {"type": "number"}},
            "problem_ko": {"type": "string"}}}}}}


# ---------------------------------------------------------------------------------------------- 자동 평가 (luna 심사)
# 정답 = luna 의 판단 (사람 정답 아님). 실행마다 실험 보고서에 F1 · IoU · 정확도를 적어요.
#   ① 세트 · 팁 찾기: 찾은 팁마다 번호 점을 찍은 원본 사진 -> 진짜 팁 위의 점 / 엉뚱한 점 / 점 없는 팁 -> 정밀도 · 재현율 · F1
#   ② 팁 오리기: 오리기 전 사진에서 luna 가 그린 팁 상자 vs 프로그램이 딴 팁 상자 -> 상자 IoU · Dice
#   ③ 손가락 배정: 결과 사진의 손가락마다 몇 번 팁 디자인이 보이는지 -> 정확도 · 손가락별 F1
EVAL_JUDGE = True

EVAL_DETECT_INSTRUCTIONS = """You grade an automatic press-on nail tip detector. Image 1 is a photo of press-on nail
tips (one or more sets). The detector put a small numbered red marker on every tip it found.
- real_tip_count: count every real press-on nail tip in the photo (ignore printed text, stickers, hands, tips that
  are mostly cut off by the photo border).
- wrong_markers: numbers of markers that are NOT on a real tip, or that are a second marker on a tip that already
  has another marker.
- missed_tips: how many real tips have no marker at all.
- notes_ko: one short Korean sentence about the main mistakes ("" if none).
Look carefully; count tip by tip."""
EVAL_DETECT_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["real_tip_count", "wrong_markers", "missed_tips", "notes_ko"],
                      "properties": {"real_tip_count": {"type": "integer"},
                                     "wrong_markers": {"type": "array", "items": {"type": "integer"}},
                                     "missed_tips": {"type": "integer"}, "notes_ko": {"type": "string"}}}

EVAL_CROP_INSTRUCTIONS = """You grade automatic crops of press-on nail tips. Each image is one crop meant to show ONE
tip (the tip should be in the middle; the surroundings are faded). For each image give the tight box around that ONE
whole tip, including clear/transparent parts and 3D charms, but not neighbouring tips or background: box = [x0, y0,
x1, y1] as fractions of that image's width and height (0..1). cut_off = true if the tip continues past the image
border. other_tip_inside = true if part of another tip is visible inside the crop."""
EVAL_CROP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["tips"],
                    "properties": {"tips": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["number", "box", "cut_off", "other_tip_inside"],
                        "properties": {"number": {"type": "integer"},
                                       "box": {"type": "array", "items": {"type": "number"}},
                                       "cut_off": {"type": "boolean"}, "other_tip_inside": {"type": "boolean"}}}}}}

EVAL_FINGER_INSTRUCTIONS = """You grade an AI nail-art photo. Image 1 is the result: a hand wearing press-on nail
designs. The next images are the tip designs numbered 1, 2, 3 ... For each finger of the hand in image 1 (thumb,
index, middle, ring, little - identify fingers by anatomy), say which tip number's design its nail shows. Use 0 if
that finger's nail is not visible, or its design matches none of the tips. If several hands are shown, judge the main
(largest) one. Judge the design itself (colors, pattern, motifs), not small rendering differences."""
EVAL_FINGER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fingers"],
                      "properties": {"fingers": {"type": "array", "items": {
                          "type": "object", "additionalProperties": False, "required": ["finger", "tip_number"],
                          "properties": {"finger": {"type": "string",
                                                    "enum": ["thumb", "index", "middle", "ring", "little"]},
                                         "tip_number": {"type": "integer"}}}}}}


def box_iou_dice(a, b):
    """두 상자 [x0, y0, x1, y1] -> (IoU, Dice)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    sa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    sb = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = sa + sb - inter
    return (inter / union if union > 0 else 0.0), (2 * inter / (sa + sb) if sa + sb > 0 else 0.0)


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return p, r, f


def finger_f1(pairs):
    """[(기대 팁 번호, luna 가 본 팁 번호)] (0 = 판단 안 함 제외) -> (정확도, 손가락(팁 번호)별 F1 dict, 매크로 F1)."""
    pairs = [(e, g) for e, g in pairs if g]
    if not pairs:
        return None, {}, None
    acc = sum(1 for e, g in pairs if e == g) / len(pairs)
    per = {}
    for c in sorted({e for e, _ in pairs}):
        tp = sum(1 for e, g in pairs if e == c and g == c)
        fp = sum(1 for e, g in pairs if e != c and g == c)
        fn = sum(1 for e, g in pairs if e == c and g != c)
        per[c] = prf(tp, fp, fn)[2]
    vals = [v for v in per.values() if v is not None]
    return acc, per, (sum(vals) / len(vals) if vals else None)


def b64_jpeg(im, quality=90):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def sets_reading_order(sets):
    """세트 인식 결과 -> 사진 읽는 순서 (위 줄부터, 같은 줄은 왼쪽부터). 가격 · 세트 번호를 이 순서로 매겨요."""
    def info(st):
        ts = [t for t in st.get("tips", []) if "x" in t and "y" in t]
        if not ts:
            return 9.0, 9.0, 0.1
        return (sum(float(t["y"]) for t in ts) / len(ts), sum(float(t["x"]) for t in ts) / len(ts),
                max(0.02, float(st.get("tip_length") or 0.1)))
    rows = []
    for st in sorted(sets, key=lambda s: info(s)[0]):
        cy, cx, L = info(st)
        if rows and abs(cy - rows[-1][0]) < 0.6 * L:
            rows[-1][1].append(st)
        else:
            rows.append([cy, [st]])
    return [st for _, r in rows for st in sorted(r, key=lambda s: info(s)[1])]


def _price_font(size):
    from PIL import ImageFont
    for f in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/malgun.ttf",
              "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    return ImageFont.load_default()


def price_sheet(img, priced):
    """원본 팁 모음 사진 + [(세트 인식 결과, '5.0')] -> 세트마다 맨 오른쪽 팁의 오른쪽 아래에 가격 라벨을 넣은 사진.
    글자 크기는 팁 길이에 맞추고, 글자색은 그 자리 배경색을 어둡게(배경이 어두우면 밝게) 해서 사진 분위기에 맞춰요."""
    import numpy as np
    from PIL import ImageDraw
    out = img.convert("RGB").copy()
    W, H = out.size
    arr = np.asarray(out)
    d = ImageDraw.Draw(out)
    cl = lambda v: max(0.0, min(1.0, float(v)))
    for st, label in priced:
        ts = [t for t in st.get("tips", []) if "x" in t and "y" in t]
        if not ts or not label:
            continue
        L = max(0.02, min(0.5, float(st.get("tip_length") or 0.1))) * H
        pts = sorted(((cl(t["x"]) * W, cl(t["y"]) * H, t) for t in ts), key=lambda p: p[0])
        gaps = [pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1)]
        step = sorted(gaps)[len(gaps) // 2] if gaps else 0.45 * L
        x, y, t = pts[-1]
        ys = [y + 0.5 * L] + [cl(t[k]) * H for k in ("ey", "by") if k in t]
        bottom = max(ys)
        font = _price_font(max(12, int(round(0.12 * L))))
        l, tp, r, b = d.textbbox((0, 0), label, font=font)
        tw, th = r - l, b - tp
        lx = x + 0.55 * step
        lx = min(lx, W - tw - 0.01 * W)
        ty = min(bottom - th, H - th - 0.01 * H)
        # 그 자리 배경색 (라벨 둘레) -> 같은 색 계열로 어둡게 / 밝게
        x0, y0 = int(max(0, lx - 0.3 * tw)), int(max(0, ty - 0.5 * th))
        x1, y1 = int(min(W, lx + 1.3 * tw)), int(min(H, ty + 1.5 * th))
        bg = np.median(arr[y0:y1, x0:x1].reshape(-1, 3), axis=0) if x1 > x0 and y1 > y0 else np.array([200, 200, 200])
        lum = float(bg @ np.array([0.299, 0.587, 0.114]))
        col = bg * 0.55 if lum > 110 else bg + (255 - bg) * 0.65
        d.text((lx - l, ty - tp), label, font=font, fill=tuple(int(v) for v in col))
    return out


def set_crop(img, st):
    """세트 1개(팁 중심점들) -> 그 줄만 또렷하고 둘레는 부드럽게 하얗게 한 잘라낸 사진 (세트 인식 결과). 줄을 못 만들면 None."""
    import math
    from PIL import Image, ImageDraw, ImageFilter
    W, H = img.size
    cl = lambda v: max(0.0, min(1.0, float(v)))
    tips = [t for t in st.get("tips", []) if "x" in t and "y" in t]
    pts = [(cl(t["x"]) * W, cl(t["y"]) * H) for t in tips]
    ends = [(cl(t["ex"]) * W, cl(t["ey"]) * H) if "ex" in t and "ey" in t else None for t in tips]
    bases = [(cl(t["bx"]) * W, cl(t["by"]) * H) if "bx" in t and "by" in t else None for t in tips]
    wids = [cl(t.get("w") or 0) * H for t in tips]                 # luna: 팁마다 폭 (세트 사진 픽셀, 0 = 모름)
    try:
        ref = int(st.get("reference") or 0) - 1                   # luna: 모양 기준 팁 (luna 목록 순서, 0부터)
    except (TypeError, ValueError):
        ref = -1
    refs = [k == ref for k in range(len(tips))]
    if len(pts) < 2:
        return None
    H0 = H                                 # tip_length 는 원래 사진 높이 기준
    turn = set_axis_turn(ends, bases) if SET_UPRIGHT and SET_CROP_ROTATE else None
    if turn is not None and abs(turn) >= SET_ROTATE_MIN_DEG:
        # 세트를 통째로 돌려 팁 축을 세로(Y축 일직선)로 -> 그 다음에 세트를 잘라요
        img, mp = rotate_keep(img, turn)
        W, H = img.size
        pts = [mp(p) for p in pts]
        ends = [mp(e) if e else None for e in ends]
        bases = [mp(b) if b else None for b in bases]
    # 줄 방향(가장 먼 두 점)으로 정렬 -> 줄을 따라 이어진 순서 (손가락 순서는 똑바로 세운 뒤 tip_crops 에서)
    i0, i1 = max(((i, j) for i in range(len(pts)) for j in range(len(pts))),
                 key=lambda ij: (pts[ij[0]][0] - pts[ij[1]][0]) ** 2 + (pts[ij[0]][1] - pts[ij[1]][1]) ** 2)
    dx, dy = pts[i1][0] - pts[i0][0], pts[i1][1] - pts[i0][1]
    order = sorted(range(len(pts)), key=lambda k: pts[k][0] * dx + pts[k][1] * dy)
    pts, ends, bases = [pts[k] for k in order], [ends[k] for k in order], [bases[k] for k in order]
    wids, refs = [wids[k] for k in order], [refs[k] for k in order]
    L = max(0.03, min(0.5, float(st.get("tip_length") or 0.1))) * H0
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    n = math.hypot(x1 - x0, y1 - y0) or 1.0
    ux, uy = (x1 - x0) / n, (y1 - y0) / n
    ext = 0.3 * L
    line = [(x0 - ux * ext, y0 - uy * ext)] + pts + [(x1 + ux * ext, y1 + uy * ext)]
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    r = int(0.72 * L)          # 팁 끝의 투명 부분까지 또렷하게 (0.62 -> 0.72)
    d.line(line, fill=255, width=2 * r, joint="curve")
    for x, y in (line[0], line[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    # luna 가 찾은 세트 줄만 또렷하게, 둘레는 부드럽게 하얗게 (다른 세트 · 판이 섞여 보이지 않게)
    m = m.filter(ImageFilter.GaussianBlur(max(2, L * 0.06)))
    faded = Image.blend(img, Image.new("RGB", (W, H), (245, 245, 245)), 0.82)
    out = Image.composite(img, faded, m)
    bx = m.point(lambda v: 255 if v > 20 else 0).getbbox()
    if not bx:
        return None
    pad = int(0.25 * L)
    bx = (max(0, bx[0] - pad), max(0, bx[1] - pad), min(W, bx[2] + pad), min(H, bx[3] + pad))
    if min(bx[2] - bx[0], bx[3] - bx[1]) < 48:
        return None
    out = out.crop(bx)
    pts = [(x - bx[0], y - bx[1]) for x, y in pts]
    ends = [(e[0] - bx[0], e[1] - bx[1]) if e else None for e in ends]
    bases = [(b[0] - bx[0], b[1] - bx[1]) if b else None for b in bases]
    # 세트는 돌리지 않아요: 팁 순서 = 사진에 보이는 왼쪽 -> 오른쪽 그대로. 방향은 tip_crops 에서 팁마다 세워요
    out.info["tip_pts"] = pts      # 팁을 하나씩 잘라 손가락별 참고 사진으로 써요
    out.info["tip_ends"] = ends    # 팁마다 손톱 끝(손가락이 뻗는 쪽) 점 - luna
    out.info["tip_bases"] = bases  # 팁마다 손톱 뿌리(큐티클 쪽) 점 - luna
    out.info["tip_len"] = L
    out.info["set_turn"] = turn if turn is not None and abs(turn) >= SET_ROTATE_MIN_DEG else 0.0
    out.info["tip_wids"] = wids    # 팁마다 폭 - luna (크기 비율에 써요)
    out.info["tip_refs"] = refs    # 모양 기준 팁 표시 (True 하나) - luna
    return out


def hi_set(hi_src, st, cr):
    """고해상도 원본(회전 보정한 사진) + 세트 인식 결과 + 보통 크기 세트 사진 -> {"hi": 고해상도 세트 사진, "hi_scale": 배율}.
    같은 세트를 같은 비율로 잘라서 좌표는 배율만 곱하면 돼요. 원본이 더 크지 않거나 실패하면 {}."""
    if hi_src is None:
        return {}
    try:
        c = set_crop(hi_src, st)
    except Exception:
        return {}
    if c is None or c.width <= cr.width * 1.05:
        return {}
    return {"hi": c, "hi_scale": c.width / cr.width}


def sheet_turn(img, sets):
    """사진 전체 + 세트 인식 결과 -> 사진의 모든 팁 축이 세로가 되게 돌릴 각도 (rotate_keep deg). 돌릴 필요 없으면 0."""
    W, H = img.size
    cl = lambda v: max(0.0, min(1.0, float(v)))
    ends, bases = [], []
    for st in sets or []:
        for t in st.get("tips", []):
            if all(k in t for k in ("ex", "ey", "bx", "by")):
                ends.append((cl(t["ex"]) * W, cl(t["ey"]) * H))
                bases.append((cl(t["bx"]) * W, cl(t["by"]) * H))
    turn = set_axis_turn(ends, bases) if SET_UPRIGHT else None
    return 0.0 if turn is None or abs(turn) < SET_ROTATE_MIN_DEG else turn


def straighten_sheet(img, sets):
    """사진 전체 + 세트 인식 결과 -> (Y축이 위아래로 똑바른 사진, 그 사진 기준 세트 목록, 돌린 각도).
    사진의 모든 팁 축으로 한 번만 돌려요 (세트마다 따로 X). 돌린 세트마다 "_orig" = 원래 결과 (가격표 등 원본 사진용)."""
    W, H = img.size
    cl = lambda v: max(0.0, min(1.0, float(v)))
    ends, bases = [], []
    for st in sets:
        for t in st.get("tips", []):
            if all(k in t for k in ("ex", "ey", "bx", "by")):
                ends.append((cl(t["ex"]) * W, cl(t["ey"]) * H))
                bases.append((cl(t["bx"]) * W, cl(t["by"]) * H))
    turn = set_axis_turn(ends, bases) if SET_UPRIGHT else None
    if turn is None or abs(turn) < SET_ROTATE_MIN_DEG:
        return img, [dict(st, _orig=st) for st in sets], 0.0
    out, mp = rotate_keep(img, turn)
    W2, H2 = out.size
    new = []
    for st in sets:
        tips = []
        for t in st.get("tips", []):
            t2 = dict(t)
            for kx, ky in (("x", "y"), ("ex", "ey"), ("bx", "by")):
                if kx in t and ky in t:
                    x, y = mp((cl(t[kx]) * W, cl(t[ky]) * H))
                    t2[kx], t2[ky] = x / W2, y / H2
            tips.append(t2)
        new.append(dict(st, tips=tips, tip_length=float(st.get("tip_length") or 0.1) * H / H2, _orig=st))
    print(f"  [사진 세우기] 사진 전체를 {turn:.1f}도 돌려 팁 축을 세로로 맞췄어요")
    return out, new, turn


def inspect_set_safe(cr, inspect=None):
    """세로로 세운 세트 사진 -> luna 세트 확인 결과 (손톱 끝 위/아래 · 모양 · 팁 위치와 크기 · 기준 팁) 또는 None."""
    if not inspect:
        return None
    try:
        data = inspect(cr)
    except Exception as e:
        print(f"  ! 세트 확인(luna) 실패 - 세트 찾기 결과로 진행: {type(e).__name__}: {str(e)[:150]}")
        return None
    tips = [t for t in (data or {}).get("tips") or []
            if all(isinstance(t.get(k), (int, float)) for k in ("x", "top", "bottom", "w"))]
    return dict(data, tips=tips) if len(tips) >= 2 else None


def set_tip_votes(hints, data):
    """세트 하나 -> 손톱 끝이 위라는 표 (위 표 수, 아래 표 수). luna 세트 확인이 있으면 그 판단(팁 개수만큼),
    없으면 세트 찾기 때의 끝 · 뿌리 점."""
    if data:
        n = len(data["tips"])
        return (n, 0) if data.get("free_edge_at_top") else (0, n)
    vs = [e[1] - b[1] for e, b in zip(hints.get("ends") or [], hints.get("bases") or []) if e and b]
    return sum(1 for v in vs if v < 0), sum(1 for v in vs if v > 0)


def orient_set(cr, hints, data, up):
    """세로로 세운 세트 사진 + hints + luna 세트 확인 결과 + 사진 전체의 손톱 끝 방향(up) -> (세트 사진, 새 hints).
    up=False(사진 전체에서 손톱 끝이 아래)면 상하 거울 뒤집기 - 같은 사진의 모든 세트에 똑같이 (세트마다 따로 정하지 않아요).
    위아래만 바뀌고 왼쪽 -> 오른쪽 순서는 그대로예요. 팁 위치 · 크기는 luna 가 이 세트 사진에서 잰 값으로 바꿔요."""
    from PIL import ImageOps
    W, H = cr.size
    h = dict(hints)
    if data:
        cl = lambda v: max(0.0, min(1.0, float(v)))
        pts, ends, bases, wids = [], [], [], []
        for t in data["tips"]:
            x, y0, y1 = cl(t["x"]) * W, cl(min(t["top"], t["bottom"])) * H, cl(max(t["top"], t["bottom"])) * H
            pts.append((x, (y0 + y1) / 2))
            ends.append((x, y0) if up else (x, y1))          # 손톱 끝 (사진 전체 방향대로)
            bases.append((x, y1) if up else (x, y0))         # 손톱 뿌리
            wids.append(cl(t["w"]) * W)
        lens = sorted(abs(b[1] - e[1]) for e, b in zip(ends, bases))
        ref = int(data.get("reference") or 0) - 1
        h["ref_pick"] = int(data.get("ref_sample") or 0)      # luna 가 고른 nail_ref 표본 번호 (1부터, 0 = 못 고름)
        h.update(pts=pts, ends=ends, bases=bases, wids=wids, refs=[k == ref for k in range(len(pts))],
                 len=lens[len(lens) // 2] * 1.05 or h.get("len"),
                 shape=data.get("shape") or h.get("shape"), length=data.get("length") or h.get("length"),
                 pointed=(data.get("shape") == "pointed") or bool(h.get("pointed")))
    else:                                                    # luna 확인이 없으면: 세트 찾기 점의 끝 · 뿌리를 사진 전체 방향에 맞춰요
        ends2, bases2 = [], []
        for e, b in zip(h.get("ends") or [], h.get("bases") or []):
            if e and b and ((e[1] > b[1]) if up else (e[1] < b[1])):
                e, b = b, e
            ends2.append(e); bases2.append(b)
        if ends2:
            h.update(ends=ends2, bases=bases2)
    if up:
        return cr, h
    fy = lambda p: None if p is None else (p[0], H - 1 - p[1])
    out = ImageOps.flip(cr)                                   # 상하 거울 뒤집기
    h.update(pts=[fy(p) for p in h.get("pts") or []], ends=[fy(p) for p in h.get("ends") or []],
             bases=[fy(p) for p in h.get("bases") or []], set_flipped=True)
    if h.get("hi") is not None:                               # 고해상도 원본 세트 사진도 똑같이
        h["hi"] = ImageOps.flip(h["hi"])
    return out, h


def rotate_keep(img, deg, fill=(245, 245, 245)):
    """사진을 deg 도(반시계) 돌리고 잘리지 않게 넓혀요 -> (돌린 사진, 원래 좌표 -> 새 좌표 함수)."""
    import math
    from PIL import Image
    W, H = img.size
    cx, cy = W / 2, H / 2
    out = img.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=fill)
    ncx, ncy = out.width / 2, out.height / 2
    a = math.radians(deg); ca, sa = math.cos(a), math.sin(a)
    return out, (lambda p: (ncx + (p[0] - cx) * ca + (p[1] - cy) * sa, ncy - (p[0] - cx) * sa + (p[1] - cy) * ca))


FINGER_LABELS_EN = ("1 THUMB", "2 INDEX", "3 MIDDLE", "4 RING", "5 LITTLE")
FINGER_LABELS_KO = ("1 엄지", "2 검지", "3 중지", "4 약지", "5 새끼")


def sort_tip_points(pts, ends=None, bases=None, extras=None):
    """팁 중심점들을 왼쪽 -> 오른쪽(세로줄이면 위 -> 아래)으로 정렬해요. 모델이 준 순서를 믿지 않아요.
    ends(팁마다 손톱 끝 점)가 있으면 같이 정렬해서 (pts, ends) 로 돌려줘요.
    extras: 팁마다 값이 든 목록들 (폭 등) -> 같이 정렬해서 맨 뒤에 붙여 돌려줘요."""
    if extras is not None:
        n = len(pts)
        tagged = [(float(x), float(y)) for x, y in pts]
        idx_pts, idx_e, idx_b = sort_tip_points(tagged, list(range(n)), bases if bases is not None else [None] * n)
        order = list(idx_e)                                   # ends 자리에 번호를 넣어 정렬 순서를 알아내요
        ends = list(ends or [None] * n)[:n] + [None] * max(0, n - len(ends or []))
        ex = [[(list(e) + [None] * n)[k] for k in order] for e in extras]
        return idx_pts, [ends[k] for k in order], idx_b, ex
    pts = [(float(x), float(y)) for x, y in pts]
    ends = list(ends or [None] * len(pts))[:len(pts)]
    ends += [None] * (len(pts) - len(ends))
    want_bases = bases is not None
    bases = list(bases or [None] * len(pts))[:len(pts)]
    bases += [None] * (len(pts) - len(bases))
    if len(pts) >= 2:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        horizontal = (max(xs) - min(xs)) >= (max(ys) - min(ys))
        idx = sorted(range(len(pts)), key=(lambda k: (pts[k][0], pts[k][1])) if horizontal else (lambda k: (pts[k][1], pts[k][0])))
        pts, ends, bases = [pts[k] for k in idx], [ends[k] for k in idx], [bases[k] for k in idx]
    return (pts, ends, bases) if want_bases else (pts, ends)


def keep_sheet_up(vx, vy):
    """손톱 끝 방향 (vx, vy) -> 팁이 세트 사진에서 세로로 놓여 있으면(세로에서 60도 안) 사진 위쪽 끝 = 손톱 끝.
    팁을 위아래로 뒤집지 않아요 (캐릭터 · 글자 · 투명한 부분이 사진에 보이는 위아래 그대로). 가로로 누운 팁은 luna 방향 그대로."""
    import math
    n = math.hypot(vx, vy) or 1.0
    if abs(vy) / n >= math.cos(math.radians(60)) and vy > 0:
        return -vx, -vy
    return vx, vy


TIP_SURE_RATIO = 1.15     # 팁 양 끝 폭 차이가 이 배 이상이면 "좁은 끝이 확실" (아니면 같은 세트의 확실한 팁 방향을 따라요)
TIP_SHEET_UP = True       # 세트 사진에 세로로(세로에서 45도 안) 놓인 팁은 사진 위쪽 끝 = 손톱 끝(tip), 아래쪽 끝 = 손톱 뿌리(base).
                          #   인식 모델 방향 · 좁은 끝 규칙으로 뒤집지 않고 기울기만 바로잡아요. 그 방향 그대로 레퍼런스 tip -> base 에 붙여요


def _upright(vx, vy, deg=45):
    """방향 (vx, vy) 가 세로에서 deg 도 안이면 True."""
    import math
    return abs(vy) >= math.cos(math.radians(deg)) * (math.hypot(vx, vy) or 1.0)


TIP_TERRITORY = True        # 팁마다 '자기 구역'(옆 팁보다 자기 팁 축에 더 가까운 곳)만 써요 -> 붙어 놓인 옆 팁 조각이 섞이지 않게
TIP_TERRITORY_MARGIN = 0.03 # 구역 경계를 팁 길이의 이만큼 옆 팁 쪽으로 넉넉히 (자기 팁 가장자리가 잘리지 않게)
TIP_SHAPE_AWARE = True      # luna 가 본 세트의 팁 모양(shape) · 길이(length)에 맞춰 팁 따기 기준을 바꿔요 (끄면 예전 그대로)
TIP_SHAPES = ("pointed", "square", "coffin", "round")
TIP_MIN_ASPECT = {"short": 0.95}        # 팁 모양으로 인정하는 최소 길이/폭 (기본 1.2 - 짧은 팁 · 엄지는 1.2 보다 뭉툭해요)
TIP_REFINE_MIN_ASPECT = {"short": 0.8}  # tip_refine 결과로 인정하는 최소 세로/가로 (기본 1.05)
TIP_FLAT_END_OK = ("square", "coffin")  # 손톱 끝이 원래 평평한 모양 -> '끝 평평(잘림)' 감점을 하지 않아요


def tip_set_shape(hints):
    """세트 hints -> (shape 또는 None, length 또는 None). TIP_SHAPE_AWARE 가 꺼져 있으면 (None, None)."""
    if not TIP_SHAPE_AWARE:
        return None, None
    h = hints or {}
    s = h.get("shape") if h.get("shape") in TIP_SHAPES else ("pointed" if h.get("pointed") else None)
    ln = h.get("length") if h.get("length") in ("short", "medium", "long") else None
    return s, ln


def tip_crops(img, hints, max_tips=5, verify=None, template=True, measure=None):
    """세트 사진 + 팁 중심점들 -> 팁 하나씩 분리한 사진 목록 [1번=엄지, 2번=검지, 3번=중지, 4번=약지, 5번=새끼].
    - 방향 통일 (tip_pose): 팁의 긴 쪽 = 손톱 길이 방향(손톱 끝 <-> 손톱 뿌리) -> 세로로 세우고,
      세로로 놓인 팁(TIP_SHEET_UP)은 세트 사진의 위쪽 끝 = 손톱 끝 -> 위 (뒤집지 않고 기울기만 팁 모양대로)
      가로로 누운 팁만 4개 면 중 폭이 가장 좁은 면 = 손톱 끝 -> 위. 팁 모양에서 직접 재요
      ① 팁 중심 둘레(원)에서 팁 모양을 따서 긴 축 · 좁은 끝을 재고 ② 원본 세트 사진을 그 각도로 돌려 다시 잘라요
      양 끝 폭이 거의 같아 좁은 끝이 애매한 팁은 같은 세트에서 좁은 끝이 확실한 팁들의 방향을 따라요
      (확실한 팁이 없으면 인식 모델의 손톱 끝 점(ends))
      모양을 못 딴 팁(옆 팁과 붙어 있음 등)은 인식 모델의 손톱 끝 점 -> 없으면 세트의 방향으로 세워요
    - 디자인 픽셀은 바꾸지 않아요: 돌리기 · 자르기 · 크기 키우기만 (색 보정 · 덧칠 없음)
    - 순서: 사진에 보이는 그대로 왼쪽 -> 오른쪽(세로줄이면 위 -> 아래) = 엄지 -> 새끼. 순서는 바꾸지 않아요
    - 팁 하나만 또렷하게 남기고 둘레(옆 팁 · 판)는 부드럽게 하얗게 해요 (세트 사진과 같은 느낌, 투명 부분 손보기는 없음)
    verify: 점수가 낮은 팁을 인식 모델(luna)에 다시 확인받는 함수 (OpenAIModel.verify_tips). None 이면 안 해요.
    못 자르면 []."""
    import math
    from PIL import Image, ImageDraw, ImageFilter
    pts, ends, bases, (wids, refs) = sort_tip_points((hints or {}).get("pts") or [], (hints or {}).get("ends"),
                                                     (hints or {}).get("bases") or [],
                                                     extras=[(hints or {}).get("wids") or [], (hints or {}).get("refs") or []])
    L = float((hints or {}).get("len") or 0)
    pointed_set = bool((hints or {}).get("pointed"))   # luna: 뾰족한 세트 -> 뾰족한 끝이 무조건 손톱 끝 (아래를 향해도)
    if len(pts) < 2 or L <= 4:
        return []
    shape, tlen = tip_set_shape(hints)          # 세트의 팁 모양 · 길이 (luna) -> 모양에 맞는 기준으로 따요
    grow0 = float((hints or {}).get("grow0") or 1.0)     # 자르는 범위 기본 배율 (원본에서 다시 자를 때 넓게)
    terr_margin = float((hints or {}).get("terr_margin") or TIP_TERRITORY_MARGIN)
    img_scale = float((hints or {}).get("img_scale") or 1.0)   # 이 사진이 세트 사진의 몇 배인지 (원본에서 다시 자를 때)
    no_luna_len = bool((hints or {}).get("no_luna_len"))   # luna 길이를 점수 · 늘리기에 쓰지 않아요 (원본에서 다시 자를 때)
    min_aspect = TIP_MIN_ASPECT.get(tlen, 1.2)
    if shape or tlen:
        print(f"  [팁 모양] 세트: {shape or '?'} / {tlen or '?'}")
    img = img.convert("RGB")
    W, H = img.size

    def _inside(q):            # 인식 모델 점이 사진 밖으로 나가도 사진 안으로 (자르기 오류 방지)
        return None if q is None else (min(max(float(q[0]), 0.0), W - 1.0), min(max(float(q[1]), 0.0), H - 1.0))
    pts, ends, bases = [_inside(q) for q in pts], [_inside(q) for q in ends], [_inside(q) for q in bases]
    BG = (245, 245, 245)
    faded = Image.blend(img, Image.new("RGB", (W, H), BG), 0.85)      # 팁 둘레를 하얗게 (세트 사진과 같은 느낌)
    pts, ends, bases = pts[:max_tips], ends[:max_tips], bases[:max_tips]
    wids, refs = wids[:max_tips], refs[:max_tips]
    import numpy as np
    arr = np.asarray(img)

    # 팁마다 자기 구역: 팁 축(뿌리 점 ~ 끝 점, 없으면 중심점)이 옆 팁 축보다 가까운 곳
    segs = []
    for k, (x, y) in enumerate(pts):
        e, b = ends[k], bases[k]
        if e and b:
            segs.append(((float(e[0]), float(e[1])), (float(b[0]), float(b[1]))))
        elif e:
            segs.append(((float(e[0]), float(e[1])), (2 * x - float(e[0]), 2 * y - float(e[1]))))
        else:
            segs.append(((x, y), (x, y)))

    def territory(k, box):
        """box(x0, y0, x1, y1) 안에서 k번 팁의 구역 (bool 배열)"""
        x0, y0, x1, y1 = box
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)

        def dist(s):
            (ax, ay), (bx, by) = s
            dx, dy = bx - ax, by - ay
            n = dx * dx + dy * dy
            u = np.clip(((xx - ax) * dx + (yy - ay) * dy) / n, 0, 1) if n > 1e-6 else 0.0
            return np.hypot(xx - (ax + u * dx), yy - (ay + u * dy))
        own = dist(segs[k])
        other = np.full_like(own, np.inf)
        for j in range(len(segs)):
            if j != k:
                other = np.minimum(other, dist(segs[j]))
        return own <= other + terr_margin * L

    def tbox(x, y):
        R = 1.6 * L
        return (int(max(0, x - R)), int(max(0, y - R)), int(min(W, x + R)), int(min(H, y + R)))

    terr = [territory(k, tbox(x, y)) if TIP_TERRITORY and len(pts) >= 2 else None for k, (x, y) in enumerate(pts)]

    def own_image(k):
        """k번 팁 구역 밖(옆 팁)을 하얗게 흐린 세트 사진"""
        if terr[k] is None:
            return img
        box = tbox(*pts[k])
        m = Image.fromarray(terr[k].astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(max(1.0, 0.015 * L)))
        im2 = img.copy()
        im2.paste(Image.composite(img.crop(box), faded.crop(box), m), box[:2])
        return im2

    def local_bg(x, y, R):
        """팁 둘레(반지름 R 원 위)의 배경색 - 모양을 잴 때 원 밖을 이 색으로 채워요 (회색 · 색 배경 사진도 OK)"""
        a = np.linspace(0, 2 * math.pi, 180, endpoint=False)
        xs = np.clip((x + R * np.cos(a)).astype(int), 0, W - 1)
        ys = np.clip((y + R * np.sin(a)).astype(int), 0, H - 1)
        return tuple(int(v) for v in np.median(arr[ys, xs], axis=0))

    # ① 팁마다 모양 재기 (팁 중심 둘레 원 안에서)
    poses, bgs = [], []
    for x, y in pts:
        R = 0.75 * L
        bgs.append(local_bg(x, y, R))
        circ = Image.new("L", (W, H), 0)
        ImageDraw.Draw(circ).ellipse((x - R, y - R, x + R, y + R), fill=255)
        if terr[len(poses)] is not None:           # 옆 팁 구역은 배경색으로 (옆 팁과 한 덩어리로 재지 않게)
            ca = np.array(circ)
            tb = tbox(x, y)
            sub = ca[tb[1]:tb[3], tb[0]:tb[2]]
            sub[~terr[len(poses)]] = 0
            circ = Image.fromarray(ca)
        box = (int(max(0, x - R - 4)), int(max(0, y - R - 4)), int(min(W, x + R + 4)), int(min(H, y + R + 4)))
        mk = tip_shape_mask(Image.composite(img, Image.new("RGB", (W, H), bgs[-1]), circ).crop(box))
        p = tip_pose(mk)
        if p and (p["length"] < min_aspect * p["width"] or p["length"] > 1.7 * L or _cut_by_circle(mk, x - box[0], y - box[1], R)):
            p = None                     # 팁 하나의 모양이 아니에요 (옆 팁과 붙은 덩어리, 원 밖으로 이어짐 등) -> 모양으로 정하지 않아요
        if p:
            p["gx"] += box[0]
            p["gy"] += box[1]
        poses.append(p)
    # 긴 축이 같은 세트의 다른 팁들과 45도 넘게 다르면 잘못 딴 모양으로 봐요 (세트의 팁들은 거의 나란해요)
    for k, p in enumerate(poses):
        others = [q for j, q in enumerate(poses) if q and j != k]
        if p and len(others) >= 2:
            near = sorted(abs(p["vx"] * q["vx"] + p["vy"] * q["vy"]) for q in others)[len(others) // 2]
            if near < math.cos(math.radians(45)):
                poses[k] = None
    # 세트 방향 = 좁은 끝이 확실한 팁들의 손톱 끝 방향 합
    sx = sum(p["vx"] for p in poses if p and p["ratio"] >= TIP_SURE_RATIO)
    sy = sum(p["vy"] for p in poses if p and p["ratio"] >= TIP_SURE_RATIO)
    set_dir = (sx, sy) if math.hypot(sx, sy) > 1e-6 else None

    def cut(src, fsrc, cx, cy, hw, hh, blur, bg=BG, inner=None):
        """(cx, cy) 둘레 hw x hh 만 또렷하게, 둘레(옆 팁 · 판)는 부드럽게 하얗게 -> (잘라낸 사진, 모양 재기용 사진)
        inner: 팁 자체의 반폭 · 반길이 -> 잘라낸 사진 안에서 팁이 차지하는 상자를 info["tip_box"] 로 들고 가요 (붙일 때 씀)"""
        px, py = hw * 1.25, hh * 1.12
        box = (int(max(0, cx - px)), int(max(0, cy - py)), int(min(W, cx + px)), int(min(H, cy + py)))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return None, None
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle((cx - hw, cy - hh, cx + hw, cy + hh), radius=int(hw * 0.8), fill=255)
        one = Image.composite(src, fsrc, m.filter(ImageFilter.GaussianBlur(blur))).crop(box)
        iw, ih = inner or (hw, hh)
        bw, bh = box[2] - box[0], box[3] - box[1]
        one.info["tip_box"] = (max(0.0, cx - iw - box[0]), max(0.0, cy - ih - box[1]),
                               min(bw, cx + iw - box[0]), min(bh, cy + ih - box[1]))
        # 또렷하게 남긴 창 (이 밖은 하얗게 흐림) - 팁이 이 창 끝에 닿으면 직선으로 잘린 거예요
        one.info["cut_rect"] = (max(0.0, cx - hw - box[0]), max(0.0, cy - hh - box[1]),
                                min(bw, cx + hw - box[0]), min(bh, cy + hh - box[1]))
        seg = Image.composite(src, Image.new("RGB", (W, H), bg), m).crop(box)
        return one, seg

    def turned(deg, cx, cy, base=None):
        base = base or img
        if abs(((deg + 180) % 360) - 180) <= 1:
            return base, faded
        return (base.rotate(deg, resample=Image.BICUBIC, center=(cx, cy), fillcolor=BG),
                faded.rotate(deg, resample=Image.BICUBIC, center=(cx, cy), fillcolor=BG))

    def make(k, grow=1.0, use_pose=True):
        grow = grow * grow0
        """k번 팁 후보 하나 -> (팁 사진, 인식 모델이 잰 팁 길이(같은 배율)) 또는 None.
        grow = 자르는 범위 배율, use_pose = 팁 모양으로 잰 방향 · 크기를 쓸지 (False = 인식 모델 점 기준)"""
        x, y = pts[k]
        p, e, b = (poses[k] if use_pose else None), ends[k], bases[k]
        mine = own_image(k)
        # luna 가 인식한 방향: 손톱 뿌리(b) -> 손톱 끝(e). 뿌리가 없으면 중심 -> 끝
        o = b if b and e and math.hypot(e[0] - b[0], e[1] - b[1]) > 1 else (x, y)
        model_dir = (e[0] - o[0], e[1] - o[1]) if e and math.hypot(e[0] - o[0], e[1] - o[1]) > 1 else None
        t = None
        sheet_up = TIP_SHEET_UP and not pointed_set and p is not None and _upright(p["vx"], p["vy"])
        if p and model_dir and not sheet_up:      # 끝 · 뿌리는 luna 대로. 팁 모양의 긴 축은 기울기를 정확히 맞추는 데만 써요
            mn = math.hypot(*model_dir)
            mx, my = model_dir[0] / mn, model_dir[1] / mn
            if abs(p["vx"] * mx + p["vy"] * my) < math.cos(math.radians(35)):
                p = dict(p, vx=mx, vy=my)          # 모양 축이 luna 방향과 많이 다르면 luna 방향 그대로
        if sheet_up:                                   # 세로로 놓인 팁: 사진 위쪽 끝 = 손톱 끝 (기울기만 팁 모양대로)
            vx, vy = (p["vx"], p["vy"]) if p["vy"] < 0 else (-p["vx"], -p["vy"])
        elif p and pointed_set:                        # 뾰족한 세트: 좁은 끝(뾰족한 끝) = 손톱 끝. 애매하면 luna 끝 점 쪽
            vx, vy = p["vx"], p["vy"]
            if p["ratio"] < TIP_SURE_RATIO and model_dir and vx * model_dir[0] + vy * model_dir[1] < 0:
                vx, vy = -vx, -vy
        elif p:
            vx, vy = p["vx"], p["vy"]
            if model_dir:                              # 손톱 끝 쪽 = luna 가 정한 쪽
                if vx * model_dir[0] + vy * model_dir[1] < 0:
                    vx, vy = -vx, -vy
            elif p["ratio"] < TIP_SURE_RATIO:          # luna 방향이 없고 좁은 끝이 애매하면 세트 방향
                if set_dir and vx * set_dir[0] + vy * set_dir[1] < 0:
                    vx, vy = -vx, -vy
            vx, vy = keep_sheet_up(vx, vy)             # 위아래는 세트 사진에 놓인 그대로 (뒤집지 않아요)
        if p:
            deg = math.degrees(math.atan2(vy, vx)) + 90.0       # 손톱 끝 방향 -> 위
            src, fsrc = turned(deg, p["gx"], p["gy"], mine)
            hw = max(0.5 * p["width"] * 1.35, 0.2 * L) * grow
            hh = max(0.5 * p["length"] * 1.12 + 0.04 * L, 0.58 * L) * grow   # 투명한 끝까지 여유 있게
            t, _ = cut(src, fsrc, p["gx"], p["gy"], hw, hh, max(1.5, L * 0.02), inner=(0.5 * p["width"], 0.5 * p["length"]))
        if t is None:
            # ② 모양을 못 땄으면: 인식 모델의 손톱 끝 점 -> 없으면 세트 방향으로 세우고, 같은 규칙으로 한 번 더 확인
            d = model_dir or set_dir
            if pointed_set and d:                      # 뾰족한 세트: luna 끝 점(뾰족한 끝) 방향 그대로
                pass
            else:
                if TIP_SHEET_UP and (d is None or _upright(*d)):     # 세로로 놓인 팁: 사진 위쪽 끝 = 손톱 끝
                    d = (0.0, -1.0) if d is None else ((d[0], d[1]) if d[1] < 0 else (-d[0], -d[1]))
                d = keep_sheet_up(*d) if d else None   # 위아래는 세트 사진에 놓인 그대로 (뒤집지 않아요)
            src, fsrc = turned(math.degrees(math.atan2(d[1], d[0])) + 90.0, x, y, mine) if d else (mine, faded)
            gaps = [math.hypot(pts[j][0] - x, pts[j][1] - y) for j in (k - 1, k + 1) if 0 <= j < len(pts)]
            hw = max(0.30 * L, 0.5 * min(gaps) if gaps else 0.4 * L) * grow
            # 팁이 있을 법한 상자: 폭은 luna 가 잰 팁 폭 (없으면 예전처럼 옆 팁 간격), 길이는 자르는 범위 배율만큼 여유
            iw_ = 0.55 * float(wids[k]) if wids[k] else 0.8 * hw / grow
            one, seg = cut(src, fsrc, x, y, hw, 0.66 * L * grow, max(1.5, L * 0.03), bgs[k],
                           inner=(min(iw_, 0.8 * hw), 0.5 * L * grow0))
            if one is None:
                return None
            # 여기 온 건 모양을 믿을 수 없어서예요 -> 뒤집지 않고 기울기만 조금(20도까지) 고쳐요
            #   (튀어나온 3D 파츠 · 옆 글자가 모양에 섞여도 방향이 뒤집히거나 크게 기울지 않게)
            t = orient_tip(one, seg, max_turn=10 if model_dir else 20, keep_up=True)
        # 인식 모델이 잰 팁 길이 (손톱 끝 점 ~ 뿌리 점, 투명한 끝 포함) - 팁 따기에서 투명한 끝이 빠졌을 때 보충용
        luna_len = math.hypot(e[0] - b[0], e[1] - b[1]) if e and b and not no_luna_len else None
        luna_wid = float(wids[k]) if wids[k] else None    # luna 가 잰 팁 폭 (같은 배율로 같이 키워요)
        f_ = 1.0
        if t.height < 512:         # 작으면 키워서 보내요 (장식이 뭉개지지 않게)
            box, f = t.info.get("tip_box"), 512 / t.height
            cr_ = t.info.get("cut_rect")
            t = t.resize((max(1, round(t.width * f)), 512), Image.LANCZOS)
            if box:
                t.info["tip_box"] = tuple(v * f for v in box)
            if cr_:
                t.info["cut_rect"] = tuple(v * f for v in cr_)
            if luna_len:
                luna_len *= f
            if luna_wid:
                luna_wid *= f
            f_ = f
        bx_ = t.info.get("tip_box")                 # 자른 가운데 = 팁 가운데 (기준 모양을 놓는 자리 - 자기 모양을 못 믿을 때)
        cut_c = ((bx_[0] + bx_[2]) / 2, (bx_[1] + bx_[3]) / 2) if bx_ else (t.width / 2, t.height / 2)
        cut_r = t.info.get("cut_rect")
        t = tip_clean(t, luna_len, shape, tlen) if TIP_CLEAN else t
        t.info["cut_center"] = cut_c
        t.info["bg_color"] = bgs[k]        # 팁 둘레 배경색 (직선으로 잘림 판별)
        t.info["cut_rect"] = cut_r
        t.info["luna_len"] = luna_len
        t.info["luna_wid"] = luna_wid
        t.info["px_per_set"] = f_ * img_scale      # 이 팁 사진 픽셀 / 세트 사진 픽셀 (팁 크기를 모두 같게 맞출 때)
        t = tip_union_color(t)                     # 팁 따기가 팁보다 작게 땄으면 팁 색 덩어리까지 넓혀요
        return t, luna_len

    # 팁마다 후보를 만들고 점수로 골라요 (리랭커처럼). 첫 후보가 괜찮으면 그대로 - 의심스러울 때만 후보를 더 만들어요
    firsts = [make(k) for k in range(len(pts))]
    if any(c is None for c in firsts):
        return []
    if not TIP_RERANK:
        out = point_tips_up([c[0] for c in firsts])
        out = apply_tip_shape(out, refs, shape, (hints or {}).get("ref_pick"), measure) if template and TIP_TEMPLATE else out
        return same_size_tips(out) if template and TIP_SAME_SIZE else out
    shapes = [tip_check(*c) for c in firsts]
    good = [s["aspect"] for s in shapes if s and s["penalty"] <= 0.5]
    med = sorted(good)[len(good) // 2] if len(good) >= 2 else None
    out = []
    for k, c0 in enumerate(firsts):
        best, best_s = c0, tip_score(shapes[k], med)
        if best_s < TIP_RERANK_OK:
            tried = []
            for grow, use_pose in TIP_RERANK_TRIES:
                if use_pose and poses[k] is None:
                    continue                   # 모양을 못 잰 팁: 모양 기준 후보 = 점 기준 후보와 같아요
                c = make(k, grow, use_pose)
                if c is None:
                    continue
                s = tip_score(tip_check(*c), med)
                tried.append(round(s, 2))
                if s > best_s + 0.05:
                    best, best_s = c, s
            print(f"  [팁 다시 고르기] {k + 1}번 팁: 첫 후보 점수 {tip_score(shapes[k], med):.2f} -> "
                  f"후보 {tried} 중 {best_s:.2f}" + (" (그대로)" if best is c0 else ""))
            if best_s < TIP_RERANK_WARN:
                best[0].info["tip_doubt"] = True     # 후보 모두 별로예요 - 화면에 '확인 필요'
        best[0].info["tip_score"] = best_s
        out.append(best[0])

    # ③ 그래도 점수가 낮은 팁은 인식 모델(luna)에 세트 사진 + 잘린 결과를 보여주고 팁 위치를 다시 받아 한 번 더 잘라요
    doubt = [k for k, o in enumerate(out) if o.info.get("tip_score", 0) < TIP_VERIFY_BELOW]
    if verify and TIP_VERIFY and doubt:
        try:
            fix = verify(img, pts, ends, bases, L, doubt, [out[k] for k in doubt]) or {}
        except Exception as e:
            print(f"  ! 팁 다시 확인(luna) 실패 - 그대로 써요: {type(e).__name__}: {str(e)[:150]}")
            fix = {}
        if fix:
            p2, e2, b2 = list(pts), list(ends), list(bases)
            for k, f in fix.items():
                p2[k], e2[k], b2[k] = f["pt"], f["end"], f["base"]
            again = tip_crops(img, dict(hints, pts=p2, ends=e2, bases=b2, wids=wids, refs=refs, hi=None), max_tips,
                              template=False)      # 기준 모양 맞추기는 아래에서 한 번만
            if len(again) == len(out):
                for k in fix:
                    s_old, s_new = out[k].info.get("tip_score", 0), again[k].info.get("tip_score", 0)
                    use = s_new > s_old + 0.1
                    print(f"  [팁 다시 확인] {k + 1}번 팁: luna 위치로 다시 자름 점수 {s_old:.2f} -> {s_new:.2f}"
                          + (" (바꿈)" if use else " (그대로)"))
                    if use:
                        out[k] = again[k]
    # ④ 둥글지 않고 직선으로 잘린 팁 -> 올린 원본(고해상도 세트 사진)에서 그 팁만 다시 잘라 모양 · 디자인을 다듬어요
    #    직선으로 잘렸다 = 팁이 생각보다 커서 자르는 창 밖으로 나갔다 -> 창을 단계적으로 넓히고, luna 길이는 믿지 않아요
    hi = (hints or {}).get("hi")
    if hi is None and template:          # 더 큰 원본이 없으면 같은 사진에서 창만 넓혀 다시 잘라요
        hi, hints = img, dict(hints or {}, hi_scale=1.0)
    cut_bad = [k for k, o in enumerate(out) if tip_cut_straight(o)] if TIP_HI_RECUT and hi is not None else []
    if cut_bad:
        s = float(hints.get("hi_scale") or 1.0)
        sc = lambda q: None if q is None else (q[0] * s, q[1] * s)
        h_hi = {kk: v for kk, v in hints.items() if kk not in ("hi", "hi_scale")}
        h_hi.update(pts=[sc(p) for p in pts], ends=[sc(p) for p in ends], bases=[sc(p) for p in bases],
                    wids=[(w or 0) * s for w in wids], refs=refs, len=L * s, no_luna_len=True, img_scale=s,
                    terr_margin=TIP_HI_TERR_MARGIN)
        left = list(cut_bad)
        best = {}
        for g in TIP_HI_GROWS:
            if not left:
                break
            try:
                again = tip_crops(hi, dict(h_hi, grow0=g), max_tips, template=False)
            except Exception as e:
                print(f"  ! 원본에서 다시 자르기 실패: {type(e).__name__}: {e}")
                break
            if len(again) != len(out):
                break
            for k in list(left):
                c = tip_reclean(again[k], shape, tlen)     # 넓은 창: 팁 상자를 팁 색으로 다시 잡고 둘레 다시 지우기
                sc_ = c.info.get("tip_score", tip_score(tip_check(c)))
                if not tip_cut_straight(c):
                    best[k] = (c, sc_, g, True)
                    left.remove(k)
                elif k not in best or sc_ > best[k][1]:
                    best[k] = (c, sc_, g, False)
        for k in cut_bad:
            if k not in best:
                continue
            c, sc_, g, ok_ = best[k]
            print(f"  [원본에서 다시 자르기] {k + 1}번 팁: 직선으로 잘림 -> 원본 {s:.1f}배 해상도 · 창 {g}배로 다시 자름 -> "
                  + ("둥글게 잘림 (바꿈)" if ok_ else "여전히 잘림 (그대로)"))
            if ok_:
                out[k] = c
    out = point_tips_up(out)
    if template and TIP_TEMPLATE:
        out = apply_tip_shape(out, refs, shape, (hints or {}).get("ref_pick"), measure)
    if template and TIP_SAME_SIZE:      # 세트의 팁 5개를 가장 큰 팁 크기 기준으로 같은 크기 · 같은 배율로
        out = same_size_tips(out)
    return out


TIP_POINT_UP = True           # 끝이 뾰족한 팁은 뾰족한 끝 = 손톱 끝 (세트 사진에 거꾸로 놓였어도 '사진 위 = 손톱 끝' 규칙보다 먼저)
TIP_POINT_RATIO = 1.6         # 양 끝 폭이 이 배 넘게 차이 나면 '뾰족한 팁'
TIP_POINT_WEAK = 1.3          # 세트 대부분이 거꾸로였을 때 같이 돌리는 덜 뾰족한 팁의 최소 폭 차이


def _tip_end_widths(t):
    """잘라 세운 팁 -> (위 끝 폭, 아래 끝 폭) (팁 모양 마스크로) 또는 None."""
    import numpy as np
    mk = t.info.get("tip_mask")
    if mk is None:
        return None
    ys, xs = np.nonzero(mk)
    if len(xs) < 200:
        return None
    y0, y1 = ys.min(), ys.max()
    L = float(y1 - y0 + 1)
    w = lambda f: float((mk[int(y0 + f * L)] > 0).sum())
    return (w(0.06) + w(0.12)) / 2, (w(0.88) + w(0.94)) / 2


def _turn180(t):
    """팁 사진을 180도 돌려요 (팁 모양 · 상자 · 지우기 전 사진도 같이)."""
    import numpy as np
    out = t.rotate(180)
    out.info.update({k: v for k, v in t.info.items() if k not in ("tip_mask", "tip_box", "tip_raw")})
    w, h = t.size
    if t.info.get("tip_mask") is not None:
        out.info["tip_mask"] = np.ascontiguousarray(t.info["tip_mask"][::-1, ::-1])
    if t.info.get("tip_box"):
        x0, y0, x1, y1 = t.info["tip_box"]
        out.info["tip_box"] = (w - x1, h - y1, w - x0, h - y0)
    if t.info.get("tip_raw") is not None:
        out.info["tip_raw"] = t.info["tip_raw"].rotate(180)
    if t.info.get("cut_rect"):
        x0, y0, x1, y1 = t.info["cut_rect"]
        out.info["cut_rect"] = (w - x1, h - y1, w - x0, h - y0)
    if t.info.get("cut_center"):
        out.info["cut_center"] = (w - t.info["cut_center"][0], h - t.info["cut_center"][1])
    out.info["point_flipped"] = True
    return out


def point_tips_up(tips):
    """뾰족한 팁은 뾰족한 끝이 위(= 손톱 끝)로 오게 돌려요. 세트의 뾰족한 팁 대부분이 거꾸로였으면
    (세트를 거꾸로 놓고 찍음) 덜 뾰족한 팁도 좁은 끝이 아래면 같이 돌려요."""
    if not TIP_POINT_UP or not tips:
        return tips
    ws = [_tip_end_widths(t) for t in tips]
    strong = [i for i, v in enumerate(ws) if v and max(v) >= TIP_POINT_RATIO * max(1.0, min(v))]
    down = [i for i in strong if ws[i][1] < ws[i][0]]
    set_down = len(strong) >= 2 and len(down) > len(strong) / 2
    out = list(tips)
    for i, v in enumerate(ws):
        if not v or v[1] >= v[0]:
            continue                                   # 좁은 끝이 이미 위
        if i in strong or (set_down and v[0] >= TIP_POINT_WEAK * max(1.0, v[1])):   # 거의 같은 폭이면 그대로 (투명한 끝일 수 있어요)
            out[i] = _turn180(tips[i])
            print(f"  [뾰족한 끝 = 손톱 끝] {i + 1}번 팁을 180도 돌려요 (좁은 끝이 아래였어요: 폭 {v[0]:.0f} / {v[1]:.0f})")
    return out


TIP_HI_RECUT = True           # 직선으로 잘린 팁은 올린 원본(고해상도)에서 그 팁만 다시 잘라요
TIP_CUT_COLOR_DIFF = 60       # 창 끝 띠의 픽셀이 배경색과 이만큼(RGB 차이 합) 넘게 다르면 '팁 색'
TIP_UNION_COLOR = True        # 팁 따기가 팁보다 작으면 팁 색 덩어리까지 넓혀요
TIP_CUT_SHARE = 0.10          # 창 끝 띠를 가로지르는 팁 색 줄이 창 폭(옆은 창 길이)의 이 비율을 넘으면 그 면에서 직선으로 잘림
TIP_HI_GROWS = (1.3, 1.6)     # 원본에서 다시 자를 때 자르는 범위 배율 - 차례로 넓혀 봐요 (팁이 창 밖으로 나가 직선으로 잘리지 않게)
TIP_HI_TERR_MARGIN = 0.10     # 원본에서 다시 자를 때 옆 팁과의 경계를 팁 길이의 이만큼 넉넉히 (경계 직선에 잘리지 않게)


def tip_hits_window(t, tol=0.025):
    """팁이 또렷하게 남긴 창(cut_rect) 끝까지 이어진 면 목록 (= 창 밖으로 나가 직선으로 잘림).
    창 안쪽 끝 띠(가운데 부분)의 픽셀이 배경색과 다르면(팁 색이면) 그 면에서 잘린 거예요. 팁 모양 마스크는 안 써요."""
    import numpy as np
    r, bg = t.info.get("cut_rect"), t.info.get("bg_color")
    src = t.info.get("tip_raw")
    src = src if src is not None else t
    if not r or bg is None:
        return _hits_by_mask(t, tol)
    a = np.asarray(src.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in r)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 10 or y1 - y0 < 10:
        return []
    d = max(2, int(0.02 * (y1 - y0)))
    cw, chh = (x1 - x0) // 4, (y1 - y0) // 4
    strips = {"위": a[y0 + 1:y0 + 1 + d, x0 + cw:x1 - cw], "아래": a[y1 - 1 - d:y1 - 1, x0 + cw:x1 - cw],
              "왼쪽": a[y0 + chh:y1 - chh, x0 + 1:x0 + 1 + d], "오른쪽": a[y0 + chh:y1 - chh, x1 - 1 - d:x1 - 1]}
    # 배경 = 둘레 배경색 · 하얗게 흐린 배경색(옆 팁 구역) · 흐림 바탕색 - 이 중 어디에도 안 가까우면 '팁 색'
    bgv = np.array(bg, np.float32)
    refs_ = np.stack([bgv, 0.15 * bgv + 0.85 * 245.0, np.full(3, 245.0, np.float32)]).astype(np.int16)
    hits = []
    for nm, st_ in strips.items():
        if st_.size == 0:
            continue
        dist = np.min(np.stack([np.abs(st_ - r_).sum(axis=2) for r_ in refs_]), axis=0)
        diff = dist > TIP_CUT_COLOR_DIFF
        # 띠를 가로지르는 팁 색 줄 수 (위 · 아래 = 세로줄, 옆 = 가로줄) 가 창 크기의 TIP_CUT_SHARE 넘으면 잘림
        lines = (diff.mean(axis=0) > 0.5).sum() if nm in ("위", "아래") else (diff.mean(axis=1) > 0.5).sum()
        size = (x1 - x0) if nm in ("위", "아래") else (y1 - y0)
        if lines > TIP_CUT_SHARE * size:
            hits.append(nm)
    return hits


def _hits_by_mask(t, tol=0.025):
    """팁 윤곽이 또렷하게 남긴 창(cut_rect)의 위 · 아래 · 옆 끝에 닿은 면 목록 (= 팁이 창 밖으로 이어져 직선으로 잘림)."""
    mk, r = t.info.get("tip_mask"), t.info.get("cut_rect")
    if mk is None or not r:
        return []
    b = _mask_box(mk)
    if not b:
        return []
    h, w = mk.shape
    dy, dx = tol * h, tol * w
    hits = []
    if b[1] <= r[1] + dy and r[1] > 1:
        hits.append("위")
    if b[3] >= r[3] - dy and r[3] < h - 1:
        hits.append("아래")
    if b[0] <= r[0] + dx and r[0] > 1:
        hits.append("왼쪽")
    if b[2] >= r[2] - dx and r[2] < w - 1:
        hits.append("오른쪽")
    return hits


def tip_union_color(t):
    """팁 따기(GrabCut)가 팁보다 작게 땄을 때: 팁 모양을 그 자리의 팁 색 덩어리(둘레 배경 · 흐린 배경과 다른 곳)까지 넓혀요.
    또렷한 창(cut_rect) 안에서만, 팁 모양과 겹치는 덩어리만 (흐린 옆 팁은 배경으로 봐요). 투명한 부분은 그대로."""
    import cv2, numpy as np
    from PIL import Image
    mk, bg = t.info.get("tip_mask"), t.info.get("bg_color")
    if mk is None or bg is None or not TIP_UNION_COLOR:
        return t
    raw = t.info.get("tip_raw")
    raw = raw if raw is not None else t
    a = np.asarray(raw.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    if mk.shape != (h, w):
        return t
    bgv = np.array(bg, np.float32)
    refs_ = np.stack([bgv, 0.15 * bgv + 0.85 * 245.0, np.full(3, 245.0, np.float32)]).astype(np.int16)
    dist = np.min(np.stack([np.abs(a - r_).sum(axis=2) for r_ in refs_]), axis=0)
    fg = (dist > TIP_CUT_COLOR_DIFF).astype(np.uint8)
    r = t.info.get("cut_rect")
    if r:
        lim = np.zeros_like(fg)
        lim[max(0, int(r[1])):int(np.ceil(r[3])), max(0, int(r[0])):int(np.ceil(r[2]))] = 1
        fg &= lim
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab = cv2.connectedComponents(fg)
    if n < 2:
        return t
    ov = np.bincount(lab[mk > 0].ravel(), minlength=n)
    ov[0] = 0
    c = int(np.argmax(ov))
    if ov[c] < 0.3 * float((mk > 0).sum()):
        return t                                           # 팁 모양과 별로 안 겹쳐요 -> 다른 물체
    comp = lab == c
    if comp.sum() > 2.5 * float((mk > 0).sum()):
        return t                                           # 너무 커요 (배경과 색이 비슷한 판 등)
    cs, _ = cv2.findContours(np.maximum(mk > 0, comp).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return t
    hull = cv2.convexHull(np.vstack(cs))
    new = np.zeros((h, w), np.uint8)
    cv2.fillPoly(new, [hull], 255)
    if (new > 0).sum() <= 1.02 * (mk > 0).sum():
        return t                                           # 거의 같아요
    bx, by, bw, bh = cv2.boundingRect(hull)
    keep = cv2.dilate(new, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    keep = cv2.GaussianBlur(keep, (0, 0), max(1.0, w / 150.0))
    out = Image.composite(raw.convert("RGB"), Image.new("RGB", (w, h), TIP_BG_COLOR), Image.fromarray(keep))
    out.info.update({k: v for k, v in t.info.items() if k not in ("tip_mask", "tip_box")})
    out.info.update(tip_mask=new, tip_box=(float(bx), float(by), float(bx + bw), float(by + bh)), tip_raw=raw)
    return out


def tip_reclean(t, shape=None, tlen=None):
    """잘라 세운 팁 -> 팁 색 픽셀(둘레 배경 · 흐린 배경과 다른 곳)의 가운데 덩어리로 '팁 상자'를 다시 잡고 둘레를 다시 지운 팁.
    원본에서 넓게 다시 자른 팁에 써요 (넓은 창에서는 처음 상자가 너무 커서 배경까지 팁으로 따요). 못 하면 t 그대로."""
    import cv2, numpy as np
    raw = t.info.get("tip_raw")
    raw = raw if raw is not None else t
    bg = t.info.get("bg_color")
    if bg is None:
        return t
    a = np.asarray(raw.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    bgv = np.array(bg, np.float32)
    refs_ = np.stack([bgv, 0.15 * bgv + 0.85 * 245.0, np.full(3, 245.0, np.float32)]).astype(np.int16)
    dist = np.min(np.stack([np.abs(a - r_).sum(axis=2) for r_ in refs_]), axis=0)
    fg = (dist > TIP_CUT_COLOR_DIFF).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(fg)
    if n < 2:
        return t
    cx, cy = w / 2, h / 2
    c = lab[int(cy), int(cx)] or max(range(1, n), key=lambda i: st[i, cv2.CC_STAT_AREA])
    x, y, bw, bh = st[c, 0], st[c, 1], st[c, 2], st[c, 3]
    if bw * bh < 0.01 * w * h:
        return t
    py = 0.06 * bh                                   # 투명한 끝은 색이 옅어요 - 길이 쪽으로 조금 여유
    src = raw.copy()
    src.info.update({k: v for k, v in t.info.items() if k not in ("tip_mask", "tip_raw", "tip_box")})
    src.info["tip_box"] = (float(x), max(0.0, y - py), float(x + bw), min(float(h), y + bh + py))
    out = tip_clean(src, None, shape, tlen)
    if out.info.get("tip_mask") is None:
        return t
    # 팁 따기가 가는 끝(뾰족한 끝 등)을 놓쳐도 팁 색 덩어리는 다 들어가게 (둘을 합친 볼록 껍질)
    comp = (lab == c).astype(np.uint8)
    cs, _ = cv2.findContours(np.maximum(out.info["tip_mask"] > 0, comp > 0).astype(np.uint8),
                             cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if cs:
        hull = cv2.convexHull(np.vstack(cs))
        mk = np.zeros((h, w), np.uint8)
        cv2.fillPoly(mk, [hull], 255)
        bx, by, bw2, bh2 = cv2.boundingRect(hull)
        out.info["tip_mask"] = mk
        out.info["tip_box"] = (float(bx), float(by), float(bx + bw2), float(by + bh2))
    out.info.update({k: v for k, v in t.info.items() if k not in ("tip_mask", "tip_raw", "tip_box", "tip_shape")})
    return out


def tip_cut_straight(t):
    """잘라 세운 팁이 둥글지 않고 직선으로 잘렸는지: 팁 모양을 못 땄거나, 팁 윤곽이 사진 가장자리에 닿았거나(잘림),
    손톱 끝 · 뿌리가 칼로 자른 듯 평평함 (스퀘어 · 코핀의 손톱 끝은 원래 평평해서 tip_check 가 빼요)."""
    chk = tip_check(t)
    if chk is None:
        return True
    if any(n.startswith(("가장자리", "끝 평평", "뿌리 평평")) for n in chk["notes"]):
        return True
    return bool(tip_hits_window(t))


TIP_SAME_SIZE = True          # 한 세트의 팁 5개를 같은 크기 · 같은 배율로 (가장 큰 팁의 자르는 크기 기준)
TIP_SAME_HEIGHT = 640         # 같게 맞춘 팁 사진의 높이 (px)
TIP_MASK_SMOOTH = 0.04        # 팁 모양 모서리를 둥글게 하는 정도 (팁 폭 대비, 0 = 안 함)


def same_size_tips(tips, height=None):
    """세트의 팁 사진들 -> 모두 같은 크기의 사진, 같은 배율 (세트 사진 1px = 같은 px).
    사진 크기 = 팁들 중 가장 큰 자르는 크기(세트 사진 기준), 팁은 가운데. 팁 크기 차이(엄지 > 새끼)는 그대로 보여요."""
    import numpy as np
    from PIL import Image
    height = height or TIP_SAME_HEIGHT
    scs = [t.info.get("px_per_set") for t in tips]
    if len(tips) < 2 or any(not s for s in scs):
        return tips
    Wset = max(t.width / s for t, s in zip(tips, scs))
    Hset = max(t.height / s for t, s in zip(tips, scs))
    S = height / Hset
    CW, CH = max(8, round(Wset * S)), height
    out = []
    for t, s in zip(tips, scs):
        k = S / s
        nw, nh = max(1, round(t.width * k)), max(1, round(t.height * k))
        box = t.info.get("tip_box") or (0, 0, t.width, t.height)
        ox = int(round(CW / 2 - (box[0] + box[2]) / 2 * k))
        oy = int(round(CH / 2 - (box[1] + box[3]) / 2 * k))

        def place(im, fill, res=Image.LANCZOS):
            c = Image.new(im.mode, (CW, CH), fill)
            c.paste(im.resize((nw, nh), res), (ox, oy))
            return c
        n = place(t.convert("RGB"), TIP_BG_COLOR)
        info = dict(t.info)
        mv = lambda b: (b[0] * k + ox, b[1] * k + oy, b[2] * k + ox, b[3] * k + oy)
        if info.get("tip_raw") is not None:
            info["tip_raw"] = place(info["tip_raw"].convert("RGB"), (245, 245, 245))
        if info.get("tip_mask") is not None:
            info["tip_mask"] = np.asarray(place(Image.fromarray(info["tip_mask"]), 0, Image.NEAREST)).copy()
        for kk in ("tip_box", "cut_rect"):
            if info.get(kk):
                info[kk] = mv(info[kk])
        if info.get("cut_center"):
            info["cut_center"] = (info["cut_center"][0] * k + ox, info["cut_center"][1] * k + oy)
        for kk in ("luna_len", "luna_wid"):
            if info.get(kk):
                info[kk] = info[kk] * k
        info["px_per_set"] = S
        n.info.update(info)
        out.append(n)
    print(f"  [팁 크기 맞추기] {len(tips)}개 모두 {CW}x{CH}px · 같은 배율 (가장 큰 자르는 크기 기준)")
    return out


TIP_TEMPLATE = True          # 세트에서 기준 팁 하나(luna 가 고름)의 팁 모양을 나머지 팁에도 그대로 써서 잘라요 (크기만 팁별로)
TIP_TEMPLATE_MIN_SCORE = -0.6 # 기준 팁이 될 수 있는 최소 점수 (이보다 낮으면 모양을 믿을 수 없어요)
TIP_TEMPLATE_KEEP_IOU = 0.5   # 자기 팁 모양이 믿을 만한데 기준 모양과 이보다 덜 겹치면 자기 모양 그대로 (luna 크기 · 위치가 틀렸을 때)


def _mask_box(mk):
    import numpy as np
    ys, xs = np.nonzero(mk)
    if len(xs) < 50:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def apply_tip_template(tips, refs=None):
    """세트의 팁 사진들(세운 뒤) -> 기준 팁 하나의 모양으로 모두 다시 딴 팁 사진들.
    ① 기준 팁: luna 가 고른 팁(refs 에서 True) - 그 팁 모양을 못 땄거나 점수가 낮으면 점수가 가장 좋은 팁
    ② 크기: 기준 팁의 실제 크기 / luna 가 잰 기준 팁 크기 = 비율 -> 팁마다 luna 가 잰 길이 · 폭 x 비율
       (luna 의 절대 크기보다 팁 사이 크기 비율이 더 정확해요. 폭을 모르면 기준 팁의 길이/폭 비율)
    ③ 자리: 자기 팁 모양이 있으면 그 가운데(길이가 많이 다르면 뿌리 쪽 끝에 맞춤 - 투명한 손톱 끝이 빠지기 쉬워요), 없으면 팁 상자 가운데
    방향은 이미 세워 둔 그대로 (손톱 끝 = 위). 디자인 픽셀은 바꾸지 않고 둘레 지우기 모양만 바꿔요."""
    import cv2, numpy as np
    from PIL import Image
    if not tips or len(tips) < 2:
        return tips
    refs = list(refs or [])
    scores = []
    for t in tips:
        s = t.info.get("tip_score")
        if s is None:
            s = tip_score(tip_check(t))
        scores.append(s)
    ok = [k for k, t in enumerate(tips) if t.info.get("tip_mask") is not None and _mask_box(t.info["tip_mask"])
          and scores[k] >= TIP_TEMPLATE_MIN_SCORE and not t.info.get("tip_doubt")]
    if not ok:
        print("  [기준 모양] 모양을 믿을 만한 팁이 없어요 -> 팁마다 따로 딴 그대로")
        return tips
    luna_ref = next((k for k, r in enumerate(refs) if r), None)
    ref = luna_ref if luna_ref in ok else max(ok, key=lambda k: scores[k])
    rt = tips[ref]
    rx0, ry0, rx1, ry1 = _mask_box(rt.info["tip_mask"])
    tmpl = (rt.info["tip_mask"][ry0:ry1, rx0:rx1] > 0).astype(np.uint8) * 255
    rl, rw = float(ry1 - ry0), float(rx1 - rx0)
    kl = rl / rt.info["luna_len"] if rt.info.get("luna_len") else None     # 실제 길이 / luna 길이
    kw = rw / rt.info["luna_wid"] if rt.info.get("luna_wid") else None
    print(f"  [기준 모양] {ref + 1}번 팁 모양으로 세트를 다시 따요"
          + ("" if luna_ref is None or luna_ref == ref else f" (luna 가 고른 {luna_ref + 1}번은 모양을 못 따서 대신)")
          + f"  길이/폭 {rl / rw:.2f}")
    out = []
    for k, t in enumerate(tips):
        if k == ref:
            out.append(t)
            continue
        raw = t.info.get("tip_raw")
        raw = raw if raw is not None else t.convert("RGB")
        W, H = raw.size
        own = t.info.get("tip_mask")
        ob = _mask_box(own) if own is not None else None
        # 크기
        th = kl * t.info["luna_len"] if kl and t.info.get("luna_len") else (ob[3] - ob[1] if ob else None)
        if not th:
            print(f"    {k + 1}번 팁: 크기를 몰라요 -> 그대로")
            out.append(t)
            continue
        tw = kw * t.info["luna_wid"] if kw and t.info.get("luna_wid") else th * rw / rl
        tw = max(0.5 * th * rw / rl, min(tw, 1.6 * th * rw / rl))   # luna 폭이 엉뚱하면 기준 비율 ±
        th, tw = min(th, 0.98 * H), min(tw, 0.98 * W)
        ih, iw = max(8, int(round(th))), max(8, int(round(tw)))
        # 자리
        if ob and scores[k] >= TIP_TEMPLATE_MIN_SCORE:      # 자기 모양을 믿을 수 있을 때만 그 자리에
            cx = (ob[0] + ob[2]) / 2
            olen = ob[3] - ob[1]
            if abs(olen - ih) <= 0.15 * ih:
                y0 = (ob[1] + ob[3]) / 2 - ih / 2
            else:
                y0 = ob[3] - ih                        # 뿌리 쪽 끝에 맞춰요
        else:                                               # 자른 가운데(luna 위치 · 팁 모양 중심)에
            cc = t.info.get("cut_center") or (W / 2, H / 2)
            cx, y0 = cc[0], cc[1] - ih / 2
        x0 = int(round(min(max(cx - iw / 2, 0), W - iw)))
        y0 = int(round(min(max(y0, 0), H - ih)))
        mk = np.zeros((H, W), np.uint8)
        mk[y0:y0 + ih, x0:x0 + iw] = (cv2.resize(tmpl, (iw, ih), interpolation=cv2.INTER_LINEAR) >= 128) * 255
        if own is not None and scores[k] >= TIP_TEMPLATE_MIN_SCORE:
            a, b = mk > 0, own > 0
            iou = float((a & b).sum()) / max(1.0, float((a | b).sum()))
            if iou < TIP_TEMPLATE_KEEP_IOU:
                print(f"    {k + 1}번 팁: 기준 모양과 자기 모양이 많이 달라요 (겹침 {iou:.2f}) -> 자기 모양 그대로")
                out.append(t)
                continue
        keep = cv2.dilate(mk, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        keep = cv2.GaussianBlur(keep, (0, 0), max(1.0, W / 150.0))
        n = Image.composite(raw, Image.new("RGB", raw.size, TIP_BG_COLOR), Image.fromarray(keep))
        n.info.update({kk: v for kk, v in t.info.items() if kk not in ("tip_mask", "tip_box", "tip_raw")})
        n.info.update(tip_mask=mk, tip_box=(float(x0), float(y0), float(x0 + iw), float(y0 + ih)), tip_raw=raw,
                      tip_template=ref + 1)
        print(f"    {k + 1}번 팁: 기준 모양 {iw}x{ih}px" + (f" (자기 모양 {ob[2] - ob[0]}x{ob[3] - ob[1]})" if ob else ""))
        out.append(n)
    return out


NAIL_REF_USE = True           # 팁 모양은 nail_ref 폴더의 표본만 써요 (세트마다 표본 하나를 골라 팁마다 돌리기 · 좌우 · 상하 늘리기만)
NAIL_REF_DIR = os.path.join(ROOT, "nail_ref")
NAIL_REF_FREE_EDGE = "bottom" # 표본 사진에서 손톱 끝(free edge)이 있는 쪽 ("bottom" = 아래 -> 위로 뒤집어 써요)
NAIL_REF_MAX_TILT = 30.0      # 팁 기울기를 이 각도(도)까지만 맞춰요
NAIL_REF_MIN_HEIGHT = 512     # 표본이 이보다 작으면(예: 33x97) 모양 · 비율 그대로 이 높이까지만 확대 (팁 디자인이 뭉개지지 않게)
NAIL_REF_KEEP_RATIO = True    # 팁을 표본에 넣을 때 길이 · 폭 비율 그대로 (디자인을 억지로 늘리지 않아요). False = 표본 상자에 꽉 차게 늘려요
NAIL_REF_FILL = True          # 표본 안인데 팁이 없는 곳(배경 · 옆 팁)을 팁의 바탕색으로 자연스럽게 메워요
NAIL_REF_PICK_MARGIN = 0.08   # luna 가 고른 표본이 프로그램 비교 1등보다 이만큼 넘게 덜 닮았으면 프로그램 비교를 따라요
NAIL_REF_PAD = 0.04           # 표본 틀 둘레 여백 (표본 높이 대비)
_NAIL_REFS = None


def load_nail_refs():
    """nail_ref 폴더의 표본(투명 배경 PNG) -> [(이름, 팁 모양 bool 배열 - 상자에 꼭 맞게, 손톱 끝 = 위)]. 한 번만 읽어요."""
    global _NAIL_REFS
    if _NAIL_REFS is not None:
        return _NAIL_REFS
    import glob, cv2, numpy as np
    from PIL import Image
    refs = []
    for p in sorted(glob.glob(os.path.join(NAIL_REF_DIR, "*.png"))):
        try:
            a = np.asarray(Image.open(p).convert("RGBA"))[..., 3]
        except Exception as e:
            print(f"  ! nail_ref 표본을 못 읽었어요: {os.path.basename(p)} ({e})")
            continue
        m = (a >= 128).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(m)
        if n < 2:
            continue
        big = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
        mk = (lab == big).astype(np.uint8)
        fl = np.pad(mk, 1)                                        # 속 채우기 (반사광 등으로 구멍이 있어도)
        cv2.floodFill(fl, np.zeros((fl.shape[0] + 2, fl.shape[1] + 2), np.uint8), (0, 0), 1)
        mk = np.maximum(mk, 1 - fl[1:-1, 1:-1])
        ys, xs = np.nonzero(mk)
        mk = mk[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0
        if NAIL_REF_FREE_EDGE == "bottom":
            mk = mk[::-1]
        refs.append((os.path.splitext(os.path.basename(p))[0], np.ascontiguousarray(mk)))
    _NAIL_REFS = refs
    print(f"  (nail_ref 표본 {len(refs)}개: {', '.join(n for n, _ in refs)})")
    return refs


def _norm_mask(m, size=(64, 128)):
    import cv2, numpy as np
    b = _mask_box(np.asarray(m, np.uint8))
    if not b:
        return None
    c = np.asarray(m, np.uint8)[b[1]:b[3], b[0]:b[2]] * 255
    return cv2.resize(c, size, interpolation=cv2.INTER_AREA) >= 128


def _iou(a, b):
    return float((a & b).sum()) / max(1.0, float((a | b).sum()))


def _mask_frame(mk):
    """팁 모양 -> (가운데 x, y, 기울기(도, + = 위쪽이 오른쪽으로), 길이, 폭) - 긴 축 기준. 못 재면 None."""
    import math
    import numpy as np
    ys, xs = np.nonzero(mk)
    if len(xs) < 50:
        return None
    pts = np.stack([xs, ys], 1).astype(np.float64)
    mu = pts.mean(0)
    w_, v_ = np.linalg.eigh(np.cov((pts - mu).T))
    ux, uy = v_[:, int(np.argmax(w_))]
    if uy > 0:
        ux, uy = -ux, -uy                                         # 위쪽(손톱 끝)을 향하게
    tilt = math.degrees(math.atan2(ux, -uy))
    if abs(tilt) > NAIL_REF_MAX_TILT:                             # 긴 축이 이상하면 세운 그대로
        ux, uy, tilt = 0.0, -1.0, 0.0
    px, py = -uy, ux                                              # 폭 방향
    a = (pts - mu) @ np.array([ux, uy])
    b = (pts - mu) @ np.array([px, py])
    ca, cb = (a.max() + a.min()) / 2, (b.max() + b.min()) / 2
    cx, cy = mu + ca * np.array([ux, uy]) + cb * np.array([px, py])
    return float(cx), float(cy), tilt, float(a.max() - a.min() + 1), float(b.max() - b.min() + 1)


def _fit_ref(ref, W, H, cx, cy, tilt, length, width):
    """표본 모양을 (폭 x 길이)로 좌우 · 상하 늘리고 tilt 도 돌려 (cx, cy) 에 놓은 마스크 (uint8 0/255, H x W)."""
    import cv2, numpy as np
    tw, th = max(4, int(round(width))), max(4, int(round(length)))
    t = cv2.resize(ref.astype(np.uint8) * 255, (tw, th), interpolation=cv2.INTER_LINEAR)
    M = cv2.getRotationMatrix2D((tw / 2, th / 2), -tilt, 1.0)       # cv2 각도 + = 반시계 -> 위쪽이 오른쪽(시계)은 -
    M[0, 2] += cx - tw / 2
    M[1, 2] += cy - th / 2
    m = cv2.warpAffine(t, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
    return ((m >= 128) * 255).astype(np.uint8)


def pick_nail_ref(tips, good, shape=None):
    """세트의 팁들 -> (쓸 표본 번호, 로그). 믿을 만한 팁 모양들과 가장 닮은 표본 (좌우 · 상하 늘린 뒤 겹침),
    luna 가 본 팁 모양(shape)이 있으면 그 모양과 닮은 정도도 조금 더해요."""
    import numpy as np
    refs = load_nail_refs()
    rn = [_norm_mask(m) for _, m in refs]
    owns = [_norm_mask(tips[k].info["tip_mask"] > 0) for k in good]
    owns = [o for o in owns if o is not None]
    proto = None
    if shape:
        yy, xx = np.mgrid[0:128, 0:64]
        proto = _shape_field(shape, (xx + 0.5 - 32) / 32, (yy + 0.5 - 64) / 64) <= 1.0
    best, best_s, log, scores = 0, -1.0, [], []
    for i, r in enumerate(rn):
        s = (sum(_iou(r, o) for o in owns) / len(owns)) if owns else 0.0
        if proto is not None:
            s += 0.25 * _iou(r, proto)
        log.append(f"{refs[i][0]} {s:.2f}")
        scores.append(s)
        if s > best_s:
            best, best_s = i, s
    pick_nail_ref.last = (scores, len(owns))
    return best, log


def _tight_tip_mask(t):
    """팁 모양 마스크 중 실제 팁 색인 부분만 (불투명한 팁이면 마스크 가장자리의 배경 테두리를 빼요).
    팁 색이 마스크의 85% 미만이면(투명한 팁 등) 마스크 그대로."""
    import cv2, numpy as np
    mk, bg = t.info.get("tip_mask"), t.info.get("bg_color")
    if mk is None:
        return None
    m = mk > 0
    raw = t.info.get("tip_raw")
    raw = raw if raw is not None else t
    if bg is None or raw.size[::-1] != m.shape:
        return m
    a = np.asarray(raw.convert("RGB")).astype(np.int16)
    bgv = np.array(bg, np.float32)
    refs_ = np.stack([bgv, 0.15 * bgv + 0.85 * 245.0, np.full(3, 245.0, np.float32)]).astype(np.int16)
    dist = np.min(np.stack([np.abs(a - r_).sum(axis=2) for r_ in refs_]), axis=0)
    fg = (dist > TIP_CUT_COLOR_DIFF) & m
    fg = cv2.morphologyEx(fg.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0
    if fg.sum() < 0.6 * m.sum():
        return m
    n, lab, st, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8))
    if n < 2:
        return m
    c = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    comp = lab == c
    return comp if comp.sum() >= 0.85 * fg.sum() * 0.9 else m


def nail_ref_sheet():
    """nail_ref 표본들을 왼쪽부터 나란히 + 아래에 1, 2, 3 ... 번호 -> luna 에 보여줄 한 장 (팁 모양 비교용). 표본이 없으면 None."""
    from PIL import Image, ImageDraw
    import numpy as np
    refs = load_nail_refs()
    if not refs:
        return None
    H = 300
    cells = []
    for _, m in refs:
        w = max(20, int(round(H * m.shape[1] / m.shape[0])))
        sil = Image.fromarray((~m).astype(np.uint8) * 255).resize((w, H))        # 표본 모양 실루엣 (검정 = 팁)
        cells.append(sil.convert("RGB"))
    gap, lab = 40, 50
    W = sum(c.width for c in cells) + gap * (len(cells) + 1)
    out = Image.new("RGB", (W, H + lab + gap), "white")
    d = ImageDraw.Draw(out)
    font = _label_font(34)
    x = gap
    for i, c in enumerate(cells):
        out.paste(c, (x, gap // 2))
        d.text((x + c.width / 2, H + gap // 2 + lab / 2), str(i + 1), fill="black", font=font, anchor="mm")
        x += c.width + gap
    return out


def tip_axis_frames(raws, res):
    """luna 가 잰 팁마다 {k: {"free_edge": (x, y) px, "base": (x, y) px, "width": px}} -> {k: (cx, cy, tilt, 길이, 폭)}.
    세트의 위아래(손톱 끝 = 위)는 이미 통일했으니, luna 축이 아래를 향하면 축만 쓰고 방향은 위로 둬요 (세트 안에서 팁마다 뒤집지 않게)."""
    import math
    out = {}
    for k, r in res.items():
        if k >= len(raws):
            continue
        W, H = raws[k].size
        (ex, ey), (bx, by), wd = r["free_edge"], r["base"], r["width"]
        dx, dy = ex - bx, ey - by
        ln = math.hypot(dx, dy)
        if ln < 0.25 * H or wd < 0.05 * W or wd > 1.2 * W:
            continue                                          # 엉뚱한 값
        if dy > 0:
            dx, dy = -dx, -dy
        tilt = math.degrees(math.atan2(dx, -dy))
        if abs(tilt) > 60:
            continue
        out[k] = ((ex + bx) / 2, (ey + by) / 2, tilt, ln, wd)
    return out


def _fill_sample_gaps(warped, t, M, frame, canvas_mask):
    """표본 틀 안인데 팁이 없는 곳(배경 · 옆 팁이 보이는 곳)을 팁 자신의 바탕색으로 자연스럽게 이어 칠해요.
    팁 자리 = 팁 색 부분(_tight_tip_mask) ∩ 팁 축 상자(옆 팁 제외) -> 같은 변환으로 옮긴 곳. 그 밖은 모두 메워요."""
    import math, cv2, numpy as np
    OH, OW = canvas_mask.shape
    raw = t.info.get("tip_raw")
    raw = raw if raw is not None else t
    W, H = raw.size
    cx, cy, tilt, ln, wd = frame
    box = np.zeros((H, W), np.uint8)                         # 팁 축 상자 (옆 팁 · 배경 빼기용, 조금 여유)
    rect = ((float(cx), float(cy)), (float(wd) * 1.04, float(ln) * 1.04), float(tilt))
    cv2.fillPoly(box, [np.int32(np.round(cv2.boxPoints(rect)))], 1)
    tm = _tight_tip_mask(t)
    if tm is not None and tm.shape == (H, W) and (tm & (box > 0)).sum() > 0.3 * box.sum():
        tipm = (tm & (box > 0)).astype(np.uint8)
    else:                                                    # 팁 모양을 못 믿으면 축 상자 안 타원
        tipm = np.zeros((H, W), np.uint8)
        cv2.ellipse(tipm, rect, 1, -1)
    wm = cv2.warpAffine(tipm * 255, M, (OW, OH), flags=cv2.INTER_NEAREST, borderValue=0)
    ek = max(3, int(round(OW / 90)) | 1)                     # 팁 가장자리(배경과 섞인 밝은 테두리)도 메워서 경계 자국이 없게
    wm = cv2.erode(wm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ek, ek)))
    hole = (canvas_mask > 0) & (wm == 0)
    if not hole.any() or (wm > 0).sum() < 0.2 * (canvas_mask > 0).sum():
        return warped
    # 큰 사진은 줄여서 메우고(빠르게) 다시 키워 구멍에만 써요
    f = min(1.0, 512.0 / max(OW, OH))
    sw, sh = max(8, int(OW * f)), max(8, int(OH * f))
    small = cv2.resize(warped, (sw, sh), interpolation=cv2.INTER_AREA)
    unk = cv2.resize((wm == 0).astype(np.uint8) * 255, (sw, sh), interpolation=cv2.INTER_NEAREST)
    filled = cv2.inpaint(np.ascontiguousarray(small), unk, max(3, int(sw / 12)), cv2.INPAINT_TELEA)
    big = cv2.resize(filled, (OW, OH), interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (0, 0), max(1.0, OW / 300.0))
    out = warped.copy()
    out[hole] = big[hole]
    return out


def apply_nail_ref(tips, shape=None, pick=None, measure=None):
    """세트의 팁 사진들(세운 뒤) -> nail_ref 표본 하나의 모양으로 모두 다시 딴 팁 사진들.
    ① 세트마다 표본 하나: 믿을 만한 팁 모양들 · luna 팁 모양과 가장 닮은 것
    ② 표본은 크기 · 모양을 바꾸지 않아요. 팁마다 팁의 기울기 · 길이 · 폭 · 자리(자기 모양이 믿을 만하면 그것, 아니면 luna 크기)를 재서
       팁 사진을 돌리고 좌우 · 상하로 늘려 표본 틀에 넣어요 -> 세트의 팁 5개 = 모두 같은 크기 · 같은 모양 (= 표본)
    디자인 픽셀은 바꾸지 않고 둘레 지우기 모양만 바꿔요. 표본이 없으면 None."""
    import math, cv2, numpy as np
    from PIL import Image
    refs = load_nail_refs()
    if not refs or not tips:
        return None
    scores = [t.info.get("tip_score", tip_score(tip_check(t))) for t in tips]
    good = [k for k, t in enumerate(tips) if t.info.get("tip_mask") is not None and _mask_box(t.info["tip_mask"])
            and scores[k] >= TIP_TEMPLATE_MIN_SCORE and not t.info.get("tip_doubt") and not tip_cut_straight(t)]
    ri, log = pick_nail_ref(tips, good, shape)
    if pick and 1 <= int(pick) <= len(refs):                     # luna 가 세트를 보고 고른 표본이 먼저
        sc, n_own = getattr(pick_nail_ref, "last", ([], 0))
        lp = int(pick) - 1
        if n_own >= 2 and sc and sc[ri] - sc[lp] > NAIL_REF_PICK_MARGIN:
            # luna 가 고른 표본이 실제로 딴 팁 모양들과 많이 달라요 (예: 뾰족한 팁인데 넓은 타원) -> 프로그램 비교로
            log = [f"luna 선택 {lp + 1}번은 팁 모양과 많이 달라서 프로그램 비교로 {ri + 1}번 ("
                   + ", ".join(log) + ")"]
        else:
            log = [f"luna 선택 {lp + 1}번 (프로그램 비교: " + ", ".join(log) + ")"]
            ri = lp
    rname, rmask = refs[ri]
    print(f"  [nail_ref] 이 세트는 표본 '{rname}' 모양으로 팁을 따요  (닮은 정도: {', '.join(log)})")
    frames = {k: _mask_frame(_tight_tip_mask(tips[k])) for k in good}      # 실제 팁 색 부분으로 재요 (배경 테두리 빼고)
    frames = {k: f for k, f in frames.items() if f}
    luna_ax = {}
    if measure and TIP_LUNA_AXIS:          # luna 가 팁마다 손톱 끝 · 뿌리 · 폭을 재요 -> 그 축이 프로그램이 딴 모양보다 먼저
        try:
            raws = [(t.info.get("tip_raw") if t.info.get("tip_raw") is not None else t.convert("RGB")) for t in tips]
            luna_ax = tip_axis_frames(raws, measure(raws) or {})
        except Exception as e:
            print(f"  ! luna 팁 축 재기 실패 - 프로그램이 딴 모양으로: {type(e).__name__}: {str(e)[:150]}")
            luna_ax = {}
    # 실제 크기 / luna 크기 비율 (믿을 만한 팁들의 가운데 값) - 자기 모양을 못 믿는 팁의 크기에 써요
    rl = sorted(f[3] / tips[k].info["luna_len"] for k, f in frames.items() if tips[k].info.get("luna_len"))
    rw = sorted(f[4] / tips[k].info["luna_wid"] for k, f in frames.items() if tips[k].info.get("luna_wid"))
    kl = rl[len(rl) // 2] if rl else 0.95
    kw = rw[len(rw) // 2] if rw else None
    asp = rmask.shape[1] / rmask.shape[0]                         # 표본의 폭/길이
    # 표본은 크기 · 모양을 바꾸지 않아요: 표본 그대로의 틀(캔버스 = 표본 크기, 마스크 = 표본 모양)에
    # 팁 사진을 돌리고 늘려서 넣어요 -> 한 세트의 팁 5개 = 모두 같은 크기 · 같은 모양
    up = max(1.0, NAIL_REF_MIN_HEIGHT / rmask.shape[0])           # 아주 작은 표본만 그대로의 비율로 확대 (모양 그대로)
    mh, mw = int(round(rmask.shape[0] * up)), int(round(rmask.shape[1] * up))
    pad = int(round(NAIL_REF_PAD * mh))
    OW, OH = mw + 2 * pad, mh + 2 * pad
    if up > 1:              # 작은 표본: 모양 그대로 부드럽게 확대 (계단처럼 깨지지 않게 - 확대 후 살짝 흐려 다시 경계)
        m_ = cv2.resize(rmask.astype(np.float32), (mw, mh), interpolation=cv2.INTER_CUBIC)
        m_ = cv2.GaussianBlur(m_, (0, 0), max(1.0, 1.3 * up))
        m_ = ((m_ >= 0.5) * 255).astype(np.uint8)
    else:
        m_ = rmask.astype(np.uint8) * 255
    canvas_mask = np.zeros((OH, OW), np.uint8)
    canvas_mask[pad:pad + mh, pad:pad + mw] = (m_ >= 128) * 255
    soft = cv2.GaussianBlur(canvas_mask, (0, 0), max(1.0, OW / 400.0))
    out = []
    for k, t in enumerate(tips):
        raw = t.info.get("tip_raw")
        raw = raw if raw is not None else t.convert("RGB")
        W, H = raw.size
        if k in luna_ax:
            cx, cy, tilt, ln, wd = luna_ax[k]
            how = "luna 가 잰 손톱 끝 · 뿌리 축"
        elif k in frames:
            cx, cy, tilt, ln, wd = frames[k]
            how = "자기 모양 기준"
        else:
            ln = kl * t.info["luna_len"] if t.info.get("luna_len") else None
            if not ln:
                own = t.info.get("tip_mask")
                f = _mask_frame(own > 0) if own is not None else None
                ln = f[3] if f else 0.8 * H
            wd = kw * t.info["luna_wid"] if kw and t.info.get("luna_wid") else (t.info.get("luna_wid") or ln * asp)
            cx, cy = t.info.get("cut_center") or (W / 2, H / 2)
            tilt = 0.0
            how = "luna 크기 기준"
        # 팁(돌아간 상자: 가운데 cx, cy · 기울기 · 폭 x 길이) -> 표본 상자(캔버스 안 pad ~ pad+mw/mh)
        a = math.radians(tilt)
        ux, uy = math.sin(a), -math.cos(a)                        # 손톱 끝 쪽
        px, py = -uy, ux                                          # 오른쪽
        src = np.float32([[cx + ux * ln / 2, cy + uy * ln / 2], [cx - ux * ln / 2, cy - uy * ln / 2],
                          [cx + px * wd / 2, cy + py * wd / 2]])
        if NAIL_REF_KEEP_RATIO:
            # 디자인을 억지로 늘리지 않아요: 같은 배율로(길이 · 폭 비율 그대로) 표본 상자 안에 들어가게, 가운데 맞춤
            sc_ = min(mh / max(ln, 1.0), mw / max(wd, 1.0))
            hl, hw = ln * sc_ / 2, wd * sc_ / 2
            ccx, ccy = pad + mw / 2, pad + mh / 2
            dst = np.float32([[ccx, ccy - hl], [ccx, ccy + hl], [ccx + hw, ccy]])
        else:
            dst = np.float32([[pad + mw / 2, pad], [pad + mw / 2, pad + mh], [pad + mw, pad + mh / 2]])
        M = cv2.getAffineTransform(src, dst)
        warped = cv2.warpAffine(np.asarray(raw.convert("RGB")), M, (OW, OH), flags=cv2.INTER_LANCZOS4,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=TIP_BG_COLOR)
        if NAIL_REF_FILL:
            warped = _fill_sample_gaps(warped, t, M, (cx, cy, tilt, ln, wd), canvas_mask)
        wim = Image.fromarray(warped)
        n = Image.composite(wim, Image.new("RGB", (OW, OH), TIP_BG_COLOR), Image.fromarray(soft))
        n.info.update({kk: v for kk, v in t.info.items()
                       if kk not in ("tip_mask", "tip_box", "tip_raw", "cut_rect", "cut_center", "luna_len", "luna_wid",
                                     "px_per_set")})
        n.info.update(tip_mask=canvas_mask.copy(), tip_box=(float(pad), float(pad), float(pad + mw), float(pad + mh)),
                      tip_raw=wim, nail_ref=rname)
        print(f"    {k + 1}번 팁: 팁 {wd:.0f}x{ln:.0f}px · {tilt:+.0f}도 -> 표본 틀 {mw}x{mh}px ({how})")
        out.append(n)
    print(f"  [nail_ref] 팁 {len(out)}개 모두 표본 '{rname}' 그대로의 크기 · 모양 ({OW}x{OH}px)")
    return out


def apply_tip_shape(tips, refs=None, shape=None, pick=None, measure=None):
    """팁 모양 맞추기: nail_ref 표본이 있으면 표본으로 (NAIL_REF_USE), 없으면 세트의 기준 팁 모양으로."""
    if NAIL_REF_USE:
        try:
            r = apply_nail_ref(tips, shape, pick, measure)
        except Exception as e:
            print(f"  ! nail_ref 표본으로 따기 실패 - 기준 팁 모양으로: {type(e).__name__}: {e}")
            r = None
        if r is not None:
            return r
    return apply_tip_template(tips, refs)


TIP_RERANK = True            # 팁 하나씩 자른 결과를 점수로 확인 -> 의심스러우면 자르는 범위 · 방법을 바꾼 후보를 더 만들어 가장 좋은 것으로
TIP_RERANK_OK = -0.6          # 첫 후보 점수가 이보다 낮을 때만 후보를 더 만들어요 (0 = 흠 없음)
TIP_RERANK_WARN = -2.0        # 가장 좋은 후보도 이보다 낮으면 '확인 필요'
TIP_RERANK_TRIES = ((1.3, True), (1.0, False), (1.35, False))   # (자르는 범위 배율, 팁 모양 기준?) 후보
TIP_VERIFY = True             # 다시 골라도 점수가 낮은 팁은 luna 에 세트 사진과 잘린 결과를 보여주고 팁 위치를 다시 받아요 (세트당 최대 1번 호출)
TIP_VERIFY_BELOW = -0.6       # 이 점수보다 낮은 팁만 luna 에 다시 확인


def tip_check(t, luna_len=None):
    luna_len = luna_len if luna_len is not None else t.info.get("luna_len")
    """잘라 세운 팁 사진 -> 모양 검사 {"aspect": 길이/폭, "penalty": 세트 비교 없는 감점, "notes": [...]} 또는 None.
    감점: 팁 윤곽이 사진 가장자리에 닿음(잘림) · 인식 모델이 잰 길이보다 많이 짧거나 김 · 끝이나 뿌리가 칼로 자른 듯 평평함."""
    import math
    import numpy as np
    mk = t.info.get("tip_mask")
    if mk is None:
        return None
    ys, xs = np.nonzero(mk)
    if len(xs) < 50:
        return None
    h, w = mk.shape
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    Lm, Wm = float(y1 - y0 + 1), float(x1 - x0 + 1)
    pen, notes = 0.0, []
    touch = sum((x0 <= 2, x1 >= w - 3, y0 <= 2, y1 >= h - 3))
    if touch:
        pen += 1.5 * touch; notes.append(f"가장자리에 닿음 {touch}")
    if luna_len:
        r = Lm / luna_len                      # 인식 모델 길이는 보통 10% 남짓 길어요
        d = max(0.0, abs(math.log(r / 0.9)) - 0.15)
        if d:
            pen += 4 * d; notes.append(f"길이 비 {r:.2f}")
    row = lambda yy: float((mk[int(yy)] > 0).sum())
    wmax = max(1.0, max(row(yy) for yy in np.linspace(y0, y1, 24)))
    flat_ok = t.info.get("tip_shape") in TIP_FLAT_END_OK      # 스퀘어 · 코핀: 손톱 끝이 원래 평평해요
    for nm, yy in (("끝", y0 + 0.04 * Lm), ("뿌리", y1 - 0.04 * Lm)):
        if nm == "끝" and flat_ok:
            continue
        f = row(yy) / wmax                    # 손톱 끝 · 뿌리는 둥글어서 좁아요 - 넓으면 잘린 면
        if f > 0.9:
            pen += (f - 0.9) * 10; notes.append(f"{nm} 평평 {f:.2f}")
    return {"aspect": Lm / Wm, "penalty": pen, "notes": notes}


def tip_score(chk, med_aspect=None):
    """tip_check 결과 + 세트의 보통 길이/폭 -> 점수 (0 = 흠 없음, 낮을수록 나빠요)."""
    import math
    if not chk:
        return -3.0                            # 팁 모양을 못 땄어요
    s = -chk["penalty"]
    if med_aspect:
        s -= 2.5 * max(0.0, abs(math.log(chk["aspect"] / med_aspect)) - 0.25)   # 엄지는 조금 넓어도 돼요
    return s


TIP_DROP_BGLIKE = False   # 팁 따기에서 둘레 배경(천 · 판)과 색 · 결이 같은 부분은 팁에서 빼요 (끔: 투명한 팁은 비친 배경까지 팁이라 잘려 나가요)
TIP_CLEAN = True          # 잘라 세운 팁에서 팁만 정확히 따서(천 · 판 배경 제외) 둘레를 단색으로 지워요
TIP_CLEAR_EXTEND = 1.25   # 인식 모델이 잰 팁 길이가 딴 팁보다 이 배 넘게 길 때만 '투명한 끝이 빠졌다'고 보고 늘려요
                          #   (인식값은 보통 10~15% 길게 나와요 - 그 정도로 늘리면 팁 끝에 천이 덧붙어요)


def _shape_field(shape, u, v):
    """팁 상자 안 정규화 좌표 (u: -1~1 가로, v: -1(위=손톱 끝)~1(아래=뿌리)) -> 모양 거리 (<=1 이면 팁 안).
    round/None = 타원, square = 둥근 사각형, coffin = 위로 좁아지는 둥근 사각형, pointed = 위로 뾰족한 물방울."""
    import numpy as np
    if shape == "square":
        return (np.abs(u) ** 4 + np.abs(v) ** 4) ** 0.5           # 제곱해서 타원 식(u^2+v^2)과 같은 눈금
    if shape == "coffin":
        w = 1.0 - 0.3 * np.clip(-v, 0, 1)                         # 손톱 끝 쪽 폭 70%까지
        return (np.abs(u / w) ** 4 + np.abs(v) ** 4) ** 0.5
    if shape == "pointed":
        w = np.where(v < 0, np.maximum(0.05, 1.0 + v) ** 0.7, 1.0)  # 손톱 끝 쪽으로 점점 좁아져 뾰족
        return (u / w) ** 2 + v ** 2
    return u ** 2 + v ** 2


def tip_refine(t, shape=None, tlen=None):
    """세운 팁 사진 -> 팁만 딴 (마스크 uint8 0/255, 상자 (x0, y0, x1, y1)) 또는 (None, None).
    배경(천 · 판)은 결이 거칠고 팁은 매끈해요 -> 밝기 · 결(국소 표준편차) · 색 3채널로 GrabCut.
    shape / tlen: 세트의 팁 모양 · 길이 (luna) -> GrabCut 의 '팁일 것' 영역을 그 모양으로 잡아요 (없으면 타원).
    (예전 모양 재기는 윤곽선을 쓰는데, 니트 천의 결이 모두 윤곽선으로 잡혀 천까지 팁으로 묶였어요)"""
    import cv2, numpy as np
    a = np.asarray(t.convert("RGB"))
    h, w = a.shape[:2]
    if h < 32 or w < 16:
        return None, None
    box = t.info.get("tip_box") or (0.10 * w, 0.055 * h, 0.90 * w, 0.945 * h)
    x0, y0 = max(0, int(round(box[0]))), max(0, int(round(box[1])))
    x1, y1 = min(w, int(round(box[2]))), min(h, int(round(box[3])))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None, None
    lab = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32)
    L = lab[..., 0]
    s = max(2.0, w / 60.0)
    mu = cv2.GaussianBlur(L, (0, 0), s)
    sd = np.sqrt(np.maximum(cv2.GaussianBlur(L * L, (0, 0), s) - mu * mu, 0))
    chroma = np.hypot(lab[..., 1] - 128, lab[..., 2] - 128)
    feat = np.dstack([cv2.GaussianBlur(L, (0, 0), 1.0), np.clip(sd * 10, 0, 255),
                      np.clip(chroma * 4, 0, 255)]).astype(np.uint8)
    m = np.full((h, w), cv2.GC_BGD, np.uint8)
    m[y0:y1, x0:x1] = cv2.GC_PR_BGD
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    yy, xx = np.mgrid[0:h, 0:w]
    el = _shape_field(shape, (xx - cx) / rx, (yy - cy) / ry)
    m[el <= 0.85 ** 2] = cv2.GC_PR_FGD
    m[el <= 0.35 ** 2] = cv2.GC_FGD
    try:
        cv2.setRNGSeed(1234)                       # 같은 사진이면 늘 같은 결과 (GrabCut 초기값이 무작위예요)
        cv2.grabCut(feat, m, None, np.zeros((1, 65)), np.zeros((1, 65)), 6, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None, None
    fg = ((m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD)).astype(np.uint8)
    if TIP_DROP_BGLIKE:
        # 팁 둘레의 진짜 배경(팁 상자 안 · 타원 밖, 흐리게 지운 곳 말고)과 색 · 결이 같은 곳은 팁에서 빼요 (천 · 판 조각)
        inbox = np.zeros((h, w), bool)
        inbox[y0:y1, x0:x1] = True
        samp = inbox & (el > 1.0) & ((m == cv2.GC_BGD) | (m == cv2.GC_PR_BGD))
        if samp.sum() >= 150:
            bgc = np.median(lab[samp], axis=0)
            bsd = float(np.median(sd[samp]))
            like = (np.sqrt(((lab - bgc) ** 2).sum(axis=2)) < 14) & (np.abs(sd - bsd) < max(1.5, 0.6 * bsd))
            like &= el > 0.5 ** 2                       # 팁 한가운데는 건드리지 않아요
            fg[like] = 0
    k = max(3, int(w / 40)) | 1
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, labm = cv2.connectedComponents(fg)
    c = labm[int(cy), int(cx)]
    if not c:
        return None, None
    cs, _ = cv2.findContours((labm == c).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull = cv2.convexHull(max(cs, key=cv2.contourArea))
    out = np.zeros((h, w), np.uint8)
    cv2.fillPoly(out, [hull], 255)
    bx, by, bw, bh = cv2.boundingRect(hull)
    if TIP_MASK_SMOOTH:                # 볼록 껍질의 꺾인 직선 모서리를 둥글게 (스퀘어 · 코핀은 조금만)
        sg = max(1.0, bw * (0.015 if shape in ("square", "coffin") else TIP_MASK_SMOOTH))
        out = ((cv2.GaussianBlur(out, (0, 0), sg) >= 128) * 255).astype(np.uint8)
    area, ref = float((out > 0).sum()), float(rx * ry * 4)
    if not (0.30 * ref <= area <= 0.98 * ref) or bh < TIP_REFINE_MIN_ASPECT.get(tlen, 1.05) * bw:   # 이상하면 예전 방식 그대로
        return None, None
    return out, (bx, by, bx + bw, by + bh)


def tip_clean(t, luna_len=None, shape=None, tlen=None):
    """세운 팁 사진 -> 팁만 남기고 둘레(천 · 판 · 옆 팁)를 단색(TIP_BG_COLOR)으로 지운 사진.
    info["tip_box"] = 팁에 꼭 맞는 상자, info["tip_mask"] = 팁 모양 (붙일 때 이 모양 그대로 써요).
    팁 따기에서 투명한 끝이 빠졌으면(인식 모델이 잰 팁 길이가 더 길면) 그만큼 위(손톱 끝) 쪽으로 늘려요.
    팁을 못 따면 그대로 돌려줘요."""
    import cv2, numpy as np
    from PIL import Image
    mk, box = tip_refine(t, shape, tlen)
    if mk is None:
        return t
    h, w = mk.shape
    old = t.info.get("tip_box")
    bx0, by0, bx1, by1 = box
    bh = by1 - by0
    if luna_len and old and bh * TIP_CLEAR_EXTEND < luna_len <= (old[3] - old[1]) * 1.05:
        dy = min(by0, luna_len - bh)                       # 투명한 끝 = 위쪽(손톱 끝)으로 늘려요
        if dy > 2:
            cs, _ = cv2.findContours(mk, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            pts = cs[0].reshape(-1, 2)
            cap = pts[pts[:, 1] < by0 + 0.3 * bh] - np.array([0, int(round(dy))])
            hull = cv2.convexHull(np.vstack([pts, cap]).astype(np.int32))
            mk = np.zeros((h, w), np.uint8)
            cv2.fillPoly(mk, [hull], 255)
            x, y, ww, hh = cv2.boundingRect(hull)
            bx0, by0, bx1, by1 = x, y, x + ww, y + hh
    keep = cv2.dilate(mk, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    keep = cv2.GaussianBlur(keep, (0, 0), max(1.0, w / 150.0))
    out = Image.composite(t.convert("RGB"), Image.new("RGB", t.size, TIP_BG_COLOR), Image.fromarray(keep))
    out.info["tip_box"] = (float(bx0), float(by0), float(bx1), float(by1))
    out.info["tip_mask"] = mk
    out.info["tip_shape"] = shape                # tip_check: 스퀘어 · 코핀은 '끝 평평' 감점 안 함
    out.info["tip_raw"] = t.convert("RGB")       # 둘레를 지우기 전 사진 (오리기 평가: luna 가 진짜 팁 범위를 봐요)
    return out


def tip_shape_mask(t):
    """팁 사진 -> 가운데 팁 모양 마스크 (bool 배열) 또는 None.
    배경(가장자리 색)과 색이 다른 부분 + 윤곽선(투명한 부분은 윤곽 · 광택으로만 보여요)을 합쳐 속을 채우고,
    가운데를 덮는 덩어리(없으면 가운데에 가까운 큰 덩어리)를 팁으로 봐요."""
    import cv2, numpy as np
    if t is None:
        return None
    a = np.asarray(t.convert("RGB"), np.int16)
    h, w = a.shape[:2]
    if h < 24 or w < 24:
        return None
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    fg = (np.abs(a - bg).sum(axis=2) > 36).astype(np.uint8)
    gray = cv2.cvtColor(a.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    edges = cv2.dilate(cv2.Canny(gray, 30, 90), np.ones((3, 3), np.uint8), iterations=2)
    fg = cv2.morphologyEx(np.maximum(fg, (edges > 0).astype(np.uint8)), cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    fg[[0, -1], :] = 0
    fg[:, [0, -1]] = 0
    flood = fg.copy()                                     # 속 채우기 (윤곽만 잡힌 투명 부분)
    cv2.floodFill(flood, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 1)
    fg = np.maximum(fg, 1 - flood)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg)
    if n < 2:
        return None
    cx, cy = w / 2, h / 2
    hit = lab[int(cy), int(cx)]
    best = hit if hit > 0 else max(range(1, n), key=lambda i: stats[i, cv2.CC_STAT_AREA]
                                   - 4 * (abs(stats[i, 0] + stats[i, 2] / 2 - cx) + abs(stats[i, 1] + stats[i, 3] / 2 - cy)))
    m = lab == best
    if m.sum() < 0.04 * h * w or m.sum() > 0.80 * h * w:       # 너무 작거나 사진을 거의 채우면 팁 모양이 아님
        return None
    touch = sum(int(e.sum() > 0.25 * e.size) for e in (m[1], m[-2], m[:, 1], m[:, -2]))
    if touch >= 2:                                             # 두 면 이상에서 크게 잘렸으면 모양을 믿을 수 없어요
        return None
    return m


def _cut_by_circle(m, cx, cy, R, share=0.08):
    """원 안에서 딴 모양이 원 둘레에 잘렸는지 (= 팁이 원 밖으로 이어져서 모양을 믿을 수 없음)."""
    import cv2, numpy as np
    cs, _ = cv2.findContours(np.asarray(m, np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return True
    c = max(cs, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
    r = np.hypot(c[:, 0] - cx, c[:, 1] - cy)
    return float((r >= R - 3).mean()) > share


def tip_pose(m):
    """팁 모양 마스크 -> {"gx", "gy": 중심, "vx", "vy": 손톱 끝 쪽 단위 방향, "length", "width",
    "ratio": 넓은 끝 폭 / 좁은 끝 폭} 또는 None.
    ① 긴 쪽 = 손톱 길이(손톱 끝 <-> 손톱 뿌리) 축 ② 4개 면(긴 축의 양 끝 + 양 옆) 가까이에서 폭을 재서
    가장 좁은 면 = 손톱 끝. (긴 축 규칙이 먼저: 가장 좁은 면이 옆면이면 양 끝 중 더 좁은 쪽)"""
    import numpy as np
    if m is None:
        return None
    ys, xs = np.where(m)
    if len(xs) < 50:
        return None
    gx, gy = float(xs.mean()), float(ys.mean())
    dx, dy = xs - gx, ys - gy
    vals, vecs = np.linalg.eigh(np.cov(np.vstack([dx, dy])))
    ax, ay = vecs[:, int(np.argmax(vals))]                 # 긴 축
    s = dx * ax + dy * ay                                   # 긴 축 위치
    q = dx * -ay + dy * ax                                  # 옆 방향 위치
    s0, s1, q0, q1 = s.min(), s.max(), q.min(), q.max()
    Ls, Lq = s1 - s0, q1 - q0
    if Ls < 8 or Lq < 4:
        return None

    def band_width(along, across, lo, hi):
        """along 이 [lo, hi] 인 줄들마다 across 폭 -> 중앙값"""
        sel = (along >= lo) & (along <= hi)
        if sel.sum() < 5:
            return float("inf")
        a, c = np.round(along[sel]).astype(int), across[sel]
        ws = [c[a == v].max() - c[a == v].min() + 1 for v in np.unique(a)]
        return float(np.median(ws))

    sides = {"+": band_width(s, q, s1 - 0.25 * Ls, s1 - 0.08 * Ls),       # 긴 축 + 쪽 끝
             "-": band_width(s, q, s0 + 0.08 * Ls, s0 + 0.25 * Ls),       # 긴 축 - 쪽 끝
             "side1": band_width(q, s, q1 - 0.25 * Lq, q1 - 0.08 * Lq),   # 옆면
             "side2": band_width(q, s, q0 + 0.08 * Lq, q0 + 0.25 * Lq)}
    narrow = min(sides, key=sides.get)
    if narrow not in ("+", "-"):
        narrow = "+" if sides["+"] <= sides["-"] else "-"
    wide = "-" if narrow == "+" else "+"
    sign = 1.0 if narrow == "+" else -1.0
    return {"gx": gx, "gy": gy, "vx": float(ax * sign), "vy": float(ay * sign), "length": float(Ls + 1),
            "width": float(Lq + 1), "ratio": sides[wide] / max(1.0, sides[narrow])}


def orient_tip(t, seg=None, max_turn=None, keep_up=False):
    """팁 사진 1장을 똑바로 세워요 (디자인 픽셀은 그대로 - 돌리기 · 자르기만). 규칙은 tip_pose 와 같아요.
    긴 축은 항상 세로로 맞추고, 위 · 아래는 손톱 끝이 확실할 때만 뒤집어요 (애매하면 지금 방향 그대로).
    seg: 모양을 잴 때만 쓰는 사진 (옆 팁을 지운 것, t 와 같은 크기).
    max_turn: 이미 대략 세운 사진이면 긴 축을 이 각도(도)까지만 고쳐요 (더 크게 틀어지면 잘못 딴 모양으로 봐요).
    keep_up: 위 · 아래를 이미 정한 사진이면 절대 뒤집지 않아요 (기울기만 고쳐요).
    모양을 못 따거나 팁 하나의 모양이 아니면 t 를 그대로 돌려줘요."""
    import math
    import numpy as np
    from PIL import Image
    p = tip_pose(tip_shape_mask(seg if seg is not None else t))
    if not p or p["length"] < 1.2 * p["width"]:          # 옆 팁과 붙은 덩어리 등 -> 건드리지 않아요
        return t
    vx, vy = p["vx"], p["vy"]
    if max_turn is not None and abs(vy) < math.cos(math.radians(max_turn)):
        return t
    if (keep_up or p["ratio"] < TIP_SURE_RATIO) and vy > 0:   # 애매하거나 이미 정했으면 지금 위쪽 끝을 그대로 위로
        vx, vy = -vx, -vy
    deg = math.degrees(math.atan2(vy, vx)) + 90.0
    a = np.asarray(t.convert("RGB"))
    fill = tuple(int(v) for v in np.median(np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]), axis=0))
    P = int(max(t.size))
    big = Image.new("RGB", (t.width + 2 * P, t.height + 2 * P), fill)
    big.paste(t, (P, P))
    cx, cy = p["gx"] + P, p["gy"] + P
    if abs(((deg + 180) % 360) - 180) > 1:
        big = big.rotate(deg, resample=Image.BICUBIC, center=(cx, cy), fillcolor=fill)
    hw, hh = 0.5 * p["width"] * 1.25, 0.5 * p["length"] * 1.12     # 팁 둘레만 (투명한 끝까지 여유 있게)
    x0, y0 = int(cx - hw), int(cy - hh)
    out = big.crop((x0, y0, int(cx + hw), int(cy + hh)))
    out.info["tip_box"] = (cx - 0.5 * p["width"] - x0, cy - 0.5 * p["length"] - y0,
                           cx + 0.5 * p["width"] - x0, cy + 0.5 * p["length"] - y0)
    r = t.info.get("cut_rect")
    if r:                      # 또렷한 창도 같이 돌리고 옮겨요 (돌린 창 안쪽 상자)
        a_ = math.radians(deg if abs(((deg + 180) % 360) - 180) > 1 else 0.0)
        ca, sa = math.cos(a_), math.sin(a_)
        pts_ = []
        for qx, qy in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])):
            dx, dy = qx + P - cx, qy + P - cy
            pts_.append((cx + dx * ca + dy * sa - x0, cy - dx * sa + dy * ca - y0))
        xs, ys = sorted(q[0] for q in pts_), sorted(q[1] for q in pts_)
        out.info["cut_rect"] = (xs[1], ys[1], xs[2], ys[2])
    return out


# ---------------------------------------------------------------------------------------------- 팁 직접 붙이기
# 잘라 세운 팁 사진(디자인 그대로)을 레퍼런스 사진의 손톱 자리에 그대로 붙여요 - 이미지 모델이 다시 그리지 않아요.
#   손톱 자리 = nailhand/nail_positions.json 의 손가락마다 tip(손톱 끝 점) · base(손톱 뿌리 점) · w(폭), 모두 사진 비율 0~1
#   팁 사진의 위쪽 끝(손톱 끝) -> tip, 아래쪽 끝(손톱 뿌리) -> base, 팁 폭 -> w. 팁 모양 바깥은 원래 사진 그대로
TIP_PASTE_WIDEN = 1.06
TIP_RELIGHT = True        # 붙인 팁에 레퍼런스 손톱의 빛(명암 · 반사광)을 반영
TIP_FIT_NAIL = True       # 팁 윤곽 대신 레퍼런스의 원래 손톱 모양에 맞춰 채워요 (그림은 그대로, 바탕색을 손톱 끝까지 이어서)      # 팁을 손톱 폭보다 이만큼 넓게 (원래 손톱 색이 가장자리에 남지 않게)
TIP_PASTE_BASE_INSET = 0.04  # 팁 아래쪽 끝을 손톱 뿌리(base)에서 손톱 길이의 이만큼 안쪽에 (피부 위로 넘지 않게)


def tip_alpha(t):
    """세운 팁 사진 -> (팁 모양 알파 0~255 배열, 팁이 차지하는 상자 (x0, y0, x1, y1)).
    배경 따기(GrabCut 등) 없이, 자를 때 잰 팁 상자(info["tip_box"]) 안에 둥근 네모(초타원)를 그려요.
    상자가 없으면 자르는 규칙대로 가운데(가로 80% · 세로 89%)를 팁 자리로 봐요."""
    import numpy as np
    w, h = t.size
    box = t.info.get("tip_box") or (0.10 * w, 0.055 * h, 0.90 * w, 0.945 * h)
    x0, y0 = max(0, int(round(box[0]))), max(0, int(round(box[1])))
    x1, y1 = min(w, int(round(box[2]))), min(h, int(round(box[3])))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None, None
    mk = t.info.get("tip_mask")              # tip_clean 이 딴 팁 모양이 있으면 그대로
    if isinstance(mk, np.ndarray) and mk.shape == (h, w) and mk.any():
        return (mk > 127).astype(np.uint8) * 255, (x0, y0, x1, y1)
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2 + 0.5, (y1 - y0) / 2 + 0.5
    a = ((np.abs((xx - cx) / rx) ** 3 + np.abs((yy - cy) / ry) ** 3) <= 1.0).astype(np.uint8) * 255
    return a, (x0, y0, x1, y1)


TIP_BG_COLOR = (242, 242, 242)   # (예전 RGBA 팁용) 투명 바깥을 칠하는 단색


def tip_flat(t, bg=TIP_BG_COLOR):
    """(예전 RGBA 팁용) -> 단색 배경 RGB. 단색 배경으로 처리한 RGB 팁은 그대로."""
    from PIL import Image
    if t.mode != "RGBA":
        return t
    base = Image.new("RGB", t.size, bg)
    base.paste(t.convert("RGB"), (0, 0), t.getchannel("A").point(lambda v: 255 if v > 0 else 0))
    return base


def nail_ref_mask(src, tip, base, w, outline=None):
    """레퍼런스 사진(src, RGB 배열)에서 원래 손톱 모양 (0/255 배열). JSON 의 손톱 끝 · 뿌리 · 폭 둘레에서 GrabCut.
    이상하면(너무 작거나 큼) JSON 으로 그린 손톱 모양으로 대신해요."""
    import cv2, numpy as np
    H, W = src.shape[:2]
    def poly(g):            # outline(grow) -> 다각형 (상자가 있으면 상자 모양)
        return np.int32(np.round(outline(g) if outline else nail_outline(tip, base, w, grow=g)))
    base_m = np.zeros((H, W), np.uint8)
    cv2.fillPoly(base_m, [poly(1.0)], 255)
    outer = poly(1.45)
    x0, y0 = max(0, outer[:, 0].min() - 4), max(0, outer[:, 1].min() - 4)
    x1, y1 = min(W, outer[:, 0].max() + 5), min(H, outer[:, 1].max() + 5)
    if x1 - x0 < 12 or y1 - y0 < 12:
        return base_m
    m = np.full((y1 - y0, x1 - x0), cv2.GC_BGD, np.uint8)
    off = np.array([x0, y0])
    cv2.fillPoly(m, [outer - off], cv2.GC_PR_BGD)
    cv2.fillPoly(m, [poly(1.0) - off], cv2.GC_PR_FGD)
    cv2.fillPoly(m, [poly(0.55) - off], cv2.GC_FGD)
    try:
        bgd, fgd = np.zeros((1, 65)), np.zeros((1, 65))
        cv2.grabCut(cv2.cvtColor(np.ascontiguousarray(src[y0:y1, x0:x1]), cv2.COLOR_RGB2BGR), m, None, bgd, fgd, 4,
                    cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return base_m
    fg = ((m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD)).astype(np.uint8)
    k = max(3, int(w / 14)) | 1
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab = cv2.connectedComponents(fg)
    cy, cx = int((tip[1] + base[1]) / 2 - y0), int((tip[0] + base[0]) / 2 - x0)
    c = lab[min(max(cy, 0), lab.shape[0] - 1), min(max(cx, 0), lab.shape[1] - 1)]
    if not c:
        return base_m
    cnt, _ = cv2.findContours((lab == c).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull = cv2.convexHull(max(cnt, key=cv2.contourArea))
    area, ref = cv2.contourArea(hull), float((base_m > 0).sum())
    if not (0.6 * ref <= area <= 1.6 * ref):
        return base_m
    out = np.zeros((H, W), np.uint8)
    cv2.fillPoly(out, [hull + off], 255)
    return out


def nail_light(src, geo_px, k, outline=None):
    """레퍼런스 사진(src, RGB 배열)의 원래 손톱에서 빛을 읽어요 -> (명암 배율 HxW, 반사광 HxW) - 손톱 자리 밖은 1, 0.
    명암 = 손톱 안 밝기를 크게 흐린 것 / 평균 (빛이 오는 쪽이 밝고 반대쪽이 어두운 흐름),
    반사광 = 원래 손톱의 작은 밝은 하이라이트 (유광 반사)."""
    import cv2, numpy as np
    H, W = src.shape[:2]
    (tx, ty), (bx, by), w = geo_px
    poly = outline(0.92) if outline else nail_outline((tx, ty), (bx, by), w, grow=0.92)
    m = np.zeros((H, W), np.uint8)
    cv2.fillPoly(m, [np.int32(np.round(poly))], 255)
    if m.sum() < 255 * 30:
        return None, None
    L = cv2.cvtColor(src, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    mf = (m > 0).astype(np.float32)
    s = max(2.0, w / 5.0)
    num = cv2.GaussianBlur(L * mf, (0, 0), s)
    den = cv2.GaussianBlur(mf, (0, 0), s) + 1e-3
    low = num / den                                       # 손톱 안만으로 흐린 밝기 (바깥 피부가 섞이지 않게)
    mean = float((low * mf).sum() / max(1.0, mf.sum()))
    shade = np.clip(low / max(1.0, mean), 0.72, 1.28)
    small = cv2.GaussianBlur(L, (0, 0), max(1.0, w / 25.0))
    spec = np.clip(small - low - 14.0, 0, None) / 70.0    # 주변보다 확 밝은 곳만 = 반사광
    spec = np.clip(spec, 0, 0.45) * mf
    # 원래 손톱이 반짝이 · 금박 · 크롬처럼 거친 질감이면 그 무늬가 팁에 옮겨오지 않게 반사광을 줄여요
    fine = L - cv2.GaussianBlur(L, (0, 0), max(1.0, w / 25.0))
    tex = float(np.sqrt((fine * fine * mf).sum() / max(1.0, mf.sum())))
    if tex > 6.0:
        spec *= max(0.0, 1.0 - (tex - 6.0) / 6.0)
    cov = float(((spec > 0.05) * mf).sum() / max(1.0, mf.sum()))
    if cov > 0.12:                                        # 하이라이트가 손톱을 넓게 덮으면 무늬로 보고 줄여요
        spec *= 0.12 / cov
    feather = cv2.GaussianBlur(mf, (0, 0), max(1.0, w / 12.0))
    shade = 1 + (shade - 1) * feather
    return shade, spec * feather


def _dir_word(dx, dy):
    """픽셀 방향 (dx, dy, y 는 아래로 +) -> 'LEFT', 'UP-RIGHT' 같은 사진 기준 방향."""
    import math
    a = math.degrees(math.atan2(-dy, dx)) % 360
    return ("RIGHT", "UP-RIGHT", "UP", "UP-LEFT", "LEFT", "DOWN-LEFT", "DOWN", "DOWN-RIGHT")[int((a + 22.5) // 45) % 8]


def orient_text(geos, fingers, W, H, tip_names=None):
    """프롬프트용: 손가락마다 손톱이 사진에서 어느 쪽을 향하는지 (뿌리 -> 끝) + 캐릭터 · 글자를 그 방향으로 눕히라는 규칙.
    geos: [{손가락 번호: {"tip", "base", ...}}] (손마다), W, H = 그 사진(이미지 1)의 픽셀 크기.
    tip_names: {손가락 번호: '이미지 N'} - 팁 사진 이름 (없으면 'its tip')."""
    parts = []
    for hi, geo in enumerate(geos or []):
        seen = []
        for k in fingers:
            g = (geo or {}).get(k)
            if not g:
                continue
            dx, dy = (g["tip"][0] - g["base"][0]) * W, (g["tip"][1] - g["base"][1]) * H
            if abs(dx) + abs(dy) < 1:
                continue
            seen.append(f"{FINGERS[k - 1]}: its fingertip end points {_dir_word(dx, dy)} in image 1, so the TOP of "
                        f"{(tip_names or {}).get(k, 'its tip')} points {_dir_word(dx, dy)}")
        if seen:
            parts.append((f"hand {hi + 1}: " if len(geos) > 1 else "") + "; ".join(seen))
    if not parts:
        return ""
    return (" DIRECTION OF EACH DESIGN (measured from image 1): " + " | ".join(parts) + ". Turn each whole tip "
            "design WITH its nail - including every character, animal face, letter, bow and motif on it: what is at the "
            "top of the tip image goes to the fingertip end, so on a nail pointing LEFT a cat face lies on its side "
            "with the top of its head to the LEFT, and on a nail pointing DOWN it is upside down. NEVER turn a "
            "character or motif upright to the viewer, never re-draw it in another direction or size, and never "
            "re-draw it in a different style - keep its exact drawing, only rotated, scaled and curved with the nail.")


def geo_points(g):
    """손톱 자리의 대표 점들 (사진 비율): 끝 · 뿌리 + 상자 꼭짓점 (자르기 · 가리지 않기 계산용)."""
    return [g["tip"], g["base"]] + list((g.get("box") or {}).get("corners") or ())


def geo_move(g, mv, w_scale=1.0):
    """손톱 자리를 다른 틀로 옮겨요 (mv: 비율 점 -> 비율 점). 상자 꼭짓점도 같이."""
    out = {"tip": mv(*g["tip"]), "base": mv(*g["base"])}
    if "w" in g:
        out["w"] = g["w"] * w_scale
    if g.get("box"):
        out["box"] = dict(g["box"], corners=tuple(mv(*c) for c in g["box"]["corners"]))
    return out


TIP_SIDE_WRAP = True        # 일부만 보이는 손톱(옆에서 본 엄지 등): 팁을 반 잘라 평평하게 붙이지 않고,
                            #   팁 전체를 보이는 상자 안에 둥근 손톱처럼 감아(먼 쪽일수록 좁게 · 어둡게) 붙여요
TIP_SIDE_SHADE = 0.30       # 돌아 들어가는 먼 쪽 가장자리를 이만큼 어둡게


def side_wrap(trgb, a, box, far_left):
    """팁 사진(배열) -> 옆에서 본 둥근 손톱처럼 가로로 감은 사진. box = 팁 상자 (x0, y0, x1, y1).
    far_left: 돌아 들어가 안 보이는 쪽이 팁 사진의 왼쪽이면 True. 화면 위치 s(0=가까운 쪽 ~ 1=먼 쪽) <- 팁 위치 u,
    s = sin(u * pi/2) (먼 쪽으로 갈수록 눌려 보여요). 상자 밖은 그대로."""
    import cv2, numpy as np
    h, w = a.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, x1 = max(0, x0), min(w, x1)
    bw = x1 - x0
    if bw < 4:
        return trgb, a, None
    s = (np.arange(bw, dtype=np.float32) + 0.5) / bw          # 화면 위치 (가까운 쪽 0 -> 먼 쪽 1)
    u = np.arcsin(np.clip(s, 0, 1)) / (np.pi / 2)              # 그 자리에 오는 팁 위치
    if far_left:
        s, u = s[::-1], u[::-1]
    mapx = np.tile((x0 + u * bw - 0.5).astype(np.float32), (h, 1))
    mapx = np.hstack([np.tile(np.arange(x0, dtype=np.float32), (h, 1)), mapx,
                      np.tile(np.arange(x1, w, dtype=np.float32), (h, 1))])
    mapy = np.tile(np.arange(h, dtype=np.float32)[:, None], (1, w))
    t2 = cv2.remap(np.ascontiguousarray(trgb), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    a2 = cv2.remap(np.ascontiguousarray(a), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    shade = np.ones(w, np.float32)
    shade[x0:x1] = 1.0 - TIP_SIDE_SHADE * s ** 2                # 먼 쪽(곡면이 꺾여 들어가는 쪽)일수록 어둡게
    return t2, a2, shade


def side_view_text(geos, fingers, tip_names=None):
    """프롬프트용: 일부만 보이는 손톱(옆모습 엄지 등)을 둥근 손톱으로 다시 그리라는 지시. 없으면 ''."""
    names = []
    for geo in geos or []:
        for k in fingers:
            g = (geo or {}).get(k)
            if g and box_part(g):
                n = f"the {FINGERS[k - 1]}" + (f" ({tip_names[k]})" if tip_names and k in tip_names else "")
                if n not in names:
                    names.append(n)
    if not names:
        return ""
    return (" SIDE-VIEW NAIL - " + " and ".join(names) + ": this finger is seen from the SIDE, so in image 1 only "
            "part of its nail is visible as a narrow strip along the top edge of the finger. Re-render it as a real, "
            "curved nail plate seen from the side: it sits flush on the fingertip and follows the finger's contour, "
            "with a visible cuticle and side wall; the design wraps around the curve and is foreshortened (narrower) "
            "toward the far edge that turns away from the camera, with soft shading and a highlight along the curve, "
            "like the thumbnail in a real side-view photo. It must NOT look like a flat sticker, card or slab standing "
            "off the finger and must not float above the skin. For this nail you may correct the laid outline and "
            "length to the thumb's real nail shape seen from this angle. Keep the same colors, finish and motifs "
            "(part of the design may be hidden around the curve).")


def box_part(g):
    """일부만 보이는 손톱(엄지 옆모습 등)이면 True - 상자 = 보이는 부분, visible = 손톱 전체 폭 중 보이는 비율."""
    return bool(g.get("box")) and g["box"].get("visible", 1.0) < 0.999


def box_px(g, W, H, grow=1.0, widen=1.0, base_inset=0.0, full=False):
    """상자 꼭짓점 (픽셀) [뿌리쪽1, 뿌리쪽2, 끝쪽2, 끝쪽1]. grow = 가운데 기준 폭 · 길이 확대,
    widen = 폭만 확대, base_inset = 뿌리쪽 변을 끝쪽으로 이만큼(길이 비율) 당겨요.
    full = 일부만 보이는 손톱이면 가려진 쪽(hidden_side)으로 넓힌 '손톱 전체' 상자."""
    import numpy as np
    P = np.array([(x * W, y * H) for x, y in g["box"]["corners"]], np.float64)
    if full and box_part(g):
        v = g["box"]["visible"]
        if g["box"].get("hidden_side", 2) == 2:
            P[1], P[2] = P[0] + (P[1] - P[0]) / v, P[3] + (P[2] - P[3]) / v
        else:
            P[0], P[3] = P[1] + (P[0] - P[1]) / v, P[2] + (P[3] - P[2]) / v
    if base_inset:
        P[0] += (P[3] - P[0]) * base_inset
        P[1] += (P[2] - P[1]) * base_inset
    b, t = (P[0] + P[1]) / 2, (P[3] + P[2]) / 2
    c = (b + t) / 2
    u = t - b
    u = u / (float(np.hypot(*u)) or 1.0)
    for i in range(4):
        d = P[i] - c
        a = float(d @ u)
        P[i] = c + u * a * grow + (d - u * a) * grow * widen
    return P


def box_outline(g, W, H, grow=1.0, n=10):
    """상자 안 손톱 윤곽 (픽셀 다각형) - 상자 꼭짓점대로 원근 · 기울기, 끝 · 뿌리 둥글기 반영 (편집기 미리보기와 같은 모양)."""
    import math
    import numpy as np
    P = box_px(g, W, H, grow, full=True)
    wpx = float(np.hypot(*((P[0] + P[3]) / 2 - (P[1] + P[2]) / 2))) or 1.0
    lpx = float(np.hypot(*((P[3] + P[2]) / 2 - (P[0] + P[1]) / 2))) or 1.0
    asp = wpx / lpx

    def rad(r):
        rs = min(0.5, r * 0.5)
        rv = rs * asp
        if rv > 0.5:
            rs, rv = rs * 0.5 / rv, 0.5
        return rs, rv
    ts, tv = rad(g["box"]["tip_round"])
    bs, bv = rad(g["box"]["base_round"])

    def at(s, v):
        return (1 - v) * ((1 - s) * P[0] + s * P[1]) + v * ((1 - s) * P[3] + s * P[2])
    pts = []
    for cs, cv_, rs, rv, a0, a1 in ((bs, bv, bs, bv, 1.5 * math.pi, math.pi),
                                    (ts, 1 - tv, ts, tv, math.pi, 0.5 * math.pi),
                                    (1 - ts, 1 - tv, ts, tv, 0.5 * math.pi, 0.0),
                                    (1 - bs, bv, bs, bv, 0.0, -0.5 * math.pi)):
        for i in range(n + 1):
            th = a0 + (a1 - a0) * i / n
            q = at(cs + rs * math.cos(th), cv_ + rv * math.sin(th))
            pts.append((float(q[0]), float(q[1])))
    if box_part(g):          # 일부만 보이면: 손톱 전체 윤곽 중 보이는 상자 안만
        import cv2
        V = box_px(g, W, H, grow).astype(np.float32)
        ol = np.float32(pts)
        try:
            area, cut = cv2.intersectConvexConvex(cv2.convexHull(ol), cv2.convexHull(V))
            if area > 1 and cut is not None and len(cut) >= 3:
                return [(float(x), float(y)) for x, y in cut.reshape(-1, 2)]
        except cv2.error:
            pass
        return [(float(x), float(y)) for x, y in V]
    return pts


def geo_outline(g, W, H, grow=1.0):
    """손톱 자리 -> 손톱 모양 다각형 (픽셀). 상자가 있으면 상자대로, 없으면 끝 · 뿌리 · 폭으로 그린 아몬드형."""
    if g.get("box"):
        return box_outline(g, W, H, grow)
    return nail_outline((g["tip"][0] * W, g["tip"][1] * H), (g["base"][0] * W, g["base"][1] * H), g["w"] * W, grow=grow)


def box_text(g):
    """프롬프트용: 상자로 정한 손톱의 보이는 각도 · 모양 (상자가 없으면 빈 문자열)."""
    b = (g or {}).get("box")
    if not b:
        return ""
    bits = []
    r = b.get("persp", 1.0)
    if r < 0.88:
        bits.append(f"seen at an angle, its fingertip end about {round((1 - r) * 100)}% narrower than its cuticle end "
                    "(perspective)")
    elif r > 1.12:
        bits.append(f"tilted toward the camera, its fingertip end about {round((r - 1) * 100)}% wider than its "
                    "cuticle end (perspective)")
    if b.get("visible", 1.0) < 0.999:
        c = b["corners"]
        hid = ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2) if b.get("hidden_side", 2) == 2 else \
              ((c[0][0] + c[3][0]) / 2, (c[0][1] + c[3][1]) / 2)
        cx, cy = sum(q[0] for q in c) / 4, sum(q[1] for q in c) / 4
        dx, dy = hid[0] - cx, hid[1] - cy
        way = ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("lower" if dy > 0 else "upper")
        bits.append(f"seen from the side: only about {round(b['visible'] * 100)}% of its nail width is visible and the "
                    f"rest curves away out of view on its {way} side, so show only the matching part of the design, "
                    "wrapping around the curve - never flatten the whole design into the visible part")
    tr = b.get("tip_round", 0.8)
    bits.append("a squarish fingertip end" if tr < 0.35 else "a rounded fingertip end" if tr > 0.7
                else "a softly rounded fingertip end")
    return " (" + ", ".join(bits) + "; keep that angle and the light falling on it)"


def nail_outline(tip, base, w, grow=1.0, n=32):
    """손톱 끝 점 · 뿌리 점 · 폭 (픽셀) -> 손톱 모양 다각형 (끝 쪽은 좁아지고 뿌리 쪽은 둥근 아몬드형).
    grow: 폭 · 길이를 이만큼 키워요 (원래 손톱을 넉넉히 덮을 때)."""
    import math
    (tx, ty), (bx, by) = tip, base
    L = math.hypot(tx - bx, ty - by) or 1.0
    ux, uy = (tx - bx) / L, (ty - by) / L
    px, py = -uy, ux
    ext = (grow - 1.0) * L * 0.5
    bx, by, L = bx - ux * ext, by - uy * ext, L + 2 * ext
    hw = 0.5 * w * grow

    def half(t):          # t: 0 = 뿌리, 1 = 끝
        if t >= 0.42:
            return hw * math.sqrt(max(0.0, 1 - ((t - 0.42) / 0.58) ** 2))
        return hw * math.sqrt(0.65 + 0.35 * max(0.0, 1 - ((0.42 - t) / 0.42) ** 2))

    side1 = [(bx + ux * L * k / n + px * half(k / n), by + uy * L * k / n + py * half(k / n)) for k in range(n + 1)]
    side2 = [(bx + ux * L * k / n - px * half(k / n), by + uy * L * k / n - py * half(k / n)) for k in range(n, -1, -1)]
    return side1 + side2


TIP_KEEP_LENGTH = True     # 손톱 길이 = 팁의 길이:폭 비율대로 (뿌리 · 폭 · 방향은 레퍼런스 손톱 그대로, 끝만 늘이거나 줄여요)
                           #   팁이 더 길면 레퍼런스 손톱 밖(피부 · 배경)까지 붙이고, 더 짧으면 남는 옛 손톱은 지워요
TIP_LEN_RANGE = (0.6, 1.35)  # 길이를 레퍼런스 손톱의 이 배 안에서만 바꿔요 (더 길게 하면 옆에서 본 엄지 등이 허공에 떠요)
TIP_LEN_MIN_REF = 0.8      # 레퍼런스 손톱의 길이:폭이 이보다 작으면(옆이나 끝에서 본 손톱) 길이를 바꾸지 않아요


def tip_length_geo(g, t, W, H, rng=None):
    """레퍼런스 손톱 자리 g + 팁 사진 t -> 팁의 길이:폭 비율대로 끝(tip)을 옮긴 손톱 자리 (뿌리 · 폭 · 방향 그대로).
    붙였을 때 팁의 길이:폭 = 팁 사진의 길이:폭이 되게 해요. 바꿀 수 없으면 g 그대로."""
    import math
    _, tb = tip_alpha(t)
    if not tb:
        return g
    r_tip = (tb[3] - tb[1]) / max(1.0, tb[2] - tb[0])
    (tx, ty), (bx, by) = (g["tip"][0] * W, g["tip"][1] * H), (g["base"][0] * W, g["base"][1] * H)
    L = math.hypot(tx - bx, ty - by)
    if g.get("box"):
        P = box_px(g, W, H)
        w = float(math.hypot(*((P[0] + P[3]) / 2 - (P[1] + P[2]) / 2)))
    else:
        w = g["w"] * W
    if L < 4 or w < 2 or box_part(g) or L / w < TIP_LEN_MIN_REF:
        return g
    want = r_tip * w * TIP_PASTE_WIDEN / max(0.5, 1.0 - TIP_PASTE_BASE_INSET)
    rng = rng or TIP_LEN_RANGE
    k = min(rng[1], max(rng[0], want / L))
    if abs(k - 1.0) < 0.03:
        return g
    g2 = dict(g, tip=((bx + (tx - bx) * k) / W, (by + (ty - by) * k) / H), length_k=k)
    if g.get("box"):
        P = box_px(g, W, H)
        P[3] = P[0] + (P[3] - P[0]) * k
        P[2] = P[1] + (P[2] - P[1]) * k
        g2["box"] = dict(g["box"], corners=tuple((float(x / W), float(y / H)) for x, y in P))
    return g2


def paste_tips(scene, geo, tips, fingers, light_src=None, len_range=None, soft=None):
    # soft: {손가락 번호: 진하기 0~1} - 모델이 다시 그릴 사진에서 그 손가락 팁을 흐리게 (힌트만). 예: 엄지
    """레퍼런스 사진(scene) + 손톱 자리(geo {손가락 번호: {"tip", "base", "w"}}) + 팁 사진들 -> 팁을 붙인 사진.
    tips[i] 는 fingers[i] 번 손가락(1=엄지 ... 5=새끼)의 팁. 팁 픽셀은 돌리기 · 크기 맞추기만 해요 (색 그대로).
    붙이기 전에 원래 손톱 자리를 둘레(피부 · 배경)로 메워서, 팁이 덮지 못한 가장자리에 옛 손톱 색이 남지 않게 해요."""
    import cv2, numpy as np
    from PIL import Image
    rgb0 = np.ascontiguousarray(np.asarray(scene.convert("RGB")))
    H, W = rgb0.shape[:2]
    # 빛은 원래 손톱에서 읽어요 (light_src = 같은 크기의 원래 레퍼런스. 없으면 scene 자신 - 지우기 전에)
    lsrc = rgb0 if light_src is None else np.ascontiguousarray(np.asarray(light_src.convert("RGB").resize((W, H))))
    lights = {}
    for k in fingers:
        g = geo.get(k)
        if g and TIP_RELIGHT:
            try:
                lights[k] = nail_light(lsrc, ((g["tip"][0] * W, g["tip"][1] * H), (g["base"][0] * W, g["base"][1] * H),
                                              g["w"] * W), k, outline=lambda gr, g=g: geo_outline(g, W, H, gr))
            except Exception as e:
                print(f"  ! 손톱 빛 읽기 실패 ({k}번): {type(e).__name__}: {e}")
    # 원래 손톱 모양 (레퍼런스에서 찾아요) - 팁을 이 모양 안에 자연스럽게 채워요
    targets = {}
    if TIP_FIT_NAIL:
        for k in fingers:
            g = geo.get(k)
            if g:
                try:
                    targets[k] = nail_ref_mask(lsrc, (g["tip"][0] * W, g["tip"][1] * H),
                                               (g["base"][0] * W, g["base"][1] * H), g["w"] * W,
                                               outline=lambda gr, g=g: geo_outline(g, W, H, gr))
                except Exception as e:
                    print(f"  ! 손톱 모양 찾기 실패 ({k}번): {type(e).__name__}: {e}")
    hole = np.zeros((H, W), np.uint8)
    for k in fingers:
        g = geo.get(k)
        if g:
            poly = geo_outline(g, W, H, grow=1.12)
            cv2.fillPoly(hole, [np.int32(np.round(poly))], 255)
            if k in targets:
                hole |= cv2.dilate(targets[k], np.ones((5, 5), np.uint8))
    if hole.any():
        r = max(3, int(0.004 * max(W, H)))
        rgb0 = cv2.inpaint(rgb0, cv2.dilate(hole, np.ones((3, 3), np.uint8)), r, cv2.INPAINT_TELEA)
    out = rgb0.astype(np.float32)
    for k, t in zip(fingers, tips):
        g = geo.get(k)
        if not g:
            continue
        a, box = tip_alpha(t)
        if a is None:
            continue
        if TIP_KEEP_LENGTH:               # 손톱 길이 = 팁 비율대로 (옛 손톱은 위에서 이미 다 지웠어요)
            g = tip_length_geo(g, t, W, H, len_range)
        trgb = np.ascontiguousarray(np.asarray(t.convert("RGB")).copy())
        wrap_shade = None
        if TIP_SIDE_WRAP and box_part(g):      # 옆모습: 팁 전체를 둥글게 감아서 보이는 상자에만 붙여요
            trgb, a, wrap_shade = side_wrap(trgb, a, box, far_left=g["box"].get("hidden_side", 2) == 2)
        fit = k in targets and not TIP_KEEP_LENGTH
        if fit:                           # 팁 바깥을 팁의 바탕색으로 이어 칠해요 -> 레퍼런스 손톱이 더 길거나 넓어도 자연스럽게
            outside = (a == 0).astype(np.uint8)
            if outside.any():
                trgb = cv2.inpaint(trgb, outside, max(5, trgb.shape[1] // 20), cv2.INPAINT_TELEA)
        trgb = trgb.astype(np.float32)
        if fit:
            a = np.full_like(a, 255)
        x0, y0, x1, y1 = box
        cx = (x0 + x1) / 2
        (tx, ty), (bx, by) = (g["tip"][0] * W, g["tip"][1] * H), (g["base"][0] * W, g["base"][1] * H)
        bx, by = bx + (tx - bx) * TIP_PASTE_BASE_INSET, by + (ty - by) * TIP_PASTE_BASE_INSET
        L = float(np.hypot(tx - bx, ty - by))
        if L < 4:
            continue
        ux, uy = (tx - bx) / L, (ty - by) / L          # 손톱 뿌리 -> 손톱 끝 방향
        rx, ry = -uy, ux                                 # 팁 사진의 오른쪽이 가는 방향 (뒤집지 않게)
        half = 0.5 * g["w"] * W * TIP_PASTE_WIDEN
        mx, my = (tx + bx) / 2, (ty + by) / 2
        if g.get("box"):                  # 상자 = 팁 사진 네 귀퉁이를 상자 꼭짓점에 (원근 · 기울기 그대로)
            # 일부만 보이는 손톱: 팁은 '손톱 전체' 상자에 붙이고, 보이는 상자 밖(돌아 들어간 쪽)은 잘라요
            Q = box_px(g, W, H, widen=TIP_PASTE_WIDEN, base_inset=TIP_PASTE_BASE_INSET,
                       full=not (TIP_SIDE_WRAP and box_part(g)))
            M = cv2.getPerspectiveTransform(np.float32([[x1, y1], [x0, y1], [x0, y0], [x1, y0]]), np.float32(Q))
            rgb = cv2.warpPerspective(np.clip(trgb, 0, 255).astype(np.uint8), M, (W, H), flags=cv2.INTER_LANCZOS4,
                                      borderMode=cv2.BORDER_REPLICATE)
            al = cv2.warpPerspective(a, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=255 if fit else 0)
            if wrap_shade is not None:         # 감은 팁의 곡면 명암 (먼 쪽 어둡게)
                sh = cv2.warpPerspective(np.tile(wrap_shade, (a.shape[0], 1)), M, (W, H), flags=cv2.INTER_LINEAR,
                                         borderValue=1.0)
                rgb = np.clip(rgb.astype(np.float32) * sh[..., None], 0, 255).astype(np.uint8)
            elif box_part(g):
                vm = np.zeros((H, W), np.uint8)
                cv2.fillPoly(vm, [np.int32(np.round(box_px(g, W, H, widen=TIP_PASTE_WIDEN,
                                                           base_inset=TIP_PASTE_BASE_INSET)))], 255)
                vm = cv2.GaussianBlur(vm, (3, 3), 0)
                al = (al.astype(np.float32) * (vm.astype(np.float32) / 255.0)).astype(np.uint8)
        else:
            src = np.float32([[cx, y0], [cx, y1], [x1, (y0 + y1) / 2]])
            dst = np.float32([[tx, ty], [bx, by], [mx + rx * half, my + ry * half]])
            M = cv2.getAffineTransform(src, dst)
            rgb = cv2.warpAffine(np.clip(trgb, 0, 255).astype(np.uint8), M, (W, H), flags=cv2.INTER_LANCZOS4,
                                 borderMode=cv2.BORDER_REPLICATE)
            al = cv2.warpAffine(a, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=255 if fit else 0)
        if fit:                           # 모양 = 레퍼런스의 원래 손톱 (가장자리는 1~2px 부드럽게)
            al = (al.astype(np.float32) / 255.0) * (cv2.GaussianBlur(targets[k], (0, 0), 1.2).astype(np.float32) / 255.0)
            al = al[..., None]
        else:
            al = cv2.GaussianBlur(al, (3, 3), 0).astype(np.float32)[..., None] / 255.0     # 가장자리만 살짝 부드럽게
        rgb = rgb.astype(np.float32)
        shade, spec = lights.get(k, (None, None))
        if shade is not None:             # 원래 손톱의 빛 방향 · 명암 · 반사광을 팁에 (색 · 무늬는 그대로, 밝기만)
            rgb = rgb * shade[..., None]
            rgb = rgb + (255.0 - rgb) * spec[..., None]
        if soft and k in soft:            # 모델이 다시 그릴 손가락(엄지): 팁을 흐리게 얹어 힌트만 줘요
            al = al * float(soft[k])
        out = out * (1 - al) + rgb * al
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def frame_ratio(img, keep, rw=None, rh=None):
    """rw:rh 비율로 맞춰요. keep(사진 비율 점들 - 손톱 끝 · 뿌리)이 모두 들어가게 잘라 보고,
    안 되면 자르지 않고 흐린 배경으로 넓혀요 (손톱이 잘리지 않게)."""
    from PIL import Image, ImageFilter
    rw, rh = rw or OUTPUT_RATIO[0], rh or OUTPUT_RATIO[1]
    W, H = img.size
    if W * rh == H * rw:
        return img
    xs = [p[0] * W for p in keep] or [W / 2]
    ys = [p[1] * H for p in keep] or [H / 2]
    pad = 0.04 * max(W, H)
    if W * rh > H * rw:                                  # 넓음 -> 양옆 자르기
        nw = round(H * rw / rh)
        lo, hi = max(xs) + pad - nw, min(xs) - pad       # 자르는 왼쪽 끝이 될 수 있는 범위
        if lo <= hi:
            x = int(min(max((lo + hi) / 2, 0), W - nw))
            if x <= min(xs) - pad + 1 and x + nw >= max(xs) + pad - 1:
                return img.crop((x, 0, x + nw, H))
        nw2, nh2 = W, round(W * rh / rw)
    else:                                                # 좁음 -> 위아래 자르기
        nh = round(W * rh / rw)
        lo, hi = max(ys) + pad - nh, min(ys) - pad
        if lo <= hi:
            y = int(min(max((lo + hi) / 2, 0), H - nh))
            if y <= min(ys) - pad + 1 and y + nh >= max(ys) + pad - 1:
                return img.crop((0, y, W, y + nh))
        nw2, nh2 = round(H * rw / rh), H
    # 자르면 손톱이 잘려요 -> 같은 사진을 크게 흐려서 바탕으로 깔고 가운데에 원본을 그대로
    bg = img.resize((max(nw2, W), max(nh2, H)), Image.LANCZOS).filter(ImageFilter.GaussianBlur(max(W, H) // 25))
    cw, ch = max(nw2, W), max(nh2, H)
    bg = bg.crop(((bg.width - cw) // 2, (bg.height - ch) // 2, (bg.width - cw) // 2 + cw, (bg.height - ch) // 2 + ch))
    bg.paste(img, ((cw - W) // 2, (ch - H) // 2))
    return bg


def _label_font(size, korean=False):
    from PIL import ImageFont
    for f in (("C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf") if korean else ()) +              ("C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def tip_chart(tips, korean=False, height=420, fingers=None, mirror=False):
    """팁 사진들을 왼쪽부터 나란히 + 아래에 '1 THUMB' 같은 손가락 이름 -> 손가락 배정표 한 장.
    fingers: 팁마다 손가락 번호 [1=엄지 ... 5=새끼] (None = 1번부터 차례대로)
    mirror: True(왼손) = 손 모양처럼 거울 배치 - 왼쪽부터 새끼 · 약지 · 중지 · 검지 · 엄지 (팁 사진은 뒤집지 않아요)"""
    from PIL import Image, ImageDraw
    if not tips:
        return None
    fingers = list(fingers or range(1, len(tips) + 1))
    if mirror:
        tips, fingers = list(reversed(tips)), list(reversed(fingers))
    labels = FINGER_LABELS_KO if korean else FINGER_LABELS_EN
    sc = [tip_flat(t).resize((max(1, round(t.width * height / t.height)), height), Image.LANCZOS) for t in tips]
    gap, lab_h = 24, height // 6
    W = sum(t.width for t in sc) + gap * (len(sc) + 1)
    chart = Image.new("RGB", (W, height + lab_h + gap * 2), (255, 255, 255))
    d = ImageDraw.Draw(chart)
    font = _label_font(int(lab_h * 0.6), korean)
    x = gap
    for k, t in enumerate(sc):
        chart.paste(t, (x, gap))
        d.rectangle((x - 2, gap - 2, x + t.width + 1, gap + height + 1), outline=(200, 190, 205), width=2)
        j = (fingers[k] - 1) if fingers and k < len(fingers) else k
        txt = labels[j] if j < len(labels) else str(j + 1)
        tw = d.textlength(txt, font=font)
        d.text((x + (t.width - tw) / 2, gap + height + lab_h * 0.2), txt, fill=(40, 30, 45), font=font)
        x += t.width + gap
    return chart


class _RetryFixedSize(Exception):
    """모델이 원하는 크기(WxH)를 거절했을 때 정해진 크기로 다시 보내기 위한 신호."""


class UserError(Exception):
    """사용자에게 그대로 보여 줄 오류 (status, 메시지, 오류코드 - api_schema.ErrorCode)."""

    DEFAULT_CODES = {400: "UPSTREAM_BAD_REQUEST", 404: "NOT_FOUND", 413: "FILE_TOO_LARGE", 422: "INVALID_REQUEST",
                     429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 504: "UPSTREAM_TIMEOUT"}

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status, self.msg = status, msg
        self.code = code or self.DEFAULT_CODES.get(status, "INTERNAL_ERROR")


def load_dotenv(path=os.path.join(ROOT, ".env")):
    """python-dotenv 없이 .env 의 KEY=VALUE 를 읽어요 (이미 있는 환경변수는 그대로 둬요)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v


def list_hands():
    out = []
    for p in sorted(glob.glob(os.path.join(HANDS, "*"))):
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in EXTS:
            n = os.path.basename(p)
            out.append({"id": n, "label": os.path.splitext(n)[0], "url": f"/hands/{n}"})
    return out


def nail_mask_for(hand_name, size):
    """흰색=손톱, 검은색=나머지 인 'L' 마스크. hands/masks/<이름>.png 가 우선, 없으면 nails.json 으로."""
    from PIL import Image
    stem = os.path.splitext(os.path.basename(hand_name))[0]
    p = os.path.join(MASKS, stem + ".png")
    if os.path.isfile(p):
        m = Image.open(p)
        if m.mode in ("RGBA", "LA") and m.getextrema()[-1][0] < 255:
            m = m.getchannel("A")          # 투명도로 그린 마스크
        else:
            m = m.convert("L")             # 흰/검 으로 그린 마스크 (mask_editor.html)
        return m.resize(size, Image.NEAREST) if m.size != size else m
    try:
        with open(os.path.join(HANDS, "nails.json"), encoding="utf-8") as f:
            entry = next(e for e in json.load(f) if e["file"] == os.path.basename(hand_name))
        from apply_on_hands import nail_mask
        return nail_mask(size, entry["nails"])
    except StopIteration:
        raise UserError(400, "이 손 사진의 손톱 마스크가 없어요 (hands/masks 또는 nails.json)", "MASK_MISSING")


def canvas_size(w, h):
    """API 가 받는 크기 중 손 사진 비율에 가장 가까운 것."""
    r = w / h
    return min([(1024, 1024), (1024, 1536), (1536, 1024)], key=lambda s: abs(s[0] / s[1] - r))


OUTPUT_RATIO = (4, 5)      # 배너/장면 결과 사진 비율 (가로:세로)


def pad_to_ratio(img, rw, rh):
    """가장자리 색을 늘여 rw:rh 비율로 넓혀요 (자르지 않으니 손이 잘리지 않아요). -> (사진, 넓혔는지)"""
    import cv2, numpy as np
    from PIL import Image
    W, H = img.size
    if abs(W * rh - H * rw) <= max(W, H) // 100:
        return img, False
    if W * rh > H * rw:                       # 너무 넓음 -> 위아래를 넓혀요
        add = round(W * rh / rw) - H; t, b, l, r = add // 2, add - add // 2, 0, 0
    else:                                     # 너무 좁음 -> 양옆을 넓혀요
        add = round(H * rw / rh) - W; t, b, l, r = 0, 0, add // 2, add - add // 2
    a = cv2.copyMakeBorder(np.asarray(img.convert("RGB")), t, b, l, r, cv2.BORDER_REPLICATE)
    return Image.fromarray(a), True


def crop_to_ratio(img, rw=OUTPUT_RATIO[0], rh=OUTPUT_RATIO[1]):
    """가운데를 기준으로 rw:rh 비율로 잘라요 (이미 그 비율이면 그대로)."""
    W, H = img.size
    if W * rh == H * rw:
        return img
    if W * rh > H * rw:
        nw = round(H * rw / rh); x = (W - nw) // 2
        return img.crop((x, 0, x + nw, H))
    nh = round(W * rh / rw); y = (H - nh) // 2
    return img.crop((0, y, W, y + nh))


def png_bytes(img):
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def nail_crop_box(nails, size, padding):
    """손톱들을 감싸는 영역 + 여유(padding x 손톱영역 크기). 손가락 모양이 보일 만큼은 남겨요."""
    W, H = size
    x0, y0, x1, y1 = nails.getbbox()
    pad = max(int(max(x1 - x0, y1 - y0) * padding), int(min(W, H) * 0.08))
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    if (x1 - x0) * (y1 - y0) > 0.8 * W * H:      # 거의 전체면 그냥 전체
        return (0, 0, W, H)
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------------------------- 긴 손톱
# 손 사진 마스크는 원래 짧은 손톱 크기라서, 길고 뾰족한 디자인은 손끝에서 잘려요.
# -> 손톱마다 손가락 방향으로 마스크를 늘이고(길이), 끝으로 갈수록 좁혀요(모양).
NAIL_LENGTHS = ("keep", "short", "medium", "long", "extra_long")
NAIL_SHAPES = ("almond", "oval", "round", "square", "squoval", "coffin", "stiletto")
NAIL_TAPER = {"stiletto": 0.92, "almond": 0.7, "oval": 0.5, "round": 0.4, "coffin": 0.38, "squoval": 0.18,
              "square": 0.08, "unknown": 0.5}
NAIL_LENGTH_KO = {"keep": "원본 길이", "short": "짧게", "medium": "중간", "long": "길게", "extra_long": "아주 길게"}
NAIL_SHAPE_KO = {"almond": "아몬드", "oval": "오벌", "round": "라운드", "square": "스퀘어", "squoval": "스퀘오벌",
                 "coffin": "코핀(발레리나)", "stiletto": "스틸레토(뾰족)", "unknown": "자동"}


def extend_nails(mask, hand_img, ratio, taper):
    """손톱 마스크(L, 255=손톱)를 손가락 끝 방향으로 ratio x 손톱길이 만큼 늘이고, taper(0~1) 만큼 끝을 좁혀요."""
    import cv2, numpy as np
    from PIL import Image
    if ratio <= 0:
        return mask
    m = (np.asarray(mask) >= 128).astype(np.uint8)
    H, W = m.shape
    # 손(배경이 아닌 곳): 사진 가장자리 색 = 배경색
    h = np.asarray(hand_img.convert("RGB").resize((W, H)), np.float32)
    b = max(4, min(H, W) // 40)
    bg = np.median(np.concatenate([h[:b].reshape(-1, 3), h[:, :b].reshape(-1, 3), h[:, -b:].reshape(-1, 3)]), axis=0)
    fg = (np.linalg.norm(h - bg, axis=2) > 40) & (m == 0)
    ys, xs = np.nonzero(fg)
    hand_c = np.array([xs.mean(), ys.mean()]) if len(xs) > 100 else np.array([W / 2, H])
    out = m.copy()
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    min_area = max(30, m.size // 20000)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        comp = (lab == i).astype(np.uint8)
        py, px = np.nonzero(comp)
        pts = np.stack([px, py], 1).astype(np.float64)
        c = pts.mean(0)
        size = max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT])
        # 손가락 방향: 손톱 둘레의 '손' 부분(= 손가락 쪽) -> 손톱 중심. 손끝 너머는 배경이라 안 잡혀요.
        k = max(3, int(size * 0.9)) | 1
        ring = cv2.dilate(comp, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))).astype(bool) & fg
        ry, rx = np.nonzero(ring)
        v = c - (np.array([rx.mean(), ry.mean()]) if len(rx) > 50 else hand_c)
        v /= (np.linalg.norm(v) or 1)
        # 손톱 자체의 긴 축도 섞어요 (길쭉할수록 더 믿어요)
        ev, evec = np.linalg.eigh(np.cov((pts - c).T))
        axis = evec[:, int(np.argmax(ev))]
        if axis @ v < 0:
            axis = -axis
        elong = ev.max() / max(ev.min(), 1e-6)
        w = 0.5 if elong > 2.2 else 0.3 if elong > 1.4 else 0.0
        d = axis * w + v * (1 - w)
        d /= (np.linalg.norm(d) or 1)
        proj = (pts - c) @ d
        length = proj.max() - proj.min()
        tip = c + d * proj.max()
        ext = ratio * length
        steps = max(8, int(ext / 2))
        for t in np.linspace(0, 1, steps + 1)[1:]:
            s = max(0.04, 1 - taper * t)
            # 끝점(tip) 기준으로 s 배 줄이고 손가락 방향으로 ext*t 만큼 옮긴 손톱 = 늘어나며 좁아지는 모양
            M = np.float32([[s, 0, (1 - s) * tip[0] + d[0] * ext * t],
                            [0, s, (1 - s) * tip[1] + d[1] * ext * t]])
            out |= cv2.warpAffine(comp, M, (W, H), flags=cv2.INTER_NEAREST)
    out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    return Image.fromarray(out * 255)


def extend_nails_padded(hand_img, mask, ratio, taper):
    """늘인 손톱이 사진 밖으로 나가면 잘리지 않게, 손 사진 가장자리(배경)를 그만큼 넓혀요. -> (손 사진, 마스크)"""
    import cv2, numpy as np
    from PIL import Image
    W, H = hand_img.size
    P = int(max(W, H) * 0.5)
    h = cv2.copyMakeBorder(np.asarray(hand_img.convert("RGB")), P, P, P, P, cv2.BORDER_REPLICATE)
    m = cv2.copyMakeBorder(np.asarray(mask), P, P, P, P, cv2.BORDER_CONSTANT, value=0)
    hp, mp = Image.fromarray(h), Image.fromarray(m)
    ext = extend_nails(mp, hp, ratio, taper)
    x0, y0, x1, y1 = ext.getbbox()
    pad = int(min(W, H) * 0.04)                        # 손톱 끝과 사진 끝 사이 여유
    box = (max(0, min(P, x0 - pad)), max(0, min(P, y0 - pad)),
           min(W + 2 * P, max(P + W, x1 + pad)), min(H + 2 * P, max(P + H, y1 + pad)))
    return hp.crop(box), ext.crop(box)


def nail_plan(analysis, length="auto", shape="auto", settings=None):
    """요청(auto/직접) + 인식 결과 -> (길이, 모양, 늘이는 비율, 좁히는 정도)."""
    ext = (settings or DEFAULTS)["nail_extend"]
    an = analysis or {}
    L = length if length in NAIL_LENGTHS else "auto"
    S_ = shape if shape in NAIL_SHAPES else "auto"
    if S_ == "auto":
        S_ = an.get("nail_shape") if an.get("nail_shape") in NAIL_SHAPES else "unknown"
    if L == "auto":
        if not ext["enabled"]:
            L = "keep"
        else:
            L = an.get("nail_length") if an.get("nail_length") in NAIL_LENGTHS else "keep"
            if L == "keep" and S_ in ("stiletto", "coffin"):     # 길이를 못 봤어도 뾰족/코핀이면 길게
                L = "long"
    ratio = {"keep": 0.0, "short": 0.0}.get(L, float(ext.get(L, 0) or 0))
    return L, S_, ratio, NAIL_TAPER.get(S_, 0.5)


def _nail_parts(nails):
    import cv2, numpy as np
    m = (np.asarray(nails) >= 128).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    min_area = max(30, m.size // 20000)
    return [(lab == i) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area]


def count_nails(nails):
    try:
        return len(_nail_parts(nails))
    except Exception:
        return 0


def check_nails(result, hand, nails):
    """손톱마다: 배경색이 칠해졌는지(검은 손톱 문제), 원본 그대로인지 확인."""
    import numpy as np
    res = {"total": 0, "ok": 0, "bg": 0, "unchanged": 0}
    try:
        parts = _nail_parts(nails)
    except Exception:
        return res
    r, h = np.asarray(result, np.float32), np.asarray(hand, np.float32)
    b = max(4, min(h.shape[:2]) // 40)           # 사진 가장자리 = 배경색
    border = np.concatenate([h[:b].reshape(-1, 3), h[:, :b].reshape(-1, 3), h[:, -b:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    for p in parts:
        px, orig = r[p], h[p]
        near_bg = (np.linalg.norm(px - bg, axis=1) < 38).mean()
        orig_bg = (np.linalg.norm(orig - bg, axis=1) < 38).mean()
        same = (np.abs(px - orig).sum(1) < 24).mean()
        res["total"] += 1
        if near_bg - orig_bg > 0.3:
            res["bg"] += 1
        elif same > 0.85:
            res["unchanged"] += 1
        else:
            res["ok"] += 1
    return res


def align_to(out, ref, keep):
    """out(API 결과)을 ref(보낸 사진)에 맞춰 이동/크기/회전 보정. keep=비교에 쓸 영역(흰색)."""
    info = {"used": True, "ok": False, "dx": 0.0, "dy": 0.0, "scale": 1.0}
    try:
        import cv2, numpy as np
        from PIL import Image
        w, h = ref.size
        k = 512 / max(w, h)
        sw, sh = round(w * k), round(h * k)
        a = np.asarray(ref.convert("L").resize((sw, sh), Image.BILINEAR), np.float32)
        b = np.asarray(out.convert("L").resize((sw, sh), Image.BILINEAR), np.float32)
        m = (np.asarray(keep.resize((sw, sh), Image.NEAREST)) > 0).astype(np.uint8)
        (dx, dy), _ = cv2.phaseCorrelate(a, b)                 # 먼저 대략 이동량
        warp = np.array([[1, 0, dx], [0, 1, dy]], np.float32)
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
        try:
            cc, warp = cv2.findTransformECC(a, b, warp, cv2.MOTION_AFFINE, crit, m, 5)
        except cv2.error:
            cc = None
        scale = float(np.sqrt(abs(np.linalg.det(warp[:, :2]))))
        tx, ty = float(warp[0, 2] / k), float(warp[1, 2] / k)
        info.update(dx=tx, dy=ty, scale=scale, cc=None if cc is None else float(cc))
        # 말이 안 되는 보정(크게 틀어짐)은 버려요
        if cc is None or not (0.85 < scale < 1.15) or abs(tx) > 0.15 * w or abs(ty) > 0.15 * h:
            return out, info
        full = warp.copy(); full[:, 2] /= k
        arr = cv2.warpAffine(np.asarray(out), full, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_REPLICATE)
        info["ok"] = True
        return Image.fromarray(arr), info
    except Exception as e:
        info["error"] = repr(e)
        return out, info


def save_debug(hand_name, images, text=None):
    """outputs_api/<시각>_<손>/ 에 디자인 참고·보낸 사진·마스크·API 원본·보정본·결과·프롬프트를 저장 (문제 확인용)."""
    try:
        d = os.path.join(ROOT, "outputs_api", datetime.now().strftime("%Y%m%d_%H%M%S_") + os.path.splitext(hand_name)[0])
        os.makedirs(d, exist_ok=True)
        for k, im in images.items():
            im.save(os.path.join(d, k + ".png"))
        if text:
            with open(os.path.join(d, "prompt_and_analysis.json"), "w", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        print("  디버그 이미지 저장 실패:", e)


# ----------------------------------------------------------------------------------------------
# 비용 계산 / 기록
# ----------------------------------------------------------------------------------------------
def _f(obj, name, default=None):
    """OpenAI 응답 값 읽기. SDK 가 모르는 항목은 dict 로 오기도 해서 둘 다 받아요."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(name, default)
    else:
        v = getattr(obj, name, default)
    return default if v is None else v


LOG_FIELDS = ["time", "hand", "model", "quality", "complexity", "size", "seconds",
              "text_in", "image_in", "text_out", "image_out", "total_tokens", "image_usd",
              "analysis_model", "analysis_in", "analysis_out", "analysis_usd", "usd", "krw"]


# ---------------------------------------------------------------------------------------------- 실험 보고서 (엑셀)
# 요청(업로드 1번)마다 reports/실험보고서_<시각>.xlsx 를 자동으로 저장하고, reports/실험보고서_누적.xlsx 도 새로 써요.
#   시트: 요약 · 세트별 결과(고른 레퍼런스 · 총 걸린 시간 · 비용) · 단계별 비용 · 시간(luna / Image-2 호출마다) · 단계 합계
# 비용 · 시간은 CostTracker 가 기록하는 순간 지금 스레드에 묶인 보고서로 같이 들어가요 (run_bind).
REPORT_DIR = os.path.join(ROOT, "reports")
REPORT_RUNS = os.path.join(REPORT_DIR, "runs.jsonl")      # 누적 보고서용 원본 기록 (실행마다 1줄)
_RUN_BIND = {}                                             # 스레드 번호 -> (보고서, 세트 번호 또는 None)


def run_bind(report, item=None):
    if report is not None:
        _RUN_BIND[threading.get_ident()] = (report, item)


def run_unbind():
    _RUN_BIND.pop(threading.get_ident(), None)


def run_note(**fields):
    """지금 스레드가 맡은 세트의 보고서 칸을 채워요 (예: 동그란 사진 레퍼런스)."""
    rb = _RUN_BIND.get(threading.get_ident())
    if rb and rb[1] is not None:
        rb[0].item(rb[1], **fields)


def _stage_name(label, model):
    lab = str(label or "")
    if lab.startswith("(평가"):
        return lab.strip("()").replace("평가: ", "평가 · ") + " (luna)"
    named = {"(디자인 인식)": "디자인 인식 · 손 고르기 (luna)", "(세트 나누기)": "세트 나누기 (luna)",
             "(가격 읽기)": "가격 읽기 (luna)", "(팁 다시 확인)": "팁 다시 확인 (luna)",
             "(배너 계획서)": "배너 계획서 (luna)"}
    if lab in named:
        return named[lab]
    if model == "tip-paste":
        return "팁 붙이기 (프로그램, 요금 없음)"
    if lab.startswith("guide:"):
        return "동그란 사진 (Image-2)"
    if lab.startswith("scene:"):
        return "장면 사진 네일 · 배경 (Image-2)"
    if lab.startswith("배너:"):
        return "배너 (Image-2)"
    return "네일 입히기 (Image-2)"


def run_step(label, model, seconds=None, quality="", size="", tokens_in=None, tokens_out=None, usd=None):
    rb = _RUN_BIND.get(threading.get_ident())
    if rb:
        rb[0].step(rb[1], _stage_name(label, model), model, seconds, quality, size, tokens_in, tokens_out, usd)


def _r3(v):
    return None if v is None else round(float(v), 3)


class RunReport:
    def __init__(self, meta, krw=0.0):
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.started = datetime.now()
        self.id = self.started.strftime("%Y%m%d_%H%M%S")
        self.meta, self.krw = dict(meta), float(krw or 0)
        self.steps, self.items = [], {}
        self.ev_detect, self.ev_crop, self.ev_finger = [], [], []      # 자동 평가 (luna 심사)

    def add_detect(self, photo, res):
        if res:
            with self.lock:
                p, r, f = prf(res["tp"], res["fp"], res["fn"])
                self.ev_detect.append({"사진": photo, "찍은 점(찾은 팁)": res["markers"], "luna가 센 팁": res["real"],
                                       "맞게 찾음(TP)": res["tp"], "엉뚱하게 찾음(FP)": res["fp"],
                                       "놓침(FN)": res["fn"], "정밀도": _r3(p), "재현율": _r3(r), "F1": _r3(f),
                                       "luna 메모": res["notes"]})

    def add_crops(self, i, res):
        if res:
            with self.lock:
                for c in res:
                    self.ev_crop.append({"세트": i + 1, "팁": c["tip"], "상자 IoU": _r3(c["iou"]),
                                         "Dice": _r3(c["dice"]), "잘림": c["cut_off"], "옆 팁 섞임": c["other_tip"]})
            ious = [c["iou"] for c in res if c["iou"] is not None]
            self.item(i, **{"오리기 IoU 평균": _r3(sum(ious) / len(ious)) if ious else None,
                            "잘린 팁 수": sum(1 for c in res if c["cut_off"])})

    def add_fingers(self, i, seen, n_tips):
        if not seen:
            return
        names = ["thumb", "index", "middle", "ring", "little"]
        ko = ["엄지", "검지", "중지", "약지", "새끼"]
        pairs = []
        with self.lock:
            for k, nm in enumerate(names[:n_tips]):
                g = int(seen.get(nm, 0) or 0)
                self.ev_finger.append({"세트": i + 1, "손가락": ko[k], "넣어야 할 팁": k + 1,
                                       "luna가 본 팁": g if g else "판단 안 함",
                                       "맞음": ("" if not g else g == k + 1)})
                pairs.append((k + 1, g))
        acc, _, mf1 = finger_f1(pairs)
        self.item(i, **{"손가락 배정 정확도": _r3(acc), "손가락 배정 F1": _r3(mf1)})

    def eval_summary(self):
        tp = sum(d["맞게 찾음(TP)"] for d in self.ev_detect)
        fp = sum(d["엉뚱하게 찾음(FP)"] for d in self.ev_detect)
        fn = sum(d["놓침(FN)"] for d in self.ev_detect)
        p, r, f = prf(tp, fp, fn) if self.ev_detect else (None, None, None)
        ious = [c["상자 IoU"] for c in self.ev_crop if c["상자 IoU"] is not None]
        dices = [c["Dice"] for c in self.ev_crop if c["Dice"] is not None]
        pairs = [(e["넣어야 할 팁"], e["luna가 본 팁"] if isinstance(e["luna가 본 팁"], int) else 0) for e in self.ev_finger]
        acc, per, mf1 = finger_f1(pairs)
        out = {"[평가] 팁 찾기 정밀도": _r3(p), "[평가] 팁 찾기 재현율": _r3(r), "[평가] 팁 찾기 F1": _r3(f),
               "[평가] 팁 오리기 IoU 평균": _r3(sum(ious) / len(ious)) if ious else None,
               "[평가] 팁 오리기 Dice 평균": _r3(sum(dices) / len(dices)) if dices else None,
               "[평가] 잘린 팁 수": sum(1 for c in self.ev_crop if c["잘림"] is True) if self.ev_crop else None,
               "[평가] 손가락 배정 정확도": _r3(acc), "[평가] 손가락 배정 F1(매크로)": _r3(mf1)}
        ko = {1: "엄지", 2: "검지", 3: "중지", 4: "약지", 5: "새끼"}
        for c, v in per.items():
            out[f"[평가] {ko.get(c, c)} F1"] = _r3(v)
        if any(v is not None for v in out.values()):
            out["[평가] 정답 기준"] = "luna 심사 (사람 정답 아님)"
        return out

    def step(self, item, stage, model, seconds, quality, size, tin, tout, usd):
        with self.lock:
            self.steps.append({"시각": datetime.now().strftime("%H:%M:%S"),
                               "세트": "전체" if item is None else item + 1, "단계": stage, "모델": model,
                               "품질": quality or "", "크기": size or "",
                               "걸린 시간(초)": None if seconds is None else round(float(seconds), 1),
                               "입력 토큰": tin, "출력 토큰": tout,
                               "비용(USD)": None if usd is None else round(float(usd), 5),
                               "비용(원)": None if usd is None or not self.krw else round(float(usd) * self.krw, 1)})

    def item(self, i, **fields):
        with self.lock:
            self.items.setdefault(i, {"세트": i + 1}).update(fields)

    # ------------------------------------------------------------------ 저장
    REPORT_SETTING_KEYS = ("원본 참조율(original_ratio)", "자연스러움 레벨(natural_level)")   # 세트별 결과에도 넣는 설정

    def rows(self):
        steps = list(self.steps)
        items = []
        for i in sorted(self.items):
            it = dict(self.items[i])
            for k in self.REPORT_SETTING_KEYS:          # 실험 때의 설정 -> 세트마다 (누적 시트에서 비교하기 쉽게)
                if k in self.meta:
                    it.setdefault(k, self.meta[k])
            mine = [s for s in steps if s["세트"] == i + 1]
            usd = sum(s["비용(USD)"] or 0 for s in mine)
            it["luna 호출"] = sum(1 for s in mine if "luna" in s["단계"])
            it["Image-2 호출"] = sum(1 for s in mine if "Image-2" in s["단계"])
            it["세트 비용(USD)"] = round(usd, 5)
            it["세트 비용(원)"] = round(usd * self.krw, 1) if self.krw else None
            it["총 걸린 시간(초)"] = round((it.get("팁 나누기 · 확인 시간(초)") or 0) + (it.get("생성 시간(초)") or 0), 1)
            order = ["세트", "원본 사진", "세트 설명", "고른 레퍼런스", "레퍼런스 고른 방법", "동그란 사진 레퍼런스", "상태",
                     "총 걸린 시간(초)", "팁 나누기 · 확인 시간(초)", "생성 시간(초)", "luna 호출", "Image-2 호출",
                     "세트 비용(USD)", "세트 비용(원)", "오리기 IoU 평균", "잘린 팁 수", "손가락 배정 정확도",
                     "손가락 배정 F1", "원본 참조율(original_ratio)", "자연스러움 레벨(natural_level)"]
            items.append({**{k: it[k] for k in order if k in it}, **{k: v for k, v in it.items() if k not in order}})
        total = sum(s["비용(USD)"] or 0 for s in steps)
        summary = {"실험 번호": self.id, "시작": self.started.strftime("%Y-%m-%d %H:%M:%S"),
                   **self.meta,
                   "총 걸린 시간(초)": round(time.time() - self.t0, 1),
                   "세트 수": len(items),
                   "완료": sum(1 for it in items if it.get("상태") == "완료"),
                   "실패": sum(1 for it in items if str(it.get("상태", "")).startswith("실패")),
                   "luna 호출": sum(1 for s in steps if "luna" in s["단계"]),
                   "Image-2 호출": sum(1 for s in steps if "Image-2" in s["단계"]),
                   "총 비용(USD)": round(total, 5),
                   "총 비용(원)": round(total * self.krw, 1) if self.krw else None,
                   **self.eval_summary()}
        agg = {}
        for s in steps:
            a = agg.setdefault(s["단계"], {"단계": s["단계"], "호출 수": 0, "시간 합계(초)": 0.0, "비용 합계(USD)": 0.0})
            a["호출 수"] += 1
            a["시간 합계(초)"] += s["걸린 시간(초)"] or 0
            a["비용 합계(USD)"] += s["비용(USD)"] or 0
        for a in agg.values():
            a["평균 시간(초)"] = round(a["시간 합계(초)"] / a["호출 수"], 1)
            a["평균 비용(USD)"] = round(a["비용 합계(USD)"] / a["호출 수"], 5)
            a["시간 합계(초)"] = round(a["시간 합계(초)"], 1)
            a["비용 합계(USD)"] = round(a["비용 합계(USD)"], 5)
            a["비용 합계(원)"] = round(a["비용 합계(USD)"] * self.krw, 1) if self.krw else None
        return summary, items, steps, list(agg.values())

    def save(self):
        """reports/ 에 이번 실험 엑셀 + 누적 엑셀. 실패해도 생성 결과에는 영향 없어요."""
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            summary, items, steps, agg = self.rows()
            path = os.path.join(REPORT_DIR, f"실험보고서_{self.id}.xlsx")
            write_xlsx(path, [("요약", [{"항목": k, "값": v} for k, v in summary.items()]),
                              ("세트별 결과", items), ("단계별 비용·시간", steps), ("단계 합계", agg),
                              ("평가-팁 찾기", self.ev_detect), ("평가-팁 오리기", self.ev_crop),
                              ("평가-손가락 배정", self.ev_finger)])
            with open(REPORT_RUNS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"summary": summary, "items": items, "steps": steps, "ev_detect": self.ev_detect,
                                    "ev_crop": self.ev_crop, "ev_finger": self.ev_finger}, ensure_ascii=False,
                                   default=str) + "\n")
            print(f"  실험 보고서 저장: reports/{os.path.basename(path)}")
            self.save_total()
        except Exception as e:
            print(f"  ! 실험 보고서를 저장하지 못했어요: {type(e).__name__}: {e}")

    @staticmethod
    def total_eval(runs):
        """모든 실험을 합친 평가 지표 1줄."""
        det = [x for r in runs for x in r.get("ev_detect", [])]
        crop = [x for r in runs for x in r.get("ev_crop", [])]
        fin = [x for r in runs for x in r.get("ev_finger", [])]
        tp = sum(d.get("맞게 찾음(TP)", 0) for d in det)
        fp = sum(d.get("엉뚱하게 찾음(FP)", 0) for d in det)
        fn = sum(d.get("놓침(FN)", 0) for d in det)
        p, r_, f = prf(tp, fp, fn) if det else (None, None, None)
        ious = [c["상자 IoU"] for c in crop if c.get("상자 IoU") is not None]
        dices = [c["Dice"] for c in crop if c.get("Dice") is not None]
        acc, per, mf1 = finger_f1([(e["넣어야 할 팁"], e["luna가 본 팁"] if isinstance(e["luna가 본 팁"], int) else 0)
                                   for e in fin])
        return {"실험 수": len(runs), "평가한 사진": len(det), "팁 찾기 정밀도": _r3(p), "팁 찾기 재현율": _r3(r_),
                "팁 찾기 F1": _r3(f), "평가한 팁(오리기)": len(crop),
                "팁 오리기 IoU 평균": _r3(sum(ious) / len(ious)) if ious else None,
                "팁 오리기 Dice 평균": _r3(sum(dices) / len(dices)) if dices else None,
                "평가한 손가락": sum(1 for e in fin if isinstance(e["luna가 본 팁"], int)),
                "손가락 배정 정확도": _r3(acc), "손가락 배정 F1(매크로)": _r3(mf1),
                **{f"{ {1: '엄지', 2: '검지', 3: '중지', 4: '약지', 5: '새끼'}.get(c, c)} F1": _r3(v) for c, v in per.items()},
                "정답 기준": "luna 심사 (사람 정답 아님)"}

    @staticmethod
    def save_total():
        runs = []
        with open(REPORT_RUNS, encoding="utf-8") as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except Exception:
                    pass
        tag = lambda r, rows: [{"실험 번호": r["summary"].get("실험 번호"), **x} for x in rows]
        path = os.path.join(REPORT_DIR, "실험보고서_누적.xlsx")
        try:
            write_xlsx(path, [("실험 목록", [r["summary"] for r in runs]),
                              ("세트별 결과", [x for r in runs for x in tag(r, r.get("items", []))]),
                              ("단계별 비용·시간", [x for r in runs for x in tag(r, r.get("steps", []))]),
                              ("평가-팁 찾기", [x for r in runs for x in tag(r, r.get("ev_detect", []))]),
                              ("평가-팁 오리기", [x for r in runs for x in tag(r, r.get("ev_crop", []))]),
                              ("평가-손가락 배정", [x for r in runs for x in tag(r, r.get("ev_finger", []))]),
                              ("평가 누적", [RunReport.total_eval(runs)])])
        except PermissionError:
            print("  ! 실험보고서_누적.xlsx 가 엑셀에서 열려 있어 새로 쓰지 못했어요 (이번 실험 파일은 저장됐어요)")


def write_xlsx(path, sheets):
    """[(시트 이름, [dict 행])] -> .xlsx (외부 패키지 없이). 첫 줄 = 굵은 제목, 숫자는 숫자 칸, 첫 줄 고정."""
    import zipfile
    from xml.sax.saxutils import escape

    def col(n):
        s = ""
        n += 1
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell(ref, v, style=0):
        st = f' s="{style}"' if style else ""
        if v is None or v == "":
            return f'<c r="{ref}"{st}/>'
        if isinstance(v, bool):
            v = "예" if v else "아니요"
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"{st}><v>{v}</v></c>'
        t = escape(str(v)).replace("\n", "&#10;")
        return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{t}</t></is></c>'

    xml_sheets = []
    for _, rows in sheets:
        heads = []
        for r in rows:
            for k in r:
                if k not in heads:
                    heads.append(k)
        widths = [max([len(str(h))] + [len(str(r.get(h, "") or "")) for r in rows]) for h in heads]
        cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(60, max(8, w * 1.6 + 2)):.1f}" customWidth="1"/>'
                       for i, w in enumerate(widths))
        out = ['<row r="1">' + "".join(cell(f"{col(i)}1", h, 1) for i, h in enumerate(heads)) + "</row>"]
        for n, r in enumerate(rows, start=2):
            out.append(f'<row r="{n}">' + "".join(cell(f"{col(i)}{n}", r.get(h)) for i, h in enumerate(heads)) + "</row>")
        xml_sheets.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{cols}</cols>" if cols else "") + "<sheetData>" + "".join(out) + "</sheetData></worksheet>")
    names = [escape(n[:31]) for n, _ in sheets]
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets))) + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font>'
                   '<font><b/><sz val="11"/><name val="맑은 고딕"/></font></fonts>'
                   '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FFF3E8EE"/><bgColor indexed="64"/></patternFill></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
                   "</styleSheet>")
        for i, x in enumerate(xml_sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", x)
    os.replace(tmp, path)


class CostTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.reload_prices()
        self.today = {"date": "", "count": 0, "usd": 0.0}
        self.total = {"count": 0, "usd": 0.0}
        today = datetime.now().strftime("%Y-%m-%d")
        self.today["date"] = today
        if os.path.isfile(USAGE_LOG):           # 예전 형식(열이 다름)이면 이름을 바꿔 보관하고 새로 시작
            try:
                with open(USAGE_LOG, encoding="utf-8-sig", newline="") as f:
                    header = next(csv.reader(f), [])
                if header != LOG_FIELDS:
                    old = USAGE_LOG.replace(".csv", datetime.now().strftime("_old_%Y%m%d_%H%M%S.csv"))
                    os.replace(USAGE_LOG, old)
                    print(f"usage_log.csv 형식이 바뀌어 예전 기록은 {os.path.basename(old)} 로 보관했어요 (누적에는 포함)")
                    self._olds = [old]
            except Exception:
                pass
        for path in getattr(self, "_olds", []) + [USAGE_LOG]:   # 껐다 켜도 누적이 이어지게
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        usd = float(row.get("usd") or 0)
                        self.total["count"] += 1; self.total["usd"] += usd
                        if (row.get("time") or "").startswith(today):
                            self.today["count"] += 1; self.today["usd"] += usd
            except Exception as e:
                print("usage_log.csv 를 읽지 못했어요 (새로 기록해요):", e)

    def reload_prices(self):
        try:
            with open(PRICES_FILE, encoding="utf-8") as f:
                p = json.load(f)
            self.prices, self.krw = p.get("models", {}), float(p.get("usd_krw", 0) or 0)
            self.text_prices = p.get("text_models", {})
        except Exception as e:
            print("prices.json 을 읽지 못해서 비용은 토큰만 보여 줘요:", e)
            self.prices, self.krw, self.text_prices = {}, 0.0, {}

    @staticmethod
    def _lookup(table, model):
        if model in table:
            return table[model]
        keys = [k for k in table if model.startswith(k)]
        return table[max(keys, key=len)] if keys else None

    def price_for(self, model):
        return self._lookup(self.prices, model)

    def text_cost(self, usage, model):
        """디자인 인식(Responses API) 사용량 -> dict(토큰, usd)."""
        inp = int(_f(usage, "input_tokens", 0))
        out = int(_f(usage, "output_tokens", 0))
        cached = int(_f(_f(usage, "input_tokens_details"), "cached_tokens", 0))
        reasoning = int(_f(_f(usage, "output_tokens_details"), "reasoning_tokens", 0))
        p = self._lookup(self.text_prices, model)
        usd = None if p is None else ((inp - cached) * p["input"] + cached * p.get("cached_input", p["input"])
                                      + out * p["output"]) / 1_000_000
        return {"model": model, "input": inp, "cached": cached, "output": out, "reasoning": reasoning, "usd": usd}

    def record(self, usage, model, quality, complexity, size, seconds, hand, analysis=None):
        """usage(OpenAI 응답) -> 토큰/비용 dict. 기록하고 누적도 올려요."""
        t = {"text_in": 0, "image_in": 0, "text_out": 0, "image_out": 0, "total_tokens": 0}
        if usage is not None:
            ind = _f(usage, "input_tokens_details")
            outd = _f(usage, "output_tokens_details")
            if ind:
                t["text_in"], t["image_in"] = int(_f(ind, "text_tokens", 0)), int(_f(ind, "image_tokens", 0))
            else:
                t["image_in"] = int(_f(usage, "input_tokens", 0))
            if outd:
                t["text_out"], t["image_out"] = int(_f(outd, "text_tokens", 0)), int(_f(outd, "image_tokens", 0))
            else:
                t["image_out"] = int(_f(usage, "output_tokens", 0))
            t["total_tokens"] = int(_f(usage, "total_tokens", 0)) or sum(t.values())
        p = self.price_for(model)
        image_usd = None
        if p and usage is not None:
            image_usd = (t["text_in"] * p.get("text_in", 0) + t["image_in"] * p.get("image_in", 0)
                         + t["text_out"] * p.get("text_out", 0) + t["image_out"] * p.get("image_out", 0)) / 1_000_000
        # 인식 비용은 인식할 때 따로 기록했어요. 여기서는 '이번 결과에 든 비용' 표시용으로만 더해요.
        an_usd = (analysis or {}).get("billed_usd")
        usd = image_usd
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.today["date"] != today:
                self.today = {"date": today, "count": 0, "usd": 0.0}
            self.today["count"] += 1; self.total["count"] += 1
            self.today["usd"] += usd or 0; self.total["usd"] += usd or 0
            row = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hand": hand, "model": model,
                   "quality": quality, "complexity": complexity or "", "size": size, "seconds": round(seconds, 1),
                   **t, "image_usd": "" if image_usd is None else round(image_usd, 5),
                   "analysis_model": "", "analysis_in": "", "analysis_out": "", "analysis_usd": "",
                   "usd": "" if usd is None else round(usd, 5),
                   "krw": "" if usd is None or not self.krw else round(usd * self.krw, 1)}
            new = not os.path.isfile(USAGE_LOG)
            try:
                with open(USAGE_LOG, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
            except PermissionError:
                print("  ! usage_log.csv 가 엑셀에서 열려 있어 기록하지 못했어요. 엑셀을 닫아 주세요.")
            run_step(hand, model, seconds, quality, size, t["text_in"] + t["image_in"], t["text_out"] + t["image_out"],
                     image_usd if usage is not None else (0.0 if model == "tip-paste" else None))
            shown = None if usd is None else usd + (an_usd or 0)      # 이번 결과 비용 = 생성 + (이 사진 첫 사용이면) 인식
            info = {**t, "model": model, "quality": quality, "complexity": complexity or "",
                    "usd": shown, "krw": shown * self.krw if shown is not None and self.krw else None,
                    "image_usd": image_usd, "analysis_usd": an_usd if analysis else None,
                    "priced": p is not None, "today": self.summary()["today"]}
        return info

    def record_analysis_only(self, an, hand, label="(인식만, 생성 안 함)"):
        """인식만 하고 생성은 안 한 경우(네일 없는 사진 등)도 비용은 나가니 기록해요. 배너 계획서 비용도 여기로."""
        usd = an.get("usd")
        run_step(hand, an.get("model", ""), an.get("seconds"), an.get("reasoning_effort", ""), "", an.get("input"),
                 an.get("output"), usd)
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.today["date"] != today:
                self.today = {"date": today, "count": 0, "usd": 0.0}
            self.today["usd"] += usd or 0; self.total["usd"] += usd or 0
            row = {k: "" for k in LOG_FIELDS}
            row.update({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hand": hand, "model": label,
                        "analysis_model": an.get("model", ""), "analysis_in": an.get("input", ""),
                        "analysis_out": an.get("output", ""), "analysis_usd": "" if usd is None else round(usd, 5),
                        "usd": "" if usd is None else round(usd, 5),
                        "krw": "" if usd is None or not self.krw else round(usd * self.krw, 1)})
            new = not os.path.isfile(USAGE_LOG)
            try:
                with open(USAGE_LOG, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
            except PermissionError:
                print("  ! usage_log.csv 가 엑셀에서 열려 있어 기록하지 못했어요. 엑셀을 닫아 주세요.")

    def summary(self):
        k = self.krw
        def pack(d):
            return {"count": d["count"], "usd": round(d["usd"], 4), "krw": round(d["usd"] * k) if k else None}
        return {"today": pack(self.today), "total": pack(self.total), "usd_krw": k}


def fmt_cost(usd, krw):
    if usd is None:
        return "가격표에 없는 모델 (prices.json 에 추가하세요)"
    return f"${usd:.4f}" + (f" (약 {krw:,.0f}원)" if krw is not None else "")


class OpenAIModel:
    def __init__(self, a):
        load_dotenv()
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("OPENAI_API_KEY 가 없어요. .env 파일에 OPENAI_API_KEY=sk-... 를 넣거나 환경변수로 설정하세요.")
        from openai import OpenAI
        self.a = a
        o = a.settings["openai"]
        # 적용 순서: 실행 옵션 > settings.yaml > .env > 기본값
        if a.model:
            self.model, src_m = a.model, "실행 옵션 --model"
        elif a.settings_loaded and o["model"]:
            self.model, src_m = o["model"], "settings.yaml"
        elif os.environ.get("OPENAI_IMAGE_MODEL"):
            self.model, src_m = os.environ["OPENAI_IMAGE_MODEL"], ".env"
        else:
            self.model, src_m = DEFAULTS["openai"]["model"], "기본값"
        if a.effort:
            eff, src_e = a.effort, "실행 옵션 --effort"
        elif a.settings_loaded and o["complexity"]:
            eff, src_e = o["complexity"], "settings.yaml"
        elif os.environ.get("OPENAI_IMAGE_COMPLEXITY"):
            eff, src_e = os.environ["OPENAI_IMAGE_COMPLEXITY"].strip().lower(), ".env"
        else:
            eff, src_e = "", "모델 기본값"
        if eff and eff not in COMPLEXITY:
            sys.exit(f"구상 강도(complexity)는 low / medium / high 중 하나예요 (지금: {eff}, {src_e})")
        self.complexity = eff or None
        self.fidelity = o["input_fidelity"]
        for env_k, val, src in (("OPENAI_IMAGE_MODEL", self.model, src_m), ("OPENAI_IMAGE_COMPLEXITY", eff, src_e)):
            ev = (os.environ.get(env_k) or "").strip()
            if ev and src != ".env" and ev.lower() != (val or "").lower():
                print(f"  (알림) .env 의 {env_k}={ev} 는 {src} 값이 우선이라 쓰이지 않아요")
        self.dropped = set()          # 이 모델이 받지 않는다고 확인된 옵션
        self.bad_efforts = set()      # (인식 모델, effort) 중 거절된 조합
        self.client = OpenAI(timeout=a.timeout, max_retries=2)
        self.slots = threading.Semaphore(max(1, a.concurrency))   # 동시에 보내는 요청 수 (요금/속도 제한 보호)
        self.masks = {}
        self.analysis_cache = {}
        self.analysis_pending = {}
        self.analysis_lock = threading.Lock()
        self.no_flex_size = False
        self.cost = CostTracker()
        if self.model == "gpt-image-1":
            print("! gpt-image-1 은 2026-10-23 에 종료됐어요. gpt-image-1.5 / gpt-image-2 등으로 바꾸세요.")
        if not self.cost.price_for(self.model):
            print(f"! prices.json 에 '{self.model}' 가격이 없어서 비용 대신 토큰만 보여 줘요.")
        s = self.cost.summary()
        print(f"OpenAI 이미지 모델: {self.model} ({src_m})  |  구상 강도(complexity): {self.complexity or '모델 기본값'} ({src_e})")
        print(f"품질: 빠르게={QUALITY[512]}, 기본={QUALITY[768]}, 고화질={QUALITY[1024]}  |  input_fidelity: {self.fidelity}")
        da = a.settings["design_analysis"]
        if da["enabled"]:
            print(f"디자인 인식: {da['model']} (reasoning_effort: {da['reasoning_effort'] or '모델 기본값'})"
                  + ("" if self.cost._lookup(self.cost.text_prices, da["model"]) else "  ! prices.json 에 가격 없음"))
        else:
            print("디자인 인식: 끔 (사진을 그대로 참고 이미지로 써요)")
        self.sheet_hands, self.sheet_url = [], None
        if da["enabled"] and da["choose_hand"]:
            try:
                self.sheet_hands = list_hands()
                sheet = hand_contact_sheet(self.sheet_hands)
                if sheet:
                    buf = io.BytesIO(); sheet.save(buf, "JPEG", quality=88)
                    self.sheet_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                    print(f"손 사진 자동 선택: 켬 ({len(self.sheet_hands)}장 중 디자인에 맞는 손을 {da['model']} 가 골라요)")
            except Exception as e:
                print("! 손 사진 모음을 만들지 못해서 자동 선택을 꺼요:", e)
                self.sheet_hands, self.sheet_url = [], None
        self.scene_hands, self.scene_url = [], None
        if da["enabled"]:
            try:
                self.scene_hands = list_scenes()
                sheet = scene_contact_sheet(self.scene_hands)
                if sheet:
                    buf = io.BytesIO(); sheet.save(buf, "JPEG", quality=85)
                    self.scene_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                    print(f"장면 사진(nailhand): {len(self.scene_hands)}장 · 분위기 "
                          f"{len(set(x['category'] for x in self.scene_hands))}가지")
            except Exception as e:
                print("! nailhand 장면 사진 모음을 만들지 못했어요:", e)
                self.scene_hands, self.scene_url = [], None
        # 레퍼런스 손 정보는 파일을 읽기만 해요 (인식 모델 호출 · 파일 쓰기 없음, 요금 없음)
        #   nailhand/hand_fingers.json(손톱 위치) + hand_side.json(왼손/오른손) + thumb_visible.json(엄지 보임)
        #   틀린 곳은 이 파일들을 직접 고치고 서버를 다시 켜면 돼요
        self.sides, self.fingers = {}, {}
        try:
            self.sides, self.fingers = load_ref_hands(NAILHAND, self.scene_hands or list_scenes())
        except Exception as e:
            print(f"! nailhand 손 정보 파일을 읽지 못했어요 (손가락은 이미지 모델이 알아서 찾아요): {type(e).__name__}: {e}")
        self.guides = {}
        try:
            b0 = a.settings["banner"]
            self.guides = load_guide_hands({"left": b0.get("guide_left"), "right": b0.get("guide_right")})
            if self.guides:
                print("참조용 손 (reference): " + ", ".join(
                    f"{'왼손' if s == 'left' else '오른손'} {os.path.basename(g['path'])} 손톱 {len(g['geo'])}개"
                    for s, g in self.guides.items()))
        except Exception as e:
            print(f"! 참조용 손(reference)을 읽지 못했어요 (예시 없이 그려요): {type(e).__name__}: {e}")
        b = a.settings["banner"]
        print(f"배너: {b['size']} · 계획서 {b['plan_model'] or da['model']} · 최대 {b['max_designs']}장"
              + (" · 글자 넣기" if b["text_on_image"] else " · 글자 없음"))
        print(f"지금까지 사용: 오늘 {s['today']['count']}장 {fmt_cost(s['today']['usd'], s['today']['krw'])}"
              f" · 전체 {s['total']['count']}장 {fmt_cost(s['total']['usd'], s['total']['krw'])}")

    def hand_side(self, hand_id):
        """손 사진이 왼손인지 오른손인지 양손인지 ('left' | 'right' | 'both'). 모르면 오른손.
        레퍼런스 손가락 인식(hand_fingers.json)이 있으면 그걸 먼저 써요."""
        hands = self.ref_hands(hand_id)
        if hands:
            sides = {h["side"] for h in hands}
            return "both" if len(sides) > 1 else sides.pop()
        return self.sides.get(hand_id) or self.sides.get(os.path.basename(str(hand_id))) or "right"

    def ref_hands(self, hand_id):
        """레퍼런스 사진의 손들 [{"side", "nails": {1~5: (x, y)}}] (보이는 손톱만).
        nailhand/hand_fingers.json + hand_side.json + thumb_visible.json 을 읽은 그대로 (load_ref_hands). 없으면 None."""
        f = getattr(self, "fingers", {}) or {}
        return f.get(hand_id) or f.get(os.path.basename(str(hand_id))) or None

    # ------------------------------------------------------------------------------------------
    # 1단계: 업로드한 사진에서 네일 디자인 인식 (Responses API + 비전 모델)
    # ------------------------------------------------------------------------------------------
    def _responses(self, kw, model, effort):
        """인식 모델(Responses API) 호출. effort 를 이 모델이 안 받으면 그 값만 기억해 두고 빼고 다시 보내요."""
        import openai
        if effort and (model, effort) not in self.bad_efforts:
            kw["reasoning"] = {"effort": effort}
        try:
            return self.client.responses.create(**kw)
        except openai.BadRequestError as e:
            if "reasoning" in kw and "reason" in (getattr(e, "message", "") or str(e)).lower():
                kw.pop("reasoning"); self.bad_efforts.add((model, effort))
                print(f"  ! {model} 는 reasoning_effort={effort} 를 받지 않아서 빼고 다시 보내요")
                return self.client.responses.create(**kw)
            raise

    def read_prices(self, text):
        """추가 요청 글 -> luna 가 읽은 (가격 라벨 ['6.5', '7.0', ...] (만원 단위), 가격을 뺀 나머지 글).
        숫자 해석은 모두 luna 가 해요 (PRICE_INSTRUCTIONS 규칙). 실패하면 ([], 원래 글)."""
        text = str(text or "").strip()
        if not text:
            return [], ""
        da = self.a.settings["design_analysis"]
        kw = dict(model=da["model"], instructions=PRICE_INSTRUCTIONS, max_output_tokens=2000,
                  input=[{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                  text={"format": {"type": "json_schema", "name": "prices", "schema": PRICE_SCHEMA, "strict": True}})
        t0 = time.time()
        try:
            r = self._responses(kw, da["model"], "low")
            data = json.loads(r.output_text)
        except Exception as e:
            print(f"  ! 가격 읽기 실패 -> 가격표 없이 진행: {type(e).__name__}: {str(e)[:200]}")
            return [], text
        try:
            cost = self.cost.text_cost(getattr(r, "usage", None), da["model"])
            cost["seconds"] = round(time.time() - t0, 1)
            self.cost.record_analysis_only(cost, "(가격 읽기)", label="(가격 읽기)")
        except Exception:
            pass
        labels = [str(x).strip() for x in data.get("prices") or [] if str(x).strip()]
        return labels, str(data.get("rest") or "").strip()

    def _judge(self, label, instructions, schema, content, effort="low"):
        """평가용 luna 호출 1번 -> dict (실패하면 None). 비용 · 시간은 보고서 '평가' 단계로 기록."""
        da = self.a.settings["design_analysis"]
        kw = dict(model=da["model"], instructions=instructions, max_output_tokens=4000,
                  input=[{"role": "user", "content": content}],
                  text={"format": {"type": "json_schema", "name": "grade", "schema": schema, "strict": True}})
        t0 = time.time()
        try:
            r = self._responses(kw, da["model"], effort)
            data = json.loads(r.output_text)
        except Exception as e:
            print(f"  ! 자동 평가 실패 ({label}): {type(e).__name__}: {str(e)[:150]}")
            return None
        try:
            cost = self.cost.text_cost(getattr(r, "usage", None), da["model"])
            cost["seconds"] = round(time.time() - t0, 1)
            self.cost.record_analysis_only(cost, label, label=label)
        except Exception:
            pass
        return data

    def luna_place(self, img, geo, fingers, tag=""):
        """뾰족한 팁: luna 가 손가락마다 큐티클 · 손가락 방향을 다시 봐서 손톱 자리를 고쳐요 -> (새 geo, 고친 손가락 수)."""
        import math
        from PIL import ImageDraw
        names = ["thumb", "index", "middle", "ring", "little"]
        ks = [k for k in fingers if k in geo]
        if not ks:
            return geo, 0
        im = img.convert("RGB").copy()
        f = min(1.0, 1280 / max(im.size))
        im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))))
        W, H = im.size
        ann = im.copy()
        d = ImageDraw.Draw(ann)
        cols = [(255, 60, 60), (40, 200, 90), (60, 140, 255), (255, 170, 0), (200, 60, 255)]
        font = _price_font(max(14, W // 40))
        for k in ks:
            g = geo[k]
            b, tp = (g["base"][0] * W, g["base"][1] * H), (g["tip"][0] * W, g["tip"][1] * H)
            ex = (b[0] + (tp[0] - b[0]) * 1.6, b[1] + (tp[1] - b[1]) * 1.6)     # 긴 팁이 갈 방향까지
            c = cols[k - 1]
            d.line([b, ex], fill=c, width=max(2, W // 300))
            r = max(4, W // 160)
            d.ellipse((b[0] - r, b[1] - r, b[0] + r, b[1] + r), fill=c, outline="white")
            d.text((ex[0], ex[1]), "TIMRL"[k - 1], fill=c, font=font, anchor="mm", stroke_width=2, stroke_fill="white")
        data = self._judge("(뾰족한 팁 위치 보정)", LUNA_PLACE_INSTRUCTIONS, LUNA_PLACE_SCHEMA, [
            {"type": "input_text", "text": "Fingers to check: " + ", ".join(names[k - 1] for k in ks) + "."},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(im, 90), "detail": "high"},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(ann, 90), "detail": "high"}],
            effort="medium")
        if not data:
            return geo, 0
        out, fixed = dict(geo), 0
        ok = lambda q: isinstance(q, list) and len(q) == 2 and all(0.0 <= float(v) <= 1.0 for v in q)
        for it in data.get("fingers") or []:
            if it.get("finger") not in names:
                continue
            k = names.index(it["finger"]) + 1
            if k not in out or not it.get("needs_fix") or not (ok(it.get("cuticle")) and ok(it.get("direction_point"))):
                continue
            g = out[k]
            nb = (float(it["cuticle"][0]) * W, float(it["cuticle"][1]) * H)
            nd = (float(it["direction_point"][0]) * W - nb[0], float(it["direction_point"][1]) * H - nb[1])
            ob = (g["base"][0] * W, g["base"][1] * H)
            od = (g["tip"][0] * W - ob[0], g["tip"][1] * H - ob[1])
            if math.hypot(*nd) < 3 or math.hypot(*od) < 3:
                continue
            turn = abs((math.degrees(math.atan2(nd[1], nd[0]) - math.atan2(od[1], od[0])) + 180) % 360 - 180)
            wpx = max(4.0, float(g.get("w", 0.05)) * W)
            shift = math.hypot(nb[0] - ob[0], nb[1] - ob[1])
            th = k == 1                                  # 엄지는 더 많이 고쳐요
            mn_t, mn_s = (4, 0.15) if th else (POINTED_MIN_TURN, POINTED_MIN_SHIFT)
            mx_t, mx_s = (40, 0.6) if th else (POINTED_MAX_TURN, POINTED_MAX_SHIFT)
            if turn < mn_t and shift < mn_s * wpx:
                print(f"  (뾰족한 팁 위치 보정) {it['finger']}: 차이가 작아 JSON 위치 그대로 (회전 {turn:.0f}도, 이동 {shift / wpx:.2f}폭)")
                continue
            if turn > mx_t or shift > mx_s * wpx:
                print(f"  (뾰족한 팁 위치 보정) {it['finger']}: luna 제안이 너무 커서 무시 (회전 {turn:.0f}도, 이동 {shift / wpx:.2f}폭)")
                continue
            out[k] = geo_redirect(g, W, H, nb, nd)
            fixed += 1
            print(f"  (뾰족한 팁 위치 보정) {it['finger']}: 회전 {turn:.0f}도, 큐티클 이동 {shift / wpx:.2f}폭")
        run_note(**{"뾰족한 팁 위치 보정" + (f"({tag})" if tag else ""): f"{fixed}/{len(ks)}개 손가락"})
        return out, fixed

    def judge_detection(self, img, sets):
        """세트 나누기 결과 평가 -> {"markers", "tp", "fp", "fn", "real", "notes"} 또는 None."""
        from PIL import ImageDraw
        if not EVAL_JUDGE or not sets:
            return None
        im = img.convert("RGB").copy()
        im.thumbnail((1536, 1536))
        W, H = im.size
        d = ImageDraw.Draw(im)
        n = 0
        for st in sets:
            L = float(st.get("tip_length") or 0.1) * H
            r = max(7, int(0.09 * L))
            font = _price_font(int(r * 1.3))
            for tp in st.get("tips") or []:
                if "x" not in tp or "y" not in tp:
                    continue
                n += 1
                x, y = float(tp["x"]) * W, float(tp["y"]) * H
                d.ellipse((x - r, y - r, x + r, y + r), fill=(220, 30, 60), outline="white", width=2)
                d.text((x, y), str(n), fill="white", font=font, anchor="mm")
        if not n:
            return None
        data = self._judge("(평가: 팁 찾기)", EVAL_DETECT_INSTRUCTIONS, EVAL_DETECT_SCHEMA, [
            {"type": "input_text", "text": f"The detector placed {n} markers (numbered 1-{n})."},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(im, 90), "detail": "high"}])
        if not data:
            return None
        wrong = {int(k) for k in data.get("wrong_markers") or [] if 1 <= int(k) <= n}
        tp_ = n - len(wrong)
        return {"markers": n, "tp": tp_, "fp": len(wrong), "fn": max(0, int(data.get("missed_tips") or 0)),
                "real": int(data.get("real_tip_count") or 0), "notes": data.get("notes_ko", "")}

    def judge_crops(self, tips):
        """팁 하나씩 자른 결과 평가 -> [{"tip", "iou", "dice", "cut_off", "other_tip"}] 또는 None."""
        if not EVAL_JUDGE or not tips:
            return None
        content = [{"type": "input_text", "text": f"{len(tips)} crops, image 1 = tip 1 ... image {len(tips)} = tip {len(tips)}."}]
        mine = []
        for t in tips:
            raw = t.info.get("tip_raw") or t
            box = t.info.get("tip_box")
            mine.append(None if box is None else [box[0] / t.width, box[1] / t.height, box[2] / t.width, box[3] / t.height])
            cc = raw.convert("RGB").copy()
            cc.thumbnail((512, 512))
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(cc, 88),
                            "detail": "high"})
        data = self._judge("(평가: 팁 오리기)", EVAL_CROP_INSTRUCTIONS, EVAL_CROP_SCHEMA, content)
        if not data:
            return None
        out = []
        for it in data.get("tips") or []:
            k = int(it.get("number", 0)) - 1
            b = it.get("box") or []
            if not (0 <= k < len(tips)) or len(b) != 4:
                continue
            ref = [min(max(float(v), 0.0), 1.0) for v in b]
            iou, dice = box_iou_dice(mine[k], ref) if mine[k] else (None, None)
            out.append({"tip": k + 1, "iou": iou, "dice": dice, "cut_off": bool(it.get("cut_off")),
                        "other_tip": bool(it.get("other_tip_inside"))})
        return out

    def judge_fingers(self, result, tips):
        """결과 사진 평가 -> {"thumb": 팁 번호(0 = 판단 안 함), ...} 또는 None."""
        if not EVAL_JUDGE or result is None or not tips:
            return None
        im = result.convert("RGB").copy()
        im.thumbnail((1280, 1280))
        content = [{"type": "input_text", "text": f"Image 1 = result. Images 2-{len(tips) + 1} = tips 1-{len(tips)}."},
                   {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(im, 90), "detail": "high"}]
        for t in tips:
            cc = t.convert("RGB").copy()
            cc.thumbnail((384, 384))
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(cc, 88),
                            "detail": "low"})
        data = self._judge("(평가: 손가락 배정)", EVAL_FINGER_INSTRUCTIONS, EVAL_FINGER_SCHEMA, content)
        if not data:
            return None
        return {f["finger"]: int(f.get("tip_number") or 0) for f in data.get("fingers") or [] if f.get("finger")}

    def verify_tips(self, img, pts, ends, bases, L, doubt, crops):
        """세트 사진 + 점수가 낮은 팁들(doubt, 0부터) + 그 잘린 결과 -> luna 가 본 팁 위치 {k: {"pt", "end", "base"}} (세트 사진 픽셀).
        잘린 결과가 괜찮다고 하면 그 팁은 빼요. 실패하면 {}."""
        from PIL import ImageDraw
        da = self.a.settings["design_analysis"]
        W, H = img.size
        f = min(1.0, 1536 / max(W, H))
        im = img.convert("RGB").resize((max(1, round(W * f)), max(1, round(H * f))))
        d = ImageDraw.Draw(im)
        r = max(9, int(0.09 * L * f))
        font = _price_font(int(r * 1.4))
        for k, (x, y) in enumerate(pts):          # 번호표: 팁 뿌리 너머(팁을 가리지 않게)
            b = bases[k]
            if b:
                lx, ly = b[0] + (b[0] - x) * 0.35, b[1] + (b[1] - y) * 0.35
            else:
                lx, ly = x, y + 0.65 * L
            lx, ly = min(max(lx * f, r), im.width - r), min(max(ly * f, r), im.height - r)
            d.ellipse((lx - r, ly - r, lx + r, ly + r), fill=(220, 30, 60))
            d.text((lx, ly), str(k + 1), fill="white", font=font, anchor="mm")
        nums = [k + 1 for k in doubt]
        content = [{"type": "input_text", "text": (
            f"Image 1 = the tip set with red number labels. Check tips {nums}. The next images are the automatic crops "
            f"of those tips in the same order: " + ", ".join(f"image {i + 2} = crop of tip {n}" for i, n in enumerate(nums))
            + ".")},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(im, 90), "detail": "high"}]
        for c in crops:
            cc = c.convert("RGB"); cc.thumbnail((384, 384))
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(cc, 88),
                            "detail": "high"})
        kw = dict(model=da["model"], instructions=TIP_VERIFY_INSTRUCTIONS, max_output_tokens=4000,
                  input=[{"role": "user", "content": content}],
                  text={"format": {"type": "json_schema", "name": "tip_check", "schema": TIP_VERIFY_SCHEMA, "strict": True}})
        t0 = time.time()
        r_ = self._responses(kw, da["model"], "low")
        data = json.loads(r_.output_text)
        try:
            cost = self.cost.text_cost(getattr(r_, "usage", None), da["model"])
            cost["seconds"] = round(time.time() - t0, 1)
            self.cost.record_analysis_only(cost, "(팁 다시 확인)", label="(팁 다시 확인)")
        except Exception:
            pass
        out = {}
        ok = lambda q: isinstance(q, list) and len(q) == 2 and all(0.0 <= float(v) <= 1.0 for v in q)
        for it in data.get("tips") or []:
            k = int(it.get("number", 0)) - 1
            if k not in doubt:
                continue                           # crop_ok 라고 해도 위치로 다시 잘라 보고 점수로 골라요
            c, e, b = it.get("center"), it.get("free_edge"), it.get("base")
            if not (ok(c) and ok(e) and ok(b)):
                continue
            px = lambda q: (float(q[0]) * W, float(q[1]) * H)
            out[k] = {"pt": px(c), "end": px(e), "base": px(b)}
            print(f"  [팁 다시 확인] {k + 1}번 팁: " + ("luna: 괜찮음" if it.get("crop_ok") else it.get("problem_ko", "")))
        print(f"  (luna 팁 다시 확인: 팁 {nums} -> 고칠 팁 {[k + 1 for k in out]}, {time.time() - t0:.1f}초)")
        return out

    def _ref_sheet_content(self):
        """inspect_set 에 붙이는 nail_ref 표본 한 장 (luna 가 세트와 가장 닮은 표본을 골라요). 표본이 없으면 []."""
        if not NAIL_REF_USE:
            return []
        if not hasattr(self, "_ref_sheet_b64"):
            sh = nail_ref_sheet()
            self._ref_sheet_b64 = b64_jpeg(sh, 90) if sh is not None else None
        if not self._ref_sheet_b64:
            return []
        return [{"type": "input_text", "text": "Image 2: numbered nail OUTLINE SAMPLES (black silhouettes) for ref_sample."},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + self._ref_sheet_b64, "detail": "low"}]

    def measure_tips(self, crops):
        """잘라 세운 팁 사진들 -> luna 가 잰 팁마다 {k: {"free_edge": (x, y) px, "base": (x, y) px, "width": px}} (사진 픽셀).
        팁 사진 여러 장을 한 번에 (세트당 1번 호출). 실패하면 {}."""
        da = self.a.settings["design_analysis"]
        content = [{"type": "input_text", "text": f"{len(crops)} tip images, numbered 1 to {len(crops)} in order."}]
        for i, c in enumerate(crops):
            cc = c.convert("RGB").copy(); cc.thumbnail((512, 512))
            content += [{"type": "input_text", "text": f"Image {i + 1}:"},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(cc, 88), "detail": "high"}]
        kw = dict(model=da["model"], instructions=TIP_MEASURE_INSTRUCTIONS, max_output_tokens=3000,
                  input=[{"role": "user", "content": content}],
                  text={"format": {"type": "json_schema", "name": "tip_axis", "schema": TIP_MEASURE_SCHEMA, "strict": True}})
        t0 = time.time()
        r = self._responses(kw, da["model"], "low")
        data = json.loads(r.output_text)
        try:
            cost = self.cost.text_cost(getattr(r, "usage", None), da["model"])
            cost["seconds"] = round(time.time() - t0, 1)
            self.cost.record_analysis_only(cost, "(팁 축 재기)", label="(팁 축 재기)")
        except Exception:
            pass
        out = {}
        ok2 = lambda q: isinstance(q, list) and len(q) == 2 and all(isinstance(v, (int, float)) for v in q)
        for it in data.get("tips") or []:
            k = int(it.get("number", 0)) - 1
            if not (0 <= k < len(crops)) or not it.get("visible", True):
                continue
            if not (ok2(it.get("free_edge")) and ok2(it.get("base"))):
                continue
            W, H = crops[k].size
            cl = lambda v: max(0.0, min(1.0, float(v)))
            out[k] = {"free_edge": (cl(it["free_edge"][0]) * W, cl(it["free_edge"][1]) * H),
                      "base": (cl(it["base"][0]) * W, cl(it["base"][1]) * H),
                      "width": cl(it.get("width") or 0) * W}
        print(f"  (luna 팁 축 재기: {len(out)}/{len(crops)}개, {time.time() - t0:.1f}초)")
        return out

    def inspect_set(self, img):
        """세로로 세워 자른 세트 사진 1장 -> luna 가 본 {free_edge_at_top, shape, length, tips[{x, top, bottom, w}],
        reference, note_ko} (좌표는 이 사진 기준 0~1). 실패하면 예외."""
        da = self.a.settings["design_analysis"]
        im = img.convert("RGB").copy(); im.thumbnail((1024, 1024))
        kw = dict(model=da["model"], instructions=SET_INSPECT_INSTRUCTIONS, max_output_tokens=4000,
                  input=[{"role": "user", "content": [
                      {"type": "input_text", "text": "Inspect this nail tip set."},
                      {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(im, 90), "detail": "high"}]
                      + self._ref_sheet_content()}],
                  text={"format": {"type": "json_schema", "name": "set_inspect", "schema": SET_INSPECT_SCHEMA,
                                   "strict": True}})
        t0 = time.time()
        r = self._responses(kw, da["model"], "low")
        data = json.loads(r.output_text)
        try:
            cost = self.cost.text_cost(getattr(r, "usage", None), da["model"])
            cost["seconds"] = round(time.time() - t0, 1)
            self.cost.record_analysis_only(cost, "(세트 확인)", label="(세트 확인)")
        except Exception:
            pass
        return data

    def split_sheet(self, img, effort=None):
        """⓪ 팁 모음 사진 1장 -> 세트 목록 [{tips:[{x,y}], tip_length, complete, summary_ko}], 비용 dict.
        세트 사진이 아니면 빈 목록."""
        import openai
        da, sh = self.a.settings["design_analysis"], self.a.settings["sheet"]
        effort = effort if effort in REASONING else sh["reasoning_effort"]
        im = img.copy(); im.thumbnail((1536, 1536))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
        kw = dict(model=da["model"], instructions=SPLIT_INSTRUCTIONS, max_output_tokens=16000,
                  input=[{"role": "user", "content": [
                      {"type": "input_text", "text": "Find the nail tip sets."},
                      {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                       "detail": "high"}]}],
                  text={"format": {"type": "json_schema", "name": "tip_sets", "schema": SPLIT_SCHEMA, "strict": True}})
        t0 = time.time()
        try:
            r = self._responses(kw, da["model"], effort)
            data = json.loads(r.output_text)
        except openai.AuthenticationError:
            raise UserError(500, "OpenAI API 키가 올바르지 않아요 (.env 확인)", "UPSTREAM_AUTH")
        except openai.RateLimitError as e:
            raise UserError(429, "OpenAI 사용량/요금 한도에 걸렸어요. 잠시 후 다시 시도하세요.", "RATE_LIMITED") from e
        except Exception as e:
            print(f"  ! 세트 나누기 실패 -> 사진 전체를 1개로: {type(e).__name__}: {str(e)[:200]}")
            return [], {"usd": 0.0}
        cost = self.cost.text_cost(getattr(r, "usage", None), da["model"])
        cost["seconds"] = round(time.time() - t0, 1)
        self.cost.record_analysis_only(cost, "(세트 나누기)", label="(세트 나누기)")
        sets = data.get("sets") or [] if data.get("is_tip_sheet") else []
        print(f"  세트 나누기 ({da['model']}, effort {effort or '기본'}): {len(sets)}세트"
              f" (온전한 {sum(1 for x in sets if x.get('complete'))})  {cost['seconds']}초"
              f"  {fmt_cost(cost['usd'], cost['usd'] * self.cost.krw if cost['usd'] is not None and self.cost.krw else None)}")
        return sets, cost

    def analyze_design(self, raw_bytes, design, effort=None, scene=False, tipset=False, tip_images=None):
        """-> (생성에 쓸 디자인 참고 이미지, 인식 결과 dict 또는 None). 같은 사진은 다시 돈 내지 않게 기억해요.
        tip_images: 팁 세트일 때 팁을 하나씩 자른 사진(왼쪽부터, 둘레는 하얗게) -> luna 에 1~5번으로 함께 보여줘요
        effort: 이번 요청의 Reasoning effort (None/"" = settings.yaml design_analysis.reasoning_effort)
        scene: True 면 손 사진 대신 nailhand 장면 사진 중에서 고르고 음료 색 · 배경도 정해요"""
        import hashlib, openai
        cfg = self.a.settings["design_analysis"]
        if not cfg["enabled"]:
            return design, None
        cfg = dict(cfg, reasoning_effort=effort if effort in REASONING else cfg["reasoning_effort"], scene=bool(scene),
                   tipset=bool(tipset))
        # 같은 사진이라도 effort / 고르는 사진 묶음이 다르면 다시 인식해요
        key = hashlib.sha256(raw_bytes).hexdigest() + ":" + (cfg["reasoning_effort"] or "default") + (":scene" if scene else "") + (":tips2" if tipset else "")
        # 같은 사진을 동시에 두 번 분석하지 않게 (업로드 때 미리 분석 중에 버튼을 누른 경우 등)
        with self.analysis_lock:
            ev = self.analysis_pending.get(key)
            mine = ev is None and key not in self.analysis_cache
            if mine:
                self.analysis_pending[key] = threading.Event()
        if ev is not None:
            ev.wait(timeout=120)
        try:
            return self._analyze(key, design, cfg, fresh=mine, tip_images=tip_images)
        finally:
            if mine:
                with self.analysis_lock:
                    self.analysis_pending.pop(key).set()

    def _analyze(self, key, design, cfg, fresh, tip_images=None):
        import openai
        if key in self.analysis_cache:
            an = dict(self.analysis_cache[key], cached_result=True)
        elif not fresh:
            return design, None        # 앞선 분석이 실패했어요 -> 사진 그대로
        else:
            buf = io.BytesIO(); design.save(buf, "JPEG", quality=90)
            url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            content = [{"type": "input_text", "text": "Analyze the nail design in this photo."},
                       {"type": "input_image", "image_url": url, "detail": "high"}]
            instructions = ANALYSIS_INSTRUCTIONS + (TIPSET_INSTRUCTIONS if cfg.get("tipset") else "")
            schema, pool = ANALYSIS_SCHEMA, self.sheet_hands
            if cfg.get("scene"):         # 장면 사진 중에서 고르고 음료 색 · 배경까지 (같은 호출)
                schema, pool = SCENE_ANALYSIS_SCHEMA, self.scene_hands
                if self.scene_url:
                    n = len(pool)
                    cats = "; ".join(dict.fromkeys(
                        f"{s['category']} ({s['keywords']})" if s["keywords"] else s["category"] for s in pool))
                    labels = ", ".join(f"{i + 1}=[{s['category']}] {s['title']}" for i, s in enumerate(pool))
                    instructions += SCENE_CHOICE_INSTRUCTIONS.format(n=n, cats=cats, labels=labels)
                    content += [{"type": "input_text", "text": f"Image 2: the {n} candidate scene photos."},
                                {"type": "input_image", "image_url": self.scene_url, "detail": "high"}]
                else:
                    instructions += (" Set hand_index=0 and hand_reason_ko=''. Still fill scene_drink_en, "
                                     "scene_background_en and scene_ko for a drink photo matching this design.")
            elif self.sheet_url:          # 같은 호출에서 손 사진도 골라요
                n = len(self.sheet_hands)
                labels = ", ".join(f"{i + 1}={h['label']}" for i, h in enumerate(self.sheet_hands))
                instructions += HAND_CHOICE_INSTRUCTIONS.format(n=n, labels=labels)
                content += [{"type": "input_text", "text": f"Image 2: the {n} candidate hands."},
                            {"type": "input_image", "image_url": self.sheet_url, "detail": "low"}]
            else:
                instructions += " Set hand_index=0 and hand_reason_ko=''."
            if cfg.get("tipset") and tip_images:     # 팁을 하나씩 (둘레 하얗게) -> 손가락별로 나눠 인식이 잘되게
                try:
                    ch = tip_chart(tip_images, korean=False, height=320)
                    ch.thumbnail((2048, 2048))
                    cb = io.BytesIO(); ch.save(cb, "JPEG", quality=90)
                    content += [{"type": "input_text", "text": "Final image: the same tips of the sharp row, cut out one "
                                 "by one in left-to-right order (1 = leftmost ... 5 = rightmost)."},
                                {"type": "input_image", "image_url": "data:image/jpeg;base64,"
                                 + base64.b64encode(cb.getvalue()).decode(), "detail": "high"}]
                    instructions += TIP_ONE_BY_ONE_EN
                except Exception as e:
                    print(f"  ! 팁 하나씩 사진을 만들지 못했어요 (세트 사진만 보내요): {type(e).__name__}: {e}")
            kw = dict(model=cfg["model"], instructions=instructions, max_output_tokens=4000,
                      input=[{"role": "user", "content": content}],
                      text={"format": {"type": "json_schema", "name": "nail_design", "schema": schema,
                                       "strict": True}})
            t0 = time.time()
            try:
                r = self._responses(kw, cfg["model"], cfg["reasoning_effort"])
                data = json.loads(r.output_text)
            except openai.AuthenticationError:
                raise UserError(500, "OpenAI API 키가 올바르지 않아요 (.env 확인)", "UPSTREAM_AUTH")
            except Exception as e:
                # 인식이 실패해도 생성은 계속해요 (사진을 그대로 참고 이미지로 사용)
                print(f"  ! 디자인 인식 실패 -> 사진을 그대로 써요: {type(e).__name__}: {str(e)[:200]}")
                return design, None
            an = {**data, **self.cost.text_cost(getattr(r, "usage", None), cfg["model"]),
                  "seconds": round(time.time() - t0, 1), "key": key, "unbilled": True, "tipset": bool(cfg.get("tipset")),
                  "reasoning_effort": cfg["reasoning_effort"] or ""}
            hi = int(data.get("hand_index") or 0)
            an["suggested_hand"] = pool[hi - 1]["id"] if 1 <= hi <= len(pool) else None
            self.cost.record_analysis_only(an, "(디자인 인식)")      # 비용은 인식한 순간 기록
            self.analysis_cache[key] = an
            if len(self.analysis_cache) > 200:
                self.analysis_cache.pop(next(iter(self.analysis_cache)))

        print(f"  디자인 인식 ({an['model']}{', 이전 결과 재사용' if an.get('cached_result') else ''}): "
              f"{an['summary_ko']}  [확신도 {an['confidence']:.2f}]"
              + (f"  -> 추천 손: {an['suggested_hand']} ({an.get('hand_reason_ko', '')})" if an.get("suggested_hand") else "")
              + (f"\n    음료 · 배경: {an['scene_ko']}" if an.get("scene_ko") else ""))
        if not an.get("cached_result"):
            print(f"    인식 토큰: 입력 {an['input']:,} / 출력 {an['output']:,} (추론 {an['reasoning']:,})"
                  f"  비용 {fmt_cost(an['usd'], an['usd'] * self.cost.krw if an['usd'] is not None and self.cost.krw else None)}"
                  f"  {an['seconds']}초")
        if not an["has_nails"] and cfg["reject_if_no_nails"]:
            raise UserError(422, f"사진에서 네일 디자인을 찾지 못했어요. 손톱이 잘 보이는 사진을 올려 주세요. ({an['summary_ko']})", "NO_NAILS_DETECTED")

        ref = design
        if cfg["crop_reference"] and an["has_nails"]:
            b = an["best_region"]
            x0, y0, x1, y1 = (max(0.0, min(1.0, float(b[k]))) for k in ("x0", "y0", "x1", "y1"))
            if x1 - x0 > 0.05 and y1 - y0 > 0.05 and (x1 - x0) * (y1 - y0) < 0.95:
                W, H = design.size
                px, py = (x1 - x0) * 0.14, (y1 - y0) * 0.14          # 여유 (긴 손톱 끝이 잘리지 않게)
                box = (int(max(0, x0 - px) * W), int(max(0, y0 - py) * H),
                       int(min(1, x1 + px) * W), int(min(1, y1 + py) * H))
                if min(box[2] - box[0], box[3] - box[1]) >= 96:
                    ref = design.crop(box)
                    an["crop"] = box
        return ref, an

    def _mask(self, hand_name, hand, ratio=0.0, taper=0.5):
        """-> (손 사진, 손톱 마스크). 긴 손톱이면 마스크를 늘이고, 필요하면 사진 가장자리도 넓혀요."""
        key = (hand_name, hand.size, round(ratio, 3), round(taper, 3))
        if key not in self.masks:
            m = nail_mask_for(hand_name, hand.size).point(lambda v: 255 if v >= 128 else 0)
            self.masks[key] = extend_nails_padded(hand, m, ratio, taper) if ratio > 0 else (None, m)
        h, m = self.masks[key]
        return (h or hand), m

    def flex_size(self):
        """원하는 크기(WxH)로 그릴 수 있는 모델인지 (gpt-image-2, 2.5 계열)."""
        m = self.model
        return not self.no_flex_size and (m == "gpt-image-2" or m.startswith("gpt-image-2-") or m.startswith("gpt-image-2.5"))

    def _image_call(self, kw, on_partial_b64=None):
        """OpenAI images.edit 호출 (손톱 입히기·배너 공통) -> (b64, usage, ms).
        받지 않는 옵션은 빼고 한 번 더 보내고, 오류는 사용자에게 보여 줄 UserError 로 바꿔요."""
        import openai
        st = {"stream": bool(kw.get("stream"))}

        def call():
            r = self.client.images.edit(**kw)
            if not st["stream"]:
                return r.data[0].b64_json, getattr(r, "usage", None)
            final = None
            for ev in r:
                t = getattr(ev, "type", "")
                if t.endswith("partial_image"):
                    if on_partial_b64:
                        on_partial_b64(ev.b64_json, ev.partial_image_index)
                elif t.endswith("completed"):
                    final = ev
            if final is None:
                raise RuntimeError("OpenAI 스트림이 완성 이미지 없이 끝났어요")
            return final.b64_json, getattr(final, "usage", None)

        t0 = time.time()
        with self.slots:
            try:
                try:
                    b64, usage = call()
                except openai.BadRequestError as e:
                    # 이 모델이 받지 않는 옵션은 빼고 한 번 더 (다음부터는 안 보냄)
                    msg = (getattr(e, "message", "") or str(e)).lower()
                    if self.flex_size() and "size" in msg:
                        raise _RetryFixedSize() from e
                    bad = [k for k in ("complexity", "input_fidelity", "output_compression", "output_format",
                                       "partial_images") if k in kw and k in msg]
                    if "partial_images" in bad or ("stream" in msg and st["stream"]):
                        kw.pop("stream", None); kw.pop("partial_images", None); st["stream"] = False
                        self.dropped.update({"stream", "partial_images"}); bad = [b for b in bad if b != "partial_images"]
                    elif not bad:
                        raise
                    for k in bad:
                        kw.pop(k, None); self.dropped.add(k)
                    print(f"  ! {self.model} 가 받지 않는 옵션을 빼고 다시 보내요: {msg[:120]}")
                    t0 = time.time()
                    b64, usage = call()
            except _RetryFixedSize:
                raise
            except openai.AuthenticationError:
                raise UserError(500, "OpenAI API 키가 올바르지 않아요 (.env 확인)", "UPSTREAM_AUTH")
            except openai.RateLimitError as e:
                raise UserError(429, "OpenAI 사용량/요금 한도에 걸렸어요. 잠시 후 다시 시도하거나 결제 한도를 확인하세요.", "RATE_LIMITED") from e
            except openai.BadRequestError as e:
                msg = getattr(e, "message", str(e))
                if "safety" in msg.lower() or "moderation" in msg.lower():
                    raise UserError(400, "OpenAI 안전 필터에 걸린 사진이에요. 다른 디자인 사진으로 시도해 주세요.", "CONTENT_POLICY") from e
                raise UserError(400, f"OpenAI 요청 오류: {msg[:200]}", "UPSTREAM_BAD_REQUEST") from e
            except (openai.APITimeoutError, openai.APIConnectionError) as e:
                raise UserError(504, "OpenAI 서버에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도하세요.", "UPSTREAM_TIMEOUT") from e
        return b64, usage, int((time.time() - t0) * 1000)

    def _image_options(self, kw, stream):
        """모델별 공통 옵션 (input_fidelity / complexity / 출력 형식 / 중간 그림)."""
        R = self.a.settings["request"]
        if self.fidelity == "auto":
            if self.model.startswith("gpt-image-1.5"):
                kw["input_fidelity"] = "high"
        elif self.fidelity in ("high", "low") and not self.model.startswith("gpt-image-2"):
            kw["input_fidelity"] = self.fidelity
        if self.complexity:
            kw["complexity"] = self.complexity
        fmt = str(R["api_output_format"]).lower()
        if fmt in ("webp", "jpeg"):
            kw["output_format"], kw["output_compression"] = fmt, 95
        if stream:
            kw["stream"], kw["partial_images"] = True, max(1, min(3, int(R["partial_images"])))
        for k in self.dropped:
            kw.pop(k, None)
        return kw

    # ------------------------------------------------------------------------------------------
    # 2단계 (free): 마스크 없이 손 사진 + 디자인 사진으로 통째로 다시 그리기
    # ------------------------------------------------------------------------------------------
    def generate_free(self, hand, design, desc="", size=None, hand_name="", analysis=None, on_partial=None,
                      nail_length="auto", nail_shape="auto"):
        try:
            return self._generate_free(hand, design, desc, size, hand_name, analysis, on_partial, nail_length, nail_shape)
        except _RetryFixedSize:
            self.no_flex_size = True
            return self._generate_free(hand, design, desc, size, hand_name, analysis, on_partial, nail_length, nail_shape)

    def _generate_free(self, hand, design, desc, size, hand_name, analysis, on_partial, nail_length, nail_shape):
        import cv2, numpy as np
        from PIL import Image
        R = self.a.settings["request"]
        n_len, n_shape, ratio, _ = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
        src = hand
        if ratio > 0:          # 긴 손톱이 사진 끝에서 잘리지 않게 배경을 넓혀 둬요
            W0, H0 = hand.size
            t, sd = int(H0 * 0.10 * min(ratio, 1.5)), int(W0 * 0.06 * min(ratio, 1.5))
            src = Image.fromarray(cv2.copyMakeBorder(np.asarray(hand), t, 0, sd, sd, cv2.BORDER_REPLICATE))
        W, H = src.size
        if self.flex_size():
            L = int(R["canvas_long_side"]) * 5 // 4          # 손 전체를 그리니 조금 크게 (손톱이 작아지지 않게)
            k = L / max(W, H)
            cw, ch = max(256, round(W * k / 16) * 16), max(256, round(H * k / 16) * 16)
        else:
            cw, ch = canvas_size(W, H)
        canvas = src.resize((cw, ch), Image.LANCZOS)
        prompt = FREE_PROMPT \
            + (f" The nail design recognized in image 2: {analysis['design_en']}"
               + (f" Accent nails: {analysis['accent_nails']}." if analysis.get("accent_nails") else "")
               + (f" Finish: {analysis['finish']}." if analysis.get("finish") else "")
               if analysis and analysis.get("has_nails") else "") \
            + (NAILS_LEN_FREE.format(length=n_len.replace("_", " "), shape=n_shape if n_shape != "unknown" else "",
                                     tip=NAIL_TIP_EN.get(n_shape, "natural")) if ratio > 0
               else " Keep each nail's natural length and shape.") \
            + finger_text(analysis, self.hand_side(hand_name)) \
            + (f" Extra request from the customer (highest priority): {desc.strip()[:300]}." if desc and desc.strip() else "")
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}", quality=QUALITY.get(int(size or 768), "medium"),
                  image=[("hand.png", png_bytes(canvas), "image/png"), ("design.png", png_bytes(design), "image/png")])
        self._image_options(kw, stream)

        def preview(b64, idx):
            try:
                on_partial(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), idx)
            except Exception as e:
                print("  미리보기 실패:", e)

        b64, usage, ms = self._image_call(kw, preview if stream else None)
        out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        if self.a.settings["debug"]["save_images"]:
            save_debug("free_" + hand_name, {"0_design_ref": design, "1_sent_hand": canvas, "5_result": out},
                       text=json.dumps({"prompt": prompt, "analysis": analysis}, ensure_ascii=False, indent=1, default=str))
        if analysis:
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0)
        info = self.cost.record(usage, self.model, kw["quality"], kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                "free:" + hand_name, analysis=analysis)
        info["nail_extend"] = {"length": n_len, "shape": n_shape, "ratio": round(ratio, 3)}
        print(f"  (마스크 없이) {hand_name} | {self.model} | {cw}x{ch} | {ms / 1000:.1f}초 | {fmt_cost(info.get('usd'), info.get('krw'))}")
        return out, ms, info

    # ------------------------------------------------------------------------------------------
    # 2단계 (scene): nailhand 장면 사진의 손톱을 새 디자인으로 + 음료 색 · 배경 바꾸기 (마스크 없음)
    # ------------------------------------------------------------------------------------------
    def generate_scene(self, scene, design, desc="", size=None, scene_id="", analysis=None, on_partial=None,
                       nail_length="auto", nail_shape="auto", tip_images=None, set_image=None):
        args = (scene, design, desc, size, scene_id, analysis, on_partial, nail_length, nail_shape, tip_images, set_image)
        try:
            return self._generate_scene(*args)
        except _RetryFixedSize:
            self.no_flex_size = True
            return self._generate_scene(*args)

    def _with_inset(self, out, worn, guide, n_tips, avoid):
        """결과 사진 아래 빈 곳에 팁을 씌운 참조용 손(worn)을 동그랗게 넣어요."""
        if worn is None or not guide or not self.a.settings["banner"].get("guide_inset", True):
            return out
        try:
            sq = _nail_box(worn, guide["geo"], list(range(1, n_tips + 1)), 0.18, square=True)
            print(f"  (동그란 참조 사진 넣음: {os.path.basename(guide['path'])})")
            return add_guide_inset(out, sq, avoid)
        except Exception as e:
            print(f"  ! 동그란 참조 사진을 넣지 못했어요: {type(e).__name__}: {e}")
            return out

    def _pick_guide(self, side):
        return getattr(self, "guides", {}).get("left" if side == "left" else "right")

    def _worn_guide(self, tips, side, tag="", analysis=None, design=None):
        """참조용 손(왼손 레퍼런스 -> guide_left, 오른손 -> guide_right)에 팁을 엄지~새끼 순서 · 방향대로 씌워요.
        팁을 먼저 손톱 자리에 그대로 붙인 그림을 Image-2 가 저렴한 품질(low)로 자연스럽게 다듬어요 (디자인은 그대로).
        -> (씌운 사진, guide, 요금 usd). 참조용 손이 없으면 (None, None, 0). 모델이 실패하면 붙인 그림 그대로 (요금 0)."""
        from PIL import Image
        guide = self._pick_guide(side)
        if not guide or not tips:
            return None, None, 0.0
        run_note(**{"동그란 사진 레퍼런스": os.path.basename(guide["path"])})
        fingers = list(range(1, len(tips) + 1))
        ggeo = guide["geo"]
        pointed = is_pointed(analysis)
        if pointed:                # 뾰족한 팁: 참조용 손도 luna 가 자리를 고쳐요
            ggeo, _ = self.luna_place(Image.open(guide["path"]), ggeo, fingers, "동그란 사진")
        use_model = bool(self.a.settings["banner"].get("guide_model", True))
        start = guide_paste(guide, tips, fingers, len_range=POINTED_LEN_RANGE if pointed else None, geo=ggeo,
                            soft=(rough_soft(fingers, orig_ratio(self.a.settings)) if design is not None
                                  else thumb_soft(fingers)) if use_model else None)
        # 모델이 실패해서 붙인 그림을 그대로 쓸 때는 엄지도 진하게 다시 붙여요
        hard = lambda: guide_paste(guide, tips, fingers, len_range=POINTED_LEN_RANGE if pointed else None,
                                   geo=ggeo) if (thumb_soft(fingers) or design is not None) and use_model else start
        if not self.a.settings["banner"].get("guide_model", True):
            return start, guide, 0.0
        W0, H0 = start.size
        for attempt in (0, 1):
            if self.flex_size():
                cw = 1024; ch = max(256, round(cw * H0 / W0 / 16) * 16)
            else:
                cw, ch = canvas_size(W0, H0)
            img, padded = pad_to_ratio(start, cw, ch)
            ox, oy = (img.width - W0) // 2, (img.height - H0) // 2
            names = ", ".join(f"image {i + 2} = tip {k} ({FINGERS[k - 1]})" for i, k in enumerate(fingers))
            geo_p = {k: geo_move(g, lambda x, y: ((x * W0 + ox) / img.width, (y * H0 + oy) / img.height))
                     for k, g in ggeo.items()}
            prompt = WORN_GUIDE_PROMPT.format(side="left" if side == "left" else "right", names=names)
            if GUIDE_NATURAL:              # 작게 들어가는 사진: 잔 디테일보다 자연스러움 (디자인은 알아볼 수 있게)
                for s in GUIDE_STRICT_EN:
                    prompt = prompt.replace(s, "")
            prompt += (orient_text([geo_p], fingers, cw, ch, {k: f"image {i + 2}" for i, k in enumerate(fingers)})
                       + ("" if GUIDE_NATURAL else NAIL_ART_KEEP_EN))
            if TIP_KEEP_LENGTH:
                prompt = prompt.replace(OUTLINE_FOLLOW_EN, OUTLINE_KEEP_LEN_EN) + TIP_LENGTH_EN
            prompt += side_view_text([geo_p], fingers, {k: f"image {i + 2}" for i, k in enumerate(fingers)})
            prompt += tip_shape_text(analysis, tips) + pointed_text(analysis, fingers)
            ratio = orig_ratio(self.a.settings)
            orig = original_set_image(design, ratio)   # 올린 팁 세트 원본 -> 원본을 보고 다듬어 그려요
            if orig:
                prompt += original_set_text(len(fingers) + 2, ratio)
            if GUIDE_NATURAL:
                prompt += GUIDE_NATURAL_EN
            q = str(self.a.settings["banner"].get("guide_quality") or "low")     # 동그란 사진 = 빠르게(저렴한 품질)
            kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}", quality=q,
                      image=[("hand.png", png_bytes(img.resize((cw, ch), Image.LANCZOS)), "image/png")]
                      + [(f"tip{k}_{FINGER_KEYS[k - 1]}.png", png_bytes(tip_flat(t)), "image/png")
                         for k, t in zip(fingers, tips)] + ([orig] if orig else []))
            self._image_options(kw, False)
            try:
                b64, usage, ms = self._image_call(kw)
            except _RetryFixedSize:
                self.no_flex_size = True
                continue
            except Exception as e:
                print(f"  ! 참조용 손에 팁 씌우기 실패 - 붙인 그림을 그대로 써요: {type(e).__name__}: {e}")
                return hard(), guide, 0.0
            out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB").resize(img.size, Image.LANCZOS)
            out = out.crop((ox, oy, ox + W0, oy + H0))
            info = self.cost.record(usage, self.model, q, kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                    "guide:" + tag)
            usd = float(info.get("usd") or 0.0)
            print(f"  (참조용 손에 팁 씌움) {os.path.basename(guide['path'])} | {self.model} {q} | {cw}x{ch} | "
                  f"{ms / 1000:.1f}초 | {fmt_cost(info.get('usd'), info.get('krw'))}")
            if self.a.settings["debug"]["save_images"]:
                save_debug("guide_" + (tag or "hand"), {"0_paste_start": start, "5_worn": out}, text=prompt)
            return out, guide, usd
        return hard(), guide, 0.0

    def _bg_then_paste(self, scene, scene_id, tips, tip_fingers, hands, analysis, tips_all, desc, size, on_partial,
                       design=None):
        """① 프로그램이 잘라 세운 팁을 nailhand JSON(손톱 끝/뿌리) 자리에 대략 얹어요 (어느 손가락에 어떤 팁 · 위아래 안내용)
        ② Image-2 가 그 사진 + 팁 하나씩 사진을 보고, 레퍼런스의 원래 손톱 모양 · 곡률 · 빛에 맞게 자연스럽게 녹여 다시 그려요
           (같은 호출에서 배경 · 음료도 바꿔요). 결과 = 모델이 다듬은 사진 그대로 (팁을 다시 붙이지 않아요)
        ③ 동그란 사진은 따로 만들어 마지막에 넣기만 해요"""
        from PIL import Image
        R = self.a.settings["request"]
        an = analysis or {}
        cat = next((s["category"] for s in self.scene_hands if s["id"] == scene_id), scene_id.split("/")[0])
        obj, drink_word = scene_object(cat)
        design_hint = (an.get("design_en") or "").strip()[:400]     # luna 가 인식한 팁 디자인 (음료 · 배경 고르는 데만)
        drink = (an.get("scene_drink_en") or "").strip() or (
            f"a color that harmonizes with this nail design: {design_hint}" if design_hint
            else "a color that harmonizes with the nail colors")
        bg = (an.get("scene_background_en") or "").strip() or (
            f"a new softly blurred background whose colors and mood match this nail design: {design_hint}" if design_hint
            else "a new softly blurred background")
        drink_part = (f"(1) DRINK: change the color of the {drink_word} to: {drink.rstrip('.')}. Keep the same glass/cup/"
                      "bottle shape, liquid level, garnish position, bubbles, foam, reflections and refraction - only "
                      "its color and look change. "
                      if self.a.settings["banner"].get("tip_change_drink", True) else
                      f"(1) DRINK: keep the {drink_word} exactly as it is. ").replace("(1) DRINK", "(2) DRINK", 1)
        names = ", ".join(f"image {i + 2} = the {FINGERS[k - 1]}'s tip" for i, k in enumerate(tip_fingers))
        prompt = SCENE_REFINE_PROMPT.format(obj=obj, names=names, drink_part=drink_part, bg=bg.rstrip(".")) \
            + (f" Extra request from the customer (highest priority, but never about the nail designs): "
               f"{desc.strip()[:300]}." if desc and desc.strip() else "")
        rw, rh = OUTPUT_RATIO
        if self.flex_size():
            L = int(R["canvas_long_side"]) * 5 // 4
            cw, ch = max(256, round(L * rw / rh / 16) * 16), max(256, round(L / 16) * 16)
        else:
            cw, ch = canvas_size(rw, rh)
        W0, H0 = scene.size
        sc, padded = pad_to_ratio(scene.convert("RGB"), cw, ch)
        if padded:
            prompt += (" Image 1 was widened to a new frame by stretching its edge pixels; the new background must "
                       "fill the WHOLE frame naturally, with no stretched streaks, borders or bars.")
        W1, H1 = sc.size
        ox, oy = (W1 - W0) // 2, (H1 - H0) // 2
        mv = lambda x, y: ((x * W0 + ox) / W1, (y * H0 + oy) / H1)
        canvas = sc.resize((cw, ch), Image.LANCZOS)
        rough = canvas
        keep, geo_all = [], []
        pointed = is_pointed(analysis)
        for h in hands:            # 팁을 손톱 자리에 대략 얹기 (순서 · 방향 안내용 - 모델이 자연스럽게 다시 그려요)
            geo_c = {k: geo_move(g, mv, W0 / W1) for k, g in (h.get("geo") or {}).items()}
            if pointed:            # 뾰족한 팁: luna 가 큐티클 · 손가락 방향을 다시 봐서 자리를 고쳐요
                geo_c, _ = self.luna_place(canvas, geo_c, tip_fingers, "장면")
            rough = paste_tips(rough, geo_c, tips, tip_fingers, light_src=canvas,
                               len_range=POINTED_LEN_RANGE if pointed else None,
                               soft=scene_soft(tip_fingers, self.a.settings, with_original=design is not None))
            keep += [p for k, g in geo_c.items() if k in tip_fingers for p in geo_points(g)]
            geo_all.append(geo_c)
        prompt += orient_text(geo_all, tip_fingers, cw, ch,
                              {k: f"image {i + 2}" for i, k in enumerate(tip_fingers)})
        if TIP_KEEP_LENGTH:
            prompt = prompt.replace(OUTLINE_FOLLOW_EN, OUTLINE_KEEP_LEN_EN) + TIP_LENGTH_EN
        prompt += side_view_text(geo_all, tip_fingers, {k: f"image {i + 2}" for i, k in enumerate(tip_fingers)})
        prompt += tip_shape_text(analysis, tips_all or tips) + pointed_text(analysis, tip_fingers)
        nat = natural_level(self.a.settings)       # 자연스러움 레벨 (settings.yaml banner.natural_level)
        prompt = scene_natural_prompt(prompt, nat)
        print(f"  (자연스러움 레벨 {nat * 100:.0f}: " + ("정확히 복사" if nat < 0.34 else "균형" if nat < 0.67 else "자연스러움 최우선") + ")")
        run_note(**{"팁 끝 모양(luna)": (analysis or {}).get("nail_shape") or "unknown"})
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        ratio = orig_ratio(self.a.settings)
        orig = original_set_image(design, ratio)   # 올린 팁 세트 원본 -> 모델이 원본을 보고 손톱을 다듬어 그려요
        if orig:
            prompt += original_set_text(len(tip_fingers) + 2, ratio)
            print(f"  (팁 세트 원본 사진도 보내요 · 원본 {ratio * 100:.0f}% : 잘라낸 팁 {100 - ratio * 100:.0f}%)")
        kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}", quality=QUALITY.get(int(size or 768), "medium"),
                  image=[("scene_rough_tips.png", png_bytes(rough), "image/png")]
                  + [(f"tip{k}_{FINGER_KEYS[k - 1]}.png", png_bytes(tip_flat(t)), "image/png")
                     for k, t in zip(tip_fingers, tips)] + ([orig] if orig else []))
        self._image_options(kw, stream)

        def preview(b64, idx):
            try:
                on_partial(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), idx)
            except Exception as e:
                print("  미리보기 실패:", e)

        print(f"  (팁을 손톱 자리에 대략 얹고 -> Image-2 가 레퍼런스 손톱에 자연스럽게 녹이며 배경 · 음료도 바꿔요)")
        b64, usage, ms = self._image_call(kw, preview if stream else None)
        refined = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        if refined.size != (cw, ch):
            refined = refined.resize((cw, ch), Image.LANCZOS)
        out = crop_to_ratio(refined)
        tag = os.path.splitext(os.path.basename(scene_id))[0]
        worn, guide, g_usd = (None, None, 0.0)
        if tips_all and self.a.settings["banner"].get("guide_inset", True):      # 동그란 사진: 따로 만들어 넣기만
            worn, guide, g_usd = self._worn_guide(tips_all, self.hand_side(scene_id), tag, analysis=analysis,
                                                  design=design)
            out = self._with_inset(out, worn, guide, len(tips_all), keep)
        if self.a.settings["debug"]["save_images"]:
            save_debug("scene_" + tag, {"1_sent_scene": canvas, "2_rough_tips": rough, "3_refined": refined,
                                        "5_result": out,
                                        **({"1_inset_source": worn} if worn is not None else {}),
                                        **{f"0_tip{f}": tp for f, tp in zip(tip_fingers, tips)}},
                       text=json.dumps({"mode": "paste_then_refine", "prompt": prompt, "fingers": tip_fingers,
                                        "analysis": analysis}, ensure_ascii=False, indent=1, default=str))
        if analysis:
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0)
        info = self.cost.record(usage, self.model, kw["quality"], kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                "scene:" + tag, analysis=analysis)
        if g_usd:
            info["usd"] = float(info.get("usd") or 0.0) + g_usd
            info["krw"] = info["usd"] * self.cost.krw if self.cost.krw else None
        info["nail_extend"] = {"length": "tip", "shape": "tip", "ratio": 0.0}
        print(f"  (장면 사진 · 팁 얹고 모델이 자연스럽게 다듬기 + 배경/음료) {scene_id} | 손가락 {tip_fingers} | {self.model} | "
              f"{cw}x{ch} | {ms / 1000:.1f}초 | {fmt_cost(info.get('usd'), info.get('krw'))}")
        return out, ms, info

    def _paste_scene(self, scene, scene_id, tips, tip_fingers, hands, analysis, all_tips=None):
        """팁 직접 붙이기 (이미지 모델 없음, 요금 없음): 레퍼런스 사진의 손톱 자리마다 그 손가락의 팁 사진을 그대로 붙여요.
        디자인 · 순서는 그대로 (1번 팁 = 엄지 ... 5번 = 새끼). 음료 색 · 배경은 레퍼런스 사진 그대로예요."""
        t0 = time.time()
        g_usd = 0.0
        out = scene.convert("RGB")
        keep = []
        for h in hands:
            out = paste_tips(out, h["geo"], tips, tip_fingers)
            keep += [p for k, g in h["geo"].items() if k in tip_fingers for p in geo_points(g)]
        out = frame_ratio(out, keep)
        if all_tips:       # 손톱 자리를 같은 방식으로 잘라/넓혀서 동그란 예시가 손톱을 가리지 않게
            import numpy as np
            from PIL import Image as _I, ImageDraw as _D
            mk = _I.new("RGB", scene.size, 0)
            dr = _D.Draw(mk)
            for x, y in keep:
                r = 0.02 * scene.width
                dr.ellipse((x * scene.width - r, y * scene.height - r, x * scene.width + r, y * scene.height + r),
                           fill=(255, 255, 255))
            a = np.asarray(frame_ratio(mk, keep).convert("L")) > 128
            ys, xs = np.nonzero(a)
            avoid = [(x / a.shape[1], y / a.shape[0]) for x, y in zip(xs[::40], ys[::40])]
            worn, guide, g_usd = self._worn_guide(all_tips, self.hand_side(scene_id),     # 동그란 사진도 모델이 자연스럽게 다듬어요
                                                  os.path.splitext(os.path.basename(scene_id))[0], analysis=analysis)
            out = self._with_inset(out, worn, guide, len(all_tips), avoid)
        ms = int((time.time() - t0) * 1000)
        tag = os.path.splitext(os.path.basename(scene_id))[0]
        if self.a.settings["debug"]["save_images"]:
            save_debug("scene_" + tag, {"1_sent_scene": scene, "5_result": out,
                                        **{f"0_tip{f}": t for f, t in zip(tip_fingers, tips)}},
                       text=json.dumps({"mode": "tip_paste", "fingers": tip_fingers, "analysis": analysis},
                                       ensure_ascii=False, indent=1, default=str))
        if analysis:
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0)
        info = self.cost.record(None, "tip-paste", "-", None, f"{out.width}x{out.height}", ms / 1000,
                                "scene:" + tag, analysis=analysis)
        info["usd"] = float((analysis or {}).get("billed_usd") or 0.0) + g_usd       # 동그란 사진 모델 요금 + 이미지 생성 0원 + (첫 사용이면) 인식 비용
        info["krw"] = info["usd"] * self.cost.krw if self.cost.krw else None
        info["nail_extend"] = {"length": "tip", "shape": "tip", "ratio": 0.0}
        print(f"  (장면 사진 · 팁 직접 붙이기) {scene_id} | 손가락 {tip_fingers} | {out.width}x{out.height} | {ms / 1000:.1f}초 | 이미지 생성 없음")
        return out, ms, info

    def _generate_scene(self, scene, design, desc, size, scene_id, analysis, on_partial, nail_length, nail_shape,
                        tip_images=None, set_image=None):
        orig_set = set_image if set_image is not None else design     # 팁 세트 원본 (잘리지 않은 세트 사진 전체)
        from PIL import Image
        R = self.a.settings["request"]
        an = analysis or {}
        cat = next((s["category"] for s in self.scene_hands if s["id"] == scene_id), scene_id.split("/")[0])
        obj, drink_word = scene_object(cat)
        n_len, n_shape, _, _ = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
        own_shape = bool(tip_images) and nail_length in ("auto", "", None) and nail_shape in ("auto", "", None)
        if own_shape:      # 팁 세트 + 길이 · 모양 자동: 손톱마다 자기 팁 사진의 모양 · 길이 그대로 (디자인을 안 건드려요)
            n_len, n_shape = "tip", "tip"
            nails = OWN_SHAPE_TIP_EN
        elif n_len in ("keep", "unknown") and n_shape in ("unknown",):
            nails = "Keep each nail's current length and shape."
        else:
            nails = (f"Give every nail a {n_len.replace('_', ' ') if n_len not in ('keep', 'unknown') else 'natural'} "
                     f"length and {n_shape if n_shape != 'unknown' else 'natural'} shape with a "
                     f"{NAIL_TIP_EN.get(n_shape, 'natural')} free edge, and show the WHOLE design along the full nail "
                     f"length; nails stay fully inside the picture.")
        hand0 = self.hand_side(scene_id)
        # 잘라낸 팁 사진(세트의 왼쪽 -> 오른쪽) -> 손가락 순서: 오른손 · 왼손 모두 1번 = 엄지 ... 5번 = 새끼 (뒤집지 않아요)
        tips_all = tips_by_finger(tip_images, hand0)      # 동그란 예시 · 참조용 손에도 같은 순서
        tips = list(tips_all)                              # tips[k] = (k+1)번 손가락 (1=엄지 ... 5=새끼)
        if tips:
            print(f"  팁 배정 ({'왼손: 1번 엄지 · 2번 검지 · 3번 중지 · 4번 약지 · 5번 새끼 (손에서는 5 새끼 · 4 약지 · 3 중지 · 2 검지 · 1 엄지 순으로 놓임)' if hand0 == 'left' else '오른손: 세트 왼쪽부터 엄지 · 검지 · 중지 · 약지 · 새끼'})")
        tip_fingers = list(range(1, len(tips) + 1))
        hands = self.ref_hands(scene_id) if tips else None     # 레퍼런스에서 인식한 손·손가락
        hands_c = None
        if hands:          # 레퍼런스에 보이는 손가락의 팁만 보내요 (안 보이는 손가락의 팁은 안 씀)
            vis = set().union(*(h["nails"] for h in hands))
            keep = [k for k in tip_fingers if k in vis]
            if keep:
                tips, tip_fingers = [tips[k - 1] for k in keep], keep
            ko = ("엄지", "검지", "중지", "약지", "새끼")
            print(f"  {scene_id}: " + " / ".join(
                f"{'왼손' if h['side'] == 'left' else '오른손'} " + " ".join(ko[k - 1] for k in sorted(h["nails"]))
                for h in hands) + f" -> 팁 {', '.join(ko[k - 1] for k in tip_fingers)}")
            W0, H0 = scene.size
            # 팁 직접 붙이기: 보이는 손톱마다 nail_positions.json 에 손톱 끝 · 뿌리가 있으면 이미지 모델 없이 팁 사진을 그대로 붙여요
            geo_ok = all(set(h["nails"]) <= set(h.get("geo") or {}) for h in hands)
            if self.a.settings["banner"].get("tip_paste", True):
                if geo_ok:
                    return self._paste_scene(scene, scene_id, tips, tip_fingers, hands, analysis, all_tips=tips_all)
                print("  (nail_positions.json 에 손톱 끝 · 뿌리가 없는 손가락이 있어서 이번엔 이미지 모델이 그려요)")
            elif self.a.settings["banner"].get("tip_paste_after", True):
                if geo_ok:
                    return self._bg_then_paste(scene, scene_id, tips, tip_fingers, hands, analysis, tips_all,
                                               desc, size, on_partial, design=orig_set)
                print("  (nail_positions.json 에 손톱 끝 · 뿌리가 없는 손가락이 있어서 이번엔 이미지 모델이 손톱도 그려요)")
        n_tips = len(tips)
        if tips:            # 세트: 손가락별 지시는 사진 크기를 정한 뒤에 넣어요 (자리표시자)
            nails += " {tip_fingers}"
        else:
            if an.get("has_nails"):
                nails += f" The nail design in image 2: {an['design_en']}" \
                    + (f" Accent nails: {an['accent_nails']}." if an.get("accent_nails") else "") \
                    + (f" Finish: {an['finish']}." if an.get("finish") else "")
            nails += finger_text(an, self.hand_side(scene_id))
        design_hint = (an.get("design_en") or "").strip()[:400]     # luna 가 인식한 팁 디자인 (음료 · 배경 고르는 데만 써요)
        drink = (an.get("scene_drink_en") or "").strip() or (
            f"a color that harmonizes with this nail design: {design_hint}" if design_hint
            else "a color that harmonizes with the new nail colors")
        bg = (an.get("scene_background_en") or "").strip() or (
            f"a new softly blurred background whose colors and mood match this nail design: {design_hint}" if design_hint
            else "a new softly blurred background whose colors and mood match the new nail design")
        tmpl = SCENE_PROMPT
        if tips and not self.a.settings["banner"].get("tip_change_drink", True):
            tmpl = re.sub(r"\(2\) DRINK:.*?only its color and look change\. ", lambda m: SCENE_DRINK_KEEP, tmpl,
                          flags=re.S)
        prompt = tmpl.format(obj=obj, drink_word=drink_word, drink=drink.rstrip("."), bg=bg.rstrip("."),
                             nails=nails) \
            + (f" Extra request from the customer (highest priority): {desc.strip()[:300]}." if desc and desc.strip() else "")
        # 결과는 항상 4:5 -> 장면 사진을 자르지 않고 가장자리를 넓혀 4:5(정해진 크기 모델은 그 크기 비율)로 맞춰 보내요
        rw, rh = OUTPUT_RATIO
        if self.flex_size():
            L = int(R["canvas_long_side"]) * 5 // 4          # 장면 전체를 그리니 조금 크게 (손톱이 작아지지 않게)
            cw, ch = max(256, round(L * rw / rh / 16) * 16), max(256, round(L / 16) * 16)
        else:
            cw, ch = canvas_size(rw, rh)
        scene, padded = pad_to_ratio(scene, cw, ch)
        if padded:
            prompt += (" Image 1 was widened to a new frame by stretching its edge pixels; the new background must "
                       "fill the WHOLE frame naturally, with no stretched streaks, borders or bars.")
        if hands:          # 넓힌 만큼 손톱 위치도 옮겨요
            W1, H1 = scene.size
            ox, oy = (W1 - W0) // 2, (H1 - H0) // 2
            mv = lambda x, y: ((x * W0 + ox) / W1, (y * H0 + oy) / H1)
            hands_c = [{"side": h["side"], "nails": {k: mv(x, y) for k, (x, y) in h["nails"].items()},
                        "geo": {k: geo_move(g, mv) for k, g in (h.get("geo") or {}).items()}} for h in hands]
        canvas = scene.resize((cw, ch), Image.LANCZOS)
        fmap = finger_map(canvas, hands_c, set(tip_fingers)) if hands_c else None
        gimg, worn, guide, g_usd = None, None, None, 0.0     # 참조용 손(reference)에 팁을 먼저 씌운 사진
        bn = self.a.settings["banner"]
        if tips_all and (bn.get("tip_guide", True) or bn.get("guide_inset", True)):
            worn, guide, g_usd = self._worn_guide(tips_all, self.hand_side(scene_id),
                                                  os.path.splitext(os.path.basename(scene_id))[0], analysis=analysis,
                                                  design=orig_set)
            if worn is not None and bn.get("tip_guide", True):
                gimg = _nail_box(worn, guide["geo"], list(range(1, len(tips_all) + 1)), 0.12)
                gimg.thumbnail((1536, 1536), Image.LANCZOS)
        worn_mode = gimg is not None      # 팁 사진은 보내지 않고, 팁을 씌운 참조용 손만 보고 레퍼런스 손톱에 씌워요
        if worn_mode:
            prompt = prompt.replace(OWN_SHAPE_TIP_EN, OWN_SHAPE_WORN_EN)
            prompt = prompt.replace("{tip_fingers}", worn_text(tip_fingers, guide["geo"], hands_c,
                                                               3 if fmap is not None else None))
            print(f"  (팁 사진 대신 팁 씌운 참조용 손을 보고 씌워요: {os.path.basename(guide['path'])})")
            ref = gimg
            kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}",
                      quality=QUALITY.get(int(size or 768), "medium"),
                      image=[("scene.png", png_bytes(canvas), "image/png"),
                             ("worn_reference.png", png_bytes(gimg), "image/png")]
                      + ([("finger_map.png", png_bytes(fmap), "image/png")] if fmap is not None else []))
        else:
            if tips:            # 세트: 팁 사진이 정답이에요. 틀릴 수 있는 전체 요약 글은 보내지 않아요
                prompt = prompt.replace("{tip_fingers}", finger_text(
                    an, self.hand_side(scene_id), first_tip_image=3, tip_fingers=tip_fingers, ref_hands=hands_c,
                    map_image=(3 + n_tips) if fmap is not None else None, mirror=(hand0 == "left")))
            ref = design.copy(); ref.thumbnail((1024, 1024), Image.LANCZOS)
            if tips:          # 세트: 2번 이미지 = 손가락 배정표, 3번~ = 팁 하나씩, 마지막 = 손가락 지도
                ref = tip_chart(tips, fingers=tip_fingers, mirror=(hand0 == "left"))      # 왼손 = 새끼 ... 엄지
                ref.thumbnail((1536, 1536), Image.LANCZOS)
            kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}",
                      quality=QUALITY.get(int(size or 768), "medium"),
                      image=[("scene.png", png_bytes(canvas), "image/png"),
                             ("finger_chart.png" if tips else "design.png", png_bytes(ref), "image/png")]
                      + [(f"tip{f}_{FINGER_KEYS[f - 1]}.png", png_bytes(tip_flat(t)), "image/png")
                         for f, t in zip(tip_fingers, tips)]
                      + ([("finger_map.png", png_bytes(fmap), "image/png")] if fmap is not None else []))
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        self._image_options(kw, stream)

        def preview(b64, idx):
            try:
                on_partial(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), idx)
            except Exception as e:
                print("  미리보기 실패:", e)

        b64, usage, ms = self._image_call(kw, preview if stream else None)
        out = crop_to_ratio(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))
        if tips_all:
            av = [p for h in (hands_c or []) for p in h["nails"].values()]
            out = self._with_inset(out, worn, guide, len(tips_all), av)
        tag = os.path.splitext(os.path.basename(scene_id))[0]
        if self.a.settings["debug"]["save_images"]:
            save_debug("scene_" + tag, {"0_design_ref": ref, "1_sent_scene": canvas, "5_result": out,
                                        **({"1_finger_map": fmap} if fmap is not None else {}),
                                        **({"1_worn_reference": worn} if worn is not None else {}),
                                        **({} if worn_mode else {f"0_tip{f}": t for f, t in zip(tip_fingers, tips)})},
                       text=json.dumps({"prompt": prompt, "analysis": analysis}, ensure_ascii=False, indent=1, default=str))
        if analysis:
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0)
        info = self.cost.record(usage, self.model, kw["quality"], kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                "scene:" + tag, analysis=analysis)
        if g_usd:          # 참조용 손에 팁 씌운 요금도 합쳐요
            info["usd"] = float(info.get("usd") or 0.0) + g_usd
            info["krw"] = info["usd"] * self.cost.krw if self.cost.krw else None
        info["nail_extend"] = {"length": n_len, "shape": n_shape, "ratio": 0.0}
        print(f"  (장면 사진) {scene_id} | {self.model} | {cw}x{ch} | {ms / 1000:.1f}초 | {fmt_cost(info.get('usd'), info.get('krw'))}")
        return out, ms, info

    # ------------------------------------------------------------------------------------------
    # 3단계: 네일 입힌 손 사진을 보고 배너 디자인 계획서 만들기 (gpt-6-luna 등 비전 모델)
    # ------------------------------------------------------------------------------------------
    def plan_banner(self, nail_img, analysis=None, brand="", desc="", effort=None, pose="auto", bare_design=None,
                    scene=False):
        """scene: True 면 image 1 은 음료·소품을 든 장면 사진 (그 소품과 반지는 배너에도 그대로, 자세 유지)
        -> 계획서 dict (PLAN_SCHEMA + model, usd). 실패하면 UserError.
        bare_design: direct 방식일 때 디자인 사진 (nail_img 는 맨손)
        pose: auto = 디자인에 맞는 자세를 정해요(바꿀지도 판단), keep = 원래 자세 유지
        effort: 이번 요청의 Reasoning effort (None/"" = settings.yaml banner.plan_reasoning_effort)"""
        import openai
        B, da = self.a.settings["banner"], self.a.settings["design_analysis"]
        plan_model = B["plan_model"] or da["model"]
        effort = effort if effort in REASONING else B["plan_reasoning_effort"]
        w, h = parse_size(B["size"])
        brand = (brand or B["brand"] or "").strip()[:30]
        brand_rule = (f"The salon/brand name is '{brand}': show it once, small, as a logo-like wordmark."
                      if brand else "Do not invent any brand or salon name.")
        text_rule = ("The banner shows the headline, subcopy and a CTA button as text." if B["text_on_image"] else
                     "The banner must contain NO text at all; still write the copy fields, but in image_prompt_en "
                     "only reserve clean empty space where copy would be added later.")
        pose = "keep" if scene else (pose if pose in ("auto", "keep") else B["pose"])
        instructions = PLAN_INSTRUCTIONS.format(w=w, h=h, orient=orientation(w, h), brand_rule=brand_rule,
                                                text_rule=text_rule,
                                                pose_rule=POSE_RULE_AUTO if pose == "auto" else POSE_RULE_KEEP)
        im = nail_img.copy(); im.thumbnail((1024, 1024))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        info = "Nail design (from analysis): " + (analysis.get("design_en", "") if analysis else "see image") \
            + (f" Accent nails: {analysis['accent_nails']}." if analysis and analysis.get("accent_nails") else "") \
            + (f" Finish: {analysis['finish']}." if analysis and analysis.get("finish") else "") \
            + finger_text(analysis, "right") \
            + (f" Customer request (highest priority): {desc.strip()[:300]}" if desc and desc.strip() else "")
        content = [{"type": "input_text", "text": info}, {"type": "input_image", "image_url": url, "detail": "high"}]
        if scene:
            instructions += (" NOTE: image 1 is a lifestyle photo - the hand holds a drink or an object (glass, cup, "
                             "perfume bottle) and may wear rings. Keep that object, its color and the jewelry in the "
                             "banner as part of the hero; you may extend or restyle the background around it.")
        if bare_design is not None:     # direct: 아직 네일을 안 입힌 맨손 + 입힐 디자인
            instructions += (" NOTE: image 1 is the model hand BEFORE the nails are painted; image 2 is the nail design "
                             "that the image model will paint on it in the same step. Plan for the finished nails.")
            d2 = bare_design.copy(); d2.thumbnail((768, 768))
            b2 = io.BytesIO(); d2.save(b2, "JPEG", quality=88)
            content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(b2.getvalue()).decode(),
                            "detail": "high"})
        kw = dict(model=plan_model, instructions=instructions, max_output_tokens=4000,
                  input=[{"role": "user", "content": content}],
                  text={"format": {"type": "json_schema", "name": "banner_plan", "schema": PLAN_SCHEMA, "strict": True}})
        t0 = time.time()
        try:
            r = self._responses(kw, plan_model, effort)
            plan = json.loads(r.output_text)
        except openai.AuthenticationError:
            raise UserError(500, "OpenAI API 키가 올바르지 않아요 (.env 확인)", "UPSTREAM_AUTH")
        except openai.RateLimitError as e:
            raise UserError(429, "OpenAI 사용량/요금 한도에 걸렸어요. 잠시 후 다시 시도하세요.", "RATE_LIMITED") from e
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            raise UserError(504, "OpenAI 서버에 연결하지 못했어요. 다시 시도하세요.", "UPSTREAM_TIMEOUT") from e
        except Exception as e:
            print(f"  ! 배너 계획서 실패: {type(e).__name__}: {str(e)[:200]}")
            raise UserError(400, "배너 계획서를 만들지 못했어요. 다시 시도해 주세요.", "UPSTREAM_BAD_REQUEST") from e
        cost = self.cost.text_cost(getattr(r, "usage", None), plan_model)
        cost["seconds"] = round(time.time() - t0, 1)
        self.cost.record_analysis_only(cost, "(배너 계획서)", label="(배너 계획서)")
        plan["palette"] = [c for c in plan.get("palette", []) if isinstance(c, dict)][:5]
        plan.update(model=plan_model, usd=cost["usd"], brand=brand, reasoning_effort=effort or "")
        if pose == "keep" or not str(plan.get("pose_en") or "").strip():
            plan["pose_change"] = False
        if pose == "keep":
            plan.update(pose_en="", pose_ko="원래 자세 유지")
        print(f"  배너 계획서 ({plan_model}, effort {effort or '기본'}): {plan.get('concept_ko', '')} | \"{plan.get('headline_ko', '')}\""
              f"  {cost['seconds']}초  {fmt_cost(cost['usd'], cost['usd'] * self.cost.krw if cost['usd'] is not None and self.cost.krw else None)}")
        print(f"    자세: {'바꿈 -> ' if plan['pose_change'] else '유지 · '}{plan.get('pose_ko', '')}")
        return plan

    # ------------------------------------------------------------------------------------------
    # 4단계: 계획서대로 손 사진으로 배너 만들기 (이미지 모델)
    # ------------------------------------------------------------------------------------------
    def make_banner(self, nail_img, plan, size=None, hand_name="", on_partial=None, design_ref=None,
                    apply_design=False, analysis=None, nail_length="auto", nail_shape="auto", scene=False):
        """design_ref: 원본 네일 디자인(잘라낸 참고 사진). 자세를 바꿀 때 2번째 이미지로 보내 디자인을 지켜요.
        apply_design=True (direct): nail_img 는 맨손 -> 이 단계에서 디자인을 입히면서 배너까지 한 번에"""
        args = (nail_img, plan, size, hand_name, on_partial, design_ref, apply_design, analysis, nail_length, nail_shape,
                scene)
        try:
            return self._make_banner(*args)
        except _RetryFixedSize:
            print(f"  ! {self.model} 가 원하는 크기를 받지 않아서 정해진 크기로 다시 보내요")
            self.no_flex_size = True
            return self._make_banner(*args)

    def _make_banner(self, nail_img, plan, size, hand_name, on_partial, design_ref=None, apply_design=False,
                     analysis=None, nail_length="auto", nail_shape="auto", scene=False):
        from PIL import Image
        B, R = self.a.settings["banner"], self.a.settings["request"]
        w, h = parse_size(B["size"])
        if self.flex_size():
            w, h = max(256, round(w / 16) * 16), max(256, round(h / 16) * 16)
        else:
            w, h = canvas_size(w, h)
        src = nail_img.copy(); src.thumbnail((1536, 1536), Image.LANCZOS)
        buf = io.BytesIO(); src.save(buf, "JPEG", quality=95)
        pal = ", ".join(f"{c.get('name', '')} {c.get('hex', '')}".strip() for c in plan.get("palette", []))
        repose = bool(plan.get("pose_change") and plan.get("pose_en")) and not scene
        if apply_design:    # direct: 맨손 + 디자인 -> 네일 입히기와 배너를 한 번에
            n_len, n_shape, ratio, _ = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
            head = BANNER_PROMPT.split("Keep the hand")[0].format(w=w, h=h, orient=orientation(w, h)) + DIRECT_NAILS \
                + (f"Recognized design: {analysis['design_en']} "
                   + (f"Accent nails: {analysis['accent_nails']}. " if analysis.get("accent_nails") else "")
                   + (f"Finish: {analysis['finish']}. " if analysis.get("finish") else "")
                   if analysis and analysis.get("has_nails") else "") \
                + (finger_text(analysis, self.hand_side(hand_name)).strip() + " ") \
                + (NAILS_LEN_FREE.format(length=n_len.replace("_", " "), shape=n_shape if n_shape != "unknown" else "",
                                         tip=NAIL_TIP_EN.get(n_shape, "natural")).strip() + " " if ratio > 0 else "") \
                + (f"Pose the hand like this: {str(plan['pose_en']).strip().rstrip('.')}. " if repose else
                   "Keep the hand's pose, finger shape and skin tone as in image 1; you may cut it out and move, scale "
                   "or slightly rotate it to fit the composition. ") \
                + "All five nails clearly visible. Photo-real, natural shadows matching the new scene. No extra hands " \
                  "or fingers, no rings or jewelry, no watermark, no logos." + NAIL_ART_KEEP_EN + " "
        elif repose:      # 자세 바꾸기: '조금만 옮기기' 문장 대신 새 자세 + 손톱 하나하나 그대로 옮기기
            head = BANNER_PROMPT.split("Keep the hand")[0].format(w=w, h=h, orient=orientation(w, h)) \
                + BANNER_REPOSE.replace("{pose}", str(plan["pose_en"]).strip().rstrip(".")) \
                + "Photo-real, with natural shadows that match the new scene. No extra hands or fingers, no rings or " \
                  "jewelry, no watermark, no logos." + NAIL_ART_KEEP_EN + " "
        else:
            head = BANNER_PROMPT.format(w=w, h=h, orient=orientation(w, h))
        if scene:          # 장면 사진: 들고 있는 음료·소품과 반지도 그대로
            head = head.replace("No extra hands or fingers, no rings or jewelry,", "No extra hands or fingers,") \
                + "Keep the drink or object held by the hand (its vessel shape and its color) and the rings exactly " \
                  "as in image 1, together with the hand. "
        prompt = head \
            + f"Art direction: {plan.get('image_prompt_en', '')} " \
            + (f"Color palette: {pal}. " if pal else "") \
            + f"Background: {plan.get('background_ko', '')} Layout: {plan.get('layout_ko', '')} "
        if B["text_on_image"]:
            texts = [f'headline "{plan.get("headline_ko", "")}"', f'subcopy "{plan.get("subcopy_ko", "")}"',
                     f'button "{plan.get("cta_ko", "")}"'] + ([f'brand wordmark "{plan["brand"]}"'] if plan.get("brand") else [])
            prompt += ("Render exactly these Korean texts and no other text, spelled exactly as written, crisp, "
                       f"legible and well kerned: {'; '.join(texts)}. Typography: {plan.get('typography_ko', '')}.")
        else:
            prompt += "Do not put any text, letters or numbers in the image; keep clean empty space for copy."
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        images = [("hand.jpg", buf.getvalue(), "image/jpeg")]
        if (repose or apply_design) and design_ref is not None:      # 원본 디자인도 같이 -> 손가락을 다시 그려도 디자인 유지
            ref = design_ref.copy(); ref.thumbnail((1024, 1024), Image.LANCZOS)
            rb = io.BytesIO(); ref.save(rb, "JPEG", quality=92)
            images.append(("design.jpg", rb.getvalue(), "image/jpeg"))
        kw = dict(model=self.model, prompt=prompt, n=1, size=f"{w}x{h}",
                  quality=QUALITY.get(int(size or 768), "medium"), image=images)
        self._image_options(kw, stream)

        def preview(b64, idx):
            try:
                on_partial(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), idx)
            except Exception as e:
                print("  배너 미리보기 실패:", e)

        b64, usage, ms = self._image_call(kw, preview if stream else None)
        out = crop_to_ratio(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))      # 배너는 항상 4:5
        if self.a.settings["debug"]["save_images"]:
            save_debug("banner_" + hand_name, {"6_banner_src": src, "7_banner": out,
                                               **({"6b_design_ref": design_ref} if (repose or apply_design) and design_ref is not None else {})},
                       text=json.dumps({"prompt": prompt, "plan": plan}, ensure_ascii=False, indent=1, default=str))
        try:
            info = self.cost.record(usage, self.model, kw["quality"], kw.get("complexity"), f"{w}x{h}", ms / 1000,
                                    "배너:" + hand_name)
        except Exception as e:
            print(f"  ! 비용 기록 실패 (배너는 정상): {type(e).__name__}: {e}")
            info = {"usd": None, "image_usd": None, "today": self.cost.summary()["today"]}
        print(f"  배너 {w}x{h} | {self.model} | 품질 {kw['quality']} | {ms / 1000:.1f}초 | {fmt_cost(info.get('usd'), info.get('krw'))}")
        return out, ms, info

    def generate(self, hand, design, seed=0, desc="", size=None, hand_name="", analysis=None, on_partial=None,
                 nail_length="auto", nail_shape="auto"):
        args = (hand, design, seed, desc, size, hand_name, analysis, on_partial, nail_length, nail_shape)
        try:
            return self._generate(*args)
        except _RetryFixedSize:
            print(f"  ! {self.model} 가 원하는 크기를 받지 않아서 정해진 크기로 다시 보내요")
            self.no_flex_size = True
            return self._generate(*args)

    def _generate(self, hand, design, seed, desc, size, hand_name, analysis, on_partial, nail_length, nail_shape):
        from PIL import Image, ImageFilter, ImageOps
        import openai
        n_len, n_shape, ratio, taper = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
        hand, nails = self._mask(hand_name, hand, ratio, taper)      # 긴 손톱이 밖으로 나가면 사진이 조금 커져요
        W, H = hand.size
        if not nails.getbbox():
            raise UserError(400, "손톱 마스크가 비어 있어요. mask_editor.html 로 다시 그려 주세요.", "MASK_MISSING")
        if ratio > 0:
            print(f"  긴 손톱: {NAIL_LENGTH_KO.get(n_len, n_len)} · {NAIL_SHAPE_KO.get(n_shape, n_shape)}"
                  f" -> 손톱 영역을 손톱 길이의 {ratio:.2f}배 늘림")

        R = self.a.settings["request"]
        # 0) 손톱 주변만 잘라서 보내기 -> 손톱이 크게 보여서 더 잘 그려요 (zoom_to_nails)
        box = nail_crop_box(nails, (W, H), float(R["zoom_padding"])) if R["zoom_to_nails"] else (0, 0, W, H)
        hand_c, nails_c = hand.crop(box), nails.crop(box)
        Wc, Hc = hand_c.size

        # 1) 잘라낸 손 사진을 API 크기 캔버스에 비율 유지해 넣기 (남는 곳은 가장자리 색으로 채움)
        #    gpt-image-2 계열은 잘라낸 비율 그대로, 긴 변 canvas_long_side 로 -> 필요 없는 픽셀을 안 그려서 빨라요
        if self.flex_size():
            L = int(R["canvas_long_side"])
            k = L / max(Wc, Hc)
            cw, ch = max(256, round(Wc * k / 16) * 16), max(256, round(Hc * k / 16) * 16)
        else:
            cw, ch = canvas_size(Wc, Hc)
        s = min(cw / Wc, ch / Hc)
        nw, nh = round(Wc * s), round(Hc * s)
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        fill = tuple(int(v) for v in hand_c.resize((1, 1), Image.BOX).getpixel((0, 0)))
        canvas = Image.new("RGB", (cw, ch), fill); canvas.paste(hand_c.resize((nw, nh), Image.LANCZOS), (ox, oy))

        # 2) API 용 마스크: 손톱(조금 넓힘) = 투명(고칠 곳), 나머지 = 불투명(그대로)
        grow = max(3, (min(nw, nh) // 150) | 1)
        edit = nails_c.resize((nw, nh), Image.NEAREST).filter(ImageFilter.MaxFilter(grow))
        alpha = Image.new("L", (cw, ch), 255); alpha.paste(ImageOps.invert(edit), (ox, oy))
        api_mask = Image.new("RGBA", (cw, ch), (0, 0, 0, 255)); api_mask.putalpha(alpha)
        n_nails = count_nails(nails_c)

        prompt = PROMPT + (f" There are {n_nails} fingernails visible; paint the design on all {n_nails} of them, "
                           "including the thumb and the little finger. " if n_nails else "") \
            + (f" The nail design recognized in image 2: {analysis['design_en']}"
               + (f" Accent nails: {analysis['accent_nails']}." if analysis.get("accent_nails") else "")
               + (f" Finish: {analysis['finish']}." if analysis.get("finish") else "")
               if analysis and analysis.get("has_nails") else "") \
            + (NAIL_EXTEND_SHAPE.format(length=n_len.replace("_", " "), shape=n_shape if n_shape != "unknown" else "",
                                        tip=NAIL_TIP_EN.get(n_shape, "natural")) if ratio > 0 else NAIL_KEEP_SHAPE) \
            + finger_text(analysis, self.hand_side(hand_name)) \
            + (f" Extra request from the customer (highest priority): {desc.strip()[:300]}." if desc and desc.strip() else "")
        if seed:
            prompt += f" (Variation #{int(seed)}: a slightly different but equally faithful rendition.)"
        kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}",
                  quality=QUALITY.get(int(size or 768), "medium"),
                  image=[("hand.png", png_bytes(canvas), "image/png"), ("design.png", png_bytes(design), "image/png")],
                  mask=("mask.png", png_bytes(api_mask), "image/png"))
        # input_fidelity(gpt-image-1.5 만 high) / complexity / webp 출력(작아서 빨라요) / 중간 그림
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        self._image_options(kw, stream)

        def preview(b64, idx):
            """그리는 중간 그림 -> 원본 손 사진의 잘라낸 자리에 넣어서 화면에 미리 보여 줘요."""
            try:
                p = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
                if p.size != (cw, ch):
                    p = p.resize((cw, ch), Image.BILINEAR)
                pv = hand.copy()
                pv.paste(p.crop((ox, oy, ox + nw, oy + nh)).resize((Wc, Hc), Image.BILINEAR), box[:2])
                on_partial(pv, idx)
            except Exception as e:
                print("  미리보기 실패:", e)

        b64, usage, ms = self._image_call(kw, preview if stream else None)
        out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")

        raw = out
        # 3) API 가 손 위치를 조금 옮겨 그렸으면 원본에 맞춰 되돌리기 (안 맞추면 손톱 자리에 배경이 붙어 검게 나와요)
        if out.size != (cw, ch):
            out = out.resize((cw, ch), Image.LANCZOS)
        align = {"used": False}
        if R["align"]:
            # 맞출 때 쓰는 곳 = 사진 영역 중 손톱(넓게) 을 뺀 곳 (손톱은 바뀌었으니 비교에서 빼요)
            keep = Image.new("L", (cw, ch), 0); keep.paste(255, (ox, oy, ox + nw, oy + nh))
            near_nails = ImageOps.invert(alpha).filter(ImageFilter.MaxFilter(grow * 4 + 1))
            keep.paste(0, mask=near_nails)
            out, align = align_to(out, canvas, keep)

        # 4) 결과에서 손 부분만 잘라 원래 크기로 -> 손톱 부분만 원본 위에 부드럽게 붙이기
        out_c = out.crop((ox, oy, ox + nw, oy + nh)).resize((Wc, Hc), Image.LANCZOS)
        feather = max(2, min(Wc, Hc) // 300)
        blend = nails_c.filter(ImageFilter.MaxFilter(max(3, (min(W, H) // 250) | 1))).filter(ImageFilter.GaussianBlur(feather))
        result = hand.copy()
        result.paste(Image.composite(out_c, hand_c, blend), box[:2])

        # 5) 손톱별 검사: 배경색이 칠해졌거나(검은 손톱) 그대로인 손톱 수
        check = check_nails(result, hand, nails)
        if self.a.settings["debug"]["save_images"]:
            save_debug(hand_name, {"0_design_ref": design, "1_sent_hand": canvas, "2_sent_mask": api_mask,
                                   "3_api_raw": raw, "4_aligned": out, "5_result": result},
                       text=json.dumps({"prompt": prompt, "analysis": analysis}, ensure_ascii=False, indent=1,
                                       default=str))

        u = usage
        if analysis:   # 이 사진으로 처음 만든 결과에만 인식 비용을 '이번 비용'에 보여 줘요 (기록은 인식 때 이미 했어요)
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0,
                            cached_result=not first)
        try:
            info = self.cost.record(u, self.model, kw["quality"], kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                    hand_name, analysis=analysis)
        except Exception as e:      # 비용 기록이 실패해도 완성된 그림은 꼭 돌려줘요
            print(f"  ! 비용 기록 실패 (그림은 정상): {type(e).__name__}: {e}")
            info = {"model": self.model, "quality": kw["quality"], "complexity": kw.get("complexity") or "",
                    "text_in": 0, "image_in": 0, "text_out": 0, "image_out": 0, "total_tokens": 0,
                    "usd": None, "krw": None, "image_usd": None, "analysis_usd": None, "priced": False,
                    "today": self.cost.summary()["today"]}
        if analysis:
            info["design"] = {k: analysis.get(k) for k in ("summary_ko", "has_nails", "confidence", "model",
                                                            "accent_nails", "cached_result")}
        td = info["today"]
        print(f"  {hand_name} | {self.model} | 품질 {kw['quality']} | 구상 {kw.get('complexity') or '기본'} | {ms / 1000:.1f}초")
        if u is None:
            print("    토큰: 응답에 사용량 정보가 없어요")
        else:
            print(f"    토큰: 입력 텍스트 {info['text_in']:,} + 이미지 {info['image_in']:,}"
                  f" / 출력 이미지 {info['image_out']:,}" + (f" + 텍스트 {info['text_out']:,}" if info["text_out"] else "")
                  + f"  (합계 {info['total_tokens']:,})")
        an_txt = ""
        if analysis and info.get("analysis_usd") is not None and info.get("image_usd") is not None:
            an_txt = f"  (인식 ${info['analysis_usd']:.4f} + 생성 ${info['image_usd']:.4f})"
        print(f"    비용: {fmt_cost(info['usd'], info['krw'])}{an_txt}   |   오늘 {td['count']}장 {fmt_cost(td['usd'], td['krw'])}")
        if align.get("used"):
            print(f"    위치 맞춤: 이동 ({align['dx']:+.0f}, {align['dy']:+.0f})px, 크기 x{align['scale']:.3f}"
                  + ("" if align.get("ok") else "  (맞추기 실패 -> 그대로 사용)"))
        print(f"    손톱 검사: {check['total']}개 중 정상 {check['ok']}개"
              + (f", 배경색으로 칠해짐 {check['bg']}개" if check["bg"] else "")
              + (f", 디자인 안 들어감 {check['unchanged']}개" if check["unchanged"] else ""))
        info["nails"] = check
        info["nail_extend"] = {"length": n_len, "shape": n_shape, "ratio": round(ratio, 3)}
        info["align"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in align.items()}
        self.last_info = info
        return result, ms, info


class FakeModel:
    """--fake: API 키 없이 화면과 마스크만 확인 (손톱 자리에 디자인 색을 칠해 돌려줘요, 요금 없음)."""

    def __init__(self, a):
        self.a, self.model, self.complexity, self.cost = a, "fake", None, None

    def analyze_design(self, raw_bytes, design, effort=None, scene=False, tipset=False, tip_images=None):
        return design, None

    def hand_side(self, hand_id):
        return "right"

    def ref_hands(self, hand_id):
        return None

    def read_prices(self, text):
        return [], str(text or "").strip()          # --fake: luna 가 없어서 가격표는 만들지 않아요

    def split_sheet(self, img, effort=None):
        time.sleep(0.4)          # 화면 확인용: 위아래로 2세트
        return [{"tips": [{"x": 0.15 + 0.17 * k, "y": y} for k in range(5)], "tip_length": 0.18, "complete": True,
                 "summary_ko": f"(--fake) {n}번째 세트"} for n, y in ((1, 0.3), (2, 0.7))], {"usd": 0.0}

    def generate_scene(self, scene, design, desc="", size=None, scene_id="", analysis=None, on_partial=None, **kw):
        from PIL import Image, ImageFilter
        color = design.resize((1, 1), Image.BOX).getpixel((0, 0))
        tint = Image.blend(scene, Image.new("RGB", scene.size, color), 0.35)
        if on_partial:
            for i, r in enumerate((24, 8)):
                time.sleep(0.6); on_partial(tint.filter(ImageFilter.GaussianBlur(r)), i)
        time.sleep(0.5)
        out = tint.copy()
        d = design.copy(); d.thumbnail((scene.width // 4, scene.height // 4)); out.paste(d, (scene.width - d.width - 16, 16))
        return crop_to_ratio(out), 500, {"usd": 0.0, "image_usd": 0.0, "today": {"count": 0, "usd": 0.0, "krw": 0},
                          "nail_extend": {"length": "keep", "shape": "unknown", "ratio": 0.0}}

    def generate(self, hand, design, seed=0, desc="", size=None, hand_name="", analysis=None, on_partial=None,
                 nail_length="auto", nail_shape="auto"):
        from PIL import Image, ImageFilter
        n_len, n_shape, ratio, taper = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
        nails = nail_mask_for(hand_name, hand.size).point(lambda v: 255 if v >= 128 else 0)
        if ratio > 0:
            hand, nails = extend_nails_padded(hand, nails, ratio, taper)
        nails = nails.filter(ImageFilter.GaussianBlur(2))
        color = design.resize((1, 1), Image.BOX).getpixel((0, 0))
        if on_partial:                       # 미리보기 흉내 (흐릿한 그림 2장)
            for i, r in enumerate((24, 8)):
                time.sleep(0.6)
                on_partial(Image.composite(Image.new("RGB", hand.size, color), hand, nails).filter(ImageFilter.GaussianBlur(r)), i)
        time.sleep(0.5)
        out = Image.composite(Image.new("RGB", hand.size, color), hand, nails)
        d = design.copy(); d.thumbnail((hand.width // 4, hand.height // 4)); out.paste(d, (hand.width - d.width - 16, 16))
        info = {"model": "fake", "quality": QUALITY.get(int(size or 768), "medium"), "complexity": "",
                "text_in": 0, "image_in": 0, "text_out": 0, "image_out": 0, "total_tokens": 0,
                "usd": 0.0, "krw": 0.0, "priced": True, "today": {"count": 0, "usd": 0.0, "krw": 0},
                "nail_extend": {"length": n_len, "shape": n_shape, "ratio": round(ratio, 3)}}
        return out, 500, info

    def generate_free(self, hand, design, **kw):
        return self.generate(hand, design, **kw)

    def plan_banner(self, nail_img, analysis=None, brand="", desc="", effort=None, pose="auto", bare_design=None,
                    scene=False):
        from PIL import Image
        time.sleep(0.4)
        c = nail_img.resize((1, 1), Image.BOX).getpixel((0, 0))
        hx = "#%02x%02x%02x" % c
        return {"concept_ko": "(--fake) 화면 확인용 계획서", "mood_ko": "차분하고 고급스러운", "headline_ko": "이번 시즌 네일",
                "subcopy_ko": "손끝에 담은 작은 계절", "cta_ko": "지금 예약하기",
                "palette": [{"name": "nail", "hex": hx}, {"name": "ivory", "hex": "#f7f1ea"}, {"name": "ink", "hex": "#2b2230"}],
                "background_ko": "부드러운 단색 배경", "layout_ko": "왼쪽 손, 오른쪽 문구", "typography_ko": "얇은 명조체",
                "image_prompt_en": "fake", "model": "fake", "usd": 0.0, "pose_change": pose != "keep",
                "pose_ko": "원래 자세 유지" if pose == "keep" else "(--fake) 손가락을 부채꼴로 펼친 자세", "pose_en": "", "brand": brand or self.a.settings["banner"]["brand"]}

    def make_banner(self, nail_img, plan, size=None, hand_name="", on_partial=None, design_ref=None, **kw):
        from PIL import Image, ImageDraw, ImageFont
        w, h = parse_size(self.a.settings["banner"]["size"])
        bg = tuple(int(plan["palette"][0]["hex"][i:i + 2], 16) for i in (1, 3, 5))
        out = Image.new("RGB", (w, h), tuple(int(v * 0.35 + 247 * 0.65) for v in bg))
        hand = nail_img.copy(); hand.thumbnail((int(w * 0.5), int(h * 0.92)))
        out.paste(hand, (int(w * 0.06), (h - hand.height) // 2))
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/malgunbd.ttf", h // 11)
            small = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", h // 26)
        except Exception:
            font = small = ImageFont.load_default()
        d = ImageDraw.Draw(out)
        x = int(w * 0.6)
        d.text((x, int(h * 0.34)), plan["headline_ko"], fill=(43, 34, 48), font=font)
        d.text((x, int(h * 0.50)), plan["subcopy_ko"], fill=(90, 80, 96), font=small)
        d.rounded_rectangle((x, int(h * 0.62), x + int(w * 0.18), int(h * 0.70)), radius=h // 40, fill=(43, 34, 48))
        d.text((x + int(w * 0.09), int(h * 0.66)), plan["cta_ko"], fill=(255, 255, 255), font=small, anchor="mm")
        if on_partial:
            time.sleep(0.6); on_partial(out.resize((w // 8, h // 8)).resize((w, h)), 0)
        time.sleep(0.6)
        return out, 600, {"usd": 0.0, "image_usd": 0.0, "today": {"count": 0, "usd": 0.0, "krw": 0}}


def build_app(model):
    from typing import Literal, Optional
    from fastapi import FastAPI, File, Form, Request, UploadFile
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from PIL import Image, ImageOps
    import api_schema as S

    docs = bool(model.a.settings["server"]["api_docs"])
    app = FastAPI(
        title="NailShot AI API", version=S.API_VERSION,
        description="손 사진(서버에 준비된 8장 중 하나) + 사용자가 올린 아무 네일 사진 -> 그 디자인을 입힌 손 사진.\n\n"
                    "사람이 읽는 설명서: API_SPEC.md · 오류는 항상 {detail, code} 형식이에요.",
        docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None)
    show_cost = not model.a.hide_cost
    ERR = {"model": S.ErrorResponse}

    # ---------------------------------------------------------------- 오류 형식 통일: {detail, code}
    def err(status, detail, code, errors=None):
        body = {"detail": detail, "code": code}
        if errors is not None:
            body["errors"] = errors
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(UserError)
    def _user_error(request: Request, e: UserError):
        print(f"요청 실패 [{e.code}]: {e.msg}")
        return err(e.status, e.msg, e.code)

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, e: RequestValidationError):
        items = [{"field": ".".join(str(x) for x in er.get("loc", [])[1:]) or "body", "message": er.get("msg", "")}
                 for er in e.errors()]
        names = ", ".join(i["field"] for i in items)
        return err(422, f"요청 형식이 올바르지 않아요: {names}", "INVALID_REQUEST", items)

    @app.exception_handler(StarletteHTTPException)
    def _http(request: Request, e: StarletteHTTPException):
        return err(e.status_code, str(e.detail), UserError.DEFAULT_CODES.get(e.status_code, "INTERNAL_ERROR"))

    @app.exception_handler(Exception)
    def _internal(request: Request, e: Exception):
        print("서버 오류:", repr(e))
        return err(500, f"서버 오류가 났어요: {type(e).__name__}", "INTERNAL_ERROR")

    # ---------------------------------------------------------------- 입력 확인
    def read_design(design: UploadFile):
        raw = design.file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise UserError(413, "디자인 사진은 10MB 이하로 올려 주세요", "FILE_TOO_LARGE")
        try:
            im = Image.open(io.BytesIO(raw))
            if im.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError(im.format)
            d = ImageOps.exif_transpose(im).convert("RGB")       # 위치정보 등은 버려요
        except Exception:
            raise UserError(400, "이미지 파일(JPG, PNG, WebP)만 올릴 수 있어요", "INVALID_IMAGE")
        d.thumbnail((model.a.ref_size, model.a.ref_size))
        return raw, d

    def read_hand(hand: str):
        name = os.path.basename(hand or "")
        hp = os.path.join(HANDS, name)
        if not name or not os.path.isfile(hp) or os.path.splitext(hp)[1].lower() not in EXTS:
            raise UserError(400, "고른 손 사진을 찾을 수 없어요 (GET /api/hands 의 id 를 보내 주세요)", "HAND_NOT_FOUND")
        return name, ImageOps.exif_transpose(Image.open(hp)).convert("RGB")

    def pick_quality(quality, size):
        return quality or S.LEGACY_SIZE_TO_QUALITY.get(size or 0) or "normal"

    # ---------------------------------------------------------------- 출력 만들기
    def design_out(an):
        if not an:
            return None
        return S.DesignInfo(summary_ko=an.get("summary_ko", ""), has_nails=bool(an.get("has_nails")),
                            confidence=max(0.0, min(1.0, float(an.get("confidence") or 0))),
                            accent_nails=an.get("accent_nails") or "", model=an.get("model", ""),
                            cached_result=bool(an.get("cached_result")),
                            usd=an.get("usd") if show_cost else None,
                            suggested_hand=an.get("suggested_hand"), hand_reason_ko=an.get("hand_reason_ko") or "",
                            nail_length=an.get("nail_length") or "", nail_shape=an.get("nail_shape") or "",
                            reasoning_effort=an.get("reasoning_effort") or "",
                            scene_ko=an.get("scene_ko") or "",
                            hand_side=(model.hand_side(an.get("suggested_hand")) if an.get("suggested_hand") else ""),
                            fingers_ko=finger_ko(an, model.hand_side(an.get("suggested_hand") or "")))

    def encode_result(img):
        fmt = str(model.a.settings["server"]["result_format"]).lower()
        buf = io.BytesIO()
        if fmt == "jpeg":
            img.save(buf, "JPEG", quality=95, subsampling=0); return buf.getvalue(), "image/jpeg"
        img.save(buf, "PNG"); return buf.getvalue(), "image/png"

    def result_out(img, ms, info, name, quality, variation, an):
        data, mime = encode_result(img)
        fields = S.GenerationUsage.model_fields
        usage = None
        if show_cost and info.get("today") is not None:
            usage = S.GenerationUsage(**{k: info[k] for k in fields if k in info and info[k] is not None})
        al = info.get("align")
        res = S.GenerateResult(
            image=f"data:{mime};base64," + base64.b64encode(data).decode(), mime=mime, width=img.width, height=img.height,
            ms=int(ms), hand=name, quality=quality, variation=int(variation), design=design_out(an),
            nails=S.NailCheck(**info["nails"]) if info.get("nails") else None,
            align=S.AlignInfo(**{k: v for k, v in al.items() if k in S.AlignInfo.model_fields}) if al else None,
            nail_extend=S.NailExtend(**info["nail_extend"]) if info.get("nail_extend") else None,
            usage=usage)
        return res, data, mime

    def run_generate(name, handimg, raw, d, quality, variation, desc, on_partial=None, on_design=None, on_stage=None,
                     nail_length="auto", nail_shape="auto", effort=""):
        if on_stage:
            on_stage("analyzing", "디자인 확인 중...")
        ref, an = model.analyze_design(raw, d, effort=effort)       # 1단계: 사진에서 네일 디자인 인식
        if on_design and an:
            on_design(an)
        if on_stage:
            on_stage("generating", "그리는 중...")
        img, ms, info = model.generate(handimg, ref, seed=variation, desc=desc, size=S.QUALITY_TO_LEVEL[quality],
                                       hand_name=name, analysis=an, on_partial=on_partial,   # 2단계: 손에 입히기
                                       nail_length=nail_length, nail_shape=nail_shape)
        an_now = None
        if an:
            an_now = dict(an, cached_result=(info.get("design") or {}).get("cached_result", an.get("cached_result")))
        return result_out(img, ms, info, name, quality, variation, an_now)

    # ---------------------------------------------------------------- 공통 입력 (생성 2종)
    def f_hand():
        return Form(..., description="손 사진 id (GET /api/hands 의 id)", examples=["hand1_blue_front.jpg"])

    def f_design():
        return File(..., description="네일 디자인 사진. JPG/PNG/WebP, 10MB 이하. 인스타 캡처, 손 사진 등 아무 사진")

    def f_quality():
        return Form(None, description="품질: fast(빠름, 저렴) | normal(기본) | high(고화질, 느림). 기본 normal")

    def f_var():
        return Form(0, ge=0, le=9999, description="0 = 기본. 1,2,3... = '다른 느낌으로 다시' (같은 조건에서 다른 결과)")

    def f_desc():
        return Form("", max_length=300, description="추가 요청(선택, 영어 권장). 인식 결과보다 우선")

    def f_len():
        return Form("auto", description="손톱 길이: auto(인식대로) | keep(손 사진 원래 길이) | medium | long | extra_long")

    def f_shape():
        return Form("auto", description="손톱 모양: auto(인식대로) | almond | oval | round | square | squoval | coffin | stiletto")

    def f_effort(what):
        return Form("", description=f"{what} Reasoning effort: none|minimal|low|medium|high|xhigh|max. 빈 값 = settings.yaml")

    def f_size():
        return Form(None, deprecated=True, description="[예전 호환] 512|768|1024 -> fast|normal|high. quality 를 쓰세요")

    def f_seed():
        return Form(None, deprecated=True, description="[예전 호환] variation 과 같아요")

    # ---------------------------------------------------------------- 라우트
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(WEB, "banner.html"), headers=NO_CACHE)   # 사진 1장 -> 배너 (화면 고치면 바로 반영)

    @app.get("/single", include_in_schema=False)
    def single():
        return FileResponse(os.path.join(WEB, "index.html"), headers=NO_CACHE)    # 예전 화면: 손 직접 고르고 1장씩

    @app.get("/api/health", response_model=S.HealthResponse, tags=["정보"], summary="서버 상태와 설정")
    def health():
        da = model.a.settings["design_analysis"]
        return S.HealthResponse(model=model.model, complexity=model.complexity,
                                analysis_model=da["model"] if da["enabled"] and not isinstance(model, FakeModel) else None,
                                fake=isinstance(model, FakeModel), show_cost=show_cost,
                                banner_size=str(model.a.settings["banner"]["size"]),
                                max_designs=model.a.settings["banner"]["max_designs"],
                                pose=model.a.settings["banner"]["pose"],
                                apply_mode=model.a.settings["banner"]["apply_mode"],
                                scene_banner=bool(model.a.settings["banner"]["scene_banner"]),
                                scene_count=len(list_scenes()),
                                max_sets=model.a.settings["sheet"]["max_sets"],
                                brand=model.a.settings["banner"]["brand"],
                                analysis_effort=da["reasoning_effort"],
                                plan_effort=model.a.settings["banner"]["plan_reasoning_effort"],
                                plan_model=(model.a.settings["banner"]["plan_model"] or da["model"])
                                if not isinstance(model, FakeModel) else None)

    @app.get("/api/usage", response_model=S.UsageResponse, tags=["정보"], summary="오늘/전체 사용량과 비용",
             responses={404: {**ERR, "description": "비용 숨김(server.hide_cost) 상태"}})
    def usage():
        if not show_cost:
            raise UserError(404, "Not Found", "NOT_FOUND")
        if model.cost is None:
            z = {"count": 0, "usd": 0.0, "krw": 0}
            return S.UsageResponse(today=z, total=z, usd_krw=None, model=model.model, complexity=None)
        return S.UsageResponse(**model.cost.summary(), model=model.model, complexity=model.complexity)

    @app.get("/api/hands", response_model=list[S.Hand], tags=["손 사진"], summary="고를 수 있는 손 사진 목록")
    def hands():
        return list_hands()

    @app.get("/hands/{name}", tags=["손 사진"], summary="손 사진 파일", response_class=FileResponse,
             responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}, 404: ERR})
    def hand_file(name: str):
        p = os.path.join(HANDS, os.path.basename(name))
        if not os.path.isfile(p) or os.path.splitext(p)[1].lower() not in EXTS:
            raise UserError(404, "손 사진이 없어요", "NOT_FOUND")
        return FileResponse(p)

    @app.get("/api/scenes", response_model=list[S.Hand], tags=["손 사진"], summary="장면 사진(nailhand) 목록")
    def scenes():
        return [S.Hand(id=x["id"], label=x["label"], url=x["url"]) for x in list_scenes()]

    @app.get("/nailhand/{path:path}", tags=["손 사진"], summary="장면 사진 파일", response_class=FileResponse,
             responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}, 404: ERR})
    def scene_file(path: str):
        p = scene_path(path)
        if not p:
            raise UserError(404, "장면 사진이 없어요", "NOT_FOUND")
        return FileResponse(p)

    @app.post("/api/analyze", response_model=S.AnalyzeResponse, tags=["생성"], summary="디자인 사진 미리 인식",
              description="사진을 올리는 순간 호출해 두면, 같은 사진으로 생성할 때 인식을 다시 하지 않아 빨라져요.",
              responses={400: ERR, 413: ERR, 422: {**ERR, "description": "NO_NAILS_DETECTED 또는 INVALID_REQUEST"},
                         500: ERR})
    def analyze(design: UploadFile = f_design(), analysis_effort: S.Effort = f_effort("디자인 인식")):
        raw, d = read_design(design)
        _, an = model.analyze_design(raw, d, effort=analysis_effort)
        return S.AnalyzeResponse(enabled=an is not None, design=design_out(an))

    @app.post("/api/generate", tags=["생성"], summary="네일아트 입히기 (한 번에 결과 받기)",
              response_model=S.GenerateResult,
              responses={200: {"content": {"image/jpeg": {}, "image/png": {}},
                               "description": "response_format=json(기본): GenerateResult JSON. "
                                              "response_format=image: 이미지 파일 + X-Result 헤더(JSON, image 제외)"},
                         400: ERR, 413: ERR, 422: ERR, 429: ERR, 500: ERR, 504: ERR})
    def generate(hand: str = f_hand(), design: UploadFile = f_design(), quality: Optional[S.Quality] = f_quality(),
                 variation: int = f_var(), desc: str = f_desc(), size: Optional[int] = f_size(),
                 seed: Optional[int] = f_seed(),
                 response_format: Literal["json", "image"] = Form("json", description="json(기본) | image"),
                 nail_length: S.NailLength = f_len(), nail_shape: S.NailShape = f_shape(),
                 analysis_effort: S.Effort = f_effort("디자인 인식")):
        name, handimg = read_hand(hand)
        raw, d = read_design(design)
        q = pick_quality(quality, size)
        res, data, mime = run_generate(name, handimg, raw, d, q, variation or seed or 0, desc,
                                       nail_length=nail_length, nail_shape=nail_shape, effort=analysis_effort)
        if response_format == "image":
            meta = res.model_dump(exclude={"image"})
            return Response(data, media_type=mime, headers={"X-Elapsed-Ms": str(res.ms), "Cache-Control": "no-store",
                                                            "X-Result": json.dumps(meta, ensure_ascii=True)})
        return JSONResponse(res.model_dump(), headers={"Cache-Control": "no-store"})

    @app.post("/api/generate_stream", tags=["생성"], summary="네일아트 입히기 (그리는 중간 그림 받기, SSE)",
              description="응답은 text/event-stream. 이벤트 순서: stage -> design -> stage -> partial(0~3개) -> final. "
                          "실패 시 error 이벤트 하나로 끝나요. 입력 확인 오류는 일반 JSON 오류로 바로 응답해요.",
              responses={200: {"content": {"text/event-stream": {}}}, 400: ERR, 413: ERR, 422: ERR})
    def generate_stream(hand: str = f_hand(), design: UploadFile = f_design(),
                        quality: Optional[S.Quality] = f_quality(), variation: int = f_var(), desc: str = f_desc(),
                        size: Optional[int] = f_size(), seed: Optional[int] = f_seed(),
                        nail_length: S.NailLength = f_len(), nail_shape: S.NailShape = f_shape(),
                        analysis_effort: S.Effort = f_effort("디자인 인식")):
        import queue
        name, handimg = read_hand(hand)
        raw, d = read_design(design)
        q_level = pick_quality(quality, size)
        var = variation or seed or 0
        q = queue.Queue()

        def b64img(img, quality=70):
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=quality); return base64.b64encode(buf.getvalue()).decode()

        def put(kind, model_obj):
            q.put((kind, model_obj.model_dump()))

        def work():
            try:
                res, _, _ = run_generate(
                    name, handimg, raw, d, q_level, var, desc,
                    on_stage=lambda step, text: put("stage", S.StageEvent(step=step, text=text)),
                    on_design=lambda an: put("design", design_out(an)),
                    on_partial=lambda im, i: put("partial", S.PartialEvent(
                        index=i, image="data:image/jpeg;base64," + b64img(im.resize((im.width // 2, im.height // 2))))),
                    nail_length=nail_length, nail_shape=nail_shape, effort=analysis_effort)
                put("final", res)
            except UserError as e:
                print(f"생성 실패 [{e.code}]: {e.msg}")
                put("error", S.ErrorEvent(status=e.status, code=e.code, message=e.msg))
            except Exception as e:
                print("생성 오류:", repr(e))
                put("error", S.ErrorEvent(status=500, code="INTERNAL_ERROR",
                                          message=f"생성 중 오류가 났어요: {type(e).__name__}"))
            finally:
                q.put(None)

        threading.Thread(target=work, daemon=True).start()

        def events():
            yield ": start\n\n"
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"          # 연결이 끊기지 않게
                    continue
                if item is None:
                    break
                kind, payload = item
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- 배너: 사진 1~5장 -> 배너 1~5장
    @app.post("/api/banner_stream", tags=["배너"], summary="네일 사진 1~5장 -> 손 고르기·입히기·계획서·배너 (SSE)",
              description="응답은 text/event-stream. 사진마다 item(0부터)이 붙은 이벤트가 섞여 와요: "
                          "item_stage -> item_design -> item_partial(nail) -> item_nail -> item_plan -> "
                          "item_partial(banner) -> item_banner. 실패한 사진은 item_error 로 끝나고 나머지는 계속돼요. "
                          "모두 끝나면 done. 입력 확인 오류는 일반 JSON 오류로 바로 응답해요.",
              responses={200: {"content": {"text/event-stream": {}}}, 400: ERR, 413: ERR, 422: ERR})
    def banner_stream(designs: list[UploadFile] = File(..., description="네일 사진 1~5장 (같은 이름 designs 로 여러 번)"),
                      quality: Optional[S.Quality] = f_quality(),
                      brand: str = Form("", max_length=30, description="배너에 넣을 상호명 (선택)"),
                      desc: str = f_desc(),
                      analysis_effort: S.Effort = f_effort("① 디자인 인식·손 고르기"),
                      plan_effort: S.Effort = f_effort("③ 배너 계획서"),
                      nail_length: S.NailLength = f_len(), nail_shape: S.NailShape = f_shape(),
                      apply_mode: Literal["", "mask", "free", "direct", "scene"] = Form("", description="② 적용 방식: mask(손톱 마스크, 가장 정확) | free(마스크 없이 통째로) | direct(② 없이 배너에서 한 번에) | scene(nailhand 장면 사진에 입히고 음료 색·배경 바꾸기). 빈 값 = settings.yaml banner.apply_mode"),
                      scene_banner: Literal["", "on", "off"] = Form("", description="scene 방식에서 ③④ 배너까지 만들지: on | off(장면 사진이 완성품). 빈 값 = settings.yaml banner.scene_banner"),
                      pose: Literal["", "auto", "keep"] = Form("", description="④ 배너의 손 자세: auto(디자인에 맞게 바꾸기) | keep(원래 자세). 빈 값 = settings.yaml banner.pose"),
                      split_sets: Literal["", "on", "off"] = Form("", description="on = 사진 1장에 네일 팁 세트(5개씩)가 여러 개 -> 세트마다 1장씩 만들어요 (먼저 sets 이벤트로 세트 목록). 빈 값 = off")):
        import queue
        B = model.a.settings["banner"]
        up_names = [os.path.basename(f.filename or f"사진{i + 1}") for i, f in enumerate(designs)]
        report = RunReport({"업로드 사진": ", ".join(up_names), "적용 방식": apply_mode or B["apply_mode"],
                            "품질": quality or "", "세트 나누기": split_sets or "off", "추가 요청(원문)": desc or "",
                            "인식 effort": analysis_effort or "", "계획서 effort": plan_effort or "",
                            "이미지 모델": getattr(model, "model", ""),
                            # 이 실험 때의 Image-2 적용 설정 (settings.yaml banner) - 실제로 쓴 값
                            "원본 참조율(original_ratio)": round(orig_ratio(model.a.settings) * 100),
                            "자연스러움 레벨(natural_level)": round(natural_level(model.a.settings) * 100)},
                           krw=model.cost.krw if getattr(model, "cost", None) else 0)
        run_bind(report)
        try:
            prices, desc = model.read_prices(desc)     # 추가 요청에 적은 가격 -> luna 가 라벨로 (나머지 글만 배경 · 음료에)
        finally:
            run_unbind()
        report.meta["가격"] = ", ".join(prices)
        if prices:
            print(f"  가격 {len(prices)}개: {', '.join(prices)}" + (f" / 나머지 요청: {desc}" if desc else ""))
            split_sets = "on"                      # 가격표는 세트를 찾아야 붙일 수 있어요
        if not 1 <= len(designs) <= B["max_designs"]:
            raise UserError(422, f"네일 사진은 1~{B['max_designs']}장 올려 주세요 (지금 {len(designs)}장)", "INVALID_REQUEST")
        items = []
        for i, f in enumerate(designs):
            try:
                items.append(read_design(f))
            except UserError as e:
                raise UserError(e.status, f"{i + 1}번째 사진: {e.msg}", e.code)
        mode = apply_mode or B["apply_mode"]
        scene = mode == "scene"
        with_banner = (scene_banner == "on") if scene_banner else bool(B["scene_banner"])
        hands_all = list_scenes() if scene else list_hands()
        if not hands_all:
            raise UserError(400, "nailhand 폴더에 장면 사진이 없어요" if scene else "hands 폴더에 손 사진이 없어요",
                            "HAND_NOT_FOUND")
        hand_by_id = {h["id"]: h for h in hands_all}
        level = S.QUALITY_TO_LEVEL[pick_quality(quality, None)]
        if mode == "mask":
            missing = [h["id"] for h in hands_all if not os.path.isfile(os.path.join(MASKS, os.path.splitext(h["id"])[0] + ".png"))]
            if missing:      # 마스크 없는 손은 마스크 방식에서 고르지 않아요
                hands_all = [h for h in hands_all if h["id"] not in missing] or hands_all
                hand_by_id = {h["id"]: h for h in hands_all}
        q = queue.Queue()
        totals = {"ok": 0, "failed": 0, "usd": 0.0, "priced": True}
        lock = threading.Lock()

        def put(kind, obj):
            q.put((kind, obj.model_dump()))

        def data_url(img, quality=90, half=False):
            if half:
                img = img.resize((max(1, img.width // 2), max(1, img.height // 2)))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=quality)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

        def add(v):
            return 0.0 if v is None else float(v)

        tip_cache, tip_lock = {}, threading.Lock()

        def get_tips(d, tipset):
            """세트의 팁 하나씩 사진 (미리보기와 생성이 같은 결과를 쓰게 한 번만 잘라요). 점수 낮은 팁은 luna 재확인."""
            key = id(tipset)
            with tip_lock:
                if key in tip_cache:
                    return tip_cache[key]
            res = tip_crops(d, tipset, verify=getattr(model, "verify_tips", None),
                            measure=getattr(model, "measure_tips", None))
            with tip_lock:
                tip_cache[key] = res
            return res

        def judge_fingers(i, img, tips):
            """자동 평가 ③: 결과 사진의 손가락마다 몇 번 팁 디자인인지 (luna 심사) -> 정확도 · F1"""
            if not tips or not hasattr(model, "judge_fingers"):
                return
            try:
                report.add_fingers(i, model.judge_fingers(img, tips), len(tips))
            except Exception as e:
                print(f"  ! 손가락 배정 평가 실패: {e!r}")

        def work(i, raw, d, tipset=False):
            stage = lambda step, text: put("item_stage", S.BannerStageEvent(item=i, step=step, text=text))
            run_bind(report, i)
            t_item = time.time()
            done_item = lambda st: report.item(i, 상태=st, **{"생성 시간(초)": round(time.time() - t_item, 1)})
            try:
                # 1) 디자인 인식 + 손 고르기 (인식 모델 1번 호출)
                stage("analyzing", "디자인 인식 · 손 고르는 중...")
                try:
                    tip_imgs = get_tips(d, tipset) if isinstance(tipset, dict) else None     # 팁 하나씩 (둘레 하얗게)
                except Exception as e:      # 팁 자르기가 실패해도 세트 사진 통째로 계속 진행
                    print(f"  [경고] 팁 자르기 실패 - 세트 사진 그대로 진행: {e!r}")
                    tip_imgs = None
                ref, an = model.analyze_design(raw, d, effort=analysis_effort, scene=scene, tipset=bool(tipset),
                                               tip_images=tip_imgs)
                hid = (an or {}).get("suggested_hand")
                auto = hid in hand_by_id
                hand = hand_by_id[hid] if auto else hands_all[i % len(hands_all)]
                report.item(i, **{"고른 레퍼런스": hand["id"], "레퍼런스 고른 방법": "luna 자동" if auto else "순서대로(인식 실패)"})
                put("item_design", S.BannerDesignEvent(item=i, design=design_out(an), hand_auto=auto,
                                                       hand=S.Hand(id=hand["id"], label=hand["label"], url=hand["url"]),
                                                       hand_reason_ko=(an or {}).get("hand_reason_ko", "") if auto else ""))
                # 2) 고른 손에 네일아트 입히기 (이미지 모델)
                if scene:
                    name = hand["id"]
                    handimg = ImageOps.exif_transpose(Image.open(scene_path(name))).convert("RGB")
                else:
                    name, handimg = read_hand(hand["id"])
                partial_nail = lambda im, k: put("item_partial", S.BannerPartialEvent(
                    item=i, phase="nail", index=k, image=data_url(im, 70, half=True)))
                if scene:                  # ② 장면 사진: 손톱 + 음료 색 + 배경 (생성 1번)
                    stage("applying", "장면 사진에 네일 입히고 음료 색 · 배경 바꾸는 중...")
                    img, ms, info = model.generate_scene(
                        handimg, ref, desc=desc, size=level, scene_id=name, analysis=an,
                        on_partial=partial_nail if with_banner else (lambda im, k: put("item_partial", S.BannerPartialEvent(
                            item=i, phase="banner", index=k, image=data_url(im, 70, half=True)))),
                        nail_length=nail_length, nail_shape=nail_shape,
                        tip_images=tip_imgs, set_image=d if isinstance(tipset, dict) else None)
                    if not with_banner:    # 장면 사진이 완성품 -> ③④ 건너뜀
                        data, mime = encode_result(img)
                        item_usd = info.get("usd")
                        with lock:
                            totals["ok"] += 1
                            totals["usd"] += add(item_usd)
                            totals["priced"] &= item_usd is not None
                        done_item("완료")
                        put("item_banner", S.BannerResultEvent(
                            item=i, image=f"data:{mime};base64," + base64.b64encode(data).decode(), mime=mime,
                            width=img.width, height=img.height, ms=int(ms), item_usd=item_usd if show_cost else None))
                        judge_fingers(i, img, tip_imgs)
                        return
                    put("item_nail", S.BannerNailEvent(item=i, image=data_url(img, 92), width=img.width, height=img.height,
                                                       ms=int(ms), hand=os.path.basename(name), nails=None, nail_extend=None))
                elif mode == "direct":       # ② 건너뜀: ④ 에서 맨손 + 디자인으로 한 번에
                    stage("applying", "건너뜀 · 배너에서 한 번에 입혀요")
                    img, ms = handimg, 0
                    entry = model.analysis_cache.get((an or {}).get("key"), {}) if hasattr(model, "analysis_cache") else {}
                    info = {"usd": ((an or {}).get("usd") or 0.0) if entry.pop("unbilled", False) else 0.0}
                else:
                    stage("applying", "손에 네일아트 입히는 중..." + (" (마스크 없이)" if mode == "free" else ""))
                    gen = model.generate_free if mode == "free" else model.generate
                    kwargs = {} if mode == "free" else {"seed": 0}
                    img, ms, info = gen(handimg, ref, desc=desc, size=level, hand_name=name, analysis=an,
                                        on_partial=partial_nail, nail_length=nail_length, nail_shape=nail_shape, **kwargs)
                    put("item_nail", S.BannerNailEvent(item=i, image=data_url(img, 92), width=img.width, height=img.height,
                                                       ms=int(ms), hand=name,
                                                       nails=S.NailCheck(**info["nails"]) if info.get("nails") else None,
                                                       nail_extend=S.NailExtend(**info["nail_extend"]) if info.get("nail_extend") else None))
                # 3) 네일 입힌 사진을 보고 배경·배너 계획서 (인식 모델)
                stage("planning", "배경 · 배너 디자인 계획서 만드는 중...")
                plan = model.plan_banner(img, an, brand=brand, desc=desc, effort=plan_effort, pose=pose,
                                         bare_design=ref if mode == "direct" else None, scene=scene)
                put("item_plan", S.BannerPlanEvent(item=i, plan=S.BannerPlan(
                    **{**plan, "usd": plan.get("usd") if show_cost else None})))
                # 4) 계획서대로 그 손 사진으로 배너 (이미지 모델)
                stage("banner", "배너 만드는 중...")
                ban, ms2, binfo = model.make_banner(
                    img, plan, size=level, hand_name=os.path.basename(name), design_ref=ref,
                    on_partial=lambda im, k: put("item_partial", S.BannerPartialEvent(
                        item=i, phase="banner", index=k, image=data_url(im, 70, half=True))),
                    apply_design=(mode == "direct"), analysis=an, nail_length=nail_length, nail_shape=nail_shape,
                    scene=scene)
                data, mime = encode_result(ban)
                parts = (info.get("usd"), plan.get("usd"), binfo.get("image_usd", binfo.get("usd")))
                item_usd = None if any(p is None for p in parts) else sum(add(p) for p in parts)
                with lock:
                    totals["ok"] += 1
                    totals["usd"] += add(item_usd)
                    totals["priced"] &= item_usd is not None
                done_item("완료")
                put("item_banner", S.BannerResultEvent(
                    item=i, image=f"data:{mime};base64," + base64.b64encode(data).decode(), mime=mime,
                    width=ban.width, height=ban.height, ms=int(ms2), item_usd=item_usd if show_cost else None))
                judge_fingers(i, ban if mode == "direct" else img, tip_imgs)
            except UserError as e:
                done_item(f"실패: {e.msg}")
                print(f"배너 {i + 1}번 실패 [{e.code}]: {e.msg}")
                with lock:
                    totals["failed"] += 1
                put("item_error", S.BannerErrorEvent(item=i, status=e.status, code=e.code, message=e.msg))
            except Exception as e:
                done_item(f"실패: {type(e).__name__}")
                print(f"배너 {i + 1}번 오류:", repr(e))
                with lock:
                    totals["failed"] += 1
                put("item_error", S.BannerErrorEvent(item=i, status=500, code="INTERNAL_ERROR",
                                                     message=f"만드는 중 오류가 났어요: {type(e).__name__}"))
            finally:
                run_unbind()

        def split_all():
            """⓪ 사진마다 세트를 찾아 세트 하나 = 만들 사진 하나로 펼쳐요 -> [(raw, 잘라낸 사진, tipset)]"""
            SH = model.a.settings["sheet"]
            out, info = [], []
            pi = 0                                  # 다음에 붙일 가격 번호
            for n, (raw, d) in enumerate(items):
                try:
                    full = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
                    full.thumbnail((1536, 1536))
                    sets, cost = model.split_sheet(full)
                    with lock:
                        totals["usd"] += add(cost.get("usd"))
                        totals["priced"] &= cost.get("usd") is not None
                    if sets and hasattr(model, "judge_detection"):       # 자동 평가 ①: 팁 찾기 F1 (luna 심사)
                        report.add_detect(up_names[n] if n < len(up_names) else n + 1, model.judge_detection(full, sets))
                except UserError:
                    raise
                except Exception as e:
                    print("세트 나누기 오류:", repr(e))
                    sets = []
                found, priced = 0, []
                # ① 올린 원본 사진을 통째로 돌려 Y축을 위아래로 똑바로 (회전 보정)
                # ② 돌린 사진에서 세트를 다시 찾고 (luna) ③ 그 사진에서 세트를 하나씩 그대로 잘라요 (세트마다 따로 돌리지 않아요)
                turn = sheet_turn(full, sets) if sets else 0.0
                full_up, sets_up = full, sets
                hi_src = None                       # 고해상도 원본 (회전 보정 포함) - 직선으로 잘린 팁을 여기서 다시 잘라요
                if sets and TIP_HI_RECUT:
                    try:
                        hi_src = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
                        hi_src.thumbnail((2560, 2560))
                        if turn:
                            hi_src, _ = rotate_keep(hi_src, turn)
                    except Exception as e:
                        print(f"  ! 고해상도 원본 준비 실패: {e!r}")
                        hi_src = None
                if turn:
                    try:
                        orig = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
                        orig.thumbnail((2560, 2560))
                        full_up, _ = rotate_keep(orig, turn)
                        full_up.thumbnail((1536, 1536))
                        print(f"  [회전 보정] {n + 1}번 사진을 {turn:.1f}도 돌렸어요 -> 돌린 사진에서 세트를 다시 찾아요")
                        sets2, cost2 = model.split_sheet(full_up)
                        with lock:
                            totals["usd"] += add(cost2.get("usd"))
                            totals["priced"] &= cost2.get("usd") is not None
                        if sets2:
                            sets_up = sets2
                        else:                                # 다시 못 찾으면 처음 찾은 위치를 돌린 사진 좌표로 옮겨 써요
                            full_up, sets_up, _ = straighten_sheet(full, sets)
                    except UserError:
                        raise
                    except Exception as e:
                        print(f"  ! 회전 보정 후 세트 찾기 실패 - 처음 찾은 위치를 옮겨 써요: {e!r}")
                        full_up, sets_up, _ = straighten_sheet(full, sets)
                for st in sets_reading_order(sets_up):       # 위 줄부터, 같은 줄은 왼쪽부터 (회전 보정한 사진 기준)
                    if SH["skip_incomplete"] and not st.get("complete", True):
                        continue
                    crop = set_crop(full_up, st)
                    if crop is None:
                        continue
                    found += 1
                    price = prices[pi] if pi < len(prices) else None
                    if price:
                        priced.append((st, price))           # 가격 라벨은 회전 보정한 사진 위에
                        pi += 1
                    cr = crop.copy(); cr.thumbnail((model.a.ref_size, model.a.ref_size))
                    buf = io.BytesIO(); cr.save(buf, "PNG")
                    f = cr.width / crop.width
                    out.append((buf.getvalue(), cr, {"pts": [(x * f, y * f) for x, y in crop.info.get("tip_pts", [])],
                                                     "ends": [(e[0] * f, e[1] * f) if e else None
                                                              for e in crop.info.get("tip_ends", [])],
                                                     "bases": [(b[0] * f, b[1] * f) if b else None
                                                               for b in crop.info.get("tip_bases", [])],
                                                     "len": crop.info.get("tip_len", 0) * f,
                                                     "pointed": bool(st.get("pointed")),
                                                     "shape": st.get("shape"), "length": st.get("length"),
                                                     "wids": [w * f for w in crop.info.get("tip_wids", [])],
                                                     "refs": list(crop.info.get("tip_refs", [])),
                                                     **hi_set(hi_src, st, cr)}))
                    info.append((n, f"{n + 1}번 사진 · {found}세트" + (f" · 가격 {price}" if price else "")
                                 + (f" · {st['summary_ko']}" if st.get("summary_ko") else "")))
                if priced:          # 원본 사진(큰 크기)에 세트마다 가격 라벨
                    try:
                        big = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
                        big.thumbnail((2560, 2560))
                        if turn:                             # 세트 위치가 회전 보정한 사진 기준이에요
                            big, _ = rotate_keep(big, turn)
                        ps = price_sheet(big, priced)
                        put("price_sheet", S.BannerPriceSheetEvent(source=n, image=data_url(ps, 92),
                                                                   prices=[p for _, p in priced], sets=found))
                        print(f"  가격표: {n + 1}번 사진 {len(priced)}세트 ({', '.join(p for _, p in priced)})")
                    except Exception as e:
                        print("  ! 가격표 만들기 실패:", repr(e))
                if not found:       # 세트 사진이 아니면 사진 전체를 1개로
                    out.append((raw, d, False))
                    info.append((n, f"{n + 1}번 사진 (세트를 찾지 못해 사진 전체)"))
            if prices and pi < len(prices):
                print(f"  ! 가격 {len(prices)}개 중 {len(prices) - pi}개는 붙일 세트가 없어서 뺐어요")
            dropped = max(0, len(out) - SH["max_sets"])
            out, info = out[:SH["max_sets"]], info[:SH["max_sets"]]
            if SET_UPRIGHT and any(isinstance(w[2], dict) for w in out):
                # 세트마다 luna 가 세트 전체를 보고 손톱 끝 위/아래 · 모양 · 크기 확인 -> 아래면 상하 뒤집기 (동시에)
                from concurrent.futures import ThreadPoolExecutor
                put("sets_stage", S.BannerStageEvent(item=0, step="analyzing", text="세트마다 손톱 끝 방향 · 모양 확인 중..."))

                insp = getattr(model, "inspect_set", None)
                with ThreadPoolExecutor(max_workers=4) as ex:      # ① 세트마다 luna 확인 (아직 뒤집지 않아요)
                    datas = list(ex.map(lambda w: inspect_set_safe(w[1], insp) if isinstance(w[2], dict) else None, out))
                # ② 손톱 끝 위/아래는 업로드 사진 1장마다 한 번만 정해요 (그 사진의 모든 세트 표를 모아서)
                #    -> 같은 사진의 세트는 모두 똑같이 (아래면 전부 상하 거울 뒤집기). 세트마다 따로 뒤집지 않아요
                photo_up = {}
                for n in {src for src, _ in info}:
                    up_v = dn_v = 0
                    for k, w in enumerate(out):
                        if info[k][0] == n and isinstance(w[2], dict):
                            u, d_ = set_tip_votes(w[2], datas[k])
                            up_v += u; dn_v += d_
                    photo_up[n] = up_v >= dn_v
                    print(f"  [손톱 끝 방향] {n + 1}번 사진: 위 {up_v}표 / 아래 {dn_v}표 -> "
                          + ("그대로" if photo_up[n] else "모든 세트를 똑같이 상하 거울 뒤집기"))
                for k, w in enumerate(out):                    # ③ 사진 방향대로 세트마다 같은 처리
                    if not isinstance(w[2], dict):
                        continue
                    try:
                        cr2, h2 = orient_set(w[1], w[2], datas[k], photo_up[info[k][0]])
                    except Exception as e:
                        print(f"  ! 세트 세우기 실패 - 그대로: {e!r}")
                        continue
                    d = datas[k]
                    if d:
                        print(f"  [세트 확인] {k + 1}번 세트: {h2.get('shape')} / {h2.get('length')} · 팁 {len(d['tips'])}개 · "
                              f"기준 {int(d.get('reference') or 0)}번  (luna 가 본 손톱 끝: "
                              f"{'위' if d.get('free_edge_at_top') else '아래'} - {d.get('note_ko', '')})")
                    if cr2 is w[1]:
                        out[k] = (w[0], cr2, h2)
                    else:
                        buf = io.BytesIO(); cr2.save(buf, "PNG")
                        out[k] = (buf.getvalue(), cr2, h2)
            return out, info, dropped

        def run_all():
            work_items = [(raw, d, False) for raw, d in items]
            if split_sets != "on":
                for i in range(len(items)):
                    report.item(i, **{"원본 사진": up_names[i]})
            if split_sets == "on":
                put("sets_stage", S.BannerStageEvent(item=0, step="analyzing", text="세트 나누는 중..."))
                try:
                    work_items, info, dropped = split_all()
                except UserError as e:
                    put("item_error", S.BannerErrorEvent(item=0, status=e.status, code=e.code, message=e.msg))
                    put("done", S.BannerDoneEvent(total=0, ok=0, failed=1, usd=None, krw=None, today=None))
                    q.put(None)
                    return
                if any(isinstance(w[2], dict) for w in work_items):     # 세트마다 팁 자르기 (luna 재확인 포함) - 동시에
                    put("sets_stage", S.BannerStageEvent(item=0, step="analyzing", text="팁 나누고 확인하는 중..."))
                    from concurrent.futures import ThreadPoolExecutor

                    def _pre(iw):
                        i, w = iw
                        run_bind(report, i)
                        t1 = time.time()
                        try:
                            tl = get_tips(w[1], w[2])
                            if tl and hasattr(model, "judge_crops"):     # 자동 평가 ②: 팁 오리기 IoU (luna 심사)
                                report.add_crops(i, model.judge_crops(tl))
                        except Exception as e:
                            print(f"  [경고] 팁 자르기 · 평가 실패: {e!r}")
                        finally:
                            report.item(i, **{"팁 나누기 · 확인 시간(초)": round(time.time() - t1, 1)})
                            run_unbind()
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        list(ex.map(_pre, [(i, w) for i, w in enumerate(work_items) if isinstance(w[2], dict)]))

                def chart_url(w):
                    try:
                        ch = tip_chart(get_tips(w[1], w[2]), korean=True, height=200) if isinstance(w[2], dict) else None
                    except Exception as e:
                        print(f"  [경고] 팁 미리보기 실패: {e!r}")
                        ch = None
                    return data_url(ch, 88) if ch else None
                for i, (n, lab) in enumerate(info):
                    report.item(i, **{"원본 사진": up_names[n] if n < len(up_names) else n + 1, "세트 설명": lab})
                put("sets", S.BannerSetsEvent(total=len(work_items), dropped=dropped, items=[
                    S.BannerSetItem(item=i, source=n, label=lab, thumb=data_url(w[1], 92), is_set=bool(w[2]),
                                    tips_chart=chart_url(w))
                    for i, ((n, lab), w) in enumerate(zip(info, work_items))]))
            print(f"\n배너 요청: {len(work_items)}장" + (f" (사진 {len(items)}장에서 세트 나눔)" if split_sets == "on" else ""))
            threads = [threading.Thread(target=work, args=(i, raw, d, tip), daemon=True)
                       for i, (raw, d, tip) in enumerate(work_items)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            today = model.cost.summary()["today"] if (show_cost and model.cost) else None
            usd = totals["usd"] if (show_cost and totals["priced"]) else None
            krw = usd * model.cost.krw if (usd is not None and model.cost and model.cost.krw) else None
            put("done", S.BannerDoneEvent(total=len(work_items), ok=totals["ok"], failed=totals["failed"], usd=usd, krw=krw,
                                          today=today))
            q.put(None)

        def run_all_report():
            run_bind(report)                      # 세트 나누기 등 '전체' 단계도 보고서에
            try:
                run_all()
            finally:
                run_unbind()
                report.save()                     # reports/실험보고서_<시각>.xlsx + 누적

        threading.Thread(target=run_all_report, daemon=True).start()

        def events():
            yield ": start\n\n"
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                kind, payload = item
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    if os.path.isdir(WEB):
        app.mount("/web", StaticFiles(directory=WEB), name="web")
    return app


def main():
    ap = argparse.ArgumentParser()
    # 옵션을 안 주면 settings.yaml 값을 써요
    ap.add_argument("--port", type=int, default=None, help="settings.yaml server.port")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이/공유기의 다른 기기에서도 접속 허용 (settings.yaml server.lan)")
    ap.add_argument("--model", default=None, help="OpenAI 이미지 모델 (settings.yaml openai.model 보다 우선)")
    ap.add_argument("--effort", default=None, choices=COMPLEXITY,
                    help="그리기 전 구상 강도 = complexity (settings.yaml openai.complexity 보다 우선)")
    ap.add_argument("--hide_cost", action="store_true", help="웹 화면에 비용 숨김 (settings.yaml server.hide_cost)")
    ap.add_argument("--ref_size", type=int, default=None, help="디자인 사진 최대 크기 (settings.yaml request.design_max_size)")
    ap.add_argument("--concurrency", type=int, default=None, help="동시 요청 수 (settings.yaml request.concurrency)")
    ap.add_argument("--timeout", type=float, default=None, help="응답 대기 최대 초 (settings.yaml request.timeout_sec)")
    ap.add_argument("--fake", action="store_true", help="API 없이 화면/마스크만 확인 (요금 없음)")
    a = ap.parse_args()

    s, loaded = load_settings()
    a.settings, a.settings_loaded = s, loaded
    a.port = a.port or int(s["server"]["port"])
    a.lan = a.lan or bool(s["server"]["lan"])
    a.hide_cost = a.hide_cost or bool(s["server"]["hide_cost"])
    a.ref_size = a.ref_size or int(s["request"]["design_max_size"])
    a.concurrency = a.concurrency or int(s["request"]["concurrency"])
    a.timeout = a.timeout or float(s["request"]["timeout_sec"])
    QUALITY.update({512: s["quality"]["fast"], 640: s["quality"]["normal"], 768: s["quality"]["normal"],
                    1024: s["quality"]["high"]})

    model = FakeModel(a) if a.fake else OpenAIModel(a)
    app = build_app(model)
    import uvicorn
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    print(f"\n웹 화면: http://localhost:{a.port}")
    if a.lan:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
            print(f"다른 기기에서: http://{ip}:{a.port}  (같은 공유기에 연결돼 있어야 해요)")
        except Exception:
            pass
    uvicorn.run(app, host=host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
