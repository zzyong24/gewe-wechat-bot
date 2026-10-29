"""发图链路单测（全 mock：不上传、不打 gewe）。真实验证见 README「发图」一节。"""
import sys, tempfile, json
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app, imagehost, gewe_client
from router import Reply

png = Path(tempfile.mkdtemp()) / "a.png"
png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
cfg = Path(tempfile.mkdtemp()) / "relay.json"
cfg.write_text(json.dumps({"server_url": "http://relay.example:8787", "secret": "S3CRET"}))

class Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d

with mock.patch.object(imagehost, "CONFIG", cfg), \
     mock.patch("imagehost.requests.post", return_value=Resp(200, {"path": "/img/abc.png"})) as up, \
     mock.patch.object(gewe_client, "post_image", return_value={"newMsgId": 1}) as pi, \
     mock.patch.object(gewe_client, "post_text") as pt:
    app.default_sender(Reply("20000000002@chatroom", "文字版", image_path=str(png)))
    assert up.call_args.args[0] == "http://relay.example:8787/upload/S3CRET"
    pi.assert_called_once_with("20000000002@chatroom", "http://relay.example:8787/img/abc.png")
    pt.assert_not_called()

with mock.patch.object(imagehost, "CONFIG", cfg), \
     mock.patch("imagehost.requests.post", return_value=Resp(500, {})), \
     mock.patch.object(gewe_client, "post_image") as pi, mock.patch.object(gewe_client, "post_text") as pt:
    app.default_sender(Reply("20000000002@chatroom", "文字版", image_path=str(png)))   # 上传失败 → 降级文字
    pi.assert_not_called(); pt.assert_called_once_with("20000000002@chatroom", "文字版", None)
    try:
        imagehost.upload(png)
    except imagehost.ImageHostError as e:
        assert "S3CRET" not in str(e)
print("发图链路单测通过 ✅")
