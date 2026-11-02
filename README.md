# BlueArchive 対抗戦DB

ブルーアーカイブの戦術対抗戦について、結果画像からログを収集し、相手編成への勝利例を検索する非公式Webアプリです。

画像をアップロードすると、OpenCVによる前処理とGoogle Cloud Visionによる文字認識を行います。利用者が結果を確認・修正して確定した後、Googleスプレッドシートへ書き込み、GASで変換します。検索にはシーズン別のJSONキャッシュを使います。

## 構成

| パス | 役割 |
| --- | --- |
| `Blue_Archive_Battlelog_collect_public/` | 一般公開版のFlaskアプリ、画面、静的ファイル |
| `Blue_Archive_Battlelog_collect/` | 限定版。既定では起動対象外 |
| `manage.py` | 一般版の起動と、必要な場合の限定版の接続 |
| `battlelog_safety.py` | 入力確認、画像の保存、ロック、JSONの安全な書き込み |
| `battlelog_storage.py` | 一般版・限定版のシートとキャッシュの操作 |
| `battlelog_submission.py` | 確認トークンと登録処理の状態管理 |
| `battlelog_web.py` | アップロード・登録確定の受付 |
| `battlelog_gas.py` | GAS呼び出し。import時には実行しない |
| `tools/maintenance.py` | キャッシュと処理記録の確認、明示的なキャッシュ更新 |
| `tests/` | 外部通信を行わない回帰テスト |
| `docs/maintenance.md` | 設定、検証、復旧の手順と未確認の前提 |

以前のREADMEでは、一般版と限定版のフォルダ説明が逆になっていました。上の表が現行コードの対応です。

## 維持する検索・認識の仕様

- 防衛側を指定すると、その防衛に攻撃側が勝ったログを返します。攻撃側を指定すると、その攻撃を防衛側が防いだログを返します。
- STRIKERは指定した枠で照合し、SPECIALは順不同で照合します。空欄は条件にしません。
- STRIKERの射程・遮蔽条件でも検索できます。
- 一般版と限定版は、シート・GAS・キャッシュ・URLの対応を確認して扱います。フォルダ名や `source` ラベルだけでは公開範囲を判断しません。
- OCRの複数回呼び出しは、調整を重ねた認識方法の一部です。回数だけを理由に統合・削除しません。2026年11月2日の改修でも、画像前処理・切り出し・OCR・解析の本体は変更していません。
- 防衛メーカーは、射程・遮蔽の並びを集計して各枠の生徒を選びます。評価値を、完成した6人編成そのものの予測勝率とは扱いません。

## 起動・検証

依存関係は `requirements.txt`、Pythonの指定は `runtime.txt` を参照してください。ルートからのWSGI起動先は `manage:application` です。Renderの既存設定が自動でこの起動先へ変わるわけではありません。

テストには本番の認証情報は不要です。

```text
python -B -m unittest discover -s tests -v
node tests/test_frontend.js
```

キャッシュや画像、処理記録、認証情報はGit管理外です。コードの履歴だけではデータを復元できません。本番へ反映する前に、[保守・検証・復旧の手順](docs/maintenance.md)に従い、Renderの保存先、実シート、GAS、バックアップを確認してください。
