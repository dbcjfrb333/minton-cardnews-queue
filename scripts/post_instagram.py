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
DRY = os.environ.get("DRY_RUN") == "true"  # 연습 모드: 인스타에 '올리기'만 안 한다
if DRY and not date.startswith("test"):
    sys.exit("연습 모드는 test 로 시작하는 시험 폴더에서만 쓸 수 있어요. (진짜 폴더 보호)")


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


def push_safely():
    """main이 먼저 바뀌었어도 합쳐서 올린다 (3번까지 재시도)."""
    for i in range(3):
        if subprocess.run(["git", "push", "origin", "HEAD:main"]).returncode == 0:
            return
        run("git", "pull", "--rebase", "origin", "main")
        time.sleep(2 * (i + 1))
    sys.exit("git push 실패")


def already_on_instagram(caption):
    """인스타에 같은 글이 최근 3일 안에 있으면 그 글 id를 돌려준다.
    확인이 안 되면(3번 시도) 중복 위험이 있으니 올리지 않고 멈춘다."""
    first = caption.strip().splitlines()[0].strip() if caption.strip() else ""
    last_err = ""
    for i in range(3):
        try:
            r = requests.get(
                f"{GRAPH}/{USER_ID}/media",
                params={"fields": "id,caption,timestamp", "limit": 25, "access_token": TOKEN},
                timeout=60,
            )
            if r.ok:
                now = datetime.datetime.now(datetime.timezone.utc)
                for m in r.json().get("data", []):
                    ts = datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
                    cap = (m.get("caption") or "").strip()
                    same = cap == caption or (first and cap.splitlines()[:1] == [first])
                    if same and (now - ts).days < 3:
                        return m["id"]
                return None
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(3 * (i + 1))
    sys.exit("인스타 최근 글을 확인하지 못해서 중복 방지를 위해 올리지 않고 멈춰요. "
             f"(다음 확인 때 다시 시도해요) 이유: {last_err}")


def warn_if_duplicate(caption, media_id):
    """올린 뒤 같은 글이 인스타에 2개 이상 보이면 빨간 경고를 남긴다. (자동 삭제는 못 해요)
    확인 자체가 안 돼도 이미 올린 뒤라 멈추지 않는다."""
    first = caption.strip().splitlines()[0].strip() if caption.strip() else ""
    if not first:
        return False
    time.sleep(60)  # 인스타가 목록에 반영할 시간
    try:
        r = requests.get(
            f"{GRAPH}/{USER_ID}/media",
            params={"fields": "id,caption,timestamp,permalink,media_type,media_product_type,is_comment_enabled", "limit": 25, "access_token": TOKEN},
            timeout=60,
        )
        if not r.ok:
            print(f"중복 확인 실패(무시): {r.status_code} {r.text[:200]}")
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        same = []
        for m in r.json().get("data", []):
            ts = datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
            cap = (m.get("caption") or "").strip()
            if cap.splitlines()[:1] == [first] and (now - ts).total_seconds() < 86400:
                same.append(m)
        if len(same) > 1:
            lines = ", ".join(f"{m['id']} ({m['timestamp']}) {m.get('permalink','')} [{m.get('media_type')}/{m.get('media_product_type')}]" for m in same)
            print(f"::error::같은 글이 인스타에 {len(same)}개 있어요! 방금 올린 글: {media_id}. 목록: {lines}")
            return True
        print("중복 확인 완료: 같은 글은 1개뿐이에요.")
    except Exception as e:
        print(f"중복 확인 실패(무시): {e}")
    return False


def mark_posted(media_id):
    (folder / ".posted").write_text(media_id + "\n")
    run("git", "add", str(folder / ".posted"))
    run("git", "commit", "-m", f"posted {date}")
    push_safely()


