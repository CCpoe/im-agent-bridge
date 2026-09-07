from types import SimpleNamespace

import pytest

from lark_client.card_service import CardService


@pytest.mark.asyncio
async def test_upload_image_uses_lark_message_image_api(tmp_path):
    captured = []

    class ImageAPI:
        def create(self, request):
            captured.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(image_key="img_v3_uploaded"),
            )

    path = tmp_path / "preview.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    service = CardService.__new__(CardService)
    service.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(image=ImageAPI())))

    assert await service.upload_image(path) == "img_v3_uploaded"
    assert len(captured) == 1
    assert captured[0].request_body.image_type == "message"


@pytest.mark.asyncio
async def test_upload_image_rejects_non_image_without_calling_lark(tmp_path):
    called = False

    class ImageAPI:
        def create(self, request):
            nonlocal called
            called = True
            raise AssertionError("invalid files must not reach Lark")

    path = tmp_path / "not-image.png"
    path.write_text("plain text", encoding="utf-8")
    service = CardService.__new__(CardService)
    service.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(image=ImageAPI())))

    assert await service.upload_image(path) is None
    assert called is False
