#!/bin/bash

# Review a PR using Claude Code.
#
# This script fetches the PR diff and uses Claude Code to provide a code review.
# The review is posted as a PR comment using the GitHub CLI.
#
# Usage:
#   tools/review-pr-with-claude.sh [options]
#
# Options:
#   --pr NUMBER         PR number to review (required in CI, auto-detected
#                       locally)
#   --max-turns N       Maximum Claude turns (default: 50)
#   --interactive       Run Claude in interactive mode (default: headless)
#   --ci                CI mode: output machine-readable status, no colors
#   --dry-run           Don't post the review, just print it
#   --force             Review even if bot has already reviewed this PR
#   --output-dir DIR    Directory for output files (default: temp dir)
#   --help              Show this help message
#
# Environment:
#   GITHUB_TOKEN        Required for posting reviews
#   GITHUB_REPOSITORY   Repository in owner/repo format (set by GitHub Actions)
#
# Exit codes:
#   0 - Review posted successfully
#   1 - Error occurred
#
# Examples:
#   # Review PR #123
#   tools/review-pr-with-claude.sh --pr 123
#
#   # CI mode (PR number from environment)
#   tools/review-pr-with-claude.sh --ci
#
#   # Dry run to see what would be posted
#   tools/review-pr-with-claude.sh --pr 123 --dry-run

set -e

topdir=$(cd "$(dirname "$0")/.." && pwd)
cd "${topdir}"

# Default options
pr_number=""
max_turns=50
interactive=false
ci_mode=false
dry_run=false
force=false
output_dir=""

# Colors for output (disabled in CI mode)
setup_colors() {
    if [ "${ci_mode}" = true ]; then
        RED=''
        GREEN=''
        YELLOW=''
        BLUE=''
        NC=''
    else
        RED='\033[0;31m'
        GREEN='\033[0;32m'
        YELLOW='\033[1;33m'
        BLUE='\033[0;34m'
        NC='\033[0m'
    fi
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --pr)
            pr_number="$2"
            shift 2
            ;;
        --max-turns)
            max_turns="$2"
            shift 2
            ;;
        --interactive)
            interactive=true
            shift
            ;;
        --ci)
            ci_mode=true
            shift
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        --force)
            force=true
            shift
            ;;
        --output-dir)
            output_dir="$2"
            shift 2
            ;;
        --help|-h)
            head -38 "$0" | tail -35
            exit 0
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            shift
            ;;
    esac
done

setup_colors

# Validate --max-turns is a positive integer
if ! [[ "${max_turns}" =~ ^[0-9]+$ ]] || [ "${max_turns}" -lt 1 ]; then
    echo -e "${RED}Error: --max-turns must be a positive integer${NC}"
    exit 1
fi

# Create output directory
if [ -z "${output_dir}" ]; then
    output_dir=$(mktemp -d)
    cleanup_output=true
else
    mkdir -p "${output_dir}"
    cleanup_output=false
fi

cleanup() {
    if [ "${cleanup_output}" = true ]; then
        rm -rf "${output_dir}"
    fi
}
trap cleanup EXIT

