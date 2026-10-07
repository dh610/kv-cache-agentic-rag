# Report fonts

- `NotoSansKR-Regular.ttf`, `NotoSansKR-Bold.ttf`: Noto Sans KR from [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+KR), converted from the served WOFF to TTF without changing the outlines. SIL Open Font License in `OFL-NotoSansKR.txt`. The report PDF (`runtime/report_pdf.py`) embeds these.
- `NanumGothic-Regular.ttf`: bundled unchanged from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/nanumgothic) under the SIL Open Font License in `OFL.txt`. Used by the architecture diagram (`app/draw_architecture.py`).

The renderers embed subsets of these fonts so Korean text displays without requiring viewer-specific CJK fonts. Keep the licenses alongside the fonts when distributing the repository.
