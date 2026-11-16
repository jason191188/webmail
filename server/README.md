# Webmail Assistant API

카페24 호스팅 메일을 주기적으로 확인해 새 메일 알림(웹앱 푸시)을 보내고, 관리자가 요청한 메일에 한해 Claude API 로 요약과 답장 초안을 만들어 답장까지 보낼 수 있는 서버.

## 배포 (리눅스 + Docker)

```bash
cp .env.example .env      # 값 채우기 (ANTHROPIC_API_KEY, MAIL_ENC_KEY, ADMIN_PASSWORD)
mkdir -p data && sudo chown 1000 data
docker compose up -d --build
docker compose logs -f api
```

- 웹 화면: `http://<서버주소>:8000/` (프록시 프리픽스를 쓰면 `https://<도메인>/webmail/`)
- API 문서(`/docs`)는 기본으로 닫혀 있다. 개발할 때만 `.env` 에 `DOCS_ENABLED=true` 를 넣어 연다.
- 로그인 토큰과 비밀번호가 오가므로 외부에 열기 전에 HTTPS(리버스 프록시)를 앞에 둬야 한다.

## 휴대폰 알림 (웹앱)

HTTPS 주소에서만 동작한다. `.env` 에 `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` 를 넣어야 켜진다.

- 안드로이드: Chrome 으로 접속 → 메뉴의 "홈 화면에 추가" (또는 "앱 설치") → 로그인 → "이 기기에서 알림 받기"
- 아이폰 (iOS 16.4 이상): Safari 로 접속 → 공유 버튼 → "홈 화면에 추가" → 홈 화면 아이콘으로 실행 → 로그인 → "이 기기에서 알림 받기"
- PC: 브라우저에서 로그인 → "이 기기에서 알림 받기"

알림은 기기마다 따로 켠다. 로그아웃하거나 "내 계정 → 내 기기"에서 끊으면 그 기기의 알림 등록도 지워진다.

어떤 메일을 알릴지는 사용자마다 "알림 설정"에서 정한다. 판단 순서는 ① 알리지 않을 발신자·단어에 해당하면 알리지 않음 → ② 중요한 발신자·단어에 해당하면 알림 → ③ "중요한 메일만"이 켜져 있으면 알리지 않음 → ④ 광고·대량 발송 메일이고 제외가 켜져 있으면 알리지 않음 → ⑤ 그 밖에는 알림. 광고·대량 발송 여부는 `List-Unsubscribe` / `List-Id` 헤더, `Precedence: bulk|junk|list`, 제목의 `(광고)` 로 판단한다.

## 권한

| 기능 | 일반 사용자 | 관리자 | 최초 관리자 |
|---|---|---|---|
| 로그인, 비밀번호 변경, 자기 메일 계정 등록 | O | O | O |
| 새 메일 알림 목록 (보낸사람, 제목, 시간), 알림 설정, 내 기기 관리 | O | O | O |
| 메일 본문 조회 | X | O (자기 메일) | O (자기 메일) |
| 요약 / 답장 초안 요청, 답장 발송 | X | O (자기 메일) | O (자기 메일) |
| 사용자 관리, 사용량 조회, 백업 | X | X | O |

최초 관리자는 첫 실행 때 `.env` 의 `ADMIN_USERNAME` 으로 만들어진 계정 하나뿐이며, 강등하거나 비활성화할 수 없다.

## 답장 발송

`POST /mails/{id}/reply` 를 호출할 때만 등록된 메일 계정의 SMTP 로 보낸다. 자동 발송은 없다. 서버가 POP3 만 지원해 웹메일의 보낸편지함에는 남지 않으므로, 기본으로 본인 주소를 숨은참조에 넣어 받은편지함에 사본을 남긴다. 보낸 기록(받는사람, 제목, 내용, 시각)은 서버에도 저장된다.

## 백업과 보관

- 하루 한 번 `data/backups/app-YYYYMMDD-HHMMSS.db` 로 DB 전체를 복사하고 최근 `BACKUP_KEEP` 개(기본 7)를 남긴다. "사용량·백업" 화면에서 바로 만들 수도 있다.
- 백업은 같은 디스크에 있으므로 디스크 고장에는 대비가 안 된다. `data/backups` 를 주기적으로 다른 곳에 복사해 둔다.
- 백업 파일에는 메일 본문과 암호화된 메일 비밀번호가 들어 있다. `.env` 의 `MAIL_ENC_KEY` 가 함께 있어야 메일 비밀번호를 풀 수 있으니 `.env` 도 따로 보관한다.
- 받은 지 `MAIL_RETENTION_DAYS` 일(기본 90)이 지난 메일은 요약, 답장 기록과 함께 자동으로 지운다. 사용량 기록은 지우지 않는다. `0` 이면 지우지 않는다.

복구:

```bash
docker compose down
cp data/backups/app-YYYYMMDD-HHMMSS.db data/app.db
rm -f data/app.db-wal data/app.db-shm
docker compose up -d
```

## 주요 API

모든 요청은 `Authorization: Bearer <토큰>` 헤더가 필요하다 (`/health`, `/auth/login` 제외).

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/auth/login` | 로그인, 토큰 발급 (기본 30일 유효) |
| POST | `/auth/logout` | 현재 토큰 폐기 |
| GET | `/me` | 내 정보 |
| PUT | `/me/password` | 비밀번호 변경 (다른 기기는 로그아웃) |
| GET | `/me/devices` | 내 계정으로 로그인된 기기 목록 |
| DELETE | `/me/devices/{id}` | 그 기기를 로그아웃시키고 알림 등록도 삭제 |
| PUT / GET / DELETE | `/me/mail-account` | 메일 계정 등록(로그인 검증 후 저장) / 조회 / 삭제 |
| GET / PUT | `/me/notification-settings` | 알림 설정 조회 / 저장 |
| GET | `/mails?after_id=` | 새 메일 알림 목록. 앱은 마지막으로 본 id 를 `after_id` 로 넘겨 폴링한다 |
| POST | `/mails/sync` | 주기를 기다리지 않고 지금 확인 |
| GET | `/push/public-key` | 푸시 구독용 공개 키 |
| POST | `/push/subscriptions` | 이 기기를 알림 대상으로 등록 |
| POST | `/push/unsubscribe` | 이 기기 알림 해제 |
| POST | `/push/test` | 내 기기들에 테스트 알림 발송 |
| GET | `/mails/{id}` | 메일 본문과 보낸 답장 기록 (관리자) |
| POST | `/mails/{id}/analysis` | 요약 / 답장 초안 요청 (관리자). 이미 분석한 메일은 저장된 결과 반환, `?force=true` 로 재분석 |
| GET | `/mails/{id}/analysis` | 저장된 분석 결과 (관리자) |
| POST | `/mails/{id}/reply` | 답장 발송 (관리자) |
| GET / POST | `/admin/users` | 사용자 목록 / 생성 (최초 관리자) |
| PATCH | `/admin/users/{id}` | 역할 변경, 비활성화, 비밀번호 초기화 (최초 관리자) |
| GET | `/admin/usage?months=` | 월별·사용자별 Claude API 사용량과 환산 비용 (최초 관리자) |
| GET / POST | `/admin/backups` | 백업 목록 / 지금 백업 (최초 관리자) |

## 개발

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest -q
```
