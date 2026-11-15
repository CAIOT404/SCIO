#!/bin/sh
# Print a repository's inventory, to compare before and after a transfer (#141 part 4b).
# Usage: sh scripts/dev/repo_inventory.sh OWNER/REPO
R="$1"
echo "repo: $(gh api "repos/$R" --jq '.full_name + " owner=" + .owner.type + " vis=" + .visibility + " default=" + .default_branch')"
echo "labels: $(gh api "repos/$R/labels?per_page=100" --paginate --jq '.[].name' | wc -l)"
echo "milestones (all): $(gh api "repos/$R/milestones?state=all&per_page=100" --paginate --jq '.[].number' | wc -l)"
echo "releases: $(gh api "repos/$R/releases?per_page=100" --paginate --jq '.[].tag_name' | wc -l)"
echo "tags: $(gh api "repos/$R/tags?per_page=100" --paginate --jq '.[].name' | wc -l)"
echo "issues+PRs (all): $(gh api "search/issues?q=repo:$R" --jq .total_count)"
echo "rulesets: $(gh api "repos/$R/rulesets" --jq '[.[] | .name + "#" + (.id|tostring) + ":" + .enforcement] | join(", ")')"
echo "actions variables: $(gh api "repos/$R/actions/variables" --jq .total_count)"
echo "actions secrets: $(gh api "repos/$R/actions/secrets" --jq .total_count)"
echo "workflows: $(gh api "repos/$R/actions/workflows" --jq '[.workflows[] | .name + "=" + .state] | join(", ")')"
echo "stars: $(gh api "repos/$R" --jq .stargazers_count)"
