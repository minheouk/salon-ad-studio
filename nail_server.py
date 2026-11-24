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
    "banner": {"size": "1536x1024", "max_designs": 5, "plan_model": "", "plan_reasoning_effort": "low",
               "text_on_image": True, "brand": "", "pose": "auto", "apply_mode": "mask",
               "scene_banner": False},
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
    "No text, no watermark, no extra fingers, no rings or jewelry."
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
    "No extra hands or fingers, no rings or jewelry, no watermark, no logos. "
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
    "banners, captions, watermarks or logos anywhere in the image. No extra fingers."
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
    "Find every set. For each set list the CENTER of each tip in order from LEFT to RIGHT as seen in the image "
    "(for a vertical column: top to bottom), as normalized 0-1 coordinates of the whole image (x right, y down). "
    "Do not reorder by finger - just left to right. " + CLEAR_SEE_EN + " A tip's extent always includes its clear "
    "parts (e.g. a clear free edge with a bow on it), so the CENTER and tip_length must cover the whole tip from "
    "its very end to its base, clear parts included. tip_length = the average length of one tip (normalized to image "
    "height), complete = false only if a tip of the set is cut off by the image edge so badly that its design cannot be "
    "seen, or the set has fewer than 4 visible tips (slight overlap with neighbouring tips is fine). "
    "summary_ko: short Korean description of the set (max 30 characters). Ignore text, logos, inset photos of "
    "hands, and stray single tips. Order the sets top to bottom, then left to right. If the image is not a "
    "collection of nail tip sets (e.g. one hand, one design), set is_tip_sheet=false and sets=[]."
)
_PT = {"type": "object", "additionalProperties": False, "required": ["x", "y"],
       "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}
SPLIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["is_tip_sheet", "sets"],
    "properties": {"is_tip_sheet": {"type": "boolean"},
                   "sets": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["tips", "tip_length", "complete", "summary_ko"],
                       "properties": {"tips": {"type": "array", "items": _PT}, "tip_length": {"type": "number"},
                                      "complete": {"type": "boolean"}, "summary_ko": {"type": "string"}}}}},
}
# 팁 방향 규칙 (고정): 팁 하나에서 긴 쪽 = 손톱의 위아래(길이) 축, 그 긴 축의 위쪽 끝 = 손톱 바깥쪽(끝, free edge)
TIP_ORIENT_EN = (
    "Orientation rule for every tip (fixed, never guess otherwise): the LONGER dimension of each tip in the photo is "
    "the nail's length axis (cuticle to free edge), and the UPPER end of that long axis is the OUTER end of the nail "
    "(the free edge, toward the fingertip); the lower end is the cuticle/base."
)
TIPSET_INSTRUCTIONS = (
    " This photo is ONE set of press-on nail tips cut out of a larger sheet: the sharp, non-faded row in the "
    "middle (the faded surroundings are other sets - ignore them). tips_en: describe EACH tip of the sharp row "
    "separately, in order from the LEFTMOST tip to the RIGHTMOST tip as seen in the photo (for a vertical row: top "
    "to bottom), one entry per tip (usually 5), each 8-30 English words with its colors, pattern, decorations and "
    "finish, so a painter can reproduce every tip exactly. " + TIP_ORIENT_EN + " Describe every tip in that "
    "orientation: say what is at the free edge (top) and what is at the cuticle (bottom), e.g. 'free edge: gold "
    "glitter; cuticle half: nude'. " + CLEAR_SEE_EN + " For EVERY tip state its clear part explicitly with its "
    "position and rough share of the length, e.g. 'free edge 40%: clear transparent with a brown 3D bow at the "
    "boundary; cuticle 60%: milky pink with brown dots' - or write 'no clear part'. Do not name fingers. "
    "design_en = the common style of the "
    "set in a few sentences; accent_nails = ''. nail_shape and nail_length describe the tips. best_region = the "
    "whole sharp row."
)
# 세트 규칙 (통일): 한 줄의 팁을 왼쪽부터 1~5 번이라 하면
#   왼손·오른손 모두 1번 = 엄지, 2번 = 검지, 3번 = 중지, 4번 = 약지, 5번 = 새끼 (손이 사진에서 어느 쪽을 향하든 같아요)
FINGERS = ("thumb", "index finger", "middle finger", "ring finger", "little finger")
HAND_SIDE_INSTRUCTIONS = (
    "The image is a contact sheet of {n} numbered photos, each showing a hand (sometimes holding a drink or an "
    "object). For every photo decide whether the main, most visible hand is a LEFT or a RIGHT hand (anatomically: "
    "use the thumb position together with whether the palm or the back of the hand faces the camera). If BOTH a "
    "left and a right hand with visible fingernails appear in the photo, answer BOTH. Answer for all {n} numbers."
)
HAND_SIDE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["hands"],
    "properties": {"hands": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["number", "side"],
        "properties": {"number": {"type": "integer"}, "side": {"type": "string", "enum": ["left", "right", "both"]}}}}},
}


