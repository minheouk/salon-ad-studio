"""서빙 오류 계약을 웹 응답이 유지하는지 검증합니다."""

from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest

from src.pipelines.service.backend.model_api import response_error


# HTTP 응답과 같은 헤더 및 본문을 가진 오류를 만듭니다.
def make_error(status, body, retry_after=None):
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return HTTPError("http://model.invalid/edit", status, "failed", headers, BytesIO(body))


# 서빙 오류의 코드, 필드, 메시지와 재시도 시간을 그대로 읽습니다.
def test_nested_error_details():
    error = response_error(make_error(
        429, b'{"error":{"code":"QUEUE_FULL","field":"image","message":"Try again"}}', "10",
    ))

    assert (error.status, error.code, error.field, str(error), error.retry_after) == (
        429, "QUEUE_FULL", "image", "Try again", "10",
    )


# 식별할 수 없는 오류 본문은 기존 기본 메시지로 처리합니다.
@pytest.mark.parametrize("body", [b"bad gateway", b'{"error":null}', b'["error"]'])
def test_invalid_error_body(body):
    error = response_error(make_error(502, body))

    assert (error.status, error.code, error.field) == (502, "MODEL_REQUEST_FAILED", None)


# 입력 필드가 없는 서빙 타임아웃은 웹 타임아웃 코드로 변환합니다.
def test_timeout_mapping():
    error = response_error(make_error(504, b'{"error":{"code":"TIMEOUT","message":"Timed out"}}'))

    assert (error.status, error.code, error.field) == (504, "GENERATION_TIMEOUT", None)
