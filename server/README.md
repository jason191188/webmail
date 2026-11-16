# Webmail Assistant API

카페24 호스팅 메일을 주기적으로 확인해 새 메일 알림을 제공하고, 관리자가 요청한 메일에 한해 Claude API 로 요약과 답장 초안을 만드는 API 서버.

## 배포 (리눅스 + Docker)

```bash
cp .env.example .env      # 값 채우기 (ANTHROPIC_API_KEY, MAIL_ENC_KEY, ADMIN_PASSWORD)
mkdir -p data && sudo chown 1000 data
docker compose up -d --build
docker compose logs -f api
```

- 웹 화면: `http://<서버주소>:8000/` (프록시 프리픽스를 쓰면 `https://<도메인>/webmail/`)
- API 문서와 테스트 화면: `http://<서버주소>:8000/docs`
- 데이터(SQLite)는 `./data` 에 저장된다. 이 폴더와 `.env` 를 백업하면 된다.
- 로그인 토큰과 비밀번호가 오가므로 외부에 열기 전에 HTTPS(리버스 프록시)를 앞에 둬야 한다.

## 휴대폰 알림 (웹앱)

HTTPS 주소에서만 동작한다. `.env` 에 `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` 를 넣어야 켜진다.

- 안드로이드: Chrome 으로 접속 → 메뉴의 "홈 화면에 추가" (또는 "앱 설치") → 로그인 → "이 기기에서 알림 받기"
- 아이폰 (iOS 16.4 이상): Safari 로 접속 → 공유 버튼 → "홈 화면에 추가" → 홈 화면 아이콘으로 실행 → 로그인 → "이 기기에서 알림 받기"
- PC: 브라우저에서 로그인 → "이 기기에서 알림 받기"

알림은 기기마다 따로 켠다. 로그아웃하면 그 기기의 알림 등록도 지워진다.

## 권한

| 기능 | 일반 사용자 | 관리자 | 최초 관리자 |
|---|---|---|---|
| 로그인, 비밀번호 변경, 자기 메일 계정 등록 | O | O | O |
| 새 메일 알림 목록 (보낸사람, 제목, 시간) | O | O | O |
| 메일 본문 조회 | X | O (자기 메일) | O (자기 메일) |
| 요약 / 답장 초안 요청 | X | O (자기 메일) | O (자기 메일) |
| 사용자 생성, 역할 변경, 비활성화 | X | X | O |

최초 관리자는 첫 실행 때 `.env` 의 `ADMIN_USERNAME` 으로 만들어진 계정 하나뿐이며, 강등하거나 비활성화할 수 없다.

## 주요 API

모든 요청은 `Authorization: Bearer <토큰>` 헤더가 필요하다 (`/health`, `/auth/login` 제외).

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/auth/login` | 로그인, 토큰 발급 (기본 30일 유효) |
| POST | `/auth/logout` | 현재 토큰 폐기 |
| GET | `/me` | 내 정보 |
| PUT | `/me/password` | 비밀번호 변경 |
| PUT / GET / DELETE | `/me/mail-account` | 메일 계정 등록(로그인 검증 후 저장) / 조회 / 삭제 |
| GET | `/mails?after_id=` | 새 메일 알림 목록. 앱은 마지막으로 본 id 를 `after_id` 로 넘겨 폴링한다 |
| POST | `/mails/sync` | 주기를 기다리지 않고 지금 확인 |
| GET | `/push/public-key` | 푸시 구독용 공개 키 |
| POST | `/push/subscriptions` | 이 기기를 알림 대상으로 등록 |
| POST | `/push/unsubscribe` | 이 기기 알림 해제 |
| POST | `/push/test` | 내 기기들에 테스트 알림 발송 |
| GET | `/mails/{id}` | 메일 본문 (관리자) |
| POST | `/mails/{id}/analysis` | 요약 / 답장 초안 요청 (관리자). 이미 분석한 메일은 저장된 결과 반환, `?force=true` 로 재분석 |
| GET | `/mails/{id}/analysis` | 저장된 분석 결과 (관리자) |
| GET / POST | `/admin/users` | 사용자 목록 / 생성 (최초 관리자) |
| PATCH | `/admin/users/{id}` | 역할 변경, 비활성화, 비밀번호 초기화 (최초 관리자) |

## 개발

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest -q
```