def finger_assign(analysis, side):
    """팁 세트 인식 결과(tips_en, 왼쪽부터) + 손(left/right) -> [(손가락, 팁 설명, 팁 번호)]. 세트가 아니면 [].
    side == 'both' 이면 오른손 배정을 돌려줘요 (양손은 finger_text/finger_ko 에서 손마다 따로 써요)."""
    tips = [str(t).strip() for t in ((analysis or {}).get("tips_en") or []) if str(t).strip()]
    if not (analysis or {}).get("tipset") or not tips:
        return []
    # 왼손·오른손 모두 같은 규칙: 1번(왼쪽 끝) = 엄지 ... 5번(오른쪽 끝) = 새끼
    return [(FINGERS[k], tips[k], k + 1) for k in range(min(5, len(tips)))]


def _finger_list(fa):
    return "; ".join(f"{f} = tip {i}: {t.rstrip('.')}" for f, t, i in fa)


def finger_text(analysis, side, first_tip_image=None, n_tip_images=0):
    """이미지 모델에 보낼 손가락별 지시 (세트가 아니면 빈 문자열). 양손(both)이면 같은 줄을 손마다 전부 적용.
    first_tip_image: 팁을 하나씩 잘라 함께 보낼 때 1번 팁 사진의 이미지 번호 (그 사진들이 정답, 글은 참고)"""
    fa = finger_assign(analysis, "right" if side == "both" else side)
    if not fa:
        return ""
    if first_tip_image and n_tip_images:
        fa = fa[:n_tip_images]
        imgs = ", ".join(f"image {first_tip_image + i - 1} = tip {i}" for _, _, i in fa)
        lst = "; ".join(f"{f} = tip {i} = image {first_tip_image + i - 1} (text hint: {t.rstrip('.')})" for f, t, i in fa)
        head = (f" Image 2 is the whole set of press-on nail tips, numbered 1 to {len(fa)} from left to right, and "
                f"each tip is also shown alone, enlarged: {imgs}. THE TIP IMAGES ARE THE GROUND TRUTH: each finger's "
                "nail must look exactly like its own tip image - the same base color and translucency, the same "
                "pattern, the same decorations (3D charms, bows, beads, stickers, dots, stars) in the same positions "
                "and sizes, the same finish. Copy what you SEE in the tip image; the short text hints may be wrong - "
                "if a text hint and the image disagree, follow the image. Do not add, remove, simplify or replace "
                "anything (no glitter, beads or dots that are not in the image). " + TIP_ORIENT_EN + " So the top end "
                "of each tip image goes to the free edge at the fingertip and the bottom end to the cuticle - never "
                "upside down." + CLEAR_PAINT_EN + " ")
        if side == "both":
            return (head + "Image 1 shows BOTH a left hand and a right hand. Apply the WHOLE set to EACH hand "
                    "separately with the same finger mapping (identify each finger anatomically): " + lst + ".")
        return (head + f"The hand in image 1 is a {side or 'right'} hand. Identify each finger anatomically (not by "
                "its position in the photo) and put the tips on exactly like this (each finger has its own design - "
                "do not repeat one design on every nail): " + lst + ".")
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


def set_crop(img, st):
    """세트 1개(팁 중심점들) -> 그 줄만 또렷하고 나머지는 흐리게(하얗게) 한 잘라낸 사진. 줄을 못 만들면 None."""
    import math
    from PIL import Image, ImageDraw, ImageFilter
    W, H = img.size
    pts = [(max(0.0, min(1.0, float(t["x"]))) * W, max(0.0, min(1.0, float(t["y"]))) * H) for t in st.get("tips", [])]
    if len(pts) < 2:
        return None
    L = max(0.03, min(0.5, float(st.get("tip_length") or 0.1))) * H
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
    out.info["tip_pts"] = [(x - bx[0], y - bx[1]) for x, y in pts]      # 팁을 하나씩 잘라 손가락별 참고 사진으로 써요
    out.info["tip_len"] = L
    return out


