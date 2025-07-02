from google.cloud import vision

def perform_google_vision_ocr(image_path):
    """
    Google Cloud Vision OCR を使って画像から文字認識を行う
    :param image_path: 画像ファイルのパス
    :return: 認識されたテキスト（文字列）
    """
    client = vision.ImageAnnotatorClient()
    with open(image_path, 'rb') as image_file:
        content = image_file.read()
    image = vision.Image(content=content)
    
    response = client.text_detection(image=image)
    texts = response.text_annotations
    if texts:
        return texts[0].description.strip()
    else:
        return ""

# テスト用の実行
if __name__ == "__main__":
    image_path = "uploads/battle.jpg"  # OCRをかける画像
    ocr_text = perform_google_vision_ocr(image_path)
    print("OCRで認識されたテキスト:", ocr_text)
