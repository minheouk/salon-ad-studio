"""단건 이미지 수정 결과의 다운로드 계약을 검증합니다."""

from unittest.mock import Mock

import pytest

from src.pipelines.service.backend.model_api import PNG_SIGNATURE, ModelAPI, ModelError


# 서빙이 반환한 중첩 URL로 PNG를 즉시 내려받습니다.
def test_edit_downloads_nested_result_url(monkeypatch):
    model = ModelAPI("http://model.invalid")
    revised = PNG_SIGNATURE + b"revised"
    fetch = Mock(side_effect=[
        ("application/json", b'{"result":{"url":"/files/revised.png?signature=test"},"timing_ms":{}}'),
        ("image/png", revised),
    ])
    monkeypatch.setattr(model, "fetch", fetch)

    assert model.edit(b"source", "공통 지시", "개별 지시") == revised
    submit, download = (call.args[0] for call in fetch.call_args_list)
    assert (submit.get_method(), submit.full_url) == ("POST", "http://model.invalid/edit")
    assert (download.get_method(), download.full_url) == (
        "GET", "http://model.invalid/files/revised.png?signature=test",
    )


# 이전 임시 형식과 URL이 누락된 응답은 다운로드하지 않습니다.
@pytest.mark.parametrize("body", [b'{"url":"/old.png"}', b'{"result":{}}'])
def test_edit_rejects_missing_nested_url(monkeypatch, body):
    model = ModelAPI("http://model.invalid")
    fetch = Mock(return_value=("application/json", body))
    monkeypatch.setattr(model, "fetch", fetch)

    with pytest.raises(ModelError) as error:
        model.edit(b"source", "공통 지시", "")

    assert error.value.code == "INVALID_MODEL_RESPONSE"
    assert fetch.call_count == 1
