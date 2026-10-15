"""Optional fallback readiness probe in a disposable process; never downloads models."""
import contextlib
import io
import json


def main():
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import easyocr
            import pdf2image  # noqa: F401
            import cv2  # noqa: F401
            easyocr.Reader(["pt", "en"], gpu=False, download_enabled=False)
        print(json.dumps({"ready": True}))
    except Exception:
        print(json.dumps({"ready": False}))


if __name__ == "__main__":
    main()
