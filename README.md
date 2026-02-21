# 보안게시판 크롤러

웹페이지 게시판 URL 목록을 주기적으로 조회하고, 신규 게시글 여부를 Slack Webhook으로 알립니다.

## 구성 파일
- `monitor.py`: 크롤링/비교/Slack 전송 메인 스크립트
- `pyproject.toml`: `uv` 기반 의존성 관리 파일
- `config/boards.yaml`: 모니터링 대상 게시판 설정
- `data/board_state.json`: 이전 조회 상태 저장 파일
- `.github/workflows/daily-monitor.yml`: GitHub Actions 일일 실행 워크플로우

## 로컬 실행
```bash
uv python install 3.11
uv sync
uv run python monitor.py --config config/boards.yaml --state data/board_state.json --dry-run
```

실제 Slack 전송 테스트:
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
uv run python monitor.py --config config/boards.yaml --state data/board_state.json
```

`max_items` 적용 검사(상태 파일/Slack 전송 없음):
```bash
uv run python monitor.py --config config/boards.yaml --state data/board_state.json --inspect-items
```

미리보기 출력 건수 지정:
```bash
uv run python monitor.py --config config/boards.yaml --state data/board_state.json --inspect-items --inspect-limit 20
```

## boards.yaml 형식
```yaml
boards:
  - name: 게시판 이름
    url: https://example.com/board
    item_selector: "table tbody tr"
    title_selector: "td:nth-child(2) a"
    max_items: 20
```

- `item_selector`: 게시글 행/카드 선택자 (권장)
- `title_selector`: `item_selector` 내부 제목 선택자
- `max_items`: 비교할 상단 게시글 개수

선택자를 지정하지 않으면 페이지 내 `<a>` 텍스트를 휴리스틱으로 추출합니다.

## GitHub Actions 설정
1. 리포지토리 `Settings > Secrets and variables > Actions`에서 `SLACK_WEBHOOK_URL` 시크릿 추가
2. 기본 브랜치에 푸시
3. Actions 탭에서 `Daily Board Monitor` 워크플로우 확인

기본 스케줄은 `0 0 * * *` (UTC 기준 매일 00:00)입니다.