def acquire_lock():
    """'지금 올리는 중' 표시를 먼저 올린다. 푸시는 한 명만 성공하므로 한 번에 한 로봇만 통과한다."""
    lock = folder / ".posting"
    lock.write_text(datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
    run("git", "add", str(lock))
    run("git", "commit", "-m", f"lock {date}")
    if subprocess.run(["git", "push", "origin", "HEAD:main"]).returncode != 0:
        run("git", "fetch", "origin", "main")
        run("git", "reset", "--hard", "origin/main")
        print("다른 로봇이 먼저 자물쇠를 잡았어요 → 나는 멈춰요.")
        return False
    return True


def release_lock():
    """올리기 전에 실패했을 때만 자물쇠를 풀어 다음 확인 때 다시 시도할 수 있게 한다."""
    try:
        run("git", "rm", "-q", str(folder / ".posting"))
        run("git", "commit", "-m", f"unlock {date}")
        push_safely()
    except BaseException as e:
        print(f"자물쇠 풀기 실패(수동 확인 필요): {e}")


def set_git_name():
    """git 이름표를 맨 처음에 한 번 달아 둔다. (없으면 '올림' 표시를 저장할 때 멈춘다)"""
    run("git", "config", "user.name", "cardnews-bot")
    run("git", "config", "user.email", "cardnews-bot@users.noreply.github.com")


def main():
    set_git_name()
    # 대기하다 늦게 시작한 실행이 옛날 상태를 보지 않도록 최신 main으로 맞춘다.
    run("git", "fetch", "origin", "main")
    run("git", "reset", "--hard", "origin/main")
    if not folder.is_dir():
        print(f"{folder} 없음 → 오늘은 올릴 게시물이 없어요.")
        return
    if (folder / ".posted").exists():
        print(f"{folder} 는 이미 게시됨 → 건너뜀.")
        return
    lock = folder / ".posting"
    if lock.exists():
        t = datetime.datetime.fromisoformat(lock.read_text().strip())
        age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
        if age < 1800:
            print("다른 로봇이 지금 올리는 중 → 건너뜀.")
            return
        sys.exit("이전 실행이 올리는 도중 멈췄어요. 인스타를 확인하고, 안 올라갔으면 "
                 f"{lock} 파일을 지워 주세요. (중복 방지를 위해 자동으로 다시 올리지 않아요)")

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

    found = already_on_instagram(caption)
    if found:
        print(f"인스타에 같은 글이 이미 있어요({found}) → 올리지 않고 기록만 해요.")
        mark_posted(found)
        return

    if not acquire_lock():
        return

    try:
        # 인스타 API는 JPEG만 받으므로 변환해서 _jpg 폴더에 저장 후 푸시
        jpg_dir = folder / "_jpg"
        jpg_dir.mkdir(exist_ok=True)
        jpgs = []
        for i, img in enumerate(images, 1):
            out = jpg_dir / f"{i:02d}.jpg"
            Image.open(img).convert("RGB").save(out, "JPEG", quality=95)
            jpgs.append(out)

        run("git", "add", str(jpg_dir))
        run("git", "commit", "-m", f"convert {date} images to jpeg")
        push_safely()
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
    except BaseException:
        release_lock()  # 아직 인스타에 올리기 전이라 다시 시도해도 안전해요
        raise

    if DRY:
        print("연습 모드: 여기까지 성공! 인스타에 올리기는 하지 않았어요. (자물쇠는 시험을 위해 남겨 둠)")
        return

    # 여기부터는 결과가 불확실할 수 있으므로 실패해도 자물쇠를 풀지 않는다 (중복 방지).
    t0 = datetime.datetime.now(datetime.timezone.utc)
    print(f"올리기 명령 보냄(1번뿐): {t0.isoformat()} 컨테이너 {carousel}")
    media_id = api("POST", f"{USER_ID}/media_publish", creation_id=carousel)["id"]
    t1 = datetime.datetime.now(datetime.timezone.utc)
    print(f"게시 완료: {media_id} (응답까지 {(t1 - t0).total_seconds():.1f}초, {t1.isoformat()})")
    mark_posted(media_id)
    if warn_if_duplicate(caption, media_id):
        sys.exit(1)  # 기록은 이미 저장했어요. 실행을 빨갛게 만들어 알려 줘요.


if __name__ == "__main__":
    main()
