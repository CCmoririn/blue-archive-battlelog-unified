"""Create initial admin credentials outside the repository; never print secrets."""
import argparse
import json
import secrets
from pathlib import Path
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    files = (destination / "admin-login.txt", destination / "admin-render-settings.json")
    if any(path.exists() for path in files):
        raise RuntimeError("Credential files already exist; refusing to rotate or overwrite")
    password = secrets.token_urlsafe(24)
    settings = {"BATTLELOG_ADMIN_PASSWORD_HASH": generate_password_hash(password, method="pbkdf2:sha256:600000"),
                "BATTLELOG_ADMIN_SESSION_KEY": secrets.token_urlsafe(48)}
    with files[0].open("x", encoding="utf-8") as stream:
        stream.write("対抗戦DB 管理画面\nhttps://bluearchive-battlelog-p.com/admin/\n\n管理用パスワード：\n" + password +
                     "\n\nこのファイルは公開・共有しないでください。ログインは12時間で期限が切れます。\n")
    with files[1].open("x", encoding="utf-8") as stream:
        json.dump(settings, stream, indent=2)
    print("Admin login and Render settings saved. No secrets printed.")


if __name__ == "__main__":
    main()