def tip_crops(img, hints, max_tips=5):
    """세트 사진 + 팁 중심점들(왼쪽부터) -> 팁 하나씩 잘라낸 사진 목록 (손가락별 참고 사진). 못 자르면 []."""
    from PIL import Image
    pts = list((hints or {}).get("pts") or [])[:max_tips]
    L = float((hints or {}).get("len") or 0)
    if len(pts) < 2 or L <= 4:
        return []
    import math
    W, H = img.size
    ang = abs(math.atan2(pts[-1][1] - pts[0][1], pts[-1][0] - pts[0][0]))      # 줄 방향 (0 = 가로줄)
    tilt = min(1.0, 1.6 * abs(math.sin(ang)))     # 대각선 줄이면 팁도 기울어져 있어서 가로로 넓게
    out = []
    for k, (x, y) in enumerate(pts):
        gaps = [((pts[j][0] - x) ** 2 + (pts[j][1] - y) ** 2) ** 0.5 for j in (k - 1, k + 1) if 0 <= j < len(pts)]
        hw = max(0.36 * L, 0.56 * min(gaps) if gaps else 0.4 * L, 0.62 * L * tilt)
        hh = 0.62 * L
        box = (int(max(0, x - hw)), int(max(0, y - hh)), int(min(W, x + hw)), int(min(H, y + hh)))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return []
        t = img.crop(box)
        if t.height < 384:          # 작으면 키워서 보내요 (장식이 뭉개지지 않게)
            t = t.resize((max(1, round(t.width * 384 / t.height)), 384), Image.LANCZOS)
        out.append(t)
    return out


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
            shown = None if usd is None else usd + (an_usd or 0)      # 이번 결과 비용 = 생성 + (이 사진 첫 사용이면) 인식
            info = {**t, "model": model, "quality": quality, "complexity": complexity or "",
                    "usd": shown, "krw": shown * self.krw if shown is not None and self.krw else None,
                    "image_usd": image_usd, "analysis_usd": an_usd if analysis else None,
                    "priced": p is not None, "today": self.summary()["today"]}
        return info

    def record_analysis_only(self, an, hand, label="(인식만, 생성 안 함)"):
        """인식만 하고 생성은 안 한 경우(네일 없는 사진 등)도 비용은 나가니 기록해요. 배너 계획서 비용도 여기로."""
        usd = an.get("usd")
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
        self.sides = {}
        if da["enabled"]:
            for folder, pool, sheet_fn in ((HANDS, list_hands(), hand_contact_sheet), (NAILHAND, self.scene_hands, scene_contact_sheet)):
                try:
                    self.sides.update(self._load_sides(folder, pool, sheet_fn))
                except Exception as e:
                    print(f"! {os.path.basename(folder)} 왼손/오른손 확인 실패 (오른손으로 봐요): {type(e).__name__}: {e}")
        b = a.settings["banner"]
        print(f"배너: {b['size']} · 계획서 {b['plan_model'] or da['model']} · 최대 {b['max_designs']}장"
              + (" · 글자 넣기" if b["text_on_image"] else " · 글자 없음"))
        print(f"지금까지 사용: 오늘 {s['today']['count']}장 {fmt_cost(s['today']['usd'], s['today']['krw'])}"
              f" · 전체 {s['total']['count']}장 {fmt_cost(s['total']['usd'], s['total']['krw'])}")

    def hand_side(self, hand_id):
        """손 사진이 왼손인지 오른손인지 양손인지 ('left' | 'right' | 'both'). 모르면 오른손."""
        return self.sides.get(hand_id) or self.sides.get(os.path.basename(str(hand_id))) or "right"

    def _load_sides(self, folder, pool, sheet_fn):
        """<폴더>/hand_side.json 에서 읽고, 없는 사진만 인식 모델로 한 번 확인해 저장해요 (직접 고쳐도 돼요)."""
        if not pool or not os.path.isdir(folder):
            return {}
        path = os.path.join(folder, "hand_side.json")
        saved = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                saved = {k: v for k, v in json.load(f).items() if v in ("left", "right", "both")}
        missing = [h for h in pool if h["id"] not in saved]
        if missing:
            sheet = sheet_fn(pool)
            buf = io.BytesIO(); sheet.save(buf, "JPEG", quality=88)
            model = self.a.settings["design_analysis"]["model"]
            kw = dict(model=model, instructions=HAND_SIDE_INSTRUCTIONS.format(n=len(pool)), max_output_tokens=6000,
                      input=[{"role": "user", "content": [
                          {"type": "input_text", "text": "Left or right hand for each numbered photo?"},
                          {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                           "detail": "high"}]}],
                      text={"format": {"type": "json_schema", "name": "hand_sides", "schema": HAND_SIDE_SCHEMA, "strict": True}})
            r = self._responses(kw, model, "medium")
            got = {int(x["number"]): x["side"] for x in json.loads(r.output_text)["hands"]}
            for i, h in enumerate(pool):
                if h["id"] not in saved and got.get(i + 1) in ("left", "right", "both"):
                    saved[h["id"]] = got[i + 1]
            cost = self.cost.text_cost(getattr(r, "usage", None), model)
            self.cost.record_analysis_only(cost, "(왼손/오른손 확인)", label="(왼손/오른손 확인)")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saved, f, ensure_ascii=False, indent=1)
            print(f"  {os.path.basename(folder)}: 왼손/오른손 확인해서 hand_side.json 에 저장 ({len(missing)}장)")
        n_left = sum(1 for h in pool if saved.get(h["id"]) == "left")
        n_both = sum(1 for h in pool if saved.get(h["id"]) == "both")
        print(f"{os.path.basename(folder)} 손: 오른손 {len(pool) - n_left - n_both}장 · 왼손 {n_left}장 · 양손 {n_both}장"
              " (hand_side.json)")
        return saved

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

    def analyze_design(self, raw_bytes, design, effort=None, scene=False, tipset=False):
        """-> (생성에 쓸 디자인 참고 이미지, 인식 결과 dict 또는 None). 같은 사진은 다시 돈 내지 않게 기억해요.
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
            return self._analyze(key, design, cfg, fresh=mine)
        finally:
            if mine:
                with self.analysis_lock:
                    self.analysis_pending.pop(key).set()

    def _analyze(self, key, design, cfg, fresh):
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
                       nail_length="auto", nail_shape="auto", tip_images=None):
        args = (scene, design, desc, size, scene_id, analysis, on_partial, nail_length, nail_shape, tip_images)
        try:
            return self._generate_scene(*args)
        except _RetryFixedSize:
            self.no_flex_size = True
            return self._generate_scene(*args)

    def _generate_scene(self, scene, design, desc, size, scene_id, analysis, on_partial, nail_length, nail_shape,
                        tip_images=None):
        from PIL import Image
        R = self.a.settings["request"]
        an = analysis or {}
        cat = next((s["category"] for s in self.scene_hands if s["id"] == scene_id), scene_id.split("/")[0])
        obj, drink_word = scene_object(cat)
        n_len, n_shape, _, _ = nail_plan(analysis, nail_length, nail_shape, self.a.settings)
        if n_len in ("keep", "unknown") and n_shape in ("unknown",):
            nails = "Keep each nail's current length and shape."
        else:
            nails = (f"Give every nail a {n_len.replace('_', ' ') if n_len not in ('keep', 'unknown') else 'natural'} "
                     f"length and {n_shape if n_shape != 'unknown' else 'natural'} shape with a "
                     f"{NAIL_TIP_EN.get(n_shape, 'natural')} free edge, and show the WHOLE design along the full nail "
                     f"length; nails stay fully inside the picture.")
        tips = list(tip_images or []) if finger_assign(an, "right") else []
        if tips:            # 세트: 팁 사진이 정답이에요. 틀릴 수 있는 전체 요약 글은 보내지 않아요
            nails += finger_text(an, self.hand_side(scene_id), first_tip_image=3, n_tip_images=len(tips))
        else:
            if an.get("has_nails"):
                nails += f" The nail design in image 2: {an['design_en']}" \
                    + (f" Accent nails: {an['accent_nails']}." if an.get("accent_nails") else "") \
                    + (f" Finish: {an['finish']}." if an.get("finish") else "")
            nails += finger_text(an, self.hand_side(scene_id))
        drink =(an.get("scene_drink_en") or "").strip() or "a color that harmonizes with the new nail colors"
        bg = (an.get("scene_background_en") or "").strip() or \
            "a new softly blurred background whose colors and mood match the new nail design"
        prompt = SCENE_PROMPT.format(obj=obj, drink_word=drink_word, drink=drink.rstrip("."), bg=bg.rstrip("."),
                                     nails=nails) \
            + (f" Extra request from the customer (highest priority): {desc.strip()[:300]}." if desc and desc.strip() else "")
        W, H = scene.size
        if self.flex_size():
            L = int(R["canvas_long_side"]) * 5 // 4          # 장면 전체를 그리니 조금 크게 (손톱이 작아지지 않게)
            k = L / max(W, H)
            cw, ch = max(256, round(W * k / 16) * 16), max(256, round(H * k / 16) * 16)
        else:
            cw, ch = canvas_size(W, H)
        canvas = scene.resize((cw, ch), Image.LANCZOS)
        ref = design.copy(); ref.thumbnail((1024, 1024), Image.LANCZOS)
        stream = bool(on_partial) and int(R["partial_images"]) > 0
        kw = dict(model=self.model, prompt=prompt, n=1, size=f"{cw}x{ch}", quality=QUALITY.get(int(size or 768), "medium"),
                  image=[("scene.png", png_bytes(canvas), "image/png"), ("design.png", png_bytes(ref), "image/png")]
                  + [(f"tip{k + 1}.png", png_bytes(t), "image/png") for k, t in enumerate(tips)])
        self._image_options(kw, stream)

        def preview(b64, idx):
            try:
                on_partial(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), idx)
            except Exception as e:
                print("  미리보기 실패:", e)

        b64, usage, ms = self._image_call(kw, preview if stream else None)
        out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        tag = os.path.splitext(os.path.basename(scene_id))[0]
        if self.a.settings["debug"]["save_images"]:
            save_debug("scene_" + tag, {"0_design_ref": ref, "1_sent_scene": canvas, "5_result": out,
                                        **{f"0_tip{k + 1}": t for k, t in enumerate(tips)}},
                       text=json.dumps({"prompt": prompt, "analysis": analysis}, ensure_ascii=False, indent=1, default=str))
        if analysis:
            entry = self.analysis_cache.get(analysis.get("key"), {})
            first = entry.pop("unbilled", False)
            analysis = dict(analysis, billed_usd=(analysis.get("usd") or 0.0) if first else 0.0)
        info = self.cost.record(usage, self.model, kw["quality"], kw.get("complexity"), f"{cw}x{ch}", ms / 1000,
                                "scene:" + tag, analysis=analysis)
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
                  "or fingers, no rings or jewelry, no watermark, no logos. "
        elif repose:      # 자세 바꾸기: '조금만 옮기기' 문장 대신 새 자세 + 손톱 하나하나 그대로 옮기기
            head = BANNER_PROMPT.split("Keep the hand")[0].format(w=w, h=h, orient=orientation(w, h)) \
                + BANNER_REPOSE.replace("{pose}", str(plan["pose_en"]).strip().rstrip(".")) \
                + "Photo-real, with natural shadows that match the new scene. No extra hands or fingers, no rings or " \
                  "jewelry, no watermark, no logos. "
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
        out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
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

    def analyze_design(self, raw_bytes, design, effort=None, scene=False, tipset=False):
        return design, None

    def hand_side(self, hand_id):
        return "right"

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
        return out, 500, {"usd": 0.0, "image_usd": 0.0, "today": {"count": 0, "usd": 0.0, "krw": 0},
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

        def work(i, raw, d, tipset=False):
            stage = lambda step, text: put("item_stage", S.BannerStageEvent(item=i, step=step, text=text))
            try:
                # 1) 디자인 인식 + 손 고르기 (인식 모델 1번 호출)
                stage("analyzing", "디자인 인식 · 손 고르는 중...")
                ref, an = model.analyze_design(raw, d, effort=analysis_effort, scene=scene, tipset=bool(tipset))
                hid = (an or {}).get("suggested_hand")
                auto = hid in hand_by_id
                hand = hand_by_id[hid] if auto else hands_all[i % len(hands_all)]
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
                        tip_images=tip_crops(d, tipset) if isinstance(tipset, dict) else None)
                    if not with_banner:    # 장면 사진이 완성품 -> ③④ 건너뜀
                        data, mime = encode_result(img)
                        item_usd = info.get("usd")
                        with lock:
                            totals["ok"] += 1
                            totals["usd"] += add(item_usd)
                            totals["priced"] &= item_usd is not None
                        put("item_banner", S.BannerResultEvent(
                            item=i, image=f"data:{mime};base64," + base64.b64encode(data).decode(), mime=mime,
                            width=img.width, height=img.height, ms=int(ms), item_usd=item_usd if show_cost else None))
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
                put("item_banner", S.BannerResultEvent(
                    item=i, image=f"data:{mime};base64," + base64.b64encode(data).decode(), mime=mime,
                    width=ban.width, height=ban.height, ms=int(ms2), item_usd=item_usd if show_cost else None))
            except UserError as e:
                print(f"배너 {i + 1}번 실패 [{e.code}]: {e.msg}")
                with lock:
                    totals["failed"] += 1
                put("item_error", S.BannerErrorEvent(item=i, status=e.status, code=e.code, message=e.msg))
            except Exception as e:
                print(f"배너 {i + 1}번 오류:", repr(e))
                with lock:
                    totals["failed"] += 1
                put("item_error", S.BannerErrorEvent(item=i, status=500, code="INTERNAL_ERROR",
                                                     message=f"만드는 중 오류가 났어요: {type(e).__name__}"))

        def split_all():
            """⓪ 사진마다 세트를 찾아 세트 하나 = 만들 사진 하나로 펼쳐요 -> [(raw, 잘라낸 사진, tipset)]"""
            SH = model.a.settings["sheet"]
            out, info = [], []
            for n, (raw, d) in enumerate(items):
                try:
                    full = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
                    full.thumbnail((1536, 1536))
                    sets, cost = model.split_sheet(full)
                    with lock:
                        totals["usd"] += add(cost.get("usd"))
                        totals["priced"] &= cost.get("usd") is not None
                except UserError:
                    raise
                except Exception as e:
                    print("세트 나누기 오류:", repr(e))
                    sets = []
                found = 0
                for st in sets:
                    if SH["skip_incomplete"] and not st.get("complete", True):
                        continue
                    crop = set_crop(full, st)
                    if crop is None:
                        continue
                    found += 1
                    cr = crop.copy(); cr.thumbnail((model.a.ref_size, model.a.ref_size))
                    buf = io.BytesIO(); cr.save(buf, "PNG")
                    f = cr.width / crop.width
                    out.append((buf.getvalue(), cr, {"pts": [(x * f, y * f) for x, y in crop.info.get("tip_pts", [])],
                                                     "len": crop.info.get("tip_len", 0) * f}))
                    info.append((n, f"{n + 1}번 사진 · {found}세트" + (f" · {st['summary_ko']}" if st.get("summary_ko") else "")))
                if not found:       # 세트 사진이 아니면 사진 전체를 1개로
                    out.append((raw, d, False))
                    info.append((n, f"{n + 1}번 사진 (세트를 찾지 못해 사진 전체)"))
            dropped = max(0, len(out) - SH["max_sets"])
            return out[:SH["max_sets"]], info[:SH["max_sets"]], dropped

        def run_all():
            work_items = [(raw, d, False) for raw, d in items]
            if split_sets == "on":
                put("sets_stage", S.BannerStageEvent(item=0, step="analyzing", text="세트 나누는 중..."))
                try:
                    work_items, info, dropped = split_all()
                except UserError as e:
                    put("item_error", S.BannerErrorEvent(item=0, status=e.status, code=e.code, message=e.msg))
                    put("done", S.BannerDoneEvent(total=0, ok=0, failed=1, usd=None, krw=None, today=None))
                    q.put(None)
                    return
                put("sets", S.BannerSetsEvent(total=len(work_items), dropped=dropped, items=[
                    S.BannerSetItem(item=i, source=n, label=lab, thumb=data_url(w[1], 92), is_set=bool(w[2]))
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

        threading.Thread(target=run_all, daemon=True).start()

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
