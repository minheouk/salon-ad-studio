"""모델 실험 페이지를 제공하고 OpenAI 이미지 생성·수정 요청을 처리합니다."""

import argparse
import binascii
import json
import os
import shlex
from base64 import b64decode
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVICE = Path(__file__).resolve().parents[1]
ENV_FILE = Path(__file__).resolve().parents[4] / ".env"
API_PATH = "/api/model-lab/generate"
API_ROOT = "https://api.openai.com/v1/images"
DEFAULT_MODEL = "gpt-image-2.6-flare"
SELECTABLE_IMAGE_MODELS = ("gpt-image-1-mini", "gpt-image-2")
MAX_BODY = 32 * 1024 * 1024
MAX_IMAGE_URL = 20 * 1024 * 1024
MAX_IMAGES = 16
IMAGE_PREFIXES = tuple(f"data:image/{kind};base64," for kind in ("png", "jpeg"))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
STATIC_FILES = {
    "/shared/model-lab-files.js": ("model_lab/shared/model-lab-files.js", "text/javascript; charset=utf-8"),
    "/model-lab/": ("model_lab/web/index.html", "text/html; charset=utf-8"),
    "/model-lab/app.js": ("model_lab/web/app.js", "text/javascript; charset=utf-8"),
    "/model-lab/model-client.js": ("model_lab/web/model-client.js", "text/javascript; charset=utf-8"),
    "/model-lab/styles.css": ("model_lab/web/styles.css", "text/css; charset=utf-8"),
}


# 셸 환경에 키가 없으면 저장소의 .env에서 해당 값만 읽습니다.
def load_api_key() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().removeprefix("export ").partition("=")
            if name.strip() == "OPENAI_API_KEY":
                parts = shlex.split(value, comments=True)
                if parts:
                    return parts[0]
    raise ValueError("환경에 OPENAI_API_KEY가 설정되어 있지 않습니다.")


# 브라우저 입력을 확인하고 수정 대상과 참고 이미지의 순서를 정합니다.
def request_inputs(body: object) -> tuple[str, list[str]]:
    if not isinstance(body, dict):
        raise ValueError("요청 본문은 JSON 객체여야 합니다.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 32_000:
        raise ValueError("프롬프트를 1~32,000자로 입력해 주세요.")

    attachments = body.get("additionalImages", [])
    previous = body.get("resultImage")
    if not isinstance(attachments, list):
        raise ValueError("추가 이미지는 배열이어야 합니다.")
    images = attachments if previous is None else [previous, *attachments]
    if len(images) > MAX_IMAGES:
        raise ValueError("직전 결과를 포함해 이미지를 최대 16장까지 보낼 수 있습니다.")
    for image in images:
        if not isinstance(image, str) or not image.startswith(IMAGE_PREFIXES):
            raise ValueError("JPG와 PNG 이미지만 보낼 수 있습니다.")
        if len(image) > MAX_IMAGE_URL:
            raise ValueError("첨부 이미지가 너무 큽니다. 이미지 크기를 줄여 주세요.")
    return prompt.strip(), images


# 이미지가 있으면 편집 API를 호출하며, 결과를 PNG 바이트로 변환합니다.
def generate_image(
    api_key: str, prompt: str, images: list[str], model: str | None = None
) -> bytes:
    payload = {
        "model": model or os.environ.get("OPENAI_IMAGE_MODEL", DEFAULT_MODEL),
        "prompt": prompt,
        "quality": "low",
        "size": "1024x1024",
        "output_format": "png",
    }
    action = "generations"
    if images:
        action = "edits"
        payload["images"] = [{"image_url": image} for image in images]

    request = Request(
        f"{API_ROOT}/{action}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        body = json.load(response)
    image = b64decode(body["data"][0]["b64_json"], validate=True)
    if not image.startswith(PNG_SIGNATURE):
        raise ValueError("OpenAI 응답에 PNG 이미지가 없습니다.")
    return image


class ModelLabHandler(BaseHTTPRequestHandler):
    """로컬에서 모델 실험 파일과 생성 API를 같은 주소로 제공합니다."""

    api_key: str

    # 응답의 콘텐츠 종류와 크기를 설정합니다.
    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # 브라우저에 표시할 오류 메시지를 JSON으로 보냅니다.
    def fail(self, status: int, message: str) -> None:
        self.respond(status, "application/json", json.dumps({"message": message}).encode())

    # 실험 화면 실행에 필요한 파일만 제공합니다.
    def do_GET(self) -> None:
        asset = STATIC_FILES.get(self.path)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        self.respond(HTTPStatus.OK, content_type, (SERVICE / filename).read_bytes())

    # 실험 요청의 크기와 형식을 확인합니다.
    def read_inputs(self) -> tuple[str, list[str], str | None]:
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_BODY:
            raise ValueError("요청은 최대 32 MiB까지 보낼 수 있습니다.")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("JSON 요청만 보낼 수 있습니다.")
        body = json.loads(self.rfile.read(length))
        prompt, images = request_inputs(body)
        model = body.get("imageModel", "default")
        if model == "default":
            return prompt, images, None
        if model not in SELECTABLE_IMAGE_MODELS:
            raise ValueError("사용할 이미지 생성 모델을 선택해 주세요.")
        return prompt, images, model

    # 입력 오류와 모델 호출 실패를 구분해 결과를 반환합니다.
    def do_POST(self) -> None:
        if self.path != API_PATH:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            prompt, images, model = self.read_inputs()
        except ValueError as error:
            self.fail(HTTPStatus.BAD_REQUEST, str(error))
            return

        try:
            image = generate_image(self.api_key, prompt, images, model)
        except HTTPError as error:
            self.log_error("OpenAI 응답 코드 %s, 요청 ID %s", error.code, error.headers.get("x-request-id"))
            self.fail(HTTPStatus.BAD_GATEWAY, f"OpenAI 이미지 요청이 실패했습니다 (HTTP {error.code}).")
            return
        except (URLError, TimeoutError):
            self.log_error("OpenAI 이미지 요청 연결 실패")
            self.fail(HTTPStatus.BAD_GATEWAY, "OpenAI에 연결하지 못했습니다. 다시 시도해 주세요.")
            return
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error):
            self.log_error("OpenAI 이미지 응답 형식 오류")
            self.fail(HTTPStatus.BAD_GATEWAY, "OpenAI에서 이미지 결과를 읽지 못했습니다.")
            return
        self.respond(HTTPStatus.OK, "image/png", image)


# 팀 작업 규칙에 맞게 루프백 주소에서만 서버를 시작합니다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8516)
    args = parser.parse_args()
    try:
        ModelLabHandler.api_key = load_api_key()
    except ValueError:
        parser.error("저장소 .env 또는 서버 환경에 유효한 OPENAI_API_KEY를 설정해 주세요.")

    with ThreadingHTTPServer(("127.0.0.1", args.port), ModelLabHandler) as server:
        print(f"http://127.0.0.1:{args.port}/model-lab/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
