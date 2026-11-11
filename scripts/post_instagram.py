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


def delete_media(mid):
    """인스타 글 1개를 지운다. 성공하면 True. (권한이 없거나 실패하면 False, 멈추지 않음)"""
    try:
        r = requests.delete(f"{GRAPH}/{mid}", params={"access_token": TOKEN}, timeout=60)
        if r.ok:
            return True
        print(f"자동 삭제 실패: {mid} {r.status_code} {r.text[:300]}")
    except Exception as e:
        print(f"자동 삭제 실패: {mid} {e}")
    return False


def watch_duplicates(caption, media_id):
    """올린 뒤 1·3·5·10·15·30분에 인스타를 본다.
    로봇이 올린 글(media_id) 말고 같은 첫 줄 글이 '나중에' 또 생기면(11/6~11/11에 메타 쪽에서 생긴 복제)
    먼저 번호·시각·주소를 기록하고, 그 나중 글만 지운다. 로봇이 올린 글은 절대 지우지 않는다.
    지우지 못하면 True를 돌려줘서 실행을 빨갛게 만든다(코치님이 직접 지우도록)."""
    first = caption.strip().splitlines()[0].strip() if caption.strip() else ""
    if not first:
        return False
    t_post = datetime.datetime.now(datetime.timezone.utc)
    waited = 0
    problem = False
    deleted = set()
    for minutes in (1, 3, 5, 10, 15, 30):
        time.sleep(minutes * 60 - waited)  # 인스타가 목록에 반영할 시간
        waited = minutes * 60
        try:
            r = requests.get(
                f"{GRAPH}/{USER_ID}/media",
                params={"fields": "id,caption,timestamp,permalink,media_type,media_product_type", "limit": 25, "access_token": TOKEN},
                timeout=60,
            )
            if not r.ok:
                print(f"중복 확인({minutes}분 뒤) 실패(무시): {r.status_code} {r.text[:200]}")
                continue
            now = datetime.datetime.now(datetime.timezone.utc)
            same = []
            for m in r.json().get("data", []):
                ts = datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
                cap = (m.get("caption") or "").strip()
                if cap.splitlines()[:1] == [first] and (now - ts).total_seconds() < 86400:
                    m["_ts"] = ts
                    same.append(m)
            ids = [m["id"] for m in same]
            if media_id not in ids:
                print(f"::warning::로봇이 올린 글({media_id})이 목록에 아직 안 보여요({minutes}분 뒤). 이번엔 아무것도 지우지 않아요.")
                continue
            extra = [m for m in same if m["id"] != media_id and m["id"] not in deleted]
            if not extra:
                print(f"중복 확인 완료({minutes}분 뒤): 같은 글은 1개뿐이에요.")
                continue
            for m in extra:
                gap = (m["_ts"] - t_post).total_seconds()
                info = (f"{m['id']} ({m['timestamp']}, 로봇이 올린 시각보다 {gap:+.0f}초) "
                        f"{m.get('permalink','')} [{m.get('media_type')}/{m.get('media_product_type')}]")
                print(f"::warning::같은 글이 하나 더 생겼어요({minutes}분 뒤 확인). 로봇 글: {media_id}. 더 생긴 글: {info}")
                # 로봇이 올리기 전부터 있던 글이면 사람이 올린 것일 수 있으니 건드리지 않는다.
                if gap < -30:
                    print(f"::error::이 글은 로봇보다 먼저 올라온 글이라 자동으로 지우지 않아요: {m['id']}")
                    problem = True
                    deleted.add(m["id"])
                    continue
                if delete_media(m["id"]):
                    print(f"더 생긴 글을 자동으로 지웠어요: {m['id']} (로봇 글 {media_id}는 그대로)")
                else:
                    print(f"::error::더 생긴 글을 지우지 못했어요. 인스타에서 직접 지워 주세요: {m.get('permalink','')} (나중에 올라온 글)")
                    problem = True
                deleted.add(m["id"])
        except Exception as e:
            print(f"중복 확인({minutes}분 뒤) 실패(무시): {e}")
    return problem


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
    if watch_duplicates(caption, media_id):
        sys.exit(1)  # 기록은 이미 저장했어요. 직접 지워야 할 글이 있으면 실행을 빨갛게 만들어 알려 줘요.


if __name__ == "__main__":
    main()
