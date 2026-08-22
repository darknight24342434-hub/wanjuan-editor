from __future__ import annotations

import base64
import io
import unittest
import zipfile

from src.importer import import_document


class ImporterTests(unittest.TestCase):
    def test_imports_utf8_text(self) -> None:
        encoded = base64.b64encode("第一段。\n\n第二段。".encode("utf-8")).decode("ascii")
        result = import_document("測試稿件.md", encoded)
        self.assertEqual(result["title"], "測試稿件")
        self.assertIn("第二段", result["content"])

    def test_imports_docx_paragraphs(self) -> None:
        xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>標題</w:t></w:r></w:p><w:p><w:r><w:t>正文內容</w:t></w:r></w:p>
</w:body></w:document>'''.encode("utf-8")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", xml)
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        result = import_document("新聞.docx", encoded)
        self.assertEqual(result["content"], "標題\n\n正文內容")

    def test_rejects_unsupported_file(self) -> None:
        encoded = base64.b64encode(b"data").decode("ascii")
        with self.assertRaises(ValueError):
            import_document("稿件.pdf", encoded)


if __name__ == "__main__":
    unittest.main()
