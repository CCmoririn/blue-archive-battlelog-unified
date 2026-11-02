import sys
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if os.environ.get("BATTLELOG_SKIP_DOTENV") != "1":
    load_dotenv(ROOT / ".env")

LIMITED_PREFIX = "/wMxKzMnDb6Lr"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "Blue_Archive_Battlelog_collect")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "Blue_Archive_Battlelog_collect_public")))

from Blue_Archive_Battlelog_collect_public.app import app as general_app
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware

# 未使用の限定版は、明示的に有効にした場合だけ読み込む。
application = general_app
if os.environ.get("ENABLE_LIMITED_APP", "0").lower() in ("1", "true", "yes"):
    import hmac
    from flask import request, Response
    from battlelog_safety import configure_app
    access_token = os.environ.get("LIMITED_ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("ENABLE_LIMITED_APP requires LIMITED_ACCESS_TOKEN")
    from Blue_Archive_Battlelog_collect.app_limited import limited as limited_blueprint
    limited_app = Flask("limited")
    configure_app(limited_app)
    limited_app.register_blueprint(limited_blueprint, url_prefix="/")

    @limited_app.before_request
    def authorize_limited():
        authorization = request.authorization
        bearer = request.headers.get("Authorization", "")
        provided = authorization.password if authorization and authorization.type == "basic" else ""
        if bearer.startswith("Bearer "):
            provided = bearer[7:]
        if not provided or not hmac.compare_digest(provided.encode(), access_token.encode()):
            return Response("認証が必要です。", 401, {"WWW-Authenticate": 'Basic realm="Limited"'})

    application = DispatcherMiddleware(general_app, {LIMITED_PREFIX: limited_app})

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple('127.0.0.1', int(os.environ.get("PORT", "5000")), application)
