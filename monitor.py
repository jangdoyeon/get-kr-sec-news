#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests
import yaml
from bs4 import BeautifulSoup, Tag


DEFAULT_CONFIG_PATH = Path("config/boards.yaml")
DEFAULT_STATE_PATH = Path("data/board_state.json")
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_ITEMS = 20
USER_AGENT = "get-news-monitor/0.1 (+https://github.com)"


@dataclass(frozen=True)
class BoardConfig:
    name: str
    url: str
    source_type: str = "html"
    data_url: str | None = None
    method: str = "GET"
    payload: dict[str, str] | None = None
    item_selector: str | None = None
    title_selector: str | None = None
    json_items_key: str = "data"
    json_title_key: str = "title"
    json_row_fields: list[str] | None = None
    max_items: int = DEFAULT_MAX_ITEMS


@dataclass(frozen=True)
class BoardResult:
    board: BoardConfig
    current_items: list[str]
    added_items: list[str]
    extracted_total: int
    row_text_by_title: dict[str, str]
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Board change monitor with Slack notification.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to boards YAML config.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Path to state JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Print result only, do not send Slack message.")
    parser.add_argument(
        "--inspect-items",
        action="store_true",
        help="Fetch boards and print extracted items for max_items verification (no state/slack update).",
    )
    parser.add_argument(
        "--inspect-limit",
        type=int,
        default=10,
        help="How many extracted items to print in inspect mode (default: 10).",
    )
    return parser.parse_args()


def load_boards(config_path: Path) -> list[BoardConfig]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    boards_raw = raw.get("boards", [])
    if not isinstance(boards_raw, list) or not boards_raw:
        raise ValueError("Config must include non-empty 'boards' list.")

    boards: list[BoardConfig] = []
    for idx, item in enumerate(boards_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"boards[{idx}] must be an object.")
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            raise ValueError(f"boards[{idx}] requires 'name' and 'url'.")

        max_items = item.get("max_items", DEFAULT_MAX_ITEMS)
        if not isinstance(max_items, int) or max_items <= 0:
            raise ValueError(f"boards[{idx}].max_items must be positive integer.")

        boards.append(
            BoardConfig(
                name=name,
                url=url,
                source_type=str(item.get("source_type", "html")).strip().lower(),
                data_url=str(item["data_url"]).strip() if item.get("data_url") else None,
                method=str(item.get("method", "GET")).strip().upper(),
                payload={str(k): str(v) for k, v in item.get("payload", {}).items()}
                if isinstance(item.get("payload"), dict)
                else None,
                item_selector=str(item["item_selector"]).strip() if item.get("item_selector") else None,
                title_selector=str(item["title_selector"]).strip() if item.get("title_selector") else None,
                json_items_key=str(item.get("json_items_key", "data")).strip(),
                json_title_key=str(item.get("json_title_key", "title")).strip(),
                json_row_fields=[str(v).strip() for v in item.get("json_row_fields", [])]
                if isinstance(item.get("json_row_fields"), list)
                else None,
                max_items=max_items,
            )
        )
    return boards


def load_state(state_path: Path) -> dict[str, list[str]]:
    if not state_path.exists():
        return {}
    with state_path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    if not isinstance(raw, dict):
        return {}

    parsed: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, list):
            parsed[key] = [str(v) for v in value]
    return parsed


