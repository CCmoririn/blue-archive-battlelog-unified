import os
import sys
import json
import requests
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

# --- シーズン名をコマンドライン引数から取得（なければ's9'） ---
season = sys.argv[1] if len(sys.argv) > 1 else "s9"

# 環境変数 'credentials' にサービスアカウントキー JSON 全文を設定している前提
credentials_content = os.environ.get("credentials")
if not credentials_content:
    raise Exception("Environment variable 'credentials' is not set or empty.")

# 一時ファイルに書き出し
credentials_path = "/tmp/google_credentials.json"
with open(credentials_path, "w") as f:
    f.write(credentials_content)

# サービスアカウント認証情報の生成
SCOPES = ['https://www.googleapis.com/auth/script.external_request']
creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)

# トークンを更新（必要な場合）
if not creds.valid or creds.expired:
    creds.refresh(Request())
access_token = creds.token

# Apps Script WebアプリのエンドポイントURL（環境変数から取得）
script_url = os.environ.get("GAS_SCRIPT_URL")
if not script_url:
    raise Exception("Environment variable 'GAS_SCRIPT_URL' is not set or empty.")

# --- シーズン名をパラメータに追加してGASへ送信 ---
payload = {
    'function': 'main',
    'parameters': [{
        'parameter': {
            'season': season
        }
    }]
}

# GAS側（doPost/main）で「e.parameter.season」で受け取れる形
# 互換性重視で"parameters"キー配列の最初に辞書を入れているが、
# GAS側main(e)の e.parameter.season で取得可

headers = {
    'Authorization': f'Bearer {access_token}',
    'Content-Type': 'application/json',
}

response = requests.post(script_url, headers=headers, json=payload)
if response.status_code != 200:
    raise Exception(f"Error calling Apps Script: {response.status_code} {response.text}")

print("Apps Script 実行結果：", response.text)
