#!/usr/bin/env bash

set -Eeuo pipefail

# Linux/GNU userland is assumed for date --iso-8601 and mktemp behavior.
# Secret exclusion is best-effort; review the generated file before sharing it.

INCLUDE_REGEX='\.(cs|csproj|sln|props|targets|py|toml|json|yml|yaml|xml|md|sh|service|conf|config)$'
EXCLUDE_REGEX='(^|/)(\.git|bin|obj|build|dist|node_modules|\.venv|venv|__pycache__|\.pytest_cache|\.ruff_cache|coverage|TestResults|artifacts|data)(/|$)|(^|/)\.env($|\.)|(^|/).*\.db$|(^|/).*\.sqlite3?$|(^|/).*\.pyc$|(^|/).*\.pdb$|(^|/).*\.dll$|(^|/).*\.exe$'

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || fail "git is required"

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

output_file="${OUTPUT_FILE:-$repo_root/combined_code.txt}"
output_dir="$(dirname "$output_file")"
mkdir -p "$output_dir"

output_abs="$(realpath -m "$output_file")"
tmp_file="$(mktemp "$output_dir/.combined_code.tmp.XXXXXX")"
trap 'rm -f "$tmp_file"' EXIT

repo_name="$(basename "$repo_root")"
commit_sha="$(git rev-parse HEAD)"
branch_name="$(git branch --show-current)"
generated_at="$(date --iso-8601=seconds)"

{
    printf '# Combined source code\n\n'
    printf 'Repository: %s\n' "$repo_name"
    printf 'Branch: %s\n' "$branch_name"
    printf 'Commit: %s\n' "$commit_sha"
    printf 'Generated: %s\n\n' "$generated_at"
    printf 'This file contains tracked source and project files only.\n'
    printf 'Secrets, databases, generated artifacts, and local environments are excluded on a best-effort basis.\n\n'
} > "$tmp_file"

file_count=0
total_lines=0

while IFS= read -r -d '' file; do
    file_abs="$(realpath -m "$file")"

    [[ "$file_abs" == "$output_abs" ]] && continue
    [[ "$file" =~ $INCLUDE_REGEX ]] || continue
    [[ "$file" =~ $EXCLUDE_REGEX ]] && continue
    [[ -f "$file" ]] || continue

    if ! grep -Iq . "$file" && [[ -s "$file" ]]; then
        log "Skipping non-text file: $file"
        continue
    fi

    line_count="$(wc -l < "$file")"

    {
        printf '\n'
        printf '================================================================================\n'
        printf 'FILE: %s\n' "$file"
        printf 'LINES: %s\n' "$line_count"
        printf '================================================================================\n\n'
        cat "$file"

        if [[ -s "$file" ]] && [[ "$(tail -c 1 "$file" | wc -l)" -eq 0 ]]; then
            printf '\n'
        fi
    } >> "$tmp_file"

    file_count=$((file_count + 1))
    total_lines=$((total_lines + line_count))
done < <(git ls-files -z | sort -z)

{
    printf '\n'
    printf '================================================================================\n'
    printf 'SUMMARY\n'
    printf '================================================================================\n'
    printf 'Files: %d\n' "$file_count"
    printf 'Source lines: %d\n' "$total_lines"
} >> "$tmp_file"

mv "$tmp_file" "$output_file"
trap - EXIT

log "Combined file created: $output_file"
log "Files included: $file_count"
log "Source lines included: $total_lines"
