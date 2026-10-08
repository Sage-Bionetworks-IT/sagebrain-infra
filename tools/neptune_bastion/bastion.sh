#!/usr/bin/env bash
# Temporary direct access to Neptune (private subnets only) via an SSM-managed EC2.
# Runbook: docs/memory/runbooks/direct-neptune-access.md
#
#   bastion.sh <aws-profile> up                     # deploy the box (~3 min)
#   bastion.sh <aws-profile> graphs                 # named graphs + triple counts
#   bastion.sh <aws-profile> count  <graph-iri>
#   bastion.sh <aws-profile> query  '<sparql>' [timeout-s]
#   bastion.sh <aws-profile> update '<sparql>' [timeout-s]
#   bastion.sh <aws-profile> drop   <graph-iri> [batch-size]   # batched delete, prompts first
#   bastion.sh <aws-profile> down                   # destroy the box — always do this
set -euo pipefail

PROFILE=${1:?aws profile (sagebrain-dev | sagebrain)}
CMD=${2:?command}
shift 2
export AWS_PROFILE=$PROFILE
STACK=sagebrain-neptune-bastion
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
CDK_APP="$ROOT/.venv/bin/python $HERE/app.py"

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# sparql query|update '<statement>' [timeout] — run on the box, SigV4-signed by its role.
sparql() {
  local field=$1 stmt=$2 timeout=${3:-300} instance endpoint b64 remote params cmd_id status
  instance=$(output InstanceId)
  endpoint=$(output NeptuneEndpoint)
  b64=$(printf '%s' "$stmt" | base64 | tr -d '\n')
  remote=$(cat <<EOF
eval "\$(aws configure export-credentials --format env)"
printf '%s' '$b64' | base64 -d > /tmp/stmt.rq
curl -sS --max-time $timeout -w '\nHTTP %{http_code} in %{time_total}s\n' \
  --aws-sigv4 'aws:amz:${AWS_DEFAULT_REGION:-us-east-1}:neptune-db' \
  --user "\$AWS_ACCESS_KEY_ID:\$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: \$AWS_SESSION_TOKEN" \
  -H 'Accept: application/sparql-results+json' \
  --data-urlencode "$field@/tmp/stmt.rq" \
  https://$endpoint:8182/sparql
EOF
)
  params=$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":[sys.argv[2]]}))' \
    "$remote" "$((timeout + 60))")
  cmd_id=$(aws ssm send-command --instance-ids "$instance" --document-name AWS-RunShellScript \
    --parameters "$params" --timeout-seconds 60 --query Command.CommandId --output text)
  while :; do
    sleep 5
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$instance" \
      --query Status --output text 2>/dev/null || echo Pending)
    case $status in Pending | InProgress | Delayed) ;; *) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$instance" \
    --query StandardOutputContent --output text
}

count() {
  sparql query "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <$1> { ?s ?p ?o } }" |
    python3 -c 'import sys,re; print(re.search(r"\"value\" : \"(\d+)\"", sys.stdin.read()).group(1))'
}

case $CMD in
  up)
    (cd "$HERE" && cdk deploy --app "$CDK_APP" --profile "$PROFILE" --require-approval never)
    instance=$(output InstanceId)
    echo "Waiting for $instance to register with SSM..."
    until [ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$instance" \
      --query 'InstanceInformationList[0].PingStatus' --output text)" = Online ]; do sleep 10; done
    echo "Ready."
    ;;
  down)
    (cd "$HERE" && cdk destroy --app "$CDK_APP" --profile "$PROFILE" --force)
    ;;
  graphs)
    sparql query 'SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY DESC(?n)' 300 |
      python3 -c '
import json, sys
text = sys.stdin.read()
for b in json.loads(text[text.index("{"):text.rindex("}") + 1])["results"]["bindings"]:
    n, g = int(b["n"]["value"]), b["g"]["value"]
    print(f"{n:>14,}  {g}")'
    ;;
  count) count "${1:?graph iri}" ;;
  query | update) sparql "$CMD" "${1:?sparql}" "${2:-300}" ;;
  drop)
    graph=${1:?graph iri}
    batch=${2:-300000}
    n=$(count "$graph")
    echo "$graph has $n triples (profile $PROFILE)."
    read -r -p "Type the graph IRI to delete it: " confirm
    [ "$confirm" = "$graph" ] || { echo "Aborted."; exit 1; }
    # One DROP GRAPH over ~1M+ triples exceeds Neptune's 120s query timeout and rolls
    # back, so delete in LIMIT batches (300k ≈ 50s on prod serverless).
    while [ "$n" != 0 ]; do
      out=$(sparql update "DELETE { GRAPH <$graph> { ?s ?p ?o } } WHERE { { SELECT ?s ?p ?o WHERE { GRAPH <$graph> { ?s ?p ?o } } LIMIT $batch } }" 300)
      echo "$out" | grep -q 'HTTP 200' || { echo "$out"; exit 1; }
      n=$(count "$graph")
      echo "  $(echo "$out" | grep HTTP) — remaining $n"
    done
    echo "Done. Remember: bastion.sh $PROFILE down"
    ;;
  *) echo "Unknown command '$CMD'" >&2; exit 1 ;;
esac
