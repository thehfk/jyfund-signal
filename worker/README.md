# JY Fund Signal — 관리자 Worker

브라우저의 그룹 편집 → 이 Worker → GitHub API. PAT은 Worker 시크릿에만 존재하고 브라우저·저장소엔 없음.

## 엔드포인트

- `GET  /health`  — ping
- `POST /login`   — body `{password}` → `{ok:true, token, expiresInMs}` 또는 401
- `PUT  /groups`  — header `Authorization: Bearer <token>` + body는 `data/groups.json` 콘텐츠. Worker가 GitHub API로 커밋.

## 최초 배포

```bash
# 1) wrangler 설치
npm i -g wrangler         # 또는  brew install cloudflare-wrangler

# 2) Cloudflare 로그인 (브라우저 팝업)
wrangler login

# 3) 이 디렉토리에서 시크릿 세팅
cd worker
wrangler secret put ADMIN_PW         # 원하시는 비밀번호 (12자 이상 권장)
wrangler secret put GITHUB_PAT       # jyfund-signal에 대한 contents:write PAT
openssl rand -hex 32 | wrangler secret put SESSION_SECRET

# 4) 배포
wrangler deploy
# → 콘솔에 https://jyfund-signal-admin.<subdomain>.workers.dev 출력됨
#   이 URL을 프론트엔드 index.html의 WORKER_URL 상수에 세팅
```

## 비밀번호 · PAT 교체

```bash
wrangler secret put ADMIN_PW         # 새 비밀번호 입력
# 기존 로그인 세션은 SESSION_SECRET을 갱신할 때만 강제 무효화됨.
# 세션까지 무효화하려면:
openssl rand -hex 32 | wrangler secret put SESSION_SECRET
```

PAT 교체 시: GitHub에서 새 PAT 발급 → `wrangler secret put GITHUB_PAT` → 기존 PAT는 GitHub에서 revoke.

## 로컬 개발

```bash
wrangler dev     # http://127.0.0.1:8787
# 로컬 시크릿은 .dev.vars 파일에 (git ignored):
#   ADMIN_PW=dev-password
#   GITHUB_PAT=github_pat_...
#   SESSION_SECRET=any-random-hex
```

## 로그 확인

```bash
wrangler tail
```
