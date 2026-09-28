#!/usr/bin/env python
"""Search a codebase and have Jev order every lead for a coding agent.

With --pattern, search every literal match in the ripgrep-visible scope. Without it,
score every chunk of every ripgrep-visible UTF-8 text file. No low-scoring result is
discarded. This tool never edits code or invokes the large coding model itself.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from route import jev, probability


CHUNK_LINES = 20
MAX_SNIPPET_CHARS = 2400
MAX_ENTRIES = 2000
BATCH_SIZE = 10
RG_SCOPE = ["--hidden", "--glob", "!.git/**", "--glob", "!**/node_modules/**",
            "--glob", "!**/target/**", "--glob", "!**/.venv/**",
            "--glob", "!**/.env*", "--glob", "!**/*.key", "--glob", "!**/*.pem",
            "--glob", "!**/Cargo.lock", "--glob", "!**/package-lock.json",
            "--glob", "!**/pnpm-lock.yaml", "--glob", "!**/yarn.lock"]


def rg(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["rg", *args], cwd=repo, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ripgrep (rg) is required") from exc
    if result.returncode not in (0, 1):
        message = result.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"ripgrep failed: {message}")
    return result


def visible_files(repo: Path, exclude: Path | None = None) -> list[Path]:
    result = rg(repo, ["--files", "-0", *RG_SCOPE])
    paths = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "replace")
        path = (repo / relative).resolve()
        if path.is_relative_to(repo) and path.is_file() and path != exclude:
            paths.append(path)
    return sorted(set(paths))


def source_lines(path: Path):
    raw = path.read_bytes()
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def snippet(lines: list[str], start: int, end: int) -> tuple[str, bool]:
    text = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))
    clipped = len(text) > MAX_SNIPPET_CHARS
    if clipped:
        text = text[:MAX_SNIPPET_CHARS]
    return text, clipped


def chunks(lines: list[str]):
    """Cover every source line, splitting oversized lines instead of losing them."""
    index = 0
    while index < len(lines):
        line_number = index + 1
        prefix = f"{line_number}: "
        if len(prefix) + len(lines[index]) > MAX_SNIPPET_CHARS:
            width = MAX_SNIPPET_CHARS - len(prefix)
            for offset in range(0, len(lines[index]), width):
                yield line_number, line_number, prefix + lines[index][offset:offset + width]
            index += 1
            continue
        start = index
        parts = []
        size = 0
        while index < len(lines) and index - start < CHUNK_LINES:
            part = f"{index + 1}: {lines[index]}"
            if parts and size + len(part) + 1 > MAX_SNIPPET_CHARS:
                break
            parts.append(part)
            size += len(part) + 1
            index += 1
        yield start + 1, index, "\n".join(parts)


def all_chunks(repo: Path, max_entries: int, exclude: Path | None = None):
    leads, excluded = [], []
    for path in visible_files(repo, exclude):
        relative = path.relative_to(repo).as_posix()
        try:
            lines = source_lines(path)
        except OSError as exc:
            raise RuntimeError(f"Cannot read {relative}: {exc.strerror}") from exc
        if lines is None:
            excluded.append(relative)
            continue
        for start, end, excerpt in chunks(lines):
            leads.append({"path": relative, "start_line": start,
                          "end_line": end, "snippet": excerpt, "clipped": False})
            if len(leads) > max_entries:
                raise ValueError(f"More than {max_entries} code chunks; narrow --repo or raise --max-entries")
    return leads, excluded


def literal_matches(repo: Path, pattern: str, max_entries: int,
                    exclude: Path | None = None):
    result = rg(repo, ["--json", "-n", "-F", *RG_SCOPE, "--", pattern, "."])
    leads = []
    cache = {}
    excluded = []
    for raw in result.stdout.splitlines():
        event = json.loads(raw)
        if event.get("type") != "match":
            continue
        data = event["data"]
        relative = data["path"].get("text")
        line = data["line_number"]
        if not isinstance(relative, str) or not isinstance(line, int):
            continue
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo) or not path.is_file() or path == exclude:
            continue
        relative = path.relative_to(repo).as_posix()
        if relative not in cache:
            cache[relative] = source_lines(path)
        lines = cache[relative]
        if lines is None:
            excluded.append(relative)
            continue
        start, end = max(0, line - 3), min(len(lines), line + 2)
        excerpt, clipped = snippet(lines, start, end)
        leads.append({"path": relative, "start_line": line, "end_line": line,
                      "snippet": excerpt, "clipped": clipped})
        if len(leads) > max_entries:
            raise ValueError(f"More than {max_entries} literal matches; narrow --repo or raise --max-entries")
    return leads, sorted(set(excluded))


def rank(task: str, leads: list[dict]):
    ranked = []
    model = None
    for start in range(0, len(leads), BATCH_SIZE):
        batch = leads[start:start + BATCH_SIZE]
        state = {"requested_change": task, "leads": [
            {"id": f"l{i}", **lead} for i, lead in enumerate(batch)
        ]}
        questions = {f"l{i}": {
            "type": "noul",
            "instructions": (
                f"Could `leads[{i}]` contain code the coding agent should inspect for "
                "`requested_change`? Judge the code in context, not word overlap. "
                "A yes is a search lead, not proof that an edit is needed."
            ),
            "criteria": {
                "true": "This location may participate in the requested behavior or change.",
                "false": "This location is unrelated to the requested behavior or change."
            }
        } for i in range(len(batch))}
        response = jev(state, questions, retries=1)
        model = response.get("model", model)
        answers = response["answers"]
        for i, lead in enumerate(batch):
            answer = answers.get(f"l{i}")
            if not isinstance(answer, dict) or answer.get("type") not in (None, "noul"):
                raise ValueError(f"Missing Jev relevance answer for lead {start + i}")
            ranked.append({**lead, "relevance": probability(answer["noul"])})
    ranked.sort(key=lambda item: (-item["relevance"], item["path"], item["start_line"]))
    return ranked, model


def run(repo: Path, task: str, pattern: str | None = None,
        max_entries: int = MAX_ENTRIES, exclude: Path | None = None):
    repo = repo.resolve()
    if not repo.is_dir() or not task.strip():
        raise ValueError("--repo must exist and --task must be non-empty")
    if pattern == "" or max_entries < 1:
        raise ValueError("--pattern must be non-empty and --max-entries positive")
    exclude = exclude.resolve() if exclude else None
    if pattern is None:
        leads, excluded = all_chunks(repo, max_entries, exclude)
        mode = "all_text_chunks"
    else:
        leads, excluded = literal_matches(repo, pattern, max_entries, exclude)
        mode = "all_literal_matches"
    result = {"task": task, "mode": mode, "pattern": pattern,
              "scope": "ripgrep-visible UTF-8 text files under --repo",
              "lead_count": len(leads), "excluded_files": excluded,
              "clipped_leads": sum(lead["clipped"] for lead in leads),
              "ranking_used": False, "leads": leads}
    if len(leads) > 1:
        try:
            result["leads"], result["model"] = rank(task, leads)
            result["ranking_used"] = True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            result["error"] = f"Jev ranking unavailable: {exc}"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--pattern", help="optional exhaustive literal search")
    parser.add_argument("--max-entries", type=int, default=MAX_ENTRIES)
    parser.add_argument("--output", type=Path, help="write the complete handoff JSON here")
    args = parser.parse_args()
    try:
        result = run(args.repo, args.task, args.pattern, args.max_entries, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Code search failed: {exc}\n")
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(args.output.resolve()),
                          "lead_count": result["lead_count"],
                          "ranking_used": result["ranking_used"],
                          "error": result.get("error")}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(2)


if __name__ == "__main__":
    main()
