#!/usr/bin/env python3
"""Create GitHub issues for actionable review items.

This script takes the review JSON and creates GitHub issues for items
with action='fix' or action='document'. The issues are linked to the PR
so they auto-close when the PR merges.

Usage:
    create-review-issues.py <input.json> <output.json> --pr NUMBER

The output JSON is the same as input but with 'issue_number' and 'issue_url'
fields added to each actionable item.

Environment:
    GITHUB_REPOSITORY: Repository in owner/repo format
    GH_TOKEN or GITHUB_TOKEN: GitHub token for API access
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    pr_number: int
) -> tuple[int, str] | None:
    """Create a GitHub issue and return (issue_number, issue_url).

    Returns None if issue creation fails.
    """
    # Build the gh command
    cmd = [
        'gh', 'issue', 'create',
        '--repo', repo,
        '--title', title,
        '--body', body,
    ]

    for label in labels:
        cmd.extend(['--label', label])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        # gh issue create outputs the issue URL
        issue_url = result.stdout.strip()
        # Extract issue number from URL
        issue_number = int(issue_url.rstrip('/').split('/')[-1])
        return issue_number, issue_url
    except subprocess.CalledProcessError as e:
        print(f'Warning: Failed to create issue: {e.stderr}', file=sys.stderr)
        return None
    except (ValueError, IndexError) as e:
        print(f'Warning: Failed to parse issue URL: {e}', file=sys.stderr)
        return None


def build_issue_body(item: dict[str, Any], pr_number: int) -> str:
    """Build the issue body from a review item."""
    lines = []

    lines.append(f'**From automated review of PR #{pr_number}**')
    lines.append('')

    if item.get('description'):
        lines.append('## Description')
        lines.append('')
        lines.append(item['description'])
        lines.append('')

    if item.get('location'):
        lines.append(f'**Location:** `{item["location"]}`')
        lines.append('')

    if item.get('suggestion'):
        lines.append('## Suggestion')
        lines.append('')
        lines.append(item['suggestion'])
        lines.append('')

    # Add link to close issue when PR merges
    lines.append('---')
    lines.append(f'*This issue will be automatically closed when PR #{pr_number} '
                 'is merged.*')
    lines.append('')
    lines.append(f'Closes #{pr_number} addresses this issue.')

    return '\n'.join(lines)


def get_labels_for_item(item: dict[str, Any]) -> list[str]:
    """Determine appropriate labels for an issue based on item metadata."""
    labels = ['automated-review']

    category = item.get('category', '')
    if category == 'security':
        labels.append('security')
    elif category == 'bug':
        labels.append('bug')
    elif category == 'documentation':
        labels.append('documentation')
    elif category == 'testing':
        labels.append('testing')

    severity = item.get('severity', '')
    if severity in ('critical', 'high'):
        labels.append('priority:high')
    elif severity == 'medium':
        labels.append('priority:medium')

    return labels


def main() -> None:
    if len(sys.argv) < 4 or '--pr' not in sys.argv:
        print(__doc__)
        sys.exit(1)

    # Parse arguments
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    pr_idx = sys.argv.index('--pr')
    pr_number = int(sys.argv[pr_idx + 1])

    # Get repository from environment
    repo = os.environ.get('GITHUB_REPOSITORY')
    if not repo:
        print('Error: GITHUB_REPOSITORY environment variable not set',
              file=sys.stderr)
        sys.exit(1)

    # Load review JSON
    with open(input_path) as f:
        review_data = json.load(f)

    # Track created issues
    issues_created = 0

    # Process each item
    for item in review_data.get('items', []):
        action = item.get('action', 'none')

        # Only create issues for actionable items
        if action not in ('fix', 'document'):
            continue

        # Skip if issue already exists for this item
        if item.get('issue_number'):
            print(f'Item {item["id"]}: Already has issue #{item["issue_number"]}')
            continue

        # Build issue title and body
        action_prefix = '[FIX]' if action == 'fix' else '[DOC]'
        title = f'{action_prefix} {item["title"]}'

        # Truncate title if too long (GitHub limit is 256)
        if len(title) > 200:
            title = title[:197] + '...'

        body = build_issue_body(item, pr_number)
        labels = get_labels_for_item(item)

        print(f'Creating issue for item {item["id"]}: {item["title"]}...')

        result = create_issue(repo, title, body, labels, pr_number)
        if result:
            issue_number, issue_url = result
            item['issue_number'] = issue_number
            item['issue_url'] = issue_url
            issues_created += 1
            print(f'  Created issue #{issue_number}: {issue_url}')
        else:
            print('  Failed to create issue')

    # Save updated JSON
    with open(output_path, 'w') as f:
        json.dump(review_data, f, indent=2)

    print(f'\nCreated {issues_created} issues')
    print(f'Updated review JSON saved to {output_path}')


if __name__ == '__main__':
    main()
