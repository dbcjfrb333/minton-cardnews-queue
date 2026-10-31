"""queue/<날짜>/ 폴더의 카드뉴스를 인스타그램 캐러셀로 게시한다.

폴더 구성: 이미지 2~10장(png/jpg) + cardnews_caption.txt
- 폴더가 없으면 아무것도 하지 않고 종료
- 이미 게시했으면(.posted 파일 존재) 다시 올리지 않음
"""
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

GRAPH = os.environ.get("IG_GRAPH_BASE", "https://graph.facebook.com/v21.0")
USER_ID = os.environ["IG_USER_ID"]
TOKEN = os.environ["IG_ACCESS_TOKEN"]
REPO = os.environ["REPO"]

date = os.environ.get("TARGET_DATE") or datetime.datetime.now(
    ZoneInfo("Asia/Seoul")
).strftime("%Y-%m-%d")
folder = Path("queue") / date


def run(*cmd):
    subprocess.run(cmd, check=True)


def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{GRAPH}/{path}", data=params, timeout=60)
    if not r.ok:
        sys.exit(f"Instagram API 오류 {r.status_code}: {r.text}")
    return r.json()


def wait_finished(container_id):
    for _ in range(30):
        r = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code", "access_token": TOKEN},
            timeout=60,
        ).json()
        status = r.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            sys.exit(f"컨테이너 처리 실패: {r}")
        time.sleep(5)
    sys.exit("컨테이너 처리 시간 초과")


def main():
    if not folder.is_dir():
        print(f"{folder} 없음 → 오늘은 올릴 게시물이 없어요.")
        return
    if (folder / ".posted").exists():
        print(f"{folder} 는 이미 게시됨 → 건너뜀.")
        return

    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
    )
    if not 2 <= len(images) <= 10:
        sys.exit(f"이미지는 2~10장이어야 해요 (현재 {len(images)}장)")

    caption_file = folder / "cardnews_caption.txt"
    caption = caption_file.read_text(encoding="utf-8").strip() if caption_file.exists() else ""
    if len(caption) > 2200:
        sys.exit(f"캡션이 너무 길어요 ({len(caption)}자, 최대 2200자)")

    # 인스타 API는 JPEG만 받으므로 변환해서 _jpg 폴더에 저장 후 푸시
    jpg_dir = folder / "_jpg"
    jpg_dir.mkdir(exist_ok=True)
    jpgs = []
    for i, img in enumerate(images, 1):
        out = jpg_dir / f"{i:02d}.jpg"
        Image.open(img).convert("RGB").save(out, "JPEG", quality=95)
        jpgs.append(out)

    run("git", "config", "user.name", "cardnews-bot")
    run("git", "config", "user.email", "cardnews-bot@users.noreply.github.com")
    run("git", "add", str(jpg_dir))
    run("git", "commit", "-m", f"convert {date} images to jpeg")
    run("git", "push")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    urls = [f"https://raw.githubusercontent.com/{REPO}/{sha}/{p.as_posix()}" for p in jpgs]
    for u in urls:  # raw URL이 반영될 때까지 대기
        for _ in range(12):
            if requests.head(u, timeout=30).status_code == 200:
                break
            time.sleep(5)
        else:
            sys.exit(f"이미지 URL 접근 불가: {u} (저장소가 Public인지 확인)")

    children = []
    for u in urls:
        c = api("POST", f"{USER_ID}/media", image_url=u, is_carousel_item="true")["id"]
        wait_finished(c)
        children.append(c)

    carousel = api(
        "POST", f"{USER_ID}/media",
        media_type="CAROUSEL", children=",".join(children), caption=caption,
    )["id"]
    wait_finished(carousel)
    media_id = api("POST", f"{USER_ID}/media_publish", creation_id=carousel)["id"]
    print(f"게시 완료: {media_id}")

    (folder / ".posted").write_text(media_id + "\n")
    run("git", "add", str(folder / ".posted"))
    run("git", "commit", "-m", f"posted {date}")
    run("git", "push")


if __name__ == "__main__":
    main()
