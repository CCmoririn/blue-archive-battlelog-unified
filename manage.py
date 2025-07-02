import sys
import os

LIMITED_PREFIX = "/wMxKzMnDb6Lr"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "Blue_Archive_Battlelog_collect")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "Blue_Archive_Battlelog_collect_public")))

from Blue_Archive_Battlelog_collect.app_limited import limited as limited_blueprint
from Blue_Archive_Battlelog_collect_public.app import app as general_app
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware

# --- 限定アプリ側のFlaskインスタンスを作成 ---
limited_app = Flask("limited")
limited_app.register_blueprint(limited_blueprint, url_prefix="/")  # Blueprint側でurl_prefixは使わない！

# DispatcherMiddlewareに登録
application = DispatcherMiddleware(
    general_app,
    {
        LIMITED_PREFIX: limited_app,  # ここにFlaskインスタンスを入れる
    }
)

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    run_simple('0.0.0.0', 5000, application, use_debugger=True, use_reloader=True)
