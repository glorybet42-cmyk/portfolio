import os
import re
import shutil
import subprocess

import pytesseract
from PIL import Image
import pymupdf

# ========== ПУТЬ К TESSERACT (Linux) ==========
_tess = shutil.which("tesseract") or "/usr/bin/tesseract"
pytesseract.pytesseract.tesseract_cmd = _tess

# ========== ЛЕНИВАЯ ЗАГРУЗКА EASYOCR ==========
_easyocr_reader = None

def get_easyocr():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        print("Загружаю EasyOCR...")
        _easyocr_reader = easyocr.Reader(['ru', 'en'], gpu=False)
        print("EasyOCR загружен")
    return _easyocr_reader

def _count_russian_letters(text):
    return len(re.findall(r'[А-Яа-яЁё]', text))

def extract_text_from_image_easyocr(image_path):
    reader = get_easyocr()
    results = reader.readtext(image_path, detail=0, paragraph=True)
    return '\n'.join(results)

def extract_text_from_image_tesseract(image_path, lang='rus+eng'):
    image = Image.open(image_path)
    return pytesseract.image_to_string(image, lang=lang)

def extract_text_from_image(image_path, lang='rus+eng'):
    text_easy = ""
    score_easy = -1
    try:
        text_easy = extract_text_from_image_easyocr(image_path)
        score_easy = _count_russian_letters(text_easy)
    except Exception as e:
        print(f"EasyOCR не сработал: {e}")

    try:
        text_tess = extract_text_from_image_tesseract(image_path, lang)
        score_tess = _count_russian_letters(text_tess)
    except Exception as e:
        print(f"Tesseract не сработал: {e}")
        text_tess = ""
        score_tess = -1

    if score_easy < 0 and score_tess < 0:
        return ""
    return text_easy if score_easy > score_tess else text_tess

def extract_text_from_scanned_pdf(pdf_path, lang='rus+eng', dpi=300):
    full_text = []

    try:
        doc = pymupdf.open(pdf_path)
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = pytesseract.image_to_string(img, lang=lang)
            full_text.append(text)
        return "\n".join(full_text)
    except Exception as e:
        print(f"pymupdf не смог: {e}")

    try:
        import tempfile
        from pdf2image import convert_from_path
        with tempfile.TemporaryDirectory() as tmp:
            images = convert_from_path(pdf_path, dpi=dpi, output_folder=tmp)
            for img in images:
                text = pytesseract.image_to_string(img, lang=lang)
                full_text.append(text)
        return "\n".join(full_text)
    except Exception as e:
        print(f"pdf2image тоже не смог: {e}")
        return ""

if __name__ == "__main__":
    print(f"tesseract: {_tess}")
    try:
        print(f"версия: {pytesseract.get_tesseract_version()}")
        langs = pytesseract.get_languages(config='')
        print(f"языки: {langs}")
        print("rus:", "rus" in langs, "| eng:", "eng" in langs)
    except Exception as e:
        print(f"Ошибка tesseract: {e}")
