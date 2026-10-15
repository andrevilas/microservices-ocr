"""Single-file multipart streaming, without framework spooling or full-body buffering."""
from python_multipart.multipart import MultipartParser, parse_options_header
from python_multipart.exceptions import MultipartParseError

from app.application import ApplicationError
from app.services.upload_service import filename_checked


class MultipartUpload:
    def __init__(self, content_type: str, session):
        media, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if media != b"multipart/form-data" or not 1 <= len(boundary) <= 200:
            raise ApplicationError(415, "unsupported_media_type", "Send one PDF in multipart field 'file'.")
        self.session, self.parts, self.ended = session, 0, False
        self.headers, self.field, self.value = {}, bytearray(), bytearray()
        self.header_bytes = 0
        self.parser = MultipartParser(boundary, max_header_size=8192, max_header_count=8, callbacks={
            "on_part_begin": self.part_begin, "on_header_field": self.header_field,
            "on_header_value": self.header_value, "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
            "on_part_data": lambda data, start, end: self.session.write(memoryview(data)[start:end]),
            "on_end": self.end,
        })

    def part_begin(self):
        self.parts += 1
        if self.parts != 1:
            raise ApplicationError(422, "invalid_multipart", "Exactly one file part is required.")

    def _header(self, target, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 8192:
            raise ApplicationError(413, "headers_too_large", "Multipart headers exceed 8 KiB.")
        target.extend(memoryview(data)[start:end])

    def header_field(self, data, start, end):
        self._header(self.field, data, start, end)

    def header_value(self, data, start, end):
        self._header(self.value, data, start, end)

    def header_end(self):
        key = bytes(self.field).lower()
        if key in self.headers:
            raise ApplicationError(422, "invalid_multipart", "Duplicate multipart header.")
        self.headers[key] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def headers_finished(self):
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or options.get(b"name") != b"file" or b"filename" not in options:
            raise ApplicationError(422, "invalid_multipart", "Exactly one file part named 'file' is required.")
        if self.headers.get(b"content-type", b"application/octet-stream") not in (b"application/pdf", b"application/octet-stream"):
            raise ApplicationError(415, "unsupported_media_type", "Only PDF content is accepted.")
        try:
            self.session.filename = filename_checked(options[b"filename"].decode("utf-8"))
        except UnicodeDecodeError:
            raise ApplicationError(422, "invalid_filename", "Filename must be UTF-8.") from None

    def end(self):
        self.ended = True

    def write(self, data):
        try:
            self.parser.write(data)
        except MultipartParseError as error:
            # The pinned parser can reject before delivering the header callback.
            if str(error) == "Maximum header size exceeded":
                raise ApplicationError(413, "headers_too_large", "Multipart headers exceed 8 KiB.") from None
            raise

    def finish(self):
        self.parser.finalize()
        if self.parts != 1 or not self.ended:
            raise ApplicationError(422, "invalid_multipart", "Multipart body is incomplete.")
