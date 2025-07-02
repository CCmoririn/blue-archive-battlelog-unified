from ultralytics import YOLO
import cv2

def detect_objects(image_path, model_path):
    """
    YOLOv8 を使って画像認識を行う関数
    :param image_path: 画像のファイルパス
    :param model_path: YOLOv8の学習済みモデル（.ptファイル）
    :return: 検出されたオブジェクトのリスト
    """
    model = YOLO(model_path)
    image = cv2.imread(image_path)
    results = model(image)
    
    detected_labels = []
    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls)
            label = model.names.get(cls_id, f"ID_{cls_id}")
            detected_labels.append(label)

    return list(set(detected_labels))

# テスト用の実行
if __name__ == "__main__":
    image_path = "uploads/battle.jpg"  # 認識したい画像を指定
    model_path = "yolov8_custom.pt"  # YOLOのモデルファイル
    detected_objects = detect_objects(image_path, model_path)
    print("検出されたオブジェクト:", detected_objects)
