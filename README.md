# Blue Archive Battlelog Collect - README

---

## 🌟 プロジェクト概要

このプロジェクトは、**ブルーアーカイブ 戦術対抗戦ログ収集アプリ**の全コード・ドキュメントを管理するものです。  
画像アップロード～OCR～スプレッドシート連携～DB検索までを一元化。  
一般公開モード・限定公開モードを**単一のプロジェクトで共存**させる設計です。

---

## 📂 ディレクトリ構成（2025/06/23現在）

Blue-Archive-Battlelog-collect-unified/
├─ Blue_Archive_Battlelog_collect/ # 一般版アプリ本体
│ ├─ app.py
│ ├─ main.py
│ ├─ spreadsheet_manager.py
│ ├─ ocr_processing.py
│ ├─ object_detection.py
│ ├─ defense_suggester.py
│ ├─ config.py
│ ├─ call_gas.py
│ ├─ ...
│ ├─ templates/ # 一般版用Jinja2テンプレート(html)
│ ├─ static/ # 静的ファイル（css, js, 画像）
│
├─ Blue_Archive_Battlelog_collect_public/ # 限定版アプリ本体
│ ├─ app_limited.py
│ ├─ main_limited.py
│ ├─ spreadsheet_manager_limited.py
│ ├─ ocr_processing_limited.py
│ ├─ object_detection_limited.py
│ ├─ defense_suggester_limited.py
│ ├─ config_limited.py
│ ├─ call_gas_limited.py
│ ├─ ...
│ ├─ templates/ # 限定版用テンプレート（limited/ サブフォルダで分岐）
│ ├─ static/
│
├─ cache_general/ # 一般版キャッシュ
├─ cache_limited/ # 限定版キャッシュ
├─ uploads/ # 画像一時保存（root共有/旧仕様）
├─ .env # 環境変数ファイル（git管理外・機密）
├─ call_gas.py # GAS呼び出し（ルート直下/補助用）
├─ call_gas_limited.py # GAS呼び出し（限定用/補助用）
├─ manage.py # サーバ管理・一括起動スクリプト等
├─ requirements.txt # Python依存パッケージ
├─ credentials.json
├─ .gitignore
└─ README.md # （本ファイル・方針/構成ドキュメント）

---

## 📝 **運用と開発の絶対ルール**

### 1. **一般/限定はサブディレクトリで厳密に分割**
- `Blue_Archive_Battlelog_collect/`：一般公開用（誰でもアクセスOK、検索・アップロードなど基本機能のみ）
- `Blue_Archive_Battlelog_collect_public/`：限定公開用（サークル・仲間向け、内部統計・高機能検索・内部データ）

### 2. **各バージョンに対応した個別ファイルを維持**
- app.py、main.py、spreadsheet_manager.py など**一般/限定で必ずファイルを分ける**
- GAS・設定・キャッシュも同様に `_limited` サフィックス付きで区別

### 3. **テンプレート/静的ファイルも同様に分岐**
- `/templates` 配下で `limited/` サブディレクトリを用意し、限定版専用htmlを分離
- CSS/JSも必要に応じて`limited_`接頭/接尾を付与して切り分け

### 4. **プロジェクト全体に関わる共通ファイルは直下配置**
- manage.py, requirements.txt, .env などは**root直下**  
- これらは「どちらにも依存しない」or「環境全体に関わる」もの

---

## 🚩 **設計ポリシー・開発方針**

- **全ソース・設定は常に「どの機能が一般/限定どちら向けか」明確に分けて運用**
- 変更時・新規追加時は、**必ず両バージョンで差異が反映されているか確認**
- テンプレートやAPIの「限定用URL」「限定用キャッシュ」「限定GAS呼び出し」など、必ず専用のものを利用
- **「README.md＝最高権威の運用ガイド」**として随時更新・記録（例：分岐理由、仕様差分、今後のTODOなども記載）

---

## 📋 **サンプル：新機能追加時の手順**

1. 仕様検討（一般/限定のどちら向け？両対応？を必ず明記）
2. **両ディレクトリで必要なファイルを新規作成 or 片方に追記**
3. テンプレートは一般→`/templates/`、限定→`/templates/limited/`で管理
4. config、環境変数、キャッシュなども**一般/限定で別名を徹底**
5. README.mdの「ディレクトリ構成」「運用ルール」「設計方針」を**最新に必ず追記**

---

## 🏷️ **備考（運用Tips）**

- 開発者が moririn（ご主人）である限り、**「自分の頭の整理・未来の自分/イルAIへの引き継ぎ」**が主目的
- 万が一複数人開発になった場合も、README.mdとサブディレクトリを最優先で参照すること
- **定期的にディレクトリ・ファイル構成の「実態」とこのREADMEがズレていないか確認すること**

---

*最終更新: 2025/06/23（イル作成）*