# CI mode output helper
ci_output() {
    local key="$1"
    local value="$2"
    if [ "${ci_mode}" = true ]; then
        echo "${key}=${value}"
    fi
}

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Shaken Fist PR Reviewer${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Step 1: Validate environment
echo -e "${YELLOW}Step 1: Validating environment...${NC}"

if ! command -v gh &> /dev/null; then
    echo -e "${RED}Error: GitHub CLI (gh) not found${NC}"
    exit 1
fi

if ! command -v claude &> /dev/null; then
    echo -e "${RED}Error: Claude Code CLI not found${NC}"
    exit 1
fi

# Get PR number if not provided
if [ -z "${pr_number}" ]; then
    # Try to get from GitHub Actions event
    if [ -n "${GITHUB_EVENT_PATH}" ] && [ -f "${GITHUB_EVENT_PATH}" ]; then
        jq_filter='.pull_request.number // .number // empty'
        pr_number=$(jq -r "${jq_filter}" "${GITHUB_EVENT_PATH}" \
            2>/dev/null || true)
    fi

    # Try to get from current branch
    if [ -z "${pr_number}" ]; then
        pr_number=$(gh pr view --json number -q '.number' 2>/dev/null || true)
    fi

    if [ -z "${pr_number}" ]; then
        echo -e "${RED}Error: Could not determine PR number${NC}"
        echo "Use --pr NUMBER to specify explicitly"
        exit 1
    fi
fi

echo -e "${GREEN}Reviewing PR #${pr_number}${NC}"
echo

# Step 2: Fetch PR information
echo -e "${YELLOW}Step 2: Fetching PR information...${NC}"

# Get PR details
gh pr view "${pr_number}" --json title,body,author,baseRefName,headRefName \
    > "${output_dir}/pr-info.json"

pr_title=$(jq -r '.title' "${output_dir}/pr-info.json")
pr_author=$(jq -r '.author.login' "${output_dir}/pr-info.json")
base_branch=$(jq -r '.baseRefName' "${output_dir}/pr-info.json")
head_branch=$(jq -r '.headRefName' "${output_dir}/pr-info.json")

echo "Title: ${pr_title}"
echo "Author: ${pr_author}"
echo "Branch: ${head_branch} -> ${base_branch}"
echo

# Get the diff
echo -e "${YELLOW}Step 3: Fetching PR diff...${NC}"
gh pr diff "${pr_number}" > "${output_dir}/pr-diff.txt"

diff_lines=$(wc -l < "${output_dir}/pr-diff.txt")
echo "Diff size: ${diff_lines} lines"
echo

# Check if diff is too large
if [ "${diff_lines}" -gt 5000 ]; then
    msg="Warning: Large diff (${diff_lines} lines), review may be limited"
    echo -e "${YELLOW}${msg}${NC}"
fi

# Step 4: Check for existing bot reviews
echo -e "${YELLOW}Step 4: Checking for existing reviews...${NC}"

jq_filter='.reviews[] | select(.author.login == "github-actions"'
jq_filter+=' or .author.login == "shakenfist-bot") | .id'
existing_review=$(gh pr view "${pr_number}" --json reviews \
    --jq "${jq_filter}" 2>/dev/null | head -1 || true)

if [ -n "${existing_review}" ]; then
    if [ "${force}" = true ]; then
        echo -e "${YELLOW}Note: Bot has already reviewed this PR${NC}"
        echo "Proceeding with new review (--force specified)..."
    else
        echo -e "${YELLOW}Bot has already reviewed this PR${NC}"
        echo "Use --force to review again"
        ci_output "review_skipped" "already_reviewed"
        exit 0
    fi
fi
echo

# Step 5: Run Claude Code for review
echo -e "${YELLOW}Step 5: Running Claude Code for review...${NC}"
echo

# Build the prompt - request structured JSON output
cat > "${output_dir}/claude-prompt.txt" << 'PROMPT_EOF'
You are reviewing Pull Request #${pr_number} for the Shaken Fist occystrap project.

## PR Information

- **Title**: ${pr_title}
- **Author**: ${pr_author}
- **Branch**: ${head_branch} -> ${base_branch}

## Your Task

0. Read the contents of AGENTS.md, ARCHITECTURE.md, and README.md to
   gather context.

1. Read the PR diff below carefully

2. Analyze the changes for:
   - Code quality and readability
   - Potential bugs or logic errors
   - Security concerns (path traversal, injection, etc.)
   - Performance implications
   - Test coverage (are new features tested?)
   - Documentation (are changes documented?)
   - Style consistency with the codebase

3. Output your review as a JSON object with the following structure:

```json
{
  "summary": "Brief 1-3 sentence summary of what the PR does",
  "items": [
    {
      "id": 1,
      "title": "Short title for this item",
      "category": "security|bug|performance|documentation|style|testing|other",
      "severity": "critical|high|medium|low",
      "action": "fix|document|consider|none",
      "description": "Detailed description of the issue or observation",
      "location": "occystrap/file.py:100-150",
      "suggestion": "Specific suggestion for how to address this",
      "rationale": "For action=none or consider, explain why"
    }
  ],
  "positive_feedback": [
    {
      "title": "What was done well",
      "description": "Why this is good"
    }
  ],
  "test_coverage": {
    "adequate": true,
    "missing": ["list of missing test scenarios"]
  }
}
```

## Action Types

- **fix**: This MUST be fixed before merging (security issues, bugs, etc.)
- **document**: Documentation should be added or updated
- **consider**: Optional improvement, reviewer's suggestion but not required
- **none**: Informational observation only, no action needed

## Important Rules

1. Every item MUST have: id, title, category, action
2. Items with action="fix" MUST have severity
3. Items with action="none" or "consider" SHOULD have rationale
4. Include location (file:lines) when referencing specific code
5. Be specific in suggestions - vague advice is not actionable

## CRITICAL: Output Format

Your response MUST contain a JSON code block with the review data.
Start the JSON block with ```json and end with ```.
Do NOT post the review to GitHub - just output the JSON.
The JSON will be validated and rendered to markdown by a separate script.

## Code Style Notes for Shaken Fist

- Python code uses single quotes for strings, double quotes for docstrings
- Line length limit is 80 chars
- Type hints are encouraged

## The PR Diff

PROMPT_EOF

# Substitute variables in the prompt using Python for safe handling of
# user-controlled input (PR titles can contain any characters including
# newlines, quotes, and shell metacharacters)
prompt_file="${output_dir}/claude-prompt.txt"
python3 - "${prompt_file}" "${pr_number}" "${pr_title}" "${pr_author}" \
    "${head_branch}" "${base_branch}" << 'PYSUBST'
import sys
from pathlib import Path

prompt_file = Path(sys.argv[1])
pr_number, pr_title, pr_author, head_branch, base_branch = sys.argv[2:7]

content = prompt_file.read_text()
content = content.replace('${pr_number}', pr_number)
content = content.replace('${pr_title}', pr_title)
content = content.replace('${pr_author}', pr_author)
content = content.replace('${head_branch}', head_branch)
content = content.replace('${base_branch}', base_branch)
prompt_file.write_text(content)
PYSUBST

# Append the diff
cat "${output_dir}/pr-diff.txt" >> "${prompt_file}"

if [ "${interactive}" = true ]; then
    echo "Prompt file: ${prompt_file}"
    echo
    echo "Run 'claude' and paste the prompt to review the PR interactively."
    exit 0
fi

if [ "${dry_run}" = true ]; then
    echo "Dry run mode - would send this prompt to Claude:"
    echo "---"
    head -80 "${prompt_file}"
    echo "..."
    echo "---"
    echo
    echo "Then Claude would output JSON which would be rendered and posted."
    exit 0
fi

# Run Claude Code to get JSON review
echo "Running Claude to generate review JSON..."
claude -p - \
    --dangerously-skip-permissions \
    --max-turns "${max_turns}" \
    --output-format json < "${prompt_file}" > "${output_dir}/claude-output.json" || true

# Extract metadata for CI output
claude_output="${output_dir}/claude-output.json"
if [ -f "${claude_output}" ]; then
    num_turns=$(jq -r '.num_turns // "unknown"' "${claude_output}")
    duration_ms=$(jq -r '.duration_ms // "unknown"' "${claude_output}")
    cost_usd=$(jq -r '.total_cost_usd // "unknown"' "${claude_output}")

    echo -e "${BLUE}Claude execution stats:${NC}"
    echo "  Turns: ${num_turns} / ${max_turns}"
    echo "  Duration: ${duration_ms}ms"
    echo "  Cost: \$${cost_usd}"

    ci_output "claude_turns" "${num_turns}"
    ci_output "claude_duration_ms" "${duration_ms}"
    ci_output "claude_cost_usd" "${cost_usd}"
fi

# Extract the JSON review from Claude's output
# Claude's response is in .result, and the JSON is in a code block
echo
echo -e "${YELLOW}Step 6: Extracting and validating review JSON...${NC}"

claude_result=$(jq -r '.result // empty' "${claude_output}")
if [ -z "${claude_result}" ]; then
    echo -e "${RED}Error: No result from Claude${NC}"
    ci_output "review_posted" "false"
    exit 1
fi

# Extract JSON from code block (between ```json and ```)
# Allow for whitespace variations in the markers
json_start='^[[:space:]]*```json[[:space:]]*$'
json_end='^[[:space:]]*```[[:space:]]*$'
review_json=$(echo "${claude_result}" | \
    sed -n "/${json_start}/,/${json_end}/p" | sed '1d;$d')

if [ -z "${review_json}" ]; then
    msg="Warning: No JSON code block found with standard markers"
    echo -e "${YELLOW}${msg}${NC}"
    echo "Attempting fallback extraction..."

    # Fallback: use Python for portable JSON extraction
    # This handles multiline JSON without requiring grep -P (PCRE)
    review_json=$(echo "${claude_result}" | python3 -c '
import sys
import re
import json

content = sys.stdin.read()

# Try to find a JSON object with summary and items fields
# Match from first { to last } that contains required fields
match = re.search(r"\{[^{}]*\"summary\"[^{}]*\"items\".*\}", content, re.DOTALL)
if match:
    # Validate it is parseable JSON
    try:
        candidate = match.group(0)
        json.loads(candidate)
        print(candidate)
    except json.JSONDecodeError:
        # Try to find balanced braces
        pass
' 2>/dev/null || true)

    if [ -z "${review_json}" ]; then
        msg="Error: Could not extract JSON from Claude's response"
        echo -e "${RED}${msg}${NC}"
        echo "Response was:"
        echo "${claude_result}" | head -50
        ci_output "review_posted" "false"
        exit 1
    fi
fi

# Save the extracted JSON
review_json_file="${output_dir}/review.json"
review_json_with_issues="${output_dir}/review-with-issues.json"
review_md_file="${output_dir}/review.md"
render_script="${topdir}/tools/render-review.py"
create_issues_script="${topdir}/tools/create-review-issues.py"

echo "${review_json}" > "${review_json_file}"
echo "Extracted review JSON to ${review_json_file}"

# Validate the JSON
echo "Validating JSON..."
if ! python3 "${render_script}" --validate "${review_json_file}"; then
    echo -e "${RED}Error: Review JSON failed validation${NC}"
    echo "JSON content:"
    cat "${review_json_file}"
    ci_output "review_posted" "false"
    exit 1
fi
echo -e "${GREEN}JSON validation passed${NC}"

# Create GitHub issues for actionable items
echo
echo -e "${YELLOW}Step 7: Creating GitHub issues for action items...${NC}"
if [ "${dry_run}" = true ]; then
    echo "Dry run mode - skipping issue creation"
    cp "${review_json_file}" "${review_json_with_issues}"
else
    python3 "${create_issues_script}" \
        "${review_json_file}" \
        "${review_json_with_issues}" \
        --pr "${pr_number}" || {
        echo -e "${YELLOW}Warning: Issue creation failed, continuing without issues${NC}"
        cp "${review_json_file}" "${review_json_with_issues}"
    }
fi

# Render to markdown (with embedded JSON for address-comments automation)
echo
echo -e "${YELLOW}Step 8: Rendering review to markdown...${NC}"
python3 "${render_script}" --embed-json \
    "${review_json_with_issues}" "${review_md_file}"
echo "Rendered review to ${review_md_file}"

# Post the review
echo
echo -e "${YELLOW}Step 9: Posting review to PR...${NC}"
gh pr review "${pr_number}" --comment --body "$(cat "${review_md_file}")"

echo
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}PR review complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo "Review JSON saved to: ${review_json_with_issues}"
echo "Review markdown saved to: ${review_md_file}"
ci_output "review_posted" "true"
ci_output "review_json_path" "${review_json_with_issues}"
