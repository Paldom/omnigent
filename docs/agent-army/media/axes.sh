#!/bin/bash
# axes.sh <port> — the four capability axes for the first harness
curl -s "localhost:$1/v1/harnesses" | jq -c '.data[0].capabilities | {skills, hooks, mcp, plan_mode}'