def save_state(state_path: Path, state: dict[str, list[str]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)


def normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def choose_text_from_node(node: Tag, title_selector: str | None) -> str:
    if title_selector:
        title_node = node.select_one(title_selector)
        if title_node:
            return normalize_text(title_node.get_text(" ", strip=True))

    if node.name == "a":
        return normalize_text(node.get_text(" ", strip=True))

    anchor = node.find("a")
    if anchor:
        return normalize_text(anchor.get_text(" ", strip=True))

    return normalize_text(node.get_text(" ", strip=True))


def unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def extract_items(html: str, board: BoardConfig) -> tuple[list[str], int, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    unique_items: list[str] = []
    row_text_by_title: dict[str, str] = {}
    seen: set[str] = set()
    total_count = 0

    if board.item_selector:
        nodes = soup.select(board.item_selector)
        for node in nodes:
            text = choose_text_from_node(node, board.title_selector)
            if not text:
                continue
            total_count += 1
            if text in seen:
                continue
            seen.add(text)
            unique_items.append(text)
            row_text = normalize_text(node.get_text(" ", strip=True))
            if row_text:
                row_text_by_title[text] = row_text
    else:
        for anchor in soup.select("a"):
            text = normalize_text(anchor.get_text(" ", strip=True))
            if not (6 <= len(text) <= 140):
                continue
            total_count += 1
            if text in seen:
                continue
            seen.add(text)
            unique_items.append(text)

    limited_items = unique_items[: board.max_items]
    limited_row_text_by_title = {title: row_text_by_title[title] for title in limited_items if title in row_text_by_title}
    return limited_items, total_count, limited_row_text_by_title


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def fetch_json(url: str, method: str, payload: dict[str, str] | None) -> Any:
    response = requests.request(
        method=method,
        url=url,
        data=payload or {},
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
    )
    response.raise_for_status()
    return response.json()


def get_nested_value(data: Any, path: str) -> Any:
    current = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def extract_items_from_json(data: Any, board: BoardConfig) -> tuple[list[str], int, dict[str, str]]:
    items = get_nested_value(data, board.json_items_key)
    if not isinstance(items, list):
        return [], 0, {}

    unique_titles: list[str] = []
    row_text_by_title: dict[str, str] = {}
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        title_raw = item.get(board.json_title_key)
        if title_raw is None:
            continue
        title = normalize_text(str(title_raw))
        if not title:
            continue
        if title in seen:
            continue
        seen.add(title)
        unique_titles.append(title)

        fields = board.json_row_fields or list(item.keys())
        pairs: list[str] = []
        for field in fields:
            value = item.get(field)
            if value is None:
                continue
            value_text = normalize_text(str(value))
            if not value_text:
                continue
            pairs.append(f"{field}: {value_text}")
        row_text_by_title[title] = " | ".join(pairs) if pairs else title

    limited_titles = unique_titles[: board.max_items]
    limited_rows = {title: row_text_by_title[title] for title in limited_titles if title in row_text_by_title}
    return limited_titles, len(unique_titles), limited_rows


def diff_added(previous_items: list[str], current_items: list[str]) -> list[str]:
    previous_set = set(previous_items)
    return [item for item in current_items if item not in previous_set]


def process_board(board: BoardConfig, previous_items: list[str]) -> BoardResult:
    try:
        if board.source_type == "json":
            target_url = board.data_url or board.url
            data = fetch_json(target_url, board.method, board.payload)
            current_items, extracted_total, row_text_by_title = extract_items_from_json(data, board)
        else:
            target_url = board.data_url or board.url
            html = fetch_html(target_url)
            current_items, extracted_total, row_text_by_title = extract_items(html, board)
        added_items = diff_added(previous_items, current_items)
        return BoardResult(
            board=board,
            current_items=current_items,
            added_items=added_items,
            extracted_total=extracted_total,
            row_text_by_title=row_text_by_title,
            error=None,
        )
    except requests.RequestException as exc:
        return BoardResult(
            board=board,
            current_items=previous_items,
            added_items=[],
            extracted_total=len(previous_items),
            row_text_by_title={},
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return BoardResult(
            board=board,
            current_items=previous_items,
            added_items=[],
            extracted_total=len(previous_items),
            row_text_by_title={},
            error=str(exc),
        )


def print_inspection_report(results: list[BoardResult], inspect_limit: int) -> None:
    limit = inspect_limit if inspect_limit > 0 else 10
    print("=== max_items 검사 결과 ===")
    for result in results:
        print("")
        print(f"[{result.board.name}]")
        if result.error:
            print(f"- 상태: 실패 ({result.error})")
            continue
        print(f"- max_items: {result.board.max_items}")
        print(f"- 추출 전체(unique): {result.extracted_total}")
        print(f"- 실제 저장/비교 개수: {len(result.current_items)}")
        if result.extracted_total > len(result.current_items):
            print(f"- 제한 적용: O (상위 {result.board.max_items}개만 사용)")
        else:
            print("- 제한 적용: X (전체가 max_items 이하)")

        print(f"- 미리보기(최대 {limit}건):")
        for index, item in enumerate(result.current_items[:limit], start=1):
            print(f"  {index}. {item}")
            row_text = result.row_text_by_title.get(item)
            if row_text:
                print(f"     - 레코드 텍스트: {row_text}")


def build_slack_message(results: list[BoardResult]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"*보안게시판 모니터링 결과* ({now})"]

    has_new = any(result.added_items for result in results)
    has_error = any(result.error for result in results)

    if has_new:
        lines.append("")
        lines.append("*신규 게시글 감지*")
    else:
        lines.append("")
        lines.append("모든 게시판에서 신규 게시글이 발견되지 않았습니다.")

    for result in results:
        board_label = f"*{result.board.name}* <{result.board.url}|바로가기>"
        if result.error:
            lines.append(f"- {board_label}: 조회 실패 ({result.error})")
            continue
        if not result.added_items:
            lines.append(f"- {board_label}: 변경 없음")
            continue

        lines.append(f"- {board_label}: 신규 {len(result.added_items)}건")
        for item in result.added_items[:5]:
            lines.append(f"  - {item}")
        if len(result.added_items) > 5:
            lines.append(f"  - ... 외 {len(result.added_items) - 5}건")

    if has_error:
        lines.append("")
        lines.append("일부 게시판 조회에 실패했습니다. URL/셀렉터/네트워크 상태를 확인하세요.")

    return "\n".join(lines)


def post_to_slack(webhook_url: str, text: str) -> None:
    response = requests.post(webhook_url, json={"text": text}, timeout=DEFAULT_TIMEOUT_SECONDS)
    response.raise_for_status()


def main() -> int:
    args = parse_args()

    boards = load_boards(args.config)
    old_state = load_state(args.state)

    results: list[BoardResult] = []
    new_state: dict[str, list[str]] = {}

    for board in boards:
        previous_items = old_state.get(board.name, [])
        result = process_board(board, previous_items)
        results.append(result)
        new_state[board.name] = result.current_items

    if args.inspect_items:
        print_inspection_report(results, args.inspect_limit)
        return 0

    save_state(args.state, new_state)
    message = build_slack_message(results)
    print(message)

    if args.dry_run:
        return 0

    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL is required unless --dry-run is used.")

    post_to_slack(webhook, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
