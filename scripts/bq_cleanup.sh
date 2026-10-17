#!/usr/bin/env bash
# Truncate every table in the given BigQuery datasets (keeps the tables and schemas).
# Usage: PROJECT_ID=... ./scripts/bq_cleanup.sh supplementary_data file_audit
#   DRY_RUN=1  only list the tables that would be truncated
#   FORCE=1    skip the confirmation prompt
set -uo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
DATASETS=("$@")
[[ ${#DATASETS[@]} -gt 0 ]] || { echo "Usage: $0 <dataset> [dataset...]"; exit 1; }

tables=()
for ds in "${DATASETS[@]}"; do
  # `bq ls` prints two header lines, then: tableId  Type  ...
  listing="$(bq --project_id="$PROJECT_ID" ls --max_results 1000 "${PROJECT_ID}:${ds}" 2>&1)" || { echo "Cannot list ${ds}: ${listing}"; exit 1; }
  while read -r name type _; do
    [[ "$type" == "TABLE" ]] && tables+=("${ds}.${name}")
  done < <(tail -n +3 <<<"$listing")
done

if [[ ${#tables[@]} -eq 0 ]]; then
  echo "No tables found in: ${DATASETS[*]}"
  exit 0
fi

echo "Project: ${PROJECT_ID}"
echo "Tables to truncate (all rows deleted, schemas kept):"
printf '  %s\n' "${tables[@]}"

if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "DRY_RUN=1: nothing truncated."
  exit 0
fi

if [[ "${FORCE:-}" != "1" ]]; then
  read -r -p "Type the project id (${PROJECT_ID}) to confirm: " answer
  [[ "$answer" == "$PROJECT_ID" ]] || { echo "Aborted."; exit 1; }
fi

failed=0
for t in "${tables[@]}"; do
  if out="$(bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --quiet \
              "TRUNCATE TABLE \`${PROJECT_ID}.${t}\`" 2>&1)"; then
    printf '  \033[32mTRUNCATED\033[0m  %s\n' "$t"
  else
    printf '  \033[31mFAILED\033[0m     %s\n' "$t"
    if grep -qi "streaming buffer" <<<"$out"; then
      echo "             rows are still in the streaming buffer (recent streaming inserts); retry in ~30-90 min"
    else
      sed 's/^/             /' <<<"$out" | head -5
    fi
    failed=$((failed + 1))
  fi
done

echo
echo "Done: $(( ${#tables[@]} - failed )) truncated, ${failed} failed"
[[ "$failed" -eq 0 ]]
