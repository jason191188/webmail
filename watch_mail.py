"""새 메일 감시: 주기적으로 POP3 를 확인해 새 메일마다 요약/답장 초안을 만들고 알림을 띄운다.

- 메일을 삭제하거나 발송하지 않는다.
- 처음 실행할 때 이미 있던 메일은 '확인한 것'으로만 기록하고 처리하지 않는다.
- 결과는 drafts 폴더에 메일 1건당 텍스트 파일 1개로 저장한다.

사용법:
    python watch_mail.py            계속 감시 (Ctrl+C 로 종료)
    python watch_mail.py --once     한 번만 확인하고 종료
"""
import json
import os
import re
import subprocess
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import mailai
import mailconn

warnings.simplefilter("ignore", DeprecationWarning)
sys.stdout.reconfigure(encoding="utf-8")

INTERVAL_SEC = 300          # 확인 주기 (5분)
MAX_PER_CYCLE = 10          # 한 번에 처리할 최대 메일 수 (나머지는 다음 주기에)

BASE = Path(__file__).parent
SEEN_PATH = BASE / "seen_uids.json"
DRAFT_DIR = BASE / "drafts"

BALLOON_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(10000, $env:MAIL_TITLE, $env:MAIL_TEXT, 'Info')
Start-Sleep -Seconds 8
$n.Dispose()
"""


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title, text):
    """Windows 알림을 띄운다. 실패해도 감시는 계속한다."""
    env = dict(os.environ, MAIL_TITLE=title[:60], MAIL_TEXT=text[:240] or " ")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", BALLOON_PS],
            env=env, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log(f"알림 실패: {e}")


def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return None                      # None = 첫 실행


def save_seen(seen):
    SEEN_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def summary_of(result):
    m = re.search(r"\[요약\]\s*(.*?)(?=\n\[|\Z)", result, re.S)
    return (m.group(1) if m else result).strip()


def save_draft(mail, result):
    DRAFT_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", mail["subject"]).strip()[:50] or "메일"
    path = DRAFT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.txt"
    note = "※ 본문이 길어 앞부분만 분석했습니다.\n\n" if mail["truncated"] else ""
    path.write_text(
        f"{note}{result}\n\n{'=' * 60}\n[원문]\n{mail['text']}\n", encoding="utf-8"
    )
    return path


def check_once(user, password):
    seen = load_seen()
    pop = mailconn.connect_pop3(user, password)
    try:
        uids = {}
        for line in pop.uidl()[1]:
            num, uid = line.decode("ascii", "replace").split(maxsplit=1)
            uids[uid] = int(num)

        if seen is None:
            save_seen(set(uids))
            log(f"첫 실행: 기존 메일 {len(uids)}통은 건너뜁니다. 이후 도착하는 메일부터 처리합니다.")
            return

        new = sorted((n, u) for u, n in uids.items() if u not in seen)
        if not new:
            log("새 메일 없음")
        elif len(new) > MAX_PER_CYCLE:
            log(f"새 메일 {len(new)}통 중 {MAX_PER_CYCLE}통만 이번에 처리합니다.")

        for num, uid in new[:MAX_PER_CYCLE]:
            raw = b"\r\n".join(pop.retr(num)[1])
            mail = mailai.parse_mail(raw)
            log(f"새 메일: {mail['sender']} / {mail['subject']}")
            try:
                data = mailai.ask_claude(mail["text"])
            except Exception as e:
                # 기록하지 않고 넘어가서 다음 주기에 다시 시도한다
                log(f"  Claude 처리 실패, 다음 주기에 재시도: {e}")
                continue
            path = save_draft(mail, data["result"])
            log(f"  저장: {path}")
            notify(f"새 메일: {mail['subject']}", summary_of(data["result"]))
            seen.add(uid)
            save_seen(seen)

        # 서버에서 사라진 메일의 기록은 정리한다 (처리 못 한 새 메일은 건드리지 않는다)
        save_seen(seen & set(uids))
    finally:
        try:
            pop.quit()
        except Exception:
            pass


def main():
    user, password = mailconn.load_account()
    once = "--once" in sys.argv
    log(f"메일 감시 시작 ({user}, {INTERVAL_SEC}초 주기)")
    while True:
        try:
            check_once(user, password)
        except Exception as e:
            log(f"확인 실패, 다음 주기에 재시도: {type(e).__name__}: {e}")
        if once:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("종료")
